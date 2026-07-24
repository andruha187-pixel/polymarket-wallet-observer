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
ERROR_RETRY_INTERVAL = 30
DB_FILE = "wallet_observer.db"

# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ============================================================
# БАЗА ДАННЫХ
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

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }

    try:
        response = requests.post(url, json=payload, timeout=20)
        if not response.ok:
            print("[TELEGRAM ERROR]", response.status_code, response.text)
    except Exception as error:
        print("[TELEGRAM ERROR]", error)

# ============================================================
# ПОЛУЧЕНИЕ ПОСЛЕДНЕЙ СДЕЛКИ
# ============================================================

def get_last_timestamp():
    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    cursor.execute("SELECT MAX(timestamp) FROM trades")
    result = cursor.fetchone()

    connection.close()

    if result and result[0]:
        return int(result[0])

    return 0

# ============================================================
# ПОЛУЧЕНИЕ СДЕЛОК
# ============================================================

def get_trades():
    params = {
        "user": WALLET,
        "limit": 100,
        "takerOnly": "false"
    }

    response = requests.get(API_URL, params=params, timeout=30)
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
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    price = float(trade.get("price", 0))
    size = float(trade.get("size", 0))
    usdc_value = price * size
    transaction_hash = trade.get("transactionHash", "")

    trade_id = f"{transaction_hash}_{timestamp}_{price}_{size}_{trade.get('outcome', '')}"

    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    try:
        cursor.execute("""
            INSERT INTO trades (
                trade_id, timestamp, datetime_utc, side,
                price, size, usdc_value, title, slug,
                outcome, outcome_index, condition_id, asset, transaction_hash
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
            transaction_hash
        ))
        connection.commit()
        is_new = True
    except sqlite3.IntegrityError:
        is_new = False

    connection.close()
    return is_new

# ============================================================
# ФОРМАТ ОДНОЙ СДЕЛКИ
# ============================================================

def format_trade(trade):
    timestamp = int(trade.get("timestamp", 0))
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    price = float(trade.get("price", 0))
    size = float(trade.get("size", 0))
    value = price * size
    side = trade.get("side", "UNKNOWN")
    outcome = trade.get("outcome", "UNKNOWN")
    title = trade.get("title", "Unknown market")

    return (
        "🔔 НОВАЯ СДЕЛКА\n\n"
        f"📊 {title}\n\n"
        f"➡️ {side}\n\n"
        f"🎯 {outcome}\n\n"
        f"💵 Цена: ${price:.4f}\n"
        f"📦 Размер: {size:.2f}\n"
        f"💰 Объём: ${value:.2f}\n\n"
        f"🕒 {dt} UTC\n\n"
        f"👛 Кошелёк:\n{WALLET}"
    )

# ============================================================
# ПОЛУЧЕНИЕ СДЕЛОК ОДНОГО РЫНКА
# ============================================================

def get_market_trades(condition_id):
    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            timestamp, datetime_utc, side, price, size, usdc_value, title, outcome, condition_id
        FROM trades
        WHERE condition_id = ?
        ORDER BY timestamp ASC, id ASC
    """, (condition_id,))

    rows = cursor.fetchall()
    connection.close()

    trades = []
    for row in rows:
        trades.append({
            "timestamp": row[0],
            "datetime_utc": row[1],
            "side": row[2],
            "price": row[3],
            "size": row[4],
            "usdc_value": row[5],
            "title": row[6],
            "outcome": row[7],
            "condition_id": row[8]
        })

    return trades

# ============================================================
# НОРМАЛИЗАЦИЯ НАЗВАНИЯ СТОРОНЫ
# ============================================================

def normalize_outcome(outcome):
    if not outcome:
        return "UNKNOWN"

    outcome = str(outcome).strip().lower()

    if outcome == "up":
        return "UP"
    if outcome == "down":
        return "DOWN"

    return outcome.upper()

# ============================================================
# АНАЛИЗ ОДНОГО РЫНКА
# ============================================================

