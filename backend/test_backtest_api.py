import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

_test_database = tempfile.TemporaryDirectory()
os.environ["CROSSLINE_DB"] = os.path.join(_test_database.name, "backtest-api-test.sqlite3")

from backend.main import BacktestRequest, fetch_candles
from pydantic import ValidationError


class FakeResponse:
    def __init__(self, candles):
        self.candles = candles

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": {"candles": self.candles}}


class BacktestApiTests(unittest.TestCase):
    def test_long_candle_range_is_split_into_non_overlapping_chunks(self):
        instrument = {"instrument_key": "NSE_EQ|TEST"}
        with patch("backend.main.requests.get", side_effect=[FakeResponse([["first"]]), FakeResponse([["second"]])]) as get:
            candles = fetch_candles(instrument, "test-token", date(2024, 4, 1), date(2024, 1, 1))

        self.assertEqual(get.call_count, 2)
        self.assertIn("/2024-03-25/2024-01-01", get.call_args_list[0].args[0])
        self.assertIn("/2024-04-01/2024-03-26", get.call_args_list[1].args[0])
        self.assertEqual(candles, [["first"], ["second"]])

    def test_five_minute_history_uses_short_provider_windows(self):
        instrument = {"instrument_key": "NSE_EQ|TEST"}
        responses = [FakeResponse([["first"]]), FakeResponse([["second"]])]
        with patch("backend.main.requests.get", side_effect=responses) as get:
            candles = fetch_candles(instrument, "test-token", date(2025, 1, 31), date(2025, 1, 1), "5m")

        self.assertEqual(get.call_count, 2)
        self.assertIn("/minutes/5/2025-01-28/2025-01-01", get.call_args_list[0].args[0])
        self.assertIn("/minutes/5/2025-01-31/2025-01-29", get.call_args_list[1].args[0])
        self.assertEqual(candles, [["first"], ["second"]])

    def test_accepts_custom_strategy_and_one_year_history(self):
        config = BacktestRequest(
            period_days=365,
            fast_period=8,
            slow_period=50,
            stop_lookback=10,
            risk_percent=1.5,
            reward_to_risk=4,
        )

        self.assertEqual(config.period_days, 365)
        self.assertEqual(config.fast_period, 8)

    def test_rejects_unsafe_or_invalid_strategy_values(self):
        with self.assertRaises(ValidationError):
            BacktestRequest(fast_period=30, slow_period=30)
        with self.assertRaises(ValidationError):
            BacktestRequest(risk_percent=2.1)
        with self.assertRaises(ValidationError):
            BacktestRequest(period_days=366)


if __name__ == "__main__":
    unittest.main()