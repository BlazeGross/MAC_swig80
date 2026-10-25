import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import swig80_backtest as app


class ConfigTests(unittest.TestCase):
    def test_defaults_use_only_swig80_and_its_costs(self):
        cfg, optimize = app.parse_args([])
        self.assertEqual(cfg.name, "sWIG80")
        self.assertEqual(cfg.commission, 0.0029)
        self.assertEqual(cfg.min_commission, 5)
        self.assertEqual(cfg.spread, 0.004)
        self.assertEqual(cfg.out_dir.name, "sWIG80")
        self.assertIsNone(optimize)

    def test_overrides_and_output_directory(self):
        cfg, optimize = app.parse_args([
            "--out", "reports", "--commission", "0.0039",
            "--min-commission", "7", "--spread", "0.006", "--optimize",
        ])
        self.assertEqual(cfg.out_dir, Path("reports") / "sWIG80")
        self.assertEqual((cfg.commission, cfg.min_commission, cfg.spread), (0.0039, 7, 0.006))
        self.assertEqual(optimize, 60)

    def test_invalid_arguments_fail_explicitly(self):
        cases = [
            ["--tickers", "OTHER"],
            ["--years", "0"], ["--fast", "0"], ["--slow", "50"],
            ["--delay", "-1"], ["--cooldown", "-1"], ["--optimize", "-1"],
            ["--capital", "0"], ["--capital", "nan"], ["--commission", "inf"],
            ["--commission", "-0.1"], ["--commission", "1"], ["--spread", "2"],
            ["--commission", "0.9", "--spread", "0.4"],
            ["--min-commission", "-1"], ["--min-commission", "10000"],
            ["--min-commission", "9999", "--spread", "0.004"],
            ["--cash-rate", "-1"],
        ]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    app.parse_args(args)
                self.assertEqual(error.exception.code, 2)


class DataTests(unittest.TestCase):
    def fetch(self, payload):
        session = MagicMock()
        session.get.return_value.json.return_value = payload
        with patch.object(app.creq, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = app.fetch_gpw_index()
        return result, session

    def test_fetch_uses_official_isin_and_normalizes_dates(self):
        def timestamp(day):
            return int(pd.Timestamp(day, tz="Europe/Warsaw").timestamp())

        result, session = self.fetch([{"data": [
            {"t": timestamp("2024-07-02"), "c": "103"},
            {"t": timestamp("2024-07-01"), "c": "100"},
            {"t": timestamp("2024-07-02"), "c": "104"},
        ]}])
        self.assertEqual(result.tolist(), [100, 104])
        expected = pd.DatetimeIndex(["2024-07-01", "2024-07-02"], name="t").as_unit("s")
        pd.testing.assert_index_equal(result.index, expected)
        request = json.loads(session.get.call_args.kwargs["params"]["req"])
        self.assertEqual(request, [{"isin": "PL9999999979", "mode": "ARCH"}])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.return_value.raise_for_status.call_count, 2)

    def test_bad_payload_is_not_silently_accepted(self):
        for payload in ({}, [], [{}], [{"data": []}], [{"data": [{"x": 1}]}],
                        [{"data": [{"t": 1700000000, "c": -1}]}],
                        [{"data": [{"t": 1700000000, "c": "NaN"}]}]):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                self.fetch(payload)

    def test_network_errors_propagate(self):
        with patch.object(app.creq, "Session") as factory:
            factory.return_value.__enter__.return_value.get.side_effect = RuntimeError("network")
            with self.assertRaisesRegex(RuntimeError, "network"):
                app.fetch_gpw_index()

    def test_insufficient_history_fails_explicitly(self):
        close = pd.Series([100.0], index=pd.DatetimeIndex([pd.Timestamp.today().normalize()]))
        with patch.object(app, "fetch_gpw_index", return_value=close):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                app.download_data(app.Config())


