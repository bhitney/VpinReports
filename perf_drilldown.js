// ===========================================================================
// Run drill-down: re-reads a run's raw PresentMon CSV (picked locally by the
// user) and re-grades it in the browser. Injected by perf_report.py into the
// Performance Stats IIFE, so it shares rawRecords, currentState, colors,
// gradeColors, escapeHtml, drillCfgs, etc.
// ===========================================================================

//#region drill-core (pure functions; also exercised by the Node self-test)
const DRILL_WARMUP_SEC = 30;   // VPinPerfMon default --delaystart, used only when nothing better is known
const DRILL_WARMUP_MAX_SEC = 900;
const DRILL_FALLBACK_CFG = {
  DoubledRefreshes: 1.5, TripleRefreshes: 2.5, BandA: 0.1, BandB: 0.4, BandC: 1, BandD: 3, ZeroScorePct: 10,
  TripleWeight: 3, TripleCapGrade: 'C', TripleCapMinCount: 2, ClusterWindowSec: 5, ClusterWeight: 0.1,
  ClusterMaxExcess: 4, ClusterMinEvents: 3, ClusterMinPeak: 2, MinFrames: 100, TrimEndSeconds: 5,
  BlameMinStalls: 8, BlameGpuSaturation: 0.9,
};
const DRILL_NUM_COLS = ['CPUStartTime', 'FrameTime', 'CPUBusy', 'CPUWait', 'GPUBusy', 'GPUWait', 'DisplayLatency', 'DisplayedTime'];

