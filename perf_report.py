"""
Builds the Performance Stats section (CSS / HTML body / JS fragments) of the
VPin stats report from CustomPerfStats rows (PresentMon-based pinball table
performance captures). Adapted from VPinPerfMon generate_scatter_report.py.

This file is executed by build_report.py with these globals injected:
    SRC        DataFrame of CustomPerfStats rows, or a path to a CSV of them
    SRC_LABEL  human-readable description of the data source
    PERF_LOGS_DIR  optional logs path or list (first = primary; None/[] = from FileRawDataPath)
and leaves PERF_CSS, PERF_BODY, PERF_SCRIPT and REPORT_INFO behind.

See PERF_STATS_REPORT_PROCESS.md in the VPinPerfMon repo (Reporting folder) for methodology notes.
"""
import html
import json
import os
import re
import sys
from datetime import datetime

import pandas as pd

# Chart data colors are separate from the report UI theme so they can be
# adjusted without editing the generated HTML or the Chart.js configuration.
CHART_POINT_COLOR = "#4fd1c5"

# GPU load bands (percent of the refresh interval): below the first = headroom, up to the second = moderate,
# above = high. Injected from report_config.GPU_LOAD_BANDS; VR users will want higher values.
GPU_LOAD_BANDS = tuple(globals().get("GPU_LOAD_BANDS") or (50, 85))
if len(GPU_LOAD_BANDS) != 2 or not 0 < GPU_LOAD_BANDS[0] < GPU_LOAD_BANDS[1]:
    sys.exit("GPU_LOAD_BANDS must be two ascending positive numbers, e.g. (50, 85).")

# ---------------------------------------------------------------------------
# 1. Load input
# ---------------------------------------------------------------------------
df = SRC.copy() if isinstance(SRC, pd.DataFrame) else pd.read_csv(SRC)

# ---------------------------------------------------------------------------
# 2. Select / clean columns used for charting
# ---------------------------------------------------------------------------
# Table display name: new db-extracted CSVs carry "GameDisplay" (via
# extract_perf_stats.py) -- the clean, human-readable name. Older/alternate
# exports may instead have "GameName" (raw filename-ish) or "SourceFile"
# (raw PresentMon export). Support all three so any source works.
for candidate in ("GameDisplay", "GameName", "SourceFile"):
    if candidate in df.columns:
        NAME_COL = candidate
        break
else:
    sys.exit(
        "Expected a 'GameDisplay', 'GameName', or 'SourceFile' column in the CSV; found neither."
    )

HAS_EMULATOR = "EmuDisplay" in df.columns

# Emulator name -> configured color (EMU_COLORS is keyed by emulator ID and injected
# by build_report.py). Needs an EMUID column; otherwise the JS palette is used.
EMU_COLOR_BY_NAME = {}
if HAS_EMULATOR and "EMUID" in df.columns:
    _configured = {str(k): v for k, v in globals().get("EMU_COLORS", {}).items()}
    for _id, _name in df[["EMUID", "EmuDisplay"]].drop_duplicates().itertuples(index=False):
        if str(_id) in _configured and isinstance(_name, str):
            EMU_COLOR_BY_NAME[_name] = _configured[str(_id)]
HAS_GAME_RATING = "GameRating" in df.columns

cols = [
    NAME_COL, "EntryDate", "SampleCount",
    "CpuAverage", "GpuAverage", "GpuMemoryAverage",
    "FpsAverage", "FpsMin", "FpsMax", "Fps1PctLow", "FpsPoint1PctLow",
    "FrameTimeAvg", "FrameTimeStdDev", "FrameTimeMax",
    "StutterScoreFT", "StutterScoreDT",
    "BlameCpuPct", "BlameGpuPct",
    "AnimationErrorRMS",
]
GRADE_COLS = [
    "StutterGrade", "StutterGradeScore", "StutterGradeCapped", "StutterGradeVersion",
    "PresentApplication", "PresentMonFrameCount", "GradeFrames", "GradeDurationSec", "GradeRefreshMs",
    "DoubledFrames", "TripleFrames", "LongestMissRefreshes", "MissedFramePct", "MissesPerMinute",
    "WeightedMissPct", "ClusterPeakMisses", "ClusterRatio", "ClusterMultiplier", "EffectiveMissPct",
    "LongGapCount", "LongGapMissCount",
]
# Grade columns are optional so older CSV extracts still build (the grade charts just stay empty).
HAS_GRADE = all(c in df.columns for c in ("StutterGrade", "StutterGradeScore", "StutterGradeVersion"))
if HAS_GRADE:
    cols += [c for c in GRADE_COLS if c in df.columns]
if HAS_EMULATOR:
    cols.append("EmuDisplay")
cols += [c for c in ("CpuMin", "CpuMax", "GpuMin", "GpuMax") if c in df.columns]
# Stall-based blame (BlameVersion 4) and GPU identity: all optional so unmigrated databases still build.
BLAME_NUMERIC = [
    "BlameVersion", "GpuBusyPctOfRefresh", "GpuBusy99th", "StallCpuCount", "StallGpuCount",
    "StallModeSwitchCount", "StallAppWaitCount", "StallDisplayCount",
]
BLAME_TEXT = ["GpuName", "GpuDriverVersion"]
cols += [c for c in BLAME_NUMERIC + BLAME_TEXT if c in df.columns and c not in cols]
if HAS_GAME_RATING:
    cols.append("GameRating")
HAS_GAME_ID = "GameID" in df.columns
HAS_VR = "IsVR" in df.columns
if HAS_GAME_ID:
    cols.append("GameID")
if HAS_VR:
    cols.append("IsVR")
data = df[cols].copy()

if HAS_VR:
    data["IsVR"] = pd.to_numeric(data["IsVR"], errors="coerce").fillna(0).astype(int).clip(0, 1)

if HAS_GAME_RATING:
    data["GameRating"] = pd.to_numeric(data["GameRating"], errors="coerce").fillna(0)
    invalid_ratings = ~data["GameRating"].between(0, 5)
    if invalid_ratings.any():
        bad_values = sorted(data.loc[invalid_ratings, "GameRating"].unique())
        sys.exit(f"GameRating must be between 0 and 5; found: {bad_values}")
    data["GameRating"] = data["GameRating"].astype(int)
else:
    data["GameRating"] = 0


def clean_name(s):
    # Only strip known table-file extensions; GameName values typically
    # don't have one, so this is a no-op for db-extracted data.
    for ext in (".vpx", ".fpt", ".txt", ".pxp"):
        if isinstance(s, str) and s.lower().endswith(ext):
            return s[: -len(ext)]
    return s


data["Table"] = data[NAME_COL].apply(clean_name)
data["SpreadRatio"] = (data["FpsMax"] - data["FpsMin"]) / data["FpsAverage"]
entry_dates = pd.to_datetime(data["EntryDate"], errors="coerce")
if entry_dates.isna().any():
    bad_count = int(entry_dates.isna().sum())
    sys.exit(f"EntryDate contains {bad_count} value(s) that could not be parsed.")
data["EntryDateDate"] = entry_dates.dt.strftime("%Y-%m-%d")
data["EntryDateTime"] = entry_dates.dt.strftime("%Y-%m-%d %H:%M:%S")
MIN_ENTRY_DATE = data["EntryDateDate"].min()
MAX_ENTRY_DATE = data["EntryDateDate"].max()

GRADE_COLORS = {"A": "#48bb78", "B": "#4fd1c5", "C": "#ecc94b", "D": "#ed8936", "F": "#f56565"}
RENDERER_LABELS = {
    "VPinballX64.exe": "VPX 10.8", "VPinballX86.exe": "VPX 10.8 (32-bit)",
    "VPinballX_BGFX64.exe": "VPX BGFX",
}
RENDERER_PALETTE = ["#63b3ed", "#f6ad55", "#b794f4", "#fc8181", "#68d391", "#faf089", "#a0aec0"]
RENDERER_COLORS = {}
if HAS_GRADE:
    # Only graded runs that actually captured frames take part in the grade charts.
    _valid = data["StutterGradeVersion"].notna() & (
        pd.to_numeric(data["PresentMonFrameCount"], errors="coerce").fillna(0) > 0
        if "PresentMonFrameCount" in data.columns else True
    )
    _grade_numeric = [c for c in GRADE_COLS if c in data.columns and c not in ("StutterGrade", "PresentApplication")]
    for c in _grade_numeric:
        data[c] = pd.to_numeric(data[c], errors="coerce")
    data.loc[~_valid, [c for c in _grade_numeric if c != "PresentMonFrameCount"]] = float("nan")
    data.loc[~_valid, "StutterGrade"] = None
    data["GradeHz"] = 1000.0 / data["GradeRefreshMs"].where(data["GradeRefreshMs"] > 0)
    data["StallMs"] = data["LongestMissRefreshes"] * data["GradeRefreshMs"]
    # EntryDate is UTC; shift to local wall time, then encode as epoch-ms so the JS can format it as UTC.
    _local = pd.to_datetime(data["EntryDate"], errors="coerce", utc=True).dt.tz_convert(
        datetime.now().astimezone().tzinfo).dt.tz_localize(None)
    data["EntryTs"] = (_local - pd.Timestamp("1970-01-01")) / pd.Timedelta(milliseconds=1)
    data["Renderer"] = data["PresentApplication"].map(
        lambda a: RENDERER_LABELS.get(a, a) if isinstance(a, str) and a else None)
    for i, name in enumerate(sorted(data["Renderer"].dropna().unique())):
        RENDERER_COLORS[name] = RENDERER_PALETTE[i % len(RENDERER_PALETTE)]
    cols += ["GradeHz", "StallMs", "EntryTs", "Renderer"]

# Stall-based blame (VPinPerfMon BlameVersion 4). Counts are per cause of the stalls the app itself caused.
HAS_BLAME = "BlameVersion" in data.columns
for c in BLAME_NUMERIC:
    if c in data.columns:
        data[c] = pd.to_numeric(data[c], errors="coerce")
_blame_versions = data["BlameVersion"].dropna() if HAS_BLAME else pd.Series(dtype=float)
LATEST_BLAME_VERSION = int(_blame_versions.max()) if len(_blame_versions) else None
if HAS_BLAME:
    # Anything below the highest version present (or NULL) used an older formula: label it, never mix it silently.
    data["BlameLegacy"] = (data["BlameVersion"].isna() | (data["BlameVersion"] < LATEST_BLAME_VERSION)
                           ).astype(int) if LATEST_BLAME_VERSION is not None else 1
    cols.append("BlameLegacy")
_stall_cols = ["StallCpuCount", "StallGpuCount", "StallModeSwitchCount", "StallAppWaitCount", "StallDisplayCount"]
HAS_STALLS = all(c in data.columns for c in _stall_cols)
if HAS_STALLS and HAS_GRADE:
    _minutes = (data["GradeDurationSec"] / 60.0).where(data["GradeDurationSec"] > 0)
    data["StallCpuPerMin"] = data["StallCpuCount"] / _minutes
    data["StallTotal"] = data[_stall_cols].sum(axis=1, min_count=len(_stall_cols))
    cols += ["StallCpuPerMin", "StallTotal"]
if HAS_GRADE and "GpuBusyPctOfRefresh" not in data.columns:
    # Older databases: no stored GPU load, so none is shown (the p99 form below is only a chart axis fallback).
    data["GpuBusyPctOfRefresh"] = float("nan")
if HAS_GRADE and "GpuBusy99th" in data.columns:
    data["GpuBusyP99PctOfRefresh"] = pd.to_numeric(data["GpuBusy99th"], errors="coerce") / data["GradeRefreshMs"].where(data["GradeRefreshMs"] > 0) * 100
    cols.append("GpuBusyP99PctOfRefresh")
if HAS_GRADE and "GpuBusyPctOfRefresh" in data.columns and "GpuBusyPctOfRefresh" not in cols:
    cols.append("GpuBusyPctOfRefresh")

# Run drill-down: the browser re-reads a run's raw PresentMon CSV (picked by the user) and
# re-grades it, so each record carries the CSV file name, swap chain and grading config.
# All optional so older CSV extracts still build (the drill-down then just matches nothing).
DRILL_DEFAULT_CFG = {
    "CfgDoubledRefreshes": 1.5, "CfgTripleRefreshes": 2.5, "CfgBandA": 0.1, "CfgBandB": 0.4,
    "CfgBandC": 1.0, "CfgBandD": 3.0, "CfgZeroScorePct": 10.0, "CfgTripleWeight": 3.0,
    "CfgTripleCapGrade": "C", "CfgTripleCapMinCount": 2, "CfgClusterWindowSec": 5.0,
    "CfgClusterWeight": 0.1, "CfgClusterMaxExcess": 4.0, "CfgClusterMinEvents": 3,
    "CfgClusterMinPeak": 2, "CfgMinFrames": 100, "CfgTrimEndSeconds": 5.0,
    "CfgBlameMinStalls": 8, "CfgBlameGpuSaturation": 0.9,
}
DRILL_CFGS = []  # distinct grading configs; records point at one via CfgIdx
for c in ("GradeMissingFrames", "GradeRefreshSource", "SwapChainAddress"):
    if c in df.columns:
        data[c] = df[c]
        cols.append(c)
