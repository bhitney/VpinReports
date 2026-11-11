const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'perf_drilldown.js'), 'utf8');
const core = source.slice(source.indexOf('//#region drill-core'), source.indexOf('//#endregion drill-core'));
const played = source.slice(source.indexOf('function drillPlayedAt('), source.indexOf('const drillBandsPlugin'));

const context = vm.createContext({});
vm.runInContext(core + played, context);
const { drillParseCsv, drillAnalyze, drillPlayedAt, drillCause, drillLimiterCall } = vm.runInContext(
  '({ drillParseCsv, drillAnalyze, drillPlayedAt, drillCause, drillLimiterCall })', context);

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
