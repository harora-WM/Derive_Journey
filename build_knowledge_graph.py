"""
Build a knowledge graph of API call sequences from Azure container logs.
Groups log lines by thread ID to reconstruct per-request call chains,
clusters similar journeys, then outputs an interactive HTML graph.
"""

import json
import re
from collections import defaultdict, Counter
from datetime import datetime

import networkx as nx
from pyvis.network import Network
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from sklearn.preprocessing import normalize
import numpy as np

# ── 1. Load logs ──────────────────────────────────────────────────────────────

with open("azure_nonsimple.txt") as f:
    data = json.load(f)

rows = data["tables"][0]["rows"]
# columns: TenantId, Computer, ContainerId, ContainerName, PodName, PodNamespace,
#          LogMessage, LogSource, TimeGenerated, KubernetesMetadata, LogLevel, ...

# ── 2. Parse each log line ─────────────────────────────────────────────────────

LOG_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"   # timestamp
    r" \[(http-nio-\d+-exec-\d+)\]"                    # thread
    r" (\w+)"                                           # level
    r"\s+[\w\.]+\s+-\s+"                               # logger
    r"(.+)"                                             # message body
)
METHOD_RE  = re.compile(r"Executing (\w+) method")
ERROR_RE   = re.compile(r"RestClientResponse Exception.*?:\s*(.{0,120})")
HTTP_RE    = re.compile(r"(\d{3}) (Bad Request|OK|Not Found|Unauthorized|Internal Server Error)", re.I)

parsed = []
for row in rows:
    msg = str(row[6])
    m = LOG_RE.match(msg)
    if not m:
        continue
    ts_str, thread, level, body = m.groups()
    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")

    method_m  = METHOD_RE.search(body)
    error_m   = ERROR_RE.search(body)
    http_m    = HTTP_RE.search(body)

    parsed.append({
        "ts":      ts,
        "thread":  thread,
        "level":   level,
        "body":    body,
        "method":  method_m.group(1) if method_m else None,
        "is_error": level.upper() == "ERROR",
        "error_snippet": error_m.group(1) if error_m else None,
        "http_status":   http_m.group(1)  if http_m  else None,
    })

print(f"Parsed {len(parsed):,} structured log lines from {len(rows):,} total rows")

# ── 3. Group by thread → reconstruct call sequences ───────────────────────────

thread_events = defaultdict(list)
for e in parsed:
    thread_events[e["thread"]].append(e)

# Sort each thread's events by timestamp
for t in thread_events:
    thread_events[t].sort(key=lambda x: x["ts"])

# Tomcat reuses threads across requests; split into per-request sessions
# using a 5-second idle gap as the session boundary.
SESSION_GAP_SEC = 5.0

sessions = []   # each session: list of events
for thread, events in thread_events.items():
    current = [events[0]]
    for ev in events[1:]:
        gap = (ev["ts"] - current[-1]["ts"]).total_seconds()
        if gap >= SESSION_GAP_SEC:
            sessions.append((thread, current))
            current = [ev]
        else:
            current.append(ev)
    sessions.append((thread, current))

# Build a sequence of method names per session
thread_sequences = {}  # session_id → list of method names
thread_meta      = {}  # session_id → metadata

for idx, (thread, events) in enumerate(sessions):
    methods = [e["method"] for e in events if e["method"]]
    if not methods:
        continue
    sid = f"{thread}#{idx}"
    thread_sequences[sid] = methods
    has_error = any(e["is_error"] for e in events)
    error_codes = [e["error_snippet"] for e in events if e["error_snippet"]]
    thread_meta[sid] = {
        "thread":      thread,
        "start":       events[0]["ts"],
        "end":         events[-1]["ts"],
        "duration_ms": (events[-1]["ts"] - events[0]["ts"]).total_seconds() * 1000,
        "has_error":   has_error,
        "error_codes": error_codes,
        "event_count": len(events),
    }

