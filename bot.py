import os
import re
import csv
import json
import time
import sqlite3
import logging
import threading
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

import requests

# ============================================================
# НАСТРОЙКИ
# ============================================================

WALLET = "0xf3531b23b504cf0aed4ff21325232b2a2d496685"

DATA_API_URL = "https://data-api.polymarket.com/trades"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"

POLL_INTERVAL = 5
ERROR_RETRY_INTERVAL = 20
AUTO_EXPORT_INTERVAL = 3600

# Data API позволяет limit до 10000.
# Большой лимит снижает вероятность пропуска сделок активного кошелька.
TRADES_LIMIT = 2000

DB_FILE = "wallet_observer.db"
LOG_FILE = "polymarket_bot.log"
EXPORT_DIR = "exports"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

NY_TZ = ZoneInfo("America/New_York")

# Один requests.Session быстрее и стабильнее множества отдельных соединений.
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "polymarket-wallet-research-observer/2.0"})

DB_LOCK = threading.RLock()
EXPORT_LOCK = threading.Lock()
STOP_EVENT = threading.Event()

os.makedirs(EXPORT_DIR, exist_ok=True)

# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)

logger = logging.getLogger("POLYMARKET_OBSERVER")

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def utc_iso(timestamp: Optional[float] = None) -> str:
    if timestamp is None:
        timestamp = time.time()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def to_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_side(value: Any) -> str:
    return str(value or "UNKNOWN").strip().upper()


def normalize_outcome(value: Any) -> str:
    text = str(value or "UNKNOWN").strip().upper()
    if text in {"UP", "DOWN"}:
        return text
    return text


def detect_coin(title: str, slug: str = "") -> str:
    text = f"{title} {slug}".lower()
    if "bitcoin" in text or re.search(r"\bbtc\b", text):
        return "BTC"
    if "ethereum" in text or re.search(r"\beth\b", text):
        return "ETH"
    return "UNKNOWN"


def binance_symbol_for_coin(coin: str) -> Optional[str]:
    if coin == "BTC":
        return "BTCUSDT"
    if coin == "ETH":
        return "ETHUSDT"
    return None


