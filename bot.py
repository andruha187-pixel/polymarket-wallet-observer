import os
import time
import sqlite3
import requests
import json
import csv
import logging
import threading
from datetime import datetime, timezone

# ============================================================
# НАСТРОЙКИ
# ============================================================

WALLET = "0xf3531b23b504cf0aed4ff21325232b2a2d496685"

API_URL = "https://data-api.polymarket.com/trades"

POLL_INTERVAL = 10
ERROR_RETRY_INTERVAL = 30

DB_FILE = "wallet_observer.db"
LOG_FILE = "polymarket_bot.log"

# Как часто отправлять полный пакет данных (в секундах)
AUTO_EXPORT_INTERVAL = 3600  # 1 час

# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)

logger = logging.getLogger("POLYMARKET_OBSERVER")


# ============================================================
# TELEGRAM API
# ============================================================

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=30
        )

        if response.ok:
            return True

        logger.error(f"Telegram ошибка: {response.status_code} {response.text}")

    except Exception as error:
        logger.error(f"Ошибка отправки сообщения в Telegram: {error}")

    return False


def send_telegram_file(file_path, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    try:
        with open(file_path, "rb") as file:
            response = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption
                },
                files={
                    "document": file
                },
                timeout=120
            )

        if response.ok:
            logger.info(f"Файл отправлен в Telegram: {file_path}")
            return True

        logger.error(f"Ошибка отправки файла: {response.status_code} {response.text}")

    except Exception as error:
        logger.error(f"Ошибка отправки файла: {error}")

    return False


# ============================================================
# БАЗА ДАННЫХ
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

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_condition_id
        ON trades(condition_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_timestamp
        ON trades(timestamp)
    """)

    connection.commit()
    connection.close()

    logger.info("База данных инициализирована")


# ============================================================
# ПОЛУЧЕНИЕ ПОСЛЕДНЕЙ СДЕЛКИ
# ============================================================

def get_last_timestamp():
    connection = get_connection()
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
# ПОЛУЧЕНИЕ СДЕЛОК POLYMARKET
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
# СОХРАНЕНИЕ СДЕЛКИ
# ============================================================

def save_trade(trade):
    timestamp = int(trade.get("timestamp", 0))

    datetime_utc = datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc
    ).isoformat()

    price = float(trade.get("price", 0))
    size = float(trade.get("size", 0))

    usdc_value = price * size

    transaction_hash = trade.get("transactionHash", "")

    trade_id = (
        f"{transaction_hash}_"
        f"{timestamp}_"
        f"{price}_"
        f"{size}_"
        f"{trade.get('side', '')}_"
        f"{trade.get('outcome', '')}"
    )

    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""
            INSERT INTO trades (
                trade_id, timestamp, datetime_utc, side, price,
                size, usdc_value, title, slug, outcome,
                outcome_index, condition_id, asset, transaction_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id, timestamp, datetime_utc,
            trade.get("side"), price, size, usdc_value,
            trade.get("title"), trade.get("slug"),
            trade.get("outcome"), trade.get("outcomeIndex"),
            trade.get("conditionId"), trade.get("asset"),
            transaction_hash
        ))

        connection.commit()
        is_new = True

    except sqlite3.IntegrityError:
        is_new = False

    connection.close()
    return is_new


# ============================================================
# НОРМАЛИЗАЦИЯ
# ============================================================

def normalize_outcome(outcome):
    if not outcome:
        return "UNKNOWN"

    outcome = str(outcome).strip().upper()

    if outcome == "UP":
        return "UP"

    if outcome == "DOWN":
        return "DOWN"

    return outcome


def normalize_side(side):
    if not side:
        return "UNKNOWN"

    return str(side).strip().upper()


# ============================================================
# ПОЛУЧЕНИЕ ВСЕХ СДЕЛОК РЫНКА
# ============================================================

def get_market_trades(condition_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id, timestamp, datetime_utc, side, price,
            size, usdc_value, title, slug, outcome,
            outcome_index, condition_id, asset, transaction_hash
        FROM trades
        WHERE condition_id = ?
        ORDER BY timestamp ASC, id ASC
    """, (condition_id,))

    rows = cursor.fetchall()
    connection.close()

    trades = []
    for row in rows:
        trades.append({
            "id": row[0],
            "timestamp": row[1],
            "datetime_utc": row[2],
            "side": row[3],
            "price": row[4],
            "size": row[5],
            "usdc_value": row[6],
            "title": row[7],
            "slug": row[8],
            "outcome": row[9],
            "outcome_index": row[10],
            "condition_id": row[11],
            "asset": row[12],
            "transaction_hash": row[13]
        })

    return trades


