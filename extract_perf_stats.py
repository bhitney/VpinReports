"""
Extracts pinball table perf-capture data (CustomPerfStats + Games) out of the
PinUP Popper SQLite database into a CSV, ready for generate_scatter_report.py.

Usage:
    python extract_perf_stats.py [path\\to\\database.db]

If no path is given, the single *.db file in the current directory is used
(an error is raised if there are zero or more than one, since it would be
ambiguous which one to read).

Output is written as CustomPerfStats_<timestamp>.csv, timestamped with the
current date/time (YYYYMMDDHHMM) so it slots into the same naming convention
generate_scatter_report.py already looks for, and keeps a dated history of
extracts alongside prior ones.

Edit QUERY below to add/remove/rename columns as the source schema evolves.
"""
import glob
import sqlite3
import sys
from datetime import datetime

import pandas as pd

# ---------------------------------------------------------------------------
# The extraction query. Tweak this (e.g. add columns, change the join, filter
# rows) as needed -- everything downstream just reads whatever columns come
# back from here.
# ---------------------------------------------------------------------------
QUERY = """
SELECT g.GameId, g.GameDisplay,
CAST(COALESCE(NULLIF(TRIM(g.GameRating), ''), '0') AS INTEGER) AS GameRating,
g.EMUID, e.EmuDisplay, IsVR, EntryDate, SampleCount, PresentMonFrameCount, CpuAverage, CpuMin, CpuMax, GpuAverage, GpuMin, GpuMax, GpuMemoryAverage,
GpuMemoryMin, GpuMemoryMax, FpsAverage, FpsMin, FpsMax, Fps1PctLow, FpsPoint1PctLow, FrameTimeAvg, FrameTimeMin, FrameTimeMax, FrameTime999th, FrameTime995th, FrameTime99th,
FrameTime95th, FrameTimeStdDev, FrameTimeDeltaAvg, FrameTimeDeltaMax, FrameTimeDelta999th, FrameTimeDelta995th, FrameTimeDelta99th,
FrameTimeDelta95th, DisplayedTimeAvg,
DisplayedTimeStdDev, DisplayedTimeDeltaAvg, DisplayedTimeDeltaMax, DisplayedTimeDelta999th, DisplayedTimeDelta995th, DisplayedTimeDelta99th, DisplayedTimeDelta95th,
AnimationErrorMAD, AnimationErrorRMS, StutterScoreFT, StutterScoreDT, BlameCpuPct, BlameGpuPct, GpuBusyAvg, GpuBusyMin, GpuBusyMax, GpuBusy999th, GpuBusy995th, GpuBusy99th,
GpuBusy95th, GpuWaitAvg, GpuWaitMin, GpuWaitMax, GpuWait999th, GpuWait995th, GpuWait99th, GpuWait95th, CpuBusyAvg, CpuBusyMin, CpuBusyMax, CpuBusy999th, CpuBusy995th,
CpuBusy99th, CpuBusy95th, CpuWaitAvg, CpuWaitMin, CpuWaitMax, CpuWait999th, CpuWait995th, CpuWait99th, CpuWait95th,
PresentApplication, StutterGrade, StutterGradeScore, StutterGradeCapped, StutterGradeVersion, GradeFrames, GradeDurationSec, GradeRefreshMs,
DoubledFrames, TripleFrames, LongestMissRefreshes, MissedFramePct, MissesPerMinute, WeightedMissPct, ClusterPeakMisses, ClusterRatio, ClusterMultiplier,
EffectiveMissPct, LongGapCount, LongGapMissCount,
GradeMissingFrames, GradeRefreshSource, SwapChainAddress, FileRawDataPath,
CfgDoubledRefreshes, CfgTripleRefreshes, CfgBandA, CfgBandB, CfgBandC, CfgBandD, CfgZeroScorePct, CfgTripleWeight,
CfgTripleCapGrade, CfgTripleCapMinCount, CfgClusterWindowSec, CfgClusterWeight, CfgClusterMaxExcess, CfgClusterMinEvents,
CfgClusterMinPeak, CfgMinFrames, CfgTrimEndSeconds
FROM CustomPerfStats cps
JOIN Games g
ON g.GameID = cps.GameId
JOIN Emulators e
ON g.EMUID = e.EMUID
"""

# Newer CustomPerfStats columns; selected only when the database has them so
# older databases keep working.
OPTIONAL_COLUMNS = ["CfgTrimStartSeconds", "CfgTimeoutSec"]


def build_query(con):
    """QUERY plus whichever OPTIONAL_COLUMNS exist in this database."""
    have = {row[1] for row in con.execute("PRAGMA table_info(CustomPerfStats)")}
    extra = [c for c in OPTIONAL_COLUMNS if c in have]
    if not extra:
        return QUERY
    return QUERY.replace("\nFROM CustomPerfStats cps", ", " + ", ".join(extra) + "\nFROM CustomPerfStats cps", 1)


def resolve_db_path():
    if len(sys.argv) > 1:
        return sys.argv[1]
    candidates = glob.glob("*.db")
    if len(candidates) == 0:
        sys.exit("No .db file found in current directory.")
    if len(candidates) > 1:
        sys.exit(
            "Multiple .db files found; pass the path explicitly:\n  "
            + "\n  ".join(candidates)
        )
    return candidates[0]


def main():
    db_path = resolve_db_path()
    stamp = datetime.now().strftime("%Y%m%d%H%M")
    out_path = f"CustomPerfStats_{stamp}.csv"

    con = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(build_query(con), con)
    finally:
        con.close()

    df.to_csv(out_path, index=False)
    print(f"Extracted {len(df)} rows from {db_path} -> {out_path}")


if __name__ == "__main__":
    main()
