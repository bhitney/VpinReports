"""
Generates HistoryReport.html: a self-contained HTML report of play-history
clusters (manufacturer, year/decade, type, rating, recency, monthly trend)
from the PinUP Popper database.

Used by build_report.py for the Play History tab. Can also run on its own:
    python history_report.py [days]

Settings (DB_PATH, TARGET_EMU_IDS, HISTORY_DAYS) come from report_config.py.
Originally Vpin_HistoryReport.py from the Vpin-AI-Recommender repo.
Charts use Chart.js from a CDN, so the browser needs internet access.
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

from report_config import DB_PATH, TARGET_EMU_IDS as _CFG_EMUS, HISTORY_DAYS

# Override without touching config: set VPIN_EMU_IDS=10 (comma-separated)
TARGET_EMU_IDS = os.environ.get("VPIN_EMU_IDS", _CFG_EMUS)

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else int(HISTORY_DAYS)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HistoryReport.html")

GAMES_QUERY = f"""
SELECT count(1) AS Plays,
       sum(SessionPlayedSecs) AS Secs,
       CAST(JULIANDAY('now') - JULIANDAY(max(cgl.PlayDate)) AS INTEGER) AS LastPlayedDays,
       g.GameID, g.EMUID, e.EmuDisplay, GameDisplay, GameYear, Manufact, GameType, GameRating
FROM Games g JOIN CustomGameLog cgl ON g.GameID = cgl.GameID
LEFT JOIN Emulators e ON e.EMUID = g.EMUID
WHERE g.EMUID IN ({TARGET_EMU_IDS}) AND g.visible = 1
  AND PlayDate > DateTime('Now', 'LocalTime', '-' || ? || ' Day')
GROUP BY cgl.GameID
HAVING sum(SessionPlayedSecs) >= 60
"""

# Page-level time filter: (key, label, days). days=None means all time.
RANGES = [("1d", "1 day", 1), ("1w", "1 week", 7), ("1m", "1 month", 30),
          ("1y", "1 year", 365), ("all", "All", None)]
ALL_DAYS = 36500
# Trend-chart granularity per range (strftime format, chart title)
BUCKETS = {"1d": ("%Y-%m-%d %H:00", "Hourly"), "1w": ("%Y-%m-%d", "Daily"), "1m": ("%Y-%m-%d", "Daily"),
           "1y": ("%Y-%m", "Monthly"), "all": ("%Y-%m", "Monthly")}


def monthly_query(fmt):
    return f"""
SELECT strftime('{fmt}', PlayDate) AS Month, g.EMUID, e.EmuDisplay, count(1) AS Plays, sum(SessionPlayedSecs) AS Secs
FROM Games g JOIN CustomGameLog cgl ON g.GameID = cgl.GameID
LEFT JOIN Emulators e ON e.EMUID = g.EMUID
WHERE g.EMUID IN ({TARGET_EMU_IDS}) AND g.visible = 1
  AND PlayDate > DateTime('Now', 'LocalTime', '-' || ? || ' Day')
GROUP BY Month, g.EMUID ORDER BY Month
"""


def default_range(days):
    """Smallest range covering the configured window (365 -> '1y')."""
    for key, _, d in RANGES:
        if d is not None and days <= d:
            return key
    return "all"

# PlayDate is stored in local time; %w gives 0=Sunday..6=Saturday
TIME_QUERY = f"""
SELECT CAST(strftime('%w', PlayDate) AS INTEGER) AS Dow, CAST(strftime('%H', PlayDate) AS INTEGER) AS Hour,
       count(1) AS Plays, sum(SessionPlayedSecs) AS Secs
FROM Games g JOIN CustomGameLog cgl ON g.GameID = cgl.GameID
WHERE g.EMUID IN ({TARGET_EMU_IDS}) AND g.visible = 1
  AND PlayDate > DateTime('Now', 'LocalTime', '-' || ? || ' Day')
GROUP BY Dow, Hour
"""

DAILY_QUERY = f"""
SELECT date(PlayDate) AS Day, count(DISTINCT cgl.GameID) AS Tables, count(1) AS Plays, sum(SessionPlayedSecs) AS Secs
FROM Games g JOIN CustomGameLog cgl ON g.GameID = cgl.GameID
WHERE g.EMUID IN ({TARGET_EMU_IDS}) AND g.visible = 1
  AND PlayDate > DateTime('Now', 'LocalTime', '-' || ? || ' Day')
