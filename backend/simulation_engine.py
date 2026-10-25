import copy
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from math import floor
from typing import Optional
from zoneinfo import ZoneInfo

from backend.backtesting import STARTING_CAPITAL, parse_bars
from backend.ema_zones import execution_time_block, next_exit_reason, prepare_symbol, resolve_target
from backend.strategy_registry import STRATEGIES, strategy_signal


INDIA_TZ = ZoneInfo("Asia/Kolkata")
COST_FIELDS = (
    "brokerage_per_order",
    "stt_sell_percent",
    "exchange_turnover_percent",
    "gst_percent",
    "stamp_buy_percent",
    "sebi_per_crore",
)


def calculate_costs(direction: str, quantity: int, entry: float, exit_price: float, model: dict) -> Optional[dict]:
    if any(model.get(field) is None for field in COST_FIELDS):
        return None
    entry_turnover = entry * quantity
    exit_turnover = exit_price * quantity
    buy_turnover = entry_turnover if direction == "long" else exit_turnover
    sell_turnover = exit_turnover if direction == "long" else entry_turnover
    total_turnover = entry_turnover + exit_turnover
    brokerage = float(model["brokerage_per_order"]) * 2
    stt = sell_turnover * float(model["stt_sell_percent"]) / 100
    exchange = total_turnover * float(model["exchange_turnover_percent"]) / 100
    sebi = total_turnover / 10_000_000 * float(model["sebi_per_crore"])
    stamp = buy_turnover * float(model["stamp_buy_percent"]) / 100
    gst = (brokerage + exchange + sebi) * float(model["gst_percent"]) / 100
    spread_slippage = total_turnover * (float(model.get("spread_bps", 0)) + float(model.get("slippage_bps", 0))) / 10_000
    costs = {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_charges": exchange,
        "gst": gst,
        "stamp_duty": stamp,
        "sebi_fees": sebi,
        "spread_slippage": spread_slippage,
    }
    costs["total"] = sum(costs.values())
    return costs


def _variable_cost_rate(model: dict) -> float:
    stt = float(model["stt_sell_percent"]) / 100
    exchange = float(model["exchange_turnover_percent"]) / 100
    stamp = float(model["stamp_buy_percent"]) / 100
    sebi = float(model["sebi_per_crore"]) / 10_000_000
    slip = (float(model.get("spread_bps") or 0) + float(model.get("slippage_bps") or 0)) / 10_000
    gst = float(model["gst_percent"]) / 100
    return stt + (2 * exchange) + stamp + (2 * sebi) + (2 * slip) + (gst * 2 * exchange)


def _quantity_with_costs(risk_budget: float, entry: float, per_share: float, allocation_quantity: int, model: dict) -> int:
    """Size so the stop distance plus an estimate of round-trip costs stays inside the risk budget."""
    fixed = float(model["brokerage_per_order"]) * 2 * (1 + float(model["gst_percent"]) / 100)
    unit = per_share + entry * _variable_cost_rate(model)
    if unit <= 0 or risk_budget <= fixed:
        return 0
    return max(0, min(floor((risk_budget - fixed) / unit), allocation_quantity))


def _compact_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trade_count": 0, "win_rate": 0.0, "gross_pnl": 0.0, "average_r": None}
    wins = sum(trade["gross_pnl"] > 0 for trade in trades)
    r_values = [trade["r_multiple"] for trade in trades if trade.get("r_multiple") is not None]
    return {
        "trade_count": len(trades),
        "win_rate": round(wins / len(trades) * 100, 2),
        "gross_pnl": round(sum(trade["gross_pnl"] for trade in trades), 2),
        "average_r": round(sum(r_values) / len(r_values), 2) if r_values else None,
    }


