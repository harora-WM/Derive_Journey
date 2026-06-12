"""
Generate a standalone HTML dashboard showing user journey clusters.
Reads azure_nonsimple.txt directly; no dependency on build_knowledge_graph.py.
"""

import json
from collections import Counter, defaultdict

# ── Load data ─────────────────────────────────────────────────────────────────

with open("azure_nonsimple.txt") as f:
    raw = json.load(f)

# Re-parse sessions to get per-cluster transition frequencies
import re
from datetime import datetime

LOG_RE     = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) \[(http-nio-\d+-exec-\d+)\] (\w+)\s+[\w\.]+\s+-\s+(.+)")
METHOD_RE  = re.compile(r"Executing (\w+) method")
SESSION_GAP = 5.0

rows = raw["tables"][0]["rows"]
thread_events = defaultdict(list)
for row in rows:
    msg = str(row[6])
    m = LOG_RE.match(msg)
    if not m:
        continue
    ts_str, thread, level, body = m.groups()
    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
    mm = METHOD_RE.search(body)
    thread_events[thread].append({"ts": ts, "thread": thread, "level": level,
                                   "body": body, "method": mm.group(1) if mm else None,
                                   "is_error": level.upper() == "ERROR"})

for t in thread_events:
    thread_events[t].sort(key=lambda x: x["ts"])

sessions = []
for thread, events in thread_events.items():
    cur = [events[0]]
    for ev in events[1:]:
        if (ev["ts"] - cur[-1]["ts"]).total_seconds() >= SESSION_GAP:
            sessions.append((thread, cur))
            cur = [ev]
        else:
            cur.append(ev)
    sessions.append((thread, cur))

session_list = []
for idx, (thread, events) in enumerate(sessions):
    methods = [e["method"] for e in events if e["method"]]
    if not methods:
        continue
    session_list.append({
        "sid":       f"{thread}#{idx}",
        "methods":   methods,
        "has_error": any(e["is_error"] for e in events),
        "duration":  (events[-1]["ts"] - events[0]["ts"]).total_seconds() * 1000,
    })

# Re-cluster (same approach as main script) to get cluster assignments
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from sklearn.preprocessing import normalize
import numpy as np

docs   = [" ".join(s["methods"]) for s in session_list]
vect   = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1)
X      = normalize(vect.fit_transform(docs))
labels = KMeans(n_clusters=8, random_state=42, n_init=10).fit_predict(X)

# Attach cluster id to each session
for s, lbl in zip(session_list, labels):
    s["cluster"] = int(lbl)

# Build per-cluster transition map
cluster_data = defaultdict(lambda: {
    "sessions": [], "transitions": Counter(), "errors": 0,
    "durations": [], "entry_methods": Counter()
})

for s in session_list:
    cid = s["cluster"]
    cd  = cluster_data[cid]
    cd["sessions"].append(s)
    cd["durations"].append(s["duration"])
    cd["entry_methods"][s["methods"][0]] += 1
    if s["has_error"]:
        cd["errors"] += 1
    for i in range(len(s["methods"]) - 1):
        cd["transitions"][(s["methods"][i], s["methods"][i+1])] += 1

# Cluster colour palette
COLORS = ["#4e79a7","#f28e2b","#e15759","#76b7b2",
          "#59a14f","#edc948","#b07aa1","#ff9da7"]

CLUSTER_LABELS = {
    cid: " + ".join(m for m, _ in cd["entry_methods"].most_common(2))
    for cid, cd in cluster_data.items()
}

# ── Build HTML ────────────────────────────────────────────────────────────────

def make_sankey(transitions, color, max_nodes=8):
    """Return a small inline SVG Sankey-ish flow for the top transitions."""
    if not transitions:
        return ""
    top = transitions.most_common(12)
    total = sum(c for _, c in top)

    # Collect ordered nodes (left = source, right = dest)
    src_nodes, dst_nodes = [], []
    for (s, d), _ in top:
        if s not in src_nodes: src_nodes.append(s)
        if d not in dst_nodes: dst_nodes.append(d)

    src_nodes = src_nodes[:max_nodes]
    dst_nodes = dst_nodes[:max_nodes]

    W, H = 520, max(max(len(src_nodes), len(dst_nodes)) * 36 + 20, 120)
    node_h = 22

    def sy(nodes, i): return 10 + i * 36 + node_h / 2

    lines = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-height:340px">']
    lines.append(f'<style>.nl{{font:10px monospace;fill:#ccc}} .nr{{font:10px monospace;fill:#ccc;text-anchor:end}}</style>')

    max_c = max(c for _, c in top)
    for (src, dst), cnt in top:
        if src not in src_nodes or dst not in dst_nodes:
            continue
        si = src_nodes.index(src)
        di = dst_nodes.index(dst)
        x1, y1 = 190, sy(src_nodes, si)
        x2, y2 = 330, sy(dst_nodes, di)
        sw = max(1, int(cnt / max_c * 6))
        alpha = 0.25 + 0.65 * (cnt / max_c)
        lines.append(
            f'<path d="M{x1},{y1} C{(x1+x2)//2},{y1} {(x1+x2)//2},{y2} {x2},{y2}" '
            f'stroke="{color}" stroke-width="{sw}" fill="none" stroke-opacity="{alpha:.2f}"/>'
        )

    for i, n in enumerate(src_nodes):
        y = sy(src_nodes, i)
        lines.append(f'<rect x="160" y="{y-node_h//2}" width="28" height="{node_h}" rx="3" fill="{color}" opacity="0.7"/>')
        lines.append(f'<text x="155" y="{y+4}" class="nr">{n[:22]}</text>')

    for i, n in enumerate(dst_nodes):
        y = sy(dst_nodes, i)
        lines.append(f'<rect x="332" y="{y-node_h//2}" width="28" height="{node_h}" rx="3" fill="{color}" opacity="0.55"/>')
        lines.append(f'<text x="365" y="{y+4}" class="nl">{n[:22]}</text>')

    lines.append("</svg>")
    return "\n".join(lines)


