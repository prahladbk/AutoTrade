import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.ema_zones import DEFAULTS, atr_series, ema_series, execution_time_block, prepare_symbol, resolve_target
from backend.simulation_engine import _quantity_with_costs, run_event_backtest


INDIA = ZoneInfo("Asia/Kolkata")


def loose_config(**overrides):
    config = {
        **DEFAULTS,
        "fast_period": 2,
        "slow_period": 3,
        "require_slow_ema_slope": False,
        "trend_mode": "trend",
        "pivot_strength": 1,
        "zone_width_min_atr": 0,
        "zone_width_max_atr": 50,
        "departure_atr": 0,
        "min_departure_relative_volume": 0,
        "max_zone_age_bars": 300,
        "max_prior_touches": 0,
        "min_opposing_distance_atr": 0,
        "atr_period": 2,
        "invalidation_atr_buffer": 0.05,
        "entry_mode": "touch_trend_rejection",
        "volume_filter_enabled": False,
        "stop_method": "zone_atr",
        "atr_buffer": 0.1,
        "target_mode": "fixed_r",
        "reward_to_risk": 2,
        "max_target_r": 2,
        "min_reward_to_risk": 1,
        "momentum_failure_enabled": False,
        "max_holding_bars": 40,
        "direction": "long_short",
        "max_participation": 1,
        "entry_start_time": "09:30",
        "entry_cutoff": "13:00",
        "cutoff_exclusive": True,
        "square_off_time": "15:15",
    }
    config.update(overrides)
    return config


def candle(day, hour, minute, open_price, high, low, close, volume=100000):
    stamp = datetime(2026, 1, day, hour, minute, tzinfo=INDIA)
    return [stamp.isoformat(), open_price, high, low, close, volume]


def bar(day, hour, minute, open_price, high, low, close, volume=100000):
    row = candle(day, hour, minute, open_price, high, low, close, volume)
    return {"time": datetime.fromisoformat(row[0]), "open": open_price, "high": high, "low": low, "close": close, "volume": volume}


def rising_warmup(day=5):
    rows = []
    price = 100.0
    hour, minute = 10, 0
    for _ in range(8):
        rows.append(bar(day, hour, minute, price, price + 0.6, price - 0.3, price + 0.4))
        price += 0.8
        minute += 5
        if minute >= 60:
            hour += 1
            minute -= 60
    return rows