if "GradeMissingFrames" in data.columns:
    data["GradeMissingFrames"] = pd.to_numeric(data["GradeMissingFrames"], errors="coerce")
if "FileRawDataPath" in df.columns:
    data["RawFile"] = df["FileRawDataPath"].map(
        lambda p: re.split(r"[\\/]", p.strip())[-1] if isinstance(p, str) and p.strip() else None)
    cols.append("RawFile")


def _logs_info():
    """Where the drill-down's CSVs are expected, and (when that folder exists on the build machine)
    which runs actually have their CSV there. Runs get LogOnDisk 1/0, or None when unchecked."""
    configured = globals().get("PERF_LOGS_DIR")
    if configured is None:
        folders = []
    elif isinstance(configured, str):
        folders = [configured]
    elif isinstance(configured, (list, tuple)):
        folders = list(configured)
    else:
        raise ValueError("PERF_LOGS_DIR must be a path string, a list of paths, or None.")
    if any(not isinstance(p, str) or not p.strip() for p in folders):
        raise ValueError("PERF_LOGS_DIR paths must be non-empty strings.")
    folders = list(dict.fromkeys(os.path.normpath(p) for p in folders))
    raw = df["FileRawDataPath"] if "FileRawDataPath" in df.columns else pd.Series(dtype=object)
    raw = raw.map(lambda p: p.strip() if isinstance(p, str) and p.strip() else None)
    dirs = raw.dropna().map(lambda p: re.sub(r"[\\/][^\\/]*$", "", p))
    info = {"dir": None, "alternates": folders[1:], "source": None,
            "checked": False, "found": 0, "total": int(raw.notna().sum())}
    if folders:
        info["dir"], info["source"] = folders[0], "config"
    elif not dirs.empty:
        info["dir"], info["source"] = dirs.value_counts().index[0], "database"
    on_disk = pd.Series([None] * len(df), index=df.index, dtype=object)
    if info["dir"] and os.path.isdir(info["dir"]):
        names = {n.lower() for n in os.listdir(info["dir"])}
        for i, p in raw.items():
            if p:
                name = re.split(r"[\\/]", p)[-1]
                # Without a configured folder each run is also checked at its own recorded path.
                on_disk[i] = int(name.lower() in names or (not folders and os.path.isfile(p)))
        info["checked"] = True
        info["found"] = int(sum(v == 1 for v in on_disk))
    return info, on_disk


DRILL_LOGS, _on_disk = _logs_info()
if DRILL_LOGS["checked"]:
    data["LogOnDisk"] = _on_disk
    cols.append("LogOnDisk")


def _drill_cfg(row):
    cfg = {}
    for key, default in DRILL_DEFAULT_CFG.items():
        v = row.get(key, default)
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            v = default
        cfg[key[3:]] = v if isinstance(v, str) else float(f"{float(v):.6g}")
    # Warm-up (--delaystart) isn't in older databases; the browser then reads the run's TXT log or infers it.
    delay = row.get("CfgTrimStartSeconds")
    if delay is not None and not pd.isna(delay):
        cfg["TrimStartSeconds"] = float(f"{float(delay):.6g}")
    if cfg not in DRILL_CFGS:
        DRILL_CFGS.append(cfg)
    return DRILL_CFGS.index(cfg)


data["CfgIdx"] = df.apply(_drill_cfg, axis=1)
cols.append("CfgIdx")

def point_list(source_records, xkey, ykey):
    pts = []
    for r in source_records:
        x, y = r.get(xkey), r.get(ykey)
        if x is None or y is None:
            continue
        pts.append({
            "x": x,
            "y": y,
            "label": r["Table"],
            "rating": r["GameRating"],
        })
    return pts


_LEGACY_BLAME_NOTE = (
    " Rows graded with an older blame formula are labelled <b>legacy blame</b> in the tooltip; they came from a "
    "different calculation (worst 1% of frames, which frame pacing contaminated) and are not comparable."
    if HAS_BLAME else ""
)
BLAME_NOTE = (
    "The CPU-vs-GPU share of the <b>stalls the app itself caused</b>, weighted by milliseconds over the refresh "
    "&mdash; the two always add up to 100%. Present-mode-switch, display/compositor and app-waiting stalls are not "
    "blamed on either side, and runs with too few CPU/GPU stalls (see <code>CfgBlameMinStalls</code>) have no blame "
    "at all, so most runs are absent here. Lead with GPU load; this is the secondary &ldquo;what caused the hitches&rdquo; view."
    + _LEGACY_BLAME_NOTE
)


chart_defs = [
    {
        "id": "fpsVsCpu", "title": "FPS Average vs CPU Average",
        "x": "CpuAverage", "y": "FpsAverage",
        "xlabel": "CPU Average (%)", "ylabel": "FPS Average",
    },
    {
        "id": "fpsVsGpu", "title": "FPS Average vs GPU Average",
        "x": "GpuAverage", "y": "FpsAverage",
        "xlabel": "GPU Average (%)", "ylabel": "FPS Average",
    },
    {
        "id": "cpuVsGpu", "title": "CPU Average vs GPU Average",
        "x": "CpuAverage", "y": "GpuAverage",
        "xlabel": "CPU Average (%)", "ylabel": "GPU Average (%)",
    },
    {
        "id": "frameTimeStability", "title": "Frame Time Avg vs Frame Time Std Dev (stutter indicator)",
        "x": "FrameTimeAvg", "y": "FrameTimeStdDev",
        "xlabel": "Frame Time Avg (ms)", "ylabel": "Frame Time Std Dev (ms)",
    },
    {
        "id": "fpsVsGpuMem", "title": "FPS Average vs GPU Memory Average",
        "x": "GpuMemoryAverage", "y": "FpsAverage",
        "xlabel": "GPU Memory Average (units)", "ylabel": "FPS Average",
    },
    {
        "id": "blame", "title": "Blame CPU% vs Blame GPU% (bottleneck attribution)",
        "x": "BlameCpuPct", "y": "BlameGpuPct", "edgeMark": True,
        "xlabel": "Blame CPU %", "ylabel": "Blame GPU %",
        "note": BLAME_NOTE,
    },
    {
        "id": "fps1pctLow", "title": "FPS Average vs 1% Low FPS (consistency)",
        "x": "FpsAverage", "y": "Fps1PctLow",
        "xlabel": "FPS Average", "ylabel": "FPS 1% Low",
    },
]

# Stutter-grade charts (A-F grade, based on missed refreshes of DisplayedTime).
# colorBy: "grade" | "renderer"; sizeBy: record key scaling point radius;
# xType: "time" formats the x axis as local date/time; yLines: reference lines (band edges).
GRADE_NOTE_FILTER = "Only graded runs with captured frames are shown. "
if HAS_GRADE:
    chart_defs += [
        {
            "id": "gradeDist", "kind": "gradeBar",
            "title": "Stutter Grade Mix by Emulator",
            "note": GRADE_NOTE_FILTER + (
                "Share of runs at each A&ndash;F grade, per emulator (each bar is 100%, so emulators with "
                "different run counts compare fairly; the run count is in the label). Hover for counts and "
                "the number of runs <b>capped at C</b> by two or more 3+ refresh stalls."
            ),
        },
        {
            "id": "gradeVsCadence", "title": "Grade Score vs Display Cadence",
            "x": "GradeHz", "y": "StutterGradeScore", "colorBy": "grade", "legend": "grade",
            "xlabel": "Display cadence (Hz, 1000 / GradeRefreshMs)", "ylabel": "Stutter Grade Score (0-100)",
            "note": GRADE_NOTE_FILTER + (
                "Do low scores cluster at particular display setups or frame caps? Points are colored by grade."
            ),
        },
        {
            "id": "missBreakdown", "title": "Why It Scored Badly: Doubled vs Triple Frames",
            "x": "DoubledFrames", "y": "TripleFrames", "colorBy": "grade", "legend": "grade",
            "sizeBy": "StallMs", "focus": True,
            "xlabel": "Doubled frames (1 missed refresh)", "ylabel": "Triple+ frames (2+ missed refreshes)",
            "note": GRADE_NOTE_FILTER + (
                "Point size is the longest stall in ms (capped at the 95th percentile so one huge stall "
                "doesn't swamp the rest). Bottom-right = many small misses; top-left = a few long stalls; "
                "a large point means one very long stall."
            ),
        },
        {
            "id": "burstiness", "title": "Burst vs Steady Misses",
            "x": "MissesPerMinute", "y": "ClusterPeakMisses", "colorBy": "grade", "legend": "grade",
            "focus": True,
            "xlabel": "Misses per minute", "ylabel": "Peak misses in one 5 s window",
            "note": GRADE_NOTE_FILTER + (
                "Steady stutter has a high rate with a modest peak; a burst has a low rate but a high peak "
                "(it earns the burst penalty). Points are colored by grade."
            ),
        },
        {
            "id": "missRates", "title": "Miss Rate: Raw vs Effective (grade bands)",
            "x": "MissedFramePct", "y": "EffectiveMissPct", "colorBy": "grade", "legend": "grade",
            "yLines": [0.1, 0.4, 1, 3], "yType": "log", "focus": True,
            "xlabel": "Missed frame % (plain)", "ylabel": "Effective miss % (weighted x burst, log scale)",
            "note": GRADE_NOTE_FILTER + (
                "Horizontal lines are the grade band edges (A &lt; 0.1%, B &lt; 0.4%, C &lt; 1%, D &lt; 3%, "
                "F above). Points far above the diagonal were penalized for 3+ stalls or bursts. The y axis "
                "is logarithmic so every band gets room; runs with no misses sit on the bottom edge (&#9660;)."
            ),
        },
        {
            "id": "gradeVsGpu", "title": "GPU Load vs Grade Score",
            "x": "GpuAverage", "y": "StutterGradeScore", "colorBy": "grade", "legend": "grade",
            "xlabel": "GPU Average (%)", "ylabel": "Stutter Grade Score (0-100)",
            "note": GRADE_NOTE_FILTER + "Does heavier GPU load actually drive stutter? (System-wide, 1 s samples.)",
        },
        {
            "id": "gradeVsCpu", "title": "CPU Load vs Grade Score",
            "x": "CpuAverage", "y": "StutterGradeScore", "colorBy": "grade", "legend": "grade",
            "xlabel": "CPU Average (%, whole machine)", "ylabel": "Stutter Grade Score (0-100)",
            "note": GRADE_NOTE_FILTER + "CPU is whole-machine utilization, not just the game.",
        },
        {
            "id": "longGaps", "title": "Long Present Gaps vs Visible Misses",
            "x": "LongGapCount", "y": "LongGapMissCount", "colorBy": "grade", "legend": "grade",
            "focus": True,
            "xlabel": "Long gaps between Present() calls", "ylabel": "Long gaps followed by a visible miss",
            "note": GRADE_NOTE_FILTER + (
                "A long gap with no visible miss costs nothing (frame pacing makes many gaps harmless). "
                "Points near the bottom have many gaps but few visible effects."
            ),
        },
    ]
    if HAS_STALLS and "GpuBusyPctOfRefresh" in data.columns:
        chart_defs.append({
            "id": "gpuLoadVsCpuStalls", "title": "GPU Load vs CPU-Caused Stalls",
            "x": "GpuBusyPctOfRefresh", "y": "StallCpuPerMin", "colorBy": "renderer", "legend": "renderer",
            "focus": True,
            "xlabel": "GPU load (median GPU busy, % of refresh)", "ylabel": "CPU-labelled stalls per minute",
            "note": GRADE_NOTE_FILTER + (
                "Does the GPU explain the hitches? Bottom-right (high GPU load, few CPU stalls) is busy but smooth; "
                "top-left (low GPU load, many CPU stalls) is CPU-side hitching. A run can have high GPU load "
                "<i>and</i> CPU stalls, so read both axes. Points are colored by renderer. Only runs graded with the "
                "stall-based formula appear."
            ),
        })

# focus: open zoomed on the bulk of runs; runs past ~2x the 98th percentile are pinned to the edge.
FOCUS_NOTE = (
    " <b>Focused view:</b> runs far beyond the rest (over twice the 98th percentile) are pinned to the "
    "edge as &#9654;/&#9650; markers &mdash; hover for the real values, or use <b>Show all</b>."
)
for c in chart_defs:
    if c.get("focus"):
        c["note"] = c.get("note", "") + FOCUS_NOTE