// Banker's rounding, matching .NET Math.Round used by VPinPerfMon.
function drillRound(v) {
  const f = Math.floor(v);
  const d = v - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

function drillMax(...values) {
  let m = NaN;
  for (const v of values) if (Number.isFinite(v) && !(v <= m)) m = v;
  return m;
}

function drillMedian(values) {
  if (!values.length) return NaN;
  const s = Float64Array.from(values).sort();
  const h = s.length >> 1;
  return s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2;
}

function drillPercentile(values, q) {
  if (!values.length) return NaN;
  const s = Float64Array.from(values).sort();
  return s[Math.min(s.length - 1, Math.floor(q * s.length))];
}

function drillParseCsv(text) {
  const lines = text.split(/\r?\n/);
  const header = (lines[0] || '').replace(/^\uFEFF/, '').split(',').map(s => s.trim());
  const missing = ['CPUStartTime', 'DisplayedTime'].filter(c => !header.includes(c));
  if (missing.length) {
    throw new Error(`this doesn't look like a PresentMon --v2_metrics CSV (no ${missing.join(' / ')} column)`);
  }
  const rows = [];
  for (let i = 1; i < lines.length; i++) if (lines[i].length) rows.push(lines[i]);
  const n = rows.length;
  const csv = { n, header };
  const idx = {};
  for (const c of DRILL_NUM_COLS) {
    idx[c] = header.indexOf(c);
    csv[c] = new Float64Array(n).fill(NaN);
  }
  const swapIdx = header.indexOf('SwapChainAddress');
  const modeIdx = header.indexOf('PresentMode');
  csv.swap = new Array(n);
  csv.mode = new Array(n);
  for (let r = 0; r < n; r++) {
    const cells = rows[r].split(',');
    for (const c of DRILL_NUM_COLS) {
      const k = idx[c];
      if (k < 0) continue;
      const s = cells[k];
      csv[c][r] = s === undefined || s === '' || s === 'NA' ? NaN : +s;
    }
    csv.swap[r] = swapIdx >= 0 ? (cells[swapIdx] ?? '') : '';
    csv.mode[r] = modeIdx >= 0 ? (cells[modeIdx] ?? '') : '';
  }
  return csv;
}

// Warm-up seconds from the run's VPinPerfMon console log (TXT next to the CSV), or NaN.
function drillParseLog(text) {
  const thr = /Warm-up threshold:\s*([\d.]+)\s*ms/i.exec(text);
  const first = /First frame CPUStartTime:\s*([\d.]+)\s*ms/i.exec(text);
  if (thr && first) return (+thr[1] - +first[1]) / 1000;
  const m = /begin after\s*([\d.]+)\s*second delay/i.exec(text) || /from warm-up period\s*\(([\d.]+)\s*s\)/i.exec(text);
  return m ? +m[1] : NaN;
}

function drillPickSwap(csv, rec, from, forceSwap) {
  const counts = new Map();
  for (let r = 0; r < csv.n; r++) if (csv.CPUStartTime[r] >= from) counts.set(csv.swap[r], (counts.get(csv.swap[r]) || 0) + 1);
  if (!counts.size) return { swap: null, counts };
  const want = String(forceSwap || rec?.SwapChainAddress || '').toLowerCase();
  let swap = [...counts.keys()].find(k => want && k.toLowerCase() === want);
  if (swap == null) swap = drillLikelyPlayfield(drillSwapStats(csv, from));
  return { swap, counts };
}

// Chains presented in the same render pass tie on frame count and timing; the playfield is the one doing real GPU work.
function drillLikelyPlayfield(stats) {
  const most = Math.max(...stats.map(s => s.frames));
  const cands = stats.filter(s => s.frames >= most * 0.99);
  let best = cands[0];
  for (const s of cands.slice(1)) if (s.gpuMed > best.gpuMed || (!Number.isFinite(best.gpuMed) && Number.isFinite(s.gpuMed))) best = s;
  return best.swap;
}

// Per swap chain (after warm-up): frame count, typical refresh, dominant present mode and mode changes.
// BGFX VPX presents the playfield, DMD and backglass on separate swap chains.
function drillSwapStats(csv, from) {
  const by = new Map();
  for (let r = 0; r < csv.n; r++) {
    if (!(csv.CPUStartTime[r] >= from)) continue;
    let s = by.get(csv.swap[r]);
    if (!s) { s = { swap: csv.swap[r], frames: 0, dts: [], gpus: [], modes: new Map(), changes: 0, prev: null }; by.set(csv.swap[r], s); }
    s.frames++;
    if (Number.isFinite(csv.GPUBusy[r])) s.gpus.push(csv.GPUBusy[r]);
    if (csv.DisplayedTime[r] > 0) s.dts.push(csv.DisplayedTime[r]);
    const m = csv.mode[r];
    s.modes.set(m, (s.modes.get(m) || 0) + 1);
    if (m && s.prev && m !== s.prev) s.changes++;
    s.prev = m;
  }
  return [...by.values()].map(s => {
    const refreshMs = drillMedian(s.dts);
    const mode = [...s.modes.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || '';
    return { swap: s.swap, frames: s.frames, refreshMs, hz: 1000 / refreshMs, mode, changes: s.changes, gpuMed: drillMedian(s.gpus) };
  }).sort((a, b) => b.frames - a.frames);
}

// Find the whole-second warm-up whose post-warm-up frame count equals the stored PresentMonFrameCount.
function drillInferWarmup(csv, rec) {
  const target = rec?.PresentMonFrameCount;
  const t0 = csv.CPUStartTime[0];
  if (!Number.isFinite(target) || !Number.isFinite(t0)) return null;
  const { swap } = drillPickSwap(csv, rec, -Infinity);
  const times = [];
  for (let r = 0; r < csv.n; r++) if (csv.swap[r] === swap && Number.isFinite(csv.CPUStartTime[r])) times.push(csv.CPUStartTime[r]);
  times.sort((a, b) => a - b);
  const countFrom = limit => {
    let lo = 0, hi = times.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (times[mid] < limit) lo = mid + 1; else hi = mid; }
    return times.length - lo;
  };
  let best = null;
  for (let d = 0; d <= DRILL_WARMUP_MAX_SEC; d++) {
    const err = Math.abs(countFrom(t0 + d * 1000) - target);
    // Ties happen when no frames were presented for a while (loading); prefer the value nearest the default.
    if (!best || err < best.err || (err === best.err && Math.abs(d - DRILL_WARMUP_SEC) < Math.abs(best.sec - DRILL_WARMUP_SEC))) best = { sec: d, err };
  }
  return { sec: best.sec, source: best.err === 0 ? 'inferred from frame count' : 'inferred, approximate' };
}

// Priority: database (CfgTrimStartSeconds) > the run's TXT log > inference > the 30 s default.
function drillResolveWarmup(csv, rec, cfg, logSec) {
  if (Number.isFinite(cfg?.TrimStartSeconds)) return { sec: cfg.TrimStartSeconds, source: 'database' };
  if (Number.isFinite(logSec)) return { sec: logSec, source: 'TXT log' };
  return drillInferWarmup(csv, rec) || { sec: DRILL_WARMUP_SEC, source: 'assumed default' };
}

function drillScore(eff, cfg) {
  const zero = Math.max(cfg.ZeroScorePct, cfg.BandD * 1.5);
  const pts = [[0, 100], [cfg.BandA, 90], [cfg.BandB, 80], [cfg.BandC, 70], [cfg.BandD, 60], [zero, 0]];
  if (!(eff > 0)) return 100;
  for (let i = 1; i < pts.length; i++) {
    const [x1, y1] = pts[i];
    if (eff <= x1) {
      const [x0, y0] = pts[i - 1];
      return y0 + (y1 - y0) * (eff - x0) / (x1 - x0);
    }
  }
  return 0;
}

function drillLetter(score) {
  return score >= 90 ? 'A' : score >= 80 ? 'B' : score >= 70 ? 'C' : score >= 60 ? 'D' : 'F';
}

// Mirrors VPinPerfMon's stall classifier (BlameVersion 4). `p` is the stalled frame's position in the swap chain;
// `lo` is the position of the first graded frame (earlier frames are warm-up and are never looked at).
// A missing value on any frame counts as 0, as in the C# code.
const DRILL_CAUSES = {
  cpu: 'CPU', gpu: 'GPU', modeSwitch: 'Present-mode switch', appWait: 'App waiting / OS', display: 'Display / compositor',
};
function drillCause(csv, chain, p, refresh, cfg, lo) {
  const num = x => (Number.isFinite(x) ? x : 0);
  const at = (arr, q) => (q >= 0 && q < chain.length ? num(arr[chain[q]]) : 0);
  const modeAt = q => (q >= lo && q < chain.length ? csv.mode[chain[q]] : null);
  const cpu = Math.max(at(csv.CPUBusy, p), at(csv.CPUBusy, p + 1));
  const gpu = Math.max(at(csv.GPUBusy, p), at(csv.GPUBusy, p + 1));
  const nextFt = at(csv.FrameTime, p + 1);
  const ft = Math.max(at(csv.FrameTime, p), nextFt);
  const mode = csv.mode[chain[p]];
  const modeSwitch = [p - 1, p + 1].some(q => modeAt(q) != null && modeAt(q) !== mode);
  const lim = 1.5 * refresh;
  let key = 'display';
  if (cpu >= lim && !(gpu > cpu)) key = 'cpu';
  else if (gpu >= cfg.BlameGpuSaturation * refresh && ft >= lim) key = 'gpu';
  else if (modeSwitch) key = 'modeSwitch';
  else if (nextFt >= lim) key = 'appWait';
  return { cpu, gpu, nextFt, mode, causeKey: key, cause: DRILL_CAUSES[key] };
}

// Report-side three-way call: is the run limited by the GPU, the CPU, or by something an upgrade won't fix?
// Needs both facts: stall causes alone or GPU load alone can mislead (high GPU load with CPU-type hitches is common).
function drillLimiterCall(counts, gpuLoadPct, gpuP99Pct, cfg, bands = [50, 85]) {
  const total = counts.cpu + counts.gpu + counts.modeSwitch + counts.appWait + counts.display;
  const detail = `${total} stalls: ${counts.cpu} CPU, ${counts.gpu} GPU, ${total - counts.cpu - counts.gpu} other`;
  if (total < cfg.BlameMinStalls) return { call: 'unknown', label: 'Too few stalls to call', detail };
  const hasLoad = Number.isFinite(gpuLoadPct);
  const level = !hasLoad ? 'unknown' : gpuLoadPct < bands[0] ? 'low' : gpuLoadPct <= bands[1] ? 'moderate' : 'high';
  // Mostly present-mode switch / display / app-waiting stalls: not fixed by a CPU or GPU upgrade.
  if (counts.cpu + counts.gpu < total / 2) return { call: 'neither', label: 'Neither (not CPU/GPU-caused)', detail };
  if (level === 'high' && (counts.gpu > 0 || gpuP99Pct >= 100) && counts.gpu >= counts.cpu) return { call: 'gpu', label: 'GPU-limited', detail };
  if (level === 'low' && counts.cpu > counts.gpu * 2) return { call: 'cpu', label: 'CPU-limited', detail };
  return { call: 'unknown', label: 'Unclear', detail };
}

// Mirrors the VPinPerfMon v2 stutter grade (see DATABASE_COLUMNS.md); rec supplies the stored
// swap chain / fixed refresh when available.
function drillAnalyze(csv, rec, cfgIn, logSec, forceSwap) {
  const cfg = { ...DRILL_FALLBACK_CFG, ...(cfgIn || {}) };
  const st = csv.CPUStartTime;
  const dt = csv.DisplayedTime;
  const t0 = st[0];
  if (!Number.isFinite(t0)) throw new Error('first row has no CPUStartTime');
  const warmup = drillResolveWarmup(csv, rec, cfg, logSec);
  const warmEnd = t0 + warmup.sec * 1000;

  const { swap, counts } = drillPickSwap(csv, rec, warmEnd, forceSwap);
  if (swap == null) throw new Error(`no frames after the ${warmup.sec} s warm-up (${warmup.source})`);

  const chain = [];
  for (let r = 0; r < csv.n; r++) if (csv.swap[r] === swap) chain.push(r);
  const sel = chain.filter(r => st[r] >= warmEnd);
  // Mirrors VPinPerfMon StutterGrading.Grade: NA/<=0 DisplayedTime is missing; the tail trim pops
  // valid frames from the end until their DisplayedTime sum reaches TrimEndSeconds. Values are float32 there.
  const dtf = r => Math.fround(dt[r]);
  const valid = sel.filter(r => dt[r] > 0);
  const graded = valid.slice();
  const limitMs = cfg.TrimEndSeconds * 1000;
  let trimmedMs = 0, tailStart = Infinity;
  if (limitMs > 0) {
    while (graded.length && trimmedMs < limitMs) { const r = graded.pop(); trimmedMs += dtf(r); tailStart = st[r]; }
  }
  const frames = graded.length;
  let total = 0;
  for (const r of graded) total += dtf(r);
  const duration = total / 1000;

  let refresh = frames ? dtf(graded.slice().sort((a, b) => dt[a] - dt[b])[frames >> 1]) : NaN;
  let refreshSource = 'auto';
  if (rec && rec.GradeRefreshSource && rec.GradeRefreshSource !== 'auto' && Number.isFinite(rec.GradeRefreshMs)) {
    refresh = rec.GradeRefreshMs;
    refreshSource = rec.GradeRefreshSource;
  }

  const pos = new Map(chain.map((r, p) => [r, p]));
  const lo = graded.length ? pos.get(graded[0]) : 0;
  const stallCounts = { cpu: 0, gpu: 0, modeSwitch: 0, appWait: 0, display: 0 };
  let cpuMs = 0, gpuMs = 0;
  const winMs = cfg.ClusterWindowSec * 1000;
  const winCounts = new Map();
  const winFrames = new Map();
  const misses = [];
  let doubled = 0, triple = 0, maxRatio = 0, weightedSum = 0, cum = 0;
  for (const r of graded) {
    const w = Math.floor(cum / winMs);
    if (!winFrames.has(w)) winFrames.set(w, [r, r]); else winFrames.get(w)[1] = r;
    const ratio = dtf(r) / refresh;
    if (ratio >= cfg.DoubledRefreshes) {
      const isTriple = ratio >= cfg.TripleRefreshes;
      const refreshes = drillRound(ratio);
      if (isTriple) { triple++; weightedSum += cfg.TripleWeight * (refreshes - 1); } else { doubled++; weightedSum += 1; }
      if (ratio > maxRatio) maxRatio = ratio;
      winCounts.set(w, (winCounts.get(w) || 0) + 1);
      const why = drillCause(csv, chain, pos.get(r), refresh, cfg, lo);
      stallCounts[why.causeKey]++;
      if (why.causeKey === 'cpu') cpuMs += dtf(r) - refresh;
      else if (why.causeKey === 'gpu') gpuMs += dtf(r) - refresh;
      misses.push({ r, ratio, refreshes, triple: isTriple, win: w, ms: dt[r], ...why });
    }
    cum += dtf(r);
  }
  // Present-mode changes inside the graded window only (load / exit switches are excluded).
  const modeChanges = [];
  for (let p = 1; p < chain.length; p++) {
    const r = chain[p], prev = csv.mode[chain[p - 1]];
    if (!csv.mode[r] || !prev || csv.mode[r] === prev) continue;
    if (st[r] >= warmEnd && st[r] < tailStart) modeChanges.push({ r, from: prev, to: csv.mode[r] });
  }
  const missCount = misses.length;
  // Blame: millisecond-weighted CPU vs GPU share of the app-caused stalls; NULL below BlameMinStalls.
  let blameCpuPct = null, blameGpuPct = null;
  if (stallCounts.cpu + stallCounts.gpu >= cfg.BlameMinStalls && cpuMs + gpuMs > 0) {
    blameCpuPct = 100 * cpuMs / (cpuMs + gpuMs);
    blameGpuPct = 100 - blameCpuPct;
  }
  // GPU load: median GPUBusy of graded frames with GPUBusy > 0, as a percent of the refresh interval.
  const gpuBusyVals = graded.map(r => csv.GPUBusy[r]).filter(v => v > 0);
  const gpuLoadPct = gpuBusyVals.length && refresh > 0 ? drillMedian(gpuBusyVals) / refresh * 100 : NaN;
  const gpuP99Pct = gpuBusyVals.length && refresh > 0 ? drillPercentile(gpuBusyVals, 0.99) / refresh * 100 : NaN;
  const longest = missCount ? drillRound(maxRatio) : 1;
  const weighted = frames ? weightedSum / frames * 100 : 0;
  let peak = 0, peakWin = null;
  for (const [w, c] of winCounts) if (c > peak || (c === peak && w < peakWin)) { peak = c; peakWin = w; }
  const windows = Math.max(1, duration / cfg.ClusterWindowSec);
  const clusterRatio = missCount ? peak / (missCount / windows) : 0;
  const multiplier = peak >= cfg.ClusterMinPeak && missCount >= cfg.ClusterMinEvents
    ? 1 + cfg.ClusterWeight * Math.min(Math.max(clusterRatio - 1, 0), cfg.ClusterMaxExcess) : 1;
  const effective = weighted * multiplier;
  let score = drillScore(effective, cfg);
  let capped = false;
  const capFloor = { B: 90, C: 80, D: 70, F: 60 }[cfg.TripleCapGrade];
  if (capFloor != null && triple >= cfg.TripleCapMinCount && score >= capFloor) { score = capFloor - 0.1; capped = true; }
  const gradeable = frames >= cfg.MinFrames;
  const peakFrames = peakWin != null ? winFrames.get(peakWin) : null;

  return {
    cfg, t0, warmEnd, warmupSec: warmup.sec, warmupSource: warmup.source, swap, swapChains: counts.size, chain, graded, refresh, refreshSource,
    framesAfterWarmup: sel.length, trimmed: valid.length - frames, trimmedSec: trimmedMs / 1000, tailStart,
    frames, missingFrames: sel.length - valid.length, duration,
    doubled, triple, longest, weighted, misses, modeChanges, stallCounts,
    blameCpuPct, blameGpuPct, gpuLoadPct, gpuP99Pct,
    limiter: drillLimiterCall(stallCounts, gpuLoadPct, gpuP99Pct, cfg, typeof drillGpuBands !== 'undefined' ? drillGpuBands : undefined),
    peak, peakWin, clusterRatio, multiplier, effective, swapStats: drillSwapStats(csv, warmEnd),
    score: gradeable ? score : null, capped, grade: gradeable ? drillLetter(score) : null,
    worst: peakFrames ? { from: st[peakFrames[0]], to: st[peakFrames[1]] + dt[peakFrames[1]] } : null,
  };
}
//#endregion drill-core

const drillEl = id => document.getElementById(id);
const drillFiles = new Map();   // lower-case file name -> File or FileSystemFileHandle
const drillCharts = {};
let drillRid = null;
let drillToken = 0;
let drillSyncing = false;
let drillFolderLabel = '';
let drillLast = null;
let drillCtx = null;
let drillSwapPick = null;
let drillUnderlay = true;

function drillStatus(html, kind = '') {
  const el = drillEl('drillStatus');
  el.className = 'drill-status' + (kind ? ' ' + kind : '');
  el.innerHTML = html;
}

function drillAlpha(color, a) {
  const c = String(color || '').trim();
  const rgb = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(c);
  if (rgb) return `rgba(${rgb[1]},${rgb[2]},${rgb[3]},${a})`;
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c);
  if (!m) return `rgba(128,128,128,${a})`;
  const h = m[1].length === 3 ? m[1].split('').map(ch => ch + ch).join('') : m[1];
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

const drillFmt = (v, d = 2) => (Number.isFinite(v) ? v.toFixed(d) : '&ndash;');
const drillInt = v => (Number.isFinite(v) ? Math.round(v).toLocaleString() : '&ndash;');
const drillGradeTag = g => (g ? `<span class="drill-grade" style="background:${gradeColors[g] || colors.muted}">${g}</span>` : '&ndash;');
const drillHasFile = rec => !!(rec && rec.RawFile && drillFiles.has(rec.RawFile.toLowerCase()));

function drillRefreshRuns() {
  const select = drillEl('drillRun');
  if (!select) return;
  const rows = (currentState?.rows || rawRecords).filter(r => r.RawFile);
  const current = drillRid != null ? rawRecords[drillRid] : null;
  if (current && !rows.includes(current)) rows.push(current);
  rows.sort((a, b) => String(a.Table).localeCompare(String(b.Table)) || String(b.EntryDateTime).localeCompare(String(a.EntryDateTime)));
  const available = rows.filter(drillHasFile).length;
  // ● = file connected now; ○ = its CSV was in the logs folder when the report was built (connect it to load)
  const mark = r => (drillHasFile(r) ? '&#9679; ' : !drillFiles.size && r.LogOnDisk === 1 ? '&#9675; ' : '');
  select.innerHTML = `<option value="">Choose a run&hellip; (${rows.length} in current filters)</option>` + rows.map(r =>
    `<option value="${r._i}">${mark(r)}${escapeHtml(r.Table)} &mdash; ${escapeHtml(r.EntryDateTime)}` +
    `${r.StutterGrade ? ` &mdash; grade ${escapeHtml(r.StutterGrade)}` : ''}</option>`).join('');
  select.value = drillRid != null ? String(drillRid) : '';
  const onDisk = rows.filter(r => r.LogOnDisk === 1).length;
  drillEl('drillCount').textContent = drillFiles.size
    ? `${drillFolderLabel ? drillFolderLabel + ': ' : ''}${drillFiles.size} CSV file(s) connected · ● ${available} of these runs available`
    : drillLogs.checked && drillLogs.found ? `○ ${onDisk} of these runs had their CSV in the logs folder at build time` : '';
}

function drillExpectedPath(rec) {
  if (!drillLogs.dir || !rec?.RawFile) return '';
  const sep = drillLogs.dir.includes('\\') ? '\\' : '/';
  return drillLogs.dir.replace(/[\\/]+$/, '') + sep + rec.RawFile;
}

async function drillCopyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (err) { /* fall back below */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (err) { /* ignore */ }
  ta.remove();
  return ok;
}

function drillRenderLogsHint() {
  const el = drillEl('drillLogsHint');
  if (!el || !drillLogs.dir) return;
  const from = drillLogs.source === 'config' ? 'PERF_LOGS_DIR in report_config.py' : 'the folder recorded in the database';
  const check = drillLogs.checked
    ? ` &middot; ${drillLogs.found} of ${drillLogs.total} runs had their CSV there when this report was built.`
    : ' &middot; this folder wasn&rsquo;t found on the PC that built the report, so it may differ here.';
  const tip = drillLogs.source !== 'config' && !(drillLogs.checked && drillLogs.found)
    ? ' To point the report elsewhere, rebuild with <code>--perf-logs</code> or set PERF_LOGS_DIR.' : '';
  const folders = [drillLogs.dir, ...(drillLogs.alternates || [])];
  const paths = folders.map((path, i) =>
    `<div>${i === 0 ? 'Expected logs folder' : 'Alternate logs folder'}: <code>${escapeHtml(path)}</code>` +
    `<button type="button" data-drill-copy-path="${i}">Copy path</button></div>`).join('');
  el.innerHTML = paths + `(${from})${check} Tip: paste a path into the folder picker&rsquo;s address bar, then connect that folder.${tip}`;
  el.hidden = false;
  el.querySelectorAll('[data-drill-copy-path]').forEach(button => {
    button.addEventListener('click', async () => {
      const ok = await drillCopyText(folders[Number(button.dataset.drillCopyPath)]);
      button.textContent = ok ? 'Copied' : 'Copy failed';
      setTimeout(() => { button.textContent = 'Copy path'; }, 1500);
    });
  });
}

async function drillOpenRecord(rid, scroll) {
  drillRid = rid;
  const rec = rawRecords[rid];
  drillRefreshRuns();
  if (scroll) drillEl('drillSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (!rec) return;
  if (!rec.RawFile) {
    drillStatus('This run has no raw CSV path (FileRawDataPath) recorded in the database.', 'warn');
    return;
  }
  const entry = drillFiles.get(rec.RawFile.toLowerCase());
  if (!entry) {
    const expected = drillExpectedPath(rec);
    const missing = rec.LogOnDisk === 0 ? ' It wasn&rsquo;t in that folder when this report was built.' : '';
    drillStatus(`${escapeHtml(rec.Table)} needs <code>${escapeHtml(expected || rec.RawFile)}</code> &mdash; connect your VPinPerfMon logs folder, or open / drop that CSV.${missing}`, 'warn');
    return;
  }
  await drillLoad(entry, rec);
}

async function drillLoad(entry, rec) {
  const token = ++drillToken;
  drillSwapPick = null;
  try {
    const file = entry.getFile ? await entry.getFile() : entry;
    drillStatus(`Reading <code>${escapeHtml(file.name)}</code> (${(file.size / 1048576).toFixed(1)} MB)&hellip;`);
    const text = await file.text();
    if (token !== drillToken) return;
    const csv = drillParseCsv(text);
    const logSec = await drillReadLogWarmup(file.name);
    if (token !== drillToken) return;
    drillCtx = { csv, rec, file, logSec };
    const res = drillAnalyze(csv, rec, rec ? drillCfgs[rec.CfgIdx] : drillCfgs[0], logSec, drillSwapPick);
    drillRender(csv, res, rec, file);
  } catch (err) {
    if (token !== drillToken) return;
    drillStatus(`Could not analyze this capture: ${escapeHtml(err.message || err)}`, 'error');
  }
}

// Re-run the analysis on the already-loaded CSV after the user picks another swap chain.
function drillReanalyze() {
  if (!drillCtx) return;
  const { csv, rec, file, logSec } = drillCtx;
  try {
    drillRender(csv, drillAnalyze(csv, rec, rec ? drillCfgs[rec.CfgIdx] : drillCfgs[0], logSec, drillSwapPick), rec, file);
  } catch (err) {
    drillStatus(`Could not analyze this swap chain: ${escapeHtml(err.message || err)}`, 'error');
  }
}

// The VPinPerfMon console log saved beside the CSV records the warm-up actually used.
async function drillReadLogWarmup(csvName) {
  const entry = drillFiles.get(csvName.toLowerCase().replace(/\.csv$/, '.txt'));
  if (!entry) return NaN;
  try {
    const file = entry.getFile ? await entry.getFile() : entry;
    return drillParseLog(await file.slice(0, 65536).text());
  } catch {
    return NaN;
  }
}

// Returns the number of CSVs added; matching TXT logs are kept too.
function drillAddFiles(list) {
  let added = 0;
  for (const f of list) {
    if (/\.csv$/i.test(f.name)) { drillFiles.set(f.name.toLowerCase(), f); added++; }
    else if (/\.txt$/i.test(f.name)) drillFiles.set(f.name.toLowerCase(), f);
  }
  return added;
}

// Drop / open: one known file opens its run; an unknown single file is analyzed standalone.
function drillUseFiles(files) {
  const csvs = [...files].filter(f => /\.csv$/i.test(f.name));
  if (!csvs.length) { drillStatus('Please choose a PresentMon .csv file (optionally with its .txt log).', 'warn'); return; }
  drillAddFiles(files);
  const match = csvs.map(f => rawRecords.find(r => r.RawFile && r.RawFile.toLowerCase() === f.name.toLowerCase())).find(Boolean);
  const current = drillRid != null ? rawRecords[drillRid] : null;
  if (drillHasFile(current) && csvs.some(f => f.name.toLowerCase() === current.RawFile.toLowerCase())) drillOpenRecord(drillRid);
  else if (match) drillOpenRecord(match._i);
  else {
    drillRid = null;
    drillRefreshRuns();
    drillLoad(csvs[0], null);
  }
}

async function drillConnectFolder() {
  if (window.showDirectoryPicker) {
    try {
      const dir = await window.showDirectoryPicker({ id: 'vpinperfmon-logs', mode: 'read' });
      await drillUseDirectory(dir);
      drillSaveHandle(dir);
      return;
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      // Not allowed here (e.g. some file:// setups) -- fall back to the classic folder input.
    }
  }
  drillEl('drillFolderInput').click();
}

async function drillUseDirectory(dir) {
  drillFiles.clear();
  drillStatus(`Scanning <code>${escapeHtml(dir.name)}</code>&hellip;`);
  for await (const [name, handle] of dir.entries()) {
    if (handle.kind === 'file' && /\.(csv|txt)$/i.test(name)) drillFiles.set(name.toLowerCase(), handle);
  }
  drillAfterConnect(dir.name);
}

function drillAfterConnect(label) {
  drillFolderLabel = label;
  drillEl('drillReconnectBtn').hidden = true;
  const known = rawRecords.filter(drillHasFile).length;
  drillRefreshRuns();
  const current = drillRid != null ? rawRecords[drillRid] : null;
  if (drillHasFile(current)) { drillOpenRecord(drillRid); return; }
  drillStatus(`Connected <code>${escapeHtml(label)}</code>: ${drillFiles.size} CSV file(s), ${known} of the report's runs found. ` +
    'Click a chart point or pick a run (● = available).', known ? 'ok' : 'warn');
}

// Remember the picked folder (File System Access handle) so a regenerated report can reconnect in one click.
function drillDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('vpin-perf-drilldown', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('handles');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function drillSaveHandle(dir) {
  try {
    const db = await drillDb();
    db.transaction('handles', 'readwrite').objectStore('handles').put(dir, 'logs');
  } catch (err) { /* persistence is optional */ }
}

async function drillOfferReconnect() {
  try {
    if (!window.showDirectoryPicker || !window.indexedDB) return;
    const db = await drillDb();
    const dir = await new Promise((resolve, reject) => {
      const req = db.transaction('handles').objectStore('handles').get('logs');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    if (!dir) return;
    const btn = drillEl('drillReconnectBtn');
    btn.textContent = `Reconnect "${dir.name}"`;
    btn.hidden = false;
    btn.onclick = async () => {
      try {
        if ((await dir.requestPermission({ mode: 'read' })) !== 'granted') return;
        await drillUseDirectory(dir);
      } catch (err) {
        drillStatus(`Could not reconnect: ${escapeHtml(err.message || err)}`, 'error');
      }
    };
  } catch (err) { /* persistence is optional */ }
}

const DRILL_MODE_COLOR = '#b07cff';
const DRILL_MODE_INFO = {
  'Hardware: Independent Flip': 'Best case: the game owns the screen and flips frames straight to it; Windows is not compositing anything on top.',
  'Hardware Composed: Independent Flip': 'Also direct: frames go to the screen, but Windows may blend a small overlay (such as a cursor or notification) in hardware without a copy. Same performance class as Independent Flip.',
  'Hardware: Legacy Flip': 'Older exclusive-fullscreen style flip straight to the display.',
  'Hardware: Legacy Copy to front buffer': 'Older fullscreen path that copies each frame to the display buffer.',
  'Composed: Flip': 'The Windows compositor (DWM) takes each frame and blends it with the desktop before display. Slightly more latency and more chance of hitches.',
  'Composed: Copy with GPU GDI': 'Compositor path where the frame is copied by the GPU (windowed GDI-style apps).',
  'Composed: Copy with CPU GDI': 'Compositor path where the frame is copied by the CPU; slowest and rare.',
  'Composed: Composition Atlas': 'Frame is drawn through the compositor into a shared surface (typical of UWP / XAML content).',
};

// When the game was played: the capture start stamped in the file name (local time) is the most
// accurate; fall back to the database entry time (end of run), then the file's modified time.
function drillPlayedAt(file, rec) {
  const m = /_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:_|\.)/.exec(file?.name || '');
  if (m) return { text: `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`, note: 'capture start (from file name)' };
  if (rec?.EntryDateTime) return { text: String(rec.EntryDateTime), note: 'database entry time' };
  if (file?.lastModified) {
    const d = new Date(file.lastModified), p = n => String(n).padStart(2, '0');
    return { text: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`, note: 'file modified time' };
  }
  return null;
}

// Overall CPU / GPU utilization (avg with min / max) from the database record, when present.
function drillLoadChips(rec) {
  if (!rec) return [];
  const pct = v => (v != null && Number.isFinite(Number(v)) ? `${Number(v).toFixed(1)}%` : '&ndash;');
  const chip = (name, avg, min, max, tip) => (avg != null && Number.isFinite(Number(avg))
    ? [[`${pct(min)} / ${pct(avg)} / ${pct(max)}`, `${name} (min / avg / max)`, tip]] : []);
  return [
    ...chip('CPU', rec.CpuAverage, rec.CpuMin, rec.CpuMax, 'Total processor utilization of the whole machine (not just the game), sampled about once per second.'),
    ...chip('GPU', rec.GpuAverage, rec.GpuMin, rec.GpuMax, 'Utilization of GPU 0 from NVIDIA NVML, sampled about once per second.'),
  ];
}

// GPU load bands (percent of the refresh interval); the report injects drillGpuBands from GPU_LOAD_BANDS.
const drillBands = () => (typeof drillGpuBands !== 'undefined' ? drillGpuBands : [50, 85]);
const drillLoadLabel = pct => (!Number.isFinite(pct) ? '' : pct < drillBands()[0] ? 'headroom' : pct <= drillBands()[1] ? 'moderate' : 'high');

// Lead with GPU load ("is the graphics card the limit?"); blame is secondary and shown with the stall counts behind it.
function drillBlameChips(res, rec) {
  const c = res.stallCounts, minStalls = res.cfg.BlameMinStalls;
  const chips = [];
  const stored = Number(rec?.GpuBusyPctOfRefresh);
  const load = Number.isFinite(stored) ? stored : res.gpuLoadPct;
  if (Number.isFinite(load)) {
    chips.push([`${load.toFixed(0)}% \u00b7 ${drillLoadLabel(load)}`, 'GPU load (median GPU busy as % of refresh)',
      `Median GPU busy time per frame as a percent of the refresh interval. Under ${drillBands()[0]}% = headroom, ${drillBands()[0]}-${drillBands()[1]}% = moderate, over ${drillBands()[1]}% = high. Bands are set by GPU_LOAD_BANDS in report_config.py (raise them for VR).`]);
  }
  const enough = c.cpu + c.gpu >= minStalls;
  const legacy = rec && rec.BlameLegacy === 1;
  const blameText = enough && res.blameCpuPct != null
    ? `CPU ${res.blameCpuPct.toFixed(0)}% / GPU ${res.blameGpuPct.toFixed(0)}%` : 'not enough app-caused stalls';
  chips.push([blameText, `blame of ${c.cpu + c.gpu} CPU/GPU stalls (${c.cpu} CPU \u00b7 ${c.gpu} GPU)${legacy ? ' \u00b7 legacy formula in DB' : ''}`,
    `The CPU-vs-GPU share of the stalls the app itself caused, weighted by milliseconds over the refresh. Present-mode-switch, display/compositor and app-waiting stalls are not blamed on either side. Needs at least ${minStalls} CPU+GPU stalls; otherwise it is not reported.`]);
  const lim = res.limiter;
  if (lim && lim.call !== 'unknown') chips.push([lim.label, 'likely limiter', `${lim.detail}. Combines GPU load with the stall causes; a run is not called CPU-bound just because GPU load is low.`]);
  return chips;
}

const DRILL_REC_MIN_CHANGES = 4;
const DRILL_REC_MIN_CPU = 25;

// Screen-recording sessions (e.g. PinUP Popper "record screen") show repeated present-mode changes plus high CPU.
function drillRecordingSuspect(res, rec) {
  const cpu = Number(rec?.CpuAverage);
  return res.modeChanges.length >= DRILL_REC_MIN_CHANGES && Number.isFinite(cpu) && cpu >= DRILL_REC_MIN_CPU;
}

const drillBandsPlugin = {
  id: 'drillBands',
  beforeDatasetsDraw(chart, args, opts) {
    const { ctx, chartArea: a, scales: { x, y } } = chart;
    ctx.save();
    ctx.beginPath();
    ctx.rect(a.left, a.top, a.width, a.height);
    ctx.clip();
    ctx.font = '10px sans-serif';
    (opts.bands || []).forEach(b => {
      const x0 = Math.max(a.left, x.getPixelForValue(b.from));
      const x1 = Math.min(a.right, x.getPixelForValue(b.to));
      if (!(x1 > x0)) return;
      ctx.fillStyle = b.color;
      ctx.fillRect(x0, a.top, Math.max(1, x1 - x0), a.height);
      if (b.label && x1 - x0 > 60) { ctx.fillStyle = colors.muted; ctx.fillText(b.label, x0 + 4, a.top + 12); }
    });
    ctx.strokeStyle = drillAlpha(colors.danger, 0.55);
    ctx.lineWidth = 1;
    (opts.marks || []).forEach(v => {
      const px = x.getPixelForValue(v);
      if (px < a.left || px > a.right) return;
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
    });
    ctx.strokeStyle = drillAlpha(DRILL_MODE_COLOR, 0.6);
    (opts.modeMarks || []).forEach(v => {
      const px = x.getPixelForValue(v);
      if (px < a.left || px > a.right) return;
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
    });
    ctx.setLineDash([4, 4]);
    (opts.lines || []).forEach(l => {
      const py = y.getPixelForValue(l.value);
      if (py < a.top || py > a.bottom) return;
      ctx.strokeStyle = colors.muted;
      ctx.fillStyle = colors.muted;
      ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke();
      ctx.fillText(l.label, a.right - ctx.measureText(l.label).width - 4, py - 3);
    });
    ctx.restore();
  },
};

function drillSync({ chart }) {
  if (drillSyncing) return;
  drillSyncing = true;
  const { min, max } = chart.scales.x;
  for (const other of [drillCharts.timeline, drillCharts.busy]) {
    if (other && other !== chart) other.zoomScale('x', { min, max }, 'none');
  }
  drillSyncing = false;
}

function drillZoomTo(x) {
  for (const c of [drillCharts.timeline, drillCharts.busy]) c?.zoomScale('x', { min: Math.max(0, x - 1), max: x + 1 }, 'none');
  drillEl('drillTimeline').closest('.card').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function drillLineOptions(ylabel, xMax, overlay, tooltipLabel) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true, spanGaps: true,
    interaction: { mode: 'nearest', axis: 'x', intersect: false },
    plugins: {
      decimation: { enabled: true, algorithm: 'min-max' },
      legend: { labels: { color: colors.text, boxWidth: 12 } },
      drillBands: overlay,
      tooltip: { callbacks: { title: items => `t = ${items[0].parsed.x.toFixed(3)} s`, label: tooltipLabel } },
      zoom: {
        limits: { x: { min: 0, max: xMax, minRange: 0.05 } },
        pan: { enabled: true, mode: 'x', onPanComplete: drillSync },
        zoom: { wheel: { enabled: true, speed: 0.1 }, pinch: { enabled: true }, mode: 'x', onZoomComplete: drillSync },
      },
    },
    scales: {
      x: { type: 'linear', min: 0, max: xMax, title: { display: true, text: 'Seconds since capture start', color: colors.muted },
           grid: { color: colors.border }, ticks: { color: colors.muted } },
      y: { beginAtZero: true, title: { display: true, text: ylabel, color: colors.muted },
           grid: { color: colors.border }, ticks: { color: colors.muted } },
    },
  };
}

function drillRender(csv, res, rec, file) {
  Object.values(drillCharts).forEach(c => c.destroy());
  drillLast = { csv, res, rec, file };
  drillEl('drillResults').hidden = false;
  const st = csv.CPUStartTime, dt = csv.DisplayedTime;
  const sec = ms => (ms - res.t0) / 1000;
  const refresh = res.refresh;

  drillEl('drillTitle').innerHTML = rec
    ? `${escapeHtml(rec.Table)} <span class="muted">&middot; ${escapeHtml(rec.EntryDateTime)} &middot; ${escapeHtml(rec.EmuDisplay ?? '')} &middot; <code>${escapeHtml(file.name)}</code></span>`
    : `<code>${escapeHtml(file.name)}</code> <span class="muted">&middot; not in this report &mdash; standalone analysis</span>`;

  // ---- summary chips
  const causes = {};
  res.misses.forEach(m => { causes[m.cause] = (causes[m.cause] || 0) + 1; });
  const causeText = Object.entries(causes).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c} (${n}x)`).join(' · ') || 'no stalls';
  const worstMiss = res.misses.reduce((w, m) => (!w || m.ratio > w.ratio ? m : w), null);
  const played = drillPlayedAt(file, rec);
  const chips = [
    ...(played ? [[escapeHtml(played.text), 'Start Time', `Approximate: ${played.note}`]] : []),
    [`${drillGradeTag(res.grade)} ${drillFmt(res.score, 1)}`, `recomputed grade${rec?.StutterGrade ? ` (DB: ${rec.StutterGrade} ${drillFmt(rec.StutterGradeScore, 1)})` : ''}${res.capped ? ' · capped' : ''}`],
    [drillInt(res.frames), `graded frames · ${drillFmt(res.duration, 1)} s`],
    [`${drillFmt(1000 / refresh, 1)} Hz`, `refresh (${drillFmt(refresh, 3)} ms, ${escapeHtml(res.refreshSource)})`],
    [drillInt(res.misses.length), `missed refreshes (2×: ${res.doubled} · 3×+: ${res.triple})`],
    [worstMiss ? `${drillFmt(worstMiss.ms, 1)} ms` : '&ndash;', worstMiss ? `longest stall (${worstMiss.refreshes} refreshes) at ${drillFmt(sec(st[worstMiss.r]), 1)} s` : 'longest stall'],
    [`${drillFmt(res.effective, 3)}%`, `effective miss % (cluster ×${drillFmt(res.multiplier, 2)}, peak ${res.peak} per ${res.cfg.ClusterWindowSec} s)`],
    [drillInt(res.modeChanges.length), 'present-mode changes', 'Present-mode changes while graded (after warm-up, before the trimmed tail). Usually caused by an overlay, popup or window focus change.'],
    ...drillLoadChips(rec),
    ...drillBlameChips(res, rec),
    ...(drillRecordingSuspect(res, rec) ? [['Recording?', 'possible screen-recording session', `Repeated present-mode changes (${res.modeChanges.length}) with high average CPU (${drillFmt(rec.CpuAverage, 1)}%) is the typical signature of a screen recording (for example Popper's record screen) running during the capture, not normal play.`]] : []),
    [escapeHtml(causeText), 'likely stall causes (number of stalls)'],
  ];
  drillEl('drillChips').innerHTML = chips.map(([v, l, tip]) => `<div${tip ? ` title="${escapeHtml(tip)}"` : ''}><b>${v}</b>${l}</div>`).join('');

  // ---- swap chain selector + underlay toggle (only when the capture has several chains)
  const chainBar = drillEl('drillChainBar');
  chainBar.hidden = res.swapStats.length < 2;
  if (!chainBar.hidden) {
    const playfield = drillLikelyPlayfield(res.swapStats);
    const describe = s => `${s.swap || '(none)'} \u00b7 ${Number.isFinite(s.hz) ? s.hz.toFixed(0) : '?'} Hz \u00b7 ${s.frames.toLocaleString()} frames \u00b7 ${s.mode || '?'}${s.changes ? ` \u00b7 ${s.changes} mode changes` : ''}${s.swap === playfield ? ' \u00b7 likely playfield (highest GPU load)' : ''}`;
    drillEl('drillChain').innerHTML = res.swapStats.map(s => `<option value="${escapeHtml(s.swap)}"${s.swap === res.swap ? ' selected' : ''}>${escapeHtml(describe(s))}</option>`).join('');
    drillEl('drillUnderlay').checked = drillUnderlay;
    const stored = String(rec?.SwapChainAddress || '').toLowerCase();
    drillEl('drillChainNote').textContent = stored && stored !== res.swap.toLowerCase() ? 'Not the chain VPinPerfMon graded, so the database recheck below will differ.' : '';
  }

  // ---- timeline + CPU/GPU charts (all frames of the graded swap chain, warm-up included)
  const dtData = [], ftData = [], cpuData = [], gpuData = [];
  for (const r of res.chain) {
    const x = sec(st[r]);
    if (!Number.isFinite(x)) continue;
    if (Number.isFinite(dt[r])) dtData.push({ x, y: dt[r] });
    if (Number.isFinite(csv.FrameTime[r])) ftData.push({ x, y: csv.FrameTime[r] });
    if (Number.isFinite(csv.CPUBusy[r])) cpuData.push({ x, y: csv.CPUBusy[r] });
    if (Number.isFinite(csv.GPUBusy[r])) gpuData.push({ x, y: csv.GPUBusy[r] });
  }
  const xMax = Math.max(1, ...[dtData, ftData, cpuData].map(d => (d.length ? d[d.length - 1].x : 0)));
  const others = new Map();
  if (drillUnderlay) {
    for (let r = 0; r < csv.n; r++) {
      const s = csv.swap[r], x = sec(st[r]);
      if (s === res.swap || !Number.isFinite(x) || !Number.isFinite(dt[r])) continue;
      if (!others.has(s)) others.set(s, []);
      others.get(s).push({ x, y: dt[r] });
    }
  }
  const underColors = ['#e8a33d', '#c06c84', '#6c9bd2', '#8fbf6a'];
  const underlays = [...others].map(([s, data], i) => ({ label: `Other chain ${s || '(none)'} (displayed ms)`, data,
    borderColor: drillAlpha(underColors[i % underColors.length], 0.5), backgroundColor: drillAlpha(underColors[i % underColors.length], 0.5),
    borderWidth: 1, pointRadius: 0, order: 4 }));
  const missPts = res.misses.map(m => ({ x: sec(st[m.r]), y: m.ms, m }));
  const bands = [{ from: 0, to: res.warmupSec, color: drillAlpha(colors.muted, 0.12), label: 'warm-up (not graded)' }];
  if (Number.isFinite(res.tailStart)) bands.push({ from: sec(res.tailStart), to: xMax, color: drillAlpha(colors.muted, 0.12), label: 'trimmed' });
  if (res.worst && res.peak > 0) {
    bands.push({ from: sec(res.worst.from), to: sec(res.worst.to), color: drillAlpha(colors.danger, 0.14),
                 label: `worst ${res.cfg.ClusterWindowSec} s: ${res.peak} miss${res.peak === 1 ? '' : 'es'}` });
  }
  const marks = missPts.map(p => p.x);
  const modePts = res.modeChanges.map(c => ({ x: sec(st[c.r]), y: 0, c }));
  const modeMarks = modePts.map(p => p.x);
  const missLabel = m => [`${m.refreshes}× refresh stall: ${m.ms.toFixed(1)} ms on screen`,
    `CPU busy ${drillFmt(m.cpu, 1)} ms · GPU busy ${drillFmt(m.gpu, 1)} ms`, `Likely: ${m.cause}`];

  drillCharts.timeline = new Chart(drillEl('drillTimeline'), {
    type: 'line',
    plugins: [drillBandsPlugin],
    data: { datasets: [
      { label: 'Displayed time (ms)', data: dtData, borderColor: colors.chartPoint, backgroundColor: colors.chartPoint,
        borderWidth: 1, pointRadius: 0, order: 2 },
      { label: 'Frame time (ms)', data: ftData, borderColor: drillAlpha(colors.link, 0.55), backgroundColor: drillAlpha(colors.link, 0.55),
        borderWidth: 1, pointRadius: 0, order: 3 },
      { type: 'scatter', label: 'Missed refresh', data: missPts, order: 1, pointRadius: 4, pointHoverRadius: 7,
        backgroundColor: missPts.map(p => (p.m.triple ? colors.danger : colors.warning)),
        borderColor: missPts.map(p => (p.m.triple ? colors.danger : colors.warning)) },
      { type: 'scatter', label: 'Present-mode change', data: modePts, order: 0, pointStyle: 'triangle', pointRadius: 5, pointHoverRadius: 8,
        backgroundColor: DRILL_MODE_COLOR, borderColor: DRILL_MODE_COLOR },
      ...underlays,
    ] },
    options: drillLineOptions('ms', xMax,
      { bands, modeMarks, lines: [1, 2, 3].map(k => ({ value: k * refresh, label: `${k}× ${(k * refresh).toFixed(1)} ms` })) },
      item => (item.raw.m ? missLabel(item.raw.m) : item.raw.c ? [`Present mode: ${item.raw.c.from}`, `→ ${item.raw.c.to}`] : `${item.dataset.label}: ${item.parsed.y.toFixed(2)}`)),
  });

  drillCharts.busy = new Chart(drillEl('drillBusy'), {
    type: 'line',
    plugins: [drillBandsPlugin],
    data: { datasets: [
      { label: 'CPU busy (ms)', data: cpuData, borderColor: colors.warning, backgroundColor: colors.warning, borderWidth: 1, pointRadius: 0 },
      { label: 'GPU busy (ms)', data: gpuData, borderColor: colors.accent, backgroundColor: colors.accent, borderWidth: 1, pointRadius: 0 },
    ] },
    options: drillLineOptions('ms', xMax,
      { bands, marks, modeMarks, lines: [{ value: refresh, label: `1 refresh ${refresh.toFixed(1)} ms` }] },
      item => `${item.dataset.label}: ${item.parsed.y.toFixed(2)}`),
  });

  // ---- histogram of on-screen time in refreshes
  const bins = [];
  for (const r of res.graded) {
    const b = Math.max(1, drillRound(dt[r] / refresh));
    bins[b] = (bins[b] || 0) + 1;
  }
  let binKeys = [];
  for (let b = 1; b < bins.length; b++) binKeys.push(b);
  if (binKeys.length > 30) binKeys = binKeys.filter(b => bins[b]);
  const binColor = b => (b >= 3 ? colors.danger : b === 2 ? colors.warning : colors.success);
  drillCharts.hist = new Chart(drillEl('drillHist'), {
    type: 'bar',
    data: {
      labels: binKeys.map(b => (b === 1 ? '≤1×' : `${b}×`)),
      datasets: [{ label: 'Frames', data: binKeys.map(b => bins[b] || 0), backgroundColor: binKeys.map(binColor) }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        zoom: { pan: { enabled: false }, zoom: { wheel: { enabled: false }, pinch: { enabled: false } } },
        tooltip: { callbacks: { label: item => `${item.raw.toLocaleString()} frames (${(item.raw / res.frames * 100).toPrecision(3)}%)` } },
      },
      scales: {
        x: { title: { display: true, text: 'Refreshes on screen', color: colors.muted }, grid: { color: colors.border }, ticks: { color: colors.muted } },
        y: { type: 'logarithmic', min: 0.5, title: { display: true, text: 'Frames (log)', color: colors.muted },
             grid: { color: colors.border }, ticks: { color: colors.muted, callback: v => ([1, 10, 100, 1000, 10000, 100000].includes(v) ? v.toLocaleString() : '') } },
      },
    },
  });

  // ---- recheck vs database
  const checks = [
    ['Frames after warm-up', 'PresentMonFrameCount', res.framesAfterWarmup, 0, 0],
    ['Graded frames', 'GradeFrames', res.frames, 0, 0],
    ['Missing frames', 'GradeMissingFrames', res.missingFrames, 0, 0],
    ['Graded duration (s)', 'GradeDurationSec', res.duration, 2, 0.01],
    ['Refresh (ms)', 'GradeRefreshMs', res.refresh, 4, 0.01],
    ['Doubled frames', 'DoubledFrames', res.doubled, 0, 0],
    ['3+ refresh frames', 'TripleFrames', res.triple, 0, 0],
    ['Longest miss (refreshes)', 'LongestMissRefreshes', res.longest, 0, 0],
    ['Weighted miss %', 'WeightedMissPct', res.weighted, 4, 'rel'],
    ['Cluster peak (misses)', 'ClusterPeakMisses', res.peak, 0, 0],
    ['Cluster multiplier', 'ClusterMultiplier', res.multiplier, 3, 0.005],
    ['Effective miss %', 'EffectiveMissPct', res.effective, 4, 'rel'],
    ['Grade score', 'StutterGradeScore', res.score, 2, 0.05],
    ['Grade', 'StutterGrade', res.grade, null, 0],
    ['CPU stalls', 'StallCpuCount', res.stallCounts.cpu, 0, 0],
    ['GPU stalls', 'StallGpuCount', res.stallCounts.gpu, 0, 0],
    ['Present-mode switch stalls', 'StallModeSwitchCount', res.stallCounts.modeSwitch, 0, 0],
    ['App-waiting stalls', 'StallAppWaitCount', res.stallCounts.appWait, 0, 0],
    ['Display / compositor stalls', 'StallDisplayCount', res.stallCounts.display, 0, 0],
    ['Blame CPU %', 'BlameCpuPct', res.blameCpuPct, 1, 0.05, true],
    ['Blame GPU %', 'BlameGpuPct', res.blameGpuPct, 1, 0.05, true],
  ];
  // Stall counts and blame exist only on rows graded with the stall-based formula; older rows are skipped.
  const hasStallData = rec != null && rec.StallCpuCount != null;
  const stallBad = [];
  let ok = 0, compared = 0;
  const rows = checks.map(([label, key, mine, digits, tol, nullable]) => {
    const isStall = /^(Stall|Blame)/.test(key);
    if (isStall && !hasStallData) return `<tr><td>${label}</td><td class="num">&ndash;</td><td class="num">${mine == null ? '&ndash;' : drillFmt(Number(mine), digits)}</td><td><span class="muted">&ndash;</span></td></tr>`;
    const db = rec ? rec[key] : null;
    let mark = '<span class="muted">&ndash;</span>';
    if (nullable) {
      // NULL blame (too few app-caused stalls) must match NULL, not just numbers.
      compared++;
      const pass = (db == null && mine == null) || (db != null && mine != null && Math.abs(mine - db) <= tol + 1e-9);
      if (pass) ok++; else stallBad.push(label);
      mark = pass ? '<span class="drill-ok">&#10003;</span>' : '<span class="drill-bad">&#10007;</span>';
    } else if (db != null && mine != null) {
      compared++;
      const diff = typeof mine === 'number' ? Math.abs(mine - db) : (mine === db ? 0 : 1);
      const pass = tol === 'rel' ? diff <= Math.max(1e-4, Math.abs(db) * 0.01) : diff <= tol + 1e-9;
      if (pass) ok++; else if (isStall) stallBad.push(label);
      mark = pass ? '<span class="drill-ok">&#10003;</span>' : '<span class="drill-bad">&#10007;</span>';
    }
    const show = v => (v == null ? '&ndash;' : digits == null ? escapeHtml(v) : drillFmt(Number(v), digits));
    return `<tr><td>${label}</td><td class="num">${show(db)}</td><td class="num">${show(mine)}</td><td>${mark}</td></tr>`;
  });
  const verdict = !rec ? '<p class="muted">Not linked to a run in this report, so there is nothing to compare against.</p>'
    : ok === compared ? `<p class="drill-ok">All ${compared} values match the database &mdash; this CSV reproduces the stored grade.</p>`
    : `<p class="drill-bad">${compared - ok} of ${compared} values differ &mdash; the grading config, warm-up or file may not match this run.</p>`;
  const stallWarn = stallBad.length
    ? `<p class="drill-bad">&#9888; Stall classification differs from the database (${escapeHtml(stallBad.join(', '))}). The warm-up, swap chain or file may not match this run, or the classifier is out of step with VPinPerfMon.</p>` : '';
  const legacyNote = rec && rec.BlameVersion != null && rec.BlameLegacy === 1
    ? '<p class="muted">This row uses legacy blame (an older formula), so its stored blame and stall counts are not comparable.</p>' : '';
  drillEl('drillCheck').innerHTML = verdict + stallWarn + legacyNote +
    `<table class="anomaly-table"><thead><tr><th>Metric</th><th class="num">Database</th><th class="num">From CSV</th><th></th></tr></thead><tbody>${rows.join('')}</tbody></table>` +
    `<p class="chart-note">Swap chain ${escapeHtml(res.swap)}${res.swapChains > 1 ? ` (of ${res.swapChains})` : ''} · ${res.warmupSec} s warm-up (${escapeHtml(res.warmupSource)}) · ${res.trimmedSec.toFixed(1)} s trimmed from the end.</p>` +
    `<details class="chart-more"><summary>More info</summary><p class="chart-note">${res.trimmed} frames trimmed from the end${res.warmupSource.startsWith('inferred') ? '; the warm-up was inferred from the frame count, so the frame-count check is not independent' : ''}. Blame needs at least ${res.cfg.BlameMinStalls} CPU+GPU stalls (CfgBlameMinStalls); the GPU rule uses ${drillFmt(res.cfg.BlameGpuSaturation * 100, 0)}% of the refresh (CfgBlameGpuSaturation).</p></details>`;

  // ---- present-mode legend (only when the graded window has changes)
  const modeBox = drillEl('drillModes');
  const seenModes = [...new Set(res.modeChanges.flatMap(c => [c.from, c.to]))];
  modeBox.hidden = !seenModes.length;
  modeBox.innerHTML = seenModes.length ? '<div class="chart-header"><h2>Present modes in this run</h2></div>' +
    '<table class="anomaly-table"><thead><tr><th>Mode</th><th>What it means</th></tr></thead><tbody>' +
    seenModes.map(m => `<tr><td><code>${escapeHtml(m)}</code></td><td>${escapeHtml(DRILL_MODE_INFO[m] || 'Not described here; see the PresentMon documentation.')}</td></tr>`).join('') +
    '</tbody></table><p class="chart-note">Rapid switches back and forth usually mean something else is drawing over the game window (overlay, notification, another app, capture tool).</p>' : '';

  // ---- stall list
  const LIMIT = 200;
  const stalls = res.misses.slice().sort((a, b) => b.ratio - a.ratio || a.r - b.r).slice(0, LIMIT).sort((a, b) => a.r - b.r);
  drillEl('drillStallsTitle').textContent = res.misses.length
    ? `Stalls (${res.misses.length}${res.misses.length > LIMIT ? `, worst ${LIMIT} shown` : ''})` : 'Stalls (none — every graded frame was on time)';
  drillEl('drillStalls').innerHTML = res.misses.length ? `<table class="anomaly-table"><thead><tr>
      <th class="num">Time (s)</th><th class="num">Refreshes</th><th class="num">On screen (ms)</th><th class="num">CPU busy</th>
      <th class="num">GPU busy</th><th class="num">Next frame time</th><th>Likely cause</th><th>Present mode</th><th></th></tr></thead><tbody>` +
    stalls.map(m => `<tr><td class="num">${drillFmt(sec(st[m.r]), 2)}</td>
      <td class="num" style="color:${m.triple ? colors.danger : colors.warning}">${m.refreshes}×</td>
      <td class="num">${drillFmt(m.ms, 1)}</td><td class="num">${drillFmt(m.cpu, 1)}</td><td class="num">${drillFmt(m.gpu, 1)}</td>
      <td class="num">${drillFmt(m.nextFt, 1)}</td><td>${escapeHtml(m.cause)}</td><td class="muted">${escapeHtml(m.mode || '')}</td>
      <td><button type="button" data-x="${sec(st[m.r])}">Zoom</button></td></tr>`).join('') + '</tbody></table>' : '';

  drillStatus(rec
    ? `Loaded <code>${escapeHtml(file.name)}</code> &mdash; ${csv.n.toLocaleString()} rows.`
    : `Loaded <code>${escapeHtml(file.name)}</code> &mdash; it doesn't match a run in this report, so it's shown without the database comparison.`,
    rec ? 'ok' : 'warn');
}

drillEl('drillRun').addEventListener('change', e => {
  if (e.target.value === '') return;
  drillOpenRecord(Number(e.target.value), false);
});
drillEl('drillChain').addEventListener('change', e => {
  drillSwapPick = e.target.value;
  drillReanalyze();
});
drillEl('drillUnderlay').addEventListener('change', e => {
  drillUnderlay = e.target.checked;
  if (drillLast) drillRender(drillLast.csv, drillLast.res, drillLast.rec, drillLast.file);
});
drillEl('drillFolderBtn').addEventListener('click', drillConnectFolder);
drillEl('drillFileBtn').addEventListener('click', () => drillEl('drillFileInput').click());
drillEl('drillFolderInput').addEventListener('change', e => {
  const files = [...e.target.files];
  if (!files.length) return;
  drillFiles.clear();
  drillAddFiles(files);
  drillAfterConnect((files[0].webkitRelativePath || '').split('/')[0] || 'folder');
  e.target.value = '';
});
drillEl('drillFileInput').addEventListener('change', e => {
  if (e.target.files.length) drillUseFiles(e.target.files);
  e.target.value = '';
});
drillEl('drillStalls').addEventListener('click', e => {
  const btn = e.target.closest('button[data-x]');
  if (btn) drillZoomTo(Number(btn.dataset.x));
});
document.querySelectorAll('#drillResults .drill-reset').forEach(btn =>
  btn.addEventListener('click', () => [drillCharts.timeline, drillCharts.busy].forEach(c => c?.resetZoom('none'))));

const drillZone = drillEl('drillZone');
['dragenter', 'dragover'].forEach(type => drillZone.addEventListener(type, e => {
  if (![...(e.dataTransfer?.types || [])].includes('Files')) return;
  e.preventDefault();
  drillZone.classList.add('drag');
}));
['dragleave', 'drop'].forEach(type => drillZone.addEventListener(type, e => {
  if (type === 'dragleave' && drillZone.contains(e.relatedTarget)) return;
  drillZone.classList.remove('drag');
}));
drillZone.addEventListener('drop', e => {
  if (!e.dataTransfer?.files?.length) return;
  e.preventDefault();
  drillUseFiles(e.dataTransfer.files);
});
drillRenderLogsHint();
drillOfferReconnect();
