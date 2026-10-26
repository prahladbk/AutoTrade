import os
import tempfile
import unittest


_test_database = tempfile.TemporaryDirectory()
os.environ["CROSSLINE_DB"] = os.path.join(_test_database.name, "workbench-config-test.sqlite3")

from fastapi import HTTPException
from fastapi import BackgroundTasks

from backend.main import (
    BacktestLaunchRequest,
    StrategyPresetRequest,
    cancel_backtest,
    create_backtest,
    db_connect,
    delete_backtest,
    delete_backtest_history,
    delete_strategy_preset,
    get_backtest,
    get_strategy_presets,
    list_backtests,
    normalize_workbench_config,
    save_strategy_preset,
    select_paper_symbols,
    update_strategy_preset,
)
from backend.strategy_registry import STRATEGIES


class WorkbenchConfigTests(unittest.TestCase):
    def test_defaults_are_versioned_and_use_nifty100(self):
        config = normalize_workbench_config("sma_crossover", {})

        self.assertEqual(config["version"], 1)
        self.assertEqual(config["common"]["starting_capital"], 100_000)
        self.assertEqual(config["common"]["universe"], "nifty100_current")
        self.assertEqual(config["common"]["timeframe"], "5m")

    def test_rejects_five_minute_history_beyond_provider_limit(self):
        with self.assertRaises(HTTPException) as context:
            normalize_workbench_config("sma_crossover", {"common": {"period_days": 29}})

        self.assertEqual(context.exception.status_code, 422)

    def test_rejects_invalid_date_order_and_risk(self):
        with self.assertRaises(HTTPException):
            normalize_workbench_config("sma_crossover", {"common": {"start_date": "2025-02-02", "end_date": "2025-02-01"}})
        with self.assertRaises(HTTPException):
            normalize_workbench_config("sma_crossover", {"common": {"risk_percent": 2.1}})

    def test_selected_stock_universe_requires_symbols(self):
        with self.assertRaises(HTTPException):
            normalize_workbench_config("sma_crossover", {"common": {"universe": "selected_symbols"}})
        config = normalize_workbench_config("sma_crossover", {"common": {"universe": "selected_symbols", "symbols": ["TCS"]}})
        self.assertEqual(config["common"]["symbols"], ["TCS"])

    def test_three_minute_ema_history_and_paper_symbol_cap(self):
        config = normalize_workbench_config("ema_supply_demand", {
            "common": {"timeframe": "3m", "period_days": 10, "universe": "selected_symbols", "symbols": ["tcs", "infy"]},
        })
        self.assertEqual(config["common"]["timeframe"], "3m")
        self.assertEqual(select_paper_symbols(config), ["TCS", "INFY"])
        wide = normalize_workbench_config("ema_supply_demand", {
            "common": {"universe": "selected_symbols", "symbols": [f"S{index}" for index in range(12)]},
        })
        self.assertEqual(len(select_paper_symbols(wide)), 10)
        with self.assertRaises(HTTPException):
            normalize_workbench_config("ema_supply_demand", {"common": {"timeframe": "3m", "period_days": 29}})
        with self.assertRaises(HTTPException) as context:
            normalize_workbench_config("ema_supply_demand", {"strategy": {"fast_period": 30, "slow_period": 7}})
        self.assertIn("EMA", context.exception.detail)
        with self.assertRaises(HTTPException):
            select_paper_symbols({"common": {"universe": "nifty100_current", "symbols": ["TCS"]}})

    def test_every_registered_strategy_normalizes_with_defaults(self):
        for key in STRATEGIES:
            with self.subTest(strategy=key):
                config = normalize_workbench_config(key, {})
                self.assertEqual(config["strategy_id"], key)

    def test_preset_create_update_delete_and_run_queue_persist(self):
        preset = save_strategy_preset(StrategyPresetRequest(name="first", strategy_id="sma_crossover", config={}))
        updated = update_strategy_preset(preset["id"], StrategyPresetRequest(name="renamed", strategy_id="sma_crossover", config={}))
        self.assertEqual(updated["name"], "renamed")
        self.assertEqual(get_strategy_presets()[0]["name"], "renamed")
        delete_strategy_preset(preset["id"])
        self.assertEqual(get_strategy_presets(), [])

        tasks = BackgroundTasks()
        run = create_backtest(BacktestLaunchRequest(strategy_id="sma_crossover", config={}), tasks)
        self.assertEqual(run["status"], "RUNNING")
        self.assertEqual(len(tasks.tasks), 1)
        self.assertEqual(get_backtest(run["id"])["status"], "RUNNING")
        self.assertEqual(cancel_backtest(run["id"])["status"], "CANCELLING")

    def test_finished_runs_can_be_deleted_while_a_live_run_stays(self):
        with db_connect() as connection:
            connection.execute(
                "INSERT INTO backtest_runs (id, strategy_id, status, config_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("done-1", "sma_crossover", "COMPLETED", "{}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
            )
            connection.execute(
                "INSERT INTO backtest_runs (id, strategy_id, status, config_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("done-2", "sma_crossover", "FAILED", "{}", "2026-01-03T00:00:00+00:00", "2026-01-03T00:01:00+00:00"),
            )
            connection.execute(
                "INSERT INTO backtest_runs (id, strategy_id, status, config_json, created_at) VALUES (?, ?, ?, ?, ?)",
                ("live-kept", "sma_crossover", "RUNNING", "{}", "2026-01-04T00:00:00+00:00"),
            )

        self.assertTrue(delete_backtest("done-1")["deleted"])
        with self.assertRaises(HTTPException) as missing:
            get_backtest("done-1")
        self.assertEqual(missing.exception.status_code, 404)
        with self.assertRaises(HTTPException) as running:
            delete_backtest("live-kept")
        self.assertEqual(running.exception.status_code, 409)

        removed = delete_backtest_history()
        self.assertGreaterEqual(removed["deleted"], 1)
        remaining = {row["id"]: row["status"] for row in list_backtests()}
        self.assertNotIn("done-2", remaining)
        self.assertEqual(remaining["live-kept"], "RUNNING")


if __name__ == "__main__":
    unittest.main()