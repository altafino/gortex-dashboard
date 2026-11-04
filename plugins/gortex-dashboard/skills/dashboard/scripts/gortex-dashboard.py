#!/usr/bin/env python3
"""Gortex dashboard — snapshot the daemon + savings DB, render one HTML page.

Data: `gortex daemon status`, `gortex repos --json`, the daemon sidecar SQLite
(tool-call token savings, memories, promoted tools). Every run appends a row to
~/.local/share/gortex-dashboard/dashboard.sqlite so index growth is chartable.

Usage: gortex-dashboard.py [--open] [--no-snapshot] [--print] [--out PATH]
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

HOME = os.path.expanduser("~")
OUT_DIR = os.path.join(HOME, ".local/share/gortex-dashboard")
DB_PATH = os.path.join(OUT_DIR, "dashboard.sqlite")
HTML_PATH = os.path.join(OUT_DIR, "index.html")
ECHARTS = os.path.join(OUT_DIR, "echarts.min.js")
ECHARTS_URL = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"
SIDECARS = [
    os.path.join(HOME, ".gortex/sidecar.sqlite"),
    os.path.join(HOME, ".local/share/gortex/sidecar.sqlite"),
]

CONFIG = os.path.join(HOME, ".config/gortex/config.yaml")
REPO_ROOTS = ["~/go/src", "~/src", "~/Projects", "~/code", "~/dev"]


# ---------------------------------------------------------------- shell helpers


def run(cmd, timeout=180):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=HOME)
        return p.stdout + (p.stderr if p.returncode else "")
    except Exception:
        return ""


SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def parse_size(s):
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", s or "")
    return float(m.group(1)) * SIZE_UNITS.get(m.group(2), 1) if m else 0.0


def parse_duration(s):
    total, found = 0, False
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)([dhms])", s or ""):
        found = True
        total += float(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return int(total) if found else 0


# ------------------------------------------------------------------- collectors


def box_tables(text):
    """Split the box-drawn tables in `gortex daemon status` into row dicts."""
    groups, cur = [], []
    for line in text.splitlines():
        if line.startswith("│"):
            cur.append([c.strip() for c in line.strip().strip("│").split("│")])
        elif line[:1] in "┌├└":
            continue  # border/separator inside the same table
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [dict(zip(g[0], row)) for g in groups if len(g) > 1
            for row in g[1:] if len(row) == len(g[0])]


def collect_status():
    text = run(["gortex", "daemon", "status"])
    kv = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([a-z_][a-z_0-9]*)\s{2,}(\S.*?)\s*$", line)
        if m:
            kv[m.group(1)] = m.group(2)
    daemon = {
        "version": (kv.get("daemon") or "").split(" ")[0],
        "pid": int(kv["pid"]) if (kv.get("pid") or "").isdigit() else None,
        "socket": kv.get("socket", ""),
        "uptime_s": parse_duration(kv.get("uptime")),
        "state": kv.get("state", ""),
        "sessions": int(kv["sessions"]) if (kv.get("sessions") or "").isdigit() else 0,
        "memory_bytes": parse_size(kv.get("memory")),
        "search_docs": int(m.group(1)) if (m := re.search(r"docs=(\d+)", kv.get("search", ""))) else 0,
        "search_engine": (kv.get("search", "").split("  ") or [""])[0],
        "goroutines": int(m.group(1)) if (m := re.search(r"goroutines=(\d+)", kv.get("runtime", ""))) else 0,
    }
    wal = re.search(r"wal=([\d.]+\s*\w+B)", text)
    db = re.search(r"db=([\d.]+\s*\w+B)", text)
    reclaim = re.search(r"bytes=([\d.]+\s*\w+B)\s*$", text, re.M)
    storage = {
        "db_bytes": parse_size(db.group(1)) if db else 0,
        "wal_bytes": parse_size(wal.group(1)) if wal else 0,
        "reclaimed_bytes": parse_size(reclaim.group(1)) if reclaim else 0,
    }
    repos, sessions = [], []
    for row in box_tables(text):
        if row.get("repo") and row["repo"] != "other":
            indexed = (row.get("files") or "").isdigit()
            repos.append({
                "name": row["repo"],
                "bytes": parse_size(row.get("total")),
                "files": int(row["files"]) if indexed else 0,
                "nodes": int(row["nodes"]) if indexed else 0,
                "edges": int(row["edges"]) if indexed else 0,
                "indexed": indexed,
                "path": row.get("path", ""),
            })
        elif row.get("client") is not None and row.get("id"):
            sessions.append({
                "id": row.get("id", ""),
                "client": row.get("client", ""),
                "version": row.get("version", ""),
                "connected": row.get("connected", ""),
                "cwd": row.get("cwd", ""),
            })
    return daemon, repos, sessions, storage


def collect_repos_json():
    try:
        data = json.loads(run(["gortex", "repos", "--json"]) or "[]")
    except Exception:
        return {}
    return {r.get("name"): r for r in data if isinstance(r, dict)}


def sidecar():
    best = None
    for path in SIDECARS:
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            n = con.execute("select count(*) from savings_events").fetchone()[0]
            con.close()
        except Exception:
            n = -1
        if best is None or n > best[1]:
            best = (path, n)
    return best[0] if best else None


def collect_savings():
    empty = {"events": 0, "saved": 0, "returned": 0, "calls": 0,
             "by_day": [], "by_tool": [], "by_lang": [], "by_client": [], "by_repo": [],
             "first_ts": None, "last_ts": None, "memories": 0, "memory_kinds": [], "promoted": []}
    path = sidecar()
    if not path:
        return empty
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = dict(empty)

    def rows(sql):
        try:
            return [dict(r) for r in con.execute(sql)]
        except Exception:
            return []

    out["by_day"] = rows(
        "select date(ts/1000000000,'unixepoch','localtime') day, sum(saved) saved,"
        " sum(returned) returned, count(*) calls from savings_events"
        " group by day order by day")
    out["by_tool"] = rows(
        "select tool, count(*) calls, sum(saved) saved from savings_events"
        " group by tool order by saved desc")
    out["by_lang"] = rows(
        "select case when language='' then 'unknown' else language end language,"
        " sum(saved) saved, count(*) calls from savings_events group by 1 order by saved desc")
    out["by_client"] = rows(
        "select case when client='' then 'cli / other' else client end client, count(*) calls,"
        " sum(saved) saved from savings_events group by 1 order by calls desc")
    out["by_repo"] = rows(
        "select repo, count(*) calls, sum(saved) saved from savings_events"
        " group by repo order by saved desc")
    tot = rows("select sum(saved) saved, sum(returned) returned, count(*) calls,"
               " min(ts) first_ts, max(ts) last_ts from savings_events")
    if tot:
        out.update({k: tot[0][k] for k in ("saved", "returned", "calls", "first_ts", "last_ts")})
        out["events"] = out["calls"] or 0
    out["memories"] = (rows("select count(*) n from memories") or [{"n": 0}])[0]["n"]
    out["memory_kinds"] = rows(
        "select kind, count(*) n from memories group by kind order by n desc")
    out["promoted"] = rows(
        "select tool, workspace_id, use_count from promoted_tools order by use_count desc limit 8")
    con.close()
    return out


# ------------------------------------------------------------- repo management


def config_paths():
    """Repo paths listed in the global gortex config (tracked, indexed or not)."""
    try:
        with open(CONFIG) as f:
            return [os.path.realpath(os.path.expanduser(l.split("path:", 1)[1].strip()))
                    for l in f if "path:" in l]
    except Exception:
        return []


def candidates():
    """Git repos on disk that the config does not list yet."""
    tracked = set(config_paths())
    out = []
    for root in REPO_ROOTS:
        base = os.path.expanduser(root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            if dirpath[len(base):].count(os.sep) > 3:
                dirnames[:] = []
                continue
            if ".git" in dirnames or ".git" in filenames:
                dirnames[:] = []  # don't walk into a repo
                real = os.path.realpath(dirpath)
                if real not in tracked:
                    out.append({"name": os.path.basename(dirpath), "path": real})
    return sorted(out, key=lambda c: c["name"].lower())


def do_track(path):
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isdir(path):
        return {"ok": False, "message": f"not a directory: {path}"}
    if os.path.realpath(path) in set(config_paths()):
        return {"ok": True, "message": f"already tracked: {path}"}
    out = run(["gortex", "track", path], timeout=900)
    for _ in range(20):  # a track can take a while to show up in the daemon view
        if os.path.realpath(path) in set(config_paths()):
            return {"ok": True, "message": (out.strip().splitlines() or ["tracked"])[-1]}
        time.sleep(0.5)
    return {"ok": False, "message": out.strip()[-800:] or "track command did not confirm"}


def do_untrack(path):
    path = os.path.abspath(os.path.expanduser(path or ""))
    out = run(["gortex", "untrack", path, "--confirm"], timeout=900)
    # untrack drops the index first and the config entry on the second pass
    if os.path.realpath(path) in set(config_paths()):
        out += run(["gortex", "untrack", path, "--confirm"], timeout=900)
    gone = os.path.realpath(path) not in set(config_paths())
    return {"ok": gone, "message": (out.strip().splitlines() or ["untracked"])[-1]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self):
        data = build(snapshot=False)
        data["served"] = True
        data["candidates"] = candidates()
        render(data, HTML_PATH)
        with open(HTML_PATH, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html"):
            self._page()
        elif route == "/echarts.min.js":
            try:
                with open(ECHARTS, "rb") as f:
                    self._send(200, f.read(), "application/javascript")
            except OSError:
                self._send(404, {"error": "echarts not cached; rerun with network access"})
        elif route == "/favicon.ico":
            self._send(204, b"")
        elif route == "/api/candidates":
            self._send(200, candidates())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.split("?")[0]
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if route == "/api/track":
            self._send(200, do_track(body.get("path", "")))
        elif route == "/api/untrack":
            self._send(200, do_untrack(body.get("path", "")))
        else:
            self._send(404, {"error": "not found"})


def serve(port, open_browser=True):
    data = build()  # one snapshot per server start, not per request
    data["served"] = True
    data["candidates"] = candidates()
    render(data, HTML_PATH)
    url = f"http://127.0.0.1:{port}/"
    print(url)
    if open_browser:
        webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


# ---------------------------------------------------------------- snapshot store


def store_snapshot(daemon, repos, savings):
    os.makedirs(OUT_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        create table if not exists snapshots(
          ts integer primary key, version text, uptime_s integer, sessions integer,
          memory_bytes integer, repos integer, files integer, nodes integer, edges integer,
          db_bytes integer, saved integer, calls integer);
        create table if not exists repo_snapshots(
          ts integer, repo text, files integer, nodes integer, edges integer, bytes integer,
          primary key(ts, repo));
    """)
    ts = int(time.time())
    if not repos:  # daemon down or still warming: nothing worth plotting
        hist = con.execute("select 1").fetchall()
        con.close()
        return []
    con.execute("insert or replace into snapshots values(?,?,?,?,?,?,?,?,?,?,?,?)", (
        ts, daemon["version"], daemon["uptime_s"], daemon["sessions"], int(daemon["memory_bytes"]),
        len(repos), sum(r["files"] for r in repos), sum(r["nodes"] for r in repos),
        sum(r["edges"] for r in repos), int(sum(r["bytes"] for r in repos)),
        int(savings["saved"] or 0), int(savings["calls"] or 0)))
    con.executemany("insert or replace into repo_snapshots values(?,?,?,?,?,?)",
                    [(ts, r["name"], r["files"], r["nodes"], r["edges"], int(r["bytes"])) for r in repos])
    con.commit()
    hist = [dict(zip(("ts", "nodes", "edges", "files"), r)) for r in con.execute(
        "select ts, nodes, edges, files from snapshots where nodes > 0"
        " order by ts desc limit 120")]
    con.close()
    return list(reversed(hist))