# Legacy score
chart_defs.append({
    "id": "stutterScores", "title": "Stutter Score: Frame Time vs Displayed Time (legacy)",
    "x": "StutterScoreFT", "y": "StutterScoreDT",
    "xlabel": "Stutter Score (Frame Time)", "ylabel": "Stutter Score (Displayed Time)",
    "note": (
        "<b>Legacy.</b> Both are 0&ndash;100 <b>smoothness</b> scores (higher = better; 100 = no detected stutter). "
        "<b>Frame Time</b> measures rendering-pipeline consistency (how evenly the app submits frames); "
        "<b>Displayed Time</b> measures perceived visual smoothness (how evenly frames actually land on "
        "screen). A perfect 100 on Frame Time but much lower Displayed Time indicates downstream "
        "(vsync/compositor/driver) is causing visible hitches. Prefer the stutter grade charts above."
    ),
})

# One- or two-sentence summaries shown under each chart; the longer note (if any) sits behind "More info".
CHART_BRIEF = {
    "fpsVsCpu": "Do busier CPUs mean lower FPS? One dot per run.",
    "fpsVsGpu": "Do busier GPUs mean lower FPS? One dot per run.",
    "cpuVsGpu": "CPU load against GPU load per run: which side is working harder.",
    "frameTimeStability": "Average frame time against its spread. Dots high above the trend are uneven, stuttery runs.",
    "fpsVsGpuMem": "FPS against GPU memory-interface load.",
    "blame": "CPU vs GPU share of the stalls the app caused. Most runs are blank because they had too few stalls.",
    "fps1pctLow": "Average FPS against the 1% low. Dots far below the rest have a ragged worst-case tail.",
    "gradeDist": "Share of runs at each A&ndash;F grade, per emulator.",
    "gradeVsCadence": "Do low scores cluster at particular display rates or frame caps?",
    "missBreakdown": "Why a run scored badly: many small misses (bottom right) or a few long stalls (top left).",
    "burstiness": "Steady stutter has a high rate and a modest peak; a burst has a low rate and a high peak.",
    "missRates": "Plain vs effective miss rate. Lines mark the grade band edges.",
    "gradeVsGpu": "Does heavier GPU load drive stutter?",
    "gradeVsCpu": "Does heavier CPU load drive stutter? (CPU is whole-machine.)",
    "longGaps": "Long gaps between presents, and how many of them were actually visible.",
    "gpuLoadVsCpuStalls": "GPU load against CPU-caused stalls per minute: busy-but-smooth vs CPU hitching.",
    "stutterScores": "Legacy smoothness scores. Prefer the grade charts above.",
}
for c in chart_defs:
    if c["id"] in CHART_BRIEF:
        c["brief"] = CHART_BRIEF[c["id"]]

data["ConsistencyRatio"] = data["Fps1PctLow"] / data["FpsAverage"]


def records_for_frame(frame):
    frame_records = []
    record_cols = list(cols) + [
        "Table", "SpreadRatio", "ConsistencyRatio", "GameRating", "EntryDateDate", "EntryDateTime"
    ]
    for _, row in frame.iterrows():
        rec = {}
        for c in dict.fromkeys(record_cols):
            v = row[c]
            if pd.isna(v):
                v = None
            rec[c] = v
        frame_records.append(rec)
    return frame_records


all_records = records_for_frame(data)

# ---------------------------------------------------------------------------
# 3. Report-state calculations
# ---------------------------------------------------------------------------
# Tukey fence (1.5 x IQR) outlier detection on key metrics. Thresholds are
# derived from the dataset itself each run, so the detector adapts
# automatically as the fleet of captures grows or shifts.
anomaly_specs = [
    ("FpsAverage", "low", "Average FPS unusually low vs. the rest of the dataset"),
    ("CpuAverage", "high", "CPU utilization unusually high (possible CPU-bound/contended run)"),
    ("GpuAverage", "high", "GPU utilization unusually high (possible GPU-bound/contended run)"),
    ("FrameTimeStdDev", "high", "Frame time variance unusually high (uneven pacing)"),
    ("AnimationErrorRMS", "high", "Animation error unusually high (visible judder/stutter)"),
    ("SpreadRatio", "high", "FPS min/max spread unusually wide relative to average (transient hitch)"),
]


def tukey_bounds(series):
    s = series.dropna()
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


RERUN_THRESHOLD = 2  # rows flagged on >=2 independent metrics are the strongest re-run candidates

# One row per table: average each capture's metrics together (tables with
# multiple runs shouldn't crowd out single-capture tables, and averaging
# smooths out any one-off blip). Then rank tables against each other on
# each metric (percentile rank, 0-100, robust to outliers/scale), and blend
# those ranks into one weighted composite score. Higher score = better.
#
# The bulk of the weight is delegated to the capture tool's own recommended
# perceived-smoothness metric, StutterScoreDT, which already blends the
# frame-pacing components internally (99.5%ile delta 50%, avg delta 25%,
# std dev 10%, animation-error RMS 10%, animation-error MAD 5%). We do NOT
# also score FrameTimeStdDev / AnimationErrorRMS separately -- that would
# double-count metrics already baked into StutterScoreDT (and those two
# correlate ~0.98 with each other, so weighting both over-emphasizes a
# single dimension). Crucially, StutterScoreDT is self-normalized to each
# table's own effective refresh rate, so it is blind to raw throughput
# (a table running smoothly at 92 FPS scores as well as one at 135 FPS);
# FpsAverage is kept to catch slow-but-smooth tables that DT would hide.
PERFORMANCE_SCORE_WEIGHTS = [
    # (column, direction, weight, description)
    ("StutterScoreDT", "high", 0.60, "DisplayedTime stutter score -- the capture tool's recommended perceived-smoothness metric (already blends 99.5%ile/avg frame-time delta, std dev, and animation error internally)"),
    ("FpsAverage", "high", 0.25, "Average FPS -- raw throughput (StutterScoreDT self-normalizes to each table's own frame rate, so this is needed to catch slow-but-smooth tables)"),
    ("ConsistencyRatio", "high", 0.15, "FPS 1% Low / FPS Average -- tail consistency (how close the worst 1% of frames stay to the average), independent of the other two"),
]

def composite_score(row):
    total_w, total = 0.0, 0.0
    for col, _, w, _ in PERFORMANCE_SCORE_WEIGHTS:
        r = row[f"_rank_{col}"]
        if pd.notna(r):
            total += w * r
            total_w += w
    return total / total_w if total_w else None

def build_report_state(filtered_data):
    filtered_records = records_for_frame(filtered_data)

    state_chart_data = {
        c["id"]: point_list(filtered_records, c["x"], c["y"])
        for c in chart_defs if "x" in c
    }

    bounds = {
        col: tukey_bounds(filtered_data[col])
        for col, _, _ in anomaly_specs
    }
    anomaly_rows = []
    for _, row in filtered_data.iterrows():
        reasons = []
        for col, direction, desc in anomaly_specs:
            lo, hi = bounds[col]
            val = row[col]
            if pd.isna(val):
                continue
            if direction == "low" and val < lo:
                reasons.append(f"{col}={val:.2f} (< normal floor {lo:.2f}) \u2014 {desc}")
            elif direction == "high" and val > hi:
                reasons.append(f"{col}={val:.2f} (> normal ceiling {hi:.2f}) \u2014 {desc}")
        if reasons:
            anomaly_rows.append({
                "Table": row["Table"],
                "EntryDate": row["EntryDate"],
                "FpsAverage": None if pd.isna(row["FpsAverage"]) else row["FpsAverage"],
                "score": len(reasons),
                "reasons": reasons,
            })
    anomaly_rows.sort(key=lambda r: r["score"], reverse=True)
    rerun_candidates = [r for r in anomaly_rows if r["score"] >= RERUN_THRESHOLD]
    watch_list = [r for r in anomaly_rows if r["score"] == 1]

    score_cols = [spec[0] for spec in PERFORMANCE_SCORE_WEIGHTS]
    table_agg = filtered_data.groupby("Table", as_index=False)[score_cols].mean()
    table_agg["CaptureCount"] = filtered_data.groupby("Table").size().values
    table_agg = table_agg.dropna(subset=["FpsAverage"]).reset_index(drop=True)
    for col, direction, _, _ in PERFORMANCE_SCORE_WEIGHTS:
        valid = table_agg[col].notna()
        ranks = pd.Series(index=table_agg.index, dtype=float)
        ranks[valid] = (
            table_agg.loc[valid, col].rank(
                pct=True, ascending=(direction == "high")
            ) * 100
        )
        table_agg[f"_rank_{col}"] = ranks
    table_agg["PerformanceScore"] = table_agg.apply(composite_score, axis=1)
    ranked = table_agg.dropna(subset=["PerformanceScore"]).sort_values(
        "PerformanceScore", ascending=False
    )

    emu_chart_data = []
    if HAS_EMULATOR and not filtered_data.empty:
        emu_agg = filtered_data.dropna(subset=["FpsAverage"]).groupby("EmuDisplay").agg(
            FpsAverage=("FpsAverage", "mean"),
            CpuAverage=("CpuAverage", "mean"),
            GpuAverage=("GpuAverage", "mean"),
            FrameTimeStdDev=("FrameTimeStdDev", "mean"),
            AnimationErrorRMS=("AnimationErrorRMS", "mean"),
            CaptureCount=("FpsAverage", "size"),
        ).reset_index()
        min_std = emu_agg["FrameTimeStdDev"].min()
        max_std = emu_agg["FrameTimeStdDev"].max()
        std_range = max_std - min_std
        for _, row in emu_agg.iterrows():
            std_dev = row["FrameTimeStdDev"]
            radius = (
                20
                if pd.isna(std_dev) or std_range == 0
                else 8 + 34 * ((std_dev - min_std) / std_range)
            )
            if pd.isna(row["GpuAverage"]) or pd.isna(row["CpuAverage"]):
                continue
            emu_chart_data.append({
                "x": row["GpuAverage"],
                "y": row["CpuAverage"],
                "r": radius,
                "label": row["EmuDisplay"],
                "count": int(row["CaptureCount"]),
                "fps": None if pd.isna(row["FpsAverage"]) else round(row["FpsAverage"], 1),
                "frameTimeStdDev": None if pd.isna(std_dev) else round(std_dev, 2),
                "animErrorRms": None if pd.isna(row["AnimationErrorRMS"]) else round(row["AnimationErrorRMS"], 2),
            })

    return {
        "summary": {
            "rows": len(filtered_data),
            "tables": int(filtered_data["Table"].nunique()),
            "missingFps": int(filtered_data["FpsAverage"].isna().sum()),
            "reruns": len(rerun_candidates),
        },
        "chartData": state_chart_data,
        "emuChartData": emu_chart_data,
        "top10Best": ranked.head(10).to_dict("records"),
        "top10Worst": ranked.tail(10).iloc[::-1].to_dict("records"),
        "rerunCandidates": rerun_candidates,
        "watchList": watch_list,
        "rerunTables": sorted({r["Table"] for r in rerun_candidates}),
        "tables": sorted(str(t) for t in filtered_data["Table"].dropna().unique()),
    }


def _gpu_select(select_id, column, label):
    if column not in data.columns:
        return ""
    values = sorted({str(v) for v in data[column].dropna() if str(v).strip()})
    if not values:
        return ""
    options = "".join(f'<option value="{html.escape(v)}">{html.escape(v)}</option>' for v in values)
    return f'<select id="{select_id}" aria-label="{label}"><option value="">{label}: all</option>{options}</select>'


GPU_FILTER_HTML = _gpu_select("gpuNameFilter", "GpuName", "GPU") + _gpu_select("gpuDriverFilter", "GpuDriverVersion", "Driver")


def chart_note_html(c):
    """Short summary under the chart; the long explanation sits behind a native More info toggle."""
    note, brief = c.get("note"), c.get("brief")
    if not note and not brief:
        return ""
    if not brief:
        return f"<p class='chart-note'>{note}</p>"
    more = f"<details class='chart-more'><summary>More info</summary><p class='chart-note'>{note}</p></details>" if note else ""
    return f"<p class='chart-note'>{brief}</p>{more}"


def legend_html(kind):
    if kind == "grade":
        items = GRADE_COLORS.items()
    elif kind == "renderer":
        items = RENDERER_COLORS.items()
    else:
        return ""
    return "<p class='chart-note'>" + " &nbsp; ".join(
        f"<span style='color:{color}'>&#9679;</span> {label}" for label, color in items
    ) + "</p>"


