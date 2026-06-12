# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Two independent log-analysis pipelines, each producing a pair of interactive HTML outputs:

**Pipeline 1 — Azure / PNB MetLife (`azure_nonsimple.txt`)**
1. **`knowledge_graph.html`** — directed pyvis/vis.js graph of API method call sequences, coloured by cluster and sized by call frequency, with hover tooltips showing timing stats (avg/P95/max ms).
2. **`clusters.html`** — dashboard of user-journey clusters, each showing a Sankey-style flow, top transitions table, example sequences, and error-rate metrics.

**Pipeline 2 — AWS ALB / Watermelon (`logs-insights-results.json`)**
1. **`service_graph.html`** — directed pyvis/vis.js graph of service-to-service call flows, nodes sized by call volume and coloured red if error rate > 20%, edges weighted by call count and coloured red if error rate > 30%.
2. **`service_dashboard.html`** — per-service cards showing call volume, error rate, avg/P95 latency, top endpoints, an endpoint→HTTP-status Sankey, and TID co-dependency chips.

## Running the scripts

```bash
# Activate the venv first
source .venv/bin/activate

# Pipeline 1 — Azure logs
python build_knowledge_graph.py   # → knowledge_graph.html, knowledge_graph.json
python build_cluster_view.py      # → clusters.html

# Pipeline 2 — AWS ALB logs
python build_service_graph.py     # → service_graph.html, service_graph.json
python build_service_dashboard.py # → service_dashboard.html
```

All four scripts are standalone; there is no build system or test suite.

> **Portability note:** `knowledge_graph.html` and `service_graph.html` reference assets in `lib/` (bundled `vis-network.min.js`, `vis-network.css`, `bindings/utils.js`) via relative paths. Moving either file out of the project root will break graph rendering.

## Dependencies