# --------------------------------------------------------------------- rendering

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gortex — dashboard</title>
<script src="__ECHARTS__"></script>
<style>
:root{
  --bg:#080b12; --card:#0f1521; --card2:#121a29; --line:#1e2a3d;
  --ink:#e6edf7; --muted:#8496b0; --dim:#5b6b84;
  --vio:#7c5cff; --cyn:#22d3ee; --grn:#34d399; --amb:#fbbf24; --red:#f87171;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 15% -10%,#151d33 0%,var(--bg) 60%) no-repeat,var(--bg);
  color:var(--ink);font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;padding:26px 26px 54px}
.mono{font-family:ui-monospace,"JetBrains Mono",SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:1440px;margin:0 auto}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.logo{display:flex;align-items:baseline;gap:10px}
.logo b{font-size:22px;letter-spacing:.16em;font-weight:700}
.logo b span{color:var(--vio)}
.chip{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;
  background:var(--card);border:1px solid var(--line);color:var(--muted);font-size:12px;white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:var(--grn);box-shadow:0 0 0 3px rgba(52,211,153,.15)}
.dot.bad{background:var(--red);box-shadow:0 0 0 3px rgba(248,113,113,.15)}
.grow{flex:1}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(4,1fr);margin-bottom:16px}
@media(max-width:1100px){.kpis{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.kpis{grid-template-columns:1fr}}
.card{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);
  border-radius:14px;padding:16px 18px}