GROUP BY Day
"""

CATALOG_QUERY = f"""
SELECT Manufact, GameYear, GameType FROM Games
WHERE EMUID IN ({TARGET_EMU_IDS}) AND visible = 1
"""


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load():
    """Return ({range key: (games, monthly, timing, daily)}, catalog)."""
    if not os.path.exists(DB_PATH):
        sys.exit(f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    out = {}
    for key, _, days in RANGES:
        d = (days or ALL_DAYS,)
        out[key] = (
            _clean_games([dict(r) for r in cur.execute(GAMES_QUERY, d)]),
            [dict(r) for r in cur.execute(monthly_query(BUCKETS[key][0]), d)],
            [dict(r) for r in cur.execute(TIME_QUERY, d)],
            [dict(r) for r in cur.execute(DAILY_QUERY, d)],
        )
    catalog = [dict(r) for r in cur.execute(CATALOG_QUERY)]
    conn.close()
    return out, catalog


def _clean_games(games):
    for g in games:
        g["Year"] = to_int(g["GameYear"])
        if g["Year"] is not None and not 1930 <= g["Year"] <= datetime.now().year + 1:
            g["Year"] = None
        g["Manufact"] = (g["Manufact"] or "").strip() or "Unknown"
        g["GameType"] = (g["GameType"] or "").strip() or "Unknown"
        r = to_int(g["GameRating"])
        g["Rating"] = r if r and 1 <= r <= 5 else 0
        g["Hours"] = round((g["Secs"] or 0) / 3600, 2)
        g["AvgMin"] = round((g["Secs"] or 0) / g["Plays"] / 60, 1)
        g["Name"] = g["GameDisplay"]
        g["Emu"] = (g["EmuDisplay"] or "").strip() or f"Emulator {g['EMUID']}"
    return games


def decade(y):
    return "Unknown" if y is None else f"{y // 10 * 10}s"


def group(games, keyfn):
    out = defaultdict(lambda: {"tables": 0, "plays": 0, "hours": 0.0})
    for g in games:
        o = out[keyfn(g)]
        o["tables"] += 1
        o["plays"] += g["Plays"]
        o["hours"] += g["Hours"]
    return out


def build_all():
    """One payload per range, plus the shared catalog years (identical for every range)."""
    data, catalog = load()
    payloads = {}
    for key, _, days in RANGES:
        games, monthly, timing, daily = data[key]
        if days is None:  # all time: per-day averages use the span since the first play
            first = min((datetime.strptime(d["Day"], "%Y-%m-%d") for d in daily), default=None)
            days = max(1, (datetime.now() - first).days + 1) if first else 1
        payloads[key] = build_payload(games, monthly, catalog, timing, daily, days)
        payloads[key]["trend"] = BUCKETS[key][1]
    catalog_years = payloads["all"].pop("catalogYears")
    for p in payloads.values():
        p.pop("catalogYears", None)
    return {"ranges": [{"key": k, "label": l} for k, l, _ in RANGES],
            "default": default_range(DAYS), "data": payloads, "catalogYears": catalog_years}


def build_payload(games, monthly, catalog, timing, daily, days):
    by_manu = group(games, lambda g: g["Manufact"])
    manu_rows = sorted(by_manu.items(), key=lambda kv: -kv[1]["hours"])
    top_manus = [m for m, _ in manu_rows[:12]]

    by_year = group([g for g in games if g["Year"]], lambda g: g["Year"])
    years = sorted(by_year)
    by_dec = group(games, lambda g: decade(g["Year"]))
    decs = sorted(by_dec, key=lambda d: (d == "Unknown", d))
    by_type = group(games, lambda g: g["GameType"])
    by_rating = group(games, lambda g: g["Rating"])

    # Manufacturer x decade heatmap (hours)
    heat = defaultdict(lambda: defaultdict(float))
    for g in games:
        m = g["Manufact"] if g["Manufact"] in top_manus else "Other"
        heat[m][decade(g["Year"])] += g["Hours"]
    heat_rows = top_manus + (["Other"] if "Other" in heat else [])

    # Played-vs-owned coverage by manufacturer
    owned = defaultdict(int)
    for c in catalog:
        owned[(c["Manufact"] or "").strip() or "Unknown"] += 1
    coverage = [
        {"manu": m, "owned": owned[m], "played": by_manu[m]["tables"]}
        for m in top_manus if owned.get(m)
    ]

    recency_buckets = [("0-7d", 0, 7), ("8-30d", 8, 30), ("31-90d", 31, 90),
                       ("91-180d", 91, 180), ("181d+", 181, 10 ** 6)]
    recency = [
        {"label": lbl, "tables": sum(1 for g in games if lo <= (g["LastPlayedDays"] or 0) <= hi)}
        for lbl, lo, hi in recency_buckets
    ]

    by_emu = group(games, lambda g: g["Emu"])
    emu_rows = sorted(by_emu.items(), key=lambda kv: -kv[1]["hours"])
    emu_names = {m["EMUID"]: (m["EmuDisplay"] or "").strip() or f"Emulator {m['EMUID']}" for m in monthly}
    months = sorted({m["Month"] for m in monthly})
    monthly_total = [{"Month": mo,
                      "Plays": sum(m["Plays"] for m in monthly if m["Month"] == mo),
                      "Secs": sum(m["Secs"] or 0 for m in monthly if m["Month"] == mo)} for mo in months]
    monthly_emu = [{"name": emu_names[eid],
                    "hours": [round(sum(m["Secs"] or 0 for m in monthly if m["Month"] == mo and m["EMUID"] == eid) / 3600, 2)
                              for mo in months]}
                   for eid in sorted(emu_names, key=lambda e: -sum(m["Secs"] or 0 for m in monthly if m["EMUID"] == e))]

    rounded = lambda d: {k: round(v, 1) if isinstance(v, float) else v for k, v in d.items()}
    return {
        "days": days,
        "summary": {
            "tables": len(games),
            "plays": sum(g["Plays"] for g in games),
            "hours": round(sum(g["Hours"] for g in games), 1),
            "manus": len(by_manu),
            "emus": len({g["EMUID"] for g in games}),
            "catalog": len(catalog),
            "activeDays": len(daily),
            "tablesPerDay": round(sum(d["Tables"] for d in daily) / days, 2),
            "playsPerDay": round(sum(d["Plays"] for d in daily) / days, 2),
            "hoursPerDay": round(sum(d["Secs"] or 0 for d in daily) / 3600 / days, 2),
        },
        "manu": [{"name": m, **rounded(v)} for m, v in manu_rows[:15]],
        "years": [{"year": y, **rounded(by_year[y])} for y in years],
        "decades": [{"name": d, **rounded(by_dec[d])} for d in decs],
        "types": [{"name": t, **rounded(v)} for t, v in sorted(by_type.items(), key=lambda kv: -kv[1]["hours"])],
        "ratings": [{"name": "Unrated" if r == 0 else f"{r}★", **rounded(by_rating[r])}
                    for r in sorted(by_rating)],
        "heat": {"rows": heat_rows, "cols": decs,
                 "vals": [[round(heat[r].get(c, 0), 1) for c in decs] for r in heat_rows]},
        "catalogYears": [{"x": y, "manu": (c["Manufact"] or "").strip() or "Unknown"}
                         for c in catalog for y in [to_int(c["GameYear"])]
                         if y and 1930 <= y <= datetime.now().year + 1],
        "dowHour": {"hours": [[round(sum(t["Secs"] or 0 for t in timing if t["Dow"] == d and t["Hour"] == h) / 3600, 2)
                               for h in range(24)] for d in range(7)],
                    "plays": [[sum(t["Plays"] for t in timing if t["Dow"] == d and t["Hour"] == h)
                               for h in range(24)] for d in range(7)]},
        "coverage": coverage,
        "recency": recency,
        "monthly": monthly_total,
        "monthlyEmu": monthly_emu,
        "emus": [{"name": n, **rounded(v)} for n, v in emu_rows],
        "scatter": [{"x": g["Year"], "y": g["AvgMin"], "r": g["Hours"], "label": g["Name"],
                     "manu": g["Manufact"], "rating": g["Rating"]}
                    for g in games if g["Year"]],
        "top": sorted(
            [{"name": g["Name"], "manu": g["Manufact"], "year": g["Year"], "type": g["GameType"],
              "rating": g["Rating"], "plays": g["Plays"], "hours": g["Hours"],
              "avg": g["AvgMin"], "last": g["LastPlayedDays"]} for g in games],
            key=lambda r: -r["hours"])[:40],
    }


HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>VPin Play History Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root { --bg:#3d3b3a; --surface:#292929; --soft:#2e2e2e; --border:#474747; --text:#f0f0f0;
          --muted:#b5b5b5; --accent:#e0457b; --pt:#4fd1c5; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text);
         font-family:"Segoe UI",Arial,sans-serif; }
  h1 { margin:0 0 4px; } h2 { margin:0 0 12px; font-size:16px; }
  .sub { color:var(--muted); margin-bottom:20px; }
  .summary { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:20px; }
  .summary div { background:var(--surface); border:1px solid var(--border); border-radius:8px;
                 padding:12px 18px; color:var(--muted); font-size:12px; }
  .summary b { display:block; font-size:22px; color:var(--pt); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(520px,1fr)); gap:16px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; }
  .card.wide { grid-column:1/-1; }
  .card { display:flex; flex-direction:column; }
  .chart { position:relative; flex:1 1 auto; min-height:320px; }
  .chart.tall { min-height:440px; }
  .chart > canvas { position:absolute; left:0; top:0; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th,td { padding:5px 8px; text-align:right; border-bottom:1px solid var(--border); }
  th:first-child,td:first-child { text-align:left; }
  th { color:var(--muted); position:sticky; top:0; background:var(--surface); cursor:default; }
  .note { color:var(--muted); font-size:12px; margin-top:8px; }
  .scroll { max-height:480px; overflow:auto; }
  .rangebar { display:flex; margin-bottom:16px; }
  .rangebar button { background:var(--soft); color:var(--muted); border:1px solid var(--border); margin-left:-1px;
                     padding:6px 16px; cursor:pointer; font:inherit; font-size:13px; }
  .rangebar button:first-child { border-radius:6px 0 0 6px; margin-left:0; }
  .rangebar button:last-child { border-radius:0 6px 6px 0; }
  .rangebar button:hover { color:var(--text); }
  .rangebar button.active { background:var(--surface); color:var(--pt); border-color:var(--pt); position:relative; z-index:1; }
</style></head><body>
<h1>VPin Play History</h1>
<div class="sub" id="sub"></div>
<div class="rangebar" id="rangeBar"></div>
<div class="summary" id="summary"></div>
<div class="grid">
  <div class="card"><h2>Hours by Manufacturer (top 15)</h2><div class="chart tall"><canvas id="cManu"></canvas></div></div>
  <div class="card"><h2 id="trendTitle">Monthly Play Trend</h2><div class="chart tall"><canvas id="cMonth"></canvas></div></div>
  <div class="card"><h2>Hours by Table Year</h2><div class="chart tall"><canvas id="cYear"></canvas></div></div>
  <div class="card"><h2>Hours by Decade</h2><div class="chart"><canvas id="cDec"></canvas></div></div>
  <div class="card"><h2>Hours by Table Type</h2><div class="chart"><canvas id="cType"></canvas></div></div>
  <div class="card"><h2>Tables by Rating</h2><div class="chart"><canvas id="cRate"></canvas></div></div>
  <div class="card"><h2>Hours by Emulator</h2><div class="chart"><canvas id="cEmu"></canvas></div></div>
  <div class="card"><h2>Tables by Last Played</h2><div class="chart"><canvas id="cRecency"></canvas></div></div>
  <div class="card"><h2>Catalog Coverage: Played vs Owned</h2><div class="chart"><canvas id="cCov"></canvas></div></div>

  <div class="card wide"><h2>Day of Week &times; Hour of Day Heatmap (hours played)</h2><div id="heat2"></div></div>
  <div class="card wide"><h2>Manufacturer &times; Decade Heatmap (hours played)</h2><div id="heat"></div></div>
  <div class="card wide"><h2>Table Year vs Average Session Length</h2>
    <div class="chart tall"><canvas id="cScatter"></canvas></div>
    <div class="note">Bubble size = total hours played. Color = manufacturer (top 8; others grey).</div></div>
  <div class="card wide"><h2>Unique Tables per Year by Manufacturer
    <label style="float:right;font-size:12px;font-weight:normal;margin-left:12px"><input type="checkbox" id="catChk" checked> Show catalog (dashed)</label><select id="binSel" style="float:right;margin-left:12px"><option value="1">1 year</option><option value="5" selected>5 years</option><option value="10">10 years</option></select></h2>
    <div class="chart tall"><canvas id="cManuYear"></canvas></div>
    <div class="note" id="myNote">Count of distinct tables played, by table year and manufacturer (top 8; others grouped).</div></div>
  <div class="card wide"><h2>Top 40 Tables by Time Played</h2><div class="scroll" id="top"></div></div>
</div>
<script>
const ALL = __DATA__;
let D;
const PAL = ['#4fd1c5','#e0457b','#f59e0b','#60a5fa','#a78bfa','#34d399','#f87171','#fbbf24','#38bdf8','#c084fc','#fb923c','#94a3b8'];
const BARPAL = PAL.filter((_,i)=>i!==1); // keep the Sessions line colour unique
Chart.defaults.color = '#d0d0d0'; Chart.defaults.borderColor = '#474747';
const fmt = n => n.toLocaleString();
const generated = new Date().toLocaleString();
const charts = [];
const track = (el, cfg) => { const c = new Chart(el, cfg); charts.push(c); return c; };

const rangeBar = document.getElementById('rangeBar');
rangeBar.innerHTML = ALL.ranges.map(r=>`<button type="button" data-r="${r.key}">${r.label}</button>`).join('');
rangeBar.onclick = e => { const b = e.target.closest('button'); if (b) render(b.dataset.r); };

const bar = (id, labels, data, o={}) => track(document.getElementById(id), {
  type:'bar', data:{labels, datasets:[{data, backgroundColor:o.colors||PAL[0], label:o.label||'Hours'}]},
  options:{indexAxis:o.horizontal?'y':'x', maintainAspectRatio:false, plugins:{legend:{display:false}}}});

function render(key){
charts.splice(0).forEach(c=>c.destroy());
D = ALL.data[key]; D.catalogYears = ALL.catalogYears;
rangeBar.querySelectorAll('button').forEach(b=>b.classList.toggle('active', b.dataset.r===key));
const rangeLabel = ALL.ranges.find(r=>r.key===key).label;
document.getElementById('sub').textContent = `${key==='all' ? 'All time' : 'Last ' + rangeLabel} \u00b7 generated ${generated}` + (D.summary.tables ? '' : ' \u00b7 no play recorded in this period');
document.getElementById('trendTitle').textContent = `${D.trend} Play Trend`;
const S = D.summary;
document.getElementById('summary').innerHTML =
  [['Emulators',S.emus],['Tables played',S.tables],['of catalog',S.catalog],['Sessions',S.plays],['Hours',S.hours],['Manufacturers',S.manus],['Active days',S.activeDays],['Tables / day',S.tablesPerDay],['Sessions / day',S.playsPerDay],['Hours / day',S.hoursPerDay]]
  .map(([k,v])=>`<div><b>${fmt(v)}</b>${k}</div>`).join('');

bar('cManu', D.manu.map(r=>r.name), D.manu.map(r=>r.hours), {horizontal:true});
bar('cYear', D.years.map(r=>r.year), D.years.map(r=>r.hours), {colors:PAL[1]});
bar('cDec', D.decades.map(r=>r.name), D.decades.map(r=>r.hours), {colors:PAL[2]});
bar('cType', D.types.map(r=>r.name), D.types.map(r=>r.hours), {colors:PAL[3]});
bar('cEmu', D.emus.map(r=>r.name), D.emus.map(r=>r.hours), {colors:PAL[6]});
bar('cRate', D.ratings.map(r=>r.name), D.ratings.map(r=>r.tables), {colors:PAL[4], label:'Tables'});
bar('cRecency', D.recency.map(r=>r.label), D.recency.map(r=>r.tables), {colors:PAL[5], label:'Tables'});

track(document.getElementById('cMonth'), {
  data:{labels:D.monthly.map(r=>r.Month), datasets:[
    ...D.monthlyEmu.map((e,i)=>({type:'bar', label:e.name, data:e.hours, backgroundColor:BARPAL[i%BARPAL.length], yAxisID:'y', stack:'hours', order:1})),
    {type:'line', label:'Sessions', data:D.monthly.map(r=>r.Plays), borderColor:PAL[1], backgroundColor:PAL[1], yAxisID:'y1', tension:.25, order:0}]},
  options:{maintainAspectRatio:false, scales:{x:{stacked:true}, y:{stacked:true, title:{display:true,text:'Hours'}},
    y1:{position:'right', grid:{drawOnChartArea:false}, title:{display:true,text:'Sessions'}}}}});

track(document.getElementById('cCov'), {
  type:'bar', data:{labels:D.coverage.map(r=>r.manu), datasets:[
    {label:'Owned', data:D.coverage.map(r=>r.owned), backgroundColor:'#5f5f5f'},
    {label:'Played', data:D.coverage.map(r=>r.played), backgroundColor:PAL[0]}]},
  options:{maintainAspectRatio:false}});

const topManus = D.manu.slice(0,8).map(r=>r.name);
const colorOf = m => { const i = topManus.indexOf(m); return i<0 ? '#777' : PAL[i]; };
track(document.getElementById('cScatter'), {
  type:'bubble', data:{datasets:[...topManus, 'Other'].map(m=>({label:m,
    data:D.scatter.filter(p=>(topManus.includes(p.manu)?p.manu:'Other')===m).map(p=>({x:p.x,y:p.y,r:Math.max(3,Math.min(24,Math.sqrt(p.r)*3)),p})),
    backgroundColor:colorOf(m)+'aa', borderColor:colorOf(m)}))},
  options:{maintainAspectRatio:false, plugins:{legend:{display:true, position:'right'}, tooltip:{callbacks:{label:c=>{
    const p=c.raw.p; return `${p.label} (${p.manu}, ${p.x}) \u2014 ${p.y} min/session, ${p.r} h`;}}}},
    scales:{x:{ticks:{callback:v=>String(v)}, title:{display:true,text:'Table year'}}, y:{title:{display:true,text:'Avg session (min)'}}}}});

// Unique tables per year (or bin) by manufacturer; solid = played, dashed = whole catalog
(function(){
  const series = [...topManus, 'Other'];
  const grp = m => topManus.includes(m) ? m : 'Other';
  const allYears = [...D.scatter.map(p=>p.x), ...D.catalogYears.map(p=>p.x)];
  const minY = Math.min(...allYears), maxY = Math.max(...allYears);
  let chart;
  function tally(rows, start, bin, n){
    const c = {}; series.forEach(s=>c[s]=Array(n).fill(0));
    rows.forEach(p=>{ c[grp(p.manu)][Math.floor((p.x-start)/bin)]++; });
    return c;
  }
  function draw(){
    const bin = +document.getElementById('binSel').value;
    const showCat = document.getElementById('catChk').checked;
    const start = Math.floor(minY/bin)*bin;
    const labels = [];
    for (let y=start; y<=maxY; y+=bin) labels.push(y);
    const played = tally(D.scatter, start, bin, labels.length);
    const owned = tally(D.catalogYears, start, bin, labels.length);
    const pool = series.flatMap(s=>[...played[s], ...(showCat ? owned[s] : [])]).filter(v=>v>0).sort((a,b)=>a-b);
    const peak = pool[pool.length-1] || 1;
    const rough = (pool[Math.floor(pool.length*0.9)] || 1) * 1.5;
    const step = rough<=10 ? 1 : rough<=50 ? 5 : 10;
    const ceil = Math.max(5, Math.ceil(rough/step)*step);
    document.getElementById('myNote').textContent = 'Solid = tables played, dashed = tables in catalog (top 8 manufacturers; others grouped).' +
      (peak > ceil ? ` Y axis capped at ${ceil}; peak is ${peak}; capped values are drawn as triangles at the top.` : '');
    const mk = (s, vals, dashed) => ({label: dashed ? s+' (catalog)' : s, raw:vals, data:vals.map(v=>Math.min(v,ceil)),
      borderColor:colorOf(s)+(dashed?'80':''), backgroundColor:colorOf(s)+(dashed?'80':''),
      borderDash:dashed?[6,4]:[], borderWidth:dashed?1.5:2.5, order:dashed?2:1,
      pointStyle:vals.map(v=>v>ceil?'triangle':'circle'),
      pointRadius:vals.map(v=>v>ceil?7:dashed?0:(bin===1?2:4)), cubicInterpolationMode:'monotone'});
    const datasets = [];
    if (showCat) series.forEach(s=>datasets.push(mk(s, owned[s], true)));
    series.forEach(s=>datasets.push(mk(s, played[s], false)));
    const data = {labels:labels.map(y=>bin===1?y:`${y}-${y+bin-1}`), datasets};
    if (chart) { chart.data = data; chart.options.scales.y.max = ceil; chart.update(); return; }
    chart = track(document.getElementById('cManuYear'), {type:'line', data,
      options:{maintainAspectRatio:false, plugins:{
        legend:{position:'right', labels:{filter:i=>!i.text.endsWith('(catalog)')}},
        tooltip:{callbacks:{label:c=>{const r=c.dataset.raw[c.dataIndex]; return c.dataset.label+': '+r+(r>c.parsed.y?' (off chart)':'');}}}},
        scales:{x:{title:{display:true,text:'Table year'}}, y:{beginAtZero:true, max:ceil, ticks:{precision:0}, title:{display:true,text:'Unique tables'}}}}});
  }
  document.getElementById('binSel').onchange = draw;
  document.getElementById('catChk').onchange = draw;
  draw();
})();
// Heatmap
(function(){
  const H = D.heat, max = Math.max(1, ...H.vals.flat());
  let h = '<table><tr><th>Manufacturer</th>' + H.cols.map(c=>`<th>${c}</th>`).join('') + '</tr>';
  H.rows.forEach((r,i)=>{ h += `<tr><td>${r}</td>` + H.vals[i].map(v=>{
    const a = v ? (0.12 + 0.88*v/max).toFixed(2) : 0;
    return `<td style="background:rgba(79,209,197,${a});color:${a>0.55?'#111':'inherit'}">${v||''}</td>`;}).join('') + '</tr>'; });
  document.getElementById('heat').innerHTML = h + '</table>';
})();

// Day x hour heatmap (hours across, days down)
(function(){
  const V = D.dowHour.hours, P = D.dowHour.plays, max = Math.max(0.01, ...V.flat());
  const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  let h = '<table style="table-layout:fixed"><tr><th style="width:56px"></th>' +
    V[0].map((_,i)=>`<th style="text-align:center;padding:5px 2px">${String(i).padStart(2,'0')}</th>`).join('') + '</tr>';
  V.forEach((row,d)=>{ h += `<tr><td>${days[d]}</td>` + row.map((v,i)=>{
    const a = v ? (0.12 + 0.88*v/max).toFixed(2) : 0;
    return `<td title="${days[d]} ${String(i).padStart(2,'0')}:00 \u2014 ${v} h, ${P[d][i]} sessions" style="text-align:center;padding:8px 2px;background:rgba(79,209,197,${a});color:${a>0.55?'#111':'inherit'}">${v?v.toFixed(1):''}</td>`;}).join('') + '</tr>'; });
  document.getElementById('heat2').innerHTML = h + '</table>';
})();
// Top tables
document.getElementById('top').innerHTML =
  '<table><tr><th>Table</th><th>Manufacturer</th><th>Year</th><th>Type</th><th>Rating</th><th>Sessions</th><th>Hours</th><th>Avg min</th><th>Last played (days)</th></tr>' +
  D.top.map(r=>`<tr><td>${r.name}</td><td>${r.manu}</td><td>${r.year??''}</td><td>${r.type}</td><td>${r.rating?r.rating+'\u2605':''}</td><td>${r.plays}</td><td>${r.hours}</td><td>${r.avg}</td><td>${r.last}</td></tr>`).join('') + '</table>';
  }
  render(ALL.default);
  </script></body></html>
"""


def main():
    payload = build_all()
    if not payload["data"]["all"]["summary"]["tables"]:
        sys.exit("No play history found.")
    data = json.dumps(payload, default=str).replace("</", "<\\/")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(HTML.replace("__DATA__", data))
    shown = payload["data"][payload["default"]]["summary"]["tables"]
    print(f"Wrote {OUT} ({shown} tables in default range '{payload['default']}')")


if __name__ == "__main__":
    main()
