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

def get_connection():

    return sqlite3.connect(
        DB_FILE,
        timeout=30
    )


def init_database():

    connection = get_connection()

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

                "[TELEGRAM ERROR]",

                response.status_code,

                response.text

            )


    except Exception as error:

        print(

            "[TELEGRAM ERROR]",

            error

        )


# ============================================================
# API
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


    if not isinstance(

        data,

        list

    ):

        return []


    return data


# ============================================================
# TRADE ID
# ============================================================

def make_trade_id(trade):

    transaction_hash = str(

        trade.get(

            "transactionHash",

            ""

        )

    )


    timestamp = str(

        trade.get(

            "timestamp",

            ""

        )

    )


    price = str(

        trade.get(

            "price",

            ""

        )

    )


    size = str(

        trade.get(

            "size",

            ""

        )

    )


    outcome = str(

        trade.get(

            "outcome",

            ""

        )

    )


    asset = str(

        trade.get(

            "asset",

            ""

        )


    )


    return (

        transaction_hash

        + "_"

        + timestamp

        + "_"

        + price

        + "_"

        + size

        + "_"

        + outcome

        + "_"

        + asset

    )


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


    trade_id = make_trade_id(

        trade

    )


    connection = get_connection()

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
# GET ALL TRADES OF MARKET
# ============================================================

def get_market_trades(

    condition_id,

    title

):

    connection = get_connection()

    cursor = connection.cursor()


    if condition_id:

        cursor.execute("""

            SELECT

                timestamp,

                datetime_utc,

                side,

                price,

                size,

                usdc_value,

                outcome,

                condition_id

            FROM trades

            WHERE condition_id = ?

            ORDER BY timestamp ASC, id ASC

        """, (

            condition_id,

        ))

    else:

        cursor.execute("""

            SELECT

                timestamp,

                datetime_utc,

                side,

                price,

                size,

                usdc_value,

                outcome,

                condition_id

            FROM trades

            WHERE title = ?

            ORDER BY timestamp ASC, id ASC

        """, (

            title,

        ))


    rows = cursor.fetchall()


    connection.close()


    return rows


# ============================================================
# ANALYSIS
# ============================================================

def analyze_market(

    condition_id,

    title

):

    rows = get_market_trades(

        condition_id,

        title

    )


    if not rows:

        return ""


    up_trades = []

    down_trades = []


    total_invested = 0.0


    for row in rows:

        (

            timestamp,

            datetime_utc,

            side,

            price,

            size,

            value,

            outcome,

            saved_condition_id

        ) = row


        total_invested += value


        outcome_lower = str(

            outcome

        ).lower()


        if outcome_lower == "up":

            up_trades.append(

                (

                    price,

                    size,

                    value

                )

            )


        elif outcome_lower == "down":

            down_trades.append(

                (

                    price,

                    size,

                    value

                )

            )


    def calculate_side(

        trades

    ):

        if not trades:

            return {

                "size": 0.0,

                "cost": 0.0,

                "average": 0.0,

                "payout": 0.0,

                "profit": 0.0

            }


        total_size = sum(

            item[1]

            for item in trades

        )


        total_cost = sum(

            item[2]

            for item in trades

        )


        average_price = (

            total_cost

            / total_size

            if total_size > 0

            else 0

        )


        payout = total_size


        profit = payout - total_cost


        return {

            "size": total_size,

            "cost": total_cost,

            "average": average_price,

            "payout": payout,

            "profit": profit

        }


    up = calculate_side(

        up_trades

    )


    down = calculate_side(

        down_trades

    )


    if up["cost"] > 0 and down["cost"] > 0:

        combined_average = (

            up["cost"]

            + down["cost"]

        ) / (

            up["size"]

            + down["size"]

        )


    else:

        combined_average = 0


    lines = []


    lines.append(

        "📈 АНАЛИЗ РЫНКА"

    )


    lines.append("")


    lines.append(

        f"📊 {title}"

    )


    lines.append("")


    lines.append(

        f"💰 Всего вложено: "

        f"${total_invested:.2f}"

    )


    lines.append("")


    lines.append(

        "🟢 UP"

    )


    lines.append(

        f"Покупок: {len(up_trades)}"

    )


    lines.append(

        f"Количество: {up['size']:.2f}"

    )


    lines.append(

        f"Вложено: ${up['cost']:.2f}"

    )


    lines.append(

        f"Средняя цена: "

        f"${up['average']:.4f}"

    )


    lines.append(

        f"Выплата при победе: "

        f"${up['payout']:.2f}"

    )


    lines.append(

        f"Результат при победе: "

        f"${up['profit']:.2f}"

    )


    lines.append("")


    lines.append(

        "🔴 DOWN"

    )


    lines.append(

        f"Покупок: {len(down_trades)}"

    )


    lines.append(

        f"Количество: {down['size']:.2f}"

    )


    lines.append(

        f"Вложено: ${down['cost']:.2f}"

    )


    lines.append(

        f"Средняя цена: "

        f"${down['average']:.4f}"

    )


    lines.append(

        f"Выплата при победе: "

        f"${down['payout']:.2f}"

    )


    lines.append(

        f"Результат при победе: "

        f"${down['profit']:.2f}"

    )


    lines.append("")


    if (

        up["cost"] > 0

        and down["cost"] > 0

    ):

        lines.append(

            "⚖️ ОБЕ СТОРО
