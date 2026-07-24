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

    return sqlite3.connect(DB_FILE)


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

    print("[OK] Database initialized")


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

        print(f"[TELEGRAM ERROR] {error}")


# ============================================================
# GET TRADES FROM POLYMARKET
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
# TRADE ID
# ============================================================

def create_trade_id(trade):

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

    asset = str(

        trade.get(

            "asset",

            ""

        )

    )

    side = str(

        trade.get(

            "side",

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

    return (

        transaction_hash

        + "_"

        + timestamp

        + "_"

        + asset

        + "_"

        + side

        + "_"

        + price

        + "_"

        + size

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

    trade_id = create_trade_id(trade)

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

    finally:

        connection.close()

    return is_new


# ============================================================
# GET ALL TRADES OF ONE MARKET
# ============================================================

def get_market_trades(condition_id):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute("""

        SELECT

            timestamp,

            datetime_utc,

            side,

            price,

            size,

            usdc_value,

            title,

            outcome,

            asset,

            transaction_hash

        FROM trades

        WHERE condition_id = ?

        ORDER BY timestamp ASC, id ASC

    """, (

        condition_id,

    ))

    rows = cursor.fetchall()

    connection.close()

    return rows


# ============================================================
# MARKET ANALYSIS
# ============================================================

def analyze_market(condition_id):

    trades = get_market_trades(condition_id)

    if not trades:

        return None

    title = trades[0][6]

    up_trades = []

    down_trades = []

    total_invested = 0

    total_up = 0

    total_down = 0

    for trade in trades:

        (

            timestamp,

            datetime_utc,

            side,

            price,

            size,

            usdc_value,

            title,

            outcome,

            asset,

            transaction_hash

        ) = trade

        if side != "BUY":

            continue

        total_invested += usdc_value

        if outcome.lower() == "up":

            up_trades.append(

                {

                    "price": price,

                    "size": size,

                    "value": usdc_value,

                    "timestamp": timestamp

                }

            )

            total_up += usdc_value

        elif outcome.lower() == "down":

            down_trades.append(

                {

                    "price": price,

                    "size": size,

                    "value": usdc_value,

                    "timestamp": timestamp

                }

            )

            total_down += usdc_value

    up_size = sum(

        item["size"]

        for item in up_trades

    )

    down_size = sum(

        item["size"]

        for item in down_trades

    )

    up_average = (

        total_up / up_size

        if up_size > 0

        else 0

    )

    down_average = (

        total_down / down_size

        if down_size > 0

        else 0

    )

    return {

        "condition_id": condition_id,

        "title": title,

        "trades": trades,

        "up_trades": up_trades,

        "down_trades": down_trades,

        "total_up": total_up,

        "total_down": total_down,

        "total_invested": total_invested,

        "up_size": up_size,

        "down_size": down_size,

        "up_average": up_average,

        "down_average": down_average

    }


# ============================================================
# FORMAT MARKET ANALYSIS
# ============================================================

def format_market_analysis(analysis):

    if not analysis:

        return "No analysis available"

    title = analysis["title"]

    up_trades = analysis["up_trades"]

    down_trades = analysis["down_trades"]

    total_up = analysis["total_up"]

    total_down = analysis["total_down"]

    total_invested = analysis["total_invested"]

    up_average = analysis["up_average"]

    down_average = analysis["down_average"]

    lines = []

    lines.append("")

    lines.append("=" * 70)

    lines.append("📊 MARKET ANALYSIS")

    lines.append("=" * 70)

    lines.append("")

    lines.append(f"📌 {title}")

    lines.append("")

    lines.append("🟢 UP POSITIONS")

    lines.append("-" * 40)

    if up_trades:

        for item in up_trades:

            lines.append(

                f"BUY UP: "

                f"{item['size']:.2f} "

                f"@ ${item['price']:.4f} "

                f"= ${item['value']:.2f}"

            )

    else:

        lines.append("No UP trades")

    lines.append("")

    lines.append(

        f"TOTAL UP: ${total_up:.2f}"

    )

    lines.append(

        f"UP SIZE: {analysis['up_size']:.2f}"

    )

    lines.append(

        f"UP AVG PRICE: ${up_average:.4f}"

    )

    lines.append("")

    lines.append("🔴 DOWN POSITIONS")

    lines.append("-" * 40)

    if down_trades:

        for item in down_trades:

            lines.append(

                f"BUY DOWN: "

                f"{item['size']:.2f} "

                f"@ ${item['price']:.4f} "

                f"= ${item['value']:.2f}"

            )

    else:

        lines.append("No DOWN trades")

    lines.append("")

    lines.append(

        f"TOTAL DOWN: ${total_down:.2f}"

    )

    lines.append(

        f"DOWN SIZE: {analysis['down_size']:.2f}"

    )

    lines.append(

        f"DOWN AVG PRICE: ${down_average:.4f}"

    )

    lines.append("")

    lines.append("💰 TOTAL INVESTED")

    lines.append("-" * 40)

    lines.append(

        f"${total_invested:.2f}"

    )

    lines.append("")

    lines.append("🧠 TRADE SEQUENCE")

    lines.append("-" * 40)

    for trade in analysis["trades"]:

        (

            timestamp,

            datetime_utc,

            side,

            price,

            size,

            usdc_value,

            title,

            outcome,

            asset,

            transaction_hash

        ) = trade

        lines.append(

            f"{datetime_utc} | "

            f"{side} {outcome} | "

            f"${price:.4f} | "

            f"{size:.2f} tokens | "

            f"${usdc_value:.2f}"

        )

    lines.append("")

    lines.append("=" * 70)

    lines.append("")

    return "\n".join(lines)


# ============================================================
# FORMAT SINGLE TRADE
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

        "UNKNOWN
