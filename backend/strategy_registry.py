from dataclasses import dataclass
from datetime import time
from typing import Optional
from zoneinfo import ZoneInfo


INDIA_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class StrategySpec:
    key: str
    name: str
    description: str
    defaults: dict
    minimum_bars: int


STRATEGIES = {
    "sma_crossover": StrategySpec("sma_crossover", "SMA Crossover", "Completed-bar fast/slow SMA cross.", {"fast_period": 6, "slow_period": 30, "stop_lookback": 5, "reward_to_risk": 3.0, "direction": "long_only"}, 31),
    "momentum_breakout": StrategySpec("momentum_breakout", "Momentum Breakout", "Prior-range breakout with relative-volume confirmation.", {"breakout_lookback": 20, "relative_volume": 1.2, "atr_period": 14, "stop_lookback": 5, "reward_to_risk": 3.0, "direction": "long_short"}, 21),
    "opening_range_breakout": StrategySpec("opening_range_breakout", "Opening Range Breakout", "Break of the completed NSE opening range.", {"opening_range_minutes": 15, "relative_volume": 1.0, "stop_lookback": 5, "reward_to_risk": 3.0, "direction": "long_short", "entry_cutoff": "14:30"}, 2),
    "trend_pullback": StrategySpec("trend_pullback", "Trend-Following Pullback", "EMA trend, session VWAP, pullback and confirmation candle.", {"ema_fast": 9, "ema_slow": 21, "atr_period": 14, "pullback_lookback": 6, "relative_volume": 1.2, "stop_lookback": 5, "reward_to_risk": 2.0, "direction": "long_short"}, 22),
    "mean_reversion": StrategySpec("mean_reversion", "Mean Reversion", "Extended-price reversal toward VWAP or a moving average.", {"reference": "vwap", "deviation_percent": 1.0, "rsi_period": 14, "rsi_long": 30, "rsi_short": 70, "stop_lookback": 5, "reward_to_risk": 2.0, "direction": "long_short"}, 15),
    "vwap_reclaim": StrategySpec("vwap_reclaim", "VWAP Reclaim", "Completed-bar cross back through session VWAP.", {"ema_period": 21, "relative_volume": 1.0, "stop_lookback": 5, "reward_to_risk": 2.0, "direction": "long_short"}, 22),
    "donchian_breakout": StrategySpec("donchian_breakout", "Donchian Breakout", "Break of the prior Donchian channel, excluding the signal bar.", {"entry_lookback": 20, "exit_lookback": 10, "atr_period": 14, "relative_volume": 1.0, "stop_lookback": 5, "reward_to_risk": 3.0, "direction": "long_short"}, 21),
}


def strategy_catalog() -> list[dict]:
    return [
        {"key": spec.key, "name": spec.name, "description": spec.description, "defaults": spec.defaults, "minimum_bars": spec.minimum_bars}
        for spec in STRATEGIES.values()
    ]


def _sma(values: list[float], index: int, period: int) -> Optional[float]:
    if period < 1 or index + 1 < period:
        return None
    return sum(values[index - period + 1:index + 1]) / period


def _ema(values: list[float], index: int, period: int) -> Optional[float]:
    if index + 1 < period:
        return None
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:index + 1]:
        value = alpha * item + (1 - alpha) * value
    return value


def _rsi(values: list[float], index: int, period: int) -> Optional[float]:
    if index < period:
        return None
    changes = [values[position] - values[position - 1] for position in range(index - period + 1, index + 1)]
    gains = sum(max(change, 0) for change in changes) / period
    losses = sum(max(-change, 0) for change in changes) / period
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def _atr(bars: list[dict], index: int, period: int) -> Optional[float]:
    if index < period:
        return None
    ranges = []
    for position in range(index - period + 1, index + 1):
        previous_close = bars[position - 1]["close"]
        bar = bars[position]
        ranges.append(max(bar["high"] - bar["low"], abs(bar["high"] - previous_close), abs(bar["low"] - previous_close)))
    return sum(ranges) / period


