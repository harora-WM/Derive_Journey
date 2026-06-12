"""
Build a knowledge graph of service-to-service call flows from AWS ALB logs.
Reads logs-insights-results.json, groups requests by TID (trace ID) to reconstruct
cross-service call chains, then outputs service_graph.html and service_graph.json.
"""

import json
import re
from collections import defaultdict, Counter
from datetime import datetime

from pyvis.network import Network
import numpy as np

# ── 1. Load and parse ALB logs ────────────────────────────────────────────────

with open("logs-insights-results.json") as f:
    raw = json.load(f)

# ALB access log format (fields separated by spaces; request is double-quoted)
ALB_RE = re.compile(
    r'\S+'                           # type
    r' (\S+)'                        # request_timestamp
    r' \S+'                          # elb
    r' \S+'                          # client:port
    r' \S+'                          # target:port
    r' \S+'                          # request_processing_time
    r' ([0-9.-]+)'                   # target_processing_time
    r' \S+'                          # response_processing_time
    r' (\d+)'                        # elb_status_code
    r' \S+'                          # target_status_code
    r' \d+'                          # received_bytes
    r' \d+'                          # sent_bytes
    r' "(\w+) (https?://\S+) \S+"'  # method, url
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
        'ts':       ts,
        'tpt_ms':   tpt_f * 1000 if tpt_f >= 0 else None,
        'status':   status,
        'method':   method,
        'path':     f"{method} {norm_path(url)}",
        'service':  extract_service(url),
        'is_error': status.startswith('5'),
        'tid':      tid_m.group(1) if tid_m else None,
    })

print(f"Parsed {len(parsed):,} requests from {len(raw):,} records")

# ── 2. Per-service stats ──────────────────────────────────────────────────────

svc_calls = defaultdict(list)
for req in parsed:
    svc_calls[req['service']].append(req)

svc_stats = {}
for svc, reqs in svc_calls.items():
    tpts = [r['tpt_ms'] for r in reqs if r['tpt_ms'] is not None]
    arr  = np.array(tpts) if tpts else np.array([0.0])
    errs = sum(1 for r in reqs if r['is_error'])
    svc_stats[svc] = {
        'count':      len(reqs),
        'errors':     errs,
        'error_rate': round(errs / len(reqs), 3),
        'avg_ms':     round(float(arr.mean()), 1),
        'p95_ms':     round(float(np.percentile(arr, 95)), 1),
        'max_ms':     round(float(arr.max()), 1),
    }

print("\nServices:")
for svc, s in sorted(svc_stats.items(), key=lambda x: -x[1]['count']):
    print(f"  {svc}: {s['count']} calls, {s['errors']} errors, "
          f"avg {s['avg_ms']}ms p95 {s['p95_ms']}ms")

# ── 3. Build edges from TID-ordered sequences ──────────────────────────────────

tid_reqs = defaultdict(list)
for req in parsed:
    if req['tid']:
        tid_reqs[req['tid']].append(req)

for tid in tid_reqs:
    tid_reqs[tid].sort(key=lambda x: x['ts'])

edge_counts = Counter()   # (src, dst) → total calls
edge_errors = Counter()   # (src, dst) → calls where dst returned 5xx

for reqs in tid_reqs.values():
    for i in range(len(reqs) - 1):
        src, dst = reqs[i]['service'], reqs[i + 1]['service']
        if src == dst:
            continue  # skip self-loops; they add visual noise without insight
        edge_counts[(src, dst)] += 1
        if reqs[i + 1]['is_error']:
            edge_errors[(src, dst)] += 1

multi_tid = sum(1 for t in tid_reqs.values() if len(t) > 1)
print(f"\n{len(tid_reqs):,} unique TIDs, {multi_tid} multi-request chains")
print(f"Graph: {len(svc_stats)} service nodes, {len(edge_counts)} edges")

# ── 4. Render with pyvis ──────────────────────────────────────────────────────

COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
    "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
]

net = Network(height="900px", width="100%", directed=True, bgcolor="#1a1a2e",
              font_color="white", notebook=False)
net.set_options("""
{
  "physics": {
    "barnesHut": { "gravitationalConstant": -8000, "springLength": 220 },
    "stabilization": { "iterations": 300 }
  },
  "edges": {
    "arrows": { "to": { "enabled": true, "scaleFactor": 0.6 } },
    "smooth": { "type": "curvedCW", "roundness": 0.2 }
  }
}
""")

MAX_W = max(edge_counts.values()) if edge_counts else 1

for i, svc in enumerate(sorted(svc_stats)):
    s     = svc_stats[svc]
    size  = 14 + min(s['count'] / 15, 42)
    color = "#e15759" if s['error_rate'] > 0.2 else COLORS[i % len(COLORS)]
    label = (svc[:22] + '…') if len(svc) > 22 else svc
    title = (
        f"<b>{svc}</b><br>"
        f"Calls: {s['count']}<br>"
        f"Errors: {s['errors']} ({s['error_rate']:.1%})<br>"
        f"Avg: {s['avg_ms']} ms | P95: {s['p95_ms']} ms | Max: {s['max_ms']} ms"
    )
    net.add_node(svc, label=label, size=size, color=color, title=title, font={"size": 10})

for (src, dst), cnt in edge_counts.items():
    err_rate  = edge_errors[(src, dst)] / cnt
    thickness = 1 + (cnt / MAX_W) * 8
    color     = "#e15759" if err_rate > 0.3 else "#8888aa"
    title     = f"{src} → {dst}<br>Calls: {cnt}<br>Error rate: {err_rate:.1%}"
    net.add_edge(src, dst, value=thickness, color=color, title=title)

net.save_graph("service_graph.html")
print("Saved service_graph.html")

# ── 5. Save structured JSON ────────────────────────────────────────────────────

out = {
    "summary": {
        "total_requests":   len(parsed),
        "unique_services":  len(svc_stats),
        "unique_tids":      len(tid_reqs),
        "multi_tid_chains": multi_tid,
        "total_errors":     sum(s['errors'] for s in svc_stats.values()),
    },
    "services": {
        svc: {
            **s,
            "top_endpoints": [
                {"path": p, "count": c}
                for p, c in Counter(r['path'] for r in svc_calls[svc]).most_common(10)
            ],
        }
        for svc, s in svc_stats.items()
    },
    "top_edges": [
        {
            "from": src, "to": dst,
            "count": edge_counts[(src, dst)],
            "error_rate": round(edge_errors[(src, dst)] / edge_counts[(src, dst)], 3),
        }
        for (src, dst) in sorted(edge_counts, key=edge_counts.get, reverse=True)[:30]
    ],
}

with open("service_graph.json", "w") as f:
    json.dump(out, f, indent=2)
print("Saved service_graph.json")
