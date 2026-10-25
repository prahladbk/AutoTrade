"""Causal EMA 7/21 and supply/demand evaluation.

Pivots become usable only after the confirming right-side candles have closed.
Nothing in this module reads a future bar to build a zone, a signal, or a volume baseline.
"""

from datetime import datetime, time
from statistics import median
from typing import Optional
from zoneinfo import ZoneInfo


INDIA_TZ = ZoneInfo("Asia/Kolkata")

DEFAULTS = {
    "fast_period": 7,
    "slow_period": 21,
    "trend_mode": "trend",
    "crossover_lookback": 3,
    "require_slow_ema_slope": True,
    "slope_lookback": 3,
    "direction": "long_short",
    "pivot_strength": 3,
    "zone_width_min_atr": 0.15,
    "zone_width_max_atr": 2.0,
    "departure_atr": 1.0,
    "min_departure_relative_volume": 1.0,
    "max_zone_age_bars": 40,
    "max_prior_touches": 0,
    "min_opposing_distance_atr": 0.5,
    "atr_period": 14,
    "invalidation_atr_buffer": 0.0,
    "entry_mode": "touch_trend_rejection",
    "min_relative_volume": 1.2,
    "volume_lookback_sessions": 20,
    "volume_filter_enabled": True,
    "entry_start_time": "09:30",
    "entry_cutoff": "13:00",
    "cutoff_exclusive": True,
    "square_off_time": "15:15",
    "max_holding_bars": 8,
    "stop_method": "zone_atr",
    "atr_buffer": 0.2,
    "atr_stop_multiple": 1.5,
    "swing_lookback": 20,
    "target_mode": "opposing_zone",
    "reward_to_risk": 2.0,
    "max_target_r": 2.0,
    "min_reward_to_risk": 1.0,
    "momentum_failure_enabled": True,
    "momentum_failure_bars": 4,
    "momentum_progress_threshold": 0.25,
    "max_participation": 0.01,
    "timeframe": "5m",
    "max_positions": 3,
}


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    series: list[Optional[float]] = [None] * len(values)
    if period < 1 or len(values) < period:
        return series
    current = sum(values[:period]) / period
    series[period - 1] = current
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        current = alpha * values[index] + (1 - alpha) * current
        series[index] = current
    return series


def atr_series(bars: list[dict], period: int) -> list[Optional[float]]:
    series: list[Optional[float]] = [None] * len(bars)
    if period < 1:
        return series
    true_ranges = [0.0] * len(bars)
    for index in range(1, len(bars)):
        previous_close = bars[index - 1]["close"]
        bar = bars[index]
        true_ranges[index] = max(
            bar["high"] - bar["low"],
            abs(bar["high"] - previous_close),
            abs(bar["low"] - previous_close),
        )
    for index in range(period, len(bars)):
        window = true_ranges[index - period + 1:index + 1]
        series[index] = sum(window) / period
    return series


def execution_time_block(local_time: time, start: time, cutoff: time, exclusive: bool) -> Optional[str]:
    """Return a rejection reason when a new entry is outside the session window."""
    if local_time < start:
        return "ENTRY_TOO_EARLY"
    if exclusive and local_time >= cutoff:
        return "ENTRY_CUTOFF"
    if not exclusive and local_time > cutoff:
        return "ENTRY_CUTOFF"
    return None