print(f"Reconstructed {len(thread_sequences):,} request-level sessions")

# Show a few examples
for t, seq in list(thread_sequences.items())[:3]:
    meta = thread_meta[t]
    print(f"  {t}: {seq}  ({meta['duration_ms']:.0f} ms, error={meta['has_error']})")

# ── 4. Cluster sequences by similarity ────────────────────────────────────────

threads  = list(thread_sequences.keys())
seq_docs = [" ".join(thread_sequences[t]) for t in threads]

# TF-IDF over method-name n-grams (bigrams capture ordering)
vect = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1)
X    = normalize(vect.fit_transform(seq_docs))

N_CLUSTERS = 8
km = KMeans(n_clusters=N_CLUSTERS, random_state=42, n_init=10)
labels = km.fit_predict(X)

# Name each cluster by its most common top-level method
cluster_threads = defaultdict(list)
for thread, label in zip(threads, labels):
    cluster_threads[label].append(thread)

CLUSTER_NAMES = {}
for cid, cthreads in cluster_threads.items():
    all_methods = []
    for t in cthreads:
        all_methods.extend(thread_sequences[t])
    top = Counter(all_methods).most_common(3)
    CLUSTER_NAMES[cid] = " → ".join(m for m, _ in top)

print("\nClusters:")
for cid, name in sorted(CLUSTER_NAMES.items()):
    ct = cluster_threads[cid]
    errors = sum(1 for t in ct if thread_meta[t]["has_error"])
    avg_ms = np.mean([thread_meta[t]["duration_ms"] for t in ct])
    print(f"  Cluster {cid} [{len(ct):3d} sessions, {errors} errors, avg {avg_ms:.0f}ms]: {name}")

# Map thread → cluster
thread_cluster = {t: l for t, l in zip(threads, labels)}

# ── 5. Build knowledge graph ───────────────────────────────────────────────────

G = nx.DiGraph()

# Node colours per cluster
CLUSTER_COLORS = [
    "#4e79a7","#f28e2b","#e15759","#76b7b2",
    "#59a14f","#edc948","#b07aa1","#ff9da7",
]

# --- Add method nodes ---
all_methods = set(m for seq in thread_sequences.values() for m in seq)
method_freq = Counter(m for seq in thread_sequences.values() for m in seq)

for method in all_methods:
    G.add_node(method, node_type="method", freq=method_freq[method])

# --- Add edges (method → next method within same thread) ---
edge_counts  = Counter()   # (src, dst) → count
edge_errors  = Counter()   # (src, dst) → error count
edge_clusters= defaultdict(Counter)  # (src, dst) → cluster → count

for thread, seq in thread_sequences.items():
    cid = thread_cluster[thread]
    has_error = thread_meta[thread]["has_error"]
    for i in range(len(seq) - 1):
        src, dst = seq[i], seq[i + 1]
        edge_counts[(src, dst)]  += 1
        edge_clusters[(src, dst)][cid] += 1
        if has_error:
            edge_errors[(src, dst)] += 1

for (src, dst), count in edge_counts.items():
    dom_cluster = edge_clusters[(src, dst)].most_common(1)[0][0]
    err_rate    = edge_errors[(src, dst)] / count
    G.add_edge(src, dst,
               weight=count,
               error_rate=round(err_rate, 3),
               dominant_cluster=dom_cluster)

