import unittest
from datetime import datetime, timedelta, timezone

from backend.backtesting import build_setups, run_portfolio_backtest, size_position


def candles_from_closes(closes, tail=None):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index, close in enumerate(closes):
        stamp = (start + timedelta(hours=4 * index)).isoformat()
        rows.append([stamp, close, close + 1, close - 1, close, 1000, 0])
    if tail:
        for index, (open_price, high, low, close) in enumerate(tail, start=len(rows)):
            stamp = (start + timedelta(hours=4 * index)).isoformat()
            rows.append([stamp, open_price, high, low, close, 1000, 0])
    return rows


class BacktestingTests(unittest.TestCase):
    def test_position_size_caps_planned_loss_and_sets_three_r_target(self):
        setup = build_setups(candles_from_closes([100.0] * 30 + [110.0], [(111.0, 112.0, 110.0, 111.0)]))[0]
        quantity, planned_risk = size_position(100_000, setup["entry"], setup["stop"])

        self.assertGreater(quantity, 0)
        self.assertLessEqual(planned_risk, 2_000)
        self.assertEqual(setup["target"] - setup["entry"], 3 * (setup["entry"] - setup["stop"]))
        _, profit_capped_risk = size_position(150_000, setup["entry"], setup["stop"])
        self.assertLessEqual(profit_capped_risk, 2_000)

    def test_stop_is_taken_first_when_same_bar_touches_stop_and_target(self):
        candles = candles_from_closes(
            [100.0] * 30 + [110.0],
            [(111.0, 112.0, 110.0, 111.0), (111.0, 150.0, 90.0, 120.0)],
        )
        result = run_portfolio_backtest({"TEST": candles})

        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "STOP_LOSS")
        self.assertLessEqual(result["trades"][0]["planned_risk"], 2_000)

    def test_crossover_enters_on_the_following_bar(self):
        candles = candles_from_closes([100.0] * 30 + [110.0], [(111.0, 112.0, 110.0, 111.0)])

        setup = build_setups(candles)[0]

        self.assertEqual(setup["entry"], 111.0)
        self.assertGreater(setup["entry_time"], setup["signal_time"])


if __name__ == "__main__":
    unittest.main()