# VPin Reports

Builds `VpinStatsReport.html`, a single self-contained HTML page with these tabs:
- **Play History** (default): from `history_report.py`.
- **Performance Stats**: adapted from the [VPinPerfMon](https://github.com/bhitney/VPinPerfMon) scatter report.
- **AI Recommendations**: shown only when the Vpin-AI-Recommender's `ai_recommendations.json` is found.

Inputs: the PinUP Popper database (`CustomGameLog`, `CustomPerfStats`, `Games`, `Emulators`), VPinPerfMon's logs folder for the drill-down, and optionally `ai_recommendations.json`. See `DATABASE_COLUMNS.md`.

Easiest: double-click or run `BuildReport.bat`. It creates `.venv` in this folder if needed, installs `pandas` and `Pillow` (`requirements.txt`), builds the report, and opens it. Any arguments are passed through (e.g. `BuildReport.bat --days 180`).

Manual equivalent, from this folder with the venv active:

```cmd
pip install -r requirements.txt
python build_report.py
```

## Configuration

Edit `report_config.py`:

| Setting | Purpose |
|---|---|
| `DB_PATH` | Database used by all tabs |
| `TARGET_EMU_IDS` | Emulator IDs for History and Performance tabs (`"4,6,10"`) |
| `HISTORY_DAYS` | Play History window |
| `RECS_PATH` | `ai_recommendations.json`; defaults to a sibling `..\Vpin-AI-Recommender` checkout |
| `PERF_LOGS_DIR` | Logs path or list of paths: first is primary, others are alternate locations to copy on other PCs. `None` or `[]` uses `FileRawDataPath`. Repeat `--perf-logs` to override with one or more paths. |
| `PERF_CSV` | Optional pre-extracted CustomPerfStats CSV (bypasses the DB and emulator filter for the perf tab) |

Command-line options override the config.

## How it works

- One document, one shared theme/stylesheet (dark). Each tab's script runs in its own scope so names can't collide; panel-specific CSS is scoped with native CSS nesting (modern Edge/Chrome/Firefox/Safari).
- History: reuses the page template from `history_report.py` (body markup + script) with its data built by that module. It started as `Vpin_HistoryReport.py` in the Vpin-AI-Recommender repo and can still build a standalone `HistoryReport.html` (`python history_report.py [days]`).
- Perf: `perf_report.py` is a fragment-producing adaptation of VPinPerfMon's `generate_scatter_report.py`; `extract_perf_stats.py` supplies the query (filtered by emulator here). Methodology: `PERF_STATS_REPORT_PROCESS.md` in the VPinPerfMon repo.
- Outlier-prone perf charts (miss breakdown, burstiness, miss rates, long gaps) open in a focused view: an axis ends at the largest value within 2× its 98th percentile, and runs beyond it are pinned to the edge as ▶/▲ markers (hover shows real values; **Show all** restores the full range). Miss Rate uses a log y axis with zero-miss runs as ▼ on the bottom edge, and bubble sizes are capped at the 95th percentile.
- If a section has no data, its tab shows a message and the other still renders. Charts need internet access for Chart.js (CDN).

## Run drill-down (Performance Stats tab)

Raw PresentMon CSVs are too large to embed, so the report reads them on demand from your PC (nothing is uploaded):

1. Click any point in a perf chart, or pick a run in the **Run drill-down** section.
2. Click **Connect logs folder…** and choose the VPinPerfMon logs folder (runs are matched by the file name in `FileRawDataPath`; ● marks runs that were found), or use **Open CSV…** / drag-and-drop a single CSV. In Edge/Chrome the folder is remembered, so a regenerated report offers **Reconnect** next time.
   Browsers can't open a local path on their own, so the section shows the **Expected logs folder** and any **Alternate logs folder** entries, each with a **Copy path** button. Copy the path appropriate for this PC and paste it into the folder picker's address bar. Set `PERF_LOGS_DIR` to a single path or a list such as `[r"C:\vPinball\mytools\VPinPerfMon\logs", r"\\cabinet\VPinPerfMon\logs"]`. The first path is primary; additional paths are convenience shortcuts only and need not be accessible from the build PC. Alternatively, use `build_report.py --perf-logs "<primary>" --perf-logs "<alternate>"` (replaces the configured list). Without configured paths, the report uses the most common folder in `FileRawDataPath`. At build time only the primary folder is checked: ○ marks those runs until a folder is connected, and a missing file shows its full expected primary path. Copying a path does not connect a folder; use **Connect logs folder…** to grant browser access.
and a recheck of every grade value against the database. If the run's `SysStats_`, `SysProcs_` and `SysEvents_` files are in the same folder (VPinPerfMon's system sampler, QPC captures only), synced system-load and per-process charts and the busiest processes for each stall are added; see `CHANGES.md`.

The grade is recomputed in `perf_drilldown.js` with the run's stored `Cfg*` settings. The warm-up (`--delaystart`) varies by launcher/emulator; it is taken from, in order:

1. `CustomPerfStats.CfgTrimStartSeconds` (the start-of-run counterpart of `CfgTrimEndSeconds`). Older databases without the column still work; the report also picks up an optional `CfgTimeoutSec`, and both are selected only when the columns exist.
2. The run's VPinPerfMon console log — the `.txt` with the same name as the CSV (`Warm-up threshold` / `begin after N second delay`), when it is in the connected folder or opened/dropped with the CSV.
3. Inference: the whole number of seconds whose post-warm-up frame count equals the stored `PresentMonFrameCount` (the frame-count recheck is then not an independent check).
4. 30 s.

The recheck follows VPinPerfMon's `StutterGrading`:
- Frames with no `DisplayedTime` count as missing.
- The cool-down trim removes the last `CfgTrimEndSeconds` of displayed frames, measured by summed on-screen time.
- The refresh period is the upper median of `DisplayedTime`.
- Values are parsed as 32-bit floats, so it matches the database exactly.

The source used is shown under the recheck table. A CSV that doesn't match any run in the report is analyzed without the database comparison.

### Stall causes, blame and GPU load

Each stall (a graded frame held for 1.5 refreshes or more) is classified as CPU, GPU, present-mode switch, app waiting or display/compositor by the same rules as VPinPerfMon (BlameVersion 4); see `DATABASE_COLUMNS.md` for the rules. The recheck compares the five stall counts and the blame values with the database and shows a warning if they differ. The drill-down leads with **GPU load** (median GPU busy as a percent of the refresh, banded by `GPU_LOAD_BANDS` in `report_config.py`), then shows blame with the stall counts behind it. Blame is empty when there are fewer than `CfgBlameMinStalls` CPU+GPU stalls, which is most runs. A derived limiter call (GPU-limited, CPU-limited, neither, unclear) needs both the load and the stall causes. Databases that have not been migrated lack these columns; the report still builds and simply omits them, and rows graded with an older blame formula are labelled legacy.

The drill-down can also switch between swap chains, mark present-mode changes, and flag a possible screen-recording session; the playfield is the chain with the highest GPU load when several tie on frame count.