def resolve_target(entry: float, stop: float, direction: str, signal: dict) -> Optional[float]:
    """Target from the filled price. None means the room in front of the zone is too small."""
    risk = entry - stop if direction == "long" else stop - entry
    if risk <= 0:
        return None
    mode = signal.get("target_mode", "fixed_r")
    reward = float(signal.get("reward_to_risk", 2))
    max_r = float(signal.get("max_target_r", reward))
    min_r = float(signal.get("min_reward_to_risk", 1))
    opposing = signal.get("opposing_price")
    fixed = entry + risk * reward if direction == "long" else entry - risk * reward
    capped = entry + risk * max_r if direction == "long" else entry - risk * max_r

    def room_r(price: float) -> float:
        room = price - entry if direction == "long" else entry - price
        return room / risk

    if mode == "fixed_r":
        return fixed if reward + 1e-9 >= min_r else None

    if opposing is None:
        return None
    if direction == "long" and opposing <= entry:
        return None
    if direction == "short" and opposing >= entry:
        return None
    available = room_r(float(opposing))
    if available + 1e-9 < min_r:
        return None
    if mode == "opposing_zone":
        return min(float(opposing), capped) if direction == "long" else max(float(opposing), capped)
    # first of the configured R target and the opposing boundary
    if direction == "long":
        return min(fixed, float(opposing), capped)
    return max(fixed, float(opposing), capped)


def _local(bar: dict) -> datetime:
    stamp = bar["time"]
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=INDIA_TZ)
    return stamp.astimezone(INDIA_TZ)


def _slot(bar: dict) -> tuple:
    local = _local(bar)
    return local.date(), local.strftime("%H:%M")


def _is_swing(bars: list[dict], index: int, strength: int, side: str) -> bool:
    if index < strength or index + strength >= len(bars):
        return False
    field = "low" if side == "demand" else "high"
    value = bars[index][field]
    for offset in range(1, strength + 1):
        left = bars[index - offset][field]
        right = bars[index + offset][field]
        if side == "demand" and not (value < left and value < right):
            return False
        if side == "supply" and not (value > left and value > right):
            return False
    return True


def _median_prior_slot_volume(history: dict, slot: str, day, lookback: int) -> Optional[float]:
    prior = [volume for (session, name), volume in history.items() if name == slot and session < day]
    if not prior:
        return None
    return float(median(prior[-lookback:]))


def _same_slot_relative_volume(history: dict, bar: dict, lookback: int) -> Optional[float]:
    day, slot = _slot(bar)
    baseline = _median_prior_slot_volume(history, slot, day, lookback)
    volume = float(bar.get("volume") or 0)
    if baseline is None or baseline <= 0:
        return None
    return volume / baseline


class SymbolSetup:
    def __init__(self, signals: dict, rejections: list, zones: list, ema_fast: list, ema_slow: list):
        self.signals = signals
        self.rejections = rejections
        self.zones = zones
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow

    def zone_by_id(self, zone_id: str) -> Optional[dict]:
        return next((zone for zone in self.zones if zone["id"] == zone_id), None)


def prepare_symbol(symbol: str, bars: list[dict], config: dict) -> SymbolSetup:
    settings = {**DEFAULTS, **config}
    fast_period = int(settings["fast_period"])
    slow_period = int(settings["slow_period"])
    strength = max(1, int(settings["pivot_strength"]))
    closes = [bar["close"] for bar in bars]
    ema_fast = ema_series(closes, fast_period)
    ema_slow = ema_series(closes, slow_period)
    atrs = atr_series(bars, int(settings["atr_period"]))
    volume_history: dict = {}
    active: list[dict] = []
    zones: list[dict] = []
    signals: dict[int, dict] = {}
    rejections: list[dict] = []

    def reject(index: int, reason: str, detail: str, zone_id: Optional[str] = None) -> None:
        rejections.append({
            "symbol": symbol,
            "time": _local(bars[index]).isoformat(),
            "reason": reason,
            "detail": detail,
            "zone_id": zone_id,
        })

    for index, bar in enumerate(bars):
        _invalidate_zones(active, zones, bars, index, atrs, settings)
        _expire_zones(active, zones, index, settings)
        if index >= strength * 2:
            _confirm_pivots(symbol, bars, index, strength, atrs, active, zones, settings, reject)
        _evaluate_entries(
            symbol, bars, index, active, ema_fast, ema_slow, atrs, volume_history, signals, settings, reject,
        )
        _update_visits(active, bar)
        day, slot = _slot(bar)
        volume_history[(day, slot)] = float(bar.get("volume") or 0)

    for zone in zones:
        zone["age_bars"] = max(0, len(bars) - 1 - zone["confirmed_index"])
        zone["touches"] = int(zone.get("touches") or 0)
    return SymbolSetup(signals, rejections, zones, ema_fast, ema_slow)