def analyze_market(condition_id, send_telegram_alert=False):
    trades = get_market_trades(condition_id)
    if not trades:
        return

    title = trades[0].get("title", "Unknown market")
    up_trades = []
    down_trades = []
    total_invested = 0.0

    for trade in trades:
        outcome = normalize_outcome(trade.get("outcome"))
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        value = price * size

        total_invested += value

        if outcome == "UP":
            up_trades.append(trade)
        elif outcome == "DOWN":
            down_trades.append(trade)

    up_size = sum(float(trade.get("size", 0)) for trade in up_trades)
    down_size = sum(float(trade.get("size", 0)) for trade in down_trades)

    up_invested = sum(float(trade.get("usdc_value", 0)) for trade in up_trades)
    down_invested = sum(float(trade.get("usdc_value", 0)) for trade in down_trades)

    up_average = up_invested / up_size if up_size > 0 else 0
    down_average = down_invested / down_size if down_size > 0 else 0

    result_if_up = up_size - total_invested
    result_if_down = down_size - total_invested

    all_prices = [float(trade.get("price", 0)) for trade in trades]
    min_price = min(all_prices) if all_prices else 0
    max_price = max(all_prices) if all_prices else 0

    switches = 0
    previous_outcome = None
    longest_streak = 0
    current_streak = 0
    current_outcome = None

    for trade in trades:
        outcome = normalize_outcome(trade.get("outcome"))

        if previous_outcome is not None and outcome != previous_outcome:
            switches += 1
        previous_outcome = outcome

        if outcome == current_outcome:
            current_streak += 1
        else:
            current_outcome = outcome
            current_streak = 1

        if current_streak > longest_streak:
            longest_streak = current_streak

    report = [
        "",
        "📈 АНАЛИТИЧЕСКИЙ РЕЖИМ",
        "",
        f"📊 {title}",
        "",
        f"🆔 Condition ID: {condition_id}",
        "",
        f"💰 ВСЕГО СДЕЛОК: {len(trades)}",
        f"💵 ВСЕГО ВЛОЖЕНО: ${total_invested:.2f}",
        "",
        "🟢 UP",
        f"Сделок: {len(up_trades)}",
        f"Количество: {up_size:.2f}",
        f"Вложено: ${up_invested:.2f}",
        f"Средняя цена: ${up_average:.4f}",
        f"Выплата при победе: ${up_size:.2f}",
        f"Результат при победе UP: ${result_if_up:.2f}",
        "",
        "🔴 DOWN",
        f"Сделок: {len(down_trades)}",
        f"Количество: {down_size:.2f}",
        f"Вложено: ${down_invested:.2f}",
        f"Средняя цена: ${down_average:.4f}",
        f"Выплата при победе: ${down_size:.2f}",
        f"Результат при победе DOWN: ${result_if_down:.2f}",
        "",
        "⚖️ СООТНОШЕНИЕ СТОРОН",
        "",
        f"UP контрактов: {up_size:.2f}",
        f"DOWN контрактов: {down_size:.2f}"
    ]

    if up_size > 0 and down_size > 0:
        total_contracts = up_size + down_size
        report.append(f"UP доля: {up_size / total_contracts * 100:.2f}%")
        report.append(f"DOWN доля: {down_size / total_contracts * 100:.2f}%")
        combined_average = total_invested / total_contracts
        report.append("")
        report.append(f"Общая средняя цена: ${combined_average:.4f}")

    report.extend([
        "",
        "📊 ПОВЕДЕНИЕ КОШЕЛЬКА",
        "",
        f"Переключений UP/DOWN: {switches}",
        f"Максимальная серия одной стороны: {longest_streak} сделок",
        f"Минимальная цена покупки: ${min_price:.4f}",
        f"Максимальная цена покупки: ${max_price:.4f}",
        "",
        "🔎 ПОЛНАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ",
        ""
    ])

    for index, trade in enumerate(trades, start=1):
        timestamp = int(trade.get("timestamp", 0))
        time_string = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%H:%M:%S")
        outcome = normalize_outcome(trade.get("outcome"))
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        value = price * size

        report.append(f"{index}. {time_string} | {outcome} | ${price:.4f} × {size:.2f} = ${value:.2f}")

    report_text = "\n".join(report)
    print(report_text)

    if send_telegram_alert and len(report_text) <= 4000:
        send_telegram(report_text)

# ============================================================
# АНАЛИЗ ВСЕХ РЫНКОВ
# ============================================================

def analyze_all_markets():
    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT DISTINCT condition_id
        FROM trades
        WHERE condition_id IS NOT NULL
        AND condition_id != ''
        ORDER BY condition_id
    """)

    rows = cursor.fetchall()
    connection.close()

    print("\n================================================")
    print("📚 АНАЛИЗ ВСЕХ СОБРАННЫХ РЫНКОВ")
    print("================================================")

    for row in rows:
        analyze_market(row[0], send_telegram_alert=False)

# ============================================================
# ГЛАВНЫЙ ЦИКЛ
# ============================================================

def main():
    print("========================================")
    print("POLYMARKET WALLET ANALYTICS OBSERVER")
    print("========================================")
    print(f"Wallet: {WALLET}\n")
    print("📊 ANALYTICS MODE: ENABLED\n")
    print("Бот будет собирать полную историю каждого рынка для анализа стратегии.\n")

    init_database()
    print("[OK] Database initialized")

    first_run = (get_last_timestamp() == 0)

    while True:
        try:
            trades = get_trades()
            trades.sort(key=lambda x: int(x.get("timestamp", 0)))

            new_trades = []
            for trade in trades:
                if save_trade(trade):
                    new_trades.append(trade)

            if new_trades:
                print(f"\n🆕 НОВЫХ СДЕЛОК: {len(new_trades)}")
                affected_markets = set()

                for trade in new_trades:
                    condition_id = trade.get("conditionId")
                    if condition_id:
                        affected_markets.add(condition_id)

                    message = format_trade(trade)
                    print(message)

                    if not first_run:
                        send_telegram(message)

                # Анализируем рынки, по которым пришли новые сделки
                for condition_id in affected_markets:
                    analyze_market(condition_id, send_telegram_alert=(not first_run))
            else:
                print("[INFO] Нет новых сделок")

            first_run = False
            time.sleep(POLL_INTERVAL)

        except Exception as error:
            print(f"[ERROR] {error}")
            time.sleep(ERROR_RETRY_INTERVAL)

if __name__ == "__main__":
    main()
    
