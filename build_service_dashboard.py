"""
Generate a standalone HTML dashboard showing per-service call stats, top endpoints,
and endpoint→status Sankey flows from AWS ALB logs.
Reads logs-insights-results.json directly; no dependency on build_service_graph.py.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np

# ── Parse ─────────────────────────────────────────────────────────────────────

with open("logs-insights-results.json") as f:
    raw = json.load(f)

ALB_RE = re.compile(
    r'\S+ (\S+) \S+ \S+ \S+ \S+ ([0-9.-]+) \S+ (\d+) \S+ \d+ \d+ "(\w+) (https?://\S+) \S+"'
)
TID_RE = re.compile(r'(TID_[a-f0-9]+)')


def extract_service(url):
    m = re.search(r'/services/([^/?]+)', url)
    if m:
        return m.group(1)
    if 'grant-token' in url or '/api/sts/' in url:
        return 'sts-auth'
    if '/realms/' in url:
        return 'keycloak'
    if '/wd/hub/' in url:
        return 'selenium-grid'
    if 'ui-test-screenshots' in url:
        return 's3-storage'
    return 'gateway'


def norm_path(url):
    path = re.sub(r'\?.*', '', url)
    path = re.sub(r'https?://[^/]+', '', path)
    path = re.sub(r'(/wd/hub/session/)[^/]+', r'\1{session}', path)
    path = re.sub(r'/element/[^/]+', '/element/{elem}', path)
    path = re.sub(r'/\d+', '/{id}', path)
    return path


parsed = []
for rec in raw:
    msg = rec['@message']
    m = ALB_RE.match(msg)
    if not m:
        continue
    ts_str, tpt, status, method, url = m.groups()
    try:
        ts = datetime.fromisoformat(ts_str.replace('Z', ''))
    except ValueError:
        continue
    tpt_f = float(tpt)
    tid_m = TID_RE.search(msg)
    parsed.append({
        'ts':           ts,
        'tpt_ms':       tpt_f * 1000 if tpt_f >= 0 else None,
        'status':       status,
        'status_class': f"{status[0]}xx",
        'method':       method,
        'path':         norm_path(url),
        'service':      extract_service(url),
        'is_error':     status.startswith('5'),
        'tid':          tid_m.group(1) if tid_m else None,
    })

# TID → set of services (for co-dependency analysis)
tid_services = defaultdict(set)
for req in parsed:
    if req['tid']:
        tid_services[req['tid']].add(req['service'])

# ── Per-service aggregation ────────────────────────────────────────────────────

svc_data = defaultdict(lambda: {
    'reqs': [],
    'ep_counts':  Counter(),   # "METHOD /path" → total count
    'ep_errors':  Counter(),   # "METHOD /path" → error count
    'ep_status':  defaultdict(Counter),  # "METHOD /path" → status_class → count
})

for req in parsed:
    svc = req['service']
    ep  = f"{req['method']} {req['path']}"
    sd  = svc_data[svc]
    sd['reqs'].append(req)
    sd['ep_counts'][ep] += 1
    if req['is_error']:
        sd['ep_errors'][ep] += 1
    sd['ep_status'][ep][req['status_class']] += 1

# Service co-dependencies: which other services appear in the same TID
svc_deps = defaultdict(Counter)
for svcs in tid_services.values():
    for s1 in svcs:
        for s2 in svcs:
            if s1 != s2:
                svc_deps[s1][s2] += 1

COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
    "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
]

# ── Sankey: endpoint paths (left) → HTTP status classes (right) ───────────────

STATUS_COLORS = {'2xx': '#59a14f', '3xx': '#76b7b2', '4xx': '#edc948', '5xx': '#e15759'}


def make_endpoint_sankey(ep_status, accent, max_ep=7):
    if not ep_status:
        return ""

    # Aggregate (short_ep, status_class) → count
    flows = Counter()
    for ep, classes in ep_status.items():
        short = ep[-30:] if len(ep) > 30 else ep
        for cls, cnt in classes.items():
            flows[(short, cls)] += cnt

    left_nodes, right_nodes = [], []
    for (ep, cls), _ in flows.most_common(30):
        if ep not in left_nodes and len(left_nodes) < max_ep:
            left_nodes.append(ep)
        if cls not in right_nodes:
            right_nodes.append(cls)
    right_nodes = sorted(right_nodes)

    if not left_nodes or not right_nodes:
        return ""

    W    = 520
    H    = max(max(len(left_nodes), len(right_nodes)) * 38 + 20, 120)
    nh   = 22
    max_c = max(flows.values())

    def ly(i): return 10 + i * 38 + nh / 2
    def ry(i): return 10 + i * 38 + nh / 2

    svg = [
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-height:340px">',
        '<style>.nl{font:9px monospace;fill:#ccc}.nr{font:9px monospace;fill:#ccc;text-anchor:end}</style>',
    ]

    for (l, r), cnt in flows.items():
        if l not in left_nodes or r not in right_nodes:
            continue
        li, ri = left_nodes.index(l), right_nodes.index(r)
        x1, y1, x2, y2 = 185, ly(li), 330, ry(ri)
        sw    = max(1, int(cnt / max_c * 6))
        alpha = 0.2 + 0.7 * (cnt / max_c)
        rc    = STATUS_COLORS.get(r, '#888888')
        svg.append(
            f'<path d="M{x1},{y1} C{(x1+x2)//2},{y1} {(x1+x2)//2},{y2} {x2},{y2}" '
            f'stroke="{rc}" stroke-width="{sw}" fill="none" stroke-opacity="{alpha:.2f}"/>'
        )

    for i, n in enumerate(left_nodes):
        y = ly(i)
        svg.append(f'<rect x="158" y="{y-nh//2}" width="26" height="{nh}" rx="3" fill="{accent}" opacity="0.7"/>')
        svg.append(f'<text x="153" y="{y+4}" class="nr">{n}</text>')

    for i, n in enumerate(right_nodes):
        y  = ry(i)
        rc = STATUS_COLORS.get(n, '#888888')
        svg.append(f'<rect x="330" y="{y-nh//2}" width="26" height="{nh}" rx="3" fill="{rc}" opacity="0.8"/>')
        svg.append(f'<text x="361" y="{y+4}" class="nl">{n}</text>')

    svg.append("</svg>")
    return "\n".join(svg)


# ── Build per-service cards ────────────────────────────────────────────────────

cards = []
for ci, svc in enumerate(sorted(svc_data, key=lambda s: -len(svc_data[s]['reqs']))):
    sd      = svc_data[svc]
    reqs    = sd['reqs']
    color   = COLORS[ci % len(COLORS)]
    n       = len(reqs)
    errs    = sum(1 for r in reqs if r['is_error'])
    err_pct = errs / n * 100
    tpts    = [r['tpt_ms'] for r in reqs if r['tpt_ms'] is not None]
    arr     = np.array(tpts) if tpts else np.array([0.0])
    avg_ms  = arr.mean()
    p95_ms  = float(np.percentile(arr, 95))

    # Top endpoints table
    top_ep = sd['ep_counts'].most_common(6)
    trows  = ""
    for ep, cnt in top_ep:
        ep_errs = sd['ep_errors'].get(ep, 0)
        err_cls = ' class="err"' if ep_errs else ''
        err_txt = f' <span class="ep-err">({ep_errs} err)</span>' if ep_errs else ''
        short   = ep[-44:] if len(ep) > 44 else ep
        trows  += f'<tr><td{err_cls}>{short}{err_txt}</td><td class="num">{cnt}</td></tr>'

    # Co-dependency chips
    deps     = svc_deps[svc].most_common(5)
    dep_html = (
        " ".join(
            f'<span class="dep-tag">{d} <span class="dep-cnt">{c}</span></span>'
            for d, c in deps
        ) if deps else '<span style="color:#52525b">—</span>'
    )

    sankey = make_endpoint_sankey(sd['ep_status'], color)

    cards.append(f"""
