# CustomPerfStats: column reference for reports and dashboards

One row is written per capture (one table, one play session, one `VPinPerfMon.exe` run). The table lives in the PinUP Popper SQLite database (`PUPDatabase.db`). This file describes what each column means and how it is computed, so reports can be built without reading the capture tool's source.

## How to read the data

- **One row = one run.** There is no per-frame data in the database. Every statistic is a summary of a single capture, which is typically about 5 minutes after a 30 second warm-up (the launch script sets the length, so it can vary).
- **Time units are milliseconds** (ms) unless a column says otherwise. Percent columns are 0-100, not 0-1.
- **`EntryDate` is UTC** (SQLite `CURRENT_TIMESTAMP`). Convert to local time before charting by time of day.
- **Join to `Games` on `GameId`** for the table name and rating. `Games.GameDisplay` is the display name, `Games.GameRating` is a text rating, and `Games.EMUID` joins to `Emulators.EmuDisplay` for the emulator. `SourceFile` is only a fallback label and may be empty or differ from the game name.
- **Many runs per game are normal.** Runs differ by renderer (VPX 10.8 vs BGFX), display settings, sync mode and hardware. The database does not record sync mode (VSync, G-Sync, frame pacing) or the build, so group by `GameId` and `EntryDate` with care, and expect that two runs of the same table may not be comparable.
- **NULL is meaningful.** A statistic group is `NULL` when its source data was missing (for example no NVIDIA GPU, no frames captured, or PresentMon did not provide that column). Charts should drop or grey out `NULL`, not treat it as 0.
- **Rows are not all graded.** Older rows have `NULL` in every grade column until they are regraded. Filter with `StutterGradeVersion IS NOT NULL`.
- **Two stutter measures exist.** The A-F grade (`StutterGrade*`) is the recommended one. `StutterScoreFT` and `StutterScoreDT` are the older weighted scores. Do not mix them on one axis (see the sections below).

## Identity and capture context

| Columns | Meaning |
|---|---|
| `Id` | Row id (autoincrement). |
| `GameId` | Game identifier, matches `Games.GameId`. `0` means none was supplied. |
| `IsVR` | `1` for a VR capture, `0` for desktop. VR and desktop runs should normally be charted separately. |
| `SourceFile` | Table file name given to the tool (for example `Game of Thrones LE (Stern 2015).vpx`). Fallback for matching when `GameId` is unknown. |
| `EntryDate` | When the row was written, in UTC. |
| `SampleCount` | Number of CPU/GPU samples taken, about one per second after warm-up. |
| `PresentMonFrameCount` | Number of rendered frames analysed from PresentMon, after the warm-up period and for the selected swap chain only. Use this as the run's frame count. It includes the last few seconds that the grade excludes, so it is slightly larger than `GradeFrames`. |
| `SwapChainAddress` | Address of the swap chain that was analysed (the playfield). Informational. |
| `SwapChainCount` | How many swap chains were present in the capture. More than 1 means the tool had to choose (the one with the most frames). Values above 1 mark captures where the selection mattered. |
| `PresentApplication` | The executable that owns the analysed swap chain, for example `VPinballX64.exe` (VPX 10.8) or `VPinballX_BGFX64.exe` (BGFX). Useful for splitting renderers. |
| `PresentMode` | How frames reach the display, for example `Hardware: Independent Flip`, `Hardware Composed: Independent Flip` or `Composed: Flip`. Independent flip is the lowest-overhead mode. The most common value for the chain is stored. |
| `FileAnalysisPath` | Absolute path to the text log of the run. May no longer exist if logs were cleaned up. |
| `FileRawDataPath` | Absolute path to the PresentMon CSV of the run. `NULL` when the CSV was deleted at run time. May no longer exist. |

## Stutter grade (recommended measure)

The grade counts **missed refreshes**: frames that stayed on screen for more than one refresh interval (for example 14.5 ms or more on a 138 Hz playfield). It is based on `DisplayedTime`. This is the main quality signal for a pinball table.