# ============================================================
# АНАЛИЗ ПОЗИЦИИ
# ============================================================

def analyze_position(trades):
    position = {
        "UP": {"bought": 0.0, "sold": 0.0, "buy_volume": 0.0, "sell_volume": 0.0},
        "DOWN": {"bought": 0.0, "sold": 0.0, "buy_volume": 0.0, "sell_volume": 0.0}
    }

    for trade in trades:
        outcome = normalize_outcome(trade.get("outcome"))
        side = normalize_side(trade.get("side"))
        size = float(trade.get("size", 0))
        value = float(trade.get("usdc_value", 0))

        if outcome not in ["UP", "DOWN"]:
            continue

        if side == "BUY":
            position[outcome]["bought"] += size
            position[outcome]["buy_volume"] += value
        elif side == "SELL":
            position[outcome]["sold"] += size
            position[outcome]["sell_volume"] += value

    result = {}
    for outcome in ["UP", "DOWN"]:
        bought = position[outcome]["bought"]
        sold = position[outcome]["sold"]
        remaining = max(0.0, bought - sold)

        average_buy = (position[outcome]["buy_volume"] / bought) if bought > 0 else 0
        average_sell = (position[outcome]["sell_volume"] / sold) if sold > 0 else 0

        if sold == 0 and bought > 0:
            behavior = "ACCUMULATION"
        elif 0 < sold < bought:
            behavior = "PARTIAL_CLOSE"
        elif sold >= bought and bought > 0:
            behavior = "FULL_CLOSE"
        else:
            behavior = "NO_POSITION"

        result[outcome] = {
            "bought": round(bought, 6),
            "sold": round(sold, 6),
            "remaining": round(remaining, 6),
            "buy_volume": round(position[outcome]["buy_volume"], 6),
            "sell_volume": round(position[outcome]["sell_volume"], 6),
            "average_buy_price": round(average_buy, 6),
            "average_sell_price": round(average_sell, 6),
            "behavior": behavior
        }

    return result


# ============================================================
# АНАЛИЗ ПОВЕДЕНИЯ
# ============================================================

def analyze_behavior(trades, position):
    outcomes = [normalize_outcome(t.get("outcome")) for t in trades if normalize_outcome(t.get("outcome")) in ["UP", "DOWN"]]

    switches = sum(1 for i in range(1, len(outcomes)) if outcomes[i] != outcomes[i - 1])

    buy_count = sum(1 for t in trades if normalize_side(t.get("side")) == "BUY")
    sell_count = sum(1 for t in trades if normalize_side(t.get("side")) == "SELL")

    up_remaining = position["UP"]["remaining"]
    down_remaining = position["DOWN"]["remaining"]

    if sell_count == 0 and buy_count > 0:
        strategy_type = "BUY_AND_HOLD"
    elif sell_count > 0 and (position["UP"]["sold"] > 0 or position["DOWN"]["sold"] > 0):
        strategy_type = "ACTIVE_TRADING"
    else:
        strategy_type = "UNKNOWN"

    if up_remaining > 0 and down_remaining > 0:
        final_position = "HEDGED_UP_AND_DOWN"
    elif up_remaining > 0:
        final_position = "HOLDING_UP"
    elif down_remaining > 0:
        final_position = "HOLDING_DOWN"
    else:
        final_position = "FULLY_CLOSED"

    return {
        "total_trades": len(trades),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "switches_between_up_down": switches,
        "strategy_type": strategy_type,
        "final_position": final_position,
        "holds_position_until_end": (sell_count == 0 and buy_count > 0),
        "has_partial_closing": (
            (0 < position["UP"]["sold"] < position["UP"]["bought"]) or
            (0 < position["DOWN"]["sold"] < position["DOWN"]["bought"])
        ),
        "has_full_closing": (
            (position["UP"]["sold"] >= position["UP"]["bought"] > 0) or
            (position["DOWN"]["sold"] >= position["DOWN"]["bought"] > 0)
        )
    }


# ============================================================
# ПОЛНЫЙ АНАЛИЗ РЫНКА
# ============================================================

