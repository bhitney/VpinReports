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
const { drillParseCsv, drillAnalyze, drillPlayedAt } = vm.runInContext('({ drillParseCsv, drillAnalyze, drillPlayedAt })', context);

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
