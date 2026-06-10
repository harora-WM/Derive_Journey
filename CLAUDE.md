# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Analyses Azure Kubernetes container logs from a customer portal (`usermgmt` service) to produce two interactive HTML visualisations:

1. **`knowledge_graph.html`** — a directed pyvis/vis.js graph of API method call sequences, coloured by cluster and sized by call frequency, with hover tooltips showing timing stats (avg/P95/max ms).
2. **`clusters.html`** — a dashboard of user-journey clusters, each showing a Sankey-style flow, top transitions table, example sequences, and error-rate metrics.

## Running the scripts

```bash
# Activate the venv first
source .venv/bin/activate

# Step 1 — build the knowledge graph (reads azure_nonsimple.txt, writes knowledge_graph.html + knowledge_graph.json)
python build_knowledge_graph.py

# Step 2 — build the cluster dashboard (reads azure_nonsimple.txt, writes clusters.html)
python build_cluster_view.py
```

Both scripts are standalone; there is no build system or test suite.

> **Portability note:** `knowledge_graph.html` references assets in `lib/` (bundled `vis-network.min.js`, `vis-network.css`, `bindings/utils.js`) via relative paths. Moving it out of the project root will break the graph rendering.

## Dependencies

Managed in `.venv` (Python 3.12). Key packages: `networkx`, `pyvis`, `scikit-learn` (KMeans + TF-IDF), `numpy`. To recreate the venv:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install networkx pyvis scikit-learn numpy
```

## Data pipeline architecture

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
> - The `<p>` summary line in the `clusters.html` header (`521 sessions · 8 clusters`) is hardcoded (line 391); it won't auto-update if you change `N_CLUSTERS` or switch to a different log file.
> - Method duration stats in the knowledge graph are approximate: they measure the gap between consecutive `Executing X` log lines on the same thread, not true method exit times.

## Key constants to tune

| Constant | File | Default | Effect |
|---|---|---|---|
| `SESSION_GAP_SEC` | `build_knowledge_graph.py:79` | `5.0` | Idle gap (seconds) used to split thread events into per-request sessions |
| `N_CLUSTERS` | `build_knowledge_graph.py:132` | `8` | Number of KMeans clusters |
| `ngram_range` | both scripts | `(1, 2)` | TF-IDF n-gram range over method names |
| `MAX_EDGE_W` / edge thickness | `build_knowledge_graph.py:242` | computed | Normalises edge widths in the graph |
| error-rate threshold | `build_knowledge_graph.py:269` | `0.3` | Edges with error rate above this are coloured red |

`N_CLUSTERS` and `SESSION_GAP_SEC` are duplicated between the two scripts and must be kept in sync manually.

## Input data context

`azure_nonsimple.txt` contains logs from the `usermgmt` service of a PNB MetLife customer portal. It covers the authentication journey (login OTP flow, session management, logout) but not post-login product navigation. See `derivable.txt` for a human-readable breakdown of what is and isn't extractable from this log file.