initial_state = build_report_state(data)

# ---------------------------------------------------------------------------
# 4. Render HTML
# ---------------------------------------------------------------------------

# Scoped under #tab-perf using native CSS nesting; shared theme variables,
# summary tiles, cards and grids live in the shell stylesheet.
PERF_CSS = f"""#tab-perf {{
  h2.section {{ font-size: 17px; margin: 32px 0 10px; border-top: 1px solid var(--cp-border); padding-top: 20px; }}
  p.muted {{ color: var(--cp-text-muted); font-size: 13px; }}
  .card.expanded {{
    position: fixed; inset: 16px; z-index: 1000; display: flex; flex-direction: column;
    background: var(--cp-surface); padding: 20px;
  }}
  .card.expanded canvas {{ flex: 1; max-height: none; min-height: 0; }}
  canvas {{ max-height: 340px; }}
  .chart-header {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    margin-bottom: 10px;
  }}
  .chart-header h2 {{ margin: 0; }}
  .chart-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .chart-actions button {{
    padding: 6px 10px; border: 1px solid var(--cp-border); border-radius: 0.625rem;
    background: var(--cp-surface-soft); color: var(--cp-text); cursor: pointer;
    font: inherit; font-size: 12px;
  }}
  .chart-actions button:hover {{ border-color: var(--cp-accent); color: var(--cp-accent); }}
  .chart-help {{ color: var(--cp-text-muted); font-size: 11px; margin: 0 0 8px; }}
  p.chart-note {{
    color: var(--cp-text-muted); font-size: 12px; margin: 10px 0 0; line-height: 1.5;
  }}
  details.chart-more {{ margin-top: 6px; }}
  details.chart-more summary {{ cursor: pointer; color: var(--cp-accent); font-size: 12px; user-select: none; }}
  .filter-bar {{
    margin-bottom: 18px;
  }}
  .filter-row {{
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  }}
  .filter-row ~ .filter-row {{ margin-top: 10px; }}
  .filter-bar label {{ color: var(--cp-text-muted); font-size: 13px; }}
  #searchBox, #ratingFilter, #vrFilter, #startDate, #endDate {{
    padding: 8px 12px; border-radius: 0.625rem; border: 1px solid var(--cp-border);
    background: var(--cp-surface); color: var(--cp-text); font-size: 13px;
  }}
  .perf-rangebar {{ display: flex; }}
  .perf-rangebar button {{
    background: var(--cp-surface-soft); color: var(--cp-text-muted); border: 1px solid var(--cp-border);
    margin-left: -1px; padding: 8px 14px; cursor: pointer; font: inherit; font-size: 13px;
  }}
  .perf-rangebar button:first-child {{ border-radius: 0.625rem 0 0 0.625rem; margin-left: 0; }}
  .perf-rangebar button:last-child {{ border-radius: 0 0.625rem 0.625rem 0; }}
  .perf-rangebar button:hover {{ color: var(--cp-text); }}
  .perf-rangebar button.active {{
    background: var(--cp-surface); color: var(--cp-chart-point); border-color: var(--cp-chart-point);
    position: relative; z-index: 1;
  }}
  #emuFilter, #tblFilter {{ position: relative; }}
  #emuFilter summary, #tblFilter summary {{
    padding: 8px 12px; border-radius: 0.625rem; border: 1px solid var(--cp-border); cursor: pointer;
    background: var(--cp-surface); color: var(--cp-text); font-size: 13px; list-style: none; user-select: none;
  }}
  #emuFilter summary::-webkit-details-marker, #tblFilter summary::-webkit-details-marker {{ display: none; }}
  #emuFilter summary::after, #tblFilter summary::after {{ content: " \\25BE"; color: var(--cp-text-muted); }}
  #emuFilter[open] summary, #tblFilter[open] summary {{ border-color: var(--cp-chart-point); }}
  #emuPanel, #tblPanel {{
    position: absolute; z-index: 20; top: calc(100% + 4px); left: 0; min-width: 260px; max-height: 320px; overflow: auto;
    background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 0.625rem; padding: 8px 10px;
    box-shadow: 0 8px 24px rgba(0,0,0,.4);
  }}
  #emuPanel label, #tblPanel label {{ display: flex; gap: 8px; align-items: center; padding: 4px 0; color: var(--cp-text); cursor: pointer; }}
  #emuPanel .emu-actions, #tblPanel .emu-actions {{ display: flex; gap: 6px; padding-bottom: 6px; border-bottom: 1px solid var(--cp-border); margin-bottom: 4px; }}
  #emuPanel .emu-actions button, #tblPanel .emu-actions button {{
    padding: 3px 10px; border-radius: 0.5rem; border: 1px solid var(--cp-border);
    background: var(--cp-surface-soft); color: var(--cp-text); cursor: pointer; font-size: 12px;
  }}
  #dateFilterError {{ color: var(--cp-danger); display: block; font-size: 12px; }}
  #searchBox {{ width: 320px; }}
  #tblFilter summary {{ width: 296px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  #tblPanel {{ width: 420px; }}
  #tblSearch {{ flex: 1; padding: 4px 8px; border-radius: 0.5rem; border: 1px solid var(--cp-border); background: var(--cp-surface-soft); color: var(--cp-text); font-size: 12px; }}
  #tblList {{ max-height: 240px; overflow: auto; }}
  table.anomaly-table {{
    width: 100%; border-collapse: collapse; font-size: 13px; background: var(--cp-surface);
    border: 1px solid var(--cp-border); border-radius: 0.625rem; overflow: hidden;
  }}
  table.anomaly-table th, table.anomaly-table td {{
    text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--cp-border); vertical-align: top;
  }}
  table.anomaly-table th {{ color: var(--cp-text-muted); font-weight: 600; }}
  table.anomaly-table tr:last-child td {{ border-bottom: none; }}
  .score-badge {{ color: var(--cp-warning); font-weight: 700; }}
  .perf-columns {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
    gap: 20px;
  }}
  .perf-columns h3 {{ font-size: 14px; margin: 0 0 10px; }}
  .perf-columns h3.best {{ color: var(--cp-success); }}
  .perf-columns h3.worst {{ color: var(--cp-danger); }}
  ol.perf-list {{
    background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 0.625rem;
    margin: 0; padding: 10px 14px; font-size: 13px; list-style: none;
  }}
  ol.perf-list li {{ padding: 6px 0; border-bottom: 1px solid var(--cp-border); }}
  ol.perf-list li:last-child {{ border-bottom: none; }}
  .perf-list .rank {{ color: var(--cp-text-muted); font-weight: 600; margin-right: 4px; }}
  .perf-table {{ font-weight: 600; }}
  footer {{ color: var(--cp-text-muted); font-size: 12px; margin-top: 30px; }}
}}
"""

# Run drill-down (see perf_drilldown.js). Plain strings, so no brace doubling.
PERF_CSS += """#tab-perf {
  #drillZone { border: 2px dashed transparent; border-radius: 16px; transition: border-color .15s; }
  #drillZone.drag { border-color: var(--cp-chart-point); }
  .drill-toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .drill-toolbar select, .drill-toolbar button {
    padding: 8px 12px; border-radius: 0.625rem; border: 1px solid var(--cp-border);
    background: var(--cp-surface); color: var(--cp-text); font: inherit; font-size: 13px;
  }
  .drill-toolbar select { min-width: 320px; max-width: 640px; }
  .drill-toolbar button { cursor: pointer; background: var(--cp-surface-soft); }
  .drill-toolbar button:hover { border-color: var(--cp-accent); color: var(--cp-accent); }
  .drill-toolbar .drill-count { color: var(--cp-text-muted); font-size: 12px; }
  .drill-hint { font-size: 12px; color: var(--cp-text-muted); margin-top: 8px; }
  .drill-hint code { color: var(--cp-text); user-select: all; }
  .drill-hint button { font: inherit; font-size: 11px; padding: 1px 8px; margin: 0 4px; cursor: pointer; border-radius: 6px;
                       border: 1px solid var(--cp-border); background: var(--cp-surface-soft); color: var(--cp-text); }
  .drill-hint button:hover { border-color: var(--cp-accent); color: var(--cp-accent); }
  .drill-chain { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; margin: 10px 0; font-size: 13px; color: var(--cp-text-muted); }
  .drill-chain[hidden] { display: none; }
  .drill-when { font-size: 13px; font-weight: 400; color: var(--cp-text-muted); margin-left: 6px; font-variant-numeric: tabular-nums; }
  .drill-groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; margin: 14px 0 20px; align-items: start; }
  .drill-group { background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: var(--radius); padding: 10px 12px 12px; }
  .drill-group h4 { margin: 0 0 8px; font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--cp-text-muted); }
  .drill-tiles { display: flex; flex-direction: column; gap: 8px; }
  .drill-tile { background: var(--cp-surface-soft); border: 1px solid var(--cp-border); border-radius: 8px; padding: 8px 12px; font-size: 12px; color: var(--cp-text-muted); }
  .drill-tile b { display: block; font-size: 17px; line-height: 1.3; color: var(--cp-chart-point); overflow-wrap: anywhere; }
  .drill-tile.warn { border-color: var(--cp-warning); }
  .drill-tile.warn b { color: var(--cp-warning); }
  .drill-chain select { max-width: 640px; padding: 4px 8px; border-radius: 0.5rem; border: 1px solid var(--cp-border); background: var(--cp-surface-soft); color: var(--cp-text); }
  .drill-status { font-size: 13px; color: var(--cp-text-muted); margin: 10px 0 4px; min-height: 18px; }
  .drill-status.warn { color: var(--cp-warning); }
  .drill-status.error { color: var(--cp-danger); }
  .drill-status.ok { color: var(--cp-success); }
  .drill-status code { color: var(--cp-text); }
  #drillTitle { font-size: 15px; margin: 18px 0 0; }
  #drillResults > .card, #drillResults > .grid { margin-bottom: 20px; }
  .drill-canvas { position: relative; height: 320px; }
  .drill-canvas.short { height: 230px; }
  .drill-canvas canvas { max-height: none; }
  .card.expanded .drill-canvas { flex: 1; height: auto; min-height: 0; }
  .drill-ok { color: var(--cp-success); font-weight: 700; }
  .drill-bad { color: var(--cp-danger); font-weight: 700; }
  .drill-grade { display: inline-block; min-width: 1.4em; text-align: center; border-radius: 6px; color: #111; font-weight: 700; }
  .drill-scroll { max-height: 420px; overflow: auto; }
  table.anomaly-table td.num, table.anomaly-table th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .drill-scroll button {
    padding: 2px 8px; border-radius: 0.5rem; border: 1px solid var(--cp-border);
    background: var(--cp-surface-soft); color: var(--cp-text); cursor: pointer; font-size: 12px;
  }
}
"""

