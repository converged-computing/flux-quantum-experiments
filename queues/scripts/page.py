#!/usr/bin/env python3
"""One page with the four days in it: tabs, hover everywhere, plain words.

    ./page.py results/queues.jsonl --out plots/index.html

Everything is embedded, one point every five minutes per machine for the
long views and every minute for Forte-1, so the page needs no server and
loads nothing from the network. Each tab says in a few sentences what the
view shows and what it means, then draws it.
"""

import argparse
import datetime
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figures import ORDER, STATE, VENDOR, UNIT, devices_in_order, percentiles, short, state  # noqa: E402
from metrics import by_device, changes, hour_of, load, measure, quantiles, spearman  # noqa: E402

STATES = list(STATE)
FORTE_B = "arn:aws:braket:us-east-1::device/qpu/ionq/Forte-1"
FORTE_I = "qpu.forte-1"


def val(r):
    v = measure(r)
    if v is not None and r["vendor"] == "ionq":
        v = v / 3600.0
    return v


def series(rs, t0, every):
    pts, last = [], -1e9
    for r in rs:
        if r["t"] - last < every - 1:
            continue
        last = r["t"]
        v = val(r)
        pts.append([round((r["t"] - t0) / 3600.0, 3), None if v is None else round(v, 2), STATES.index(state(r))])
    pct = percentiles([p[1] for p in pts])
    return [[p[0], p[1], p[2], None if q is None else round(q, 1)] for p, q in zip(pts, pct)]


def runs_of(rs, t0):
    runs, start, cur = [], rs[0]["t"], state(rs[0])
    for a, b in zip(rs, rs[1:]):
        s = state(b)
        if s != cur:
            runs.append([round((start - t0) / 3600.0, 3), round((b["t"] - t0) / 3600.0, 3), STATES.index(cur)])
            start, cur = b["t"], s
    runs.append([round((start - t0) / 3600.0, 3), round((rs[-1]["t"] - t0) / 3600.0, 3), STATES.index(cur)])
    return runs


CHANGE_KINDS = ["timestamps", "pointer", "few numbers", "many numbers", "calibration"]
CHANGE_COLORS = ["#C8CDD2", "#56B4E9", "#009E73", "#0072B2", "#D55E00"]
CHANGE_LABELS = ["only dates changed", "new calibration file reference", "a few numbers changed", "many numbers changed", "full calibration"]


def load_changes(path):
    """The sorted record changes from changes.py, by device, or None."""
    if not path or not os.path.exists(path):
        return None
    out = defaultdict(list)
    with open(path) as f:
        for c in json.load(f):
            out[(c["vendor"], c["device"])].append(c)
    return out