def _exit_for_bar(position: dict, bar: dict, index: int, config: dict) -> Optional[tuple[float, str]]:
    direction = position["direction"]
    if direction == "long":
        if bar["open"] <= position["stop"]:
            return bar["open"], "STOP_GAP"
        if bar["open"] >= position["target"]:
            return position["target"], "TARGET_GAP"
        if bar["low"] <= position["stop"]:
            return position["stop"], "STOP_LOSS"
        if bar["high"] >= position["target"]:
            return position["target"], "TARGET"
    else:
        if bar["open"] >= position["stop"]:
            return bar["open"], "STOP_GAP"
        if bar["open"] <= position["target"]:
            return position["target"], "TARGET_GAP"
        if bar["high"] >= position["stop"]:
            return position["stop"], "STOP_LOSS"
        if bar["low"] <= position["target"]:
            return position["target"], "TARGET"

    held = index - position["entry_index"]
    if held >= int(config.get("max_holding_bars", 12)):
        return bar["close"], "TIME_EXIT"
    local_time = bar["time"].astimezone(INDIA_TZ).time()
    if config.get("intraday", True) and local_time >= time.fromisoformat(config.get("square_off_time", "15:25")):
        return bar["close"], "END_OF_DAY"
    return None


def _finalize_trade(position: dict, exit_price: float, reason: str, bar: dict, equity: float, cost_model: dict) -> dict:
    direction = position["direction"]
    sign = 1 if direction == "long" else -1
    gross_pnl = (exit_price - position["entry_price"]) * position["quantity"] * sign
    costs = calculate_costs(direction, position["quantity"], position["entry_price"], exit_price, cost_model)
    net_pnl = gross_pnl - costs["total"] if costs is not None else None
    deployed_capital = max(position["entry_price"] * position["quantity"], 1)
    trade = {
        "symbol": position["symbol"],
        "strategy": position["strategy"],
        "direction": direction,
        "signal_time": position["signal_time"].isoformat(),
        "entry_time": position["entry_time"].isoformat(),
        "exit_time": bar["time"].isoformat(),
        "entry_signal": position["entry_reason"],
        "entry_price": round(position["entry_price"], 4),
        "exit_price": round(exit_price, 4),
        "quantity": position["quantity"],
        "initial_stop": round(position["stop"], 4),
        "target": round(position["target"], 4),
        "planned_risk": round(position["planned_risk"], 2),
        "exit_reason": reason,
        "gross_pnl": round(gross_pnl, 2),
        "costs": {key: round(value, 2) for key, value in costs.items()} if costs is not None else None,
        "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
        "return_on_capital_percent": round(net_pnl / deployed_capital * 100, 2) if net_pnl is not None else None,
        "return_on_capital_gross_percent": round(gross_pnl / deployed_capital * 100, 2),
        "r_multiple": round(gross_pnl / position["planned_risk"], 2) if position["planned_risk"] else None,
        "holding_bars": max(1, position["bars_held"]),
        "simulation_status": "SIMULATED",
        "equity_after": round(equity + (net_pnl if net_pnl is not None else gross_pnl), 2),
    }
    for field in ("relative_volume", "zone_id", "prior_touches", "planned_r"):
        if field in position:
            trade[field] = position[field]
    return trade


def _pending_open_exit(position: dict, bar: dict) -> Optional[tuple[float, str]]:
    pending = position.get("pending_exit")
    if not pending:
        return None
    direction = position["direction"]
    if direction == "long":
        if bar["open"] <= position["stop"]:
            return bar["open"], "STOP_GAP"
        if bar["open"] >= position["target"]:
            return bar["open"], "TARGET_GAP"
    else:
        if bar["open"] >= position["stop"]:
            return bar["open"], "STOP_GAP"
        if bar["open"] <= position["target"]:
            return bar["open"], "TARGET_GAP"
    return bar["open"], pending