DRILL_BODY = """<h2 class="section" id="drillSection">Run drill-down &mdash; frame-by-frame from the raw PresentMon CSV</h2>
<p class="muted">
Click any point in the charts above (or pick a run below) to open its raw PresentMon capture. The CSVs aren't
embedded in this report &mdash; connect your VPinPerfMon <b>logs</b> folder once and runs are matched by file
name, or open / drag-and-drop a single CSV. Files are read locally by your browser and never leave your PC
(Chrome may still say &ldquo;upload&rdquo; when you pick a folder). The run is then re-graded from scratch with
the same rules as VPinPerfMon and checked against the values stored in the database.
</p>
<div id="drillZone">
  <div class="drill-toolbar">
    <select id="drillRun" aria-label="Run to drill into"></select>
    <button type="button" id="drillFolderBtn">Connect logs folder&hellip;</button>
    <button type="button" id="drillReconnectBtn" hidden></button>
    <button type="button" id="drillFileBtn">Open CSV&hellip;</button>
    <span class="drill-count" id="drillCount"></span>
    <input type="file" id="drillFolderInput" webkitdirectory multiple hidden>
    <input type="file" id="drillFileInput" accept=".csv,.txt,text/csv,text/plain" multiple hidden>
  </div>
  <div id="drillLogsHint" class="drill-hint" hidden></div>
  <div id="drillStatus" class="drill-status">No run selected. Tip: you can also drop a PresentMon CSV (plus its .txt log, if you have it) anywhere in this section.</div>
  <div id="drillResults" hidden>
    <h3 id="drillTitle"></h3>
    <div id="drillChips" class="summary"></div>
    <div id="drillChainBar" class="drill-chain" hidden>
      <label>Swap chain <select id="drillChain"></select></label>
      <label><input type="checkbox" id="drillUnderlay" checked> Show other chains on the timeline (faint)</label>
      <span id="drillChainNote" class="muted"></span>
    </div>
    <div class="card">
      <div class="chart-header">
        <h2>Frame timeline &mdash; how long each frame stayed on screen</h2>
        <div class="chart-actions">
          <button type="button" class="drill-reset">Reset zoom</button>
          <button type="button" class="toggle-expand" data-chart="drillTimeline">Expand</button>
        </div>
      </div>
      <p class="chart-help">Mouse wheel to zoom &middot; drag to pan &middot; both timelines stay in sync</p>
      <div class="drill-canvas"><canvas id="drillTimeline"></canvas></div>
      <p class="chart-note">Dots are frames held for &ge;1.5 refreshes (amber = doubled, red = 3+); magenta marks a present-mode change and violet a user flag.</p>
      <details class="chart-more"><summary>More info</summary>
      <p class="chart-note">Dashed lines mark 1&times;/2&times;/3&times; the display refresh; dots are frames held for &ge;1.5 refreshes
      (amber = doubled, red = 3+ refreshes). Shaded: the warm-up and trimmed tail (not graded) and the worst
      cluster window. Magenta triangles/lines mark a present-mode change while graded (rare in steady play; usually an
      overlay, popup or window focus change). Frame time is when the app <i>finished</i> the frame &mdash; if it spikes with displayed time the app was late,
      if only displayed time spikes the delay happened after the app (compositor/display).</p></details>
    </div>
    <div class="card" id="drillSysCard" hidden>
      <div class="chart-header">
        <h2>System load during the run</h2>
        <div class="chart-actions">
          <button type="button" class="drill-reset">Reset zoom</button>
          <button type="button" class="toggle-expand" data-chart="drillSys">Expand</button>
        </div>
      </div>
      <p class="chart-help">Whole-machine CPU and GPU, one point per second &middot; stays in sync with the timelines above</p>
      <div class="drill-canvas short"><canvas id="drillSys"></canvas></div>
      <p class="chart-note" id="drillSysNote"></p>
      <details class="chart-more"><summary>More info</summary>
      <p class="chart-note">Recorded by VPinPerfMon (<code>--sysstats</code>) once a second, from Windows performance counters. Each point is the
      average of the second before it, so it can show that something else was busy around a stall but not what happened inside a single frame.
      CPU is the whole machine (all cores); busiest core shows a single saturated thread that the total hides. GPU is the busiest engine of
      the adapter. Red ticks are the same missed refreshes as above. The tool starts recording a few seconds before the first game frame, so the
      first seconds are not shown here.</p></details>
    </div>
    <div class="card" id="drillProcCard" hidden>
      <div class="chart-header">
        <h2>Processes during the run</h2>
        <div class="chart-actions">
          <select id="drillProcView">
            <option value="cpu">CPU, stacked</option>
            <option value="gpu">GPU, stacked</option>
            <option value="cpuLines">CPU, lines</option>
            <option value="mem">Memory</option>
            <option value="io">Disk / network I/O</option>
          </select>
          <button type="button" class="drill-reset">Reset zoom</button>
          <button type="button" class="toggle-expand" data-chart="drillProc">Expand</button>
        </div>
      </div>
      <p class="chart-help">The busiest processes recorded each second &middot; stays in sync with the charts above</p>
      <div class="drill-canvas short"><canvas id="drillProc"></canvas></div>
      <p class="chart-note" id="drillProcNote"></p>
    </div>
    <div class="card">
      <div class="chart-header">
        <h2>CPU vs GPU work per frame</h2>
        <div class="chart-actions">
          <button type="button" class="drill-reset">Reset zoom</button>
          <button type="button" class="toggle-expand" data-chart="drillBusy">Expand</button>
        </div>
      </div>
      <p class="chart-help">Red ticks are missed refreshes &mdash; check which line spikes at the same moment</p>
      <div class="drill-canvas short"><canvas id="drillBusy"></canvas></div>
      <p class="chart-note">Red ticks are missed refreshes. CPU busy can include the app's own limiter wait.</p>
      <details class="chart-more"><summary>More info</summary>
      <p class="chart-note">CPU busy is the frame time minus the time blocked in the present call, so it includes any time the app
      spends waiting or spinning in its own frame limiter. With frame pacing (common in BGFX VPX) the CPU line sits flat at the
      refresh interval whether the CPU is idle or flat out, so it does not show how much CPU headroom there is.</p></details>
    </div>
    <div class="grid">
      <div class="card">
        <div class="chart-header"><h2>How long frames stayed on screen (in refreshes)</h2></div>
        <p class="chart-help">Log scale &mdash; anything right of 1&times; is a visible hitch</p>
        <div class="drill-canvas short"><canvas id="drillHist"></canvas></div>
      </div>
      <div class="card">
        <div class="chart-header"><h2>Grade recheck vs database</h2></div>
        <div id="drillCheck" class="drill-scroll"></div>
      </div>
    </div>
    <div class="card">
      <div class="chart-header"><h2 id="drillStallsTitle">Stalls</h2></div>
      <div id="drillStalls" class="drill-scroll"></div>
      <p class="chart-note">&ldquo;Likely cause&rdquo; is a heuristic based on the stalled frame and the one after it.</p>
      <details class="chart-more"><summary>More info</summary>
      <p class="chart-note">Rules, first match wins: CPU or GPU busy for &ge;1.5 refreshes (GPU also needs the app to be late),
      a present-mode switch next to the stall, the app waiting (long frame time but not busy), otherwise
      the compositor/display (frame was ready on time but shown late). These are the same rules VPinPerfMon uses for blame.</p></details>
    </div>
    <div class="card" id="drillModes" hidden></div>
  </div>
</div>
"""

_here = os.path.dirname(os.path.abspath(globals().get("__file__") or "perf_report.py"))
with open(os.path.join(_here, "perf_drilldown.js"), encoding="utf-8") as _f:
    DRILL_JS = _f.read()

PERF_BODY = f"""<h1>Performance Stats</h1>
<p class="subtitle">Source: {SRC_LABEL} &middot; PresentMon-based captures</p>

<div class="summary">
  <div><b id="summaryRows"></b>Capture rows</div>
  <div><b id="summaryTables"></b>Unique tables</div>
  <div><b id="summaryMissingFps"></b>Rows missing FPS data</div>
  <div><b id="summaryReruns"></b>Recommended re-runs</div>
</div>

<div class="filter-bar">
  <div class="filter-row">
    <input id="startDate" type="date" aria-label="Start date" min="{MIN_ENTRY_DATE}" max="{MAX_ENTRY_DATE}" value="{MIN_ENTRY_DATE}">
    <label for="endDate">Through</label>
    <input id="endDate" type="date" min="{MIN_ENTRY_DATE}" max="{MAX_ENTRY_DATE}" value="{MAX_ENTRY_DATE}">
    <div class="perf-rangebar" id="perfRangeBar">
      <button type="button" data-range="1d">1 Day</button>
      <button type="button" data-range="1w">1 Week</button>
      <button type="button" data-range="1m">1 Month</button>
      <button type="button" data-range="1y">1 Year</button>
      <button type="button" data-range="all" class="active">All</button>
    </div>
    <label><input id="latestOnly" type="checkbox" checked> Latest run per table only</label>
  </div>
  <span id="dateFilterError" role="alert"></span>
  <div class="filter-row">
    <select id="ratingFilter" {"disabled" if not HAS_GAME_RATING else ""}>
      <option value="0">All ratings</option>
      <option value="1">1 or higher</option>
      <option value="2">2 or higher</option>
      <option value="3">3 or higher</option>
      <option value="4">4 or higher</option>
      <option value="5">5 only</option>
    </select>
    <select id="vrFilter" {"disabled" if not HAS_VR else ""}>
      <option value="all">Desktop + VR</option>
      <option value="desktop">Desktop only</option>
      <option value="vr">VR only</option>
    </select>
    {'<details id="emuFilter"><summary id="emuSummary">All emulators</summary><div id="emuPanel"><div class="emu-actions"><button type="button" id="emuAll">All</button><button type="button" id="emuNone">None</button></div><div id="emuList"></div></div></details>' if HAS_EMULATOR else ''}
    {GPU_FILTER_HTML}
    <details id="tblFilter"><summary id="tblSummary">All tables (no filter)</summary><div id="tblPanel"><div class="emu-actions"><input id="tblSearch" type="text" placeholder="Search tables..."><button type="button" id="tblShown">Select shown</button><button type="button" id="tblClear">Clear</button></div><div id="tblList"></div></div></details>
    <input id="searchBox" type="text" placeholder="...or type part of a table name to highlight">
  </div>
</div>
{"<p class='muted'>This CSV has no GameRating column. Re-run extract_perf_stats.py to enable the minimum-rating filter.</p>" if not HAS_GAME_RATING else ""}

<div class="grid">
{''.join(f'''  <div class="card">
    <div class="chart-header">
      <h2>{c["title"]}</h2>
      <div class="chart-actions">
        {f"<button type='button' class='focus-toggle' data-chart='{c['id']}' hidden>Show all</button>" if c.get("focus") else ""}
        <button type="button" class="reset-zoom" data-chart="{c["id"]}">Reset zoom</button>
        <button type="button" class="toggle-expand" data-chart="{c["id"]}">Expand</button>
      </div>
    </div>
    <p class="chart-help">{"Hover for details" if c.get("kind") == "gradeBar" else "Mouse wheel to zoom &middot; drag to pan"}</p>
    <canvas id="{c["id"]}"></canvas>
    {legend_html(c.get("legend"))}
    {chart_note_html(c)}
  </div>
''' for c in chart_defs)}
</div>

{DRILL_BODY}

<h2 class="section">Average Performance by Emulator</h2>
<p class="muted">
One bubble per emulator &mdash; all of that emulator's captures are averaged together. X axis is average
GPU utilization, Y axis is average CPU utilization. <b>Bubble size</b> is that emulator's average
Frame Time Std Dev &mdash; i.e. how "steady"/consistent its pacing is &mdash; so a bigger bubble means a
less steady emulator, independent of how many captures it has.
Hover a bubble for the emulator name, capture count, and averaged metrics.
</p>
{"<div class='card'><div class='chart-header'><h2>Emulator averages</h2><div class='chart-actions'><button type='button' class='reset-zoom' data-chart='emuChart'>Reset zoom</button><button type='button' class='toggle-expand' data-chart='emuChart'>Expand</button></div></div><p class='chart-help'>Mouse wheel to zoom &middot; drag to pan</p><canvas id='emuChart'></canvas><p id='emuEmpty' class='muted'></p></div>" if HAS_EMULATOR else "<p class='muted'>No EmuDisplay column found in this CSV -- re-run extract_perf_stats.py with the emulator join to populate this chart.</p>"}

<h2 class="section">Top 10 Best / Worst Performing Tables</h2>
<p class="muted">
Ranked by a composite <b>Performance Score</b> (0&ndash;100, higher is better). Each table's captures are
averaged, then the table is percentile-ranked against every other table on the metrics below, which are
blended using these weights (missing metrics are skipped and the remaining weights re-normalized):
{''.join(f"<br>&bull; <b>{w*100:.0f}%</b> {desc}" for _, _, w, desc in PERFORMANCE_SCORE_WEIGHTS)}
<br>Most of the weight is delegated to the capture tool's recommended perceived-smoothness score
(DisplayedTime), which already blends the frame-pacing metrics internally &mdash; so std dev and animation
error aren't scored again separately. Raw FPS is retained because that score self-normalizes to each
table's own frame rate and is otherwise blind to a table running smoothly but slow. Treat this as a
starting point for investigation, not an absolute verdict. A &#9888; marks a table that's also in the
Recommended Re-runs list below, since a flagged capture can pull a table's ranking in either direction.
</p>
<div class="perf-columns">
  <div>
    <h3 class="best">&#9650; Best performing</h3>
    <div id="top10Best"></div>
  </div>
  <div>
    <h3 class="worst">&#9660; Worst performing</h3>
    <div id="top10Worst"></div>
  </div>
</div>

<h2 class="section">&#9888; Recommended Re-runs (flagged on &ge;{RERUN_THRESHOLD} metrics)</h2>
<p class="muted">These captures are statistical outliers vs. the rest of this dataset on multiple independent
metrics simultaneously (e.g. abnormally low average FPS together with a very wide FPS min/max spread).
That pattern usually means a transient issue during capture (background load, thermal throttle, a stutter
mid-run, etc.) rather than a true steady-state result for the table. Re-capturing these is recommended
before drawing conclusions about the table itself.</p>
<div id="rerunCandidates"></div>

<h2 class="section">Watch list (flagged on exactly 1 metric)</h2>
<p class="muted">Single-metric outliers &mdash; worth a glance, but not automatically re-run candidates.</p>
<div id="watchList"></div>

<footer>Methodology: see PERF_STATS_REPORT_PROCESS.md in the VPinPerfMon repo (Reporting folder).</footer>
"""

