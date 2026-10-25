import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.strategy_registry import STRATEGIES, strategy_signal


INDIA = ZoneInfo("Asia/Kolkata")


def bars_from_closes(closes):
    start = datetime(2025, 1, 1, 9, 15, tzinfo=INDIA)
    return [
        {"time": start + timedelta(minutes=5 * index), "open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000}
        for index, close in enumerate(closes)
    ]


class StrategyRegistryTests(unittest.TestCase):
    def test_registry_has_the_builtin_strategy_keys(self):
        self.assertEqual(len(STRATEGIES), 8)
        self.assertIn("ema_supply_demand", STRATEGIES)
        self.assertEqual(STRATEGIES["sma_crossover"].defaults["fast_period"], 6)

    def test_sma_strategy_signals_only_on_the_cross_bar(self):
        bars = bars_from_closes([100.0] * 30 + [110.0, 111.0, 112.0])

        signal = strategy_signal("sma_crossover", bars, 30, STRATEGIES["sma_crossover"].defaults)
        repeated = strategy_signal("sma_crossover", bars, 31, STRATEGIES["sma_crossover"].defaults)

        self.assertEqual(signal["direction"], "long")
        self.assertIn("crossed above", signal["reason"])
        self.assertIsNone(repeated)

    def test_breakout_level_excludes_the_current_signal_bar(self):
        closes = [100.0] * 20 + [102.0, 103.0]
        bars = bars_from_closes(closes)
        bars[20]["high"] = 101.0
        bars[20]["close"] = 102.0
        bars[20]["volume"] = 2000

        signal = strategy_signal("donchian_breakout", bars, 20, STRATEGIES["donchian_breakout"].defaults)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "long")

    def test_common_volume_filter_blocks_low_volume_signal(self):
        bars = bars_from_closes([100.0] * 30 + [110.0, 111.0, 112.0])
        bars[30]["volume"] = 10
        config = {**STRATEGIES["sma_crossover"].defaults, "volume_filter_enabled": True, "min_relative_volume": 1.5, "volume_lookback": 20}

        self.assertIsNone(strategy_signal("sma_crossover", bars, 30, config))


if __name__ == "__main__":
    unittest.main()