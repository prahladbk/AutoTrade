import unittest
from datetime import datetime, timedelta, timezone

from backend.simulation_engine import run_event_backtest
from backend.strategy_registry import STRATEGIES


def candles_for_cross(tail):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    closes = [100.0] * 30 + [110.0]
    rows = []
    for index, close in enumerate(closes):
        stamp = (start + timedelta(hours=4 * index)).isoformat()
        rows.append([stamp, close, close + 1, close - 1, close, 1000, 0])
    for index, (open_price, high, low, close) in enumerate(tail, start=len(rows)):
        stamp = (start + timedelta(hours=4 * index)).isoformat()
        rows.append([stamp, open_price, high, low, close, 1000, 0])
    return rows


class SimulationEngineTests(unittest.TestCase):
    def test_signal_executes_next_bar_and_marks_trade_simulated(self):
        result = run_event_backtest({"TEST": candles_for_cross([(111, 112, 110, 111), (111, 113, 110, 112)])}, "sma_crossover", {})

        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["entry_price"], 111)
        self.assertEqual(result["trades"][0]["simulation_status"], "SIMULATED")
        self.assertLessEqual(result["trades"][0]["planned_risk"], 250)

    def test_stop_wins_when_stop_and_target_touch_in_the_same_bar(self):
        candles = candles_for_cross([(111, 112, 110, 111), (111, 150, 90, 120)])

        result = run_event_backtest({"TEST": candles}, "sma_crossover", {})

        self.assertEqual(result["trades"][0]["exit_reason"], "STOP_LOSS")

    def test_opposing_crossover_closes_at_next_bar_open(self):
        tail = [(111, 112, 110, 111)] + [(100.2, 100.6, 99.7, 100.2)] * 6 + [(100.1, 100.5, 99.9, 100.2)]
        candles = candles_for_cross(tail)
        config = {
            "strategy": {"direction": "long_short", "reward_to_risk": 10},
            "common": {"direction": "long_short", "intraday": False, "max_holding_bars": 100},
        }

        result = run_event_backtest({"TEST": candles}, "sma_crossover", config)

        self.assertEqual(result["trades"][0]["exit_reason"], "OPPOSITE_SIGNAL")
        self.assertEqual(result["trades"][0]["exit_price"], 100.1)
        self.assertEqual(result["trades"][1]["direction"], "short")

    def test_unconfigured_charges_do_not_claim_net_profit(self):
        candles = candles_for_cross([(111, 112, 110, 111), (111, 150, 110, 145)])

        result = run_event_backtest({"TEST": candles}, "sma_crossover", {})

        self.assertFalse(result["costs_configured"])
        self.assertIsNone(result["net_pnl"])
        self.assertIsNotNone(result["gross_pnl"])

    def test_configured_cost_components_reduce_net_result(self):
        candles = candles_for_cross([(111, 112, 110, 111), (111, 150, 110, 145)])
        costs = {
            "brokerage_per_order": 1,
            "stt_sell_percent": 0.01,
            "exchange_turnover_percent": 0.001,
            "gst_percent": 18,
            "stamp_buy_percent": 0.001,
            "sebi_per_crore": 1,
            "spread_bps": 0,
            "slippage_bps": 0,
        }

        result = run_event_backtest({"TEST": candles}, "sma_crossover", {"costs": costs})

        self.assertTrue(result["costs_configured"])
        self.assertGreater(result["total_costs"], 0)
        self.assertAlmostEqual(result["net_pnl"], result["gross_pnl"] - result["total_costs"], places=2)

    def test_all_registered_strategies_share_the_same_result_schema(self):
        candles = candles_for_cross([(111, 112, 110, 111), (111, 112, 110, 111)])
        expected = {"starting_capital", "gross_pnl", "net_pnl", "trades", "stock_performance", "costs_configured"}

        for strategy_id in STRATEGIES:
            with self.subTest(strategy=strategy_id):
                result = run_event_backtest({"TEST": candles}, strategy_id, {})
                self.assertTrue(expected.issubset(result.keys()))


if __name__ == "__main__":
    unittest.main()