def _vwap(bars: list[dict], index: int) -> Optional[float]:
    current_day = bars[index]["time"].astimezone(INDIA_TZ).date()
    session = [bar for bar in bars[:index + 1] if bar["time"].astimezone(INDIA_TZ).date() == current_day]
    volume = sum(bar.get("volume", 0) for bar in session)
    if volume <= 0:
        return None
    return sum(((bar["high"] + bar["low"] + bar["close"]) / 3) * bar.get("volume", 0) for bar in session) / volume


def _relative_volume(bars: list[dict], index: int, lookback: int = 20) -> float:
    if index < lookback:
        return 0.0
    average = sum(bar.get("volume", 0) for bar in bars[index - lookback:index]) / lookback
    return bars[index].get("volume", 0) / average if average > 0 else 0.0


def _direction_allowed(direction: str, config: dict) -> bool:
    return direction == "long" or config.get("direction", "long_only") == "long_short"


def _opening_range(bars: list[dict], index: int, duration: int) -> Optional[tuple[float, float]]:
    current = bars[index]["time"].astimezone(INDIA_TZ)
    day_bars = [bar for bar in bars[:index] if bar["time"].astimezone(INDIA_TZ).date() == current.date()]
    start = time(9, 15)
    end_minutes = 9 * 60 + 15 + duration
    end = time(end_minutes // 60, end_minutes % 60)
    range_bars = [bar for bar in day_bars if start <= bar["time"].astimezone(INDIA_TZ).time() < end]
    if not range_bars or current.time() < end:
        return None
    return max(bar["high"] for bar in range_bars), min(bar["low"] for bar in range_bars)


def _signal_for(key: str, bars: list[dict], index: int, config: dict) -> Optional[tuple[str, str]]:
    closes = [bar["close"] for bar in bars]
    current = bars[index]
    previous = bars[index - 1]
    direction = config.get("direction", "long_only")

    if key == "sma_crossover":
        fast_period, slow_period = config.get("fast_period", 6), config.get("slow_period", 30)
        prior_fast, prior_slow = _sma(closes, index - 1, fast_period), _sma(closes, index - 1, slow_period)
        fast, slow = _sma(closes, index, fast_period), _sma(closes, index, slow_period)
        if None in (prior_fast, prior_slow, fast, slow):
            return None
        if prior_fast <= prior_slow and fast > slow:
            return "long", f"SMA {fast_period} crossed above SMA {slow_period}"
        if direction == "long_short" and prior_fast >= prior_slow and fast < slow:
            return "short", f"SMA {fast_period} crossed below SMA {slow_period}"
        return None

    if key in {"momentum_breakout", "donchian_breakout"}:
        lookback = config.get("breakout_lookback", 20) if key == "momentum_breakout" else config.get("entry_lookback", 20)
        if index < lookback:
            return None
        prior_bars = bars[index - lookback:index]
        prior_high = max(bar["high"] for bar in prior_bars)
        prior_low = min(bar["low"] for bar in prior_bars)
        volume_floor = config.get("relative_volume", 0)
        if _relative_volume(bars, index) < volume_floor:
            return None
        if current["close"] > prior_high:
            return "long", f"Close broke prior {lookback}-bar high"
        if direction == "long_short" and current["close"] < prior_low:
            return "short", f"Close broke prior {lookback}-bar low"
        return None

    if key == "opening_range_breakout":
        cutoff = time.fromisoformat(config.get("entry_cutoff", "14:30"))
        if current["time"].astimezone(INDIA_TZ).time() > cutoff or _relative_volume(bars, index) < config.get("relative_volume", 0):
            return None
        opening_range = _opening_range(bars, index, config.get("opening_range_minutes", 15))
        if not opening_range:
            return None
        high, low = opening_range
        if current["close"] > high:
            return "long", "Completed opening-range high breakout"
        if direction == "long_short" and current["close"] < low:
            return "short", "Completed opening-range low breakdown"
        return None

    if key == "trend_pullback":
        fast = _ema(closes, index, config.get("ema_fast", 9))
        slow = _ema(closes, index, config.get("ema_slow", 21))
        vwap = _vwap(bars, index)
        if fast is None or slow is None or vwap is None or _relative_volume(bars, index) < config.get("relative_volume", 0):
            return None
        recent = bars[max(0, index - config.get("pullback_lookback", 6)):index]
        if current["close"] > vwap and fast > slow and any(bar["low"] <= fast for bar in recent) and current["close"] > previous["high"]:
            return "long", "Uptrend pullback confirmed above prior high"
        if direction == "long_short" and current["close"] < vwap and fast < slow and any(bar["high"] >= fast for bar in recent) and current["close"] < previous["low"]:
            return "short", "Downtrend pullback confirmed below prior low"
        return None

    if key == "mean_reversion":
        reference_kind = config.get("reference", "vwap")
        reference = _vwap(bars, index) if reference_kind == "vwap" else _sma(closes, index, config.get("reference_period", 20))
        rsi = _rsi(closes, index, config.get("rsi_period", 14))
        if reference is None or rsi is None or reference == 0:
            return None
        deviation = (current["close"] - reference) / reference * 100
        if deviation <= -config.get("deviation_percent", 1.0) and rsi <= config.get("rsi_long", 30) and current["close"] > current["open"]:
            return "long", f"Reversal confirmation below {reference_kind}"
        if direction == "long_short" and deviation >= config.get("deviation_percent", 1.0) and rsi >= config.get("rsi_short", 70) and current["close"] < current["open"]:
            return "short", f"Reversal confirmation above {reference_kind}"
        return None

    if key == "vwap_reclaim":
        vwap, prior_vwap = _vwap(bars, index), _vwap(bars, index - 1)
        ema = _ema(closes, index, config.get("ema_period", 21))
        if vwap is None or prior_vwap is None or ema is None or _relative_volume(bars, index) < config.get("relative_volume", 0):
            return None
        if previous["close"] <= prior_vwap and current["close"] > vwap and current["close"] > ema:
            return "long", "Completed candle reclaimed session VWAP"
        if direction == "long_short" and previous["close"] >= prior_vwap and current["close"] < vwap and current["close"] < ema:
            return "short", "Completed candle lost session VWAP"
        return None

    return None


def strategy_signal(key: str, bars: list[dict], index: int, config: dict) -> Optional[dict]:
    spec = STRATEGIES.get(key)
    if spec is None or index < max(spec.minimum_bars - 1, 1) or index >= len(bars) - 1:
        return None
    if config.get("volume_filter_enabled", True):
        volume_lookback = int(config.get("volume_lookback", 20))
        minimum_relative_volume = float(config.get("min_relative_volume", 1.0))
        if _relative_volume(bars, index, volume_lookback) < minimum_relative_volume:
            return None
    intent = _signal_for(key, bars, index, config)
    if intent is None:
        return None

    direction, reason = intent
    if not _direction_allowed(direction, config):
        return None
    lookback = config.get("stop_lookback", 5)
    stop_bars = bars[max(0, index - lookback + 1):index + 1]
    stop = min(bar["low"] for bar in stop_bars) if direction == "long" else max(bar["high"] for bar in stop_bars)
    atr = _atr(bars, index, config.get("atr_period", 14))
    if config.get("stop_method") == "atr" and atr:
        multiple = config.get("atr_stop_multiple", 1.5)
        stop = bars[index]["close"] - atr * multiple if direction == "long" else bars[index]["close"] + atr * multiple
    if direction == "long" and stop >= bars[index]["close"]:
        return None
    if direction == "short" and stop <= bars[index]["close"]:
        return None
    return {"direction": direction, "reason": reason, "stop": stop, "signal_index": index}