cluster_cards = []
for cid in sorted(cluster_data.keys()):
    cd    = cluster_data[cid]
    color = COLORS[cid % len(COLORS)]
    n     = len(cd["sessions"])
    errs  = cd["errors"]
    avg   = np.mean(cd["durations"]) if cd["durations"] else 0
    p95   = float(np.percentile(cd["durations"], 95)) if cd["durations"] else 0
    label = CLUSTER_LABELS[cid]
    err_pct = errs / n * 100 if n else 0

    # top transitions table
    top_t = cd["transitions"].most_common(6)
    trows = "".join(
        f"<tr><td>{s}</td><td>→</td><td>{d}</td><td class='num'>{c}</td></tr>"
        for (s, d), c in top_t
    )

    # example sequences
    examples = []
    for s in cd["sessions"][:2]:
        short = " → ".join(s["methods"][:6]) + ("…" if len(s["methods"]) > 6 else "")
        cls = " error-seq" if s["has_error"] else ""
        examples.append(f'<div class="seq{cls}">{short}</div>')
    example_html = "\n".join(examples)

    sankey = make_sankey(cd["transitions"], color)

    card = f"""
<div class="card" style="--accent:{color}">
  <div class="card-header">
    <span class="badge" style="background:{color}">{cid}</span>
    <h2>{label}</h2>
    <div class="metrics">
      <div class="metric"><span class="val">{n}</span><span class="lbl">sessions</span></div>
      <div class="metric {'metric-err' if err_pct > 10 else ''}">
        <span class="val">{err_pct:.0f}%</span><span class="lbl">error rate</span>
      </div>
      <div class="metric"><span class="val">{avg:.0f}ms</span><span class="lbl">avg</span></div>
      <div class="metric"><span class="val">{p95:.0f}ms</span><span class="lbl">p95</span></div>
    </div>
  </div>
  <div class="card-body">
    <div class="col-flow">
      <div class="section-title">Call flow</div>
      {sankey}
    </div>
    <div class="col-right">
      <div class="section-title">Top transitions</div>
      <table class="ttable"><tbody>{trows}</tbody></table>
      <div class="section-title" style="margin-top:14px">Example sequences</div>
      {example_html}
    </div>
  </div>
</div>"""
    cluster_cards.append(card)

