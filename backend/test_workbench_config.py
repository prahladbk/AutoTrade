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
    delete_strategy_preset,
    get_backtest,
    get_strategy_presets,
    normalize_workbench_config,
    save_strategy_preset,
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


if __name__ == "__main__":
    unittest.main()