| Columns | Meaning |
|---|---|
| `StutterGrade` | Letter grade `A` (best) to `F` (unplayable). `NULL` if the run was not graded. Order is A > B > C > D > F. |
| `StutterGradeScore` | Continuous score 0-100, higher is better. 90+ is A, 80-90 B, 70-80 C, 60-70 D, below 60 F. A run capped at C scores 79.9. Prefer this over the letter for scatter plots and trend lines because it has no steps. |
| `StutterGradeCapped` | `1` if the grade was limited to C because the run had two or more frames displayed for 3+ refreshes. |
| `StutterGradeNote` | Reason the run was not graded (all `DisplayedTime` missing, or fewer than 100 frames). `NULL` for graded runs. |
| `StutterGradeVersion` | Method version. `NULL` means the row has never been graded with this method. Currently `2`. |
| `GradeFrames`, `GradeMissingFrames` | Frames the grade was computed over (after dropping the final seconds) and frames that had no `DisplayedTime`. |
| `GradeDurationSec` | Seconds of play the graded frames cover. |
| `GradeRefreshMs`, `GradeRefreshSource` | The refresh interval used as one unit (ms) and where it came from: `auto` (median `DisplayedTime`, tracks the frame cap) or `manual` (the `--hz` override). `1000 / GradeRefreshMs` is the effective refresh rate in Hz and is a good axis for grouping runs by display setup. |
| `DoubledFrames` | Frames displayed for 1.5 to under 2.5 refreshes (one missed refresh). |
| `TripleFrames` | Frames displayed for 2.5 refreshes or more (a stall of two or more missed refreshes). These are weighted much more heavily. |
| `LongestMissRefreshes` | Length in refreshes of the longest stall (`1` means there was none). Multiply by `GradeRefreshMs` for its duration in ms. |
| `MissedFramePct` | `(DoubledFrames + TripleFrames) / GradeFrames * 100`. The plain miss rate. |
| `MissesPerMinute` | Missed frames per minute of play. |
| `WeightedMissPct` | Miss rate where each extra refresh of a 3+ frame counts three times. This is the number before the burst adjustment. |
| `ClusterPeakMisses`, `ClusterRatio`, `ClusterMultiplier` | Burst detection. The capture is cut into 5 second windows. `ClusterPeakMisses` is the most misses in one window, `ClusterRatio` is that window compared with the average window, and `ClusterMultiplier` (1.0 to 1.4) is the penalty applied when the worst window holds at least 2 misses. A value of 1.0 means no burst penalty. |
| `EffectiveMissPct` | `WeightedMissPct * ClusterMultiplier`. This is the value compared with the grade bands. |

**Default bands** (on `EffectiveMissPct`): A below 0.1%, B below 0.4%, C below 1%, D below 3%, F 3% or more. The score is continuous, so a run just either side of a band edge scores almost the same.

### Grading configuration (for reproducibility)

`CfgDoubledRefreshes`, `CfgTripleRefreshes`, `CfgBandA`, `CfgBandB`, `CfgBandC`, `CfgBandD`, `CfgZeroScorePct`, `CfgTripleWeight`, `CfgTripleCapGrade`, `CfgTripleCapMinCount`, `CfgTrimStartSeconds`, `CfgTrimEndSeconds`, `CfgClusterWindowSec`, `CfgClusterWeight`, `CfgClusterMaxExcess`, `CfgClusterMinEvents`, `CfgClusterMinPeak`, `CfgMinFrames`, `CfgBlameMinStalls`, `CfgBlameGpuSaturation`.

