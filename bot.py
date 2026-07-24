import os
import time
import sqlite3
import requests
from datetime import datetime, timezone


# ============================================================
# НАСТРОЙКИ
# ============================================================

WALLET = "0xf3531b23b504cf0aed4ff21325232b2a2d496685"

API_URL = "https://data-api.polymarket.com/trades"

POLL_INTERVAL = 10

DB_FILE = "wallet_observer.db"


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# DATABASE
# ============================================================

def init_database():

    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            trade_id TEXT UNIQUE,

            timestamp INTEGER,

            datetime_utc TEXT,

            side TEXT,

            price REAL,

            size REAL,

            usdc_value REAL,

            title TEXT,

            slug TEXT,

            outcome TEXT,

            outcome_index INTEGER,

            condition_id TEXT,

            asset TEXT,

            transaction_hash TEXT

        )
    """)

    connection.commit()

    connection.close()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:

        print("[TELEGRAM] Token not configured")

        return


    if not TELEGRAM_CHAT_ID:

        print("[TELEGRAM] Chat ID not configured")

        return


    url = (

        f"https://api.telegram.org/bot"

        f"{TELEGRAM_BOT_TOKEN}/sendMessage"

    )


    payload = {

        "chat_id": TELEGRAM_CHAT_ID,

        "text": message,

        "disable_web_page_preview": True

    }


    try:

        response = requests.post(

            url,

            json=payload,

            timeout=20

        )


        if not response.ok:

            print(

                f"[TELEGRAM ERROR] "

                f"{response.status_code} "

                f"{response.text}"

            )


    except Exception as error:

        print(

            f"[TELEGRAM ERROR] {error}"

        )


# ============================================================
# GET LAST SAVED TRADE
# ============================================================

def get_last_timestamp():

    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()


    cursor.execute("""

        SELECT MAX(timestamp)

        FROM trades

    """)


    result = cursor.fetchone()


    connection.close()


    if result and result[0]:

        return int(result[0])


    return 0


# ============================================================
# GET TRADES
# ============================================================

def get_trades():

    params = {

        "user": WALLET,

        "limit": 100,

        "takerOnly": "false"

    }


    response = requests.get(

        API_URL,

        params=params,

        timeout=30

    )


    response.raise_for_status()


    data = response.json()


    if not isinstance(data, list):

        return []


    return data


# ============================================================
# SAVE TRADE
# ============================================================

def save_trade(trade):

    timestamp = int(

        trade.get(

            "timestamp",

            0

        )

    )


    dt = datetime.fromtimestamp(

        timestamp,

        tz=timezone.utc

    ).isoformat()


    price = float(

        trade.get(

            "price",

            0

        )

    )


    size = float(

        trade.get(

            "size",

            0

        )

    )


    usdc_value = price * size


    trade_id = (

        trade.get(

            "transactionHash",

            ""

        )

        + "_"

        + str(timestamp)

        + "_"

        + str(price)

        + "_"

        + str(size)

    )


    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()


    try:

        cursor.execute("""

            INSERT INTO trades (

                trade_id,

                timestamp,

                datetime_utc,

                side,

                price,

                size,

                usdc_value,

                title,

                slug,

                outcome,

                outcome_index,

                condition_id,

                asset,

                transaction_hash

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        """, (

            trade_id,

            timestamp,

            dt,

            trade.get("side"),

            price,

            size,

            usdc_value,

            trade.get("title"),

            trade.get("slug"),

            trade.get("outcome"),

            trade.get("outcomeIndex"),

            trade.get("conditionId"),

            trade.get("asset"),

            trade.get("transactionHash")

        ))


        connection.commit()


        is_new = True


    except sqlite3.IntegrityError:

        is_new = False


    connection.close()


    return is_new


# ============================================================
# FORMAT TRADE
# ============================================================

def format_trade(trade):

    timestamp = int(

        trade.get(

            "timestamp",

            0

        )

    )


    dt = datetime.fromtimestamp(

        timestamp,

        tz=timezone.utc

    ).strftime(

        "%Y-%m-%d %H:%M:%S"

    )


    price = float(

        trade.get(

            "price",

            0

        )

    )


    size = float(

        trade.get(

            "size",

            0

        )

    )


    value = price * size


    side = trade.get(

        "side",

        "UNKNOWN"

    )


    outcome = trade.get(

        "outcome",

        "UNKNOWN"

    )


    title = trade.get(

        "title",

        "Unknown market"

    )


    return f"""

🔔 НОВАЯ СДЕЛКА


📊 {title}


➡️ {side}

🎯 {outcome}


💵 Цена: ${price:.4f}

📦 Размер: {size:.2f}

💰 Объём: ${value:.2f}


🕒 {dt} UTC


👛 Кошелёк:

{WALLET}

"""


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    print(

        "========================================"

    )


    print(

        "POLYMARKET WALLET OBSERVER"

    )


    print(

        "========================================"

    )


    print(

        f"Wallet: {WALLET}"

    )


    init_database()


    print(

        "[OK] Database initialized"

    )


    # При первом запуске сохраняем текущую историю
    # без отправки старых сделок в Telegram.

    first_run = (

        get_last_timestamp() == 0

    )


    while True:

        try:

            trades = get_trades()


            trades.sort(

                key=lambda x: int(

                    x.get(

                        "timestamp",

                        0

                    )

                )

            )


            new_count = 0


            for trade in trades:

                if save_trade(trade):

                    new_count += 1


                    # Формируем сообщение сделки

                    message = format_trade(

                        trade

                    )


                    # ПОДРОБНЫЙ ЛОГ В RENDER

                    print(

                        f"\n"

                        f"[NEW TRADE]\n"

                        f"{message}"

                    )


                    # Отправляем только новые сделки
                    # после первого запуска.

                    if not first_run:

                        send_telegram(

                            message

                        )


            if new_count > 0:

                print(

                    f"[NEW] {new_count} trades"

                )

            else:

                print(

                    "[INFO] No new trades"

                )


            first_run = False


            time.sleep(

                POLL_INTERVAL

            )


        except Exception as error:

            print(

                f"[ERROR] {error}"

            )


            time.sleep(

                30

            )


if __name__ == "__main__":

    main()
