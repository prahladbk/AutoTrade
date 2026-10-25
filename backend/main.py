import csv
import gzip
import io
import json
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator
from starlette.responses import Response

from backend.backtesting import run_portfolio_backtest
from backend.simulation_engine import run_event_backtest
from backend.strategy_registry import STRATEGIES, strategy_catalog


load_dotenv(Path(__file__).with_name(".env"), override=False)

API_BASE = "https://api.upstox.com"
NIFTY100_URLS = (
    "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
)
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CANDLE_TIMEFRAMES = {
    "3m": ("minutes", 3, 28),
    "5m": ("minutes", 5, 28),
    "15m": ("minutes", 15, 28),
    "30m": ("minutes", 30, 85),
    "1h": ("hours", 1, 85),
    "4h": ("hours", 4, 85),
}
DB_PATH = Path(os.environ.get("CROSSLINE_DB", Path(__file__).with_name("crossline.db")))
INDIA_TZ = ZoneInfo("Asia/Kolkata")
_universe_cache: Optional[tuple[datetime, list[dict]]] = None
_equity_universe_cache: Optional[tuple[datetime, list[dict]]] = None
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()

app = FastAPI(title="Crossline Market Scanner", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class TradeRecord(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    status: str = Field(default="RECORDED", max_length=30)
    order_id: Optional[str] = Field(default=None, max_length=100)


class BacktestRequest(BaseModel):
    period_days: int = Field(default=180, ge=30, le=365)
    fast_period: int = Field(default=6, ge=2, le=20)
    slow_period: int = Field(default=30, ge=3, le=100)
    stop_lookback: int = Field(default=5, ge=2, le=20)
    risk_percent: float = Field(default=2.0, gt=0, le=2.0)
    reward_to_risk: float = Field(default=3.0, ge=1.0, le=5.0)

    @model_validator(mode="after")
    def validate_sma_order(self):
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period.")
        return self


class BacktestLaunchRequest(BaseModel):
    strategy_id: str
    config: dict = Field(default_factory=dict)


class StrategyPresetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    strategy_id: str
    config: dict = Field(default_factory=dict)


class PaperSessionRequest(BaseModel):
    strategy_id: str
    config: dict = Field(default_factory=dict)


PAPER_SYMBOL_CAP = 10
_paper_stops: dict[str, threading.Event] = {}


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                scanned_count INTEGER NOT NULL,
                signal_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                company TEXT NOT NULL,
                instrument_key TEXT NOT NULL,
                close REAL NOT NULL,
                sma6 REAL NOT NULL,
                sma30 REAL NOT NULL,
                candle_time TEXT NOT NULL,
                change_percent REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                status TEXT NOT NULL,
                order_id TEXT
            );
            CREATE TABLE IF NOT EXISTS strategy_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                status TEXT NOT NULL,
                config_json TEXT NOT NULL,
                result_json TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_sessions (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                status TEXT NOT NULL,
                config_json TEXT NOT NULL,
                open_json TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                stopped_at TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )


init_db()


def auth_headers(token: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def parse_nifty100_members(csv_text: str) -> dict[str, str]:
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = {name.strip().lower(): name for name in (reader.fieldnames or [])}
    symbol_column = columns.get("symbol")
    company_column = columns.get("company name") or columns.get("companyname")
    if not symbol_column:
        raise ValueError("Constituent CSV is missing the Symbol column.")

    members = {}
    for row in reader:
        symbol = (row.get(symbol_column) or "").strip().upper()
        if symbol:
            company = (row.get(company_column) or "").strip() if company_column else ""
            members[symbol] = company
    if len(members) < 80:
        raise ValueError(f"Constituent CSV contained only {len(members)} symbols; expected a Nifty 100 list.")
    return members


def load_nifty100_members() -> dict[str, str]:
    failures = []
    for url in NIFTY100_URLS:
        host = urlsplit(url).netloc
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/csv,text/plain,*/*",
            "Referer": f"https://{host}/",
        }
        try:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            return parse_nifty100_members(response.text)
        except requests.RequestException as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            reason = f"HTTP {status_code}" if status_code else type(error).__name__
            failures.append(f"{host}: {reason}")
        except ValueError as error:
            failures.append(f"{host}: {error}")
    raise RuntimeError("No Nifty 100 constituent source returned a valid list (" + "; ".join(failures) + ").")


def load_nifty100_universe() -> list[dict]:
    global _universe_cache
    now = datetime.now(timezone.utc)
    if _universe_cache and now - _universe_cache[0] < timedelta(hours=12):
        return _universe_cache[1]

    members = load_nifty100_members()

    instrument_response = requests.get(INSTRUMENTS_URL, timeout=30)
    instrument_response.raise_for_status()
    raw = gzip.decompress(instrument_response.content)
    instruments = json.loads(raw)
    by_symbol: dict[str, dict] = {}
    for instrument in instruments:
        symbol = str(instrument.get("tradingsymbol", instrument.get("trading_symbol", ""))).upper()
        if symbol not in members:
            continue
        if instrument.get("segment") != "NSE_EQ" and instrument.get("instrument_type") not in {"EQ", "EQUITY"}:
            continue
        key = instrument.get("instrument_key")
        if key:
            by_symbol[symbol] = {
                "symbol": symbol,
                "company": members[symbol] or instrument.get("name", symbol),
                "instrument_key": key,
            }

    missing = sorted(set(members) - set(by_symbol))
    if not by_symbol:
        raise RuntimeError("Could not match Nifty 100 constituents to Upstox NSE instruments.")
    if missing:
        print(f"Nifty 100 constituents without an Upstox equity instrument: {len(missing)}")
    result = sorted(by_symbol.values(), key=lambda item: item["symbol"])
    _universe_cache = (now, result)
    return result


def load_all_equity_universe() -> list[dict]:
    global _equity_universe_cache
    now = datetime.now(timezone.utc)
    if _equity_universe_cache and now - _equity_universe_cache[0] < timedelta(hours=12):
        return _equity_universe_cache[1]
    response = requests.get(INSTRUMENTS_URL, timeout=30)
    response.raise_for_status()
    instruments = json.loads(gzip.decompress(response.content))
    result = []
    for instrument in instruments:
        if instrument.get("segment") != "NSE_EQ" or instrument.get("instrument_type") not in {"EQ", "EQUITY"}:
            continue
        symbol = str(instrument.get("tradingsymbol", instrument.get("trading_symbol", ""))).upper()
        key = instrument.get("instrument_key")
        if symbol and key:
            result.append({"symbol": symbol, "company": instrument.get("name", symbol), "instrument_key": key})
    result.sort(key=lambda item: item["symbol"])
    _equity_universe_cache = (now, result)
    return result


def analyze_candles(symbol: str, company: str, instrument_key: str, candles: list) -> Optional[dict]:
    parsed = []
    for row in candles:
        if len(row) < 5:
            continue
        timestamp = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=INDIA_TZ)
        parsed.append((timestamp, float(row[4])))
    parsed.sort(key=lambda item: item[0])

    now = datetime.now(INDIA_TZ)
    market_is_open = now.weekday() < 5 and time(9, 15) <= now.time() < time(15, 30)
    if market_is_open and parsed and parsed[-1][0].astimezone(INDIA_TZ).date() == now.date():
        parsed.pop()
    if len(parsed) < 31:
        return None

    closes = [close for _, close in parsed]
    previous_fast = sum(closes[-7:-1]) / 6
    previous_slow = sum(closes[-31:-1]) / 30
    sma6 = sum(closes[-6:]) / 6
    sma30 = sum(closes[-30:]) / 30
    close = closes[-1]
    if not (previous_fast <= previous_slow and sma6 > sma30 and close > sma30):
        return None

    previous_close = closes[-2]
    return {
        "symbol": symbol,
        "company": company,
        "instrument_key": instrument_key,
        "close": close,
        "sma6": sma6,
        "sma30": sma30,
        "candle_time": parsed[-1][0].isoformat(),
        "change_percent": ((close - previous_close) / previous_close * 100) if previous_close else 0,
    }


def scan_instrument(instrument: dict, token: str, to_date: date, from_date: date) -> Optional[dict]:
    candles = fetch_candles(instrument, token, to_date, from_date)
    return analyze_candles(instrument["symbol"], instrument["company"], instrument["instrument_key"], candles)


def fetch_candles(instrument: dict, token: str, to_date: date, from_date: date, timeframe: str = "4h") -> list:
    if timeframe not in CANDLE_TIMEFRAMES:
        raise ValueError(f"Unsupported candle timeframe: {timeframe}")
    unit, interval, chunk_days = CANDLE_TIMEFRAMES[timeframe]
    key = quote(instrument["instrument_key"], safe="")
    candles = []
    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), to_date)
        url = f"{API_BASE}/v3/historical-candle/{key}/{unit}/{interval}/{chunk_end.isoformat()}/{chunk_start.isoformat()}"
        response = requests.get(url, headers=auth_headers(token), timeout=20)
        response.raise_for_status()
        candles.extend(response.json().get("data", {}).get("candles", []))
        chunk_start = chunk_end + timedelta(days=1)
    return candles


def as_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row else None


@app.get("/api/status")
def status() -> dict:
    return {"service": "online", "token_configured": bool(os.environ.get("UPSTOX_ACCESS_TOKEN"))}


@app.get("/api/scans")
def scans() -> dict:
    with db_connect() as connection:
        rows = connection.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 20").fetchall()
        latest = as_dict(rows[0]) if rows else None
        signals = []
        if latest:
            signals = [dict(row) for row in connection.execute(
                "SELECT symbol, company, instrument_key, close, sma6, sma30, candle_time, change_percent "
                "FROM scan_signals WHERE scan_id = ? ORDER BY change_percent DESC",
                (latest["id"],),
            ).fetchall()]
    return {"scans": [dict(row) for row in rows], "latest": latest, "signals": signals}


@app.post("/api/scan")
def run_scan() -> dict:
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="UPSTOX_ACCESS_TOKEN is not configured in the backend environment.")
    try:
        universe = load_nifty100_universe()
    except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=f"Could not load the Nifty 100 universe: {error}") from error

    local_now = datetime.now(INDIA_TZ)
    to_date = local_now.date()
    from_date = to_date - timedelta(days=87)
    matches = []
    error_count = 0
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(scan_instrument, item, token, to_date, from_date): item
            for item in universe
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    matches.append(result)
            except (requests.RequestException, ValueError, KeyError, TypeError):
                error_count += 1

    started_at = datetime.now(timezone.utc).isoformat()
    scanned_count = len(universe) - error_count
    with db_connect() as connection:
        cursor = connection.execute(
            "INSERT INTO scan_runs (started_at, scanned_count, signal_count, error_count) VALUES (?, ?, ?, ?)",
            (started_at, scanned_count, len(matches), error_count),
        )
        scan_id = cursor.lastrowid
        connection.executemany(
            "INSERT INTO scan_signals (scan_id, symbol, company, instrument_key, close, sma6, sma30, candle_time, change_percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (scan_id, item["symbol"], item["company"], item["instrument_key"], item["close"], item["sma6"], item["sma30"], item["candle_time"], item["change_percent"])
                for item in matches
            ],
        )
        scan = {
            "id": scan_id,
            "started_at": started_at,
            "scanned_count": scanned_count,
            "signal_count": len(matches),
            "error_count": error_count,
        }
    matches.sort(key=lambda item: item["change_percent"], reverse=True)
    return {"scan": scan, "signals": matches}


def normalize_workbench_config(strategy_id: str, supplied: dict) -> dict:
    if strategy_id not in STRATEGIES:
        raise HTTPException(status_code=422, detail="Unknown strategy id.")

    common = {
        "starting_capital": 100_000,
        "universe": "nifty100_current",
        "symbols": [],
        "timeframe": "5m",
        "period_days": 28,
        "risk_percent": 0.25,
        "aggregate_open_risk_percent": 0.5,
        "daily_loss_limit_percent": 1.0,
        "losing_day_streak_limit": 3,
        "cooldown_days": 3,
        "volume_filter_enabled": True,
        "min_relative_volume": 1.0,
        "volume_lookback": 20,
        "max_positions": 3,
        "max_allocation_percent": 50.0,
        "max_holding_bars": 12,
        "max_entries_per_symbol_per_day": 1,
        "intraday": True,
        "entry_start_time": "09:20",
        "entry_cutoff": "14:30",
        "square_off_time": "15:25",
        "start_date": None,
        "end_date": None,
    }
    common.update(supplied.get("common", {}))
    strategy = {**STRATEGIES[strategy_id].defaults, **supplied.get("strategy", {})}
    costs = {field: None for field in (
        "brokerage_per_order", "stt_sell_percent", "exchange_turnover_percent",
        "gst_percent", "stamp_buy_percent", "sebi_per_crore",
    )}
    costs.update(supplied.get("costs", {}))

    timeframe = common["timeframe"]
    if timeframe not in CANDLE_TIMEFRAMES:
        raise HTTPException(status_code=422, detail="Supported timeframes are 3m, 5m, 15m, 30m, 1h, and 4h.")
    if common["start_date"] and common["end_date"]:
        try:
            start_date = date.fromisoformat(common["start_date"])
            end_date = date.fromisoformat(common["end_date"])
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format.") from error
        if start_date > end_date or end_date > datetime.now(INDIA_TZ).date():
            raise HTTPException(status_code=422, detail="Backtest date range is invalid or ends in the future.")
        common["period_days"] = (end_date - start_date).days + 1
    else:
        common["period_days"] = int(common["period_days"])
        end_date = datetime.now(INDIA_TZ).date()
        start_date = end_date - timedelta(days=common["period_days"] - 1)
        common["start_date"] = start_date.isoformat()
        common["end_date"] = end_date.isoformat()

    if common["period_days"] < 1 or common["period_days"] > 365:
        raise HTTPException(status_code=422, detail="Backtest history must be between 1 and 365 days.")
    if timeframe in {"3m", "5m", "15m"} and common["period_days"] > 28:
        raise HTTPException(status_code=422, detail="Upstox historical 3/5/15-minute candles are limited to the latest month. Choose 30m, 1h, or 4h for longer history.")
    try:
        fast_period = int(strategy.get("fast_period", 2))
        slow_period = int(strategy.get("slow_period", 3))
        reward_to_risk = float(strategy.get("reward_to_risk", 3))
        risk_percent = float(common["risk_percent"])
        aggregate_risk = float(common["aggregate_open_risk_percent"])
        daily_loss_limit = float(common["daily_loss_limit_percent"])
        capital = float(common["starting_capital"])
        allocation = float(common["max_allocation_percent"])
        positions = int(common["max_positions"])
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="Numeric strategy and risk settings must contain valid numbers.") from error
    if fast_period >= slow_period:
        period_name = "EMA" if strategy_id == "ema_supply_demand" else "SMA"
        raise HTTPException(status_code=422, detail=f"Fast {period_name} period must be lower than slow {period_name} period.")
    if reward_to_risk <= 0 or reward_to_risk > 10:
        raise HTTPException(status_code=422, detail="Target reward-to-risk must be greater than 0 and at most 10.")
    if risk_percent <= 0 or risk_percent > 2:
        raise HTTPException(status_code=422, detail="Planned risk per trade must be positive and at most 2% of equity.")
    if not 1 <= positions <= 10:
        raise HTTPException(status_code=422, detail="Maximum simultaneous positions must be between 1 and 10.")
    if capital <= 0:
        raise HTTPException(status_code=422, detail="Starting capital must be positive.")
    if aggregate_risk <= 0 or aggregate_risk > 5:
        raise HTTPException(status_code=422, detail="Aggregate open risk must be positive and no more than 5%.")
    if daily_loss_limit <= 0 or daily_loss_limit > 10:
        raise HTTPException(status_code=422, detail="Daily loss limit must be positive and no more than 10%.")
    if int(common["losing_day_streak_limit"]) < 1 or int(common["cooldown_days"]) < 1:
        raise HTTPException(status_code=422, detail="Loss streak and cooldown days must be positive.")
    if not 0 < allocation <= 100:
        raise HTTPException(status_code=422, detail="Maximum allocation must be above 0% and at most 100%.")
    if common["universe"] not in {"nifty100_current", "selected_symbols", "all_nse_equities"}:
        raise HTTPException(status_code=422, detail="Choose Nifty 100, selected stocks, or all NSE equities.")
    if common["universe"] == "selected_symbols" and not common.get("symbols"):
        raise HTTPException(status_code=422, detail="Select at least one stock.")
    if float(common["min_relative_volume"]) <= 0 or int(common["volume_lookback"]) < 2:
        raise HTTPException(status_code=422, detail="Volume threshold and lookback must be positive.")
    try:
        entry_start = time.fromisoformat(common["entry_start_time"])
        entry_cutoff = time.fromisoformat(common["entry_cutoff"])
        time.fromisoformat(common["square_off_time"])
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="Session times must use HH:MM format.") from error
    if entry_start >= entry_cutoff:
        raise HTTPException(status_code=422, detail="Entry start must be earlier than the entry cutoff.")
    for value in costs.values():
        if isinstance(value, (int, float)) and value < 0:
            raise HTTPException(status_code=422, detail="Transaction cost rates cannot be negative.")

    return {"version": 1, "strategy_id": strategy_id, "common": common, "strategy": strategy, "costs": costs}


def _save_run(run_id: str, status_value: str, result: Optional[dict] = None, error: Optional[str] = None) -> None:
    completed_at = datetime.now(timezone.utc).isoformat() if status_value in {"COMPLETED", "FAILED", "CANCELLED"} else None
    with db_connect() as connection:
        connection.execute(
            "UPDATE backtest_runs SET status = ?, result_json = ?, error_text = ?, completed_at = ? WHERE id = ?",
            (status_value, json.dumps(result, default=str) if result is not None else None, error, completed_at, run_id),
        )


def _execute_backtest_run(run_id: str, token: str) -> None:
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return
    config = json.loads(row["config_json"])
    cancel_event = _cancel_events.get(run_id)
    try:
        common = config["common"]
        if common["universe"] == "all_nse_equities":
            universe = load_all_equity_universe()
        elif common["universe"] == "selected_symbols":
            selected = {str(symbol).upper() for symbol in common["symbols"]}
            universe = [item for item in load_all_equity_universe() if item["symbol"] in selected]
        else:
            universe = load_nifty100_universe()
        to_date = date.fromisoformat(common["end_date"])
        from_date = date.fromisoformat(common["start_date"])
        candles_by_symbol = {}
        failed_symbols = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(fetch_candles, instrument, token, to_date, from_date, common["timeframe"]): instrument
                for instrument in universe
            }
            for future in as_completed(futures):
                if cancel_event and cancel_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    _save_run(run_id, "CANCELLED")
                    return
                instrument = futures[future]
                try:
                    candles_by_symbol[instrument["symbol"]] = future.result()
                except (requests.RequestException, ValueError, KeyError, TypeError):
                    failed_symbols.append(instrument["symbol"])

        if not candles_by_symbol:
            raise RuntimeError("No historical candle data was returned for the selected universe and dates.")
        result = run_event_backtest(candles_by_symbol, config["strategy_id"], config, cancel_check=cancel_event.is_set if cancel_event else None)
        if cancel_event and cancel_event.is_set():
            _save_run(run_id, "CANCELLED")
            return
        result.update({
            "run_id": run_id,
            "strategy_id": config["strategy_id"],
            "configuration": config,
            "timeframe": common["timeframe"],
            "start_date": common["start_date"],
            "end_date": common["end_date"],
            "symbols_checked": len(candles_by_symbol),
            "symbols_failed": failed_symbols,
            "data_source": "Upstox Historical Candle V3",
            "universe_note": "Current Nifty 100 constituents are applied to the full date range; historical membership is unavailable.",
            "cost_source": config["costs"].get("source") or "Not configured",
        })
        _save_run(run_id, "COMPLETED", result)
    except Exception as error:
        _save_run(run_id, "FAILED", error=f"{type(error).__name__}: {error}")
    finally:
        with _cancel_lock:
            _cancel_events.pop(run_id, None)


@app.get("/api/strategies")
def get_strategies() -> list[dict]:
    return strategy_catalog()


@app.get("/api/universe")
def get_universe(scope: str = "nifty100") -> list[dict]:
    try:
        return load_all_equity_universe() if scope == "all" else load_nifty100_universe()
    except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=f"Could not load stock universe: {error}") from error


@app.get("/api/strategy-presets")
def get_strategy_presets() -> list[dict]:
    with db_connect() as connection:
        rows = connection.execute("SELECT * FROM strategy_presets ORDER BY updated_at DESC").fetchall()
    return [{**dict(row), "config": json.loads(row["config_json"])} for row in rows]


@app.post("/api/strategy-presets", status_code=201)
def save_strategy_preset(preset: StrategyPresetRequest) -> dict:
    config = normalize_workbench_config(preset.strategy_id, preset.config)
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as connection:
        cursor = connection.execute(
            "INSERT INTO strategy_presets (name, strategy_id, config_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (preset.name, preset.strategy_id, json.dumps(config), now, now),
        )
    return {"id": cursor.lastrowid, "name": preset.name, "strategy_id": preset.strategy_id, "config": config, "created_at": now, "updated_at": now}


@app.put("/api/strategy-presets/{preset_id}")
def update_strategy_preset(preset_id: int, preset: StrategyPresetRequest) -> dict:
    config = normalize_workbench_config(preset.strategy_id, preset.config)
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as connection:
        cursor = connection.execute(
            "UPDATE strategy_presets SET name = ?, strategy_id = ?, config_json = ?, updated_at = ? WHERE id = ?",
            (preset.name, preset.strategy_id, json.dumps(config), now, preset_id),
        )
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Strategy preset not found.")
    return {"id": preset_id, "name": preset.name, "strategy_id": preset.strategy_id, "config": config, "updated_at": now}


@app.delete("/api/strategy-presets/{preset_id}", status_code=204)
def delete_strategy_preset(preset_id: int) -> Response:
    with db_connect() as connection:
        cursor = connection.execute("DELETE FROM strategy_presets WHERE id = ?", (preset_id,))
    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Strategy preset not found.")
    return Response(status_code=204)


@app.post("/api/backtests", status_code=202)
def create_backtest(request: BacktestLaunchRequest, background_tasks: BackgroundTasks) -> dict:
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="UPSTOX_ACCESS_TOKEN is not configured in backend/.env.")
    config = normalize_workbench_config(request.strategy_id, request.config)
    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with db_connect() as connection:
        connection.execute(
            "INSERT INTO backtest_runs (id, strategy_id, status, config_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, request.strategy_id, "RUNNING", json.dumps(config), created_at),
        )
    with _cancel_lock:
        _cancel_events[run_id] = threading.Event()
    background_tasks.add_task(_execute_backtest_run, run_id, token)
    return {"id": run_id, "strategy_id": request.strategy_id, "status": "RUNNING", "created_at": created_at, "configuration": config}


@app.get("/api/backtests")
def list_backtests() -> list[dict]:
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT id, strategy_id, status, config_json, created_at, completed_at, error_text FROM backtest_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return [{**dict(row), "configuration": json.loads(row["config_json"])} for row in rows]


@app.get("/api/backtests/{run_id}")
def get_backtest(run_id: str) -> dict:
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {"id": row["id"], "strategy_id": row["strategy_id"], "status": row["status"], "configuration": json.loads(row["config_json"]), "result": result, "error": row["error_text"], "created_at": row["created_at"], "completed_at": row["completed_at"]}


@app.post("/api/backtests/{run_id}/cancel")
def cancel_backtest(run_id: str) -> dict:
    with db_connect() as connection:
        row = connection.execute("SELECT status FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    with _cancel_lock:
        cancel_event = _cancel_events.get(run_id)
    if row["status"] == "RUNNING" and cancel_event:
        cancel_event.set()
    return {"id": run_id, "status": "CANCELLING" if cancel_event else row["status"]}


@app.get("/api/backtests/{run_id}/trades.csv")
def export_backtest_trades(run_id: str) -> Response:
    with db_connect() as connection:
        row = connection.execute("SELECT result_json FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    if not row["result_json"]:
        raise HTTPException(status_code=409, detail="Backtest results are not ready to export.")
    import csv
    output = io.StringIO()
    trades = json.loads(row["result_json"]).get("trades", [])
    if trades:
        fields = list(trades[0].keys())
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for trade in trades:
            writer.writerow({key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in trade.items()})
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="crossline-{run_id}.csv"'})


def select_paper_symbols(config: dict) -> list[str]:
    common = config.get("common", {})
    symbols = []
    for symbol in common.get("symbols") or []:
        text = str(symbol).upper().strip()
        if text and text not in symbols:
            symbols.append(text)
    if common.get("universe") != "selected_symbols":
        raise HTTPException(status_code=422, detail="Paper simulation watches a selected symbol list, not the full universe.")
    if not symbols:
        raise HTTPException(status_code=422, detail="Select at least one stock for the paper session.")
    return symbols[:PAPER_SYMBOL_CAP]


def _paper_once(session_id: str, token: str) -> None:
    """Refresh simulated fills from completed candles. This never calls an order endpoint."""
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM paper_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or row["status"] != "RUNNING":
        return
    config = json.loads(row["config_json"])
    common = config["common"]
    symbols = set(select_paper_symbols(config))
    universe = [item for item in load_all_equity_universe() if item["symbol"] in symbols]
    end_date = datetime.now(INDIA_TZ).date()
    start_date = max(date.fromisoformat(common["start_date"]), end_date - timedelta(days=27))
    candles_by_symbol = {}
    for instrument in universe:
        candles_by_symbol[instrument["symbol"]] = fetch_candles(instrument, token, end_date, start_date, common["timeframe"])
    if not candles_by_symbol:
        raise RuntimeError("No candles were returned for the paper session.")
    result = run_event_backtest(candles_by_symbol, config["strategy_id"], config, close_open_positions=False)
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as connection:
        current = connection.execute("SELECT status FROM paper_sessions WHERE id = ?", (session_id,)).fetchone()
        if current is None or current["status"] != "RUNNING":
            return
        connection.execute("DELETE FROM paper_trades WHERE session_id = ?", (session_id,))
        for trade in result["trades"]:
            if trade.get("exit_reason") == "END_OF_DATA":
                continue
            connection.execute(
                "INSERT INTO paper_trades (session_id, symbol, direction, entry_time, exit_time, status, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, trade["symbol"], trade["direction"], trade["entry_time"], trade["exit_time"], "PAPER", json.dumps(trade)),
            )
        connection.execute(
            "UPDATE paper_sessions SET open_json = ?, last_error = NULL, updated_at = ? WHERE id = ? AND status = 'RUNNING'",
            (json.dumps(result.get("open_positions") or []), now, session_id),
        )


def _paper_loop(session_id: str, token: str) -> None:
    stop = _paper_stops.get(session_id)
    while stop is not None and not stop.is_set():
        try:
            _paper_once(session_id, token)
        except Exception as error:
            with db_connect() as connection:
                connection.execute(
                    "UPDATE paper_sessions SET last_error = ?, updated_at = ? WHERE id = ?",
                    (f"{type(error).__name__}: {error}", datetime.now(timezone.utc).isoformat(), session_id),
                )
        if stop.wait(60):
            break


def _paper_payload(row: sqlite3.Row, trades: list[sqlite3.Row]) -> dict:
    return {
        "id": row["id"],
        "strategy_id": row["strategy_id"],
        "status": row["status"],
        "configuration": json.loads(row["config_json"]),
        "open_positions": json.loads(row["open_json"]) if row["open_json"] else [],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "stopped_at": row["stopped_at"],
        "trades": [json.loads(trade["payload_json"]) for trade in trades],
        "order_placement": "disabled",
    }


@app.post("/api/paper/sessions", status_code=201)
def start_paper_session(request: PaperSessionRequest) -> dict:
    if request.strategy_id != "ema_supply_demand":
        raise HTTPException(status_code=422, detail="Paper simulation is available for EMA 7/21 + Supply/Demand only.")
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="UPSTOX_ACCESS_TOKEN is not configured in backend/.env.")
    config = normalize_workbench_config(request.strategy_id, request.config)
    select_paper_symbols(config)
    with db_connect() as connection:
        running = connection.execute("SELECT id FROM paper_sessions WHERE status = 'RUNNING'").fetchone()
    if running:
        raise HTTPException(status_code=409, detail="A paper session is already running.")
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as connection:
        connection.execute(
            "INSERT INTO paper_sessions (id, strategy_id, status, config_json, open_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, request.strategy_id, "RUNNING", json.dumps(config), "[]", now, now),
        )
    stop = threading.Event()
    _paper_stops[session_id] = stop
    threading.Thread(target=_paper_loop, args=(session_id, token), daemon=True).start()
    return {"id": session_id, "status": "RUNNING", "order_placement": "disabled", "configuration": config}


@app.get("/api/paper/sessions/current")
def current_paper_session() -> dict:
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM paper_sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        trades = connection.execute(
            "SELECT * FROM paper_trades WHERE session_id = ? ORDER BY entry_time",
            (row["id"],),
        ).fetchall() if row else []
    if row is None:
        return {"status": "NONE", "trades": [], "open_positions": [], "order_placement": "disabled"}
    return _paper_payload(row, trades)


@app.post("/api/paper/sessions/{session_id}/stop")
def stop_paper_session(session_id: str) -> dict:
    stop = _paper_stops.get(session_id)
    if stop:
        stop.set()
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as connection:
        cursor = connection.execute(
            "UPDATE paper_sessions SET status = 'STOPPED', stopped_at = ?, updated_at = ? WHERE id = ? AND status = 'RUNNING'",
            (now, now, session_id),
        )
        row = connection.execute("SELECT * FROM paper_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Paper session not found.")
    if cursor.rowcount == 0 and row["status"] != "STOPPED":
        raise HTTPException(status_code=409, detail="Paper session is not running.")
    return {"id": session_id, "status": row["status"], "order_placement": "disabled"}


@app.get("/api/backtests/{run_id}/chart")
def backtest_chart(run_id: str, symbol: str) -> dict:
    with db_connect() as connection:
        row = connection.execute("SELECT result_json FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    if not row["result_json"]:
        raise HTTPException(status_code=409, detail="Backtest results are not ready.")
    chart = json.loads(row["result_json"]).get("charts", {}).get(symbol.upper())
    if chart is None:
        raise HTTPException(status_code=404, detail="No cached chart for that symbol. Charts are stored for symbols that traded.")
    return chart


@app.get("/api/backtests/{run_id}/stocks")
def backtest_stocks(run_id: str) -> list[dict]:
    with db_connect() as connection:
        row = connection.execute("SELECT result_json FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    if not row["result_json"]:
        raise HTTPException(status_code=409, detail="Backtest results are not ready.")
    return json.loads(row["result_json"]).get("stock_performance", [])


@app.post("/api/backtests/compare")
def compare_backtests(run_ids: list[str] = Body(min_length=2, max_length=6)) -> dict:
    with db_connect() as connection:
        rows = [connection.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone() for run_id in run_ids]
    if any(row is None or not row["result_json"] for row in rows):
        raise HTTPException(status_code=404, detail="One or more completed backtest runs were not found.")
    configurations = [json.loads(row["config_json"]) for row in rows]
    comparables = all(
        configurations[0]["common"] == config["common"] and configurations[0]["costs"] == config["costs"]
        for config in configurations[1:]
    )
    warning = None if comparables else "Runs use different common, date, timeframe, risk, or transaction-cost assumptions."
    return {"comparable_assumptions": comparables, "warning": warning, "runs": [{"id": row["id"], "strategy_id": row["strategy_id"], **json.loads(row["result_json"])} for row in rows]}


@app.post("/api/backtest")
def backtest(request: BacktestRequest) -> dict:
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="UPSTOX_ACCESS_TOKEN is not configured in the backend environment.")
    try:
        universe = load_nifty100_universe()
    except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=f"Could not load the Nifty 100 universe: {error}") from error

    to_date = datetime.now(INDIA_TZ).date()
    from_date = to_date - timedelta(days=request.period_days)
    candles_by_symbol = {}
    failed_symbols = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_candles, item, token, to_date, from_date): item
            for item in universe
        }
        for future in as_completed(futures):
            instrument = futures[future]
            try:
                candles_by_symbol[instrument["symbol"]] = future.result()
            except (requests.RequestException, ValueError, KeyError, TypeError):
                failed_symbols.append(instrument["symbol"])

    if not candles_by_symbol:
        raise HTTPException(status_code=502, detail="Historical candle requests failed for every Nifty 100 instrument. Check the Upstox token and API access.")

    result = run_portfolio_backtest(
        candles_by_symbol,
        fast_period=request.fast_period,
        slow_period=request.slow_period,
        stop_lookback=request.stop_lookback,
        risk_percent=request.risk_percent,
        reward_to_risk=request.reward_to_risk,
    )
    result["period_days"] = request.period_days
    result["symbols_checked"] = len(candles_by_symbol)
    result["symbols_failed"] = len(failed_symbols)
    result["stop_rule"] = f"Lowest low of the signal candle and previous {request.stop_lookback - 1} completed 4-hour candles"
    result["entry_rule"] = f"SMA {request.fast_period}/{request.slow_period}; next 4-hour candle open; one open position at a time"
    result["same_bar_rule"] = "Stop loss is assumed first when stop and target are both touched in one candle"
    result["risk_note"] = "2% is planned risk at the stop; gaps, slippage, taxes, and brokerage can increase real losses."
    return result


@app.get("/api/trades")
def trades() -> list[dict]:
    with db_connect() as connection:
        rows = connection.execute("SELECT * FROM trade_log ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/trades", status_code=201)
def record_trade(trade: TradeRecord) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    with db_connect() as connection:
        cursor = connection.execute(
            "INSERT INTO trade_log (timestamp, symbol, side, quantity, price, status, order_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (timestamp, trade.symbol.upper(), trade.side, trade.quantity, trade.price, trade.status.upper(), trade.order_id),
        )
        row = connection.execute("SELECT * FROM trade_log WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)