Managed in `.venv` (Python 3.12). Key packages: `networkx`, `pyvis`, `scikit-learn` (KMeans + TF-IDF), `numpy`. To recreate the venv:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install networkx pyvis scikit-learn numpy
```

## Data pipeline architecture

### Pipeline 1 — Azure logs

`azure_nonsimple.txt` is a raw Azure Log Analytics JSON export (`data["tables"][0]["rows"]`; column 6 is the log message string).

**build_knowledge_graph.py** pipeline:
1. Regex-parse each log line → extract timestamp, Tomcat thread ID, log level, method name (`Executing X method`), HTTP status, error snippets.
2. Group events by thread; split threads into per-request *sessions* at a **5-second idle gap** (Tomcat reuses threads across requests).
3. TF-IDF (unigram + bigram) over method-name sequences → L2-normalised → **KMeans with N_CLUSTERS=8**.
4. Build a `networkx.DiGraph`: nodes = method names (sized by call frequency), edges = consecutive method pairs within a session (weighted by count, coloured red if error rate > 30%).
5. Render via `pyvis.Network` → `knowledge_graph.html`; also dump structured JSON → `knowledge_graph.json`.

**build_cluster_view.py** pipeline:
- Re-parses logs and re-runs the same TF-IDF + KMeans (same `random_state=42`) to get per-cluster transition counters.
- Generates inline SVG "Sankey-ish" flows (`make_sankey()`) for the top 12 transitions per cluster.
- Emits a single self-contained `clusters.html` with all CSS/SVG inlined.

> **Gotchas:**
> - The `<p>` summary line in the `clusters.html` header (`521 sessions · 8 clusters`) is hardcoded (`build_cluster_view.py:388`); it won't auto-update if you change the cluster count or switch to a different log file.
> - Method duration stats in the knowledge graph are approximate: they measure the gap between consecutive `Executing X` log lines on the same thread, not true method exit times.

## Key constants to tune

| Constant | File | Default | Effect |
|---|---|---|---|
| `SESSION_GAP_SEC` | `build_knowledge_graph.py:79` | `5.0` | Idle gap (seconds) used to split thread events into per-request sessions |
| `SESSION_GAP` | `build_cluster_view.py:20` | `5.0` | Same idle gap in the cluster script — **separate variable, must be kept in sync** |
| `N_CLUSTERS` | `build_knowledge_graph.py:132` | `8` | Number of KMeans clusters |
| `n_clusters=8` | `build_cluster_view.py:71` | `8` | Same cluster count, **inlined in the `KMeans()` call** — no named constant |
| `ngram_range` | both scripts | `(1, 2)` | TF-IDF n-gram range over method names |
| `MAX_EDGE_W` / edge thickness | `build_knowledge_graph.py:242` | computed | Normalises edge widths in the graph |
| error-rate threshold | `build_knowledge_graph.py:269` | `0.3` | Edges with error rate above this are coloured red |

Both scripts re-parse and re-cluster independently. When changing `SESSION_GAP`/`N_CLUSTERS`, update both files and the hardcoded header string at `build_cluster_view.py:388`.

### Pipeline 2 — AWS ALB logs

`logs-insights-results.json` is a CloudWatch Logs Insights export: a JSON array of `{"@timestamp", "@message"}` objects where `@message` is a standard AWS ALB access log line.

**build_service_graph.py** pipeline:
1. Regex-parse each ALB log line → extract request timestamp, target processing time, status code, HTTP method, URL, and TID (custom trace ID field at end of line).
2. Derive service name from URL path (`/services/<name>/...`; special-cased for `sts-auth`, `keycloak`, `selenium-grid`, `s3-storage`, `gateway`).
3. Normalise URL paths: strip query strings, replace Selenium session IDs and numeric IDs with `{session}` / `{id}`.
4. Group requests by TID, sort each group by request timestamp → build directed edges between consecutive services within each trace.
5. Render via `pyvis.Network` → `service_graph.html`; dump structured JSON → `service_graph.json`.

**build_service_dashboard.py** pipeline:
- Re-parses logs independently (no dependency on `build_service_graph.py`).
- Aggregates per-service: call count, error count, latency (avg/P95), top endpoints, per-endpoint status-class breakdown.
- Builds TID co-dependency map: which other services appear in the same trace as each service.
- Generates an inline SVG Sankey per service (`make_endpoint_sankey()`): left nodes = top endpoint paths, right nodes = HTTP status classes (2xx/4xx/5xx), coloured green/yellow/red.
- Emits a single self-contained `service_dashboard.html`.

> **Gotchas:**
> - TID coverage is sparse: only ~8.8% of traces (380/4,322) have more than one request, so the knowledge graph shows a subset of real service dependencies.
> - `@timestamp` in the JSON is a CloudWatch batch-collection time, not the actual request time. Use the timestamp embedded in `@message` (second field) for ordering.
> - The `wmeberrordashboardservice` and `wmerrorbudgetitsmservice` services have high error rates (22% and 45% respectively) as of the current log file — expected to change as those are fixed.

## Key constants to tune

### Pipeline 1

| Constant | File | Default | Effect |
|---|---|---|---|
| `SESSION_GAP_SEC` | `build_knowledge_graph.py:79` | `5.0` | Idle gap (seconds) used to split thread events into per-request sessions |
| `SESSION_GAP` | `build_cluster_view.py:20` | `5.0` | Same idle gap in the cluster script — **separate variable, must be kept in sync** |
| `N_CLUSTERS` | `build_knowledge_graph.py:132` | `8` | Number of KMeans clusters |
| `n_clusters=8` | `build_cluster_view.py:71` | `8` | Same cluster count, **inlined in the `KMeans()` call** — no named constant |
| `ngram_range` | both scripts | `(1, 2)` | TF-IDF n-gram range over method names |
| `MAX_EDGE_W` / edge thickness | `build_knowledge_graph.py:242` | computed | Normalises edge widths in the graph |
| error-rate threshold | `build_knowledge_graph.py:269` | `0.3` | Edges with error rate above this are coloured red |

### Pipeline 2

| Constant | File | Default | Effect |
|---|---|---|---|
| node error-rate threshold | `build_service_graph.py` | `0.2` | Nodes with error rate above this are coloured red |
| edge error-rate threshold | both scripts | `0.3` | Edges with error rate above this are coloured red |
| `max_ep` | `build_service_dashboard.py` | `7` | Max endpoint paths shown on the left side of each Sankey |

## Input data context

`azure_nonsimple.txt` contains logs from the `usermgmt` service of a PNB MetLife customer portal. It covers the authentication journey (login OTP flow, session management, logout) but not post-login product navigation. See `derivable.txt` for a human-readable breakdown of what is and isn't extractable from this log file.

`logs-insights-results.json` contains 1 hour of AWS ALB access logs (08:00–09:00) for `wm-sandbox-1.watermelon.us`. Traffic is primarily machine-to-machine: internal microservice calls and Selenium WebDriver automated test runs. There are no end-user identifiers in these logs.