These store the thresholds the row was graded with. They are constants unless the tool was run with non-default settings or the rules were retuned, so they are rarely worth charting. Use them to check that two rows were graded the same way before comparing them. The values at the time of writing are 1.5 refreshes (doubled), 2.5 (3+), bands 0.1 / 0.4 / 1 / 3, weight 3, cap at C when 2 or more 3+ frames, warm-up skipped (`CfgTrimStartSeconds`, the launcher's `--delaystart`: usually 30 s, longer for some launchers/emulators), 5 s tail dropped, 5 s windows, and a 100 frame minimum.

### Present gap diagnostics (informational)

| Columns | Meaning |
|---|---|
| `LongGapCount`, `LongGapPct` | Frames preceded by a long gap between the game's present calls (1.5 refreshes or more), as a count and as a percent of frames. With frame pacing these are often harmless. |
| `LongGapThresholdMs` | The gap length counted as "long". |
| `LongGapMissCount` | How many of those long gaps were followed by a frame that was actually displayed for 1.5+ refreshes. This is the number that matters: a long gap with no visible miss costs nothing. |
| `LongGapNextDisplayedAvg` | Mean displayed time of the frame after a long gap. Close to `GradeRefreshMs` means nothing was visible. |

## Legacy stutter scores

`StutterScoreFT` and `StutterScoreDT` (0-100, higher is better) are the original weighted blends: 99.5th percentile frame-to-frame delta 50%, average delta 25%, standard deviation 10%, animation error RMS 10%, animation error MAD 5%. `FT` is computed from `FrameTime` (rendering pipeline), `DT` from `DisplayedTime` (what was shown). They are kept so old reports keep working.

Known limits: the 99.5th percentile component falls from full marks to zero depending on whether the miss rate is above or below about 0.5%, and the average and standard deviation parts give full marks to almost any VSync-locked run. They also include animation error, which is not valid for every renderer. Use the A-F grade for new visuals. If you show both, plot them on separate axes and label them.

## Frame rate

PresentMon frame data, one value per rendered frame, summarized per run.

| Columns | Meaning |
|---|---|
| `FpsAverage`, `FpsMin`, `FpsMax` | Mean, lowest and highest instantaneous FPS (`1000 / FrameTime` per frame). `FpsMax` and `FpsMin` are single-frame extremes and are noisy. |
| `Fps1PctLow`, `FpsPoint1PctLow` | The 1st and 0.1th percentile of per-frame FPS: the value below which 1% (or 0.1%) of frames fall. |

**Do not read these as the display cadence.** When a game uses a present queue (frame pacing, BGFX), `FrameTime` swings between short and long values, so `FpsAverage` can read far above the display rate (a capture ran at 137.9 Hz on screen but reported an average near 219 FPS) and the 1% low can look bad even though every frame was shown once. For smoothness use the grade or the `DisplayedTime` columns. For the real cadence use `1000 / DisplayedTimeAvg` or `1000 / GradeRefreshMs`.

## Frame time (rendering)

`FrameTime` is the time between consecutive calls to Present() by the game. It reflects the game's own rendering pace, not what appears on screen.

| Columns | Meaning |
|---|---|
| `FrameTimeAvg`, `FrameTimeMin`, `FrameTimeMax`, `FrameTimeStdDev` | Mean, minimum, maximum and population standard deviation of frame time. |
| `FrameTime999th`, `FrameTime995th`, `FrameTime99th`, `FrameTime95th` | The 99.9th, 99.5th, 99th and 95th percentile frame times: the value that this fraction of frames stayed under. The high percentiles describe the worst frames, so larger is worse. |
| `FrameTimeDeltaAvg`, `FrameTimeDeltaMax` | Mean and maximum of the absolute difference between consecutive frame times. A measure of frame-to-frame jitter. |
| `FrameTimeDelta999th`, `FrameTimeDelta995th`, `FrameTimeDelta99th`, `FrameTimeDelta95th` | Percentiles of that frame-to-frame difference. |

Frame time describes the rendering pipeline. With frame pacing, long and short gaps between Present() calls alternate (a queue filling and draining) without any visible effect, so frame-time statistics can look bad on a smooth run. Treat them as diagnostics.

## Displayed time (what was shown)

`DisplayedTime` is how long each frame stayed on the screen. On a fixed refresh it is a multiple of the refresh interval (about 7.25 ms at 138 Hz). It can be missing (`NA`) for some present modes, in which case the columns below are `NULL`.

| Columns | Meaning |
|---|---|
| `DisplayedTimeAvg`, `DisplayedTimeMin`, `DisplayedTimeMax`, `DisplayedTimeStdDev` | Mean, minimum, maximum and standard deviation of displayed time. `1000 / DisplayedTimeAvg` is the true display cadence in Hz. `DisplayedTimeMax` is the longest single frame (a stall). |
| `DisplayedTime999th`, `DisplayedTime995th`, `DisplayedTime99th`, `DisplayedTime95th` | Percentiles of displayed time. A value near the refresh interval means nearly all frames were shown once. Values near twice the interval mean misses. |
| `DisplayedTimeDeltaAvg`, `DisplayedTimeDeltaMax` | Mean and maximum change in displayed time between consecutive frames. |
| `DisplayedTimeDelta999th`, `DisplayedTimeDelta995th`, `DisplayedTimeDelta99th`, `DisplayedTimeDelta95th` | Percentiles of that change. |

## Animation error (informational)

`AnimationErrorMAD` is the mean absolute animation error and `AnimationErrorRMS` is its root mean square, in ms. PresentMon measures the gap between when a frame's animation was meant to be shown and when it actually was, against the CPU present time. That does not fit a renderer that simulates to a predicted display time, so this is **not reliable across renderers** (VPX 10.8 reported near zero, BGFX a few ms for the same table). It is not used in the grade. Only non-zero values are included, so a run with zero or unavailable animation error has `NULL`. Do not rank tables or builds by it.

## CPU and GPU utilization (system-wide, sampled about once per second)

| Columns | Meaning |
|---|---|
| `CpuAverage`, `CpuMin`, `CpuMax` | Total processor utilization of the whole machine in percent (Windows `Processor / % Processor Time / _Total`), not just the game. Sampled about once per second. |
| `GpuAverage`, `GpuMin`, `GpuMax` | Utilization of GPU 0 in percent, from NVIDIA NVML. `NULL` without an NVIDIA GPU. |
| `GpuMemoryAverage`, `GpuMemoryMin`, `GpuMemoryMax` | The NVML memory utilization of GPU 0 in percent. By NVML's definition this is the share of time the memory interface was busy, not how much VRAM was used. `NULL` without an NVIDIA GPU. |

These are coarse one-second samples, so short spikes are averaged away. Min and max are single samples.

## Per-frame CPU and GPU timing (PresentMon)

Four families with identical columns, all in ms per frame:

| Family | Meaning |
|---|---|
| `GpuBusy*` | Time the GPU spent actively working on the frame. |
| `GpuWait*` | Time the GPU sat idle waiting (for the CPU or synchronization). |
| `CpuBusy*` | Time the CPU spent preparing the frame. |
| `CpuWait*` | Time the CPU waited (for the GPU or synchronization). |

Each family has `Avg`, `Min`, `Max`, `999th`, `995th`, `99th` and `95th` columns (for example `GpuBusyAvg`, `CpuWait995th`). The percentile columns are the value that fraction of frames stayed under. Only frames with a value above zero are counted.

Caution: `CpuBusy` plus `CpuWait` is close to `FrameTime`, and a game that waits for VSync can show that wait as busy time. Do not read a high `CpuBusyAvg` as proof that the CPU is the bottleneck. GPU busy near zero for the whole run (a few hundredths of a ms) usually means the chain is not the main render chain.

## Bottleneck blame

`BlameCpuPct` and `BlameGpuPct` (0-100, always sum to 100) are the CPU-vs-GPU share of the **stalls the app itself caused**. A stall is a graded frame held for 1.5 refreshes or more. Each stall is classified (see [Stall causes](#stall-causes)), and the two numbers are the millisecond-weighted shares of the CPU-classified and GPU-classified stalls, where each stall weighs `DisplayedTime - refresh`. Present-mode-switch, app-waiting and display/compositor stalls are not blamed on either side.

Both are `NULL` when CPU stalls plus GPU stalls is below `CfgBlameMinStalls` (default 8). `NULL` means "not enough app-caused stalls to say", not "no timing data". Most runs are `NULL`, so never build a headline on blame: lead with `GpuBusyPctOfRefresh` and show blame, with the stall counts behind it, as the secondary "what caused the hitches" view.

`BlameVersion` records which formula produced the row: **4 is current; `NULL` or 1 is the legacy formula**. The legacy formula looked at the worst 1% of frames by `FrameTime` and used `CpuBusy * GpuWait` / `GpuBusy * CpuWait`. Frame-pacing limiters contaminated it, because `CpuBusy` includes the app's own limiter wait, so it reported extreme CPU blame on paced runs. Do not compare legacy and current values; the report labels anything below the highest version present as "legacy blame".

### Stall causes

| Columns | Meaning |
|---|---|
| `StallCpuCount`, `StallGpuCount`, `StallModeSwitchCount`, `StallAppWaitCount`, `StallDisplayCount` | Number of stalls per cause. Each stall falls in exactly one; the first matching rule below wins. |
| `GpuBusyPctOfRefresh` | Median `GpuBusy` (frames with `GpuBusy > 0`, graded frames) as a percent of the refresh interval. The headline "is the graphics card the limit?" number. |
| `CfgBlameMinStalls` | Minimum CPU+GPU stalls for blame to be reported (default 8). |
| `CfgBlameGpuSaturation` | GPU rule threshold as a fraction of the refresh (default 0.9). |

For stall frame `p` (next frame `p+1` on the same swap chain), with `refresh` the stored `GradeRefreshMs` or the median `DisplayedTime`:

1. **CPU:** `max(CPUBusy[p], CPUBusy[p+1]) >= 1.5 * refresh` and not (max `GPUBusy` greater than max `CPUBusy`).
2. **GPU:** `max(GPUBusy[p], GPUBusy[p+1]) >= CfgBlameGpuSaturation * refresh` **and** `max(FrameTime[p], FrameTime[p+1]) >= 1.5 * refresh`. The app must actually be late; otherwise a run with steady GPU load above 90% would label every unexplained stall as GPU.
3. **Present-mode switch:** `PresentMode` of `p-1` or `p+1` differs from `p`. Frames before the first graded frame (warm-up) are never looked at.
4. **App waiting / OS:** `FrameTime[p+1] >= 1.5 * refresh`.
5. **Display / compositor:** anything else.

A missing `CPUBusy`, `GPUBusy` or `FrameTime` on the next frame counts as 0. The report's run drill-down reproduces this classifier from the raw CSV and compares the counts and blame with these columns.

### Reading GPU load together with the stalls

The drill-down makes a derived call (report-side only, nothing stored): many stalls, high GPU load and GPU-labelled stalls point to a **GPU-limited** run; many stalls, low GPU load and mostly CPU-labelled stalls point to **CPU-limited**; mostly present-mode-switch, display or app-waiting stalls mean **neither** (a CPU or GPU upgrade will not fix it). Anything else is unknown. Both facts are needed: a run can show 97% GPU load while its stalls are CPU hitches. The load bands (under 50% headroom, 50-85% moderate, over 85% high) are `GPU_LOAD_BANDS` in `report_config.py`; raise them for VR.

Frame pacing caveat: the classifier uses absolute limits (1.5 refreshes), not `CpuBusy` relative to the frame time, so paced runs are handled. A CPU label on a paced run can still occur if the limiter overshoots, so do not over-claim.

### GPU identity

`GpuName` and `GpuDriverVersion` are the NVML device name and driver at capture time. Use them as filter or group fields; expect a single value until rows from another machine arrive. They are `NULL` on rows captured before the columns existed.


## Suggested visuals and pitfalls

- **Headline:** `StutterGrade` (colored A-F) and `StutterGradeScore` (continuous) per game. A grade-distribution bar chart or a game x grade heatmap works well.
- **Trend over time:** `StutterGradeScore` or `MissesPerMinute` by local `EntryDate`, to see the effect of a build or setting change. Group by `PresentApplication` to separate VPX 10.8 from BGFX.
- **Why a run scored badly:** show `DoubledFrames`, `TripleFrames`, `LongestMissRefreshes * GradeRefreshMs` (stall in ms) and `ClusterPeakMisses` together. One long stall and many small misses look very different in these columns and mean different things.
- **Display cadence:** `1000 / GradeRefreshMs` (Hz) or `1000 / DisplayedTimeAvg`, not `FpsAverage`.
- **Resource load:** `CpuAverage`, `GpuAverage` and the `GpuBusy*` columns for comparing tables, with the caveats above.
- **Always filter** `PresentMonFrameCount > 0` and `StutterGradeVersion IS NOT NULL` for grade views. Rows from runs that captured no frames have no frame statistics. Separate VR from desktop with `IsVR`.
- **Do not combine** `StutterGradeScore` and `StutterScoreDT` in one average or on one axis. They measure different things on similar-looking 0-100 scales.
- **Short or unusual runs.** Fewer frames means a coarser miss rate (one doubled frame is a larger share). Consider hiding runs with a small `GradeFrames` or `GradeDurationSec`.
- **A stall is not always visible.** A long stall during a drain with nothing happening is much less noticeable than one in the middle of play. The data has no game-state context, so do not over-interpret a single `TripleFrames`.
- **Missing-data handling.** Treat `NULL` as unknown. Do not convert it to 0.
