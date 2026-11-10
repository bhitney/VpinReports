"""
Builds VpinStatsReport.html: one self-contained HTML document with two tabs.

  Tab 1 (default): Play History      -- data/charts from history_report.py
  Tab 2:           Performance Stats -- from VPinPerfMon's scatter report (perf_report.py)

Both sections share one stylesheet/theme. Their scripts are each wrapped in
their own scope so names never collide. Settings live in report_config.py.

Usage:
    python build_report.py [--db PUPDatabase.db] [--emus 4,6,10] [--days N]
                           [--perf-csv CustomPerfStats_*.csv] [--perf-logs LOGS_DIR]
                           [--out file.html]

Command-line options override report_config.py. If either section has no
data, its tab shows a message instead of failing the whole report.
"""
import argparse
import html
import json
import os
import re
import runpy
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pandas as pd  # noqa: E402

import report_config as cfg  # noqa: E402


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=cfg.DB_PATH, help="PUPDatabase.db path")
    ap.add_argument("--emus", default=cfg.TARGET_EMU_IDS, help="comma-separated emulator IDs")
    ap.add_argument("--days", type=int, default=int(cfg.HISTORY_DAYS), help="history window in days")
    ap.add_argument("--perf-csv", default=cfg.PERF_CSV, help="pre-extracted CustomPerfStats CSV for the perf tab")
    ap.add_argument("--perf-logs", action="append", default=None,
                    help="VPinPerfMon logs folder; repeat for alternate locations (overrides PERF_LOGS_DIR)")
    ap.add_argument("--out", default=os.path.join(HERE, "VpinStatsReport.html"))
    args = ap.parse_args()
    if args.perf_logs is None:
        args.perf_logs = getattr(cfg, "PERF_LOGS_DIR", None)
    args.emus = ",".join(p.strip() for p in str(args.emus).split(",") if p.strip())
    if not re.fullmatch(r"\d+(,\d+)*", args.emus):
        sys.exit(f"Emulator IDs must be comma-separated integers, got: {args.emus!r}")
    return args


def message(text):
    return {"css": "", "body": f'<p class="muted">{html.escape(text)}</p>', "script": ""}


# ---------------------------------------------------------------------------
# Play History section
# ---------------------------------------------------------------------------
HISTORY_CSS = """#tab-history {
  .card { display: flex; flex-direction: column; }
  .chart { position: relative; flex: 1 1 auto; min-height: 320px; }
  .chart.tall { min-height: 440px; }
  .chart > canvas { position: absolute; left: 0; top: 0; }
  .card.wide { grid-column: 1 / -1; }
  .grid { grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); }
  .note { color: var(--cp-text-muted); font-size: 12px; margin-top: 8px; }
  .scroll { max-height: 480px; overflow: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { padding: 5px 8px; text-align: right; border-bottom: 1px solid var(--cp-border); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--cp-text-muted); position: sticky; top: 0; background: var(--cp-surface); font-weight: 600; }
  .card h2 select, .card h2 input { font-size: 12px; }
  .rangebar { display: flex; margin-bottom: 16px; }
  .rangebar button { background: var(--cp-surface-soft); color: var(--cp-text-muted); border: 1px solid var(--cp-border);
                     margin-left: -1px; padding: 6px 16px; cursor: pointer; font: inherit; font-size: 13px; }
  .rangebar button:first-child { border-radius: var(--radius) 0 0 var(--radius); margin-left: 0; }
  .rangebar button:last-child { border-radius: 0 var(--radius) var(--radius) 0; }
  .rangebar button:hover { color: var(--cp-text); }
  .rangebar button.active { background: var(--cp-surface); color: var(--cp-chart-point); border-color: var(--cp-chart-point);
                            position: relative; z-index: 1; }
}
"""