def parse_jsonish(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_market_times_from_title(title: str) -> tuple[Optional[int], Optional[int]]:
    """
    Пример:
    Bitcoin Up or Down - July 25, 4:40AM-4:45AM ET

    Возвращает UTC timestamp начала и окончания рынка.
    """
    pattern = re.compile(
        r"-\s*([A-Za-z]+)\s+(\d{1,2}),\s*"
        r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*"
        r"(\d{1,2}:\d{2}\s*[AP]M)\s*ET",
        re.IGNORECASE,
    )
    match = pattern.search(title or "")
    if not match:
        return None, None

    month_name, day_text, start_text, end_text = match.groups()
    year = datetime.now(timezone.utc).year

    try:
        start_local = datetime.strptime(
            f"{month_name} {day_text} {year} {start_text.upper().replace(' ', '')}",
            "%B %d %Y %I:%M%p",
        ).replace(tzinfo=NY_TZ)

        end_local = datetime.strptime(
            f"{month_name} {day_text} {year} {end_text.upper().replace(' ', '')}",
            "%B %d %Y %I:%M%p",
        ).replace(tzinfo=NY_TZ)

        if end_local <= start_local:
            # На случай перехода через полночь.
            end_local = end_local.replace(day=end_local.day + 1)

        return int(start_local.timestamp()), int(end_local.timestamp())
    except (ValueError, OverflowError):
        return None, None


def derive_trade_id(trade: dict[str, Any]) -> str:
    # В ответе Data API нет отдельного уникального trade id.
    # Включаем максимально возможный набор полей.
    parts = [
        str(trade.get("transactionHash", "")),
        str(trade.get("timestamp", "")),
        str(trade.get("asset", "")),
        str(trade.get("conditionId", "")),
        str(trade.get("side", "")),
        str(trade.get("outcome", "")),
        str(trade.get("price", "")),
        str(trade.get("size", "")),
    ]
    return "|".join(parts)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен")
        return False

    # Telegram sendMessage ограничен примерно 4096 символами.
    chunks = [message[i:i + 3900] for i in range(0, len(message), 3900)] or [""]

    for chunk in chunks:
        try:
            response = HTTP.post(
                telegram_url("sendMessage"),
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if not response.ok:
                logger.error(
                    "Telegram sendMessage: %s %s",
                    response.status_code,
                    response.text,
                )
                return False
        except requests.RequestException as error:
            logger.error("Ошибка Telegram sendMessage: %s", error)
            return False

    return True


def send_telegram_file(file_path: str, caption: str = "") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram не настроен")
        return False

    try:
        with open(file_path, "rb") as file:
            response = HTTP.post(
                telegram_url("sendDocument"),
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
                files={"document": (os.path.basename(file_path), file)},
                timeout=180,
            )

        if response.ok:
            logger.info("Файл отправлен: %s", file_path)
            return True

        logger.error(
            "Telegram sendDocument: %s %s",
            response.status_code,
            response.text,
        )
    except (OSError, requests.RequestException) as error:
        logger.error("Ошибка отправки файла %s: %s", file_path, error)

    return False


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_FILE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def add_column_if_missing(
    cursor: sqlite3.Cursor,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        row[1]
        for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_database() -> None:
    with DB_LOCK:
        connection = get_connection()
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                timestamp INTEGER,
                datetime_utc TEXT,
                detected_timestamp REAL,
                detected_utc TEXT,
                api_delay_sec REAL,
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
                transaction_hash TEXT,
                coin TEXT,
                market_start_timestamp INTEGER,
                market_end_timestamp INTEGER,
                seconds_from_market_start REAL,
                seconds_to_market_end REAL,
                binance_symbol TEXT,
                binance_price_at_detection REAL,
                binance_change_from_market_start REAL,
                token_midpoint_at_detection REAL,
                token_best_bid_at_detection REAL,
                token_best_ask_at_detection REAL,
                opposite_token_id TEXT,
                opposite_midpoint_at_detection REAL,
                opposite_best_bid_at_detection REAL,
                opposite_best_ask_at_detection REAL,
                market_metadata_found INTEGER DEFAULT 0
            )
        """)

        # Миграция существующей БД старой версии.
        new_columns = {
            "detected_timestamp": "REAL",
            "detected_utc": "TEXT",
            "api_delay_sec": "REAL",
            "coin": "TEXT",
            "market_start_timestamp": "INTEGER",
            "market_end_timestamp": "INTEGER",
            "seconds_from_market_start": "REAL",
            "seconds_to_market_end": "REAL",
            "binance_symbol": "TEXT",
            "binance_price_at_detection": "REAL",
            "binance_change_from_market_start": "REAL",
            "token_midpoint_at_detection": "REAL",
            "token_best_bid_at_detection": "REAL",
            "token_best_ask_at_detection": "REAL",
            "opposite_token_id": "TEXT",
            "opposite_midpoint_at_detection": "REAL",
            "opposite_best_bid_at_detection": "REAL",
            "opposite_best_ask_at_detection": "REAL",
            "market_metadata_found": "INTEGER DEFAULT 0",
        }
        for column, declaration in new_columns.items():
            add_column_if_missing(cursor, "trades", column, declaration)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_metadata (
                condition_id TEXT PRIMARY KEY,
                slug TEXT,
                title TEXT,
                token_up_id TEXT,
                token_down_id TEXT,
                outcomes_json TEXT,
                clob_token_ids_json TEXT,
                market_start_timestamp INTEGER,
                market_end_timestamp INTEGER,
                fetched_timestamp REAL,
                raw_json TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_start_prices (
                condition_id TEXT PRIMARY KEY,
                coin TEXT,
                binance_symbol TEXT,
                market_start_timestamp INTEGER,
                sampled_timestamp REAL,
                sampled_utc TEXT,
                start_price REAL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS observer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                datetime_utc TEXT,
                event_type TEXT,
                message TEXT
            )
        """)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_detected ON trades(detected_timestamp)"
        )

        connection.commit()
        connection.close()

    logger.info("База данных инициализирована")


def record_event(event_type: str, message: str) -> None:
    with DB_LOCK:
        connection = get_connection()
        connection.execute(
            """
            INSERT INTO observer_events(timestamp, datetime_utc, event_type, message)
            VALUES (?, ?, ?, ?)
            """,
            (time.time(), utc_iso(), event_type, message),
        )
        connection.commit()
        connection.close()


# ============================================================
# ВНЕШНИЕ ДАННЫЕ
# ============================================================