def _confirm_pivots(symbol, bars, index, strength, atrs, active, zones, settings, reject) -> None:
    pivot_index = index - strength
    if pivot_index < strength:
        return
    for side in ("demand", "supply"):
        if not _is_swing(bars, pivot_index, strength, side):
            continue
        zone = _construct_zone(symbol, bars, pivot_index, index, side, atrs, active, settings)
        if zone is None:
            reject(index, "NO_VALID_ZONE", f"Confirmed {side} swing did not pass the zone quality rules.")
            continue
        if zone["status"] != "active":
            zones.append(zone)
            reject(index, "NO_VALID_ZONE", zone["reason"], zone["id"])
            continue
        active.append(zone)
        zones.append(zone)


def _construct_zone(symbol, bars, pivot_index, confirmed_index, side, atrs, active, settings) -> Optional[dict]:
    candle = bars[pivot_index]
    atr = atrs[confirmed_index]
    if atr is None or atr <= 0:
        return None
    if side == "demand":
        lower = float(candle["low"])
        upper = float(max(candle["open"], candle["close"]))
    else:
        upper = float(candle["high"])
        lower = float(min(candle["open"], candle["close"]))
    width = upper - lower
    zone_id = f"{symbol}-{side}-{pivot_index}"
    base = {
        "id": zone_id,
        "symbol": symbol,
        "side": side,
        "lower": round(lower, 4),
        "upper": round(upper, 4),
        "pivot_index": pivot_index,
        "confirmed_index": confirmed_index,
        "created_time": _local(candle).isoformat(),
        "confirmed_time": _local(bars[confirmed_index]).isoformat(),
        "invalidated_index": None,
        "invalidated_time": None,
        "status": "rejected",
        "touches": 0,
        "visit_open": False,
        "visit_consumed": False,
        "departure_atr": 0.0,
        "departure_relative_volume": None,
        "reason": "",
        "age_bars": 0,
    }
    if width <= 0:
        base["reason"] = "Zone width is zero, so the base candle has no price interval."
        return base
    min_width = float(settings["zone_width_min_atr"]) * atr
    max_width = float(settings["zone_width_max_atr"]) * atr
    if width < min_width or width > max_width:
        base["reason"] = f"Zone width {width:.4f} is outside {min_width:.4f} to {max_width:.4f}."
        return base

    window = bars[pivot_index + 1:confirmed_index + 1]
    if side == "demand":
        departure = max(bar["high"] for bar in window) - upper
    else:
        departure = lower - min(bar["low"] for bar in window)
    departure_atr = departure / atr
    base["departure_atr"] = round(departure_atr, 4)
    if departure_atr < float(settings["departure_atr"]):
        base["reason"] = f"Departure {departure_atr:.2f} ATR is below {float(settings['departure_atr']):.2f}."
        return base

    impulse = max(window, key=lambda bar: bar["high"] - upper if side == "demand" else lower - bar["low"])
    baseline_volumes = [
        float(bar.get("volume") or 0)
        for bar in bars[max(0, pivot_index - 20):pivot_index]
    ]
    baseline = float(median(baseline_volumes)) if baseline_volumes else 0.0
    departure_volume = float(impulse.get("volume") or 0) / baseline if baseline > 0 else None
    base["departure_relative_volume"] = round(departure_volume, 4) if departure_volume is not None else None
    minimum_departure_volume = float(settings["min_departure_relative_volume"])
    if minimum_departure_volume > 0 and (departure_volume is None or departure_volume < minimum_departure_volume):
        base["reason"] = "Departure volume is below the configured relative-volume floor."
        return base

    gap = _opposing_gap(lower, upper, side, active)
    minimum_gap = float(settings["min_opposing_distance_atr"]) * atr
    if gap is not None and gap < minimum_gap:
        base["reason"] = f"Nearest opposing zone is {gap:.4f} away, inside the {minimum_gap:.4f} ATR buffer."
        return base

    label = "demand" if side == "demand" else "supply"
    edge = "greater of the open and close" if side == "demand" else "lesser of the open and close"
    base["status"] = "active"
    base["reason"] = (
        f"{label.title()} zone from a swing {'low' if side == 'demand' else 'high'} confirmed after {int(settings['pivot_strength'])} "
        f"closed candles on the right. The outer price is the candle {'low' if side == 'demand' else 'high'} and the inner price is the {edge}. "
        f"Departure was {departure_atr:.2f} ATR."
    )
    return base


