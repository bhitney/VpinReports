const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'perf_drilldown.js'), 'utf8');
const core = source.slice(source.indexOf('//#region drill-core'), source.indexOf('//#endregion drill-core'));
const played = source.slice(source.indexOf('function drillPlayedAt('), source.indexOf('const drillBandsPlugin'));

const context = vm.createContext({ escapeHtml: s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;') });
vm.runInContext(core + played, context);
const { drillParseCsv, drillAnalyze, drillPlayedAt, drillCause, drillLimiterCall, drillWhen, drillTilesHtml, drillParseLog, drillInferWarmup,
  drillParseSys, drillSysStem, drillSplitLine, drillSysSeries, drillSysTickAt, drillSysProcsAt, drillProcText, drillJitter, drillJitterTile } = vm.runInContext(
  '({ drillParseCsv, drillAnalyze, drillPlayedAt, drillCause, drillLimiterCall, drillWhen, drillTilesHtml, drillParseLog, drillInferWarmup, drillParseSys, drillSysStem, drillSplitLine, drillSysSeries, drillSysTickAt, drillSysProcsAt, drillProcText, drillJitter, drillJitterTile })', context);

// 10 s at 10 ms per frame: warm-up 2 s, tail trim 1 s (graded window 2 s .. 9 s).
function buildCsv(modeAt) {
  const lines = ['CPUStartTime,SwapChainAddress,PresentMode,FrameTime,CPUBusy,GPUBusy,DisplayedTime'];
  for (let i = 0; i < 1000; i++) lines.push(`${1000 + i * 10},0xA,${modeAt(i)},10,1,1,10`);
  return lines.join('\n');
}

const cfg = { TrimStartSeconds: 2, TrimEndSeconds: 1, MinFrames: 10 };

test('only present-mode changes inside the graded window are reported', () => {
  const csv = drillParseCsv(buildCsv(i =>
    i < 50 ? 'Composed: Flip'                       // game load (warm-up): ignored
      : i === 500 ? 'Hardware: Independent Flip'    // one-frame blip while graded: 2 changes
        : i >= 950 ? 'Composed: Flip'               // game exit (trimmed tail): ignored
          : 'Hardware Composed: Independent Flip'));
  const res = drillAnalyze(csv, null, cfg, NaN);
  assert.equal(res.modeChanges.length, 2);
  assert.deepEqual([...res.modeChanges.map(c => csv.CPUStartTime[c.r])], [6000, 6010]);
  assert.equal(res.modeChanges[0].from, 'Hardware Composed: Independent Flip');
  assert.equal(res.modeChanges[0].to, 'Hardware: Independent Flip');
  assert.equal(res.modeChanges[1].to, 'Hardware Composed: Independent Flip');
});

test('a steady present mode produces no changes', () => {
  const csv = drillParseCsv(buildCsv(() => 'Hardware Composed: Independent Flip'));
  assert.equal(drillAnalyze(csv, null, cfg, NaN).modeChanges.length, 0);
});

test('played time prefers the file name, then database, then modified time', () => {
  assert.equal(drillPlayedAt({ name: 'PresentMon_20260712174044_Brave Team (Stern 1980).csv' }, null).text, '2026-07-12 17:40:44');
  const db = drillPlayedAt({ name: 'other.csv' }, { EntryDateTime: '2026-07-12 21:42:35' });
  assert.equal(db.text, '2026-07-12 21:42:35');
  assert.match(db.note, /database/);
  const mtime = drillPlayedAt({ name: 'other.csv', lastModified: new Date(2026, 6, 12, 17, 42, 1).getTime() }, null);
  assert.equal(mtime.text, '2026-07-12 17:42:01');
  assert.equal(drillPlayedAt({ name: 'other.csv' }, null), null);
});

test('swap chains are summarized and can be chosen explicitly', () => {
  const lines = ['CPUStartTime,SwapChainAddress,PresentMode,FrameTime,CPUBusy,GPUBusy,DisplayedTime'];
  for (let i = 0; i < 1000; i++) lines.push(`${1000 + i * 10},0xPLAY,Hardware: Independent Flip,10,1,1,10`);
  for (let i = 0; i < 300; i++) lines.push(`${1000 + i * 33},0xDMD,Composed: Flip,16,1,1,33`);
  const csv = drillParseCsv(lines.join('\n'));
  const auto = drillAnalyze(csv, null, cfg, NaN);
  assert.equal(auto.swap, '0xPLAY');
  assert.equal(auto.swapStats.length, 2);
  assert.equal(auto.swapStats[0].swap, '0xPLAY');
  assert.equal(Math.round(auto.swapStats[0].hz), 100);
  assert.equal(auto.swapStats[1].mode, 'Composed: Flip');
  const forced = drillAnalyze(csv, null, cfg, NaN, '0xDMD');
  assert.equal(forced.swap, '0xDMD');
  assert.equal(Math.round(forced.refresh), 33);
});

test('tied swap chains pick the one with the most GPU work', () => {
  const lines = ['CPUStartTime,SwapChainAddress,PresentMode,FrameTime,CPUBusy,GPUBusy,DisplayedTime'];
  for (let i = 0; i < 1000; i++) {
    lines.push(`${1000 + i * 10},0xDMD,Composed: Flip,10,7,0.05,10`);
    lines.push(`${1000 + i * 10},0xPLAY,Hardware: Independent Flip,10,7,2.3,10`);
  }
  const csv = drillParseCsv(lines.join('\n'));
  const res = drillAnalyze(csv, null, cfg, NaN);
  assert.equal(res.swap, '0xPLAY');
});

// ---- stall classifier and blame (BlameVersion 4) ----------------------------------------------------------

const STALL_CFG = { TrimStartSeconds: 2, TrimEndSeconds: 1, MinFrames: 10, BlameMinStalls: 8, BlameGpuSaturation: 0.9 };

// 12 s at a 10 ms refresh; warm-up is the first 2 s, stalls start at frame 300 (every 3rd frame) so they sit mid-run.
// GPUBusy 9.7 ms = 97% of the refresh, like run 185 (Masters of the Universe): high steady-state GPU load.
function stallCsv(cpuStalls, displayStalls) {
  const lines = ['CPUStartTime,SwapChainAddress,PresentMode,FrameTime,CPUBusy,GPUBusy,DisplayedTime'];
  const kinds = new Map();
  for (let k = 0; k < cpuStalls + displayStalls; k++) kinds.set(300 + 3 * k, k < cpuStalls ? 'cpu' : 'display');
  for (let i = 0; i < 1200; i++) {
    const t = 1000 + i * 10, kind = kinds.get(i);
    if (kind === 'cpu') lines.push(`${t},0xA,Hardware: Independent Flip,16,16,9.7,20`);
    else if (kind === 'display') lines.push(`${t},0xA,Hardware: Independent Flip,10,7,9.7,20`);
    else lines.push(`${t},0xA,Hardware: Independent Flip,10,7,9.7,10`);
  }
  return drillParseCsv(lines.join('\n'));
}

test('run-185 style: high GPU load, CPU stalls and display stalls, GPU rule needs a late app', () => {
  const res = drillAnalyze(stallCsv(40, 139), null, STALL_CFG, NaN);
  assert.deepEqual({ ...res.stallCounts }, { cpu: 40, gpu: 0, modeSwitch: 0, appWait: 0, display: 139 });
  assert.equal(res.blameCpuPct, 100);
  assert.equal(res.blameGpuPct, 0);
  assert.ok(Math.abs(res.gpuLoadPct - 97) < 1e-6);
  // 97% GPU load must not make the unexplained stalls GPU: the app was not late on them.
  assert.equal(res.stallCounts.gpu, 0);
  // Mostly display stalls, so a CPU or GPU upgrade is not what fixes this run.
  assert.equal(res.limiter.call, 'neither');
});

test('blame is NULL below the minimum number of CPU+GPU stalls', () => {
  const res = drillAnalyze(stallCsv(7, 20), null, STALL_CFG, NaN);
  assert.equal(res.stallCounts.cpu, 7);
  assert.equal(res.blameCpuPct, null);
  assert.equal(res.blameGpuPct, null);
  const enough = drillAnalyze(stallCsv(8, 20), null, STALL_CFG, NaN);
  assert.equal(enough.blameCpuPct, 100);
  const custom = drillAnalyze(stallCsv(7, 20), null, { ...STALL_CFG, BlameMinStalls: 5 }, NaN);
  assert.equal(custom.blameCpuPct, 100);
});

test('classifier rules: order, GPU needs a late app, mode switch scope, missing values count as 0', () => {
  const mk = (rows) => ({
    CPUBusy: rows.map(r => r.cpu), GPUBusy: rows.map(r => r.gpu), FrameTime: rows.map(r => r.ft), mode: rows.map(r => r.mode),
  });
  const cfg = { BlameGpuSaturation: 0.9 };
  const refresh = 10;
  const n = (cpu, gpu, ft, mode = 'A') => ({ cpu, gpu, ft, mode });

  // CPU wins unless GPU busy is larger than CPU busy.
  let csv = mk([n(16, 5, 10), n(1, 1, 10)]);
  assert.equal(drillCause(csv, [0, 1], 0, refresh, cfg, 0).causeKey, 'cpu');
  csv = mk([n(16, 18, 16), n(1, 1, 10)]);
  assert.equal(drillCause(csv, [0, 1], 0, refresh, cfg, 0).causeKey, 'gpu');

  // GPU saturated but the app was not late -> not GPU (falls through to display).
  csv = mk([n(1, 9.5, 10), n(1, 1, 10)]);
  assert.equal(drillCause(csv, [0, 1], 0, refresh, cfg, 0).causeKey, 'display');
  // Same load with a late next frame -> GPU.
  csv = mk([n(1, 9.5, 10), n(1, 1, 16)]);
  assert.equal(drillCause(csv, [0, 1], 0, refresh, cfg, 0).causeKey, 'gpu');
  // The saturation fraction comes from the config.
  assert.equal(drillCause(csv, [0, 1], 0, refresh, { BlameGpuSaturation: 0.99 }, 0).causeKey, 'appWait');

  // Mode switch: a different mode on the previous or next frame.
  csv = mk([n(1, 1, 10, 'A'), n(1, 1, 10, 'B'), n(1, 1, 10, 'B')]);
  assert.equal(drillCause(csv, [0, 1, 2], 1, refresh, cfg, 0).causeKey, 'modeSwitch');
  // ...but the frame before the first graded frame is warm-up and is never looked at.
  assert.equal(drillCause(csv, [0, 1, 2], 1, refresh, cfg, 1).causeKey, 'display');
  // The frame after the last graded frame is still read (tail trim only removes frames from the graded set).
  csv = mk([n(1, 1, 10, 'B'), n(1, 1, 10, 'A')]);
  assert.equal(drillCause(csv, [0, 1], 0, refresh, cfg, 0).causeKey, 'modeSwitch');

  // App waiting, and NaN / missing next-frame values count as 0.
  csv = mk([n(1, 1, 10), n(1, 1, 16)]);
  assert.equal(drillCause(csv, [0, 1], 0, refresh, cfg, 0).causeKey, 'appWait');
  csv = mk([n(1, 1, 10), n(NaN, NaN, NaN)]);
  assert.equal(drillCause(csv, [0, 1], 0, refresh, cfg, 0).causeKey, 'display');
  assert.equal(drillCause(csv, [0], 0, refresh, cfg, 0).causeKey, 'display');
});

test('limiter call needs both GPU load and the stall causes', () => {
  const cfg = { BlameMinStalls: 8 };
  const c = (cpu, gpu, other = 0) => ({ cpu, gpu, modeSwitch: other, appWait: 0, display: 0 });
  assert.equal(drillLimiterCall(c(2, 20), 95, 105, cfg).call, 'gpu');
  assert.equal(drillLimiterCall(c(20, 1), 30, 60, cfg).call, 'cpu');
  // High GPU load with CPU-type hitches is not GPU-limited, and low GPU load alone never means CPU-bound.
  assert.equal(drillLimiterCall(c(30, 0), 97, 80, cfg).call, 'unknown');
  assert.equal(drillLimiterCall(c(3, 3), 20, 40, cfg).call, 'unknown');
  assert.equal(drillLimiterCall(c(1, 1, 40), 60, 80, cfg).call, 'neither');
  assert.equal(drillLimiterCall(c(3, 2), 30, 40, cfg).call, 'unknown');
  // Custom bands (for example VR) move the thresholds.
  assert.equal(drillLimiterCall(c(2, 20), 80, 105, cfg, [70, 95]).call, 'unknown');
});

test('start/end time: file-name start plus capture length, no time zone conversion', () => {
  const f = { name: 'PresentMon_20260822225834_Mariner (Bally 1971).csv' };
  // Same day: the end shows only the time.
  assert.equal(drillWhen(f, null, 3254).text, '2026-08-22 22:58:34 – 23:52:48');
  // Crossing midnight (and a month end): the end shows the full date.
  const late = { name: 'PresentMon_20260831235500_Game.csv' };
  assert.equal(drillWhen(late, null, 600).text, '2026-08-31 23:55:00 – 2026-09-01 00:05:00');
  // No usable span -> start only.
  assert.equal(drillWhen(f, null, NaN).text, '2026-08-22 22:58:34');
  // No file-name time: the database time is UTC, so it is shown alone as an approximate time, never as a range.
  const db = drillWhen({ name: 'other.csv' }, { EntryDateTime: '2026-07-12 21:42:35' }, 600);
  assert.equal(db.text, '2026-07-12 21:42:35');
  assert.match(db.tip, /database/);
  assert.equal(drillWhen({ name: 'other.csv' }, null, 600), null);
});

test('tile groups: empty groups are dropped, extra classes and hover text are kept and escaped', () => {
  const html = drillTilesHtml([
    { title: 'Run', tiles: [['7,284', 'graded frames']] },
    { title: 'Load', tiles: [] },
    { title: 'Diagnosis', tiles: [['Recording?', 'possible', 'a "quoted" <tip>', 'warn'], ['x', 'y', null, 'wide']] },
  ]);
  assert.equal((html.match(/<section class="drill-group">/g) || []).length, 2);
  assert.ok(!html.includes('Load'));
  assert.match(html, /drill-tile warn" title="a &quot;quoted&quot; &lt;tip>"/);
  assert.match(html, /drill-tile wide">/);
});

// ---------------------------------------------------------------------------
// Time base: VPinPerfMon writes CPUStartQPCTime (absolute QPC ms) by default; older captures have the
// relative CPUStartTime. Both must give the same analysis.
// ---------------------------------------------------------------------------

// Same 10 s capture as buildCsv, with a configurable time column and origin. One 40 ms frame at i = 500 is a
// 4-refresh stall; everything else is steady.
function buildTimed(column, origin) {
  const lines = [`${column},SwapChainAddress,PresentMode,FrameTime,CPUBusy,GPUBusy,DisplayedTime`];
  let t = origin;
  for (let i = 0; i < 1000; i++) {
    const stall = i === 500;
    const ft = stall ? 40 : 10;
    lines.push(`${t.toFixed(4)},0xA,Hardware Composed: Independent Flip,${ft},${stall ? 38 : 1},1,${ft}`);
    t += ft;
  }
  return lines.join('\n');
}

test('the time column is detected from the header and recorded', () => {
  assert.equal(drillParseCsv(buildTimed('CPUStartTime', 1000)).timeBase, 'relative');
  assert.equal(drillParseCsv(buildTimed('CPUStartQPCTime', 12423667.2169)).timeBase, 'qpc');
  // Byte-order mark and CRLF line endings, as written by PresentMon on Windows.
  const parsed = drillParseCsv('\uFEFF' + buildTimed('CPUStartQPCTime', 5000).replace(/\n/g, '\r\n'));
  assert.equal(parsed.timeBase, 'qpc');
  assert.equal(parsed.n, 1000);
  assert.equal(parsed.CPUStartTime[0], 5000);
});

test('QPC values keep their precision (absolute origin around 1e7 ms)', () => {
  const csv = drillParseCsv(buildTimed('CPUStartQPCTime', 12423667.2169));
  assert.equal(csv.CPUStartTime[0], 12423667.2169);
  assert.ok(Math.abs(csv.CPUStartTime[1] - csv.CPUStartTime[0] - 10) < 1e-6);
});

test('a QPC capture analyzes exactly like the same capture with a relative origin', () => {
  const rel = drillAnalyze(drillParseCsv(buildTimed('CPUStartTime', 1000)), null, cfg, NaN);
  const qpc = drillAnalyze(drillParseCsv(buildTimed('CPUStartQPCTime', 12423667.2169)), null, cfg, NaN);
  assert.equal(qpc.frames, rel.frames);
  assert.equal(qpc.misses.length, 1);
  assert.equal(rel.misses.length, 1);
  assert.equal(qpc.grade, rel.grade);
  assert.ok(qpc.grade, 'the capture is gradeable');
  assert.ok(Math.abs(qpc.score - rel.score) < 1e-9);
  assert.equal(qpc.warmupSec, 2);
  assert.ok(Math.abs(qpc.duration - rel.duration) < 1e-6);
  // The chart x axis is seconds since the first frame, whatever the origin.
  assert.ok(Math.abs((qpc.misses[0].ms) - (rel.misses[0].ms)) < 1e-6);
});

test('the warm-up cutoff is relative to the first frame, not to zero', () => {
  const csv = drillParseCsv(buildTimed('CPUStartQPCTime', 12423667.2169));
  const res = drillAnalyze(csv, null, { TrimStartSeconds: 3, TrimEndSeconds: 1, MinFrames: 10 }, NaN);
  assert.ok(Math.abs(res.warmEnd - (csv.CPUStartTime[0] + 3000)) < 1e-6);
  assert.equal(res.t0, csv.CPUStartTime[0]);
  assert.equal(res.warmupSec, 3);
});

test('the warm-up from the console log works for relative and QPC captures', () => {
  const rel = 'First frame CPUStartTime: 3569.31ms, Warm-up threshold: 33569.31ms\nParsed 1 total frames';
  const qpc = 'First frame CPUStartTime: 21932929.82ms, Warm-up threshold: 21962929.82ms\nParsed 1 total frames';
  assert.ok(Math.abs(drillParseLog(rel) - 30) < 1e-6);
  assert.ok(Math.abs(drillParseLog(qpc) - 30) < 1e-6);
  // No first-frame line: fall back to the configured delay.
  assert.equal(drillParseLog('Data collection will begin after 15 second delay (warm-up period).'), 15);
  assert.ok(Number.isNaN(drillParseLog('nothing useful')));
});

test('the warm-up can be inferred from the stored frame count with a QPC origin', () => {
  const csv = drillParseCsv(buildTimed('CPUStartQPCTime', 12423667.2169));
  const framesAfter = secs => csv.CPUStartTime.filter(t => t >= csv.CPUStartTime[0] + secs * 1000).length;
  const inferred = drillInferWarmup(csv, { PresentMonFrameCount: framesAfter(4) });
  assert.equal(inferred.sec, 4);
  assert.match(inferred.source, /inferred/);
});

test('a CSV with neither time column is refused with a message that names both', () => {
  assert.throws(() => drillParseCsv('FrameTime,DisplayedTime\n1,2'), /CPUStartTime \(or CPUStartQPCTime\)/);
  assert.throws(() => drillParseCsv('CPUStartQPCTime,FrameTime\n1,2'), /DisplayedTime/);
});

// ---------------------------------------------------------------------------
// System sampler files (VPinPerfMon --sysstats): SysStats_, SysProcs_ and SysEvents_ next to the run CSV.
// ---------------------------------------------------------------------------

const SYS_STATS = 'QpcMs,UtcTime,CpuTotalPct,CpuMaxCorePct,DpcPct,InterruptPct,CtxSwitchesPerSec,DiskBytesPerSec,DiskQueue,MemAvailMB,PagesInputPerSec,GpuTotalPct,ForegroundProcess,SampleMs,SampleCpuMs,ReadMs,ComputeMs\n' +
  '12417000.5000,2026-11-12T21:49:56Z,10.5,30.1,0.2,0.4,20000,1000,0.00,26000,0.0,35.5,VPinballX64,3.1,2.0,2.4,0.7\n' +
  '12418000.5000,2026-11-12T21:49:57Z,71.0,98.2,0.3,0.5,21000,2000,0.00,26000,0.0,36.0,VPinballX64,3.4,3.1,2.5,0.9\n' +
  '12419000.5000,2026-11-12T21:49:58Z,,,0.1,0.2,19000,500,0.00,26000,0.0,,"Odd, name",3.0,2.0,2.4,0.6\n';
const SYS_PROCS = 'QpcMs,Pid,ProcessName,CpuPct,GpuPct,WorkingSetMB,IoBytesPerSec\n' +
  '12418000.5000,812,powershell,49.80,0.00,55.5,0\n' +
  '12418000.5000,4400,PinUpDisplayX,3.10,5.20,311.0,428277\n' +
  '12419000.5000,4400,"we,ird",1.20,0.00,311.0,0\n';
const SYS_EVENTS = 'QpcMs,Event,ProcessName,Pid\n12418000.5000,ProcessStart,powershell,812\n';

test('sampler file names map to the run name stem', () => {
  assert.equal(drillSysStem('PresentMon_20261112192820_Die Hard Trilogy (VPW 2023).csv'), '20261112192820_Die Hard Trilogy (VPW 2023)');
  assert.equal(drillSysStem('SysStats_20261112192820_Die Hard Trilogy (VPW 2023).csv'), null);
  assert.equal(drillSysStem('other.csv'), null);
});

test('quoted cells are split correctly', () => {
  assert.deepEqual([...drillSplitLine('1.0,12,"a,b",3.5')], ['1.0', '12', 'a,b', '3.5']);
  assert.deepEqual([...drillSplitLine('1,"he said ""hi""",2')], ['1', 'he said "hi"', '2']);
  assert.deepEqual([...drillSplitLine('a,b,c')], ['a', 'b', 'c']);
});

test('SysStats, SysProcs and SysEvents are parsed; empty cells are NaN and QpcMs keeps its precision', () => {
  const sys = drillParseSys({ stats: SYS_STATS, procs: SYS_PROCS, events: SYS_EVENTS });
  assert.equal(sys.stats.n, 3);
  assert.equal(sys.stats.QpcMs[0], 12417000.5);
  assert.equal(sys.stats.CpuTotalPct[1], 71);
  assert.ok(Number.isNaN(sys.stats.CpuTotalPct[2]));
  assert.ok(Number.isNaN(sys.stats.GpuTotalPct[2]));
  assert.equal(sys.stats.ForegroundProcess[2], 'Odd, name');
  assert.equal(sys.stats.SampleCpuMs[0], 2);
  assert.equal(sys.procs.n, 3);
  assert.equal(sys.procs.ProcessName[2], 'we,ird');
  assert.equal(sys.events.n, 1);
  assert.deepEqual([...sys.procsByTick.get(12418000.5)], [0, 1]);
  assert.equal(sys.problems.length, 0);
});

test('an older SysStats without the timing columns, and a row cut off at the end, still load', () => {
  const old = 'QpcMs,UtcTime,CpuTotalPct,CpuMaxCorePct,DpcPct,InterruptPct,CtxSwitchesPerSec,DiskBytesPerSec,DiskQueue,MemAvailMB,PagesInputPerSec,GpuTotalPct,ForegroundProcess,SampleMs\n' +
    '100.0,2026-11-12T17:00:00Z,4.5,20,0.1,0.2,15000,1000,0.0,16000,0,2.2,Helper,3.1\n' +
    '1100.0,2026-11-12T17:00:01Z,5.5,21,0.1,0.2,16000,1000,0.0,16000,0,2.3,Helper,3.0\n' +
    '2100.0,2026-11-12T17:00:02Z,6';
  const sys = drillParseSys({ stats: old });
  assert.equal(sys.stats.n, 2);
  assert.equal(sys.stats.skipped, 1);
  assert.equal(sys.stats.SampleCpuMs, undefined);
  assert.equal(sys.procs, null);
});

test('without SysStats there is no sampler data; a damaged SysProcs is reported and left out', () => {
  assert.equal(drillParseSys({ procs: SYS_PROCS }), null);
  assert.equal(drillParseSys(undefined), null);
  const sys = drillParseSys({ stats: SYS_STATS, procs: 'nonsense' });
  assert.equal(sys.stats.n, 3);
  assert.equal(sys.procs, null);
  assert.match(sys.problems[0], /SysProcs/);
  assert.throws(() => drillParseSys({ stats: 'no,time,column\n1,2,3' }), /QpcMs/);
});

test('sampler time is seconds since the first frame, on the same clock as the QPC run CSV', () => {
  const csv = drillParseCsv(buildTimed('CPUStartQPCTime', 12417500));
  const res = drillAnalyze(csv, null, cfg, NaN);
  assert.equal(res.t0, 12417500);
  const series = drillSysSeries(drillParseSys({ stats: SYS_STATS }), csv, res);
  // SysStats rows are stamped 12417000.5, 12418000.5, 12419000.5 ms: the first one is 0.4995 s before the first frame.
  assert.deepEqual([...series.x].map(v => +v.toFixed(4)), [-0.4995, 0.5005, 1.5005]);
  assert.equal(series.n, 3);
  assert.equal(series.cpu.length, 2);                // the row with an empty CPU cell is left out of the line
  assert.equal(series.cpu[1].y, 71);
  assert.equal(series.gpu.length, 2);
  assert.equal(series.firstX, series.x[0]);
});

test('a relative-origin run CSV has no clock in common with the sampler, so no sampler series is built', () => {
  const csv = drillParseCsv(buildTimed('CPUStartTime', 1000));
  const res = drillAnalyze(csv, null, cfg, NaN);
  assert.equal(drillSysSeries(drillParseSys({ stats: SYS_STATS }), csv, res), null);
  const qpc = drillParseCsv(buildTimed('CPUStartQPCTime', 12417500));
  assert.equal(drillSysSeries(null, qpc, drillAnalyze(qpc, null, cfg, NaN)), null);
});

test('a time maps to the sampler second that ends at or after it (each sample averages the second before its stamp)', () => {
  const sys = drillParseSys({ stats: 'QpcMs,CpuTotalPct\n1000,10\n2000,20\n3000,30\n4000,40' });
  const series = drillSysSeries(sys, { timeBase: 'qpc' }, { t0: 1000 });    // x = 0, 1, 2, 3 s
  assert.deepEqual([...series.x], [0, 1, 2, 3]);
  const at = t => drillSysTickAt(series, t);
  assert.equal(at(-0.5), 0);       // inside the second before the first stamp
  assert.equal(at(0), 0);          // exactly on a stamp: that stamp
  assert.equal(at(0.0001), 1);
  assert.equal(at(1), 1);
  assert.equal(at(2.9), 3);
  assert.equal(at(3), 3);
  assert.equal(at(-1), -1);        // before the first second
  assert.equal(at(3.0001), -1);    // after the last stamp
  assert.equal(at(NaN), -1);
});

test('busiest processes for a sampler second, highest load first, with a readable label', () => {
  const sys = drillParseSys({
    stats: 'QpcMs,CpuTotalPct\n1000,10\n2000,20',
    procs: 'QpcMs,Pid,ProcessName,CpuPct,GpuPct,WorkingSetMB,IoBytesPerSec\n1000,1,dwm,2.0,9.0,1,0\n1000,2,"a,b",30.0,0.0,1,0\n2000,3,idle,0.1,0.0,1,0',
  });
  const p = drillSysProcsAt(sys, 0);
  assert.deepEqual([...p.map(x => x.name)], ['a,b', 'dwm']);
  assert.equal(drillProcText(p[0]), 'a,b CPU 30%');
  assert.equal(drillProcText(p[1]), 'dwm CPU 2% GPU 9%');
  assert.equal(drillProcText(drillSysProcsAt(sys, 1)[0]), 'idle');
  assert.equal(drillSysProcsAt(sys, -1).length, 0);
  assert.equal(drillSysProcsAt(drillParseSys({ stats: 'QpcMs,CpuTotalPct\n1,1' }), 0).length, 0);
  assert.equal(drillSysProcsAt(null, 0).length, 0);
});

function jitterInput(frameAt, shownAt, n = 1000, refresh = 7.246) {
  const FrameTime = new Float64Array(n), DisplayedTime = new Float64Array(n);
  for (let i = 0; i < n; i++) { FrameTime[i] = frameAt(i); DisplayedTime[i] = shownAt(i); }
  return { csv: { FrameTime, DisplayedTime }, res: { graded: Array.from({ length: n }, (_, i) => i), refresh } };
}

test('a limiter zigzag (late frame, early frame) with steady on-screen time reads as jitter, not judder', () => {
  const { csv, res } = jitterInput(i => 7.246 + (i % 2 ? 0.6 : -0.6), () => 7.246);
  const j = drillJitter(csv, res);
  assert.ok(Math.abs(j.frameSd - 0.6) < 0.01);
  assert.ok(j.alternating < -0.95);
  assert.ok(j.displayedSd < 0.001);
  const tile = drillJitterTile(j, res.refresh)[0];
  assert.match(tile[0], /^±0\.60 ms$/);
  assert.match(tile[1], /zigzag, on-screen time steady/);
  assert.equal(tile[3], undefined);
});

test('uneven on-screen time is flagged, and stalls do not count toward jitter', () => {
  const { csv, res } = jitterInput(() => 7.246, i => (i % 2 ? 7.9 : 6.6));
  const tile = drillJitterTile(drillJitter(csv, res), res.refresh)[0];
  assert.match(tile[1], /on-screen time varies/);
  assert.equal(tile[3], 'warn');
  const withStall = jitterInput(i => (i === 500 ? 14.5 : 7.246), i => (i === 500 ? 14.5 : 7.246));
  assert.ok(drillJitter(withStall.csv, withStall.res).frameSd < 1e-9);
});

test('too few frames, or no refresh, gives no jitter tile', () => {
  const small = jitterInput(() => 7.2, () => 7.2, 50);
  assert.equal(drillJitter(small.csv, small.res), null);
  assert.equal(drillJitter(null, null), null);
  assert.equal(drillJitterTile(null, 7.2).length, 0);
});