def pack(rows, every=300, record_changes=None):
    dev = by_device(rows)
    keys = devices_in_order(dev)
    t0 = min(r["t"] for rs in dev.values() for r in rs)
    t1 = max(r["t"] for rs in dev.values() for r in rs)
    machines = []
    for v, d in keys:
        rs = dev[(v, d)]
        xs = [val(r) for r in rs]
        q = quantiles(xs)
        up_xs = [val(r) for r in rs if state(r) == "taking work" and val(r) is not None]
        dn_xs = [val(r) for r in rs if state(r) != "taking work" and val(r) is not None]
        qu = quantiles(up_xs) if up_xs else {}
        by_h = defaultdict(list)
        for r in rs:
            by_h[hour_of(r["t"])[0]].append(r)
        hourly = []
        for h in range(24):
            hs = by_h.get(h, [])
            hourly.append({
                "closed": round(sum(1 for r in hs if state(r) != "taking work") / len(hs), 3) if hs else None,
                "median": round(quantiles([val(r) for r in hs]).get("0.5", 0) or 0, 2) if hs else None,
            })
        c = changes(rs) or {}
        fall = c.get("steepest_fall") or {}
        machines.append({
            "vendor": v, "name": short(d), "device": d, "unit": UNIT[v], "color": VENDOR[v],
            "points": series(rs, t0, every),
            "runs": runs_of(rs, t0),
            "stats": {"p10": qu.get("0.1", q.get("0.1")), "p50": qu.get("0.5", q.get("0.5")), "p90": qu.get("0.9", q.get("0.9")), "max": q.get("max"), "mean": q.get("mean"),
                      "empty": round(sum(1 for x in xs if x == 0) / len(xs), 3) if xs else None,
                      "empty_open": round(sum(1 for x in up_xs if x == 0) / len(up_xs), 3) if up_xs else None,
                      "empty_closed": round(sum(1 for x in dn_xs if x == 0) / len(dn_xs), 3) if dn_xs else None,
                      "n_open": len(up_xs), "n_closed": len(dn_xs),
                      "mean_open": round(sum(up_xs) / len(up_xs), 2) if up_xs else None,
                      "mean_closed": round(sum(dn_xs) / len(dn_xs), 2) if dn_xs else None,
                      "up": round(sum(1 for r in rs if state(r) == "taking work") / len(rs), 3),
                      "drain_per_min": round(fall.get("per_tick", 0) * 60.0 / 60.0, 2) if fall else None,
                      "drain_from": fall.get("from"), "drain_to": fall.get("to"),
                      "drain_at": round((fall["t"] - t0) / 3600.0, 2) if fall else None,
                      "rising": c.get("rising"), "falling": c.get("falling"), "flat": c.get("flat")},
            "hourly": hourly,
            "calibration": [round((r["t"] - t0) / 3600.0, 3) for r in rs if "properties" in r][1:],
            "changes": [[round((c["t"] - t0) / 3600.0, 3), CHANGE_KINDS.index(c["kind"]), c["moved"], c["share"]]
                        for c in sorted((record_changes or {}).get((v, d), []), key=lambda c: c["t"])],
            "versions": len([r for r in rs if "properties" in r]),
        })
    # Forte-1 at full resolution, both services, aligned by tick
    fb = {r["tick"]: r for r in dev.get(("braket", FORTE_B), [])}
    fi = {r["tick"]: r for r in dev.get(("ionq", FORTE_I), [])}
    ticks = sorted(set(fb) & set(fi))
    forte = [[round((fb[k]["t"] - t0) / 3600.0, 3), measure(fb[k]), None if measure(fi[k]) is None else round(measure(fi[k]) / 3600.0, 2),
              STATES.index(state(fb[k])), STATES.index(state(fi[k]))] for k in ticks]
    bx = [f[1] for f in forte if f[1] is not None and f[2] is not None]
    iy = [f[2] for f in forte if f[1] is not None and f[2] is not None]
    pb, pi = percentiles([f[1] for f in forte]), percentiles([f[2] for f in forte])
    forte = [f + [None if a is None else round(a, 1), None if b is None else round(b, 1)] for f, a, b in zip(forte, pb, pi)]
    start = datetime.datetime.fromtimestamp(t0, datetime.timezone.utc)
    return {
        "start_iso": start.isoformat(), "hours": round((t1 - t0) / 3600.0, 2), "n_rows": len(rows),
        "states": STATES, "state_colors": [STATE[s] for s in STATES], "machines": machines,
        "change_kinds": CHANGE_LABELS, "change_colors": CHANGE_COLORS, "have_changes": record_changes is not None,
        "forte": forte, "forte_rho": None if spearman(bx, iy) is None else round(spearman(bx, iy), 2),
        "forte_both_up": round(sum(1 for f in forte if f[3] == 0 and f[4] == 0) / max(1, sum(1 for f in forte if f[3] == 0 or f[4] == 0)), 3),
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Queues, 14 to 18 October 2026</title>
<style>
:root{--ink:#1f2328;--muted:#656d76;--line:#e6e8eb;--faint:#f4f6f8;--bg:#f7f8fa;--card:#fff;--accent:#0072B2;--braket:#D55E00;--ionq:#0072B2;--ibm:#009E73;--good:#009E73;--closed:#56B4E9;--maint:#E69F00;--down:#D55E00}
*{box-sizing:border-box} html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif}
.app{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
aside{background:#14181d;color:#cbd2d9;padding:26px 18px;position:sticky;top:0;height:100vh;overflow:auto}
aside .brand{color:#fff;font-weight:700;font-size:15px;letter-spacing:-.01em;margin:0 0 2px} aside .when{font-size:12px;color:#8b949e;margin:0 0 22px;line-height:1.4}
aside nav{display:flex;flex-direction:column;gap:2px} aside button{all:unset;cursor:pointer;display:block;padding:8px 12px;border-radius:8px;font-size:13.5px;color:#cbd2d9;border-left:3px solid transparent}
aside button:hover{background:#1f252c;color:#fff} aside button.on{background:#232a32;color:#fff;border-left-color:var(--accent);font-weight:600}
aside .legend{margin-top:26px;font-size:11.5px;color:#8b949e;line-height:1.8} aside .legend .sw{margin-right:7px}
main{padding:0 0 64px;min-width:0}
.band{background:linear-gradient(135deg,#ffffff 0%,#eef3f8 100%);border-bottom:1px solid var(--line);padding:30px 36px 24px}
.band h1{margin:0 0 6px;font-size:26px;font-weight:700;letter-spacing:-.02em} .band p{margin:0;color:var(--muted);max-width:1280px;font-size:15px}
.content{padding:26px 36px 0} section{display:none;animation:fade .25s ease} section.on{display:block} @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:0 0 18px;position:relative;box-shadow:0 1px 2px rgba(16,24,40,.04);transition:box-shadow .2s,transform .2s}
.card:hover{box-shadow:0 6px 18px rgba(16,24,40,.08)} .card h2{margin:0 0 6px;font-size:17px;font-weight:650;letter-spacing:-.01em} .card .text{color:#3b434c;margin:0 0 10px;max-width:860px;font-size:14px}
section>.text{margin:0 0 14px;font-size:15px;line-height:1.6}
.tilekey{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:0 0 22px;font-size:12.5px} .tilekey .kh{font-weight:600;color:var(--muted);text-transform:uppercase;font-size:10.5px;letter-spacing:.05em;margin-left:6px} .tilekey .kh:first-child{margin-left:0}
.method{display:flex;gap:10px;align-items:flex-start;font-size:12.5px;color:var(--muted);background:var(--faint);border-radius:8px;padding:8px 12px;margin:0 0 14px;max-width:860px}
.method b{color:var(--ink);font-weight:600;white-space:nowrap;font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding-top:2px}
.take{margin:12px 0 0;padding:10px 14px;border-left:3px solid var(--accent);background:#f3f8fc;border-radius:0 8px 8px 0;max-width:860px;font-size:13.5px} .take b{color:var(--accent);font-weight:600}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px} .row>.card{margin:0}
.controls{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:12.5px;margin:2px 0 12px}
.controls label{cursor:pointer;white-space:nowrap;display:inline-flex;align-items:center;gap:5px} .controls .vendor{font-weight:600;margin-left:6px;color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.05em}
.chip{border:1px solid var(--line);border-radius:999px;padding:4px 12px;background:#fff;cursor:pointer;font-size:12.5px;transition:all .15s} .chip:hover{border-color:#9ca3af} .chip.on{background:var(--ink);color:#fff;border-color:var(--ink)}
select{font:inherit;font-size:12.5px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:#fff}
svg{display:block;width:100%;height:auto} .panel{position:relative}
.tip{position:absolute;pointer-events:none;background:#1f2328;color:#fff;padding:8px 11px;font-size:12px;line-height:1.45;border-radius:7px;box-shadow:0 4px 14px rgba(0,0,0,.18);display:none;white-space:nowrap;z-index:5} .tip b{color:#fff}
.sw{display:inline-block;width:10px;height:10px;margin-right:6px;vertical-align:middle;border-radius:3px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:0 0 18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px} .stat .n{font-size:26px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums} .stat .l{color:var(--muted);font-size:12px;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px 10px;cursor:pointer;transition:box-shadow .2s,transform .2s,border-color .2s;position:relative}
.tile:hover{box-shadow:0 8px 22px rgba(16,24,40,.1);transform:translateY(-2px);border-color:#c9d1d9}
.tile .name{font-weight:650;font-size:13.5px;display:flex;justify-content:space-between;align-items:baseline} .tile .vend{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#fff;border-radius:4px;padding:1px 6px}
.tile .nums{display:flex;gap:14px;font-size:11.5px;color:var(--muted);margin-top:6px} .tile .nums b{color:var(--ink);font-weight:650;font-variant-numeric:tabular-nums}
.findings{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:18px} .finding{border-top:3px solid var(--accent);background:var(--card);border-radius:0 0 12px 12px;border:1px solid var(--line);border-top:3px solid var(--accent);padding:14px 16px}
.finding h3{margin:0 0 6px;font-size:14.5px} .finding p{margin:0;color:#3b434c;font-size:13px} .finding a{color:var(--accent);text-decoration:none;font-size:12.5px;display:inline-block;margin-top:8px;cursor:pointer} .finding a:hover{text-decoration:underline}
table.tbl{border-collapse:collapse;font-size:12.5px;width:100%} .tbl th,.tbl td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums} .tbl th:first-child,.tbl td:first-child{text-align:left} .tbl th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em} .tbl tr:hover td{background:var(--faint)}
.legend{font-size:12px;margin:2px 0 10px} .legend span{margin-right:14px;white-space:nowrap} .note{color:var(--muted);font-size:12.5px;margin:10px 0 0}
@media (max-width:860px){.app{grid-template-columns:1fr} aside{position:static;height:auto} .band,.content{padding-left:18px;padding-right:18px}}
</style></head><body><div class="app">
<aside><p class="brand">Quantum queues</p><p class="when">Braket, IonQ and IBM<br>14 to 18 October 2026<br>one reading a minute</p><nav id="nav"></nav>
<div class="legend"><div><span class="sw" style="background:var(--good)"></span>taking work</div><div><span class="sw" style="background:var(--closed)"></span>online, outside its window</div><div><span class="sw" style="background:var(--maint)"></span>maintenance</div><div><span class="sw" style="background:var(--down)"></span>offline or unavailable</div><div style="margin-top:10px"><span class="sw" style="background:var(--braket)"></span>Braket &nbsp;<span class="sw" style="background:var(--ionq)"></span>IonQ &nbsp;<span class="sw" style="background:var(--ibm)"></span>IBM</div></div></aside>
<main><div class="band"><h1 id="title">What do quantum vendor queues actually look like? 🤔</h1><p id="subtitle"></p></div><div class="content" id="main"></div></main></div>
<script>
const D = __DATA__;
const DAY = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const start = new Date(D.start_iso);
const W = 1080, L = 64, R = 16;
function when(h){ const d = new Date(start.getTime() + h*3600e3); return DAY[d.getUTCDay()]+" "+d.getUTCDate()+" Oct "+String(d.getUTCHours()).padStart(2,"0")+":"+String(d.getUTCMinutes()).padStart(2,"0"); }
function midnights(){ const out=[]; const d=new Date(start.getTime()); d.setUTCHours(0,0,0,0); d.setUTCDate(d.getUTCDate()+1); while((d-start)/3600e3 <= D.hours){ out.push([(d-start)/3600e3, DAY[d.getUTCDay()]]); d.setUTCDate(d.getUTCDate()+1);} return out; }
const MID = midnights();
const DAYS = (()=>{ const out=[]; let h0=0; for (const [h] of MID){ out.push({from:h0,to:h}); h0=h; } out.push({from:h0,to:D.hours}); return out.map((d,i)=>({...d, name: DAY[(start.getUTCDay()+i)%7], date: new Date(start.getTime()+(d.from+.01)*3600e3).getUTCDate()})); })();
function fmt(v, d){ if (v==null) return "-"; if (d!=null) return v.toFixed(d); return Math.abs(v)>=100 ? v.toFixed(0) : (Number.isInteger(v) ? String(v) : v.toFixed(1)); }
function pct(v){ return v==null ? "-" : Math.round(v*100)+"%"; }
function el(tag, attrs, parent){ const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
function txt(parent, x, y, s, attrs){ const t = el("text", Object.assign({x:x,y:y,"font-size":10,fill:"#1f2328","font-family":"inherit"}, attrs||{}), parent); t.textContent = s; return t; }
function nice(hi, n){ n=n||4; if (hi<=0) hi=1; const raw = hi/n, mag = Math.pow(10, Math.floor(Math.log10(raw))); let step = mag; for (const m of [1,2,2.5,5,10]) { step = m*mag; if (hi/step <= n+.5) break; } const t=[]; for (let v=0; v<=hi*1.0001; v+=step) t.push(+v.toFixed(6)); return t; }
function div(cls, parent, html){ const d=document.createElement("div"); if (cls) d.className=cls; if (html!=null) d.innerHTML=html; if (parent) parent.appendChild(d); return d; }
function tipFor(container){ const t=div("tip", container); return { show(ev, html){ const r=container.getBoundingClientRect(); t.innerHTML=html; t.style.display="block"; let x=ev.clientX-r.left+14, y=ev.clientY-r.top+12; if (x+t.offsetWidth>r.width) x=Math.max(0, ev.clientX-r.left-t.offsetWidth-14); t.style.left=x+"px"; t.style.top=y+"px"; }, hide(){ t.style.display="none"; } }; }
function hoverTips(wrap, svg){ const tip=tipFor(wrap); svg.addEventListener("mousemove",ev=>{ const t=ev.target.dataset&&ev.target.dataset.tip; t?tip.show(ev,t):tip.hide(); }); svg.addEventListener("mouseleave",()=>tip.hide()); }
function dayAxis(svg, x, y, w, h, from, to, hourly){
  if (hourly){ const span=to-from; const step = span<=6?1:(span<=24?3:6); for (let hh=Math.ceil(from); hh<=to; hh+=step){ const X=x+(hh-from)/span*w; el("line",{x1:X,y1:y,x2:X,y2:y+h,stroke:"#eceff2"},svg); txt(svg,X,y+h+13,when(hh).slice(-5),{"text-anchor":"middle","font-size":9}); } }
  else for (const [h0,name] of MID){ if (h0<from||h0>to) continue; const X = x + (h0-from)/(to-from)*w; el("line",{x1:X,y1:y,x2:X,y2:y+h,stroke:"#1f2328","stroke-width":.6,"stroke-dasharray":"2,3"},svg); txt(svg,X+3,y+h+13,name+" 00:00",{"font-size":9}); }
  el("line",{x1:x,y1:y+h,x2:x+w,y2:y+h,stroke:"#9ca3af"},svg);
}
function yAxis(svg, x, y, w, h, ymax, label, yfmt, n){ for (const t of nice(ymax, n)) { const Y = y + h - t/ymax*h; el("line",{x1:x,y1:Y,x2:x+w,y2:Y,stroke:"#eef0f2"},svg); txt(svg,x-6,Y+3,yfmt?yfmt(t):fmt(t),{"text-anchor":"end","font-size":9}); } el("line",{x1:x,y1:y,x2:x,y2:y+h,stroke:"#9ca3af"},svg); if (label) txt(svg, x-46, y+h/2, label, {"text-anchor":"middle","font-size":9,fill:"#656d76",transform:`rotate(-90 ${x-46} ${y+h/2})`}); }
function stepPath(svg, pts, xi, yi, x, y, w, h, from, to, ymax, color, width){ let d="", last=null; for (const p of pts){ if (p[xi]<from||p[xi]>to) continue; const v=p[yi]; if (v==null){ last=null; continue; } const X=x+(p[xi]-from)/(to-from)*w, Y=y+h-Math.min(v,ymax)/ymax*h; d += last==null ? `M${X.toFixed(1)},${Y.toFixed(1)}` : `L${X.toFixed(1)},${last.toFixed(1)}L${X.toFixed(1)},${Y.toFixed(1)}`; last=Y; } return el("path",{d:d,fill:"none",stroke:color,"stroke-width":width||1.5,"stroke-linejoin":"round"},svg); }
function shadeRuns(svg, runs, x, y, w, h, from, to, fill){ for (const [a,b,s] of runs){ if (s===0||b<from||a>to) continue; const A=Math.max(a,from), B=Math.min(b,to); el("rect",{x:x+(A-from)/(to-from)*w,y:y,width:Math.max((B-A)/(to-from)*w,.5),height:h,fill:fill||"#f1f3f5"},svg); } }
function at(pts, hr, xi){ let best=null; for (const p of pts){ if (p[xi]<=hr) best=p; else break; } return best; }
function cursor(container, svg, x, y, w, h, from, to, reader){
  const line = el("line",{x1:0,y1:y,x2:0,y2:y+h,stroke:"#1f2328","stroke-width":.8,visibility:"hidden"},svg); const tip = tipFor(container);
  svg.addEventListener("mousemove", ev=>{ const r=svg.getBoundingClientRect(); const px=(ev.clientX-r.left)*(W/r.width); if (px<x||px>x+w){ tip.hide(); line.setAttribute("visibility","hidden"); return; } const hr=from+(px-x)/w*(to-from); line.setAttribute("x1",px); line.setAttribute("x2",px); line.setAttribute("visibility","visible"); tip.show(ev, "<b>"+when(hr)+"</b><br>"+reader(hr)); });
  svg.addEventListener("mouseleave", ()=>{ tip.hide(); line.setAttribute("visibility","hidden"); });
}
function card(parent, title, text, method, take){ const c=div("card",parent); if (title) c.innerHTML+=`<h2>${title}</h2>`; if (text) div("text",c,text); if (method) div("method",c,`<b>How we measured it</b><span>${method}</span>`); const body=div("",c); if (take) div("take",c,`<b>Why it matters</b> &nbsp;${take}`); return body; }
function sw(color){ return `<span class="sw" style="background:${color}"></span>`; }
const M = D.machines;
const forteB = M.find(m=>m.vendor==="braket"&&m.name==="Forte-1"), forteI = M.find(m=>m.vendor==="ionq"&&m.name==="qpu.forte-1");
const VC = {braket:"#D55E00",ionq:"#0072B2",ibm:"#009E73"};
document.getElementById("subtitle").textContent = `I was interested to know what queues actually looked like across IBM Quantum, Braket (AWS), and IonQ. The last two are particularly interesting because they provide interfaces to the same machines. Will they show the same status? To not incur charges, we do not submit work, but rather look at metrics that indicate queue depth. In total we read the queues of twelve quantum machines once a minute for four days. Yes - a lot of reads! We never submitted anything. There were ${D.n_rows.toLocaleString()} readings total, and none failed. Times are in UTC. Hover over anything for details.`;

const TABS = [["overview","Overview"],["calendar","Calendar"],["queues","Queues"],["forte","Forte-1, Braket vs IonQ"],["day","One day"],["hours","By hour"],["spread","Spread and drain"],["calibration","Calibration"],["data","Data"]];
const nav=document.getElementById("nav"), main=document.getElementById("main"); const sections={};
for (const [id,name] of TABS){ const b=document.createElement("button"); b.textContent=name; b.onclick=()=>show(id); b.dataset.id=id; nav.appendChild(b); const s=document.createElement("section"); s.id="s-"+id; main.appendChild(s); sections[id]=s; }
const built={}; let dayMachine = M.indexOf(forteB), dayIndex = DAYS.length-1; let redrawDay = null;
function show(id){ for (const b of nav.children) b.classList.toggle("on", b.dataset.id===id); for (const k in sections) sections[k].classList.toggle("on", k===id); if (!built[id]){ built[id]=true; BUILD[id](sections[id]); } else if (id==="day" && redrawDay) redrawDay(); location.hash=id; window.scrollTo(0,0); }
function openDay(i, d){ dayMachine=i; if (d!=null) dayIndex=d; show("day"); }

function sparkTile(parent, m, i){
  const t=div("tile",parent); t.innerHTML=`<div class="name"><span>${m.name}</span><span class="vend" style="background:${m.color}">${m.vendor}</span></div>`;
  const wrap=div("panel",t); const SW=300, SH=64, SB=8; const svg=el("svg",{viewBox:`0 0 ${SW} ${SH+SB+4}`},wrap);
  const ymax=Math.max(1,...m.points.map(p=>p[1]||0))*1.05;
  shadeRuns(svg,m.runs,0,0,SW,SH,0,D.hours,"#f1f3f5"); stepPath(svg,m.points,0,1,0,0,SW,SH,0,D.hours,ymax,m.color,1.3);
  for (const [a,b,s] of m.runs) el("rect",{x:a/D.hours*SW,y:SH+3,width:Math.max((b-a)/D.hours*SW,.5),height:SB,fill:D.state_colors[s]},svg);
  for (const [h0] of MID) el("line",{x1:h0/D.hours*SW,y1:0,x2:h0/D.hours*SW,y2:SH,stroke:"#1f2328","stroke-width":.5,"stroke-dasharray":"2,3"},svg);
  const st=m.stats; const worst = m.vendor==="ionq" ? `peak <b>${fmt(st.max)} h</b>` : `worst <b>${fmt(st.max)}</b>`;
  div("nums",t,`<span>taking work <b>${pct(st.up)}</b></span><span>${m.vendor==="ionq"?"":"queue empty <b>"+pct(st.empty_open)+"</b> of that time"}</span><span>${worst}</span>`);
  cursor(wrap,svg,0,0,SW,SH,0,D.hours,hr=>{ const p=at(m.points,hr,0); return p?`${sw(m.color)}<b>${fmt(p[1])}</b> ${m.unit}<br>${D.states[p[2]]}`:"-"; });
  t.onclick=()=>openDay(i);
}

const BUILD = {
overview(s){
  div("text",s,"We queried Braket, IonQ and IBM APIs to get metadata about their machines once a minute from Wednesday night to Sunday night. We never submitted a task, so we never changed the queues we were watching. We say a machine is taking work when it is online and serving tasks or jobs. Each box below is one machine. The line is its queue over the four days. The strip under the line shows when the machine was taking work. Click a box to see that machine one day at a time.").style.maxWidth="900px";
  { const k=div("tilekey",s); const seen=new Set(M.flatMap(m=>m.runs.map(r=>r[2])));
    k.innerHTML = `<span class="kh">Line color</span>` + ["braket","ionq","ibm"].map(v=>`<span>${sw(VC[v])}${v==="braket"?"Braket":v==="ionq"?"IonQ":"IBM"}</span>`).join("")
      + `<span class="kh">Strip under the line</span>` + D.states.map((st,i)=>seen.has(i)?`<span>${sw(D.state_colors[i])}${st}</span>`:"").join("")
      + `<span class="kh">Gray behind the line</span><span>${sw("#e6e8eb")}not taking work</span>`; }
  const g=div("stats",s);
  for (const [n,l] of [[D.n_rows.toLocaleString(),"readings over four days, none failed"],[pct(forteB.stats.empty_open),"of the time Forte-1 was taking work, its queue was empty"],[fmt(forteB.stats.max),"tasks waiting on Forte-1 at its worst, Sunday afternoon"],[D.forte_rho,"correlation between Braket's count and IonQ's average for the same machine"]]) div("stat",g,`<div class="n">${n}</div><div class="l">${l}</div>`);
  const grid=div("grid",s); M.forEach((m,i)=>sparkTile(grid,m,i));
  const f=div("findings",s);
  const F=[["Online does not mean open.","Braket says ONLINE even when a machine is between its run windows and will not run anything. Cepheus is closed four hours a day. Garnet is closed all weekend. Forte-1 is closed every evening at six. You only learn this from the device properties.","calendar","See the calendar"],
           ["Braket and IonQ report different things for the same machine.","Braket counts the tasks waiting right now. IonQ reports an average wait over past jobs, and that average barely moved all week. For Forte-1 the two numbers have a correlation of 0.05.","forte","See both numbers"],
           ["Queue depths build up while the machine is open and closed.","Forte-1's job queue got larger while it was open. Garnet's got larger while it was closed for the weekend, and it emptied minutes after it opened. I suspect different use cases.","spread","See the split"]];
  for (const [h,p,tab,link] of F){ const c=div("finding",f,`<h3>${h}</h3><p>${p}</p>`); const a=document.createElement("a"); a.textContent=link; a.onclick=()=>show(tab); c.appendChild(a); }
},
calendar(s){
  const body = card(s, "When each machine was taking work", "Green means the machine was taking work. Light blue means Braket said ONLINE but the machine was outside its run window, so it would not run anything. Orange is IBM maintenance. Red is offline or unavailable. Hover a bar to see how long it lasted. Click a name to dim the other machines.",
    "We count a machine as taking work if it would run a job sent to it at that minute. For Braket that means status ONLINE and inside a run window. For IonQ it means status available. For IBM it means operational and active. We checked once a minute.",
    "The closures follow a schedule, so you can plan around them. Maintenance and outages do not. A plan made an hour ahead can be wrong by the time the job starts.");
  const wrap=div("panel",body); const RH=24, LBL=160, PW=W-LBL-R, TOP=24, H=RH*M.length;
  const svg=el("svg",{viewBox:`0 0 ${W} ${H+TOP+28}`},wrap); let focus=null; const rows=[];
  M.forEach((m,i)=>{ const y=TOP+i*RH; const g=el("g",{},svg); rows.push(g);
    for (const [a,b,st] of m.runs){ const r=el("rect",{x:LBL+a/D.hours*PW,y:y+2,width:Math.max((b-a)/D.hours*PW,.6),height:RH-4,fill:D.state_colors[st],rx:2},g); r.dataset.tip=`<b>${m.name}</b> ${D.states[st]}<br>${when(a)} to ${when(b)} (${(b-a).toFixed(1)} h)`; }
    const t=txt(svg,LBL-8,y+RH/2+3,m.name,{"text-anchor":"end","font-size":11,cursor:"pointer"}); t.onclick=()=>{ focus = focus===i?null:i; rows.forEach((g,k)=>g.setAttribute("opacity", focus==null||focus===k?1:.15)); };
    txt(svg,LBL-8,y+RH/2+12,m.vendor,{"text-anchor":"end","font-size":8,fill:"#656d76"}); });
  for (const [h0,name] of MID){ const X=LBL+h0/D.hours*PW; el("line",{x1:X,y1:TOP,x2:X,y2:TOP+H,stroke:"#1f2328","stroke-width":.7,"stroke-dasharray":"2,3"},svg); txt(svg,X+3,TOP-7,name+" 00:00",{"font-size":9}); }
  txt(svg,LBL,TOP-7,"Wed 19:10",{"font-size":9}); hoverTips(wrap,svg);
  const b2=card(s,"How much of the time each machine took work", "Forte-1 and Forte Enterprise show up twice, once through Braket and once through IonQ, and the two answers differ. IonQ reported Forte-1 unavailable in sixteen short spells that Braket never showed.", "The percent of our once a minute checks where the machine was up and would have run a job.", null);
  bars(b2, M.map(m=>({label:m.name, v:m.stats.up, color:m.color, tip:`${m.name} was taking work ${pct(m.stats.up)} of the time`})), v=>pct(v), 1);
},
queues(s){
  const body = card(s, "Every queue over the four days", "One panel per machine. Gray shading is when the machine was not taking work. Hover to see the value at that minute. Uncheck a machine to hide it.",
    "For Braket this is the number of tasks waiting in the Normal queue. For IBM it is the number of pending jobs. For IonQ it is a field called average_queue_time. IonQ reports it in milliseconds and we show it in hours. We read once a minute and plot every five minutes.",
    "Queues are short most of the time and long in bursts. Forte-1's queue built while it was open. Garnet's built while it was shut, and it cleared minutes after the machine opened. A queue on a shut machine is work waiting for it to open.");
  const ctl=div("controls",body); let lastv=null; const vis=new Set(M.map((m,i)=>i));
  M.forEach((m,i)=>{ if (m.vendor!==lastv){ ctl.innerHTML+=`<span class="vendor">${m.vendor}</span>`; lastv=m.vendor; } const lab=document.createElement("label"); const cb=document.createElement("input"); cb.type="checkbox"; cb.checked=true; cb.onchange=()=>{ cb.checked?vis.add(i):vis.delete(i); draw(); }; lab.appendChild(cb); lab.innerHTML+=sw(m.color)+m.name; lab.insertBefore(cb, lab.firstChild); ctl.appendChild(lab); });
  const ctl2=div("controls",body,`<label><input type="checkbox" id="qshade" checked> shade the hours not taking work</label>`);
  const charts=div("",body); ctl2.querySelectorAll("input").forEach(e=>e.addEventListener("change",draw));
  function draw(){ charts.innerHTML=""; const shade=ctl2.querySelector("#qshade").checked; const ms=M.map((m,i)=>[m,i]).filter(([m,i])=>vis.has(i));
    for (const [m] of ms){ const wrap=div("panel",charts); const TOP=22,H=96,BOT=22; const svg=el("svg",{viewBox:`0 0 ${W} ${H+TOP+BOT}`},wrap); const ymax=Math.max(1,...m.points.map(p=>p[1]||0))*1.08;
      if (shade) shadeRuns(svg,m.runs,L,TOP,W-L-R,H,0,D.hours); yAxis(svg,L,TOP,W-L-R,H,ymax,m.unit,null,3); dayAxis(svg,L,TOP,W-L-R,H,0,D.hours); stepPath(svg,m.points,0,1,L,TOP,W-L-R,H,0,D.hours,ymax,m.color);
      txt(svg,L+4,TOP-8,m.name+"  ·  "+m.vendor,{"font-size":11,"font-weight":"600"});
      cursor(wrap,svg,L,TOP,W-L-R,H,0,D.hours,hr=>{ const p=at(m.points,hr,0); return p?`${sw(m.color)}${m.name} <b>${fmt(p[1])}</b> ${m.unit} · ${D.states[p[2]]}`:"-"; }); } }
  draw();
},
forte(s){
  const body = card(s, "Braket and IonQ report different numbers for Forte-1", `Forte-1 is one machine. You can reach it through Braket or through IonQ's own API. Braket reports how many tasks are waiting right now. IonQ reports average_queue_time, which is an average wait over past jobs. That average drifted from 65 hours to 57 hours over the week. The bottom panel puts both on one scale. Each minute is ranked against all the other minutes in its own line. 0 is the emptiest minute that line ever had and 100 is the busiest. If both numbers measured the same queue, the two lines would move together.`,
    "We read both services in the same pass every minute, so each pair of numbers comes from the same minute. IonQ's last_updated stamp changed every fifteen minutes. The correlation is Spearman's rank correlation over all paired minutes.",
    `The two lines do not move together. The correlation is ${D.forte_rho}. If you want to know the queue on Forte-1 right now, use Braket's count. IonQ's average does not tell you.`);
  const wrap=div("panel",body); const TOP=22,H=110,GAP=40; const total=3*(H+GAP)+TOP; const svg=el("svg",{viewBox:`0 0 ${W} ${total}`},wrap);
  const F=D.forte; const bmax=Math.max(...F.map(f=>f[1]||0))*1.08, imax=Math.max(...F.map(f=>f[2]||0))*1.08;
  const panels=[[1,bmax,"tasks waiting",forteB.color,"Braket, tasks waiting in the Normal queue"],[2,imax,"average queue time (h)",forteI.color,"IonQ, average_queue_time in hours"],[null,100,"rank within its own series",null,"Both ranked against themselves, 0 the emptiest minute that series showed, 100 the busiest"]];
  panels.forEach(([yi,ymax,label,color,title],k)=>{ const y=TOP+k*(H+GAP); yAxis(svg,L,y,W-L-R,H,ymax,label,k===2?(v=>v+""):null,3); dayAxis(svg,L,y,W-L-R,H,0,D.hours);
    if (yi!=null) stepPath(svg,F,0,yi,L,y,W-L-R,H,0,D.hours,ymax,color); else { stepPath(svg,F,0,5,L,y,W-L-R,H,0,D.hours,100,forteB.color,1.3); stepPath(svg,F,0,6,L,y,W-L-R,H,0,D.hours,100,forteI.color,1.3); }
    txt(svg,L+4,y-8,title,{"font-size":11,"font-weight":"600"}); });
  cursor(wrap,svg,L,TOP,W-L-R,total-TOP,0,D.hours,hr=>{ const f=at(F,hr,0); return f?`${sw(forteB.color)}Braket <b>${fmt(f[1])}</b> tasks waiting (rank ${fmt(f[5],0)}) · ${D.states[f[3]]}<br>${sw(forteI.color)}IonQ <b>${fmt(f[2],1)}</b> h average (rank ${fmt(f[6],0)}) · ${D.states[f[4]]}`:"-"; });
  const r=div("row",s);
  const b2=card(r,"Every minute as a dot", `Braket's count is on the x axis and IonQ's average is on the y axis. ${F.length.toLocaleString()} minutes. Darker dots are where many minutes landed on the same spot. If the two measured the same queue, the dots would climb a line.`, "One dot per minute, rounded to the nearest task and tenth of an hour.", null);
  const w2=div("panel",b2); const SW=520,SH=300,SL=60,ST=16; const sv=el("svg",{viewBox:`0 0 ${SW} ${SH+ST+40}`},w2);
  const spots=new Map(); for (const f of F){ if (f[1]==null||f[2]==null) continue; const k=Math.round(f[1])+"|"+f[2].toFixed(1); spots.set(k,(spots.get(k)||0)+1); } const top=Math.max(...spots.values()); const xs=[...spots.keys()].map(k=>+k.split("|")[0]), ys=[...spots.keys()].map(k=>+k.split("|")[1]); const xmax=Math.max(...xs)+1, ylo=Math.min(...ys)-1, yhi=Math.max(...ys)+1;
  const X=v=>SL+(v+.5)/(xmax+.5)*(SW-SL-10), Y=v=>ST+SH-(v-ylo)/(yhi-ylo)*SH;
  for (const t of nice(xmax,6)){ el("line",{x1:X(t),y1:ST,x2:X(t),y2:ST+SH,stroke:"#eef0f2"},sv); txt(sv,X(t),ST+SH+13,fmt(t),{"text-anchor":"middle","font-size":9}); }
  for (let v=Math.ceil(ylo); v<=yhi; v+=2){ el("line",{x1:SL,y1:Y(v),x2:SW-10,y2:Y(v),stroke:"#eef0f2"},sv); txt(sv,SL-6,Y(v)+3,v,{"text-anchor":"end","font-size":9}); }
  el("line",{x1:SL,y1:ST+SH,x2:SW-10,y2:ST+SH,stroke:"#9ca3af"},sv); el("line",{x1:SL,y1:ST,x2:SL,y2:ST+SH,stroke:"#9ca3af"},sv);
  txt(sv,SL+(SW-SL)/2,ST+SH+30,"Braket, tasks waiting",{"text-anchor":"middle","font-size":10}); txt(sv,SL-44,ST+SH/2,"IonQ average (h)",{"text-anchor":"middle","font-size":10,transform:`rotate(-90 ${SL-44} ${ST+SH/2})`});
  for (const [k,n] of spots){ const [x,y]=k.split("|").map(Number); const c=el("circle",{cx:X(x),cy:Y(y),r:3,fill:forteI.color,"fill-opacity":.15+.85*Math.sqrt(n/top)},sv); c.dataset.tip=`Braket ${x} waiting, IonQ ${y.toFixed(1)} h average<br>${n} minute${n>1?"s":""}`; }
  txt(sv,SL+6,ST+14,"rank correlation "+D.forte_rho,{"font-size":11,"font-weight":"600"}); hoverTips(w2,sv);
  card(r,"Do they agree the machine is up?", `Mostly. In ${pct(D.forte_both_up)} of the minutes where either one said Forte-1 was up, both did. IonQ showed sixteen short outages that Braket never did. Several were on Sunday morning while Braket's count kept growing.`, "Braket up means ONLINE and inside a run window. IonQ up means status available. We compared them minute by minute.", "Status is the one thing you can cross check between the two services. Even there they differ a little.");
},
day(s){
  const body = card(s, "One day, one machine", "Pick a day and a machine. The line is the queue through that day. Gray shading is when the machine was not taking work. It opens on Sunday on Forte-1, which had the one long queue of the whole run.",
    "This uses the same once a minute checks as the Queues view. We plot one point every five minutes for the day you pick.",
    "On Sunday Forte-1 had nine to nineteen tasks waiting for eleven hours. This is when a hold helps. Without one, a classical job sits on its cores for the whole wait. For the other 85 hours Forte-1 was basically free.");
  const ctl=div("controls",body); const dayc=div("",ctl); const mc=document.createElement("select"); M.forEach((m,i)=>{ const o=document.createElement("option"); o.value=i; o.textContent=m.name+" ("+m.vendor+")"; mc.appendChild(o); }); ctl.appendChild(mc);
  DAYS.forEach((d,i)=>{ const b=document.createElement("button"); b.className="chip"; b.textContent=d.name+" "+d.date; b.onclick=()=>{ dayIndex=i; draw(); }; dayc.appendChild(b); });
  mc.onchange=()=>{ dayMachine=+mc.value; draw(); }; const chart=div("panel",body);
  function draw(){ mc.value=dayMachine; [...dayc.children].forEach((c,k)=>c.classList.toggle("on",k===dayIndex)); chart.innerHTML=""; const m=M[dayMachine], d=DAYS[dayIndex]; const from=d.from, to=d.to; const pts=m.points.filter(p=>p[0]>=from&&p[0]<=to); const TOP=22,H=220,BOT=24; const svg=el("svg",{viewBox:`0 0 ${W} ${H+TOP+BOT}`},chart);
    const ymax=Math.max(1,...pts.map(p=>p[1]||0))*1.1; shadeRuns(svg,m.runs,L,TOP,W-L-R,H,from,to); yAxis(svg,L,TOP,W-L-R,H,ymax,m.unit,null,4); dayAxis(svg,L,TOP,W-L-R,H,from,to,true); stepPath(svg,m.points,0,1,L,TOP,W-L-R,H,from,to,ymax,m.color,1.8);
    txt(svg,L+4,TOP-8,`${m.name} · ${when(from+.01).slice(0,10)} · grey means not taking work`,{"font-size":11,"font-weight":"600"});
    cursor(chart,svg,L,TOP,W-L-R,H,from,to,hr=>{ const p=at(m.points,hr,0); return p?`${sw(m.color)}${m.name} <b>${fmt(p[1])}</b> ${m.unit} · ${D.states[p[2]]}`:"-"; }); }
  redrawDay=draw; draw();
},
hours(s){
  const body = card(s, "By hour of the day", "Two heat maps. Rows are machines and columns are UTC hours. The top map shows how much of each hour the machine was not taking work. White is never and dark is the whole hour. The bottom map shows the median queue in each hour. Each machine is scaled to its own busiest hour. Hover a cell for the numbers.",
    "We sorted every check into its UTC hour. Each cell covers four hours of one machine, one hour from each day. The top map is the percent of those checks where the machine was not taking work. The bottom map is the median queue in that hour, divided by that machine's busiest hour so the colors line up across machines.",
    "The closures repeat every day. The queues do not. Four days is not enough to say when a queue will be short, so check the queue when you send the job.");
  heat(body, "Fraction of the hour not taking work", m=>m.hourly.map(h=>h.closed), (m,h,v)=>`${m.name} at ${String(h).padStart(2,"0")}:00<br>not taking work ${pct(v)} of the hour`, [213,94,0], false);
  heat(body, "Median queue in the hour, relative to the machine's busiest hour", m=>{ const vals=m.hourly.map(h=>h.median); const mx=Math.max(...vals.filter(v=>v!=null),1e-9); return vals.map(v=>v==null?null:v/mx); }, (m,h,v)=>`${m.name} at ${String(h).padStart(2,"0")}:00<br>median ${fmt(m.hourly[h].median)} ${m.unit}`, [0,114,178], true);
},
spread(s){
  const body = card(s, "How long the queues are", "Each bar goes from the 10th to the 90th percentile of the machine's queue while it was taking work. The tick is the median. The dot is the worst minute of the whole run. A bar that is only a tick means the queue was the same number nine times out of ten, usually zero.",
    "Percentiles use only the minutes the machine was taking work. The worst minute uses every minute. There is one panel per service because the units differ.",
    "A mean is the wrong summary here. The usual value is zero and the tail is long. If you have to pick a machine, look at the median and the tail, and make sure the numbers are recent.");
  for (const v of ["braket","ionq","ibm"]){ const ms=M.filter(m=>m.vendor===v); const wrap=div("panel",body); const RH=26,LBL=170,TOP=18,PW=W-LBL-110; const svg=el("svg",{viewBox:`0 0 ${W} ${RH*ms.length+TOP+34}`},wrap); const xmax=Math.max(...ms.map(m=>m.stats.max))*1.1; const X=x=>LBL+x/xmax*PW;
    for (const t of nice(xmax,5)){ el("line",{x1:X(t),y1:TOP,x2:X(t),y2:TOP+RH*ms.length,stroke:"#eef0f2"},svg); txt(svg,X(t),TOP+RH*ms.length+13,fmt(t),{"text-anchor":"middle","font-size":9}); }
    ms.forEach((m,i)=>{ const y=TOP+i*RH+RH/2, st=m.stats; const g=el("g",{},svg); const tip=`<b>${m.name}</b><br>p10 ${fmt(st.p10)} · median ${fmt(st.p50)} · p90 ${fmt(st.p90)} · worst ${fmt(st.max)} ${m.unit}`; const r=el("rect",{x:X(st.p10),y:y-6,width:Math.max(X(st.p90)-X(st.p10),1.5),height:12,fill:m.color,"fill-opacity":.45,rx:2},g); r.dataset.tip=tip;
      const tk=el("rect",{x:X(st.p50)-1.2,y:y-8,width:2.4,height:16,fill:m.color},g); tk.dataset.tip=tip; const c=el("circle",{cx:X(st.max),cy:y,r:3.4,fill:"#1f2328"},g); c.dataset.tip=tip;
      txt(svg,LBL-8,y+3,m.name,{"text-anchor":"end","font-size":11}); txt(svg,X(st.max)+8,y+3,"worst "+fmt(st.max),{"font-size":9,fill:"#656d76"}); });
    txt(svg,LBL+PW/2,TOP+RH*ms.length+28,v+", "+ms[0].unit,{"text-anchor":"middle","font-size":10,fill:"#656d76"}); hoverTips(wrap,svg); }
  { const b=card(s,"How often the queue was empty","Dark bars are minutes the machine was taking work. Light bars are minutes it was not. We keep them apart because an empty queue on a shut machine does not mean nobody wants it. And a queue on a shut machine is just waiting for it to open.",
      "The percent of our checks where nothing was waiting. We split it by whether the machine was taking work at the time. IonQ is left out because its average is never zero.",
      "Forte-1's queue built while it was open. Garnet's built while it was shut and emptied minutes after it opened. Fez and pittsburgh on IBM had their biggest queues during maintenance. Only count a queue while the machine is open. A queue on a shut machine is the first thing it will do when it opens.");
    const items=[]; for (const m of M){ if (m.vendor==="ionq") continue; items.push({label:m.name, v:m.stats.empty_open||0, color:m.color, op:1, tip:`${m.name} while taking work<br>empty ${pct(m.stats.empty_open)} of ${m.stats.n_open.toLocaleString()} minutes, mean ${fmt(m.stats.mean_open)} ${m.unit}`}); if (m.stats.n_closed) items.push({label:"", v:m.stats.empty_closed||0, color:m.color, op:.35, tip:`${m.name} while not taking work<br>empty ${pct(m.stats.empty_closed)} of ${m.stats.n_closed.toLocaleString()} minutes, mean ${fmt(m.stats.mean_closed)} ${m.unit}`}); }
    bars(b, items, v=>pct(v), 1); }
  bars(card(s,"How fast the queues drain","The biggest drop in the queue over any ten minutes, per minute. A drop means more tasks finished than arrived, so the machine clears at least this fast. IonQ is left out because an average does not drain.",
      "For every ten-minute window we took the drop in the queue and divided by ten. We kept the biggest one and when it happened. New arrivals during the window hide some of the finished tasks, so the true rate is at least this.",
      "This turns a queue length into a wait. Ten tasks ahead of you on Forte-1 is about seven minutes at most, if nothing else arrives. IBM jobs are short, and those machines clear tens of jobs a minute."),
    M.filter(m=>m.vendor!=="ionq"&&m.stats.drain_per_min!=null).map(m=>({label:m.name,v:m.stats.drain_per_min,color:m.color,tip:`${m.name} fell from ${fmt(m.stats.drain_from)} to ${fmt(m.stats.drain_to)} in ten minutes<br>${when(m.stats.drain_at)}`})), v=>fmt(v,1)+"/min", null);
},
calibration(s){
  const body = card(s, "When does quantum device metadata change?", "I noticed that we can query the machines and see that the metadata changes over time. The change can be a no-op (a change in timestamp) or an update to metadata, or a full calibration. The plot below shows these changes. Each mark is a time the vendor changed what it publishes about the machine. Most changes are <strong>not</strong> a calibration. E.g., gray means only the dates changed. Light blue means Rigetti posted a new calibration file for Cepheus. Green and blue mean a few or many measured numbers changed, like the error rate of some gates. Red means most of the machine's numbers changed at once, which is what a full calibration looks like from the outside. The interesting thing to think about is that the further away you get from a calibration, the higher the error rates go. But when you calibrate, you cannot serve jobs. Click a color in the key to show only that kind. Hover a mark for details.",
    "Each time a record changes we compare it with the one before it. We ignore dates and timestamps. On every machine the changes either moved under 15 percent of the numbers or between 30 and 45 percent, with nothing in between. We call the big ones \"full calibrations.\" We cannot see a calibration happen, so this is the best sign the published data gives. Forte through Braket is missing because there is not enough data.",
    "Machines can stay up while their numbers change, because most changes are small re-measurements or just new dates. Full calibrations came about once a day on each IBM machine. On Cepheus they came right after its two daily closures, so that is likely when Rigetti recalibrates. Qubits usually drift between calibrations, so a job that runs long after the last red mark probably sees noisier qubits. We did not measure that drift.");
  if (!D.have_changes){ div("note",body,"Run scripts/changes.py to sort the changes by kind. Until then every change is drawn the same."); }
  const ms=M.filter(m=>m.versions>0); const shown=new Set([0,1,2,3,4]);
  const leg=div("controls",body);
  D.change_kinds.forEach((name,k)=>{ const n=ms.reduce((a,m)=>a+m.changes.filter(c=>c[1]===k).length,0); if(!n) return; const b=document.createElement("button"); b.className="chip on"; b.innerHTML=`${sw(D.change_colors[k])}${name} <span style="opacity:.6">${n}</span>`; b.onclick=()=>{ shown.has(k)?shown.delete(k):shown.add(k); b.classList.toggle("on",shown.has(k)); draw(); }; leg.appendChild(b); });
  const wrap=div("panel",body);
  function draw(){ wrap.innerHTML=""; const RH=30,LBL=170,TOP=22,PW=W-LBL-170; const svg=el("svg",{viewBox:`0 0 ${W} ${RH*ms.length+TOP+30}`},wrap);
    for (const [h0,name] of MID){ const X=LBL+h0/D.hours*PW; el("line",{x1:X,y1:TOP,x2:X,y2:TOP+RH*ms.length,stroke:"#1f2328","stroke-width":.7,"stroke-dasharray":"2,3"},svg); txt(svg,X+3,TOP-7,name,{"font-size":9}); }
    ms.forEach((m,i)=>{ const y=TOP+i*RH;
      shadeRuns(svg,m.runs,LBL,y+2,PW,RH-4,0,D.hours,"#f4f5f7");
      const list = m.changes.length ? m.changes : m.calibration.map(h=>[h,3,null,null]);
      for (const k of [0,1,2,3,4]){ if (!shown.has(k)) continue; for (const c of list){ if (c[1]!==k) continue; const X=LBL+c[0]/D.hours*PW; const big=k===4;
        const l=el("line",{x1:X,y1:y+(big?3:7),x2:X,y2:y+RH-(big?3:7),stroke:D.change_colors[k],"stroke-width":big?2.6:1.2},svg);
        l.dataset.tip=`<b>${m.name}</b> ${when(c[0])}<br>${D.change_kinds[k]}${c[2]!=null&&k>1?`<br>${c[2].toLocaleString()} numbers moved, ${Math.round(c[3]*100)}% of the record`:""}`; } }
      txt(svg,LBL-8,y+RH/2+3,m.name,{"text-anchor":"end","font-size":11});
      const full=m.changes.filter(c=>c[1]===4).length, all=m.changes.length||m.calibration.length;
      txt(svg,LBL+PW+8,y+RH/2+3,`${all} changes, ${full} full`,{"font-size":10,fill:"#656d76"});
      el("line",{x1:LBL,y1:y+RH,x2:LBL+PW,y2:y+RH,stroke:"#eef0f2"},svg); });
    txt(svg,LBL,TOP+RH*ms.length+16,"Light gray background is when the machine was not taking work.",{"font-size":9,fill:"#656d76"});
    hoverTips(wrap,svg); }
  draw();
},
data(s){
  const body = card(s, "Summary stats", "There is one row per machine, measured over four days. The \"taking work\" column is the percent of the time the machine was up and could have run a job. The \"empty while open\" column is the percent of that time when the queue had nothing in it. The \"empty while shut\" column is the percent of the time the machine was not running jobs when the queue had nothing in it. For context, people can submit work to the queue when the machines are offline and it can pile up to run when they come back. The \"worst\" column is the biggest queue we ever saw on that machine in any single minute of the four days, whether it was running or not. The queue is counted differently by each vendor. On Braket it is the number of tasks waiting. On IBM it is the number of pending jobs. On IonQ it is the average queue time in hours, because that is all IonQ reports.", null, null);
  let h="<table class='tbl'><tr><th>machine</th><th>service</th><th>unit</th><th>taking work</th><th>empty while open</th><th>empty while shut</th><th>p10</th><th>median</th><th>p90</th><th>worst</th><th>drain /min</th><th>record changes</th></tr>";
  for (const m of M){ const st=m.stats; h+=`<tr><td>${sw(m.color)}${m.name}</td><td>${m.vendor}</td><td>${m.unit}</td><td>${pct(st.up)}</td><td>${pct(st.empty_open)}</td><td>${st.n_closed?pct(st.empty_closed):"-"}</td><td>${fmt(st.p10)}</td><td>${fmt(st.p50)}</td><td>${fmt(st.p90)}</td><td>${fmt(st.max)}</td><td>${st.drain_per_min==null?"-":fmt(st.drain_per_min,1)}</td><td>${Math.max(0,m.versions-1)}</td></tr>`; }
  body.innerHTML+=h+"</table>";
  const r=div("row",s);
  card(r,"What we asked each service","We asked Braket for each device's queue depth and its device record. The depth is a count of tasks waiting in the Normal and Priority queues, plus hybrid jobs. The record has the status, the run windows and the calibration. We asked IonQ for its backend list, which gives a status and average_queue_time in milliseconds. We asked IBM for every backend the account can see, which gives pending jobs, whether it is operational, its status message, and the calibration record.",null,null);
  card(r,"What we kept, and what this cannot tell you","Every row keeps the full response from the service, how long the call took, and which minute it belongs to. That is how we line up a machine read through two services. Big records that a service repeats without change are stored once and then again whenever they change. None of this can tell you how long your own task would wait. That takes a real task, and a task on these machines costs money and only measures one submission on one day. The raw rows are in results/queues.jsonl next to this page. The figures come from scripts/figures.py and this page from scripts/page.py.",null,null);
}};

function bars(body, items, fmtv, fixedMax){ const wrap=div("panel",body); const RH=22,LBL=150,TOP=6,PW=W-LBL-90; const svg=el("svg",{viewBox:`0 0 ${W} ${RH*items.length+TOP+6}`},wrap); const mx=fixedMax||Math.max(...items.map(i=>i.v))*1.1;
  items.forEach((it,i)=>{ const y=TOP+i*RH; const r=el("rect",{x:LBL,y:y+4,width:Math.max(it.v/mx*PW,1),height:RH-8,fill:it.color,"fill-opacity":it.op==null?1:it.op,rx:3},svg); r.dataset.tip=it.tip; txt(svg,LBL-8,y+RH/2+3,it.label,{"text-anchor":"end","font-size":11}); txt(svg,LBL+it.v/mx*PW+6,y+RH/2+3,fmtv(it.v),{"font-size":10}); });
  hoverTips(wrap,svg); }
function heat(body, title, valuesOf, tipOf, hue, relative){ div("legend",body,"<b>"+title+"</b>"); const wrap=div("panel",body); const RH=22,CW=36,LBL=170,TOP=18; const svg=el("svg",{viewBox:`0 0 ${LBL+24*CW+120} ${RH*M.length+TOP+30}`},wrap); svg.style.maxWidth=(LBL+24*CW+120)+"px";
  const col=f=>{ if (f==null) return "#fff"; f=Math.max(0,Math.min(1,f)); const [r,g,b]=hue; return `rgb(${Math.round(245+(r-245)*f)},${Math.round(245+(g-245)*f)},${Math.round(248+(b-248)*f)})`; };
  M.forEach((m,i)=>{ const vals=valuesOf(m); const y=TOP+i*RH; vals.forEach((v,h)=>{ const r=el("rect",{x:LBL+h*CW+1,y:y+1,width:CW-2,height:RH-2,fill:col(v),rx:2},svg); r.dataset.tip=tipOf(m,h,v); }); txt(svg,LBL-8,y+RH/2+3,m.name,{"text-anchor":"end","font-size":11}); if (relative){ const mx=Math.max(...m.hourly.map(h=>h.median||0)); txt(svg,LBL+24*CW+8,y+RH/2+3,"busiest "+fmt(mx),{"font-size":9,fill:"#656d76"}); } });
  for (let h=0;h<24;h+=3) txt(svg,LBL+h*CW+CW/2,TOP-6,String(h).padStart(2,"0"),{"text-anchor":"middle","font-size":9});
  const y=TOP+RH*M.length+8; for (let k=0;k<20;k++) el("rect",{x:LBL+k*8,y:y,width:8,height:9,fill:col(k/19)},svg); txt(svg,LBL-6,y+8,relative?"0":"0%",{"text-anchor":"end","font-size":9}); txt(svg,LBL+166,y+8,relative?"the machine's busiest hour":"the whole hour",{"font-size":9});
  hoverTips(wrap,svg); }

const first=(location.hash||"#overview").slice(1); show(first in BUILD ? first : "overview");
</script></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plots", "index.html"))
    ap.add_argument("--every", type=int, default=300, help="seconds between the points kept in the long views, 300")
    ap.add_argument("--changes", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "changes.json"), help="the sorted record changes from changes.py")
    args = ap.parse_args(argv)
    data = pack(load(args.files), args.every, load_changes(args.changes))
    # the data would be one line half a megabyte long, which some editors
    # choke on, so it is broken at array boundaries. Newlines are whitespace
    # to JSON, so nothing changes for the browser
    blob = json.dumps(data, separators=(",", ":")).replace("],[", "],\n[").replace("},{", "},\n{")
    page = PAGE.replace("__DATA__", blob)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(page)
    print("  {} ({} KB)".format(args.out, os.path.getsize(args.out) // 1024))


if __name__ == "__main__":
    main()