class StrategyTests(unittest.TestCase):
    def test_crossover_delay_and_costs(self):
        index = pd.bdate_range("2024-01-01", periods=6)
        df = pd.DataFrame({
            "Close": [100, 100, 110, 121, 121, 121],
            "MA_fast": [0, 2, 2, 0, 0, 0],
            "MA_slow": [1] * 6,
        }, index=index)
        cfg = app.Config(commission=0.01, min_commission=0, spread=0, cooldown=0)
        result, trades = app.run_backtest(df, cfg)
        self.assertEqual(result["position"].tolist(), [0, 1, 1, 0, 0, 0])
        self.assertAlmostEqual(result["equity"].iloc[-1], 10000 * 0.99 * 1.21 * 0.99)
        self.assertAlmostEqual(result["bh_equity"].iloc[-1], 10000 * 0.99 * 1.21)
        self.assertEqual(len(trades), 1)
        self.assertFalse(trades.iloc[0]["open"])
        self.assertAlmostEqual(trades.iloc[0]["return_pct"], (1.21 * 0.99**2 - 1) * 100)
        cfg.delay = 1
        delayed, _ = app.run_backtest(df, cfg)
        self.assertEqual(delayed["position"].tolist(), [0, 0, 1, 1, 0, 0])

    def test_cooldown_preserves_pending_target(self):
        index = pd.RangeIndex(5)
        position, blocked = app.apply_cooldown(np.array([0, 1, 0, 0, 0]), 2, index)
        self.assertEqual(position.tolist(), [0, 1, 1, 1, 0])
        self.assertEqual(blocked.tolist(), [False, False, True, True, False])

    def test_minimum_commission_and_spread(self):
        cfg = app.Config()
        self.assertAlmostEqual(app.transaction_cost_frac(1000, cfg), 5 / 1000 + 0.004 / 2)
        self.assertAlmostEqual(app.transaction_cost_frac(10000, cfg), 0.0029 + 0.004 / 2)


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, close, out, *args):
        with (
            patch.object(app, "fetch_gpw_index", return_value=close),
            patch("sys.argv", ["swig80_backtest.py", "--out", str(out), *args]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            app.main()

    def test_full_pipeline_writes_only_swig80_reports(self):
        index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=1600)
        x = np.arange(len(index))
        close = pd.Series(1000 * np.exp(x * 0.0002 + 0.15 * np.sin(x / 30)), index=index)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.run_pipeline(close, out, "--years", "4", "--fast", "5", "--slow", "15", "--optimize", "2")
            self.assertEqual([p.name for p in out.iterdir()], ["sWIG80"])
            reports = out / "sWIG80"
            self.assertEqual(len(list(reports.glob("*.png"))), 7)
            self.assertEqual({p.name for p in reports.glob("*.csv")}, {
                "trades.csv", "daily_backtest.csv", "summary.csv", "cooldown_optimization.csv",
            })
            summary = pd.read_csv(reports / "summary.csv", index_col=0)
            self.assertEqual(summary.columns.tolist(), ["sWIG80 MAC", "sWIG80 B&H"])
            optimization = pd.read_csv(reports / "cooldown_optimization.csv")
            self.assertEqual(optimization["cooldown"].tolist(), [0, 1, 2])
            for image in reports.glob("*.png"):
                self.assertGreater(image.stat().st_size, 1000)

    def test_pipeline_without_trades_or_closed_trades(self):
        index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=400)
        scenarios = {
            "cash_only": np.linspace(200, 100, len(index)),
            "open_only": np.concatenate([np.linspace(200, 100, 300), np.linspace(100, 200, 100)]),
        }
        for name, values in scenarios.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                self.run_pipeline(pd.Series(values, index=index), out,
                                  "--years", "1", "--fast", "5", "--slow", "15")
                trades = pd.read_csv(out / "sWIG80" / "trades.csv")
                self.assertEqual(len(trades), 0 if name == "cash_only" else 1)
                self.assertTrue(trades.empty or trades["open"].all())
                self.assertEqual(len(list((out / "sWIG80").glob("*.png"))), 6)


if __name__ == "__main__":
    unittest.main()