def _opposing_gap(lower: float, upper: float, side: str, active: list[dict]) -> Optional[float]:
    opposite = "supply" if side == "demand" else "demand"
    gaps = []
    for zone in active:
        if zone["side"] != opposite or zone["status"] != "active":
            continue
        if side == "demand" and zone["lower"] >= upper:
            gaps.append(zone["lower"] - upper)
        if side == "supply" and zone["upper"] <= lower:
            gaps.append(lower - zone["upper"])
    return min(gaps) if gaps else None


def _invalidate_zones(active, zones, bars, index, atrs, settings) -> None:
    atr = atrs[index] or 0.0
    buffer = float(settings["invalidation_atr_buffer"]) * atr
    close = bars[index]["close"]
    still_active = []
    for zone in active:
        breached = False
        if zone["side"] == "demand" and close < zone["lower"] - buffer:
            breached = True
        if zone["side"] == "supply" and close > zone["upper"] + buffer:
            breached = True
        if breached and index > zone["confirmed_index"]:
            zone["status"] = "invalidated"
            zone["invalidated_index"] = index
            zone["invalidated_time"] = _local(bars[index]).isoformat()
            continue
        still_active.append(zone)
    active[:] = still_active


def _expire_zones(active, zones, index, settings) -> None:
    max_age = int(settings["max_zone_age_bars"])
    still_active = []
    for zone in active:
        age = index - zone["confirmed_index"]
        if age > max_age:
            zone["status"] = "expired"
            zone["age_bars"] = age
            continue
        still_active.append(zone)
    active[:] = still_active


def _update_visits(active, bar) -> None:
    for zone in active:
        intersects = bar["low"] <= zone["upper"] and bar["high"] >= zone["lower"]
        if zone.get("visit_open") and not intersects:
            zone["touches"] = int(zone.get("touches") or 0) + 1
            zone["visit_open"] = False
            zone["visit_consumed"] = False
            zone["visit_reject_reason"] = None


