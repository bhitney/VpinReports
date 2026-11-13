# VpinReports change notes

## Run drill-down: QPC captures, system sampler and processes

These changes make the drill-down read the newer VPinPerfMon captures. Everything new is optional: a run without the sampler files, and a run recorded with `--timebase relative`, behaves as before.

### Reads both PresentMon time columns

- VPinPerfMon now records `CPUStartQPCTime` (absolute QPC milliseconds) by default; `--timebase relative` records `CPUStartTime` as before. The drill-down accepts either. The header decides, not the database `TimeBase` column.
- All analysis uses differences from the first frame, so the large absolute QPC values do not matter. The warm-up cut-off from the console log is converted to the same base.

### System load lane

- When `SysStats_<stem>.csv` is in the connected logs folder, a **System load during the run** card shows whole-machine CPU, busiest core and GPU (0 to 100%) under the frame timeline. Zoom, pan and reset are shared with the timelines.
- It is shown only for QPC runs, because a relative-time CSV has no clock in common with the sampler. Otherwise the card stays hidden.
- Each sampler point covers the second before its stamp. The note under the chart says so.

### Processes

- **Stall table:** a "Busiest processes" column lists up to three processes from the sampler second covering each stall (for example `PinUpDisplayX CPU 4% GPU 6%`).
- **System-load tooltip:** hovering a point lists the busiest processes for that second.
- **Processes during the run** card, with a view selector: CPU stacked, GPU stacked, CPU lines, memory (working set) and disk/network I/O. The stacked views end with a grey band, "Game and everything unlisted": the machine total minus every listed process. It includes the game, which VPinPerfMon leaves out, and all the small processes. It is not a measurement of the game.
- The sampler records only the top few processes each second, so a process that was never in the top few has no data, and a process can be missing for a second or two. Processes outside the top seven are grouped.

### Frame-time jitter tile

- A tile in the Smoothness group: the standard deviation of frame time (the gap between Present calls) for graded frames, with stalls excluded, for example `±0.16 ms`.
- The caption says whether frame times zigzag (a late frame followed by an early one, from a frame limiter or vsync wait) and whether on-screen time stays steady. Steady on-screen time means smooth presentation even when frame time wobbles.
- The tile turns amber only when the on-screen time itself varies. It is informational and does not change the grade.

### Fixes

- The **Swap chain** selector is hidden when a capture has a single swap chain. Before, an empty selector stayed visible because the `hidden` attribute lost to the `display: flex` rule.

### Tests

- `tests/drilldown.test.js`: 32 tests. New ones cover QPC and relative parsing, warm-up conversion, sampler file parsing (quoted names, cut-off rows, old headers), mapping a frame time to a sampler second, process lookup and the jitter tile.
- The Python tests are unchanged.

### Documentation

- `DATABASE_COLUMNS.md` is identical in this repository and in VPinPerfMon (`Reporting/DATABASE_COLUMNS.md`). It now documents `CaptureAnchorUtc`, `CaptureAnchorQpcMs`, `TimeBase`, `FileAssetStem`, the warm-up and tail trim, and the blame and stall columns together. Rows before migrations 006 and 007 have `NULL` in the new columns.