def _apply_exit(position, exit_price, reason, bar, equity, daily_realized, peak_equity, max_drawdown, trades, stock_summary, cost_model, positions):
    provisional = _finalize_trade(position, exit_price, reason, bar, equity, cost_model)
    realized = provisional["net_pnl"] if provisional["net_pnl"] is not None else provisional["gross_pnl"]
    equity += realized
    daily_realized += realized
    peak_equity = max(peak_equity, equity)
    max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100 if peak_equity else 0)
    provisional["equity_after"] = round(equity, 2)
    trades.append(provisional)
    row = stock_summary[position["symbol"]]
    row["trades"] += 1
    row["wins"] += int(realized > 0)
    row["losses"] += int(realized < 0)
    row["gross_pnl"] += provisional["gross_pnl"]
    if provisional["costs"] is not None:
        row["costs"] += provisional["costs"]["total"]
    del positions[position["symbol"]]
    return equity, daily_realized, peak_equity, max_drawdown


def run_event_backtest(
    candles_by_symbol: dict[str, list],
    strategy_id: str,
    config: dict,
    cancel_check=None,
    close_open_positions: bool = True,
) -> dict:
    if strategy_id not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy_id}")
    strategy_config = {**STRATEGIES[strategy_id].defaults, **config.get("strategy", {})}
    common = config.get("common", {})
    if strategy_id != "ema_supply_demand":
        strategy_config["volume_filter_enabled"] = bool(common.get("volume_filter_enabled", True))
        strategy_config["min_relative_volume"] = float(common.get("min_relative_volume", 1.0))
        strategy_config["volume_lookback"] = int(common.get("volume_lookback", 20))
    initial_capital = float(common.get("starting_capital", STARTING_CAPITAL))
    risk_percent = float(common.get("risk_percent", 0.25))
    aggregate_risk_percent = float(common.get("aggregate_open_risk_percent", 0.5))
    max_positions = int(common.get("max_positions", 3))
    max_allocation_percent = float(common.get("max_allocation_percent", 100))
    max_holding_bars = int(common.get("max_holding_bars", 12))
    max_entries_per_day = int(common.get("max_entries_per_symbol_per_day", 1))
    daily_loss_limit_percent = float(common.get("daily_loss_limit_percent", 1.0))
    losing_day_streak_limit = int(common.get("losing_day_streak_limit", 3))
    cooldown_days = int(common.get("cooldown_days", 3))
    entry_start_time = time.fromisoformat(str(common.get("entry_start_time", "09:20")))
    entry_cutoff = time.fromisoformat(str(common.get("entry_cutoff", "14:30")))
    cutoff_exclusive = False
    cost_model = config.get("costs", {})
    costs_configured = all(cost_model.get(field) is not None for field in COST_FIELDS)
    normalized = {**common, "max_holding_bars": max_holding_bars}
    if strategy_id == "ema_supply_demand":
        entry_start_time = time.fromisoformat(str(strategy_config.get("entry_start_time", "09:30")))
        entry_cutoff = time.fromisoformat(str(strategy_config.get("entry_cutoff", "13:00")))
        cutoff_exclusive = bool(strategy_config.get("cutoff_exclusive", True))
        max_holding_bars = int(strategy_config.get("max_holding_bars", 8))
        normalized["max_holding_bars"] = max_holding_bars
        normalized["square_off_time"] = str(strategy_config.get("square_off_time", "15:15"))
        normalized["intraday"] = True

    bars_by_symbol = {symbol: parse_bars(candles) for symbol, candles in candles_by_symbol.items()}
    entry_signals = {}
    stock_summary = {}
    events = []
    setups = {}
    zone_records = []
    setup_rejections = []
    for symbol, bars in bars_by_symbol.items():
        stock_summary[symbol] = {"symbol": symbol, "signals": 0, "trades": 0, "wins": 0, "losses": 0, "gross_pnl": 0.0, "costs": 0.0, "net_pnl": None}
        if strategy_id == "ema_supply_demand":
            setup = prepare_symbol(symbol, bars, strategy_config)
            setups[symbol] = setup
            zone_records.extend(setup.zones)
            setup_rejections.extend(setup.rejections)
            for exec_index, signal in setup.signals.items():
                stock_summary[symbol]["signals"] += 1
                entry_signals[(symbol, exec_index)] = signal
            for index, bar in enumerate(bars):
                events.append((bar["time"], symbol, index))
            continue
        for index, bar in enumerate(bars):
            events.append((bar["time"], symbol, index))
            signal = strategy_signal(strategy_id, bars, index, strategy_config)
            if signal is not None and index + 1 < len(bars):
                stock_summary[symbol]["signals"] += 1
                entry_signals[(symbol, index + 1)] = signal

    events.sort(key=lambda event: (event[0], event[1]))
    positions = {}
    trades = []
    entries_per_day = defaultdict(int)
    daily_start_equity = initial_capital
    daily_realized = 0.0
    active_day = None
    equity = initial_capital
    peak_equity = initial_capital
    max_drawdown = 0.0
    max_open_positions = 0
    rejected_signals = 0
    rejection_counts = defaultdict(int)
    rejection_samples = []
    losing_day_streak = 0
    cooldown_remaining = 0
    day_is_cooldown = False

    def reject(reason: str, symbol: str, when, detail: str) -> None:
        nonlocal rejected_signals
        rejected_signals += 1
        rejection_counts[reason] += 1
        if len(rejection_samples) < 200:
            rejection_samples.append({
                "symbol": symbol,
                "time": when.isoformat() if hasattr(when, "isoformat") else str(when),
                "reason": reason,
                "detail": detail,
            })

    for timestamp, symbol, index in events:
        if cancel_check and cancel_check():
            break
        bars = bars_by_symbol[symbol]
        bar = bars[index]
        local_date = timestamp.astimezone(INDIA_TZ).date()
        if active_day != local_date:
            if active_day is not None:
                if daily_realized < 0:
                    losing_day_streak += 1
                else:
                    losing_day_streak = 0
                if losing_day_streak >= losing_day_streak_limit:
                    cooldown_remaining = cooldown_days
            active_day = local_date
            daily_start_equity = equity
            daily_realized = 0.0
            day_is_cooldown = cooldown_remaining > 0
            if day_is_cooldown:
                cooldown_remaining -= 1

        position = positions.get(symbol)
        if position is not None and index > position["entry_index"]:
            position["bars_held"] += 1
            pending_exit = _pending_open_exit(position, bar) if position.get("pending_exit") else None
            if pending_exit:
                position.pop("pending_exit", None)
            exit_result = pending_exit or _exit_for_bar(position, bar, index, normalized)
            if exit_result:
                exit_price, reason = exit_result
                equity, daily_realized, peak_equity, max_drawdown = _apply_exit(
                    position, exit_price, reason, bar, equity, daily_realized, peak_equity, max_drawdown,
                    trades, stock_summary, cost_model, positions,
                )
            elif strategy_id == "ema_supply_demand" and symbol in setups:
                follow_up = next_exit_reason(setups[symbol], position, bars, index, strategy_config)
                if follow_up:
                    position["pending_exit"] = follow_up

        signal = entry_signals.get((symbol, index))
        if signal is None:
            continue
        position = positions.get(symbol)
        if position is not None and signal["direction"] != position["direction"]:
            trade = _finalize_trade(position, bar["open"], "OPPOSITE_SIGNAL", bar, equity, cost_model)
            realized = trade["net_pnl"] if trade["net_pnl"] is not None else trade["gross_pnl"]
            equity += realized
            daily_realized += realized
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100 if peak_equity else 0)
            trade["equity_after"] = round(equity, 2)
            trades.append(trade)
            row = stock_summary[symbol]
            row["trades"] += 1
            row["wins"] += int(realized > 0)
            row["losses"] += int(realized < 0)
            row["gross_pnl"] += trade["gross_pnl"]
            if trade["costs"] is not None:
                row["costs"] += trade["costs"]["total"]
            del positions[symbol]
        local_time = timestamp.astimezone(INDIA_TZ).time()
        if day_is_cooldown:
            reject("COOLDOWN", symbol, timestamp, "New entries are paused after the losing-day streak.")
            continue
        time_block = execution_time_block(local_time, entry_start_time, entry_cutoff, cutoff_exclusive)
        if time_block:
            reject(time_block, symbol, timestamp, "The execution bar is outside the entry window.")
            continue
        if daily_realized <= -daily_start_equity * daily_loss_limit_percent / 100:
            reject("POSITION_RISK_LIMIT", symbol, timestamp, "The daily loss limit has been reached.")
            continue
        if symbol in positions or len(positions) >= max_positions:
            reject("POSITION_RISK_LIMIT", symbol, timestamp, "A position is already open or the book is full.")
            continue
        if entries_per_day[(symbol, local_date)] >= max_entries_per_day:
            reject("POSITION_RISK_LIMIT", symbol, timestamp, "This symbol already used its daily entry allowance.")
            continue
        direction = signal["direction"]
        if direction == "short" and strategy_config.get("direction", "long_only") != "long_short":
            reject("POSITION_RISK_LIMIT", symbol, timestamp, "Short entries are disabled for this configuration.")
            continue
        entry_price = bar["open"]
        stop = float(signal["stop"])
        per_share = entry_price - stop if direction == "long" else stop - entry_price
        if per_share <= 0:
            reject("POSITION_RISK_LIMIT", symbol, timestamp, "The filled price is already through the stop.")
            continue
        equity_risk_cap = min(equity, initial_capital) * risk_percent / 100
        aggregate_cap = equity * aggregate_risk_percent / 100
        open_risk = sum(item["planned_risk"] for item in positions.values())
        risk_budget = min(equity_risk_cap, max(0, aggregate_cap - open_risk))
        allocation_cap = equity * max_allocation_percent / 100
        quantity_by_allocation = floor(allocation_cap / entry_price) if entry_price > 0 else 0
        if strategy_id == "ema_supply_demand" and costs_configured:
            quantity = _quantity_with_costs(risk_budget, entry_price, per_share, quantity_by_allocation, cost_model)
        else:
            quantity = max(0, min(floor(risk_budget / per_share), quantity_by_allocation))
        if quantity < 1:
            reject("POSITION_RISK_LIMIT", symbol, timestamp, "Position size is below one share after risk and cost limits.")
            continue
        if strategy_id == "ema_supply_demand":
            participation = float(strategy_config.get("max_participation", 0.01))
            signal_volume = float(signal.get("signal_volume") or 0)
            if signal_volume <= 0 or quantity > signal_volume * participation:
                reject("INSUFFICIENT_LIQUIDITY", symbol, timestamp, "The order is larger than the allowed share of the signal candle's volume.")
                continue
        if signal.get("target_mode"):
            target = resolve_target(entry_price, stop, direction, signal)
            if target is None:
                reject("INSUFFICIENT_TARGET_SPACE", symbol, timestamp, "The filled price does not leave the minimum reward to risk before the opposing zone.")
                continue
        else:
            reward_to_risk = float(strategy_config.get("reward_to_risk", 3.0))
            target = entry_price + per_share * reward_to_risk if direction == "long" else entry_price - per_share * reward_to_risk

        planned_risk = quantity * per_share
        position = {
            "symbol": symbol,
            "strategy": strategy_id,
            "direction": direction,
            "signal_time": bars[signal["signal_index"]]["time"],
            "entry_time": timestamp,
            "entry_index": index,
            "entry_price": entry_price,
            "stop": stop,
            "target": target,
            "quantity": quantity,
            "planned_risk": planned_risk,
            "entry_reason": signal["reason"],
            "bars_held": 0,
            "planned_r": round(abs(target - entry_price) / per_share, 2) if per_share else None,
        }
        if signal.get("zone_id"):
            position["zone_id"] = signal["zone_id"]
            position["prior_touches"] = signal.get("prior_touches")
            position["relative_volume"] = signal.get("relative_volume")
        positions[symbol] = position
        entries_per_day[(symbol, local_date)] += 1
        max_open_positions = max(max_open_positions, len(positions))

        entry_exit = _exit_for_bar(position, bar, index, normalized)
        if entry_exit:
            exit_price, reason = entry_exit
            trade = _finalize_trade(position, exit_price, reason, bar, equity, cost_model)
            realized = trade["net_pnl"] if trade["net_pnl"] is not None else trade["gross_pnl"]
            equity += realized
            daily_realized += realized
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100 if peak_equity else 0)
            trade["equity_after"] = round(equity, 2)
            trades.append(trade)
            row = stock_summary[symbol]
            row["trades"] += 1
            row["wins"] += int(realized > 0)
            row["losses"] += int(realized < 0)
            row["gross_pnl"] += trade["gross_pnl"]
            if trade["costs"] is not None:
                row["costs"] += trade["costs"]["total"]
            del positions[symbol]

    open_positions = []
    if close_open_positions:
        for symbol, position in list(positions.items()):
            bar = bars_by_symbol[symbol][-1]
            trade = _finalize_trade(position, bar["close"], "END_OF_DATA", bar, equity, cost_model)
            realized = trade["net_pnl"] if trade["net_pnl"] is not None else trade["gross_pnl"]
            equity += realized
            trade["equity_after"] = round(equity, 2)
            trades.append(trade)
            row = stock_summary[symbol]
            row["trades"] += 1
            row["wins"] += int(realized > 0)
            row["losses"] += int(realized < 0)
            row["gross_pnl"] += trade["gross_pnl"]
            if trade["costs"] is not None:
                row["costs"] += trade["costs"]["total"]
    else:
        for symbol, position in positions.items():
            open_positions.append({
                "symbol": symbol,
                "direction": position["direction"],
                "entry_time": position["entry_time"].isoformat(),
                "entry_price": round(position["entry_price"], 4),
                "stop": round(position["stop"], 4),
                "target": round(position["target"], 4),
                "quantity": position["quantity"],
                "zone_id": position.get("zone_id"),
            })

    trades.sort(key=lambda trade: trade["entry_time"])
    gross_pnl = sum(trade["gross_pnl"] for trade in trades)
    total_costs = sum(trade["costs"]["total"] for trade in trades if trade["costs"] is not None)
    net_pnl = gross_pnl - total_costs if costs_configured else None
    realized_for_metrics = [trade["net_pnl"] if costs_configured else trade["gross_pnl"] for trade in trades]
    win_values = [value for value in realized_for_metrics if value > 0]
    loss_values = [-value for value in realized_for_metrics if value < 0]
    gross_profit = sum(trade["gross_pnl"] for trade in trades if trade["gross_pnl"] > 0)
    gross_loss = -sum(trade["gross_pnl"] for trade in trades if trade["gross_pnl"] < 0)
    longest_win_streak = longest_loss_streak = current_win_streak = current_loss_streak = 0
    for value in realized_for_metrics:
        if value > 0:
            current_win_streak += 1
            current_loss_streak = 0
        elif value < 0:
            current_loss_streak += 1
            current_win_streak = 0
        longest_win_streak = max(longest_win_streak, current_win_streak)
        longest_loss_streak = max(longest_loss_streak, current_loss_streak)
    wins = len(win_values)
    losses = len(loss_values)
    closed_gross_pnls = [trade["gross_pnl"] for trade in trades]
    for stock in stock_summary.values():
        stock["gross_pnl"] = round(stock["gross_pnl"], 2)
        stock["costs"] = round(stock["costs"], 2) if costs_configured else None
        stock["net_pnl"] = round(stock["gross_pnl"] - stock["costs"], 2) if costs_configured else None
        stock["win_rate"] = round(stock["wins"] / stock["trades"] * 100, 2) if stock["trades"] else 0.0

    result = {
        "starting_capital": initial_capital,
        "ending_equity": round(initial_capital + net_pnl, 2) if net_pnl is not None else None,
        "ending_equity_gross": round(initial_capital + gross_pnl, 2),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
        "total_costs": round(total_costs, 2) if costs_configured else None,
        "costs_configured": costs_configured,
        "cost_warning": None if costs_configured else "Transaction cost rates are incomplete; net results are unavailable.",
        "return_percent": round((net_pnl if net_pnl is not None else gross_pnl) / initial_capital * 100, 2),
        "trade_count": len(trades),
        "winning_trades": wins,
        "losing_trades": losses,
        "break_even_trades": len(trades) - wins - losses,
        "win_rate": round(len(win_values) / len(trades) * 100, 2) if trades else 0.0,
        "win_rate_denominator": "all completed trades, including break-even trades",
        "average_win": round(sum(win_values) / len(win_values), 2) if win_values else 0.0,
        "average_loss": round(-sum(loss_values) / len(loss_values), 2) if loss_values else 0.0,
        "expectancy_gross": round(sum(closed_gross_pnls) / len(closed_gross_pnls), 2) if trades else 0.0,
        "expectancy_net": round(sum(realized_for_metrics) / len(realized_for_metrics), 2) if costs_configured and trades else None,
        "expectancy_basis": "net after configured costs" if costs_configured else "gross; costs not configured",
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(sum(win_values) / sum(loss_values), 2) if loss_values else None,
        "max_drawdown_percent": round(max_drawdown, 2),
        "average_holding_bars": round(sum(trade["holding_bars"] for trade in trades) / len(trades), 2) if trades else 0.0,
        "longest_winning_streak": longest_win_streak,
        "longest_losing_streak": longest_loss_streak,
        "maximum_open_positions": max_open_positions,
        "signals_rejected": rejected_signals,
        "loss_streak_cooldown": {"losing_days_trigger": losing_day_streak_limit, "pause_days": cooldown_days},
        "universe_size": len(candles_by_symbol),
        "stocks_with_signals": sum(stock["signals"] > 0 for stock in stock_summary.values()),
        "stocks_with_trades": sum(stock["trades"] > 0 for stock in stock_summary.values()),
        "survivorship_warning": "Uses the current Nifty 100 constituents for all dates; historical membership is unavailable.",
        "trades": trades,
        "stock_performance": sorted(stock_summary.values(), key=lambda stock: stock["gross_pnl"], reverse=True),
        "equity_curve": [{"time": trade["exit_time"], "equity": trade["equity_after"]} for trade in trades],
        "open_positions": open_positions,
    }
    if strategy_id == "ema_supply_demand":
        for item in setup_rejections:
            rejection_counts[item["reason"]] += 1
            if len(rejection_samples) < 200:
                rejection_samples.append(item)
        result["rejection_breakdown"] = dict(rejection_counts)
        result["rejection_samples"] = rejection_samples
        result["zones"] = [_public_zone(zone) for zone in zone_records[:400]]
        result["zone_count"] = len(zone_records)
        result["average_realized_r"] = _compact_metrics(trades)["average_r"]
        result["exit_mix"] = _exit_mix(trades)
        result["direction_breakdown"] = {
            "long": _compact_metrics([trade for trade in trades if trade["direction"] == "long"]),
            "short": _compact_metrics([trade for trade in trades if trade["direction"] == "short"]),
        }
        result["entry_time_breakdown"] = _entry_time_breakdown(trades)
        result["zone_touch_breakdown"] = _touch_breakdown(trades)
        result["period_splits"] = _period_splits(trades, common.get("start_date"), common.get("end_date"))
        result["short_sale_warning"] = "Short sales are simulated for intraday research. The simulator does not apply exchange short-sale bans."
        if not config.get("_skip_sensitivity"):
            result["charts"] = _chart_cache(bars_by_symbol, zone_records, trades)
            result["sensitivity"] = _sensitivity(candles_by_symbol, strategy_id, config, cancel_check)
    return result