def _evaluate_entries(symbol, bars, index, active, ema_fast, ema_slow, atrs, volume_history, signals, settings, reject) -> None:
    can_execute = index + 1 < len(bars)
    bar = bars[index]
    fast = ema_fast[index]
    slow = ema_slow[index]
    atr = atrs[index]
    mode = settings["entry_mode"]
    needs_trend = mode in {"touch_trend", "touch_trend_rejection"}
    needs_rejection = mode in {"touch_rejection", "touch_trend_rejection"}
    relative_volume = _same_slot_relative_volume(volume_history, bar, int(settings["volume_lookback_sessions"]))

    for zone in list(active):
        intersects = bar["low"] <= zone["upper"] and bar["high"] >= zone["lower"]
        if not intersects or index < zone["confirmed_index"]:
            continue
        if not zone.get("visit_open"):
            zone["visit_open"] = True
            zone["visit_consumed"] = False
        if zone.get("visit_consumed"):
            continue
        prior_touches = int(zone.get("touches") or 0)
        if prior_touches > int(settings["max_prior_touches"]):
            continue
        direction = "long" if zone["side"] == "demand" else "short"
        if direction == "short" and settings.get("direction") != "long_short":
            continue
        failure = _setup_failure(
            bars, index, zone, direction, fast, slow, ema_fast, ema_slow, atr, relative_volume,
            needs_trend, needs_rejection, settings,
        )
        if failure:
            if zone.get("visit_reject_reason") != failure[0]:
                zone["visit_reject_reason"] = failure[0]
                reject(index, failure[0], failure[1], zone["id"])
            continue
        stop = _stop_price(bars, index, zone, direction, atr, settings)
        if stop is None:
            reject(index, "POSITION_RISK_LIMIT", "The selected stop is not on the protective side of price.", zone["id"])
            continue
        opposing = _nearest_opposing(zone, direction, active, bars[index]["close"])
        if settings["target_mode"] != "fixed_r":
            estimated = resolve_target(bars[index]["close"], stop, direction, {
                "target_mode": settings["target_mode"],
                "opposing_price": opposing,
                "reward_to_risk": settings["reward_to_risk"],
                "max_target_r": settings["max_target_r"],
                "min_reward_to_risk": settings["min_reward_to_risk"],
            })
            if estimated is None:
                reject(index, "INSUFFICIENT_TARGET_SPACE", "Room to the opposing zone is below the minimum reward to risk.", zone["id"])
                continue
        if not can_execute:
            continue
        zone["visit_consumed"] = True
        signals[index + 1] = {
            "direction": direction,
            "reason": _entry_text(direction, zone),
            "stop": stop,
            "signal_index": index,
            "zone_id": zone["id"],
            "prior_touches": prior_touches,
            "relative_volume": None if relative_volume is None else round(relative_volume, 4),
            "signal_volume": float(bar.get("volume") or 0),
            "target_mode": settings["target_mode"],
            "opposing_price": opposing,
            "reward_to_risk": float(settings["reward_to_risk"]),
            "max_target_r": float(settings["max_target_r"]),
            "min_reward_to_risk": float(settings["min_reward_to_risk"]),
            "entry_signal": _entry_text(direction, zone),
        }


def _setup_failure(bars, index, zone, direction, fast, slow, ema_fast, ema_slow, atr, relative_volume, needs_trend, needs_rejection, settings):
    if zone["status"] != "active":
        return "ZONE_INVALIDATED", "The tapped zone is no longer active."
    if needs_trend:
        if fast is None or slow is None:
            return "EMA_FILTER_FAILED", "EMA values are not available yet."
        aligned = fast > slow if direction == "long" else fast < slow
        if not aligned:
            return "EMA_FILTER_FAILED", "EMA alignment does not match the zone."
        if settings["trend_mode"] == "fresh_crossover" and not _recent_cross(ema_fast, ema_slow, index, int(settings["crossover_lookback"]), direction):
            return "EMA_FILTER_FAILED", "No fresh EMA crossover inside the lookback."
        if settings.get("require_slow_ema_slope") and not _slope_matches(ema_slow, index, int(settings["slope_lookback"]), direction):
            return "EMA_FILTER_FAILED", "The slow EMA slope does not confirm the trend."
    if needs_rejection and not _rejection_candle(bars[index], zone, direction):
        return "REJECTION_CONFIRMATION_FAILED", "The completed candle did not reject the zone."
    if settings.get("volume_filter_enabled", True):
        minimum = float(settings["min_relative_volume"])
        if relative_volume is None or relative_volume < minimum:
            return "LOW_RELATIVE_VOLUME", "Same-slot relative volume is below the threshold."
    if atr is None or atr <= 0:
        return "POSITION_RISK_LIMIT", "ATR is not available for the stop."
    return None


def _recent_cross(ema_fast, ema_slow, index, lookback, direction) -> bool:
    start = max(1, index - lookback + 1)
    for cursor in range(start, index + 1):
        previous_fast, previous_slow = ema_fast[cursor - 1], ema_slow[cursor - 1]
        fast, slow = ema_fast[cursor], ema_slow[cursor]
        if None in (previous_fast, previous_slow, fast, slow):
            continue
        if direction == "long" and previous_fast <= previous_slow and fast > slow:
            return True
        if direction == "short" and previous_fast >= previous_slow and fast < slow:
            return True
    return False


def _slope_matches(ema_slow, index, lookback, direction) -> bool:
    earlier = ema_slow[index - lookback] if index >= lookback else None
    current = ema_slow[index]
    if earlier is None or current is None:
        return False
    return current > earlier if direction == "long" else current < earlier