<div class="card" style="--accent:{color}">
  <div class="card-header">
    <span class="badge" style="background:{color}">{ci}</span>
    <h2>{svc}</h2>
    <div class="metrics">
      <div class="metric"><span class="val">{n:,}</span><span class="lbl">calls</span></div>
      <div class="metric {'metric-err' if err_pct > 10 else ''}">
        <span class="val">{err_pct:.0f}%</span><span class="lbl">errors</span>
      </div>
      <div class="metric"><span class="val">{avg_ms:.0f}ms</span><span class="lbl">avg</span></div>
      <div class="metric"><span class="val">{p95_ms:.0f}ms</span><span class="lbl">p95</span></div>
    </div>
  </div>
  <div class="card-body">
    <div class="col-flow">
      <div class="section-title">Endpoint → Status</div>
      {sankey}
    </div>
    <div class="col-right">
      <div class="section-title">Top endpoints</div>
      <table class="ttable"><tbody>{trows}</tbody></table>
      <div class="section-title" style="margin-top:14px">TID co-dependencies</div>
      <div class="deps">{dep_html}</div>
    </div>
  </div>
</div>""")

# ── Emit HTML ─────────────────────────────────────────────────────────────────

total_reqs   = len(parsed)
total_svcs   = len(svc_data)
total_errors = sum(1 for r in parsed if r['is_error'])
ts_range     = f"{min(r['ts'] for r in parsed).strftime('%H:%M')}–{max(r['ts'] for r in parsed).strftime('%H:%M UTC')}"

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Service Dashboard — WM Sandbox</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0f1117; color: #d4d4d8;
    padding: 28px 20px 60px; min-height: 100vh;
  }}
  header {{ text-align: center; margin-bottom: 36px; }}
  header h1 {{ font-size: 1.7rem; font-weight: 700; color: #fff; letter-spacing: -0.5px; }}
  header p {{ margin-top: 6px; color: #71717a; font-size: 0.88rem; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(700px, 1fr));
    gap: 22px; max-width: 1600px; margin: 0 auto;
  }}
  .card {{
    background: #18181b; border: 1px solid #27272a;
    border-left: 4px solid var(--accent); border-radius: 10px; overflow: hidden;
  }}
  .card-header {{
    padding: 16px 20px 12px; border-bottom: 1px solid #27272a;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }}
  .badge {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 28px; height: 28px; border-radius: 6px;
    font-size: 0.8rem; font-weight: 700; color: #fff; flex-shrink: 0;
  }}
  .card-header h2 {{
    font-size: 0.88rem; font-weight: 600; color: #e4e4e7;
    flex: 1; min-width: 0; word-break: break-all;
  }}
  .metrics {{ display: flex; gap: 18px; margin-left: auto; }}
  .metric {{ display: flex; flex-direction: column; align-items: flex-end; }}
  .metric .val {{ font-size: 1.05rem; font-weight: 700; color: #fff; line-height: 1.1; }}
  .metric .lbl {{ font-size: 0.68rem; color: #71717a; text-transform: uppercase; letter-spacing: 0.04em; }}
  .metric-err .val {{ color: #f87171; }}
  .card-body {{ display: flex; padding: 0; }}
  .col-flow {{ flex: 1.1; padding: 14px 16px; border-right: 1px solid #27272a; min-width: 0; }}
  .col-right {{ flex: 0.9; padding: 14px 16px; min-width: 0; }}
  .section-title {{
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.07em; color: #71717a; margin-bottom: 8px;
  }}
  .ttable {{ width: 100%; border-collapse: collapse; font-size: 0.73rem; }}
  .ttable td {{ padding: 3px 4px; color: #a1a1aa; word-break: break-all; }}
  .ttable td.num {{ color: var(--accent); font-weight: 600; text-align: right; width: 40px; min-width: 40px; }}
  .ttable td.err {{ color: #fca5a5; }}
  .ttable tr:hover td {{ color: #e4e4e7; }}
  .ep-err {{ color: #f87171; font-size: 0.68rem; }}
  .deps {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }}
  .dep-tag {{
    background: #27272a; border-radius: 4px; padding: 2px 8px;
    font-size: 0.68rem; color: #a1a1aa; word-break: break-all;
  }}
  .dep-cnt {{ color: #71717a; margin-left: 3px; }}
  @media (max-width: 780px) {{
    .card-body {{ flex-direction: column; }}
    .col-flow {{ border-right: none; border-bottom: 1px solid #27272a; }}
    .grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Service Call Dashboard</h1>
  <p>wm-sandbox-1 · {total_svcs} services · {total_reqs:,} requests · {total_errors} errors · {ts_range} · source: logs-insights-results.json</p>
</header>
<div class="grid">
{''.join(cards)}
</div>
</body>
</html>"""

with open("service_dashboard.html", "w") as f:
    f.write(HTML)

print(f"Saved service_dashboard.html ({len(HTML) // 1024} KB)")
