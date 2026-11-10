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

function drillPickSwap(csv, rec, from) {
  const counts = new Map();
  for (let r = 0; r < csv.n; r++) if (csv.CPUStartTime[r] >= from) counts.set(csv.swap[r], (counts.get(csv.swap[r]) || 0) + 1);
  if (!counts.size) return { swap: null, counts };
  const want = String(rec?.SwapChainAddress || '').toLowerCase();
  let swap = [...counts.keys()].find(k => want && k.toLowerCase() === want);
  if (swap == null) swap = [...counts.entries()].sort((a, b) => b[1] - a[1])[0][0];
  return { swap, counts };
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

// Heuristic "why" for a miss: look at the stalled frame and the next present of the same swap chain.
function drillCause(csv, chain, p, refresh) {
  const at = (arr, q) => (q >= 0 && q < chain.length ? arr[chain[q]] : NaN);
  const modeAt = q => (q >= 0 && q < chain.length ? csv.mode[chain[q]] : null);
  const cpu = drillMax(at(csv.CPUBusy, p), at(csv.CPUBusy, p + 1));
  const gpu = drillMax(at(csv.GPUBusy, p), at(csv.GPUBusy, p + 1));
  const nextFt = at(csv.FrameTime, p + 1);
  const mode = modeAt(p);
  const modeSwitch = [p - 1, p + 1].some(q => modeAt(q) != null && modeAt(q) !== mode);
  const lim = 1.5 * refresh;
  let cause = 'Display / compositor';
  if (cpu >= lim && !(gpu > cpu)) cause = 'CPU';
  else if (gpu >= lim) cause = 'GPU';
  else if (modeSwitch) cause = 'Present-mode switch';
  else if (nextFt >= lim) cause = 'App waiting / OS';
  return { cpu, gpu, nextFt, mode, cause };
}

// Mirrors the VPinPerfMon v2 stutter grade (see DATABASE_COLUMNS.md); rec supplies the stored
// swap chain / fixed refresh when available.
function drillAnalyze(csv, rec, cfgIn, logSec) {
  const cfg = { ...DRILL_FALLBACK_CFG, ...(cfgIn || {}) };
  const st = csv.CPUStartTime;
  const dt = csv.DisplayedTime;
  const t0 = st[0];
  if (!Number.isFinite(t0)) throw new Error('first row has no CPUStartTime');
  const warmup = drillResolveWarmup(csv, rec, cfg, logSec);
  const warmEnd = t0 + warmup.sec * 1000;

  const { swap, counts } = drillPickSwap(csv, rec, warmEnd);
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
      misses.push({ r, ratio, refreshes, triple: isTriple, win: w, ms: dt[r], ...drillCause(csv, chain, pos.get(r), refresh) });
    }
    cum += dtf(r);
  }
  const missCount = misses.length;
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
    doubled, triple, longest, weighted, misses, peak, peakWin, clusterRatio, multiplier, effective,
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
  try {
    const file = entry.getFile ? await entry.getFile() : entry;
    drillStatus(`Reading <code>${escapeHtml(file.name)}</code> (${(file.size / 1048576).toFixed(1)} MB)&hellip;`);
    const text = await file.text();
    if (token !== drillToken) return;
    const csv = drillParseCsv(text);
    const logSec = await drillReadLogWarmup(file.name);
    if (token !== drillToken) return;
    const res = drillAnalyze(csv, rec, rec ? drillCfgs[rec.CfgIdx] : drillCfgs[0], logSec);
    drillRender(csv, res, rec, file);
  } catch (err) {
    if (token !== drillToken) return;
    drillStatus(`Could not analyze this capture: ${escapeHtml(err.message || err)}`, 'error');
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
  const causeText = Object.entries(causes).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c} ${n}`).join(' · ') || 'no stalls';
  const worstMiss = res.misses.reduce((w, m) => (!w || m.ratio > w.ratio ? m : w), null);
  const chips = [
    [`${drillGradeTag(res.grade)} ${drillFmt(res.score, 1)}`, `recomputed grade${rec?.StutterGrade ? ` (DB: ${rec.StutterGrade} ${drillFmt(rec.StutterGradeScore, 1)})` : ''}${res.capped ? ' · capped' : ''}`],
    [drillInt(res.frames), `graded frames · ${drillFmt(res.duration, 1)} s`],
    [`${drillFmt(1000 / refresh, 1)} Hz`, `refresh (${drillFmt(refresh, 3)} ms, ${escapeHtml(res.refreshSource)})`],
    [drillInt(res.misses.length), `missed refreshes (2×: ${res.doubled} · 3×+: ${res.triple})`],
    [worstMiss ? `${drillFmt(worstMiss.ms, 1)} ms` : '&ndash;', worstMiss ? `longest stall (${worstMiss.refreshes} refreshes) at ${drillFmt(sec(st[worstMiss.r]), 1)} s` : 'longest stall'],
    [`${drillFmt(res.effective, 3)}%`, `effective miss % (cluster ×${drillFmt(res.multiplier, 2)}, peak ${res.peak} per ${res.cfg.ClusterWindowSec} s)`],
    [escapeHtml(causeText), 'likely stall causes'],
  ];
  drillEl('drillChips').innerHTML = chips.map(([v, l]) => `<div><b>${v}</b>${l}</div>`).join('');

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
  const missPts = res.misses.map(m => ({ x: sec(st[m.r]), y: m.ms, m }));
  const bands = [{ from: 0, to: res.warmupSec, color: drillAlpha(colors.muted, 0.12), label: 'warm-up (not graded)' }];
  if (Number.isFinite(res.tailStart)) bands.push({ from: sec(res.tailStart), to: xMax, color: drillAlpha(colors.muted, 0.12), label: 'trimmed' });
  if (res.worst && res.peak > 0) {
    bands.push({ from: sec(res.worst.from), to: sec(res.worst.to), color: drillAlpha(colors.danger, 0.14),
                 label: `worst ${res.cfg.ClusterWindowSec} s: ${res.peak} miss${res.peak === 1 ? '' : 'es'}` });
  }
  const marks = missPts.map(p => p.x);
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
    ] },
    options: drillLineOptions('ms', xMax,
      { bands, lines: [1, 2, 3].map(k => ({ value: k * refresh, label: `${k}× ${(k * refresh).toFixed(1)} ms` })) },
      item => (item.raw.m ? missLabel(item.raw.m) : `${item.dataset.label}: ${item.parsed.y.toFixed(2)}`)),
  });

  drillCharts.busy = new Chart(drillEl('drillBusy'), {
    type: 'line',
    plugins: [drillBandsPlugin],
    data: { datasets: [
      { label: 'CPU busy (ms)', data: cpuData, borderColor: colors.warning, backgroundColor: colors.warning, borderWidth: 1, pointRadius: 0 },
      { label: 'GPU busy (ms)', data: gpuData, borderColor: colors.accent, backgroundColor: colors.accent, borderWidth: 1, pointRadius: 0 },
    ] },
    options: drillLineOptions('ms', xMax,
      { bands, marks, lines: [{ value: refresh, label: `1 refresh ${refresh.toFixed(1)} ms` }] },
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
  ];
  let ok = 0, compared = 0;
  const rows = checks.map(([label, key, mine, digits, tol]) => {
    const db = rec ? rec[key] : null;
    let mark = '<span class="muted">&ndash;</span>';
    if (db != null && mine != null) {
      compared++;
      const diff = typeof mine === 'number' ? Math.abs(mine - db) : (mine === db ? 0 : 1);
      const pass = tol === 'rel' ? diff <= Math.max(1e-4, Math.abs(db) * 0.01) : diff <= tol + 1e-9;
      if (pass) ok++;
      mark = pass ? '<span class="drill-ok">&#10003;</span>' : '<span class="drill-bad">&#10007;</span>';
    }
    const show = v => (v == null ? '&ndash;' : digits == null ? escapeHtml(v) : drillFmt(Number(v), digits));
    return `<tr><td>${label}</td><td class="num">${show(db)}</td><td class="num">${show(mine)}</td><td>${mark}</td></tr>`;
  });
  const verdict = !rec ? '<p class="muted">Not linked to a run in this report, so there is nothing to compare against.</p>'
    : ok === compared ? `<p class="drill-ok">All ${compared} values match the database &mdash; this CSV reproduces the stored grade.</p>`
    : `<p class="drill-bad">${compared - ok} of ${compared} values differ &mdash; the grading config, warm-up or file may not match this run.</p>`;
  drillEl('drillCheck').innerHTML = verdict +
    `<table class="anomaly-table"><thead><tr><th>Metric</th><th class="num">Database</th><th class="num">From CSV</th><th></th></tr></thead><tbody>${rows.join('')}</tbody></table>` +
    `<p class="chart-note">Swap chain ${escapeHtml(res.swap)}${res.swapChains > 1 ? ` (of ${res.swapChains})` : ''} · ${res.trimmed} frames (${res.trimmedSec.toFixed(1)} s) trimmed from the end · ${res.warmupSec} s warm-up (${escapeHtml(res.warmupSource)}${res.warmupSource.startsWith('inferred') ? ', so the frame-count check is not independent' : ''}).</p>`;

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