print(f"\nGraph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

# ── 6. Compute runtime stats per method ───────────────────────────────────────

# Approximate method duration = time between consecutive "Executing X" lines
# on the same thread
method_durations = defaultdict(list)

for thread, events in thread_events.items():
    method_events = [e for e in events if e["method"]]
    for i in range(len(method_events) - 1):
        cur, nxt = method_events[i], method_events[i + 1]
        dur = (nxt["ts"] - cur["ts"]).total_seconds() * 1000
        if 0 < dur < 30_000:   # sanity: ignore gaps > 30s
            method_durations[cur["method"]].append(dur)

runtime_stats = {}
for method, durs in method_durations.items():
    arr = np.array(durs)
    runtime_stats[method] = {
        "count":   int(method_freq[method]),
        "avg_ms":  round(float(arr.mean()), 1),
        "p50_ms":  round(float(np.percentile(arr, 50)), 1),
        "p95_ms":  round(float(np.percentile(arr, 95)), 1),
        "max_ms":  round(float(arr.max()), 1),
    }

# ── 7. Render interactive HTML with pyvis ─────────────────────────────────────

net = Network(height="900px", width="100%", directed=True, bgcolor="#1a1a2e",
              font_color="white", notebook=False)
net.set_options("""
{
  "physics": {
    "barnesHut": { "gravitationalConstant": -8000, "springLength": 180 },
    "stabilization": { "iterations": 200 }
  },
  "edges": {
    "arrows": { "to": { "enabled": true, "scaleFactor": 0.6 } },
    "smooth": { "type": "curvedCW", "roundness": 0.2 }
  }
}
""")

MAX_EDGE_W = max(edge_counts.values())

for node in G.nodes():
    freq  = method_freq[node]
    size  = 12 + min(freq / 5, 30)
    stats = runtime_stats.get(node, {})
    title = (
        f"<b>{node}</b><br>"
        f"Calls: {freq}<br>"
        + (f"Avg: {stats['avg_ms']} ms | P95: {stats['p95_ms']} ms | Max: {stats['max_ms']} ms"
           if stats else "No timing data")
    )
    # colour by dominant cluster in edges
    out_edges = list(G.out_edges(node, data=True))
    if out_edges:
        dom = Counter(d["dominant_cluster"] for _, _, d in out_edges).most_common(1)[0][0]
        color = CLUSTER_COLORS[dom % len(CLUSTER_COLORS)]
    else:
        color = "#888888"

    net.add_node(node, label=node, size=size, color=color, title=title, font={"size": 11})

for src, dst, data in G.edges(data=True):
    w         = data["weight"]
    err_rate  = data["error_rate"]
    cid       = data["dominant_cluster"]
    thickness = 1 + (w / MAX_EDGE_W) * 8
    color     = "#e15759" if err_rate > 0.3 else CLUSTER_COLORS[cid % len(CLUSTER_COLORS)]
    title     = (f"{src} → {dst}<br>"
                 f"Count: {w}<br>"
                 f"Error rate: {err_rate:.1%}<br>"
                 f"Cluster: {CLUSTER_NAMES[cid]}")
    net.add_edge(src, dst, value=thickness, color=color, title=title)

net.save_graph("knowledge_graph.html")
print("Saved knowledge_graph.html")

# ── 8. Save structured JSON output ────────────────────────────────────────────

output = {
    "summary": {
        "total_log_lines": len(rows),
        "parsed_lines": len(parsed),
        "thread_sessions": len(thread_sequences),
        "unique_methods": len(all_methods),
        "clusters": N_CLUSTERS,
    },
    "clusters": {
        str(cid): {
            "name": CLUSTER_NAMES[cid],
            "session_count": len(cthreads),
            "error_count": sum(1 for t in cthreads if thread_meta[t]["has_error"]),
            "avg_duration_ms": round(np.mean([thread_meta[t]["duration_ms"] for t in cthreads]), 1),
            "example_sequences": [thread_sequences[t] for t in cthreads[:3]],
        }
        for cid, cthreads in cluster_threads.items()
    },
    "runtime_stats": runtime_stats,
    "top_edges": [
        {
            "from": src, "to": dst,
            "count": edge_counts[(src, dst)],
            "error_rate": round(edge_errors[(src, dst)] / edge_counts[(src, dst)], 3),
        }
        for (src, dst) in sorted(edge_counts, key=edge_counts.get, reverse=True)[:50]
    ],
}

with open("knowledge_graph.json", "w") as f:
    json.dump(output, f, indent=2, default=str)
print("Saved knowledge_graph.json")
