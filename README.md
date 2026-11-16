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

the playfield is the chain with the highest GPU load when several tie on frame count.

## Purging runs

A run can be spurious (a test, a screen recording, a bad capture). Purging is a soft delete: the `CustomPerfStats` row is flagged `Deleted = 1` with a UTC time in `DeletedUtc`, and the report skips it. Nothing is removed from the database or the logs folder, and a run can be restored by setting `Deleted = 0, DeletedUtc = NULL`. It needs VPinPerfMon migration `008_add_soft_delete.sql`; a database without it still builds the report, but cannot purge.

The report is a static page and is often viewed on a different PC from the cab, so purging takes two steps.

1. **Mark runs in the report (any PC).** On the Performance Stats tab, open a run in the **Run drill-down** and click **Mark for purge**. Marked runs disappear from the report straight away, and the **Pending purge** tile counts them (its **review** link jumps to the list). The list is kept in the browser (`localStorage`), so it survives a reload or a rebuilt report, but it is per browser and per file location. In the **Purge list** you can **Unmark** a run, **Clear list**, **Copy JSON**, or **Export purge file**, which downloads `purge_<timestamp>.json`:

   ```json
   { "version": 1, "createdAt": "2026-11-16T16:16:34.021Z",
     "entries": [ { "id": 1615, "table": "Breaking Bad (Original 2026)", "entryDateTime": "2026-11-15 21:42:33",
                    "grade": "F", "fileAssetStem": "20261115164007_Breaking Bad (Original 2026)" } ] }
   ```

   `id` is `CustomPerfStats.Id`. `entryDateTime` and `fileAssetStem` are checked against the row. If the report was built from a `--perf-csv` that lacks `RunId`, `id` is `null` and that entry cannot be applied.

2. **Apply on the cab, then rebuild.** Copy the file to the cab, run `purge_runs.py`, then rebuild the report; the purged runs are gone from the next build.

```cmd
python purge_runs.py "p:\somefolder\somefiles\purge*.json"           (dry run: shows what would happen)
python purge_runs.py "p:\somefolder\somefiles\purge*.json" --apply
python purge_runs.py --apply --backup --delete-manifests
```

| Argument | Purpose |
|---|---|
| `PATH ...` | A manifest file, a folder (its `purge*.json` files), or a wildcard pattern. Quote wildcards. Default: `purge*.json` in the current folder. |
| `--db FILE` | Database to update. Default: `DB_PATH` in `report_config.py`. |
| `--apply` | Make the changes. Without it the script only reports. |
| `--backup [DIR]` | Opt-in. Before applying, copy the database to `<name>.<timestamp>.bak` next to it, or in `DIR`. Only done when there is something to purge. |
| `--delete-manifests` | Opt-in. After `--apply`, delete each manifest whose entries were all handled. |

Each entry is checked before anything changes: the `id` must exist, and `entryDateTime` and `fileAssetStem` must match that row. A mismatch is skipped and reported, never purged. Entries already purged, or whose row no longer exists, count as handled, so running it twice is safe. A manifest with a skipped entry, or one that cannot be read, is kept even with `--delete-manifests`. Exit code: `0` all handled, `1` something was skipped, `2` could not run (for example, migration 008 missing). The script is non-interactive, so it can go in an existing `.bat` on the cab, for example `python purge_runs.py D:\drop --apply --delete-manifests` before the report build.

The Play History tab reads `CustomGameLog` and is not affected.