def build_history(args):
    # The history script reads its settings at import time.
    os.environ["VPIN_EMU_IDS"] = args.emus
    sys.argv = [sys.argv[0], str(args.days)]
    import history_report as hist

    hist.DB_PATH = args.db
    payload = hist.build_all()
    default = payload["data"][payload["default"]]
    if not payload["data"]["all"]["summary"]["tables"]:
        return message(f"No play history found for emulators {args.emus}."), "no data"
    data = json.dumps(payload, default=str).replace("</", "<\\/")

    # Reuse the original page template, keeping only its body markup and script.
    tpl = hist.HTML
    body = tpl.split("</style></head><body>", 1)[1].split("<script>", 1)[0]
    script = tpl.split("<script>", 1)[1].rsplit("</script>", 1)[0].replace("__DATA__", data)
    body = body.replace("<h1>VPin Play History</h1>", "<h1>Play History</h1>")
    body = body.replace('<div class="sub" id="sub">', '<div class="subtitle" id="sub">')
    section = {"css": HISTORY_CSS, "body": body, "script": f"(() => {{\n{script}\n}})();"}
    label = next(r["label"] for r in payload["ranges"] if r["key"] == payload["default"])
    return section, f"{default['summary']['tables']} tables, {label}"


# ---------------------------------------------------------------------------
# Performance Stats section
# ---------------------------------------------------------------------------
RECS_CSS = """
  #tab-recs .cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); gap: 20px; align-items: start; }
  #tab-recs .rec { display: flex; gap: 12px; align-items: center; padding: 5px 0; border-bottom: 1px solid var(--cp-border); }
  #tab-recs .rec-wheel { width: 75px; flex: none; height: auto; }
  #tab-recs .rec-wheel.dflt { opacity: .55; }
  #tab-recs .rec-wheel.none { display: none; }
  #tab-recs .rec-main { flex: 1; min-width: 0; }
  #tab-recs .rec:last-child { border-bottom: none; }
  #tab-recs .rec-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
  #tab-recs .rec-name { font-weight: 600; color: var(--cp-text); }
  #tab-recs .rec-id { color: var(--cp-text-muted); font-size: 11px; margin-left: 6px; }
  #tab-recs .rec-conf { color: var(--cp-chart-point); font-weight: 600; font-size: 13px; white-space: nowrap; }
  #tab-recs .bar { height: 4px; background: var(--cp-surface-soft); border-radius: 2px;   margin: 4px 0; }
  #tab-recs .bar i { display: block; height: 100%; background: var(--cp-chart-point); border-radius: 2px; }
  #tab-recs .rec-reason { color: var(--cp-text-muted); font-size: 13px; line-height: 1.4; }
"""


def load_wheels(ids):
    """Map GameID -> data URI of the wheel image (downscaled), where one is found."""
    import base64
    import difflib
    import glob
    import io

    from PIL import Image

    wheels = {}
    if not ids:
        return wheels
    marks = ",".join("?" * len(ids))
    con = sqlite3.connect(cfg.DB_PATH)
    try:
        rows = con.execute(
            f"SELECT g.GameID, g.GameName, g.GameFileName, e.DirMedia FROM Games g "
            f"JOIN Emulators e ON e.EMUID = g.EMUID WHERE g.GameID IN ({marks})", list(ids)
        ).fetchall()
    finally:
        con.close()
    def squash(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def title(s):  # name without (...) / [...] qualifiers such as manufacturer, year, VPW
        return squash(re.sub(r"\(.*?\)|\[.*?\]", "", s))

    def to_uri(p):
        try:
            with Image.open(p) as im:
                im.seek(0)  # first frame for apng/gif
                im = im.convert("RGBA")
                w = cfg.WHEEL_WIDTH
                im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "WEBP", quality=82, method=6)
            return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    ext_rank = {".png": 0, ".jpg": 1, ".jpeg": 1, ".webp": 2, ".apng": 3, ".gif": 4}
    listings = {}

    def listing(wheel_dir):
        if wheel_dir not in listings:
            items = []
            for p in glob.glob(os.path.join(glob.escape(wheel_dir), "*.*")):
                ext = os.path.splitext(p)[1].lower()
                if ext in ext_rank:
                    s = os.path.splitext(os.path.basename(p))[0]
                    items.append((p, squash(s), title(s), ext_rank[ext]))
            listings[wheel_dir] = items
        return listings[wheel_dir]

    def find_wheel(wheel_dir, names):
        items = listing(wheel_dir)
        if not items:
            return None
        for name in names:  # 1) same name ignoring case/punctuation
            hits = [i for i in items if i[1] == squash(name)]
            if hits:
                return min(hits, key=lambda i: i[3])[0]
        best, best_score = None, 0.0
        for name in names:
            full, t = squash(name), title(name)
            if len(t) < 4:
                continue
            for p, wfull, wt, rank in items:
                if len(wt) < 4:
                    continue
                if wt == t:
                    score = 2.0  # same title, different qualifiers
                elif wt.startswith(t) or t.startswith(wt):
                    score = 1.5 * min(len(wt), len(t)) / max(len(wt), len(t)) + 0.5
                else:
                    score = difflib.SequenceMatcher(None, wt, t).ratio()
                    if score < 0.85:
                        continue
                score += difflib.SequenceMatcher(None, wfull, full).ratio() * 0.1 - rank * 0.001
                if score > best_score:
                    best, best_score = p, score
        return best

    default_cache = {}
    for gid, gname, fname, dirmedia in rows:
        if not dirmedia:
            continue
        names = [n for n in (os.path.splitext(os.path.basename(fname or ""))[0], gname) if n]
        p = find_wheel(os.path.join(dirmedia, "Wheel"), names)
        uri = to_uri(p) if p else None
        if uri:
            wheels[gid] = (uri, False)
            continue
        if dirmedia not in default_cache:
            d = os.path.join(dirmedia, "system", "pup_default.png")
            default_cache[dirmedia] = to_uri(d) if os.path.isfile(d) else None
        if default_cache[dirmedia]:
            wheels[gid] = (default_cache[dirmedia], True)
    return wheels