.kpi .lbl{font-size:11px;letter-spacing:.13em;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
.kpi .val{font-size:26px;font-weight:650;letter-spacing:-.02em}
.kpi .sub{font-size:11.5px;color:var(--muted);margin-top:5px}
.kpi.hl{background:linear-gradient(160deg,rgba(124,92,255,.20),rgba(34,211,238,.06)),var(--card);
  border-color:#33306a}
.charts{grid-template-columns:repeat(12,1fr)}
.c{grid-column:span 6;padding:14px 16px 10px}
.c.wide{grid-column:span 12}
.c.third{grid-column:span 4}
.chart{width:100%;height:268px}
.c h3{margin:2px 0 10px;font-size:12px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;padding:0 14px 8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child{padding-left:0}
th.r{padding-right:0}
td{padding:8px 14px 8px 12px;border-bottom:1px solid rgba(30,42,61,.55);vertical-align:top}
td:first-child{padding-left:0}
tr:last-child td{border-bottom:0}
td.r,th.r{text-align:right;padding-right:0}
.tag{padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid}
.tag.ok{color:var(--grn);border-color:rgba(52,211,153,.35);background:rgba(52,211,153,.08)}
.tag.warn{color:var(--amb);border-color:rgba(251,191,36,.35);background:rgba(251,191,36,.08)}
.tag.mut{color:var(--muted);border-color:var(--line);background:rgba(148,163,184,.06)}
.mut{color:var(--muted)}
.dim{color:var(--dim)}
footer{margin-top:26px;color:var(--dim);font-size:12px;display:flex;gap:14px;flex-wrap:wrap}
.row{display:flex;gap:8px;align-items:center}
input[type=text]{flex:1;min-width:0;background:#0b1220;border:1px solid var(--line);color:var(--ink);
  border-radius:9px;padding:9px 12px;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;outline:none}
input[type=text]::placeholder{color:#4a5a73}
input[type=text]:focus{border-color:#3b3f7a;box-shadow:0 0 0 3px rgba(124,92,255,.13)}
.btn{background:#1a2334;border:1px solid var(--line);color:var(--ink);border-radius:9px;
  padding:8px 13px;font:600 12.5px ui-sans-serif,system-ui;cursor:pointer;white-space:nowrap}
.btn:hover{background:#22304a;border-color:#33415c}
.btn:disabled{opacity:.45;cursor:progress}
.btn.primary{background:linear-gradient(135deg,#7c5cff,#5b3df0);border-color:#5b3df0}
.btn.primary:hover{filter:brightness(1.08)}
.btn.x{padding:3px 9px;font-size:11px;color:#f0a0a0;border-color:rgba(248,113,113,.28);background:transparent}
.btn.x:hover{background:rgba(248,113,113,.12);border-color:rgba(248,113,113,.5)}
.cands{max-height:222px;overflow:auto;margin-top:10px;border-top:1px solid var(--line);
  -webkit-mask-image:linear-gradient(#000 88%,transparent);mask-image:linear-gradient(#000 88%,transparent)}
.cand{display:flex;align-items:center;gap:12px;padding:6px 2px;border-bottom:1px solid rgba(30,42,61,.5)}
.cand .nm{font-weight:600;min-width:170px;font-size:12.5px}
.cand .pt{flex:1;min-width:0;color:var(--dim);font-family:ui-monospace,monospace;font-size:11px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#toast{position:fixed;right:22px;bottom:22px;max-width:560px;background:#101a2b;border:1px solid var(--line);
  border-radius:12px;padding:12px 16px;font-size:12.5px;box-shadow:0 16px 40px rgba(0,0,0,.55);
  display:none;white-space:pre-wrap}
#toast.bad{border-color:rgba(248,113,113,.4)}
a{color:var(--cyn);text-decoration:none}
@media(max-width:1080px){.c,.c.third{grid-column:span 12}}
</style></head><body><div class="wrap">
<header>
  <div class="logo"><b>GOR<span>TEX</span></b><span class="chip mono" id="ver">—</span></div>
  <span class="chip" id="state"><span class="dot" id="dot"></span><span id="stateText">—</span></span>
  <span class="chip">uptime <b id="uptime" class="mono">—</b></span>
  <span class="chip">sessions <b id="sess" class="mono">—</b></span>
  <span class="chip" id="mode">—</span>
  <span class="grow"></span>
  <span class="chip">snapshot <b id="gen" class="mono">—</b></span>
</header>

<div class="grid kpis" id="kpis"></div>

<div class="grid charts">
  <div class="card c"><h3>Graph per repository</h3><div class="chart" id="chIndex"></div></div>
  <div class="card c"><h3>Tokens saved per day</h3><div class="chart" id="chDay"></div></div>
  <div class="card c third"><h3>Savings by tool</h3><div class="chart" id="chTool"></div></div>
  <div class="card c third"><h3>Savings by language</h3><div class="chart" id="chLang"></div></div>
  <div class="card c third"><h3>Tool calls by client</h3><div class="chart" id="chClient"></div></div>
  <div class="card c wide"><h3>Index growth (snapshots)</h3><div class="chart" id="chGrowth" style="height:230px"></div></div>
  <div class="card c wide"><h3>Tracked repositories</h3><table id="tRepos"></table></div>
  <div class="card c wide" id="mg"><h3>Add a repository</h3>
    <div class="row">
      <input id="pathIn" type="text" placeholder="/absolute/path/to/git/repo" spellcheck="false">
      <button class="btn primary" id="trackBtn">Track repo</button>
    </div>
    <div class="dim" id="hint" style="margin-top:9px;font-size:11.5px"></div>
    <div class="cands" id="cands"></div>
  </div>
  <div class="card c wide"><h3>Live MCP sessions</h3><table id="tSess"></table></div>
</div>

<div id="toast"></div>
<footer>
  <span id="foot"></span>
  <span class="mut" id="refreshHint"></span>
</footer>
</div>
<script>
const D = __DATA__;
const AX={axisLine:{lineStyle:{color:'#243044'}},axisTick:{show:false},
  axisLabel:{color:'#8b9bb4',fontSize:11},splitLine:{lineStyle:{color:'rgba(148,163,184,.09)'}}};
const TIP={backgroundColor:'#0b1220',borderColor:'#243044',borderWidth:1,
  textStyle:{color:'#e6edf7',fontSize:12},extraCssText:'border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.5)'};
const LEG={textStyle:{color:'#8b9bb4',fontSize:11},icon:'roundRect',itemWidth:9,itemHeight:9,top:0,right:0};
const fmt=n=>{n=+n||0;const a=Math.abs(n);
  return a>=1e9?(n/1e9).toFixed(2)+'B':a>=1e6?(n/1e6).toFixed(1)+'M':a>=1e3?(n/1e3).toFixed(1)+'k':String(n);};
const num=n=>(+n||0).toLocaleString('en-US');
const bytes=b=>{b=+b||0;const u=['B','KiB','MiB','GiB','TiB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++;}
  return (i?b.toFixed(1):b)+' '+u[i];};
const dur=s=>{s=+s||0;const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`:h?`${h}h ${m}m`:m?`${m}m`:`${s}s`;};
const charts=[];
const mk=(el,opt)=>{const c=echarts.init(document.getElementById(el),null,{renderer:'canvas'});
  c.setOption(opt);charts.push(c);return c;};
addEventListener('resize',()=>charts.forEach(c=>c.resize()));

// ---- header
document.getElementById('ver').textContent=D.daemon.version||'not running';
document.getElementById('stateText').textContent=D.daemon.state||'unknown';
document.getElementById('dot').className='dot'+(D.daemon.state.startsWith('ready')?'':' bad');
document.getElementById('uptime').textContent=dur(D.daemon.uptime_s);
document.getElementById('sess').textContent=D.daemon.sessions;
document.getElementById('gen').textContent=new Date(D.generated*1000).toLocaleString();
const modeEl=document.getElementById('mode');
modeEl.innerHTML=D.served?'<span class="dot"></span>manage mode'
  :'read-only · <span class="mono">--serve</span> to manage';

// ---- KPI cards
const ratio = D.savings.returned ? (D.savings.saved/D.savings.returned) : 0;
const kpis=[
 ['repositories',D.repos.length,`${num(D.totals.files)} files indexed`],
 ['graph nodes',fmt(D.totals.nodes),(D.totals.nodes?(D.totals.edges/D.totals.nodes).toFixed(2):'0')+' edges per node',''],
 ['graph edges',fmt(D.totals.edges),`${num(D.daemon.search_docs)} docs full-text searchable`,''],
 ['tokens saved',fmt(D.savings.saved),`${num(D.savings.calls)} tool calls · ${ratio.toFixed(1)}× vs raw`,'hl'],
 ['tokens returned',fmt(D.savings.returned),'what actually entered context'],
 ['daemon memory',bytes(D.daemon.memory_bytes),`${D.daemon.goroutines} goroutines`],
 ['store on disk',bytes(D.storage.db_bytes),`wal ${bytes(D.storage.wal_bytes)}`],
 ['memories',num(D.savings.memories),`${D.savings.memory_kinds.length} kinds · ${D.savings.promoted.length} promoted tools`],
];
document.getElementById('kpis').innerHTML=kpis.map(k=>`
 <div class="card kpi ${k[3]||''}">
   <div class="lbl">${k[0]}</div>
   <div class="val mono">${k[1]}</div>
   <div class="sub">${k[2]}</div>
 </div>`).join('');

// ---- graph per repo
const rs=[...D.repos].sort((a,b)=>b.nodes-a.nodes);
mk('chIndex',{
  grid:{left:8,right:14,top:38,bottom:6,containLabel:true},
  legend:{...LEG,data:['nodes','edges']},tooltip:{...TIP,trigger:'axis',axisPointer:{type:'shadow'}},
  xAxis:{type:'category',data:rs.map(r=>r.name),...AX,axisLabel:{...AX.axisLabel,rotate:rs.length>6?28:0}},
  yAxis:[{type:'value',...AX,axisLabel:{...AX.axisLabel,formatter:fmt}},
         {type:'value',...AX,axisLabel:{...AX.axisLabel,formatter:fmt},splitLine:{show:false}}],
  series:[
   {name:'nodes',type:'bar',data:rs.map(r=>r.nodes),barMaxWidth:26,
    itemStyle:{borderRadius:[4,4,0,0],color:new echarts.graphic.LinearGradient(0,0,0,1,
      [{offset:0,color:'#8f76ff'},{offset:1,color:'rgba(124,92,255,.28)'}])}},
   {name:'edges',type:'line',yAxisIndex:1,data:rs.map(r=>r.edges),smooth:true,symbolSize:6,
    lineStyle:{color:'#22d3ee',width:2},itemStyle:{color:'#22d3ee'}}]
});

// ---- tokens saved per day
const days=D.savings.by_day;
mk('chDay',{
  grid:{left:8,right:14,top:38,bottom:6,containLabel:true},
  legend:{...LEG,data:['saved','calls']},tooltip:{...TIP,trigger:'axis'},
  xAxis:{type:'category',data:days.map(d=>d.day.slice(5)),...AX,boundaryGap:false},
  yAxis:[{type:'value',...AX,axisLabel:{...AX.axisLabel,formatter:fmt}},
         {type:'value',...AX,axisLabel:{...AX.axisLabel,formatter:fmt},splitLine:{show:false}}],
  series:[
   {name:'saved',type:'line',data:days.map(d=>d.saved),smooth:true,symbol:'none',
    lineStyle:{color:'#7c5cff',width:2},
    areaStyle:{color:new echarts.graphic.LinearGradient(0,0,0,1,
      [{offset:0,color:'rgba(124,92,255,.45)'},{offset:1,color:'rgba(124,92,255,0)'}])}},
   {name:'calls',type:'bar',yAxisIndex:1,data:days.map(d=>d.calls),barMaxWidth:12,
    itemStyle:{color:'rgba(34,211,238,.55)',borderRadius:[3,3,0,0]}}]
});

// ---- savings by tool
const tools=D.savings.by_tool.slice(0,7).reverse();
mk('chTool',{
  grid:{left:8,right:26,top:8,bottom:6,containLabel:true},tooltip:{...TIP,trigger:'axis',
    axisPointer:{type:'shadow'},formatter:p=>`${p[0].name}<br><b>${num(p[0].value)}</b> tokens saved`},
  xAxis:{type:'value',...AX,axisLabel:{...AX.axisLabel,formatter:fmt}},
  yAxis:{type:'category',data:tools.map(t=>t.tool),...AX,splitLine:{show:false}},
  series:[{type:'bar',data:tools.map(t=>t.saved),barMaxWidth:16,
    label:{show:true,position:'right',color:'#8b9bb4',fontSize:10,formatter:p=>fmt(p.value)},
    itemStyle:{borderRadius:[0,4,4,0],color:new echarts.graphic.LinearGradient(0,0,1,0,
      [{offset:0,color:'rgba(124,92,255,.35)'},{offset:1,color:'#22d3ee'}])}}]
});

function donut(el,rows,nameKey,valKey,colors){
  const top=rows.filter(r=>r[valKey]>0).slice(0,6);
  return mk(el,{
    tooltip:{...TIP,trigger:'item',formatter:p=>`${p.name}<br><b>${num(p.value)}</b> (${p.percent}%)`},
    legend:{...LEG,orient:'horizontal',bottom:0,top:'auto',left:0},
    series:[{type:'pie',radius:['48%','72%'],center:['50%','44%'],avoidLabelOverlap:true,
      itemStyle:{borderColor:'#0f1521',borderWidth:2},label:{show:false},
      emphasis:{label:{show:true,color:'#e6edf7',fontSize:12,fontWeight:600,formatter:'{b}'}},
      color:colors,
      data:top.map(r=>({name:r[nameKey],value:r[valKey]}))}]
  });
}
donut('chLang',D.savings.by_lang,'language','saved',
  ['#7c5cff','#22d3ee','#34d399','#fbbf24','#f472b6','#60a5fa']);
donut('chClient',D.savings.by_client,'client','calls',
  ['#22d3ee','#7c5cff','#34d399','#fbbf24','#f472b6','#60a5fa']);

// ---- index growth
const H=D.history;
mk('chGrowth',{
  grid:{left:8,right:14,top:34,bottom:6,containLabel:true},
  legend:{...LEG,data:['nodes','edges']},tooltip:{...TIP,trigger:'axis'},
  title:H.length<2?{text:'only one snapshot so far — every run adds a point',
    left:'center',top:'middle',textStyle:{color:'#5b6b84',fontSize:12,fontWeight:400}}:undefined,
  xAxis:{type:'category',data:H.map(h=>new Date(h.ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})),
    ...AX,boundaryGap:false},
  yAxis:[{type:'value',...AX,axisLabel:{...AX.axisLabel,formatter:fmt},min:'dataMin'},
         {type:'value',...AX,axisLabel:{...AX.axisLabel,formatter:fmt},splitLine:{show:false}}],
  series:[
   {name:'nodes',type:'line',data:H.map(h=>h.nodes),smooth:true,symbolSize:5,
    lineStyle:{color:'#8f76ff',width:2},itemStyle:{color:'#8f76ff'}},
   {name:'edges',type:'line',yAxisIndex:1,data:H.map(h=>h.edges),smooth:true,symbolSize:5,
    lineStyle:{color:'#22d3ee',width:2},itemStyle:{color:'#22d3ee'}}]
});

// ---- tables
const repoRows=[...D.repos].sort((a,b)=>b.bytes-a.bytes);
const savedByRepo=Object.fromEntries(D.savings.by_repo.map(r=>[r.repo,r.saved]));
document.getElementById('tRepos').innerHTML=`
 <thead><tr><th>repository</th><th>branch</th><th>freshness</th><th class="r">files</th>
  <th class="r">nodes</th><th class="r">edges</th><th class="r">tokens saved</th>
  <th class="r">disk</th><th class="r">last indexed</th>${D.served?'<th class="r"></th>':''}</tr></thead><tbody>`+
 repoRows.map(r=>{
  const f=!r.indexed?`<span class="tag mut">config only</span>`
   :r.stale?`<span class="tag warn">stale</span>`:`<span class="tag ok">fresh</span>`;
  return `<tr><td><b>${r.name}</b><div class="dim mono" style="font-size:11px">${r.path}</div></td>
   <td class="mono mut">${r.branch||'—'}</td><td>${f}</td>
   <td class="r mono">${num(r.files)}</td><td class="r mono">${num(r.nodes)}</td>
   <td class="r mono">${num(r.edges)}</td>
   <td class="r mono" style="color:#a78bfa">${savedByRepo[r.name]?fmt(savedByRepo[r.name]):'<span class="dim">—</span>'}</td>
   <td class="r mono">${bytes(r.bytes)}</td>
   <td class="r mono dim">${(r.last_indexed||'').replace('T',' ').slice(0,16)||'—'}</td>
   ${D.served?`<td class="r"><button class="btn x" data-untrack="${r.path}">remove</button></td>`:''}</tr>`;}).join('')+
 '</tbody>';

document.getElementById('tSess').innerHTML=D.sessions.length?`
 <thead><tr><th>client</th><th>version</th><th class="r">connected</th><th>working dir</th><th>session</th></tr></thead><tbody>`+
 D.sessions.map(s=>`<tr><td><b>${s.client||'—'}</b></td><td class="mono mut">${s.version||'—'}</td>
   <td class="r mono">${s.connected||'—'}</td><td class="mono dim" style="font-size:11px">${s.cwd||'—'}</td>
   <td class="mono dim" style="font-size:11px">${s.id.slice(0,12)}</td></tr>`).join('')+'</tbody>'
 :'<tbody><tr><td class="dim">no live sessions</td></tr></tbody>';

// ---- repository management (only in --serve mode)
const toastEl=document.getElementById('toast');
function toast(msg,keep){toastEl.textContent=msg;toastEl.style.display='block';
  toastEl.className=keep?'bad':'';
  if(!keep) setTimeout(()=>{toastEl.style.display='none';},3000);}
async function post(url,body){
  const r=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify(body)});
  return r.json();
}
async function track(path,btn){
  if(!path) return toast('enter an absolute path to a git repository',true);
  btn&&(btn.disabled=true);
  toast('tracking '+path+'\nindexing starts now — this can take a while…');
  const r=await post('/api/track',{path});
  toast((r.ok?'✓ ':'✗ ')+(r.message||''),!r.ok);
  if(r.ok) setTimeout(()=>location.reload(),700); else btn&&(btn.disabled=false);
}
async function untrack(path,btn){
  if(!confirm('Stop using '+path+' in gortex?\n\nIts index and config entry are removed. Files on disk are untouched.')) return;
  btn&&(btn.disabled=true);
  toast('removing '+path+'…');
  const r=await post('/api/untrack',{path});
  toast((r.ok?'✓ ':'✗ ')+(r.message||''),!r.ok);
  if(r.ok) setTimeout(()=>location.reload(),700); else btn&&(btn.disabled=false);
}
if(D.served){
  const cs=(D.candidates||[]);
  document.getElementById('hint').innerHTML=cs.length
    ? `local git repos not tracked yet: ${cs.length} &middot; click to add, or paste any absolute path above`
    : 'every git repo under '+(D.roots||'~/go/src')+' is already tracked';
  document.getElementById('cands').innerHTML=cs.slice(0,80).map(c=>
    `<div class="cand"><span class="nm">${c.name}</span><span class="pt">${c.path}</span>`+
    `<button class="btn" data-track="${c.path}">+ track</button></div>`).join('');
}else{
  document.getElementById('hint').innerHTML=
    'Read-only page. Start it with <span class="mono">gortex-dashboard --serve</span> to add or remove repositories here.';
  document.getElementById('pathIn').disabled=true;
  document.getElementById('trackBtn').disabled=true;
}
document.addEventListener('click',e=>{
  const t=e.target.dataset||{};
  if(t.track) track(t.track,e.target);
  if(t.untrack) untrack(t.untrack,e.target);
});
document.getElementById('trackBtn').addEventListener('click',e=>
  track(document.getElementById('pathIn').value.trim(),e.target));

document.getElementById('refreshHint').innerHTML=D.served
  ? 'live data · reload to refresh'
  : 'refresh: <span class="mono">gortex-dashboard --open</span>';
const first=D.savings.first_ts?new Date(D.savings.first_ts/1e6).toLocaleDateString():'—';
const last=D.savings.last_ts?new Date(D.savings.last_ts/1e6).toLocaleDateString():'—';
document.getElementById('foot').innerHTML=
 `savings window <b class="mono">${first} → ${last}</b> · search index ${num(D.daemon.search_docs)} docs · `+
 `${D.storage.reclaimed_bytes?'wal reclaimed '+bytes(D.storage.reclaimed_bytes)+' · ':''}`+
 `pid ${D.daemon.pid||'—'} · generated ${new Date(D.generated*1000).toLocaleTimeString()}`;
</script></body></html>
"""


def render(data, out_path):
    have_local = os.path.exists(ECHARTS)
    if not have_local:
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            with urlopen(ECHARTS_URL, timeout=20) as r, open(ECHARTS, "wb") as f:
                f.write(r.read())
            have_local = True
        except Exception:
            pass
    src = "echarts.min.js" if have_local else ECHARTS_URL
    html = (TEMPLATE.replace("__ECHARTS__", src)
            .replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    with open(out_path, "w") as f:
        f.write(html)
    return out_path


# ------------------------------------------------------------------------ main


def build(snapshot=True):
    daemon, repos, sessions, storage = collect_status()
    info = collect_repos_json()
    for r in repos:
        j = info.get(r["name"], {})
        r["branch"] = j.get("branch", "")
        r["stale"] = bool(j.get("stale"))
        r["last_indexed"] = j.get("last_indexed", "")
        r["head"] = (j.get("head_commit") or "")[:12]
    savings = collect_savings()
    history = store_snapshot(daemon, repos, savings) if snapshot else []
    return {
        "generated": int(time.time()),
        "served": False,
        "daemon": daemon,
        "repos": repos,
        "sessions": sessions,
        "storage": storage,
        "savings": savings,
        "history": history,
        "totals": {
            "files": sum(r["files"] for r in repos),
            "nodes": sum(r["nodes"] for r in repos),
            "edges": sum(r["edges"] for r in repos),
            "bytes": sum(r["bytes"] for r in repos),
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Gortex dashboard web view")
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    ap.add_argument("--serve", action="store_true",
                    help="serve on 127.0.0.1 and enable add/remove repository buttons")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--no-open", action="store_true", help="with --serve: do not open a browser")
    ap.add_argument("--no-snapshot", action="store_true", help="render without recording a snapshot")
    ap.add_argument("--print", action="store_true", help="print the collected JSON")
    ap.add_argument("--out", default=HTML_PATH)
    a = ap.parse_args()

    if a.serve:
        return serve(a.port, open_browser=not a.no_open)
    data = build(snapshot=not a.no_snapshot)
    if a.print:
        print(json.dumps(data, indent=2))
        return
    path = render(data, a.out)
    print(path)
    if a.open:
        webbrowser.open("file://" + path)


if __name__ == "__main__":
    sys.exit(main())