cards_html = "\n".join(cluster_cards)

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>User Journey Clusters — Customer Portal</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0f1117;
    color: #d4d4d8;
    padding: 28px 20px 60px;
    min-height: 100vh;
  }}
  header {{
    text-align: center;
    margin-bottom: 36px;
  }}
  header h1 {{
    font-size: 1.7rem;
    font-weight: 700;
    color: #fff;
    letter-spacing: -0.5px;
  }}
  header p {{
    margin-top: 6px;
    color: #71717a;
    font-size: 0.88rem;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(680px, 1fr));
    gap: 22px;
    max-width: 1500px;
    margin: 0 auto;
  }}
  .card {{
    background: #18181b;
    border: 1px solid #27272a;
    border-left: 4px solid var(--accent);
    border-radius: 10px;
    overflow: hidden;
  }}
  .card-header {{
    padding: 16px 20px 12px;
    border-bottom: 1px solid #27272a;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .badge {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: 6px;
    font-size: 0.8rem;
    font-weight: 700;
    color: #fff;
    flex-shrink: 0;
  }}
  .card-header h2 {{
    font-size: 0.95rem;
    font-weight: 600;
    color: #e4e4e7;
    flex: 1;
    min-width: 0;
  }}
  .metrics {{
    display: flex;
    gap: 18px;
    margin-left: auto;
  }}
  .metric {{
    display: flex;
    flex-direction: column;
    align-items: flex-end;
  }}
  .metric .val {{
    font-size: 1.05rem;
    font-weight: 700;
    color: #fff;
    line-height: 1.1;
  }}
  .metric .lbl {{
    font-size: 0.68rem;
    color: #71717a;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .metric-err .val {{ color: #f87171; }}
  .card-body {{
    display: flex;
    gap: 0;
    padding: 0;
  }}
  .col-flow {{
    flex: 1.1;
    padding: 14px 16px;
    border-right: 1px solid #27272a;
    min-width: 0;
  }}
  .col-right {{
    flex: 0.9;
    padding: 14px 16px;
    min-width: 0;
  }}
  .section-title {{
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: #71717a;
    margin-bottom: 8px;
  }}
  .ttable {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
  }}
  .ttable td {{
    padding: 3px 4px;
    color: #a1a1aa;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 160px;
  }}
  .ttable td:nth-child(2) {{
    color: #52525b;
    padding: 0 2px;
    width: 16px;
  }}
  .ttable td.num {{
    color: var(--accent);
    font-weight: 600;
    text-align: right;
    max-width: 40px;
    width: 40px;
  }}
  .ttable tr:hover td {{ color: #e4e4e7; }}
  .seq {{
    font-size: 0.72rem;
    font-family: monospace;
    color: #a1a1aa;
    background: #09090b;
    border: 1px solid #27272a;
    border-radius: 5px;
    padding: 5px 8px;
    margin-bottom: 5px;
    line-height: 1.5;
    word-break: break-all;
  }}
  .error-seq {{
    border-color: #7f1d1d;
    color: #fca5a5;
  }}
  @media (max-width: 750px) {{
    .card-body {{ flex-direction: column; }}
    .col-flow {{ border-right: none; border-bottom: 1px solid #27272a; }}
    .metrics {{ gap: 10px; }}
    .grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<header>
  <h1>User Journey Clusters</h1>
  <p>Customer Portal · usermgmt service · 521 sessions · 8 clusters · source: azure_nonsimple.txt</p>
</header>
<div class="grid">
{cards_html}
</div>
</body>
</html>"""

with open("clusters.html", "w") as f:
    f.write(HTML)

print(f"Saved clusters.html ({len(HTML)//1024} KB)")

# ── Save structured JSON output ───────────────────────────────────────────────

total_sessions = len(session_list)
error_sessions = sum(1 for s in session_list if s["has_error"])

journeys = []
for cid in sorted(cluster_data.keys()):
    cd   = cluster_data[cid]
    n    = len(cd["sessions"])
    errs = cd["errors"]
    durs = cd["durations"]
    exit_methods = Counter(s["methods"][-1] for s in cd["sessions"])
    journeys.append({
        "journey_id":           cid,
        "label":                CLUSTER_LABELS[cid],
        "session_count":        n,
        "share_of_traffic_pct": round(n / total_sessions * 100, 1),
        "sessions_with_errors": errs,
        "error_rate_pct":       round(errs / n * 100, 1) if n else 0.0,
        "duration_ms": {
            "avg": round(float(np.mean(durs)), 1) if durs else 0.0,
            "p95": round(float(np.percentile(durs, 95)), 1) if durs else 0.0,
        },
        "entry_points": [{"method": m, "count": c} for m, c in cd["entry_methods"].most_common(5)],
        "exit_points":  [{"method": m, "count": c} for m, c in exit_methods.most_common(5)],
        "top_transitions": [
            {"from": s, "to": d, "count": c}
            for (s, d), c in cd["transitions"].most_common(12)
        ],
        "example_sequences": [s["methods"] for s in cd["sessions"][:3]],
    })

# Most common journeys first — journey_id still identifies the cluster
journeys.sort(key=lambda j: -j["session_count"])

output = {
    "description": (
        "User journeys reconstructed from usermgmt service logs (PNB MetLife customer "
        "portal). Log lines were grouped into per-request sessions by Tomcat thread ID "
        "(split at a 5s idle gap), then clustered into journey types by the similarity "
        "of their API method-call sequences (TF-IDF + KMeans)."
    ),
    "source_log_file": "azure_nonsimple.txt",
    "field_guide": {
        "journey_id":        "Cluster number; matches the badge/colour in clusters.html",
        "label":             "Two most common entry methods of the journey, joined with ' + '",
        "session_count":     "Number of request sessions assigned to this journey",
        "share_of_traffic_pct": "session_count as a percentage of all sessions",
        "sessions_with_errors": "Sessions containing at least one ERROR-level log line",
        "duration_ms":       "Session wall-clock duration (first to last log line)",
        "entry_points":      "Methods sessions start with, and how often",
        "exit_points":       "Methods sessions end with, and how often",
        "top_transitions":   "Most frequent consecutive method pairs within this journey",
        "example_sequences": "Full method-call sequences of real sessions in this journey",
    },
    "summary": {
        "total_sessions":        total_sessions,
        "journey_types":         len(journeys),
        "sessions_with_errors":  error_sessions,
        "overall_error_rate_pct": round(error_sessions / total_sessions * 100, 1),
        "avg_session_duration_ms": round(float(np.mean([s["duration"] for s in session_list])), 1),
    },
    "journeys": journeys,
}

with open("journeys.json", "w") as f:
    json.dump(output, f, indent=2)
print("Saved journeys.json")
