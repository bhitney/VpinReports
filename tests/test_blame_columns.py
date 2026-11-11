from pathlib import Path
import runpy
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]

BASE = dict.fromkeys((
    "SampleCount", "CpuAverage", "GpuAverage", "GpuMemoryAverage",
    "FpsAverage", "FpsMin", "FpsMax", "Fps1PctLow", "FpsPoint1PctLow",
    "FrameTimeAvg", "FrameTimeStdDev", "FrameTimeMax", "StutterScoreFT",
    "StutterScoreDT", "BlameCpuPct", "BlameGpuPct", "AnimationErrorRMS",
), 1)

GRADE = dict(
    StutterGrade="B", StutterGradeScore=85.0, StutterGradeCapped=0, StutterGradeVersion=2,
    PresentApplication="VPinballX_BGFX64.exe", PresentMonFrameCount=1000, GradeFrames=900,
    GradeDurationSec=120.0, GradeRefreshMs=7.25, DoubledFrames=10, TripleFrames=0,
    LongestMissRefreshes=2, MissedFramePct=1.0, MissesPerMinute=5.0, WeightedMissPct=1.0,
    ClusterPeakMisses=2, ClusterRatio=1.0, ClusterMultiplier=1.0, EffectiveMissPct=1.0,
    LongGapCount=0, LongGapMissCount=0,
)

BLAME = dict(
    BlameVersion=4, GpuBusyPctOfRefresh=32.0, StallCpuCount=40, StallGpuCount=0,
    StallModeSwitchCount=1, StallAppWaitCount=2, StallDisplayCount=139,
    CfgBlameMinStalls=8, CfgBlameGpuSaturation=0.9, GpuName="RTX 4080 SUPER", GpuDriverVersion="610.88",
)


def report(rows, globals_extra=None):
    frame = pd.DataFrame(rows)
    init = {"SRC": frame, "SRC_LABEL": "Test", "PERF_LOGS_DIR": None}
    init.update(globals_extra or {})
    return runpy.run_path(str(ROOT / "perf_report.py"), init_globals=init)


def row(**extra):
    r = dict(BASE, GameDisplay="Test", EntryDate="2026-11-10", FileRawDataPath="C:\\x\\PresentMon_t.csv")
    r.update(GRADE)
    r.update(extra)
    return r


class BlameColumnTests(unittest.TestCase):
    def test_new_columns_reach_the_records(self):
        ns = report([row(**BLAME)])
        rec = ns["all_records"][0]
        self.assertEqual(rec["StallCpuCount"], 40)
        self.assertEqual(rec["GpuName"], "RTX 4080 SUPER")
        self.assertEqual(rec["BlameLegacy"], 0)
        self.assertAlmostEqual(rec["StallCpuPerMin"], 20.0)
        self.assertEqual(rec["StallTotal"], 182)
        self.assertEqual(ns["DRILL_CFGS"][0]["BlameMinStalls"], 8)
        self.assertEqual(ns["DRILL_CFGS"][0]["BlameGpuSaturation"], 0.9)
        self.assertIn("gpuLoadVsCpuStalls", [c["id"] for c in ns["chart_defs"]])
        self.assertIn('id="gpuNameFilter"', ns["PERF_BODY"])

    def test_legacy_and_missing_columns_still_build(self):
        # One migrated row, one legacy row (BlameVersion NULL) -> the legacy row is labelled, not mixed in.
        ns = report([row(**BLAME), row(**dict(BLAME, BlameVersion=None, StallCpuCount=None))])
        flags = sorted(r["BlameLegacy"] for r in ns["all_records"])
        self.assertEqual(flags, [0, 1])
        # An unmigrated database has none of the new columns at all.
        ns = report([row()])
        self.assertNotIn("gpuLoadVsCpuStalls", [c["id"] for c in ns["chart_defs"]])
        self.assertNotIn("gpuNameFilter", ns["PERF_BODY"])

    def test_gpu_load_bands_are_configurable(self):
        ns = report([row(**BLAME)], {"GPU_LOAD_BANDS": (60, 90)})
        self.assertEqual(ns["GPU_LOAD_BANDS"], (60, 90))
        self.assertIn("const drillGpuBands = [60, 90];", ns["PERF_SCRIPT"])


if __name__ == "__main__":
    unittest.main()
