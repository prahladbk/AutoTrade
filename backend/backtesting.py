from datetime import datetime, time
from math import floor
from typing import Optional
from zoneinfo import ZoneInfo


INDIA_TZ = ZoneInfo("Asia/Kolkata")
STARTING_CAPITAL = 100_000.0
DEFAULT_RISK_PERCENT = 2.0
DEFAULT_REWARD_TO_RISK = 3.0
DEFAULT_STOP_LOOKBACK = 5


def parse_bars(candles: list) -> list[dict]:
    bars = []
    for candle in candles:
        if len(candle) < 5:
            continue
        timestamp = datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=INDIA_TZ)
        bars.append({
            "time": timestamp,
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]) if len(candle) > 5 else 0.0,
        })
    bars.sort(key=lambda bar: bar["time"])

    now = datetime.now(INDIA_TZ)
    market_is_open = now.weekday() < 5 and time(9, 15) <= now.time() < time(15, 30)
    if market_is_open and bars and bars[-1]["time"].astimezone(INDIA_TZ).date() == now.date():
        bars.pop()
    return bars


def build_setups(
    candles: list,
    fast_period: int = 6,
    slow_period: int = 30,
    stop_lookback: int = DEFAULT_STOP_LOOKBACK,
    reward_to_risk: float = DEFAULT_REWARD_TO_RISK,
) -> list[dict]:
    bars = parse_bars(candles)
    closes = [bar["close"] for bar in bars]
    setups = []

    for signal_index in range(slow_period, len(bars) - 1):
        previous_fast = sum(closes[signal_index - fast_period:signal_index]) / fast_period
        previous_slow = sum(closes[signal_index - slow_period:signal_index]) / slow_period
        fast = sum(closes[signal_index - fast_period + 1:signal_index + 1]) / fast_period
        slow = sum(closes[signal_index - slow_period + 1:signal_index + 1]) / slow_period
        if not (previous_fast <= previous_slow and fast > slow and closes[signal_index] > slow):
            continue

        entry_index = signal_index + 1
        entry = bars[entry_index]["open"]
        stop = min(bar["low"] for bar in bars[max(0, signal_index - stop_lookback + 1):signal_index + 1])
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            continue
        setups.append({
            "signal_time": bars[signal_index]["time"],
            "entry_index": entry_index,
            "entry_time": bars[entry_index]["time"],
            "entry": entry,
            "stop": stop,
            "risk_per_share": risk_per_share,
            "target": entry + reward_to_risk * risk_per_share,
            "bars": bars,
        })
    return setups


def size_position(capital: float, entry: float, stop: float, risk_percent: float = DEFAULT_RISK_PERCENT) -> tuple[int, float]:
    risk_per_share = entry - stop
    if capital <= 0 or entry <= 0 or risk_per_share <= 0:
        return 0, 0.0
    risk_fraction = risk_percent / 100
    risk_budget = min(STARTING_CAPITAL * risk_fraction, capital * risk_fraction)
    units_for_risk = floor(risk_budget / risk_per_share)
    units_affordable = floor(capital / entry)
    quantity = max(0, min(units_for_risk, units_affordable))
    return quantity, quantity * risk_per_share


def run_portfolio_backtest(
    candles_by_symbol: dict[str, list],
    fast_period: int = 6,
    slow_period: int = 30,
    stop_lookback: int = DEFAULT_STOP_LOOKBACK,
    risk_percent: float = DEFAULT_RISK_PERCENT,
    reward_to_risk: float = DEFAULT_REWARD_TO_RISK,
) -> dict:
    from backend.simulation_engine import run_event_backtest

    result = run_event_backtest(
        candles_by_symbol,
        "sma_crossover",
        {
            "strategy": {
                "fast_period": fast_period,
                "slow_period": slow_period,
                "stop_lookback": stop_lookback,
                "reward_to_risk": reward_to_risk,
                "direction": "long_only",
            },
            "common": {
                "risk_percent": risk_percent,
                "aggregate_open_risk_percent": 100,
                "max_positions": 1,
                "max_holding_bars": 100_000,
                "intraday": False,
            },
        },
    )
    trades = [
        {
            **trade,
            "stop_loss": trade["initial_stop"],
            "pnl": trade["gross_pnl"],
        }
        for trade in result["trades"]
    ]
    return {
        "starting_capital": result["starting_capital"],
        "ending_capital": result["ending_equity_gross"],
        "gross_pnl": result["gross_pnl"],
        "net_pnl": None,
        "cost_warning": result["cost_warning"],
        "return_percent": result["return_percent"],
        "trade_count": result["trade_count"],
        "win_rate": result["win_rate"],
        "max_drawdown_percent": result["max_drawdown_percent"],
        "profit_factor": result["profit_factor"],
        "risk_percent_per_trade": risk_percent,
        "reward_to_risk": reward_to_risk,
        "fast_period": fast_period,
        "slow_period": slow_period,
        "stop_lookback": stop_lookback,
        "trades": trades,
    }