PERF_SCRIPT = f"""(() => {{
const rawRecords = {json.dumps(all_records)};
rawRecords.forEach((row, i) => {{ row._i = i; }});
const anomalySpecs = {json.dumps(anomaly_specs)};
const performanceScoreWeights = {json.dumps(PERFORMANCE_SCORE_WEIGHTS)};
const chartDefs = {json.dumps(chart_defs)};
const charts = {{}};
const emuNames = [...new Set(rawRecords.map(row => row.EmuDisplay).filter(name => name != null))].sort();
const selectedEmus = new Set(emuNames);
const emuOk = row => row.EmuDisplay == null ? selectedEmus.size === emuNames.length : selectedEmus.has(row.EmuDisplay);
const gpuNameEl = document.getElementById('gpuNameFilter');
const gpuDriverEl = document.getElementById('gpuDriverFilter');
const gpuOk = row => (!gpuNameEl || !gpuNameEl.value || row.GpuName === gpuNameEl.value) &&
  (!gpuDriverEl || !gpuDriverEl.value || row.GpuDriverVersion === gpuDriverEl.value);
const drillGpuBands = {json.dumps(list(GPU_LOAD_BANDS))};
let currentState = null;
let chartData = {{}};
let emuChart = null;

const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const colors = {{
  accent: cssVar('--cp-accent'),
  danger: cssVar('--cp-danger'),
  warning: cssVar('--cp-warning'),
  success: cssVar('--cp-success'),
  link: cssVar('--cp-link'),
  text: cssVar('--cp-text'),
  muted: cssVar('--cp-text-muted'),
  border: cssVar('--cp-border'),
  chartPoint: cssVar('--cp-chart-point'),
}};

const emuColorByName = {json.dumps(EMU_COLOR_BY_NAME)};
const searchHighlightColor = {json.dumps(globals().get("SEARCH_HIGHLIGHT_COLOR"))} || colors.danger;
const emuPalette = [colors.chartPoint, colors.warning, colors.danger, colors.link, colors.success, colors.muted];
const emuColor = name => {{
  if (emuColorByName[name]) return emuColorByName[name];
  const i = emuNames.indexOf(name);
  return i < 0 ? colors.chartPoint : emuPalette[i % emuPalette.length];
}};

function finite(value) {{
  return typeof value === 'number' && Number.isFinite(value);
}}

const gradeColors = {json.dumps(GRADE_COLORS)};
const rendererColors = {json.dumps(RENDERER_COLORS)};
const gradeLetters = Object.keys(gradeColors);
const pointColor = (def, p) =>
  def.colorBy === 'grade' ? (gradeColors[p.grade] || colors.muted)
  : def.colorBy === 'renderer' ? (rendererColors[p.renderer] || colors.muted)
  : emuColor(p.emu);
// Points sitting exactly on a 0% or 100% bound (edgeMark charts) get a ring in the theme's text color
// (near-white on dark, near-black on light) so they stay visible against the axis edge.
const atBound = (def, p) => !!def.edgeMark && [p.x, p.y].some(v => finite(v) && (v <= 0.05 || v >= 99.95));
const pointBorder = (def, p) => atBound(def, p) ? colors.text : pointColor(def, p);
const pointBorderWidth = (def, p) => atBound(def, p) ? 2 : 1;
// Bubble sizes scale to the 95th percentile so one extreme run can't shrink everyone else to dots.
const sizeMax = (def, pts) => {{
  if (!def.sizeBy) return 0;
  const sizes = pts.map(p => p.size).filter(finite);
  const cap = quantile(sizes, 0.95);
  return cap > 0 ? cap : Math.max(0, ...sizes);
}};
const pointRadius = (def, p, max, base = 4) =>
  p.edge != null ? base + 2
  : def.sizeBy && max > 0 ? base - 1 + 9 * Math.sqrt(Math.min(finite(p.size) ? p.size : 0, max) / max) : base;

// Focused view: an axis stops at the largest value within 2x its 98th percentile; runs beyond are
// pinned just past that edge as outward-pointing triangles (real values kept in ox/oy).
const focusAll = {{}};
function focusLimit(values) {{
  const p98 = quantile(values, 0.98);
  if (!(p98 > 0) || !values.some(v => v > 2 * p98)) return null;
  return Math.max(...values.filter(v => finite(v) && v <= 2 * p98));
}}
function displayPoints(def, pts) {{
  const logY = def.yType === 'log';
  const positive = logY ? pts.map(p => p.y).filter(v => v > 0) : [];
  const floor = positive.length ? 10 ** Math.floor(Math.log10(Math.min(...positive))) : 0.001;
  const limX = def.focus ? focusLimit(pts.map(p => p.x)) : null;
  const limY = def.focus && !logY ? focusLimit(pts.map(p => p.y)) : null;
  const clamp = !focusAll[def.id];
  let off = 0;
  const out = pts.map(p => {{
    const offX = limX != null && p.x > limX;
    const offY = limY != null && p.y > limY;
    const low = logY && !(p.y > 0);
    if (offX || offY) off += 1;
    if (!low && !(clamp && (offX || offY))) return p;
    const cx = clamp && offX, cy = clamp && offY;
    return {{
      ...p, ox: p.x, oy: p.y,
      x: cx ? limX * 1.06 : p.x,
      y: low ? floor : cy ? limY * 1.06 : p.y,
      edge: cx && cy ? 45 : cx ? 90 : cy ? 0 : 180,
    }};
  }});
  return {{ pts: out, off }};
}}
const logTick = v => {{
  const m = v / 10 ** Math.floor(Math.log10(v) + 1e-9);
  return Math.abs(m - 1) < 1e-6 || Math.abs(m - 3) < 1e-6 ? String(+v.toPrecision(2)) : '';
}};
const formatTs = ms => new Date(ms).toISOString().replace('T', ' ').slice(0, 16);

function mean(rows, key) {{
  const values = rows.map(row => row[key]).filter(finite);
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}}

function quantile(values, q) {{
  const sorted = values.filter(finite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const position = (sorted.length - 1) * q;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
}}

function percentileRanks(rows, key, direction) {{
  const valid = rows
    .map((row, index) => ({{ row, index, value: row[key] }}))
    .filter(item => finite(item.value))
    .sort((a, b) => direction === 'high' ? a.value - b.value : b.value - a.value);
  const ranks = new Map();
  for (let i = 0; i < valid.length;) {{
    let end = i + 1;
    while (end < valid.length && valid[end].value === valid[i].value) end += 1;
    const averageRank = ((i + 1) + end) / 2;
    for (let j = i; j < end; j += 1) {{
      ranks.set(valid[j].index, averageRank / valid.length * 100);
    }}
    i = end;
  }}
  return ranks;
}}

function keepLatestRuns(rows) {{
  const latest = new Map();
  for (const row of rows) {{
    const key = row.GameID ?? row.Table;
    const prev = latest.get(key);
    if (!prev || row.EntryDateTime > prev.EntryDateTime) latest.set(key, row);
  }}
  return rows.filter(row => latest.get(row.GameID ?? row.Table) === row);
}}

function buildClientState(minRating, startDate, endDate, vrMode, latestOnly) {{
  let filtered = rawRecords.filter(row =>
    row.GameRating >= minRating &&
    row.EntryDateDate >= startDate &&
    row.EntryDateDate <= endDate &&
    emuOk(row) &&
    gpuOk(row) &&
    (vrMode === 'all' || (vrMode === 'vr') === (row.IsVR === 1))
  );
  if (latestOnly) filtered = keepLatestRuns(filtered);

  const stateChartData = Object.fromEntries(chartDefs.filter(def => def.x).map(def => [
    def.id,
    filtered
      .filter(row => finite(row[def.x]) && finite(row[def.y]))
      .map(row => ({{
        x: row[def.x],
        y: row[def.y],
        label: row.Table,
        rating: row.GameRating,
        emu: row.EmuDisplay,
        grade: row.StutterGrade,
        score: row.StutterGradeScore,
        capped: row.StutterGradeCapped,
        renderer: row.Renderer,
        legacy: row.BlameLegacy,
        size: def.sizeBy ? row[def.sizeBy] : null,
        date: row.EntryDateTime,
        rid: row._i,
      }}))
  ]));

  const gradedRows = filtered.filter(row => row.StutterGrade != null);
  const emuGroupNames = [...new Set(gradedRows.map(row => row.EmuDisplay ?? 'Unknown'))].sort();
  const gradeGroups = emuGroupNames.map(name =>
    [name, gradedRows.filter(row => (row.EmuDisplay ?? 'Unknown') === name)]);
  const gradeDist = {{
    categories: gradeGroups.map(([name, rows]) => `${{name}} (${{rows.length}} runs)`),
    counts: Object.fromEntries(gradeLetters.map(g => [g, gradeGroups.map(([, rows]) => rows.filter(r => r.StutterGrade === g).length)])),
    percents: Object.fromEntries(gradeLetters.map(g => [g, gradeGroups.map(([, rows]) =>
      rows.length ? rows.filter(r => r.StutterGrade === g).length / rows.length * 100 : 0)])),
    capped: gradeGroups.map(([, rows]) => rows.filter(r => r.StutterGradeCapped === 1).length),
  }};

  const bounds = Object.fromEntries(anomalySpecs.map(([column]) => {{
    const values = filtered.map(row => row[column]).filter(finite);
    const q1 = quantile(values, 0.25);
    const q3 = quantile(values, 0.75);
    const iqr = q1 == null || q3 == null ? null : q3 - q1;
    return [column, iqr == null ? [null, null] : [q1 - 1.5 * iqr, q3 + 1.5 * iqr]];
  }}));

  const anomalyRows = [];
  filtered.forEach(row => {{
    const reasons = [];
    anomalySpecs.forEach(([column, direction, description]) => {{
      const value = row[column];
      const [low, high] = bounds[column];
      if (!finite(value) || low == null || high == null) return;
      if (direction === 'low' && value < low) {{
        reasons.push(`${{column}}=${{value.toFixed(2)}} (< normal floor ${{low.toFixed(2)}}) — ${{description}}`);
      }} else if (direction === 'high' && value > high) {{
        reasons.push(`${{column}}=${{value.toFixed(2)}} (> normal ceiling ${{high.toFixed(2)}}) — ${{description}}`);
      }}
    }});
    if (reasons.length) {{
      anomalyRows.push({{
        Table: row.Table,
        EntryDate: row.EntryDate,
        FpsAverage: finite(row.FpsAverage) ? row.FpsAverage : null,
        score: reasons.length,
        reasons,
      }});
    }}
  }});
  anomalyRows.sort((a, b) => b.score - a.score);
  const rerunCandidates = anomalyRows.filter(row => row.score >= {RERUN_THRESHOLD});
  const watchList = anomalyRows.filter(row => row.score === 1);

  const tableGroups = new Map();
  filtered.forEach(row => {{
    if (!tableGroups.has(row.Table)) tableGroups.set(row.Table, []);
    tableGroups.get(row.Table).push(row);
  }});
  let rankedRows = Array.from(tableGroups, ([Table, rows]) => ({{
    Table,
    CaptureCount: rows.length,
    ...Object.fromEntries(performanceScoreWeights.map(([column]) => [column, mean(rows, column)])),
  }})).filter(row => finite(row.FpsAverage));

  const metricRanks = Object.fromEntries(performanceScoreWeights.map(([column, direction]) => [
    column, percentileRanks(rankedRows, column, direction),
  ]));
  rankedRows.forEach((row, index) => {{
    let total = 0;
    let totalWeight = 0;
    performanceScoreWeights.forEach(([column, , weight]) => {{
      const rank = metricRanks[column].get(index);
      if (finite(rank)) {{
        total += weight * rank;
        totalWeight += weight;
      }}
    }});
    row.PerformanceScore = totalWeight ? total / totalWeight : null;
  }});
  rankedRows = rankedRows
    .filter(row => finite(row.PerformanceScore))
    .sort((a, b) => b.PerformanceScore - a.PerformanceScore);

  const emulatorGroups = new Map();
  filtered.filter(row => finite(row.FpsAverage) && row.EmuDisplay != null).forEach(row => {{
    if (!emulatorGroups.has(row.EmuDisplay)) emulatorGroups.set(row.EmuDisplay, []);
    emulatorGroups.get(row.EmuDisplay).push(row);
  }});
  const emulatorRows = Array.from(emulatorGroups, ([label, rows]) => ({{
    label,
    rows,
    fps: mean(rows, 'FpsAverage'),
    x: mean(rows, 'GpuAverage'),
    y: mean(rows, 'CpuAverage'),
    frameTimeStdDev: mean(rows, 'FrameTimeStdDev'),
    animErrorRms: mean(rows, 'AnimationErrorRMS'),
  }})).filter(row => finite(row.x) && finite(row.y));
  const stdValues = emulatorRows.map(row => row.frameTimeStdDev).filter(finite);
  const minStd = stdValues.length ? Math.min(...stdValues) : null;
  const maxStd = stdValues.length ? Math.max(...stdValues) : null;
  const stdRange = minStd == null || maxStd == null ? 0 : maxStd - minStd;
  const emuChartData = emulatorRows.map(row => ({{
    x: row.x,
    y: row.y,
    r: !finite(row.frameTimeStdDev) || stdRange === 0
      ? 20
      : 8 + 34 * ((row.frameTimeStdDev - minStd) / stdRange),
    label: row.label,
    count: row.rows.length,
    fps: finite(row.fps) ? Number(row.fps.toFixed(1)) : null,
    frameTimeStdDev: finite(row.frameTimeStdDev) ? Number(row.frameTimeStdDev.toFixed(2)) : null,
    animErrorRms: finite(row.animErrorRms) ? Number(row.animErrorRms.toFixed(2)) : null,
  }}));

  return {{
    summary: {{
      rows: filtered.length,
      tables: new Set(filtered.map(row => row.Table)).size,
      missingFps: filtered.filter(row => !finite(row.FpsAverage)).length,
      reruns: rerunCandidates.length,
    }},
    chartData: stateChartData,
    gradeDist,
    emuChartData,
    top10Best: rankedRows.slice(0, 10),
    top10Worst: rankedRows.slice(-10).reverse(),
    rerunCandidates,
    watchList,
    rerunTables: Array.from(new Set(rerunCandidates.map(row => row.Table))).sort(),
    tables: Array.from(new Set(filtered.map(row => row.Table))).sort(),
    rows: filtered,
  }};
}}

currentState = buildClientState(0, '{MIN_ENTRY_DATE}', '{MAX_ENTRY_DATE}');
chartData = currentState.chartData;

function stutterGrade(score) {{
  if (score >= 90) return 'A - Excellent';
  if (score >= 80) return 'B - Good';
  if (score >= 70) return 'C - Fair';
  if (score >= 60) return 'D - Poor';
  return 'F - Severe';
}}

function makeGradeBar(def) {{
  const ctx = document.getElementById(def.id).getContext('2d');
  charts[def.id] = new Chart(ctx, {{
    type: 'bar',
    data: {{ labels: [], datasets: gradeLetters.map(g => ({{
      label: g, data: [], backgroundColor: gradeColors[g], borderColor: gradeColors[g],
    }})) }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ position: 'right', labels: {{ color: colors.text, boxWidth: 12 }} }},
        zoom: {{ pan: {{ enabled: false }}, zoom: {{ wheel: {{ enabled: false }}, pinch: {{ enabled: false }} }} }},
        tooltip: {{
          callbacks: {{
            label: item => {{
              const i = item.dataIndex;
              const g = item.dataset.label;
              return `${{g}}: ${{item.raw.toFixed(1)}}% (${{currentState.gradeDist.counts[g][i]}} runs)`;
            }},
            afterBody: items => {{
              const i = items[0].dataIndex;
              return `Capped at C: ${{currentState.gradeDist.capped[i]}}`;
            }},
          }},
        }},
      }},
      scales: {{
        x: {{ stacked: true, grid: {{ color: colors.border }}, ticks: {{ color: colors.muted }} }},
        y: {{ stacked: true, beginAtZero: true, max: 100, title: {{ display: true, text: '% of runs', color: colors.muted }},
              grid: {{ color: colors.border }}, ticks: {{ color: colors.muted, callback: v => v + '%' }} }},
      }},
    }},
  }});
}}

function renderGradeDist() {{
  const chart = charts.gradeDist;
  if (!chart) return;
  const dist = currentState.gradeDist;
  chart.data.labels = dist.categories;
  chart.data.datasets.forEach(ds => {{ ds.data = dist.percents[ds.label]; }});
  chart.update();
}}

// Draws horizontal reference lines (e.g. grade band edges) behind the tooltip layer.
const yLinesPlugin = {{
  id: 'yLines',
  afterDatasetsDraw(chart, args, opts) {{
    if (!opts || !opts.values) return;
    const {{ ctx, chartArea, scales }} = chart;
    ctx.save();
    ctx.strokeStyle = colors.muted;
    ctx.fillStyle = colors.muted;
    ctx.setLineDash([4, 4]);
    ctx.font = '10px sans-serif';
    opts.values.forEach(v => {{
      const y = scales.y.getPixelForValue(v);
      if (y < chartArea.top || y > chartArea.bottom) return;
      ctx.beginPath();
      ctx.moveTo(chartArea.left, y);
      ctx.lineTo(chartArea.right, y);
      ctx.stroke();
      ctx.fillText(`${{v}}%`, chartArea.right - 28, y - 3);
    }});
    ctx.restore();
  }},
}};

function makeChart(def) {{
  if (def.kind === 'gradeBar') return makeGradeBar(def);
  const ctx = document.getElementById(def.id).getContext('2d');
  const pts = displayPoints(def, chartData[def.id]).pts;
  const isStutterChart = def.id === 'stutterScores';
  const isTime = def.xType === 'time';
  const max = sizeMax(def, pts);
  const chart = new Chart(ctx, {{
    type: 'scatter',
    plugins: [yLinesPlugin],
    data: {{
      datasets: [{{
        label: def.title,
        data: pts,
        backgroundColor: pts.map(p => pointColor(def, p)),
        borderColor: pts.map(p => pointBorder(def, p)),
        borderWidth: pts.map(p => pointBorderWidth(def, p)),
        pointRadius: pts.map(p => pointRadius(def, p, max)),
        pointStyle: pts.map(p => p.edge != null ? 'triangle' : 'circle'),
        rotation: pts.map(p => p.edge ?? 0),
        pointHoverRadius: 6,
      }}]
    }},
    options: {{
      responsive: true,
      onClick: (event, elements, chart) => {{
        if (!elements.length || chart.isZoomingOrPanning?.()) return;
        const p = chart.data.datasets[elements[0].datasetIndex].data[elements[0].index];
        if (p && p.rid != null) drillOpenRecord(p.rid, true);
      }},
      onHover: (event, elements) => {{
        event.native.target.style.cursor = elements.length ? 'pointer' : '';
      }},
      plugins: {{
        legend: {{ display: false }},
        yLines: {{ values: def.yLines }},
        zoom: {{
          limits: {{
            x: {{ min: 'original', max: 'original' }},
            y: {{ min: 'original', max: 'original' }},
          }},
          pan: {{ enabled: true, mode: 'xy' }},
          zoom: {{
            wheel: {{ enabled: true, speed: 0.1 }},
            pinch: {{ enabled: true }},
            mode: 'xy',
          }}
        }},
        tooltip: {{
          callbacks: {{
            label: (item) => {{
              const p = item.raw;
              if (isStutterChart) return [
                `${{p.label}}`,
                `Emulator: ${{p.emu ?? 'Unknown'}}`,
                `Frame Time: ${{p.x.toFixed(1)}} (${{stutterGrade(p.x)}})`,
                `Displayed Time: ${{p.y.toFixed(1)}} (${{stutterGrade(p.y)}}) -- recommended`,
              ];
              const rx = p.ox ?? p.x, ry = p.oy ?? p.y;
              const fmtY = v => def.yType === 'log' && v > 0 && v < 0.01 ? v.toPrecision(2) : v.toFixed(2);
              const lines = [
                `${{p.label}} (${{p.emu ?? 'Unknown'}})`,
                `${{isTime ? formatTs(rx) : rx.toFixed(2)}}, ${{fmtY(ry)}}`,
              ];
              if (p.edge === 180) lines.push('No misses: shown on the bottom edge of the log scale');
              else if (p.edge != null) lines.push('Off-scale: pinned to the edge (Show all for its real position)');
              if (p.grade) lines.push(`Grade ${{p.grade}} (${{finite(p.score) ? p.score.toFixed(1) : '?'}})${{p.capped === 1 ? ' - capped at C' : ''}}`);
              if (def.colorBy && p.renderer) lines.push(`Renderer: ${{p.renderer}}`);
              if ((def.x === 'BlameCpuPct' || def.y === 'BlameCpuPct') && p.legacy === 1) lines.push('Legacy blame (older formula, not comparable)');
              if (def.sizeBy && finite(p.size)) lines.push(`Longest stall: ${{p.size.toFixed(1)}} ms`);
              return lines;
            }},
            footer: items => rawRecords[items[0]?.raw?.rid]?.RawFile ? 'Click for run drill-down' : '',
          }}
        }}
      }},
      scales: {{
        x: {{ title: {{ display: true, text: def.xlabel, color: colors.muted }}, grid: {{ color: colors.border }},
              ticks: {{ color: colors.muted, ...(isTime ? {{ callback: v => formatTs(v).slice(0, 10) }} : {{}}) }} }},
        y: {{ type: def.yType === 'log' ? 'logarithmic' : 'linear',
              title: {{ display: true, text: def.ylabel, color: colors.muted }}, grid: {{ color: colors.border }},
              ticks: {{ color: colors.muted, ...(def.yType === 'log' ? {{ callback: logTick, autoSkip: false }} : {{}}) }} }}
      }}
    }}
  }});
  charts[def.id] = chart;
}}

chartDefs.forEach(makeChart);

const emuCanvas = document.getElementById('emuChart');
function renderEmuChart(emuChartData) {{
  if (!emuCanvas) return;
  if (emuChart) emuChart.destroy();
  document.getElementById('emuEmpty').textContent =
    emuChartData.length ? '' : 'No emulator data is available for this rating threshold.';
  if (!emuChartData.length) return;
  emuChart = new Chart(emuCanvas.getContext('2d'), {{
    type: 'bubble',
    data: {{
      datasets: emuChartData.map((p, i) => ({{
        label: p.label,
        data: [p],
        backgroundColor: emuColor(p.label),
        borderColor: emuColor(p.label),      }}))
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ position: 'right', labels: {{ color: colors.text, boxWidth: 12, font: {{ size: 11 }} }} }},
        zoom: {{
          limits: {{
            x: {{ min: 'original', max: 'original' }},
            y: {{ min: 'original', max: 'original' }},
          }},
          pan: {{ enabled: true, mode: 'xy' }},
          zoom: {{
            wheel: {{ enabled: true, speed: 0.1 }},
            pinch: {{ enabled: true }},
            mode: 'xy',
          }}
        }},
        tooltip: {{
          callbacks: {{
            label: (item) => {{
              const p = item.raw;
              return [
                `${{p.label}} (${{p.count}} capture${{p.count === 1 ? '' : 's'}})`,
                `Avg CPU%: ${{p.y.toFixed(1)}}  Avg GPU%: ${{p.x.toFixed(1)}}  Avg FPS: ${{p.fps ?? 'n/a'}}`,
                `Frame time std dev (bubble size): ${{p.frameTimeStdDev ?? 'n/a'}}  Animation error RMS: ${{p.animErrorRms ?? 'n/a'}}`,
              ];
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ title: {{ display: true, text: 'Average GPU Utilization (%)', color: colors.muted }}, grid: {{ color: colors.border }}, ticks: {{ color: colors.muted }} }},
        y: {{ title: {{ display: true, text: 'Average CPU Utilization (%)', color: colors.muted }}, grid: {{ color: colors.border }}, ticks: {{ color: colors.muted }} }}
      }}
    }}
  }});
}}

const searchBox = document.getElementById('searchBox');
const selectedTables = new Set();
const tblFilter = document.getElementById('tblFilter');
const tblListEl = document.getElementById('tblList');
const tblSearch = document.getElementById('tblSearch');
const tblSummary = document.getElementById('tblSummary');
const ratingFilter = document.getElementById('ratingFilter');
const vrFilter = document.getElementById('vrFilter');
const latestOnlyCheckbox = document.getElementById('latestOnly');
const startDate = document.getElementById('startDate');
const endDate = document.getElementById('endDate');
const perfRangeBar = document.getElementById('perfRangeBar');
const dateFilterError = document.getElementById('dateFilterError');

function escapeHtml(value) {{
  const node = document.createElement('div');
  node.textContent = String(value);
  return node.innerHTML;
}}

function renderPerfList(targetId, rows) {{
  const target = document.getElementById(targetId);
  if (!rows.length) {{
    target.innerHTML = '<p class="muted">Not enough data to rank.</p>';
    return;
  }}
  const rerunTables = new Set(currentState.rerunTables);
  target.innerHTML = `<ol class="perf-list">${{rows.map((row, index) => `
    <li><span class="rank">${{index + 1}}.</span>
    <span class="perf-table">${{escapeHtml(row.Table)}}</span>
    ${{rerunTables.has(row.Table) ? '<span class="score-badge" title="Also flagged as a recommended re-run">&#9888;</span>' : ''}}
    <br><span class="muted">score ${{row.PerformanceScore.toFixed(1)}}/100
    &middot; FPS avg ${{row.FpsAverage.toFixed(1)}}
    &middot; ${{row.CaptureCount}} capture(s)</span></li>`).join('')}}</ol>`;
}}

function renderAnomalyTable(targetId, rows, emptyMessage) {{
  const target = document.getElementById(targetId);
  if (!rows.length) {{
    target.innerHTML = `<p class="muted">${{emptyMessage}}</p>`;
    return;
  }}
  target.innerHTML = `<table class="anomaly-table">
    <thead><tr><th>Table</th><th>Entry Date</th><th>FPS Avg</th><th>Flags</th><th>Reason(s)</th></tr></thead>
    <tbody>${{rows.map(row => `<tr>
      <td>${{escapeHtml(row.Table)}}</td>
      <td>${{escapeHtml(row.EntryDate)}}</td>
      <td>${{row.FpsAverage == null ? 'n/a' : row.FpsAverage.toFixed(1)}}</td>
      <td>${{row.score}}</td>
      <td>${{row.reasons.map(escapeHtml).join('<br>')}}</td>
    </tr>`).join('')}}</tbody>
  </table>`;
}}

function syncTableSummary() {{
  const n = selectedTables.size;
  tblSummary.textContent = n === 0 ? 'All tables (no filter)' : n === 1 ? [...selectedTables][0] : `${{n}} tables selected`;
}}

function filterTableList() {{
  const q = tblSearch.value.trim().toLowerCase();
  tblListEl.querySelectorAll('label').forEach(l => {{ l.style.display = !q || l.dataset.name.includes(q) ? '' : 'none'; }});
}}

function updateTableOptions() {{
  for (const t of [...selectedTables]) if (!currentState.tables.includes(t)) selectedTables.delete(t);
  tblListEl.innerHTML = currentState.tables.map((table, i) =>
    `<label data-name="${{escapeHtml(table.toLowerCase())}}"><input type="checkbox" data-i="${{i}}"${{selectedTables.has(table) ? ' checked' : ''}}> ${{escapeHtml(table)}}</label>`
  ).join('');
  filterTableList();
  syncTableSummary();
}}

function renderState() {{
  const summary = currentState.summary;
  document.getElementById('summaryRows').textContent = summary.rows;
  document.getElementById('summaryTables').textContent = summary.tables;
  document.getElementById('summaryMissingFps').textContent = summary.missingFps;
  document.getElementById('summaryReruns').textContent = summary.reruns;
  renderEmuChart(currentState.emuChartData);
  renderGradeDist();
  renderPerfList('top10Best', currentState.top10Best);
  renderPerfList('top10Worst', currentState.top10Worst);
  renderAnomalyTable(
    'rerunCandidates',
    currentState.rerunCandidates,
    'No rows crossed the multi-metric re-run threshold for this rating selection.'
  );
  renderAnomalyTable(
    'watchList',
    currentState.watchList,
    'No single-metric outliers for this rating selection.'
  );
}}

function applyView() {{
  const selected = selectedTables;
  const q = searchBox.value.trim().toLowerCase();

  chartDefs.filter(def => def.x).forEach(def => {{
    const chart = charts[def.id];
    const view = displayPoints(def, chartData[def.id]);
    let allPts = view.pts;
    const ds = chart.data.datasets[0];
    const max = sizeMax(def, allPts);
    // Big bubbles first so small ones stay visible on top.
    if (def.sizeBy) allPts = [...allPts].sort((a, b) => (finite(b.size) ? b.size : 0) - (finite(a.size) ? a.size : 0));
    const toggle = document.querySelector(`.focus-toggle[data-chart="${{def.id}}"]`);
    if (toggle) {{
      toggle.hidden = !view.off;
      toggle.textContent = focusAll[def.id] ? 'Focus' : `Show all (${{view.off}} off-scale)`;
    }}

    if (selected.size) {{
      // Dropdown filter: show only the selected table's points.
      const pts = allPts.filter(p => selected.has(p.label));
      ds.data = pts;
      ds.pointBackgroundColor = pts.map(p => pointColor(def, p));
      ds.pointBorderColor = colors.warning;
      ds.pointBorderWidth = 2;
      ds.pointRadius = pts.map(p => pointRadius(def, p, max, 6));
    }} else {{
      // Text search: keep the base fill, add a red outline, and draw matches last (on top).
      const isMatch = p => q && p.label.toLowerCase().includes(q);
      const pts = q ? [...allPts.filter(p => !isMatch(p)), ...allPts.filter(isMatch)] : allPts;
      ds.data = pts;
      ds.pointBackgroundColor = pts.map(p => pointColor(def, p));
      ds.pointBorderColor = pts.map(p => isMatch(p) ? searchHighlightColor : pointBorder(def, p));
      ds.pointBorderWidth = pts.map(p => isMatch(p) ? 2.5 : pointBorderWidth(def, p));
      ds.pointRadius = pts.map(p => pointRadius(def, p, max, isMatch(p) ? 7 : 4));
    }}
    ds.pointStyle = ds.data.map(p => p.edge != null ? 'triangle' : 'circle');
    ds.rotation = ds.data.map(p => p.edge ?? 0);
    chart.update();
  }});
}}

function setTableSelection(fn) {{
  fn();
  tblListEl.querySelectorAll('input').forEach(b => {{ b.checked = selectedTables.has(currentState.tables[b.dataset.i]); }});
  if (selectedTables.size) searchBox.value = '';
  syncTableSummary();
  applyView();
}}
tblListEl.addEventListener('change', e => setTableSelection(() => {{
  const name = currentState.tables[e.target.dataset.i];
  if (e.target.checked) selectedTables.add(name); else selectedTables.delete(name);
}}));
tblSearch.addEventListener('input', filterTableList);
document.getElementById('tblClear').addEventListener('click', () => setTableSelection(() => selectedTables.clear()));
document.getElementById('tblShown').addEventListener('click', () => setTableSelection(() => {{
  tblListEl.querySelectorAll('label').forEach(l => {{
    if (l.style.display !== 'none') selectedTables.add(currentState.tables[l.querySelector('input').dataset.i]);
  }});
}}));
document.addEventListener('click', event => {{ if (!tblFilter.contains(event.target)) tblFilter.open = false; }});

searchBox.addEventListener('input', () => {{
  if (searchBox.value.trim()) {{ selectedTables.clear(); updateTableOptions(); }}
  applyView();
}});

function applyReportFilters() {{
  if (startDate.value > endDate.value) {{
    const message = 'Beginning date must be on or before the ending date.';
    dateFilterError.textContent = message;
    startDate.setCustomValidity(message);
    endDate.setCustomValidity(message);
    return;
  }}
  dateFilterError.textContent = '';
  startDate.setCustomValidity('');
  endDate.setCustomValidity('');
  currentState = buildClientState(
    Number(ratingFilter.value),
    startDate.value,
    endDate.value,
    vrFilter.value,
    latestOnlyCheckbox.checked
  );
  chartData = currentState.chartData;
  updateTableOptions();
  renderState();
  applyView();
  drillRefreshRuns();
}}

ratingFilter.addEventListener('change', applyReportFilters);
const emuFilter = document.getElementById('emuFilter');
if (emuFilter) {{
  const emuListEl = document.getElementById('emuList');
  emuListEl.innerHTML = emuNames.map((name, i) =>
    `<label><input type="checkbox" data-emu="${{i}}" checked> ${{escapeHtml(name)}}</label>`).join('');
  const syncEmus = () => {{
    emuListEl.querySelectorAll('input').forEach(box => {{ box.checked = selectedEmus.has(emuNames[box.dataset.emu]); }});
    const n = selectedEmus.size;
    document.getElementById('emuSummary').textContent =
      n === emuNames.length ? 'All emulators' : n === 0 ? 'No emulators' : n === 1 ? [...selectedEmus][0] : `${{n}} of ${{emuNames.length}} emulators`;
    applyReportFilters();
  }};
  emuListEl.addEventListener('change', event => {{
    const name = emuNames[event.target.dataset.emu];
    if (event.target.checked) selectedEmus.add(name); else selectedEmus.delete(name);
    syncEmus();
  }});
  document.getElementById('emuAll').addEventListener('click', () => {{ emuNames.forEach(n => selectedEmus.add(n)); syncEmus(); }});
  document.getElementById('emuNone').addEventListener('click', () => {{ selectedEmus.clear(); syncEmus(); }});
  document.addEventListener('click', event => {{ if (!emuFilter.contains(event.target)) emuFilter.open = false; }});
}}
vrFilter.addEventListener('change', applyReportFilters);
if (gpuNameEl) gpuNameEl.addEventListener('change', applyReportFilters);
if (gpuDriverEl) gpuDriverEl.addEventListener('change', applyReportFilters);
latestOnlyCheckbox.addEventListener('change', applyReportFilters);
startDate.addEventListener('change', applyReportFilters);
endDate.addEventListener('change', applyReportFilters);
function rangeStart(key) {{
  const minDate = '{MIN_ENTRY_DATE}';
  if (key === 'all') return minDate;
  const d = new Date('{MAX_ENTRY_DATE}T00:00:00Z');
  if (key === '1d') d.setUTCDate(d.getUTCDate() - 1);
  else if (key === '1w') d.setUTCDate(d.getUTCDate() - 7);
  else if (key === '1m') d.setUTCMonth(d.getUTCMonth() - 1);
  else if (key === '1y') d.setUTCFullYear(d.getUTCFullYear() - 1);
  const iso = d.toISOString().slice(0, 10);
  return iso < minDate ? minDate : iso;
}}
function syncRangeButtons(preferred) {{
  const buttons = [...perfRangeBar.querySelectorAll('button')];
  const matches = buttons.filter(btn =>
    endDate.value === '{MAX_ENTRY_DATE}' && startDate.value === rangeStart(btn.dataset.range));
  const chosen = matches.find(btn => btn.dataset.range === preferred) ||
    matches.find(btn => btn.dataset.range === 'all') || matches[0];
  buttons.forEach(btn => btn.classList.toggle('active', btn === chosen));
}}
perfRangeBar.addEventListener('click', event => {{
  const btn = event.target.closest('button');
  if (!btn) return;
  startDate.value = rangeStart(btn.dataset.range);
  endDate.value = '{MAX_ENTRY_DATE}';
  syncRangeButtons(btn.dataset.range);
  applyReportFilters();
}});
startDate.addEventListener('change', () => syncRangeButtons());
endDate.addEventListener('change', () => syncRangeButtons());
syncRangeButtons();

function chartForId(id) {{
  return Chart.getChart(document.getElementById(id));
}}

function closeExpandedChart() {{
  const expanded = document.querySelector('.card.expanded');
  if (!expanded) return;
  expanded.classList.remove('expanded');
  document.body.classList.remove('chart-expanded');
  const button = expanded.querySelector('.toggle-expand');
  button.textContent = 'Expand';
  chartForId(button.dataset.chart)?.resize();
}}

document.querySelectorAll('.focus-toggle').forEach(button => {{
  button.addEventListener('click', () => {{
    focusAll[button.dataset.chart] = !focusAll[button.dataset.chart];
    chartForId(button.dataset.chart)?.resetZoom('none');
    applyView();
  }});
}});

document.querySelectorAll('.reset-zoom').forEach(button => {{
  button.addEventListener('click', () => chartForId(button.dataset.chart)?.resetZoom());
}});

document.querySelectorAll('.toggle-expand').forEach(button => {{
  button.addEventListener('click', () => {{
    const card = button.closest('.card');
    if (card.classList.contains('expanded')) {{
      closeExpandedChart();
      return;
    }}
    closeExpandedChart();
    card.classList.add('expanded');
    document.body.classList.add('chart-expanded');
    button.textContent = 'Close';
    requestAnimationFrame(() => chartForId(button.dataset.chart)?.resize());
  }});
}});

document.addEventListener('keydown', event => {{
  if (event.key === 'Escape') closeExpandedChart();
}});

const drillCfgs = {json.dumps(DRILL_CFGS)};
const drillLogs = {json.dumps(DRILL_LOGS).replace("</", "<\\/")};
/*__DRILLDOWN_JS__*/
applyReportFilters();
}})();
""".replace("/*__DRILLDOWN_JS__*/", DRILL_JS)

REPORT_INFO = f"{len(data)} rows, {initial_state['summary']['reruns']} recommended re-runs"