def get_trades() -> list[dict[str, Any]]:
    response = HTTP.get(
        DATA_API_URL,
        params={
            "user": WALLET,
            "limit": TRADES_LIMIT,
            "offset": 0,
            "takerOnly": "false",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def get_binance_price(symbol: Optional[str]) -> Optional[float]:
    if not symbol:
        return None

    try:
        response = HTTP.get(
            BINANCE_PRICE_URL,
            params={"symbol": symbol},
            timeout=8,
        )
        response.raise_for_status()
        return to_float(response.json().get("price"), None)
    except (requests.RequestException, ValueError, TypeError) as error:
        logger.warning("Binance %s: %s", symbol, error)
        return None


def get_clob_book(token_id: Optional[str]) -> dict[str, Optional[float]]:
    result: dict[str, Optional[float]] = {
        "midpoint": None,
        "best_bid": None,
        "best_ask": None,
    }
    if not token_id:
        return result

    try:
        response = HTTP.get(
            f"{CLOB_URL}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if response.status_code == 404:
            return result
        response.raise_for_status()
        data = response.json()

        bids = data.get("bids") or []
        asks = data.get("asks") or []

        bid_prices = [
            to_float(level.get("price"), None)
            for level in bids
            if isinstance(level, dict)
        ]
        ask_prices = [
            to_float(level.get("price"), None)
            for level in asks
            if isinstance(level, dict)
        ]

        bid_prices = [p for p in bid_prices if p is not None]
        ask_prices = [p for p in ask_prices if p is not None]

        best_bid = max(bid_prices) if bid_prices else None
        best_ask = min(ask_prices) if ask_prices else None

        result["best_bid"] = best_bid
        result["best_ask"] = best_ask

        if best_bid is not None and best_ask is not None:
            result["midpoint"] = (best_bid + best_ask) / 2
        else:
            midpoint_response = HTTP.get(
                f"{CLOB_URL}/midpoint",
                params={"token_id": token_id},
                timeout=8,
            )
            if midpoint_response.ok:
                result["midpoint"] = to_float(
                    midpoint_response.json().get("mid_price"),
                    None,
                )

    except (requests.RequestException, ValueError, TypeError) as error:
        logger.warning("CLOB book %s: %s", token_id, error)

    return result


def extract_market_metadata(
    market: dict[str, Any],
    fallback_title: str,
    fallback_slug: str,
    condition_id: str,
) -> dict[str, Any]:
    outcomes = parse_jsonish(market.get("outcomes")) or []
    token_ids = parse_jsonish(market.get("clobTokenIds")) or []

    if not isinstance(outcomes, list):
        outcomes = []
    if not isinstance(token_ids, list):
        token_ids = []

    token_up_id = None
    token_down_id = None

    for index, outcome in enumerate(outcomes):
        if index >= len(token_ids):
            break
        normalized = normalize_outcome(outcome)
        if normalized == "UP":
            token_up_id = str(token_ids[index])
        elif normalized == "DOWN":
            token_down_id = str(token_ids[index])

    # Для этих бинарных рынков Gamma обычно возвращает токены в порядке outcomes.
    if token_up_id is None and len(token_ids) >= 1:
        token_up_id = str(token_ids[0])
    if token_down_id is None and len(token_ids) >= 2:
        token_down_id = str(token_ids[1])

    title = (
        market.get("question")
        or market.get("title")
        or fallback_title
        or "Unknown market"
    )
    slug = market.get("slug") or fallback_slug or ""

    parsed_start, parsed_end = parse_market_times_from_title(fallback_title or title)

    start_timestamp = parsed_start
    end_timestamp = parsed_end

    # Если Gamma даёт даты, используем их как запасной вариант.
    for field, target in (("startDate", "start"), ("endDate", "end")):
        raw = market.get(field)
        if not raw:
            continue
        try:
            timestamp = int(
                datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            )
            if target == "start" and start_timestamp is None:
                start_timestamp = timestamp
            if target == "end" and end_timestamp is None:
                end_timestamp = timestamp
        except ValueError:
            pass

    return {
        "condition_id": condition_id,
        "slug": slug,
        "title": title,
        "token_up_id": token_up_id,
        "token_down_id": token_down_id,
        "outcomes": outcomes,
        "clob_token_ids": token_ids,
        "market_start_timestamp": start_timestamp,
        "market_end_timestamp": end_timestamp,
        "raw": market,
    }


def fetch_market_metadata(
    condition_id: str,
    slug: str,
    title: str,
) -> Optional[dict[str, Any]]:
    queries = []

    if slug:
        queries.append({"slug": slug, "limit": 10})

    # Этот фильтр поддерживается Gamma на практике; если ответ пустой,
    # ниже остаётся поиск по slug.
    if condition_id:
        queries.append({"condition_ids": condition_id, "limit": 10})

    for params in queries:
        try:
            response = HTTP.get(GAMMA_MARKETS_URL, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                continue

            for market in data:
                if not isinstance(market, dict):
                    continue
                market_condition = str(
                    market.get("conditionId")
                    or market.get("condition_id")
                    or ""
                )
                market_slug = str(market.get("slug") or "")

                if condition_id and market_condition == condition_id:
                    return extract_market_metadata(
                        market, title, slug, condition_id
                    )
                if slug and market_slug == slug:
                    return extract_market_metadata(
                        market, title, slug, condition_id
                    )
        except (requests.RequestException, ValueError, TypeError) as error:
            logger.warning("Gamma metadata %s: %s", condition_id, error)

    # Даже без Gamma сохраняем время, извлечённое из названия.
    start_ts, end_ts = parse_market_times_from_title(title)
    if start_ts is not None or end_ts is not None:
        return {
            "condition_id": condition_id,
            "slug": slug,
            "title": title,
            "token_up_id": None,
            "token_down_id": None,
            "outcomes": [],
            "clob_token_ids": [],
            "market_start_timestamp": start_ts,
            "market_end_timestamp": end_ts,
            "raw": {},
        }

    return None


def get_cached_market_metadata(
    condition_id: str,
    slug: str,
    title: str,
) -> Optional[dict[str, Any]]:
    with DB_LOCK:
        connection = get_connection()
        row = connection.execute(
            "SELECT * FROM market_metadata WHERE condition_id = ?",
            (condition_id,),
        ).fetchone()
        connection.close()

    if row:
        return {
            "condition_id": row["condition_id"],
            "slug": row["slug"],
            "title": row["title"],
            "token_up_id": row["token_up_id"],
            "token_down_id": row["token_down_id"],
            "outcomes": parse_jsonish(row["outcomes_json"]) or [],
            "clob_token_ids": parse_jsonish(row["clob_token_ids_json"]) or [],
            "market_start_timestamp": row["market_start_timestamp"],
            "market_end_timestamp": row["market_end_timestamp"],
            "raw": parse_jsonish(row["raw_json"]) or {},
        }

    metadata = fetch_market_metadata(condition_id, slug, title)
    if not metadata:
        return None

    with DB_LOCK:
        connection = get_connection()
        connection.execute(
            """
            INSERT OR REPLACE INTO market_metadata (
                condition_id, slug, title, token_up_id, token_down_id,
                outcomes_json, clob_token_ids_json,
                market_start_timestamp, market_end_timestamp,
                fetched_timestamp, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                condition_id,
                metadata.get("slug"),
                metadata.get("title"),
                metadata.get("token_up_id"),
                metadata.get("token_down_id"),
                json.dumps(metadata.get("outcomes", []), ensure_ascii=False),
                json.dumps(
                    metadata.get("clob_token_ids", []),
                    ensure_ascii=False,
                ),
                metadata.get("market_start_timestamp"),
                metadata.get("market_end_timestamp"),
                time.time(),
                json.dumps(metadata.get("raw", {}), ensure_ascii=False),
            ),
        )
        connection.commit()
        connection.close()

    return metadata


def get_market_start_binance_price(
    condition_id: str,
    coin: str,
    symbol: Optional[str],
    market_start_timestamp: Optional[int],
    current_price: Optional[float],
) -> Optional[float]:
    if not market_start_timestamp or not symbol:
        return None

    with DB_LOCK:
        connection = get_connection()
        row = connection.execute(
            """
            SELECT start_price
            FROM market_start_prices
            WHERE condition_id = ?
            """,
            (condition_id,),
        ).fetchone()
        connection.close()

    if row:
        return to_float(row["start_price"], None)

    # Если рынок только начался, первая наблюдаемая цена служит приближением.
    # Это явно помечается временем sample, поэтому не выдаётся за точный open.
    if current_price is None:
        return None

    with DB_LOCK:
        connection = get_connection()
        connection.execute(
            """
            INSERT OR IGNORE INTO market_start_prices (
                condition_id, coin, binance_symbol,
                market_start_timestamp, sampled_timestamp,
                sampled_utc, start_price
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                condition_id,
                coin,
                symbol,
                market_start_timestamp,
                time.time(),
                utc_iso(),
                current_price,
            ),
        )
        connection.commit()
        connection.close()

    return current_price


# ============================================================
# СОХРАНЕНИЕ СДЕЛОК И СНИМКА РЫНКА
# ============================================================

def build_enriched_trade(trade: dict[str, Any]) -> dict[str, Any]:
    detected_timestamp = time.time()
    trade_timestamp = to_int(trade.get("timestamp"))
    title = str(trade.get("title") or "Unknown market")
    slug = str(trade.get("slug") or "")
    condition_id = str(trade.get("conditionId") or "")
    asset = str(trade.get("asset") or "")
    outcome = normalize_outcome(trade.get("outcome"))
    coin = detect_coin(title, slug)

    metadata = get_cached_market_metadata(condition_id, slug, title)

    market_start = (
        metadata.get("market_start_timestamp")
        if metadata else None
    )
    market_end = (
        metadata.get("market_end_timestamp")
        if metadata else None
    )

    seconds_from_start = (
        trade_timestamp - market_start
        if market_start is not None else None
    )
    seconds_to_end = (
        market_end - trade_timestamp
        if market_end is not None else None
    )

    symbol = binance_symbol_for_coin(coin)
    binance_price = get_binance_price(symbol)

    market_start_price = get_market_start_binance_price(
        condition_id,
        coin,
        symbol,
        market_start,
        binance_price,
    )
    binance_change = (
        binance_price - market_start_price
        if binance_price is not None and market_start_price is not None
        else None
    )

    traded_token_id = asset or None
    opposite_token_id = None

    if metadata:
        up_id = metadata.get("token_up_id")
        down_id = metadata.get("token_down_id")
        if outcome == "UP":
            traded_token_id = traded_token_id or up_id
            opposite_token_id = down_id
        elif outcome == "DOWN":
            traded_token_id = traded_token_id or down_id
            opposite_token_id = up_id

    traded_book = get_clob_book(traded_token_id)
    opposite_book = get_clob_book(opposite_token_id)

    price = to_float(trade.get("price"), 0.0) or 0.0
    size = to_float(trade.get("size"), 0.0) or 0.0

    return {
        "trade_id": derive_trade_id(trade),
        "timestamp": trade_timestamp,
        "datetime_utc": utc_iso(trade_timestamp),
        "detected_timestamp": detected_timestamp,
        "detected_utc": utc_iso(detected_timestamp),
        "api_delay_sec": detected_timestamp - trade_timestamp,
        "side": normalize_side(trade.get("side")),
        "price": price,
        "size": size,
        "usdc_value": price * size,
        "title": title,
        "slug": slug,
        "outcome": outcome,
        "outcome_index": trade.get("outcomeIndex"),
        "condition_id": condition_id,
        "asset": asset,
        "transaction_hash": str(trade.get("transactionHash") or ""),
        "coin": coin,
        "market_start_timestamp": market_start,
        "market_end_timestamp": market_end,
        "seconds_from_market_start": seconds_from_start,
        "seconds_to_market_end": seconds_to_end,
        "binance_symbol": symbol,
        "binance_price_at_detection": binance_price,
        "binance_change_from_market_start": binance_change,
        "token_midpoint_at_detection": traded_book["midpoint"],
        "token_best_bid_at_detection": traded_book["best_bid"],
        "token_best_ask_at_detection": traded_book["best_ask"],
        "opposite_token_id": opposite_token_id,
        "opposite_midpoint_at_detection": opposite_book["midpoint"],
        "opposite_best_bid_at_detection": opposite_book["best_bid"],
        "opposite_best_ask_at_detection": opposite_book["best_ask"],
        "market_metadata_found": 1 if metadata and metadata.get("raw") else 0,
    }


def save_enriched_trade(enriched: dict[str, Any]) -> bool:
    columns = list(enriched.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)

    with DB_LOCK:
        connection = get_connection()
        try:
            connection.execute(
                f"""
                INSERT INTO trades ({column_sql})
                VALUES ({placeholders})
                """,
                tuple(enriched[column] for column in columns),
            )
            connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            connection.close()


def trade_exists(trade_id: str) -> bool:
    with DB_LOCK:
        connection = get_connection()
        row = connection.execute(
            "SELECT 1 FROM trades WHERE trade_id = ? LIMIT 1",
            (trade_id,),
        ).fetchone()
        connection.close()
    return row is not None


# ============================================================
# АНАЛИТИКА
# ============================================================

def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def get_market_trades(condition_id: str) -> list[dict[str, Any]]:
    with DB_LOCK:
        connection = get_connection()
        rows = connection.execute(
            """
            SELECT *
            FROM trades
            WHERE condition_id = ?
            ORDER BY timestamp ASC, id ASC
            """,
            (condition_id,),
        ).fetchall()
        connection.close()
    return rows_to_dicts(rows)


def analyze_position(trades: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}

    for outcome in ("UP", "DOWN"):
        relevant = [
            trade for trade in trades
            if normalize_outcome(trade.get("outcome")) == outcome
        ]

        buys = [
            trade for trade in relevant
            if normalize_side(trade.get("side")) == "BUY"
        ]
        sells = [
            trade for trade in relevant
            if normalize_side(trade.get("side")) == "SELL"
        ]

        bought = sum(to_float(t.get("size"), 0.0) or 0.0 for t in buys)
        sold = sum(to_float(t.get("size"), 0.0) or 0.0 for t in sells)
        buy_volume = sum(
            to_float(t.get("usdc_value"), 0.0) or 0.0 for t in buys
        )
        sell_volume = sum(
            to_float(t.get("usdc_value"), 0.0) or 0.0 for t in sells
        )

        remaining = bought - sold
        average_buy = buy_volume / bought if bought else 0.0
        average_sell = sell_volume / sold if sold else 0.0

        if bought and not sold:
            behavior = "ACCUMULATION"
        elif bought and 0 < sold < bought:
            behavior = "PARTIAL_CLOSE"
        elif bought and sold >= bought:
            behavior = "FULL_CLOSE"
        else:
            behavior = "NO_POSITION"

        result[outcome] = {
            "bought": round(bought, 8),
            "sold": round(sold, 8),
            "remaining": round(remaining, 8),
            "buy_volume": round(buy_volume, 8),
            "sell_volume": round(sell_volume, 8),
            "average_buy_price": round(average_buy, 8),
            "average_sell_price": round(average_sell, 8),
            "behavior": behavior,
        }

    return result


def analyze_market(condition_id: str) -> Optional[dict[str, Any]]:
    trades = get_market_trades(condition_id)
    if not trades:
        return None

    position = analyze_position(trades)
    sides = [normalize_side(t.get("side")) for t in trades]
    outcomes = [
        normalize_outcome(t.get("outcome"))
        for t in trades
        if normalize_outcome(t.get("outcome")) in {"UP", "DOWN"}
    ]

    sell_count = sum(side == "SELL" for side in sides)
    switches = sum(
        outcomes[index] != outcomes[index - 1]
        for index in range(1, len(outcomes))
    )

    first_trade = trades[0]
    last_trade = trades[-1]

    buy_total = sum(
        to_float(t.get("usdc_value"), 0.0) or 0.0
        for t in trades
        if normalize_side(t.get("side")) == "BUY"
    )
    sell_total = sum(
        to_float(t.get("usdc_value"), 0.0) or 0.0
        for t in trades
        if normalize_side(t.get("side")) == "SELL"
    )

    return {
        "condition_id": condition_id,
        "title": first_trade.get("title"),
        "coin": first_trade.get("coin"),
        "market_start_timestamp": first_trade.get("market_start_timestamp"),
        "market_end_timestamp": first_trade.get("market_end_timestamp"),
        "first_trade_timestamp": first_trade.get("timestamp"),
        "last_trade_timestamp": last_trade.get("timestamp"),
        "first_entry_delay_sec": first_trade.get("seconds_from_market_start"),
        "last_trade_seconds_to_end": last_trade.get("seconds_to_market_end"),
        "total_trades": len(trades),
        "buy_count": sum(side == "BUY" for side in sides),
        "sell_count": sell_count,
        "switches_between_outcomes": switches,
        "buy_total_usdc": round(buy_total, 8),
        "sell_total_usdc": round(sell_total, 8),
        "net_cash_spent_usdc": round(buy_total - sell_total, 8),
        "position": position,
        "holds_without_sell_in_observed_data": sell_count == 0,
        "trades": trades,
    }


# ============================================================
# ЭКСПОРТ
# ============================================================

def export_table_to_csv(
    cursor: sqlite3.Cursor,
    query: str,
    filename: str,
) -> str:
    cursor.execute(query)
    rows = cursor.fetchall()
    columns = [description[0] for description in cursor.description]

    with open(filename, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)

    return filename


def export_and_send(manual: bool = False) -> None:
    if not EXPORT_LOCK.acquire(blocking=False):
        logger.info("Экспорт уже выполняется")
        return

    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        folder = os.path.join(EXPORT_DIR, f"wallet_research_{stamp}")
        os.makedirs(folder, exist_ok=True)

        trades_csv = os.path.join(folder, "trades_enriched.csv")
        metadata_csv = os.path.join(folder, "market_metadata.csv")
        start_prices_csv = os.path.join(folder, "market_start_prices.csv")
        events_csv = os.path.join(folder, "observer_events.csv")
        analysis_json = os.path.join(folder, "markets_analysis.json")
        db_copy = os.path.join(folder, "wallet_observer.db")

        with DB_LOCK:
            connection = get_connection()
            cursor = connection.cursor()

            export_table_to_csv(
                cursor,
                "SELECT * FROM trades ORDER BY timestamp ASC, id ASC",
                trades_csv,
            )
            export_table_to_csv(
                cursor,
                "SELECT * FROM market_metadata ORDER BY market_start_timestamp ASC",
                metadata_csv,
            )
            export_table_to_csv(
                cursor,
                "SELECT * FROM market_start_prices ORDER BY market_start_timestamp ASC",
                start_prices_csv,
            )
            export_table_to_csv(
                cursor,
                "SELECT * FROM observer_events ORDER BY timestamp ASC",
                events_csv,
            )

            condition_rows = cursor.execute(
                """
                SELECT DISTINCT condition_id
                FROM trades
                WHERE condition_id IS NOT NULL AND condition_id != ''
                ORDER BY condition_id
                """
            ).fetchall()

            # Корректная копия SQLite во время работы процесса.
            backup_connection = sqlite3.connect(db_copy)
            connection.backup(backup_connection)
            backup_connection.close()
            connection.close()

        markets = []
        for row in condition_rows:
            condition_id = row[0]
            analysis = analyze_market(condition_id)
            if analysis:
                markets.append(analysis)

        export_data = {
            "export_created_utc": utc_iso(),
            "wallet": WALLET,
            "important_note": (
                "Binance and CLOB snapshot fields are captured when the observer "
                "detects a trade, not necessarily at the exact original trade time. "
                "Use api_delay_sec to assess the delay."
            ),
            "markets_count": len(markets),
            "trades_count": sum(m["total_trades"] for m in markets),
            "markets": markets,
        }

        with open(analysis_json, "w", encoding="utf-8") as file:
            json.dump(export_data, file, ensure_ascii=False, indent=2)

        zip_path = os.path.join(
            EXPORT_DIR,
            f"wallet_research_{stamp}.zip",
        )

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in (
                trades_csv,
                metadata_csv,
                start_prices_csv,
                events_csv,
                analysis_json,
                db_copy,
                LOG_FILE,
            ):
                if os.path.exists(path):
                    archive.write(path, arcname=os.path.basename(path))

        with DB_LOCK:
            connection = get_connection()
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS trades_count,
                    COUNT(DISTINCT condition_id) AS markets_count,
                    SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS sells_count
                FROM trades
                """
            ).fetchone()
            connection.close()

        caption = (
            "Пакет для анализа стратегии\n"
            f"Сделок: {counts['trades_count'] or 0}\n"
            f"Рынков: {counts['markets_count'] or 0}\n"
            f"SELL: {counts['sells_count'] or 0}\n"
            f"Режим: {'ручной' if manual else 'автоматический'}"
        )

        send_telegram_file(zip_path, caption)
        logger.info("Полный пакет создан: %s", zip_path)

    except Exception:
        logger.exception("Ошибка экспорта")
    finally:
        EXPORT_LOCK.release()


def automatic_export_loop() -> None:
    while not STOP_EVENT.wait(AUTO_EXPORT_INTERVAL):
        export_and_send(manual=False)


# ============================================================
# TELEGRAM-КОМАНДЫ
# ============================================================

def status_message() -> str:
    with DB_LOCK:
        connection = get_connection()
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS trades_count,
                COUNT(DISTINCT condition_id) AS markets_count,
                SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) AS buys_count,
                SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS sells_count,
                MAX(timestamp) AS last_trade_timestamp
            FROM trades
            """
        ).fetchone()
        connection.close()

    last_trade = (
        utc_iso(row["last_trade_timestamp"])
        if row["last_trade_timestamp"] else "нет"
    )

    return (
        "📊 СТАТУС НАБЛЮДАТЕЛЯ\n\n"
        f"Сделок: {row['trades_count'] or 0}\n"
        f"Рынков: {row['markets_count'] or 0}\n"
        f"BUY: {row['buys_count'] or 0}\n"
        f"SELL: {row['sells_count'] or 0}\n"
        f"Последняя сделка UTC: {last_trade}\n\n"
        "/export — получить ZIP\n"
        "/status — этот статус\n"
        "/help — команды"
    )


def telegram_command_loop() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Команды Telegram отключены: нет TELEGRAM_BOT_TOKEN")
        return

    offset = 0

    while not STOP_EVENT.is_set():
        try:
            response = HTTP.get(
                telegram_url("getUpdates"),
                params={"offset": offset, "timeout": 25},
                timeout=35,
            )
            response.raise_for_status()
            payload = response.json()

            if not payload.get("ok"):
                logger.warning("getUpdates вернул ok=false: %s", payload)
                STOP_EVENT.wait(5)
                continue

            for update in payload.get("result", []):
                offset = int(update["update_id"]) + 1
                message = update.get("message") or {}
                text = str(message.get("text") or "").strip().lower()
                chat_id = str((message.get("chat") or {}).get("id") or "")

                if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text in {"/start", "/help"}:
                    send_telegram_message(status_message())
                elif text == "/status":
                    send_telegram_message(status_message())
                elif text in {"/export", "/file"}:
                    send_telegram_message("📦 Формирую ZIP-пакет...")
                    threading.Thread(
                        target=export_and_send,
                        kwargs={"manual": True},
                        daemon=True,
                    ).start()

        except requests.RequestException as error:
            logger.error("Telegram getUpdates: %s", error)
            STOP_EVENT.wait(10)
        except Exception:
            logger.exception("Ошибка обработчика Telegram")
            STOP_EVENT.wait(10)


# ============================================================
# ОБРАБОТКА НОВЫХ СДЕЛОК
# ============================================================

def format_trade_message(trade: dict[str, Any]) -> str:
    delay = trade.get("api_delay_sec")
    entry_delay = trade.get("seconds_from_market_start")
    to_end = trade.get("seconds_to_market_end")

    def fmt(value: Any, digits: int = 2) -> str:
        number = to_float(value, None)
        return "нет" if number is None else f"{number:.{digits}f}"

    return (
        "🔔 НОВАЯ СДЕЛКА\n\n"
        f"📊 {trade.get('title')}\n"
        f"➡️ {trade.get('side')} {trade.get('outcome')}\n"
        f"💵 Цена: ${fmt(trade.get('price'), 4)}\n"
        f"📦 Размер: {fmt(trade.get('size'), 2)}\n"
        f"💰 Сумма: ${fmt(trade.get('usdc_value'), 2)}\n\n"
        f"⏱ От старта рынка: {fmt(entry_delay, 1)} сек.\n"
        f"⏳ До конца рынка: {fmt(to_end, 1)} сек.\n"
        f"📡 Задержка обнаружения: {fmt(delay, 1)} сек.\n\n"
        f"🪙 {trade.get('coin')} на Binance: "
        f"${fmt(trade.get('binance_price_at_detection'), 2)}\n"
        f"📈 Изменение от первой зафиксированной цены рынка: "
        f"{fmt(trade.get('binance_change_from_market_start'), 2)}\n\n"
        f"📖 Контракт сейчас: bid {fmt(trade.get('token_best_bid_at_detection'), 4)}"
        f" / ask {fmt(trade.get('token_best_ask_at_detection'), 4)}"
        f" / mid {fmt(trade.get('token_midpoint_at_detection'), 4)}\n"
        f"↔️ Противоположный: bid {fmt(trade.get('opposite_best_bid_at_detection'), 4)}"
        f" / ask {fmt(trade.get('opposite_best_ask_at_detection'), 4)}"
        f" / mid {fmt(trade.get('opposite_midpoint_at_detection'), 4)}"
    )


def process_poll(first_run: bool) -> int:
    raw_trades = get_trades()
    raw_trades.sort(key=lambda item: to_int(item.get("timestamp")))

    new_raw_trades = [
        trade
        for trade in raw_trades
        if not trade_exists(derive_trade_id(trade))
    ]

    if not new_raw_trades:
        logger.info("[INFO] Нет новых сделок")
        return 0

    logger.info("Новых сделок: %s", len(new_raw_trades))

    saved = 0
    for raw_trade in new_raw_trades:
        try:
            enriched = build_enriched_trade(raw_trade)
            if save_enriched_trade(enriched):
                saved += 1
                logger.info(
                    "%s %s %s @ %.4f x %.2f | start %+s sec | API delay %.1f sec",
                    enriched["coin"],
                    enriched["side"],
                    enriched["outcome"],
                    enriched["price"],
                    enriched["size"],
                    enriched["seconds_from_market_start"],
                    enriched["api_delay_sec"],
                )

                if not first_run:
                    send_telegram_message(format_trade_message(enriched))
        except Exception:
            logger.exception("Не удалось обработать сделку: %s", raw_trade)

    if first_run:
        logger.info(
            "Первичный импорт завершён без Telegram-уведомлений: %s сделок",
            saved,
        )

    return saved


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    init_database()

    record_event(
        "START",
        f"Observer started for wallet {WALLET}",
    )

    threading.Thread(
        target=automatic_export_loop,
        daemon=True,
        name="auto-export",
    ).start()

    threading.Thread(
        target=telegram_command_loop,
        daemon=True,
        name="telegram-commands",
    ).start()

    send_telegram_message(
        "🚀 Наблюдатель Polymarket запущен.\n"
        "/status — статистика\n"
        "/export — скачать ZIP"
    )

    first_run = True

    while not STOP_EVENT.is_set():
        try:
            process_poll(first_run=first_run)
            first_run = False
            STOP_EVENT.wait(POLL_INTERVAL)
        except KeyboardInterrupt:
            STOP_EVENT.set()
        except Exception as error:
            logger.exception("Ошибка главного цикла: %s", error)
            record_event("ERROR", str(error))
            STOP_EVENT.wait(ERROR_RETRY_INTERVAL)

    record_event("STOP", "Observer stopped")


if __name__ == "__main__":
    main()