def build_recs():
    """Return (section, info) for the AI Recommendations tab, or None if no file."""
    path = os.path.abspath(cfg.RECS_PATH)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    all_ids = {int(r["id"]) for k in ("general", "em") for r in data.get(k, []) if str(r.get("id", "")).isdigit()}
    try:
        wheels = load_wheels(all_ids)
    except Exception as e:
        print(f"Wheel images skipped: {type(e).__name__}: {e}")
        wheels = {}

    def column(title, items):
        rows = []
        for r in sorted(items, key=lambda r: -float(r.get("confidence", 0))):
            conf = float(r.get("confidence", 0))
            uri, dflt = wheels.get(int(r["id"]), (None, False)) if str(r.get("id", "")).isdigit() else (None, False)
            img = (f'<img class="rec-wheel{" dflt" if dflt else ""}" src="{uri}" alt="">'
                   if uri else '<div class="rec-wheel none"></div>')
            rows.append(
                f'<div class="rec">{img}<div class="rec-main"><div class="rec-head"><span>'
                f'<span class="rec-name">{html.escape(str(r.get("game", "")))}</span>'
                f'<span class="rec-id">#{html.escape(str(r.get("id", "")))}</span></span>'
                f'<span class="rec-conf">{conf:.0%}</span></div>'
                f'<div class="bar"><i style="width:{max(0, min(conf, 1)) * 100:.0f}%"></i></div>'
                f'<div class="rec-reason">{html.escape(str(r.get("reason", "")))}</div></div></div>'
            )
        body = "".join(rows) or '<div class="muted">No recommendations.</div>'
        return f'<div class="card"><h2>{title} ({len(items)})</h2>{body}</div>'

    general, em = data.get("general", []), data.get("em", [])
    stamp = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    body = (
        "<h1>AI Recommendations</h1>"
        f'<p class="subtitle">From {html.escape(os.path.basename(path))} &middot; updated {stamp}</p>'
        '<div class="cols">'
        + column("SS / DMD / LCD", general)
        + column("Electro-Mechanical (EM)", em)
        + "</div>"
    )
    section = {"css": RECS_CSS, "body": body, "script": ""}
    return section, f"{len(general) + len(em)} tables"