def _public_zone(zone: dict) -> dict:
    fields = (
        "id", "symbol", "side", "lower", "upper", "created_time", "confirmed_time",
        "invalidated_time", "status", "touches", "departure_atr", "departure_relative_volume",
        "reason", "age_bars",
    )
    return {field: zone[field] for field in fields if field in zone}


def _exit_mix(trades: list[dict]) -> dict:
    def share(reasons: set[str]) -> dict:
        hits = sum(trade["exit_reason"] in reasons for trade in trades)
        return {"count": hits, "percent": round(hits / len(trades) * 100, 2) if trades else 0.0}
    return {
        "target": share({"TARGET", "TARGET_GAP"}),
        "stop": share({"STOP_LOSS", "STOP_GAP"}),
        "time_out": share({"TIME_EXIT"}),
    }


def _entry_time_breakdown(trades: list[dict]) -> dict:
    buckets = {"09:30-10:30": [], "10:30-11:30": [], "11:30-13:00": [], "other": []}
    for trade in trades:
        stamp = datetime.fromisoformat(trade["entry_time"]).astimezone(INDIA_TZ)
        minutes = stamp.hour * 60 + stamp.minute
        if 9 * 60 + 30 <= minutes < 10 * 60 + 30:
            buckets["09:30-10:30"].append(trade)
        elif minutes < 11 * 60 + 30:
            buckets["10:30-11:30"].append(trade)
        elif minutes < 13 * 60:
            buckets["11:30-13:00"].append(trade)
        else:
            buckets["other"].append(trade)
    return {name: _compact_metrics(rows) for name, rows in buckets.items()}