def _rejection_candle(bar, zone, direction) -> bool:
    if direction == "long":
        return bar["close"] > bar["open"] and bar["close"] > zone["upper"]
    return bar["close"] < bar["open"] and bar["close"] < zone["lower"]


def _stop_price(bars, index, zone, direction, atr, settings) -> Optional[float]:
    method = settings["stop_method"]
    close = bars[index]["close"]
    buffer = float(settings["atr_buffer"]) * atr
    if method == "atr":
        distance = float(settings["atr_stop_multiple"]) * atr
        stop = close - distance if direction == "long" else close + distance
    elif method == "swing":
        stop = _recent_swing(bars, index, direction, int(settings["swing_lookback"]), int(settings["pivot_strength"]))
    else:
        stop = zone["lower"] - buffer if direction == "long" else zone["upper"] + buffer
    if stop is None:
        return None
    if direction == "long" and stop >= close:
        return None
    if direction == "short" and stop <= close:
        return None
    return stop


def _recent_swing(bars, index, direction, lookback, strength) -> Optional[float]:
    side = "demand" if direction == "long" else "supply"
    start = max(strength, index - lookback)
    for pivot in range(index - strength, start - 1, -1):
        if _is_swing(bars, pivot, strength, side):
            return bars[pivot]["low"] if direction == "long" else bars[pivot]["high"]
    return None


def _nearest_opposing(zone, direction, active, price: float) -> Optional[float]:
    opposite = "supply" if direction == "long" else "demand"
    candidates = []
    for other in active:
        if other["id"] == zone["id"] or other["side"] != opposite or other["status"] != "active":
            continue
        if direction == "long" and other["lower"] > price:
            candidates.append(other["lower"])
        if direction == "short" and other["upper"] < price:
            candidates.append(other["upper"])
    if not candidates:
        return None
    return min(candidates) if direction == "long" else max(candidates)


def _entry_text(direction: str, zone: dict) -> str:
    side = "demand" if direction == "long" else "supply"
    return f"{side.title()} zone {zone['id']} with EMA alignment and completed-candle confirmation."


def next_exit_reason(setup: SymbolSetup, position: dict, bars: list[dict], index: int, config: dict) -> Optional[str]:
    if index <= position["entry_index"]:
        return None
    settings = {**DEFAULTS, **config}
    zone = setup.zone_by_id(position.get("zone_id", ""))
    if zone and zone.get("invalidated_index") is not None and zone["invalidated_index"] <= index:
        return "ZONE_INVALIDATION"
    fast = setup.ema_fast[index] if index < len(setup.ema_fast) else None
    slow = setup.ema_slow[index] if index < len(setup.ema_slow) else None
    previous_fast = setup.ema_fast[index - 1] if index > 0 else None
    previous_slow = setup.ema_slow[index - 1] if index > 0 else None
    direction = position["direction"]
    if None not in (fast, slow, previous_fast, previous_slow):
        if direction == "long" and previous_fast >= previous_slow and fast < slow:
            return "OPPOSITE_EMA"
        if direction == "short" and previous_fast <= previous_slow and fast > slow:
            return "OPPOSITE_EMA"
    if not settings.get("momentum_failure_enabled", True):
        return None
    held = index - position["entry_index"]
    if held < int(settings["momentum_failure_bars"]):
        return None
    span = abs(position["target"] - position["entry_price"])
    if span <= 0:
        return None
    close = bars[index]["close"]
    progress = (close - position["entry_price"]) / span if direction == "long" else (position["entry_price"] - close) / span
    trend_failed = False
    if fast is not None and slow is not None:
        trend_failed = fast < slow if direction == "long" else fast > slow
    zone_failed = bool(zone and zone.get("status") in {"invalidated", "expired"})
    if progress < float(settings["momentum_progress_threshold"]) and (trend_failed or zone_failed):
        return "MOMENTUM_FAILURE"
    return None