def build_perf(args):
    if args.perf_csv:
        src, label = args.perf_csv, os.path.basename(args.perf_csv)
    else:
        import extract_perf_stats as ex

        if not os.path.exists(args.db):
            return message(f"Database not found at {args.db}"), "no data"
        con = sqlite3.connect(args.db)
        try:
            query = ex.build_query(con).rstrip() + f"\nWHERE g.EMUID IN ({args.emus})\n"
            src = pd.read_sql_query(query, con)
        except Exception as e:
            return message(f"Could not read CustomPerfStats: {e}"), "no data"
        finally:
            con.close()
        if src.empty:
            return message(f"No performance captures found for emulators {args.emus}."), "no data"
        label = f"{os.path.basename(args.db)} (emulators {args.emus})"

    # perf_report.py is a top-level script; run it with its inputs injected.
    ns = runpy.run_path(
        os.path.join(HERE, "perf_report.py"),
        init_globals={"SRC": src, "SRC_LABEL": html.escape(label), "EMU_COLORS": getattr(cfg, "EMU_COLORS", {}),
                      "SEARCH_HIGHLIGHT_COLOR": getattr(cfg, "SEARCH_HIGHLIGHT_COLOR", None),
                      "PERF_LOGS_DIR": args.perf_logs},
    )
    section = {"css": ns["PERF_CSS"], "body": ns["PERF_BODY"], "script": ns["PERF_SCRIPT"]}
    return section, ns["REPORT_INFO"], ns["CHART_POINT_COLOR"]


# ---------------------------------------------------------------------------
# Shared shell: theme, tabs, common component styles
# ---------------------------------------------------------------------------
SHELL = """<!DOCTYPE html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<title>VPin Stats Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js"></script>
<style>
  :root {
    color-scheme: dark;
    --cp-bg: #3d3b3a; --cp-bg-elevated: #343231; --cp-surface: #292929; --cp-surface-soft: #2e2e2e;
    --cp-border: #474747; --cp-border-strong: #5f5f5f;
    --cp-text: #dedede; --cp-text-muted: #919191; --cp-text-soft: #b0b0b0;
    --cp-accent: #fd8ea1; --cp-success: #4ade80; --cp-danger: #f87171; --cp-warning: #fbbf24; --cp-link: #4da6ff;
    --cp-overlay: rgba(41, 41, 41, 0.88);
    --cp-chart-point: __POINT__;
    --radius: 0.625rem;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--cp-bg); color: var(--cp-text);
         font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif; }
  body.chart-expanded { overflow: hidden; }
  body.chart-expanded::before { content: ""; position: fixed; inset: 0; z-index: 999; background: var(--cp-overlay); }

  .tabs { position: sticky; top: 0; z-index: 100; display: flex; gap: 4px; padding: 8px 24px 0;
          background: var(--cp-surface); border-bottom: 1px solid var(--cp-border); }
  .logo { display:flex; align-items:center; gap:8px; margin-right:12px; padding:0 14px 6px 0; align-self:center; user-select:none; pointer-events:none; cursor:default;
          font-weight:700; font-size:15px; letter-spacing:.3px; color:var(--cp-text); border-right:1px solid var(--cp-border); }
  .logo svg { display:block; }
  .logo em { font-style:normal; color:var(--cp-chart-point); }
  .tab { background: var(--cp-surface-soft); color: var(--cp-text-muted); border: 1px solid var(--cp-border);
         border-bottom: none; border-radius: var(--radius) var(--radius) 0 0; padding: 9px 20px;
         cursor: pointer; font: inherit; font-size: 14px; }
  .tab:hover { color: var(--cp-text); }
  .tab.active { background: var(--cp-bg); color: var(--cp-text); border-top: 2px solid var(--cp-accent); }
  .tab small { color: var(--cp-chart-point); margin-left: 8px; }
  .generated { margin-left: auto; align-self: center; color: var(--cp-text-muted); font-size: 12px; padding-bottom: 6px; }

  .panel { display: none; padding: 24px; }
  .panel.active { display: block; }
  .panel h1 { font-size: 22px; margin: 0 0 4px; }
  .subtitle { color: var(--cp-text-muted); margin: 0 0 20px; font-size: 13px; }
  .muted { color: var(--cp-text-muted); }

  .summary { display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0 24px; }
  .summary div { background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: var(--radius);
                 padding: 10px 16px; font-size: 13px; color: var(--cp-text-muted); }
  .summary b { display: block; font-size: 18px; color: var(--cp-chart-point); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 20px; }
  .card { background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 16px; padding: 16px; }
  .card h2 { font-size: 14px; margin: 0 0 10px; font-weight: 600; color: var(--cp-text); }
  select, input[type=text], input[type=date] { padding: 6px 10px; border-radius: var(--radius);
         border: 1px solid var(--cp-border); background: var(--cp-surface); color: var(--cp-text); font: inherit; font-size: 13px; }
</style>
<style>
__PANEL_CSS__
</style></head><body>
<div class="tabs" role="tablist">
  <span class="logo" aria-hidden="true"><svg width="30" height="22" viewBox="0 0 30 22"><circle cx="15" cy="5" r="3.5" fill="#e8e8e8"/><path d="M2 8 L13 17 L11 20 L2 13 Z" fill="var(--cp-accent)"/><path d="M28 8 L17 17 L19 20 L28 13 Z" fill="var(--cp-accent)"/></svg>VPin <em>Stats</em></span>
  <button class="tab active" role="tab" data-tab="history">Play History<small>__HIST_INFO__</small></button>
  <button class="tab" role="tab" data-tab="perf">Performance Stats<small>__PERF_INFO__</small></button>
  __RECS_TAB__  <span class="generated">Generated __STAMP__</span>
</div>
<section id="tab-history" class="panel active">
__HIST_BODY__
</section>
<section id="tab-perf" class="panel">
__PERF_BODY__
</section>
__RECS_PANEL__
<script>
Chart.defaults.color = '#d0d0d0'; Chart.defaults.borderColor = '#474747';
__HIST_SCRIPT__
__PERF_SCRIPT__
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  // Charts built while their tab was hidden need a size pass once visible.
  Object.values(Chart.instances).forEach(c => c.resize());
  history.replaceState(null, '', '#' + name);
}
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => showTab(t.dataset.tab)));
const startTab = location.hash.slice(1);
showTab(document.getElementById('tab-' + startTab) ? startTab : 'history');
</script></body></html>
"""