def _touch_breakdown(trades: list[dict]) -> dict:
    groups = defaultdict(list)
    for trade in trades:
        touches = trade.get("prior_touches")
        key = "unknown" if touches is None else f"{int(touches)} prior touches"
        groups[key].append(trade)
    return {key: _compact_metrics(rows) for key, rows in groups.items()}


def _period_splits(trades: list[dict], start_value, end_value) -> dict:
    if not start_value or not end_value:
        return {}
    start = date.fromisoformat(str(start_value))
    end = date.fromisoformat(str(end_value))
    span = max((end - start).days, 1)
    first = start + timedelta(days=span // 3)
    second = start + timedelta(days=(2 * span) // 3)
    buckets = {"development": [], "validation": [], "out_of_sample": []}
    for trade in trades:
        day = datetime.fromisoformat(trade["entry_time"]).astimezone(INDIA_TZ).date()
        if day < first:
            buckets["development"].append(trade)
        elif day < second:
            buckets["validation"].append(trade)
        else:
            buckets["out_of_sample"].append(trade)
    return {
        "boundaries": {"development_before": first.isoformat(), "validation_before": second.isoformat(), "out_of_sample_through": end.isoformat()},
        "slices": {name: _compact_metrics(rows) for name, rows in buckets.items()},
        "note": "Date-range thirds are reported separately. Settings are not chosen from the development slice.",
    }


def _chart_cache(bars_by_symbol: dict, zones: list[dict], trades: list[dict]) -> dict:
    counts = defaultdict(int)
    for trade in trades:
        counts[trade["symbol"]] += 1
    charts = {}
    for symbol, _count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]:
        bars = bars_by_symbol.get(symbol, [])
        charts[symbol] = {
            "bars": [
                {"time": bar["time"].isoformat(), "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"]}
                for bar in bars[-240:]
            ],
            "zones": [_public_zone(zone) for zone in zones if zone["symbol"] == symbol],
            "trades": [trade for trade in trades if trade["symbol"] == symbol],
        }
    return charts


def _sensitivity(candles_by_symbol: dict, strategy_id: str, config: dict, cancel_check) -> dict:
    def replay(**overrides):
        cloned = copy.deepcopy(config)
        cloned["_skip_sensitivity"] = True
        cloned.setdefault("strategy", {})
        cloned["strategy"].update(overrides)
        replayed = run_event_backtest(candles_by_symbol, strategy_id, cloned, cancel_check=cancel_check)
        return _compact_metrics(replayed["trades"])

    return {
        "volume_filter_off": replay(volume_filter_enabled=False),
        "ema_slope_filter_off": replay(require_slow_ema_slope=False),
        "note": "These replays use the same candles. They are not an optimised parameter search.",
    }