def analyze_market(condition_id):
    trades = get_market_trades(condition_id)
    if not trades:
        return None

    position = analyze_position(trades)
    behavior = analyze_behavior(trades, position)
    title = trades[0].get("title", "Unknown market")

    timestamps = [int(trade.get("timestamp", 0)) for trade in trades]
    first_timestamp = min(timestamps)
    last_timestamp = max(timestamps)

    return {
        "condition_id": condition_id,
        "title": title,
        "first_trade_timestamp": first_timestamp,
        "last_trade_timestamp": last_timestamp,
        "first_trade_utc": datetime.fromtimestamp(first_timestamp, tz=timezone.utc).isoformat(),
        "last_trade_utc": datetime.fromtimestamp(last_timestamp, tz=timezone.utc).isoformat(),
        "position": position,
        "behavior": behavior,
        "trades": trades
    }


# ============================================================
# ЭКСПОРТ ВСЕХ ДАННЫХ В JSON
# ============================================================

def export_json():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT DISTINCT condition_id
        FROM trades
        WHERE condition_id IS NOT NULL AND condition_id != ''
    """)

    rows = cursor.fetchall()
    connection.close()

    markets = []
    for row in rows:
        analysis = analyze_market(row[0])
        if analysis:
            markets.append(analysis)

    export_data = {
        "export_created_utc": datetime.now(timezone.utc).isoformat(),
        "wallet": WALLET,
        "markets_count": len(markets),
        "markets": markets
    }

    filename = f"wallet_analysis_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')}.json"

    with open(filename, "w", encoding="utf-8") as file:
        json.dump(export_data, file, ensure_ascii=False, indent=2)

    logger.info(f"JSON экспорт создан: {filename}")
    return filename


# ============================================================
# ЭКСПОРТ ВСЕХ СДЕЛОК В CSV
# ============================================================

def export_csv():
    filename = f"trades_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')}.csv"

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("SELECT * FROM trades ORDER BY timestamp ASC")
    rows = cursor.fetchall()
    columns = [description[0] for description in cursor.description]

    connection.close()

    with open(filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)

    logger.info(f"CSV экспорт создан: {filename}")
    return filename


# ============================================================
# ОТПРАВКА ПОЛНОГО ПАКЕТА ДАННЫХ
# ============================================================

def export_and_send():
    logger.info("Создание полного пакета данных...")
    json_file = export_json()
    csv_file = export_csv()

    send_telegram_message(
        "📦 ПОЛНЫЙ ПАКЕТ ДАННЫХ ГОТОВ\n\n"
        "Файлы содержат всю историю кошелька.\n"
        "JSON — полный анализ рынков и последовательность сделок.\n"
        "CSV — все сделки в табличном формате."
    )

    send_telegram_file(json_file, "📊 Полный JSON-анализ кошелька")
    send_telegram_file(csv_file, "📋 Все сделки CSV")


def start_auto_exporter():
    def export_loop():
        while True:
            time.sleep(AUTO_EXPORT_INTERVAL)
            try:
                export_and_send()
            except Exception as error:
                logger.error(f"Ошибка в авто-экспорте: {error}")

    thread = threading.Thread(target=export_loop, daemon=True)
    thread.start()


# ============================================================
# ПОИСК НОВЫХ СДЕЛОК
# ============================================================

def process_new_trades(trades, first_run=False):
    trades.sort(key=lambda x: int(x.get("timestamp", 0)))

    new_trades = []
    for trade in trades:
        if save_trade(trade):
            new_trades.append(trade)

    if not new_trades:
        logger.info("[INFO] Нет новых сделок")
        return

    logger.info(f"Новых сделок: {len(new_trades)}")

    # При первом запуске мы только заполняем БД тишиной
    if first_run:
        logger.info("Первичный импорт истории завершен.")
        return

    for trade in new_trades:
        title = trade.get("title", "Unknown market")
        side = normalize_side(trade.get("side"))
        outcome = normalize_outcome(trade.get("outcome"))
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        value = price * size

        msg = (
            f"🚨 **НОВАЯ СДЕЛКА!**\n\n"
            f"📌 **Рынок:** {title}\n"
            f"📊 **Действие:** {side} {outcome}\n"
            f"💵 **Цена:** ${price:.4f}\n"
            f"🔢 **Объем:** {size:.2f}\n"
            f"💰 **Сумма:** ${value:.2f} USDC"
        )

        send_telegram_message(msg)


# ============================================================
# ГЛАВНЫЙ ЦИКЛ
# ============================================================

def main():
    init_database()
    start_auto_exporter()

    send_telegram_message("🚀 Наблюдатель Polymarket запущен!")

    first_run = True

    while True:
        try:
            trades = get_trades()
            process_new_trades(trades, first_run=first_run)
            first_run = False

        except Exception as error:
            logger.error(f"Ошибка в главном цикле: {error}")
            time.sleep(ERROR_RETRY_INTERVAL)
            continue

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
                