class EmaZoneTests(unittest.TestCase):
    def test_ema_seeds_with_the_simple_average_then_smooths(self):
        series = ema_series([1, 2, 3, 4, 5, 6, 7, 8], 3)

        self.assertIsNone(series[1])
        self.assertAlmostEqual(series[2], 2)
        self.assertAlmostEqual(series[3], 3)
        self.assertAlmostEqual(series[4], 4)

    def test_pivot_is_not_usable_before_the_right_side_closes(self):
        bars = rising_warmup()
        bars.append(bar(5, 10, 40, 106, 106.4, 100, 104))
        bars.append(bar(5, 10, 45, 104, 108, 103, 107))
        early = prepare_symbol("TEST", bars[:-1], loose_config())
        confirmed = prepare_symbol("TEST", bars, loose_config())

        self.assertFalse(any(zone["status"] == "active" for zone in early.zones))
        self.assertTrue(any(zone["confirmed_index"] == len(bars) - 1 for zone in confirmed.zones))

    def test_demand_zone_uses_the_low_and_the_higher_of_open_or_close(self):
        bars = rising_warmup()
        bars.append(bar(5, 10, 40, 103, 106, 99, 104))
        bars.append(bar(5, 10, 45, 104, 109, 103.5, 108))
        zones = [zone for zone in prepare_symbol("TEST", bars, loose_config()).zones if zone["side"] == "demand" and zone["status"] == "active"]

        self.assertEqual(zones[-1]["lower"], 99)
        self.assertEqual(zones[-1]["upper"], 104)

    def test_close_beyond_the_buffer_invalidates_the_zone(self):
        bars = rising_warmup()
        bars.append(bar(5, 10, 40, 103, 106, 99, 104))
        bars.append(bar(5, 10, 45, 104, 109, 103.5, 108))
        bars.append(bar(5, 10, 50, 100, 101, 90, 91))
        zones = [zone for zone in prepare_symbol("TEST", bars, loose_config()).zones if zone["side"] == "demand"]

        self.assertEqual(zones[-1]["status"], "invalidated")
        self.assertIsNotNone(zones[-1]["invalidated_time"])

    def test_touch_without_a_rejection_candle_does_not_signal(self):
        bars = self._setup_bars(11, 0)
        inside = prepare_symbol("TEST", bars[:-2], loose_config())
        confirmed = prepare_symbol("TEST", bars, loose_config())

        self.assertEqual(inside.signals, {})
        self.assertTrue(any(item["reason"] == "REJECTION_CONFIRMATION_FAILED" for item in inside.rejections))
        self.assertEqual(len(confirmed.signals), 1)

    def test_same_slot_relative_volume_excludes_the_current_session(self):
        bars = []
        for day in range(1, 22):
            bars.append(bar(day, 10, 15, 100, 101, 99, 100, volume=100 if day < 21 else 180))
        setup = prepare_symbol("TEST", bars, loose_config(volume_filter_enabled=True, min_relative_volume=1.2, pivot_strength=1))
        # The helper is exercised by a direct median check through a tapped setup is unnecessary.
        from backend.ema_zones import _same_slot_relative_volume
        history = {}
        for item in bars[:-1]:
            local = item["time"].astimezone(INDIA)
            history[(local.date(), local.strftime("%H:%M"))] = item["volume"]
        ratio = _same_slot_relative_volume(history, bars[-1], 20)

        self.assertAlmostEqual(ratio, 1.8)
        self.assertIsNotNone(setup)

    def test_atr_uses_only_completed_true_ranges(self):
        bars = [bar(5, 10, minute, 100, 102, 99, 101) for minute in (0, 5, 10)]
        series = atr_series(bars, 2)

        self.assertIsNone(series[1])
        self.assertIsNotNone(series[2])

    def test_target_is_rejected_when_the_opposing_zone_is_too_close(self):
        target = resolve_target(100, 99, "long", {
            "target_mode": "opposing_zone",
            "opposing_price": 100.4,
            "reward_to_risk": 2,
            "max_target_r": 2,
            "min_reward_to_risk": 1,
        })

        self.assertIsNone(target)

    def test_target_does_not_tighten_the_stop_to_qualify(self):
        qualified = resolve_target(100, 98, "long", {
            "target_mode": "opposing_zone",
            "opposing_price": 104,
            "reward_to_risk": 3,
            "max_target_r": 2,
            "min_reward_to_risk": 1.5,
        })

        self.assertEqual(qualified, 104)

    def test_cutoff_blocks_the_execution_bar_at_and_after_1300(self):
        allowed = self._run_at(12, 59)
        at_cutoff = self._run_at(13, 0)
        after = self._run_at(13, 1)

        self.assertEqual(allowed["trade_count"], 1)
        self.assertEqual(at_cutoff["trade_count"], 0)
        self.assertGreaterEqual(at_cutoff["rejection_breakdown"].get("ENTRY_CUTOFF", 0), 1)
        self.assertEqual(after["trade_count"], 0)
        self.assertGreaterEqual(after["rejection_breakdown"].get("ENTRY_CUTOFF", 0), 1)

    def test_stop_is_filled_before_the_target_on_a_collision_bar(self):
        result = self._run_at(11, 0, follow=[(100, 140, 90, 110)])

        self.assertEqual(result["trades"][0]["exit_reason"], "STOP_LOSS")

    def test_target_fill_and_costs_and_square_off(self):
        costs = {
            "brokerage_per_order": 1,
            "stt_sell_percent": 0.025,
            "exchange_turnover_percent": 0.003,
            "gst_percent": 18,
            "stamp_buy_percent": 0.003,
            "sebi_per_crore": 10,
            "spread_bps": 0,
            "slippage_bps": 0,
        }
        targeted = self._run_at(11, 0, follow=[(109, 140, 108, 130)], costs=costs)
        flattened = self._run_at(11, 0, follow=[(109, 110, 108, 109, 15, 15)], costs=costs)

        self.assertEqual(targeted["trades"][0]["exit_reason"], "TARGET")
        self.assertTrue(targeted["costs_configured"])
        self.assertGreater(targeted["total_costs"], 0)
        self.assertAlmostEqual(targeted["net_pnl"], targeted["gross_pnl"] - targeted["total_costs"], places=2)
        self.assertEqual(flattened["trades"][0]["exit_reason"], "END_OF_DAY")
        self.assertIn("relative_volume", targeted["trades"][0])

    def test_position_size_reserves_estimated_costs(self):
        model = {
            "brokerage_per_order": 20,
            "stt_sell_percent": 0.025,
            "exchange_turnover_percent": 0.003,
            "gst_percent": 18,
            "stamp_buy_percent": 0.003,
            "sebi_per_crore": 10,
            "spread_bps": 1,
            "slippage_bps": 1,
        }
        sized = _quantity_with_costs(250, 100, 1, 1000, model)

        self.assertLess(sized, 250)
        self.assertGreater(sized, 0)

    def test_one_visit_cannot_emit_a_second_entry(self):
        bars = self._setup_bars(11, 0)
        zone_id = next(signal["zone_id"] for signal in prepare_symbol("TEST", bars, loose_config()).signals.values())
        retap = bars[-1]["time"] + timedelta(minutes=5)
        bars.append(bar(retap.day, retap.hour, retap.minute, 100, 110, 99, 109))
        again = prepare_symbol("TEST", bars, loose_config())
        self.assertEqual(sum(signal["zone_id"] == zone_id for signal in again.signals.values()), 1)

    def test_risk_limit_rejects_when_the_book_is_full(self):
        candles = {"AAA": self._setup_candles(11, 0), "BBB": self._setup_candles(11, 0)}
        result = run_event_backtest(candles, "ema_supply_demand", {
            "strategy": loose_config(),
            "common": {"max_positions": 1, "max_entries_per_symbol_per_day": 1, "intraday": True},
        })

        self.assertLessEqual(result["trade_count"], 1)
        self.assertGreaterEqual(result["rejection_breakdown"].get("POSITION_RISK_LIMIT", 0), 1)

    def test_momentum_failure_waits_for_a_failed_trend(self):
        quiet = [(109, 110, 108.5, 109.2, 11, 5 + step * 5) for step in range(6)]
        stalled = self._run_at(11, 0, follow=quiet, strategy_overrides={
            "momentum_failure_enabled": True,
            "momentum_failure_bars": 4,
            "momentum_progress_threshold": 0.9,
            "max_holding_bars": 30,
            "entry_mode": "touch_trend_rejection",
        })
        self.assertNotEqual(stalled["trades"][0]["exit_reason"], "MOMENTUM_FAILURE")

    def test_time_exit_still_uses_the_holding_limit(self):
        quiet = [(109, 110, 108.5, 109.2, 11, 5 + step * 5) for step in range(6)]
        result = self._run_at(11, 0, follow=quiet, strategy_overrides={"max_holding_bars": 2, "momentum_failure_enabled": False})

        self.assertEqual(result["trades"][0]["exit_reason"], "TIME_EXIT")

    def test_execution_window_helper_is_strict_about_one_oclock(self):
        from datetime import time
        self.assertIsNone(execution_time_block(time(12, 59), time(9, 30), time(13, 0), True))
        self.assertEqual(execution_time_block(time(13, 0), time(9, 30), time(13, 0), True), "ENTRY_CUTOFF")
        self.assertEqual(execution_time_block(time(13, 1), time(9, 30), time(13, 0), True), "ENTRY_CUTOFF")

    def _setup_bars(self, entry_hour, entry_minute):
        entry = datetime(2026, 1, 5, entry_hour, entry_minute, tzinfo=INDIA)

        def at(offset, open_price, high, low, close):
            stamp = entry + timedelta(minutes=offset)
            return bar(stamp.day, stamp.hour, stamp.minute, open_price, high, low, close)

        bars = []
        price = 100.0
        for index in range(8):
            bars.append(at(-60 + index * 5, price, price + 0.5, price - 0.2, price + 0.4))
            price += 0.6
        upper = price - 0.4
        bars.append(at(-15, price - 1, price, price - 6, upper))
        bars.append(at(-10, upper - 0.2, upper + 0.3, upper - 0.6, upper - 0.1))
        bars.append(at(-5, upper - 0.2, upper + 3, upper - 1, upper + 2.4))
        bars.append(at(0, upper + 2.4, upper + 3, upper + 2, upper + 2.5))
        return bars

    def _setup_candles(self, entry_hour, entry_minute, follow=None):
        rows = []
        for item in self._setup_bars(entry_hour, entry_minute):
            rows.append([item["time"].isoformat(), item["open"], item["high"], item["low"], item["close"], item["volume"]])
        if follow:
            last = datetime.fromisoformat(rows[-1][0])
            for offset, values in enumerate(follow, start=1):
                if len(values) == 6:
                    open_price, high, low, close, hour, minute = values
                    stamp = last.replace(hour=hour, minute=minute)
                else:
                    open_price, high, low, close = values
                    stamp = last + timedelta(minutes=5 * offset)
                rows.append([stamp.isoformat(), open_price, high, low, close, 100000])
        return rows

    def _run_at(self, entry_hour, entry_minute, follow=None, costs=None, strategy_overrides=None):
        strategy = loose_config(**(strategy_overrides or {}))
        config = {"strategy": strategy, "common": {"intraday": True, "max_holding_bars": 40, "max_entries_per_symbol_per_day": 3}}
        if costs:
            config["costs"] = costs
        return run_event_backtest({"TEST": self._setup_candles(entry_hour, entry_minute, follow)}, "ema_supply_demand", config)


if __name__ == "__main__":
    unittest.main()