def main():
    args = parse_args()
    point = "#4fd1c5"
    sections, infos = {}, {}
    for name, builder in (("history", build_history), ("perf", build_perf)):
        try:
            result = builder(args)
        except SystemExit as e:
            result = (message(str(e.code)), "unavailable")
        except Exception as e:
            result = (message(f"{type(e).__name__}: {e}"), "error")
        sections[name], infos[name] = result[0], result[1]
        if len(result) > 2:
            point = result[2]

    def safe(s):  # keep embedded content from closing its script/style tag
        return s.replace("</script", "<\\/script").replace("</style", "<\\/style")

    recs_tab = recs_panel = recs_css = ""
    recs = build_recs()
    if recs:
        recs_tab = ('<button class="tab" role="tab" data-tab="recs">AI Recommendations'
                    f'<small>{html.escape(recs[1])}</small></button>')
        recs_panel = f'<section id="tab-recs" class="panel">\n{recs[0]["body"]}\n</section>'
        recs_css = recs[0]["css"]

    out = SHELL
    for key, val in (
        ("__PANEL_CSS__", safe(sections["history"]["css"] + sections["perf"]["css"] + recs_css)),
        ("__RECS_TAB__", recs_tab),
        ("__RECS_PANEL__", recs_panel),
        ("__HIST_BODY__", sections["history"]["body"]),
        ("__PERF_BODY__", sections["perf"]["body"]),
        ("__HIST_SCRIPT__", safe(sections["history"]["script"])),
        ("__PERF_SCRIPT__", safe(sections["perf"]["script"])),
        ("__HIST_INFO__", html.escape(infos["history"])),
        ("__PERF_INFO__", html.escape(infos["perf"])),
        ("__STAMP__", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("__POINT__", point),
    ):
        out = out.replace(key, val)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"Wrote {args.out}  [history: {infos['history']}] [perf: {infos['perf']}]")


if __name__ == "__main__":
    main()
