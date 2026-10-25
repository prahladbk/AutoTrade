import os
import tempfile
import unittest


_test_database = tempfile.TemporaryDirectory()
os.environ["CROSSLINE_DB"] = os.path.join(_test_database.name, "scanner-test.sqlite3")

from backend.main import analyze_candles


class AnalyzeCandlesTests(unittest.TestCase):
    def test_detects_sma6_crossing_above_sma30(self):
        closes = [100.0] * 24 + [99.0] * 6 + [110.0]
        candles = [[f"2026-01-{index + 1:02d}T09:15:00+05:30", 0, 0, 0, close] for index, close in enumerate(closes)]

        signal = analyze_candles("TEST", "Test Co", "NSE_EQ|TEST", candles)

        self.assertIsNotNone(signal)
        self.assertGreater(signal["sma6"], signal["sma30"])
        self.assertGreater(signal["close"], signal["sma30"])

    def test_ignores_flat_prices_without_a_crossover(self):
        candles = [[f"2026-01-{index + 1:02d}T09:15:00+05:30", 0, 0, 0, 100.0] for index in range(31)]

        self.assertIsNone(analyze_candles("TEST", "Test Co", "NSE_EQ|TEST", candles))

    def test_requires_at_least_31_completed_candles(self):
        candles = [[f"2026-01-{index + 1:02d}T09:15:00+05:30", 0, 0, 0, 100.0] for index in range(30)]

        self.assertIsNone(analyze_candles("TEST", "Test Co", "NSE_EQ|TEST", candles))


if __name__ == "__main__":
    unittest.main()