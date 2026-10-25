from collections import defaultdict
from datetime import time
from math import floor
from typing import Optional
from zoneinfo import ZoneInfo

from backend.backtesting import STARTING_CAPITAL, parse_bars
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
    return {
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


def run_event_backtest(
    candles_by_symbol: dict[str, list],
    strategy_id: str,
    config: dict,
    cancel_check=None,
) -> dict:
    if strategy_id not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy_id}")
    strategy_config = {**STRATEGIES[strategy_id].defaults, **config.get("strategy", {})}
    common = config.get("common", {})
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
    entry_start_time = time.fromisoformat(common.get("entry_start_time", "09:20"))
    entry_cutoff = time.fromisoformat(common.get("entry_cutoff", "14:30"))
    cost_model = config.get("costs", {})
    costs_configured = all(cost_model.get(field) is not None for field in COST_FIELDS)
    normalized = {**common, "max_holding_bars": max_holding_bars}

    bars_by_symbol = {symbol: parse_bars(candles) for symbol, candles in candles_by_symbol.items()}
    entry_signals = {}
    stock_summary = {}
    events = []
    skipped_signals = 0
    for symbol, bars in bars_by_symbol.items():
        stock_summary[symbol] = {"symbol": symbol, "signals": 0, "trades": 0, "wins": 0, "losses": 0, "gross_pnl": 0.0, "costs": 0.0, "net_pnl": None}
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
    losing_day_streak = 0
    cooldown_remaining = 0
    day_is_cooldown = False

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
            exit_result = _exit_for_bar(position, bar, index, normalized)
            if exit_result:
                exit_price, reason = exit_result
                provisional = _finalize_trade(position, exit_price, reason, bar, equity, cost_model)
                realized = provisional["net_pnl"] if provisional["net_pnl"] is not None else provisional["gross_pnl"]
                equity += realized
                daily_realized += realized
                peak_equity = max(peak_equity, equity)
                max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100 if peak_equity else 0)
                provisional["equity_after"] = round(equity, 2)
                trades.append(provisional)
                row = stock_summary[symbol]
                row["trades"] += 1
                row["wins"] += int(realized > 0)
                row["losses"] += int(realized < 0)
                row["gross_pnl"] += provisional["gross_pnl"]
                if provisional["costs"] is not None:
                    row["costs"] += provisional["costs"]["total"]
                del positions[symbol]

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
            rejected_signals += 1
            continue
        if local_time < entry_start_time or local_time > entry_cutoff:
            rejected_signals += 1
            continue
        if daily_realized <= -daily_start_equity * daily_loss_limit_percent / 100:
            rejected_signals += 1
            continue
        if symbol in positions or len(positions) >= max_positions:
            rejected_signals += 1
            continue
        if entries_per_day[(symbol, local_date)] >= max_entries_per_day:
            rejected_signals += 1
            continue
        direction = signal["direction"]
        if direction == "short" and strategy_config.get("direction", "long_only") != "long_short":
            rejected_signals += 1
            continue
        entry_price = bar["open"]
        stop = float(signal["stop"])
        per_share = entry_price - stop if direction == "long" else stop - entry_price
        if per_share <= 0:
            rejected_signals += 1
            continue
        equity_risk_cap = min(equity, initial_capital) * risk_percent / 100
        aggregate_cap = equity * aggregate_risk_percent / 100
        open_risk = sum(item["planned_risk"] for item in positions.values())
        risk_budget = min(equity_risk_cap, max(0, aggregate_cap - open_risk))
        quantity_by_risk = floor(risk_budget / per_share)
        allocation_cap = equity * max_allocation_percent / 100
        quantity_by_allocation = floor(allocation_cap / entry_price) if entry_price > 0 else 0
        quantity = max(0, min(quantity_by_risk, quantity_by_allocation))
        if quantity < 1:
            rejected_signals += 1
            continue

        planned_risk = quantity * per_share
        reward_to_risk = float(strategy_config.get("reward_to_risk", 3.0))
        target = entry_price + per_share * reward_to_risk if direction == "long" else entry_price - per_share * reward_to_risk
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
        }
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

    return {
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
    }