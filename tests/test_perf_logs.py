import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import build_report


ROOT = Path(__file__).resolve().parents[1]


class PerfLogsTests(unittest.TestCase):
    def report(self, configured, raw_path):
        row = dict.fromkeys((
            "SampleCount", "CpuAverage", "GpuAverage", "GpuMemoryAverage",
            "FpsAverage", "FpsMin", "FpsMax", "Fps1PctLow", "FpsPoint1PctLow",
            "FrameTimeAvg", "FrameTimeStdDev", "FrameTimeMax", "StutterScoreFT",
            "StutterScoreDT", "BlameCpuPct", "BlameGpuPct", "AnimationErrorRMS",
        ), 1)
        row.update(GameDisplay="Test", EntryDate="2026-11-10", FileRawDataPath=raw_path)
        return runpy.run_path(str(ROOT / "perf_report.py"), init_globals={
            "SRC": pd.DataFrame([row]), "SRC_LABEL": "Test", "PERF_LOGS_DIR": configured,
        })

    def test_primary_checked_alternates_only_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            alternate = Path(tmp) / "alternate"
            primary.mkdir()
            alternate.mkdir()
            (alternate / "PresentMon_test.csv").touch()
            paths = [str(primary), str(alternate), r"\\other-pc\logs"]
            ns = self.report(paths, r"C:\recorded\PresentMon_test.csv")
            self.assertEqual(ns["DRILL_LOGS"]["dir"], str(primary))
            self.assertEqual(ns["DRILL_LOGS"]["alternates"], paths[1:])
            self.assertTrue(ns["DRILL_LOGS"]["checked"])
            self.assertEqual(ns["DRILL_LOGS"]["found"], 0)
            self.assertEqual(ns["data"]["LogOnDisk"].tolist(), [0])
            (primary / "PresentMon_test.csv").touch()
            ns = self.report(paths, r"C:\recorded\PresentMon_test.csv")
            self.assertEqual(ns["DRILL_LOGS"]["found"], 1)
            self.assertIn("const drillLogs =", ns["PERF_SCRIPT"])
            self.assertIn("other-pc", ns["PERF_SCRIPT"])

    def test_single_path_and_database_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "PresentMon_test.csv")
            Path(raw).touch()
            for configured in (tmp, None, []):
                with self.subTest(configured=configured):
                    info = self.report(configured, raw)["DRILL_LOGS"]
                    self.assertEqual(info["dir"], tmp)
                    self.assertEqual(info["alternates"], [])
                    self.assertEqual(info["found"], 1)
                    self.assertEqual(info["source"], "config" if configured else "database")

    def test_missing_primary_does_not_check_alternate(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "PresentMon_test.csv").touch()
            ns = self.report([os.path.join(tmp, "missing"), tmp], r"C:\logs\PresentMon_test.csv")
            self.assertFalse(ns["DRILL_LOGS"]["checked"])
            self.assertNotIn("LogOnDisk", ns["data"])

    def test_invalid_paths_fail_explicitly(self):
        for configured in (42, [""], [None], ["valid", 42]):
            with self.subTest(configured=configured), self.assertRaisesRegex(ValueError, "PERF_LOGS_DIR"):
                self.report(configured, r"C:\logs\test.csv")

    def test_cli_overrides_config_without_appending_to_it(self):
        with patch.object(build_report.cfg, "PERF_LOGS_DIR", ["configured", "other"]):
            for argv, expected in (
                ([], ["configured", "other"]),
                (["--perf-logs", "primary"], ["primary"]),
                (["--perf-logs", "primary", "--perf-logs", "alternate"], ["primary", "alternate"]),
            ):
                with self.subTest(argv=argv), patch.object(sys, "argv", ["build_report.py", *argv]):
                    self.assertEqual(build_report.parse_args().perf_logs, expected)


if __name__ == "__main__":
    unittest.main()
