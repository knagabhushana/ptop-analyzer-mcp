# PTOPS → VictoriaMetrics + Optional Vector Store Integration Design

Version: 2025-08-25
Owner: PTOPS Integration (MCP Server) Prototype
Status: Draft (for review)

## 1. Goals & Scope
Integrate offline PTOPS log files and embedded help/metric documentation into:
1. VictoriaMetrics (time-series backend) for historical & analytical queries over system, container, fastpath, DB, and process-level metrics.
2. (Deferred/Optional) External vector store (e.g., ChromaDB) for semantic retrieval over metric definitions, field provenance, and enriched help content. Prototype uses static JSONL embeddings + in-process cosine search (no running vector DB) until scale or dynamic authoring features require one.
3. Expose these via a single Model Context Protocol (MCP) server with tools/APIs for:
   - Parsing and ingesting a PTOPS log file.
   - Querying time-series metrics (e.g., CPU per core %, top process usage, disk latency trends).
   - Semantic help/intent resolution (e.g., "what does dbwr bucket 19 mean", "interpret high queue_len on DISK").

Out of scope (initial phase):
- Real-time streaming ingestion (focus is batch/offline logs).
- Automated feature-gated plugin discovery outside what's in provided log and documentation.
- Complex alerting or anomaly detection (can be layered later).

## 2. Key Artifacts
- PTOPS log(s) (e.g., ptop-20250628_1351.log) with repeated sampling epochs.
- Documentation file: `ptop/ptop_plugin_metrics_doc.md` (provenance + metric semantics + metric_kind + parsing guidance + production invocation profiles).
- Generated schema model inside code.
- MCP Server (Python) exposing tool methods.
 - Static embeddings artifact: `docs_embeddings.jsonl` (prebuilt vectors; see §16).

## 3. Parsing Model
### 3.1 Line Types
Each non-empty log line begins with a primary prefix token (or compound like `NET ifstateth0`). Epoch boundaries: blank line + TIME line anchor.

TIME line structure (example):
`TIME <run_seconds> <epoch_unix> <date> <wallclock>` — used for timestamp anchoring.

### 3.2 Identity & Namespace Strategy
All exported metrics will be namespaced by their PTOPS record prefix. Requirement: prefix all metrics with normalized lowercase prefix + underscore. Examples:
- CPU → `cpu_utilization`, `cpu_user_percent`
- DISK → `disk_reads_per_sec`
- TOP → `top_cpu_percent`
- SMAPS → `smaps_rss_kb`
- DBWR → `dbwr_bucket_count`

For per-entity metrics (per-CPU, per-disk, per-interface, per-process) we attach labels to avoid exploding metric name permutations.

### 3.3 Metric Kinds Handling
Mapping from documentation `metric_kind` to ingestion behavior:
- label: becomes a label (string dimension).
- gauge: numeric sample (value as-is).
- counter: treat as monotonic counter; if PTOPS already emits a delta (rare), map to counter if reconstructable else gauge.
- delta: store as counter with `_total` semantic OR as gauge; decision: treat delta as counter increase for time-series (VictoriaMetrics will sum windows logically). Name suffix `_total` optional.
- rate: store directly as gauge (point-in-time rate). Also optionally reconstruct raw increment if snapshot alignment exists (phase 2).
- histogram_bucket / histogram_avg: represent DBWR/DBWA/DBRD as exemplar labels: `bucket=<idx>` with two metrics: `*_count` (counter) and `*_avg_latency_seconds` (gauge).
- text / opaque: skip numeric ingestion; optionally store as annotation into ChromaDB only.

### 3.4 Field Extraction Strategy
Tokenizer splits on whitespace; plugin-specific finite state parse functions:
- CPU: anchor tokens `u`, `id/io`, `u/s/n`, `irq h/s`.
- DISK: pattern segments: `DISK <idx> <dev> rkxt <...4> wkxt <...4> sqb <...3>`.
- NET: two forms (rate vs ifstat). Rate line suppressed if zero traffic.
  * Normalization: Parser now normalizes rate metrics to rx_/tx_ semantic names (e.g., net_rx_packets_per_sec) instead of earlier rk_/tk_ prefixes. A backward compatibility toggle (`include_legacy_net_names=True` in `PTOPSParser`) emits both normalized and legacy names in Phase 1. Future code may default this to False after downstream consumers migrate.
  * Ifstat snapshot counters exposed with explicit dropped packet names: `net_rx_dropped_packets_total`, `net_tx_dropped_packets_total` (previous interim implementation used `rx_err_total` / `tx_err_total`; spec takes precedence and parser updated accordingly). Underlying line column order: iface rx_pkts rx_bytes tx_pkts tx_bytes rx_drops tx_drops.
- TOP: variable tail with optional container triplet; final token (possibly with spaces originally) is exec name (already sanitized by plugin without parentheses in code). Exec name captured as label.
- SMAPS: structured tokens until `c`, all remaining tokens joined for command.
- DBWR/DBWA/DBRD: repeating triplets until exhaustion.
- DBMPOOL: alternating token/value pairs.
- FP*, DOT/DOH/TCP_DCA/IMCDR & other fastpath: alternating key/value pairs (after fixed leading identity tokens); counts & gauges distinguished downstream.
- Opaque (DB, VADP, SNIC): store raw line string in a side channel (for embedding & context) without metric emission.

### 3.5 Timestamp Assignment
Each line in an epoch inherits the TIME line timestamp fields:
- Primary ingestion timestamp: Unix epoch (second resolution) + optional millisecond interpolation if delta cycle <1s (not required here; cycles are >=1s typically).
- Additional labels: `ptop_run_seconds` for relative time since start.

### 3.6 Derived Labels
Standard labels attached to every metric sample (Phase 1 implementation CURRENT):
- `record_type` (e.g., CPU, DISK, TOP)
- `host` (human-readable hostname parsed or derived)
- `host_id` (stable host identifier / appliance id from IDENT line)
- `source` = `ptops`
- `metric_category` (see mapping table below; now emitted by parser via `_metric_category` helper)
- `ptop_version` (from IDENT first token after `IDENT`).

Metric Category Mapping (implemented):

| Prefix / Family                    | metric_category |
|------------------------------------|-----------------|
| CPU                                | cpu             |
| MEM                                | memory          |
| DISK                               | disk            |
| NET_RATE / NET_IF (all NET forms)  | network         |
| TOP                                | process         |
| SMAPS                              | memory_map      |
| DBWR / DBWA / DBRD                 | db_histogram    |
| DBMPOOL                            | db              |
| FPPORTS / FPMBUF / FPC             | fastpath        |
| DOT_STAT / DOH_STAT / TCP_DCA_STAT | fastpath        |
| (future) NODE                      | system          |
| (future) UDP / SNMP                | network         |
| (future) VADP_* / SNIC_* / IMC*    | fastpath (TBD finer split) |
| (fallback) unknown prefix          | other           |

Notes:
1. DOT/DOH and TCP_DCA_STAT are grouped under `fastpath` initially; can be refined (e.g., `network_dns`) later without breaking stored time-series by adding parallel labels.
2. Histogram families share `db_histogram` enabling generic bucket visualizations keyed only on category.
3. Future prefixes simply require extending `_metric_category` (single place in code) plus optional doc update.
4. ChromaDB embedding metadata will also include `metric_category` so hybrid (semantic + structured) filtering is possible.

Entity-specific labels:
- CPU: `cpu` (cpu / cpuN).
- DISK: `device`.
- NET: `interface`.
- TOP: `pid`, `ppid`, `exec`, and optional `container_id`, `container_name`.
- SMAPS: `pid`, `command`.
- DBWR/DBWA/DBRD: `bucket`.
- DBMPOOL: global (no extra label).
- FP groups: group-specific labels (e.g., `port` for FPPORTS lines if present).
- IMCDR: line subtype (`imcdr_kind`), peer id, or node id list broken into repeated samples (phase 2) or single record with aggregated labels (phase 1 keep raw line).

## 4. VictoriaMetrics Mapping
### 4.1 Prometheus Metric Naming Conventions
- Lowercase, underscores; prefix with record type: `cpu_user_percent`, `disk_avg_queue_len`, `top_cpu_percent`.
- Percentages: retain `_percent` suffix.
- Rates: `_per_sec` or domain-specific (`_pps`, `_kb_per_sec`).
- Counters: `_total` suffix (e.g., `net_rx_packets_total` if we later reconstruct cumulative). For now, we ingest already-present cumulative snapshot counters from NET ifstat lines with `_total`.

### 4.2 Sample Object Internal Form
```
MetricSample {
  name: str,
  value: float,
  ts_ms: int,         # epoch milliseconds (single canonical unit)
  labels: Dict[str,str]
}
```

### 4.3 Conversion to VictoriaMetrics Import Format
VictoriaMetrics /api/v1/import expects newline-delimited JSON or Prometheus remote-write; choose simplest: line protocol via `/api/v1/import` JSON lines.
Example JSON line:
```
{"metric":"cpu_user_percent","values":[12.3],"timestamps":[1724625305000],"labels":{"record_type":"CPU","cpu":"cpu0","metric_category":"cpu","source":"ptops"}}
```
Batch lines grouped per metric/time where `values` length matches timestamps array (we can group per epoch or flush streaming). Simpler: one value per JSON object.

### 4.4 Handling Deltas & Rates
- Rate fields (already computed) stored directly as gauges; no counter reconstruction Phase 1.
- DBWR/DBWA/DBRD per-bucket counts ingested as monotonic counters: `dbwr_bucket_count_total{bucket="17"}`.
- Per-bucket average latencies as gauges: `dbwr_bucket_avg_latency_seconds{bucket="17"}`.
- Other averages / utilizations remain gauges.

### 4.5 High Cardinality Controls
- Skip ingesting `TOP` beyond configurable limit (by default whatever PTOPS logged; optional filter to top N already inherent in PTOPS output via -T flag).
- SMAPS: Potentially large; provide flag to include/exclude; default include only rss_kb, swap_kb to reduce cardinality (others optional set).
- IMCDR node peer lists: treat as separate synthetic samples only if count below threshold; else store as annotation event metric (string) in Chroma only.

### 4.6 Failure & Idempotency
- Parser runs offline; maintain a hash of input file path + size + mtime to prevent duplicate ingestion (state file).
- Use retry with exponential backoff (3 tries) on HTTP 5xx.
- Log ingestion errors per metric (aggregate counts at end).

## 5. Documentation Embedding & Retrieval (Static First)
### 5.1 Artifact & Source
Primary source: `ptop_plugin_metrics_doc.md` (field rows + plugin summaries). Builder script (`scripts/build_docs_embeddings.py`) emits `docs_embeddings.jsonl` containing:
```
{ id, level (L1/L2/L4), text, metadata{ record_type, metric_name?, metric_kind, metric_category, version, legacy_aliases[], provenance{...}, provenance_hash }, embedding:[float...] }
```
Levels: L1 per field, L2 per plugin summary, L4 selected concepts. L3 (alias clusters) deferred.

### 5.2 Runtime Retrieval (Current)
Runtime loads JSONL into memory (`embeddings_store.py`) and performs linear cosine similarity for semantic search plus simple keyword token scoring. Indices: metric_name → doc, alias → doc(s), record_type → doc list, concepts list.

### 5.3 Deferred Vector Store (Optional)
When corpus or dynamic updates demand, introduce pluggable vector backend (ChromaDB / pgvector / Milvus). Abstraction: implement interface `{search(query_emb, filter_meta), upsert(docs)}`; current in-process store already conforms logically (subset). Environment variable `VECTOR_DB_URL` (future) toggles external backend; if unset, stay in-memory.

### 5.4 Embedding Model
Offline: sentence-transformers `all-MiniLM-L6-v2` (384-dim) if installed; fallback zero vectors for deterministic shape. No runtime model inference; queries may supply their own embedding or fallback cheap hash embedding.

### 5.5 Query Examples
Same semantics as earlier design (vector + metadata). Example intents resolved entirely via static store:
- "top CPU processes" → L2 TOP plugin summary + L1 top_cpu_percent.
- "disk queue buildup meaning" → DISK queue length field doc.
- "imcdr frsxx stats" → L1 IMCDR related metrics (once documented) + concept docs.

### 5.6 Reintroduction Checklist for External Vector Store
1. Add dependency & client adapter (e.g., `vector_backends/chroma.py`).
2. On startup, if VECTOR_DB_URL set and collection empty: bulk import JSONL docs.
3. Replace `semantic_search` implementation to delegate to backend; keep keyword & alias logic local.
4. Preserve JSONL as canonical build artifact; do not rely on DB as source of truth (rebuild reproducibility).
5. Extend design §16 (already detailed) with any ANN-specific parameters (index type, efSearch, etc.).

Rollback path: unset VECTOR_DB_URL → automatically falls back to in-memory mode; no data migration needed because JSONL remains authoritative.

## 6. PTOPSParser Class Design
### 6.1 Responsibilities
- Stream parse PTOPS log file into structured records.
- Maintain last snapshot state needed for delta verification when reconstructing counters (future).
- Provide export adapters: `to_victoriametrics()` and `to_chromadb()`.

### 6.2 Interface
```
class PTOPSParser:
    def __init__(self, doc_path: str, log_path: str, host: str | None=None):
        # load documentation for schema & metric_kind

    def parse(self) -> Iterable[ParsedRecord]:
        # generator over records with fields, ts, prefix

    def iter_metric_samples(self) -> Iterable[MetricSample]:
        # apply naming + label logic

    def to_victoriametrics(self, vm_write_url: str, batch_size: int=500):
        # push MetricSample objects in batches; returns summary stats

    def to_chromadb(self, chroma_client, collection_name: str="ptops_metrics_docs", rebuild: bool=False):
        # build embeddings for docs and optionally structured field docs
```

### 6.3 Internal Data Structures
`ParsedRecord` (as documented) plus extended: `epoch_ts`, `run_seconds`, `raw_line`.
Schema dictionary: keyed by prefix; each field entry: `{name, metric_kind, units, is_label}`.
MetricCategory mapping table (static dict).

### 6.4 Algorithmic Notes
- Single pass: accumulate current TIME context; for each data line parse immediately.
- Memory efficiency: yield samples instead of storing full dataset (except minimal state for deltas/histograms if needed later).
- Complexity O(L) for L lines.

### 6.5 Error Handling
- If a line fails parse under expected prefix rule, log & skip; count errors per prefix.
- Unknown prefix: if starts with known opaque prefixes, store to `opaque_records` list for optional embedding ingestion.

### 6.6 Configuration Flags
```
PTOPSParserOptions {
  include_smaps_full: bool (default False)
  include_top: bool (default True)
  include_opaque: bool (default False for metrics; always for embeddings)
  max_top_execs: int (fallback if PTOPS not limited)
}
```
Parser constructor accepts options.

## 7. MCP Server Design
### 7.1 Tool Endpoints
- `support.load_bundle { path, force?, categories:[...] }`: register & ingest a support bundle (sptid auto‑deduced for informational label only).
- `query_metrics {query, match_labels, range}`: wrapper over VictoriaMetrics query API `/api/v1/query_range`.
- `search_docs {text, k}`: vector search in Chroma collection.
- `explain_metric {metric_name}`: retrieve metadata + doc snippet.

Additional (graph-focused) endpoints to support user-driven visualization:
- `graph_timeseries {metrics:[...], start, end, step, functions?, align_mode?}`: fetch one or more metric time-series aligned on a common step; returns canonical GraphSpec JSON.
- `graph_compare {primary_metric, comparison_metrics:[...], start, end, step, normalize?:bool}`: overlay normalized or raw series for comparative analysis between two or three metrics (explicit user request scenario).
- `graph_pointset {metrics:[...], timestamps:[t1,t2(,t3)], aggregate?}`: fetch exact (or nearest) data points for 2–3 timestamps to build quick delta / ratio / slope text or scatter plot.
- `graph_scatter {x_metric, y_metric, start, end, step, x_agg?, y_agg?}`: produce paired point arrays for correlation (e.g., disk_avg_queue_len vs disk_busy_percent).
- `graph_histogram_dbwr {prefix (dbwr|dbwa|dbrd), start, end, bucket_filter?}`: assemble per-bucket evolution to enable heatmap or stacked area reconstruction.
- `graph_topk {metric, start, end, step, k, by_label}`: compute top-K label values at each step (e.g., top processes by CPU) using server-side PromQL-like functions where possible; if not available fall back to client aggregation.

### 7.2 Session Flow (Bundle-ID Only Model)
1. Client calls `support.load_bundle` with a bundle path (or directory). The server derives a deterministic bundle_id from path hash and an informational sptid (historical field formerly named tenant_id) if detectable (pattern NIOSSSPT-XXXX).
2. Metrics are parsed & ingested once per bundle_id; re-calls with same path reuse metadata unless force:true.
3. Exactly one bundle is ACTIVE globally; others remain passive. `active_context` returns only bundle_id (plus sptid, path, time_range).
4. Metrics & queries auto-scope by bundle_id label only.
5. Unloading the active bundle promotes a random passive bundle to active (if any) to keep workflow fluid.

### 7.3 Minimal Dependencies
### 7.4 Graph / Visualization Abstraction
Goal: Provide a stable, renderer-agnostic JSON structure (GraphSpec) so any UI or CLI can generate charts (line, area, scatter, bar, top-K table) without coupling to VictoriaMetrics' raw response format.

GraphSpec schema (returned by `graph_*` endpoints):
```
GraphSpec {
  title: str,
  query_window: { start: epoch_ms, end: epoch_ms, step: int_ms },
  series: [ {
      name: str,                 # metric name or synthetic label (e.g., "cpu_utilization")
      labels: {k:v},             # label set (excluding metric name)
      type: "line"|"area"|"scatter"|"bar"|"heatmap",
      points: [ [ts_ms, value], ... ],
      annotations?: [ {ts_ms, text} ],
      meta?: { source_query: str, units: str, metric_kind: str }
  } ],
  transforms?: [ { op: str, params: {...}, target_series: [index_or_name,...]} ],
  notes?: str
}
```

Alignment Modes:
- `align_mode`: `fill_forward`, `drop_missing`, `zero_fill`. Default `drop_missing` (only aligned timestamps present across all).

Basic Transform Operations (applied client-side after raw fetch):
- `rate_to_delta` (if user wants approximate raw delta between two points).
- `normalize` (divide each series by its max over window → unitless comparative view).
- `derivative` (slope for highlighting trend steepness between sparse points).
- `ratio` (primary / secondary series producing synthetic series). Example: CPU user / total utilization.

Two/Three Point Comparative Graphs:
- Use `graph_pointset` to return minimal data: raw values, deltas, percent changes, average rate between first & last.
```
PointSetResult {
  metrics: [ name... ],
  timestamps: [t1, t2, (t3?)],
  values: { metric: [v1, v2, (v3?)] },
  deltas: { metric: [v2-v1, (v3-v2?)] },
  percent_change: { metric: [ (v2-v1)/v1*100, ... ] }
}
```
This allows quick CLI summaries and feeding into small scatter or slope graphs.

Scatter / Correlation:
- `graph_scatter` fetches aligned x_metric and y_metric; constructs `type:"scatter"` series with `points` each `[x_value, y_value, ts_ms?]` (time optional meta). If time retained, consumer can animate correlation over time.

Histogram (DBWR/DBWA/DBRD) Visualization:
- `graph_histogram_dbwr`: returns per bucket a `series` with `type:"area"` or `"heatmap"` readiness. Optionally reduce buckets via `bucket_filter` (list / range). Provide total operations per interval as synthetic series `dbwr_ops_total` for overlay.

Top-K Dynamics:
- `graph_topk` leverages VictoriaMetrics query pattern: `topk(k, <metric>{<label filters>})` per step OR fetch raw and compute. Returns stacked or separate series. Provide normalization toggle to show share-of-total at each timestamp.

Label Selection & Joins:
- Provide simple expression mini-language for `graph_timeseries` metrics array entries: `metric_name{label=value,...} | function` where function ∈ {`rate`, `avg_over_time`, `sum_by(<lbl>)`, `max`, `min`} mapping directly to VictoriaMetrics/PromQL. Parser escapes braces to build query string.

Downsampling:
- If `(end-start)/step` > 5000 points, auto-increase `step` to stay within performance ceiling or apply VictoriaMetrics `downsample` parameter if available.

Error Handling:
- Partial failure (one series fails): return successful series plus `notes` containing list of failures with reasons (HTTP status, query parse error).
- Empty dataset: series array empty, notes explain potential causes (metric not ingested, label mismatch).

Security / Safety:
- Restrict functions to allowlist. Reject queries with regex label match if disabled by configuration.

Caching:
- In-memory LRU for recent GraphSpec keyed by (metric queries tuple + window + step + transforms hash) to reduce backend load for repeated UI refreshes.

### 7.5 Support Bundle Lifecycle (Bundle-ID Only Global Active Model)
Simplified invariant: at any point in time **zero or one** bundle is globally ACTIVE. Loading (or reusing) a bundle sets it active and leaves any previously loaded bundles passive. No tenant partitioning is enforced; an informational `sptid` (extracted support ticket id like NIOSSPT-1234) is stored with each bundle for display & filtering only.

Core assumptions:
* `load_bundle` extracts (if needed) and ingests selected PTOPS logs synchronously for the specified path.
* A deterministic `bundle_id` (hash + random suffix) uniquely identifies the ingested snapshot.
* Passive bundles remain queryable if promoted (future explicit switch tool) or when the active bundle is unloaded (random passive promotion).
* Status polling is trivial (`ingest_status` always idle) because ingestion is synchronous in this phase.

#### 7.5.1 New / Revised Endpoints

`load_bundle { path, force?, max_files?, categories:[...] }`
- Behavior: If `ingest` omitted or true → start synchronous ingestion pipeline (see 7.5.2). Returns immediately **after completion**; large bundles may later use async mode (`async=true`) producing a `task_id` for polling (future enhancement).
- Response (summary):
```
{
  bundle_id: string,
  sptid: string,          # informational only (derived, may be synthetic anon-*)
  host: string,
  logs_processed: int,
  metrics_ingested: int,
  time_range: { start:int, end:int },
  replaced_previous: bool,
  reused?: bool,              // true if identical bundle already ingested & reused (idempotent skip)
  warnings?: [string]
}
```

`active_context()`
```
Response {
  active?: {
    bundle_id: string,
    host: string,
    loaded_at: int,
    logs_processed: int,
    metrics_ingested: int,
    time_range: { start:int, end:int }
  },
  total_loaded: int        # always 0 or 1 in simplified model
}
```

`ingest_status()`
- Poll current ingestion progress if a long-running load is in-flight (future async mode) OR return final summary if already finished.
```
Response {
  state: "idle"|"running"|"finalizing"|"error",
  bundle_id?: string,
  host?: string,
  progress?: {
    current_log: string,
    logs_completed: int,
    logs_total: int,
    metrics_ingested: int,
    elapsed_ms: int,
    eta_ms?: int
  },
  error?: { message: string }
}
```

`unload_bundle { bundle_id }`
```
Response { bundle_id?: string, unloaded: bool, purged: bool }
```

`list_bundles` (returns all bundles with active flag)
```
Response { bundles: [ { bundle_id, host, loaded_at } ] }
```
In single-active model this returns 0 or 1 entry (included for forward compatibility with multi-bundle mode).

### 7.5A Multi-Bundle Loaded / Single Global Active (Implemented)
Multiple bundles can coexist (passive) while exactly one is global active. Tenant scoping removed; sptid retained only as metadata.

Core Properties:
1. Multiple support bundle tarballs (sb-*.tar.gz) can be loaded concurrently; each becomes passive unless it is the newest load (auto-activated).
2. Exactly one active bundle tracked via `global_active` table.
3. Each bundle has a unique `bundle_id` and persistent `bundle_hash` for idempotency.
4. Extraction isolation: each loaded bundle is extracted under `/tmp/<sptid>/<bundle_hash_prefix>`.
5. Unloading semantics:
   - Unload specific bundle (active or passive): remove that bundle's extracted directory and delete its row from `bundles`. If it was active, `active_context` row is cleared.
  - Purge all (`purge_all=true`): delete all bundle rows, clear `global_active`, and remove all `/tmp/<sptid>` trees.
6. Listing: `list_bundles` tool returns all loaded bundles (most recent first) with `active` boolean.
7. Idempotent reload: Loading same path/hash with `force=false` returns existing row (`reused=true`) and re-activates it; `force=true` deletes and re-ingests.

Bundle Hash (current implementation):
```
if path is file:
  meta = f"FILE:{basename}:{size}:{mtime}" + first 1MB bytes
if path is directory:
  meta = f"DIR:{basename}:{mtime}" + sorted up to first 200 entry names
hash = sha256(meta_bytes)
```
Rationale: Low cost; directory mode accommodates future pre-extracted development scenarios while Phase 1 production path uses tar files.

Support Ticket ID (sptid) Discovery:
Heuristic extracts first `NIOSSPT-XXXX` (case-insensitive) from path components, filename, or tar members; fallback synthetic `anon-<hash>` retained for anonymity when absent.

Extraction & Log Scan Flow:
1. Determine destination: `/tmp/<sptid>/<hash_prefix>`; create parent dir if absent.
2. If extraction target exists and (force or not reused) remove then extract.
3. After extraction, scan `var/log` for `ptop-*.log` counting matches (stored as `logs_processed`). Parsing/metric ingestion still placeholder (Phase 1 docs + states only); future ingestion will also store metrics counts and time range after parse.

Active Switching Logic Simplified Pseudocode:
```
existing = bundles.find_by_hash(sptid, bundle_hash)
if existing and not force:
    reused=True
  set_global_active(existing.bundle_id)
    return existing_summary(reused=True)
else:
    if existing and force: delete row
    insert new bundle row
  set_global_active(new_bundle_id)
```

Unload Variants:
```
unload_bundle { bundle_id | purge_all? }

Case purge_all: delete all bundles + clear global_active; rm -rf /tmp/* (scoped to sptid dirs)
Case bundle_id: delete that bundle row + extracted dir; if active clear global_active and promote random passive.
Case neither (not exposed as tool now).
```

Response Extensions:
`UnloadResponse` now includes: `purged` (directory removal success), `active_cleared`, `total_removed` (for purge_all), `all_purged` boolean.

Listing Endpoint:
`list_bundles` tool returns array sorted by `created_at DESC` including `active`.

Test Coverage Added (§10 augment):
1. (Legacy doc) Auto-select by ticket id removed; explicit path now required.
2. Load second bundle; confirm active switches.
3. Re-load first path returns reused true & becomes active.
4. Listing shows both bundles with one active.
5. Unload passive bundle → list shrinks; active unchanged.
6. Unload active bundle → random passive promoted (or none active if no others).
7. Purge all bundles removes rows, directories, clears active.

Open Future Enhancements:
* Explicit switch endpoint: `switch_active { bundle_id }` (future) to avoid re-load.
* Asynchronous ingestion: background parse tasks per bundle after extraction.
* Cross-bundle diff tooling (compare metrics between two loaded bundles).
* Disk quota enforcement: refuse load if `/tmp` usage for tenant exceeds threshold; optional LRU eviction of oldest passive bundle.

Failure Modes & Safeguards:
* Extraction failure raises 400 with `failed to extract bundle` detail; bundle row still created earlier for transparency—subsequent purge/unload can clean it up.
* If user attempts unload with unknown bundle_id/path returns 404 to signal stale client state.
* Purge-all always clears `active_context` even if extraction removal partially fails (purged flag reflects FS outcome).

Security & Isolation Note:
Prefixing extraction by sptid and bundle hash prevents path traversal collisions and simplifies garbage collection by walking `/tmp/<sptid>`.

Documentation / Code Mapping:
* `server.py` endpoints: `/support/load_bundle`, `/support/bundles`, `/support/unload_bundle`, `/support/active_context`, `/support/ingest_status`.
* `support_store.py` additions: `delete_all_bundles_for_tenant` for purge_all logic.
* Hashing function updated to support directories plus file sampling.

This section supersedes earlier simplified single-active assumptions while preserving client ergonomics (still one call to load & switch). The design remains forward-compatible with an explicit switch endpoint and async parsing pipeline.

#### 7.5.2 Ingestion Pipeline (Inside load_bundle)
1. Resolve target bundle:
  - If `path` provided use it.
  - (Removed) Previous auto-discover by tenant directory; now caller supplies explicit path.
2. Discover & extract (if tarball) → identify PTOPS log files.
3. Manifest pass: read first & last TIME lines per log to compute window; order oldest→newest.
4. Stream parse & batch ingest (default batch_size=500 or 256KB). Abort if parse error rate >5% (configurable `PARSE_ERROR_MAX_PCT`).
5. Persist state: (bundle_id, per-log hash, metrics counts) unless duplicate and not `force`.
6. Mark active context.
7. (Docs Embedding – Static) Skip doc embedding; vectors prebuilt (§16). If provenance hash mismatch and `VERIFY_DOCS_HASH=true`, append warning `docs_provenance_mismatch`.

##### 7.5.2a PTOPS Log Discovery & Ordering (Updated 2025-08-27)
Refinement (superseding earlier TIME-line-only ordering) aligning with real file naming pattern:

Example directory listing (chronological by embedded date segment, independent of FS mtime ordering):
```
ptop-20250626_1349.log
ptop-20250627_1350.log
ptop-20250628_1351.log
ptop-20250629_1352.log
```
Here `20250629` represents YYYYMMDD (2025-06-29) and MUST be used for ordering and selection. The suffix `_HHMM` (e.g., `_1352`) refines ordering when multiple logs share the same date.

Goals:
* Deterministic ordering using filename-embedded timestamp ONLY (no mtime fallback).
* Allow caller to request "latest N files" (e.g., 5) regardless of gaps in days.
* Keep default bounded (10 files) if no override provided.

Regex Pattern:
`^ptop-(?P<date>\d{8})_(?P<time>\d{4})\.log$`

Ordering Key Computation:
```
date_part = YYYYMMDD
time_part = HHMM
key = datetime.strptime(date_part+time_part, "%Y%m%d%H%M")  # UTC assumption
```
Keys are comparable; higher key = more recent file.

Selection Algorithm:
1. Enumerate candidates under `<extract_root>/var/log` matching the regex above (ignore `.gz` or any other extension with warning `skipped_non_plain_log:<file>`).
2. Parse ordering key for each candidate.
  - If parse fails (should not if regex matched) skip with warning `skipped_bad_name:<file>`.
3. Sort descending by key.
4. Let `max_files` = request parameter (optional). If absent use `DEFAULT_MAX_PTOP_FILES=10`.
  - Validate `1 <= max_files <= DEFAULT_MAX_PTOP_FILES`. If > default, clamp and emit `max_files_clamped_to_default`.
5. Retain the first `max_files` entries (these are the latest N).
6. Reverse retained list to obtain chronological order for ingestion (oldest → newest among the selected set) so time_series append naturally.
7. If zero candidates remain emit `no_valid_ptop_logs` and skip ingestion.

Request Extension (Replaces prior day-window proposal):
```
POST /support/load_bundle
{ "path": ".../sb_x.tar.gz", "max_files": 5 }
```
If `max_files` omitted → default 10. Invalid (<=0 or non-int) → 400. > default → clamp + warning.

In-File TIME Validation (Optional Sanity):
* After selecting filenames, during parse we can compare first encountered TIME epoch inside each file to the filename-derived key.
* If absolute difference > `FILENAME_TIME_DRIFT_THRESHOLD` (e.g., 24h) emit `filename_time_mismatch:<file>` but continue (filename remains source of ordering truth).

Rationale for Filename-Based Ordering:
* Filename date/time already encodes intended chronological snapshot boundary.
* Faster than scanning each file for TIME anchor upfront.
* Aligns with operator expectations (seeing sorted names matches ingestion order).

Removed (Compared to Previous Draft):
* Day-window filtering logic (`max_days`) – superseded by direct `max_files` selection.
* TIME-line-first ordering – now secondary validation only.

Metrics & Warnings (Updated):
* skipped_non_plain_log:<file>
* skipped_bad_name:<file>
* max_files_clamped_to_default
* filename_time_mismatch:<file>
* no_valid_ptop_logs
* reused_metrics_skipped

Testing Adjustments:
1. Generate >10 synthetic filenames with sequential date/time → ensure only latest 10 chosen by default.
2. Provide `max_files=5` → latest 5 chosen; order of ingestion oldest→newest in that subset.
3. Corrupt one filename (bad digits) → skipped with warning skipped_bad_name.
4. Insert extraneous non-matching `.log` file → ignored.
5. Introduce TIME drift (mock parser returning earlier epoch) → filename_time_mismatch warning.

Future Extensions:
* Combine `max_files` with an optional `since_date` filter if needed later.
* Allow `.gz` ingestion with unified pattern once compression appears in bundles.
* Support explicit file whitelist parameter for forensic replay.

State & Idempotency (unchanged):
The `ingested` column and reuse semantics remain as previously described.

Bundle Hash Algorithm:
`bundle_hash = sha256( JSON.stringify(sorted([ {"name":basename(path),"size":bytes,"mtime":unix,"sha256_1mb":sha256(first_1MB)} ])) )`
Rationale: low-cost, collision-resistant enough for idempotency; can be switched to full-file hashing later without API change.

#### 7.5.3 Idempotency & Skips
- If same `bundle_hash` for tenant already recorded and `force` false → skip ingestion; return cached summary with `reused=true`.

#### 7.5.4 Error & Partial Handling
- If a log fails parse catastrophically, record warning; continue others unless error rate > configured threshold.
- Final response includes `warnings` and possibly `partial=true`.

#### 7.5.5 Host & Host ID Extraction & Metric Label Injection
IDENT line format (observed):
```
IDENT <ptop_version> <host_id>
```
Where:
- `ptop_version` (e.g., `9.0.5-52728-5501324ffb0c`) becomes label `ptop_version`.
- `<host_id>` is a stable unique identifier (appliance / platform specific) → label `host_id`.

Human-friendly `host` derivation order:
1. Bundle filename pattern: `sb_<host>_...` (substring after `sb_` up to next `_`).
2. If absent, attempt mapping host_id → known host alias (if a provided mapping file exists; Phase 1 skip).
3. Fallback to the `host_id` value.

If IDENT missing, we still infer `host` from filename and leave `host_id="unknown"` and `ptop_version="unknown"`.

All ingested samples automatically include labels: `bundle_id`, optional informational `sptid`, plus `host`, `host_id`, `ptop_version`, `source="ptops"`.

#### 7.5.6 Future (Not Phase 1)
- Async ingestion (`async=true`) returning task id.
- Multiple simultaneously active historical bundles with `switch_active_bundle` endpoint.
- Differential re-ingest (only newest log).

### 7.6 Minimal Public Surface (Phase 1 Recap)
Public (initial): `load_bundle` (with optional path, auto latest discovery), `get_active_context`, `get_ingest_status`, `unload_active_bundle`, plus docs & metrics endpoints: `docs.search_docs`, `docs.get_doc`, `metrics.list_metrics`, `metrics.list_label_values`, `metrics.metric_metadata`, `metrics.query_metrics`, `metrics.graph_timeseries` (and optionally `metrics.graph_compare`). Hidden/internal: manifest & log primitives.

Rationale: Simplifies model—client only needs a path; sptid derived for display.

### 7.6.1 Implemented REST Documentation Endpoints (Prototype Layer)
For the prototype we exposed a thin HTTP layer (pre-MCP adapter) to exercise documentation retrieval using the static embeddings artifact. These map conceptually to the planned MCP `docs.*` tools but are currently plain REST endpoints. This enables early integration tests and client iteration before the MCP dispatcher & schema layer are completed.

Implemented Endpoints (HTTP):
| Method & Path | Purpose | Planned MCP Tool Mapping |
|---------------|---------|--------------------------|
| GET /docs/plugins | List record_type / plugin identifiers with docs | docs.list_plugins (optional helper) |
| GET /docs/plugin/{plugin} | List doc refs for a specific plugin | docs.list_plugin_docs (optional helper) |
| GET /docs/doc/{doc_id} | Fetch full doc by id (L1/L2/L4 etc.) | docs.get_doc |
| GET /docs/metric/{metric_name} | Convenience metric lookup by canonical name | docs.get_metric (alias to get_doc w/ metric_name) |
| GET /docs/alias/{alias} | Resolve legacy alias tokens to canonical docs | docs.resolve_alias |
| GET /docs/concepts | List concept (L4) document ids | docs.list_concepts |
| POST /docs/search | Search (semantic or keyword) returning lightweight refs | docs.search_docs |
| POST /docs/search/detail | Same search returning full docs | docs.search_docs (with include_content flag) |
| POST /docs/search/metrics | Metric-focused search + confidence & auto/ambiguous decision | docs.search_metrics (new specialized tool) |

Request/Response Highlights:
* POST /docs/search body: { query:string, semantic?:bool=true, top_k?:int=5, levels?:["L1"|"L2"|"L4"], query_embedding?:[float] }
* Keyword search performs simple token containment scoring; semantic search performs cosine similarity over prebuilt vectors (cheap fallback embedding used if caller doesn't supply one and stored vectors present).
* Alias resolution leverages `legacy_aliases` & provenance legacy fields in the embedding metadata.

Design Notes (Updated):
1. Strict Separation of Concerns: The embeddings loading & search implementation was moved from `docs/embeddings_store.py` to module root `embeddings_store.py` to keep `mcp_server/docs/` directory documentation-only (specs + artifacts) per project convention.
2. Forward Compatibility: Endpoints mirror the eventual MCP tool surface; adding the MCP adapter later will wrap existing Python functions without altering behavior. New `/docs/search/metrics` directly maps to planned `docs.search_metrics` tool (specialized subset of generic search for L1 metric discovery & auto-selection heuristics).
3. Performance: In-memory indices (metric_name, alias, plugin, concept) built once at startup (lazy on first call) to provide O(1)/O(log n) lookups. Semantic search is O(N * dim) over current small corpus (<1K docs) – acceptable; will introduce ANN if scale grows.
4. Safety & Validation: (Revised) Empty `query_embedding` now yields HTTP 400. Dimension mismatches are tolerated: query vectors are deterministically truncated or tiled to stored dimension (prototype ergonomics) instead of raising. This avoids fragile client coupling to embedding dimension while offline artifact remains low-dim placeholder. A future strict mode flag can re-enable hard validation.
5. Adaptive Embedding Fallback: When no `query_embedding` supplied we build a cheap hash-based embedding automatically sized to stored `_embedding_dim` (if known) or default 128. This ensures consistent cosine similarity computation irrespective of artifact dimension.
6. Confidence Heuristics (`/docs/search/metrics`): Provides decision triage for NL→metric resolution. Heuristic definitions: `confidence = top1_score - top2_score` (or `top1_score` if only one candidate). Decision rules: 
  * `no_match`: zero candidates returned.
  * `auto`: (`top1_score >= ABS_THRESHOLD`) OR (`confidence >= GAP_THRESHOLD`) AND `metric_name` present.
  * `ambiguous`: any other case (client should present candidate list to user).
  Threshold constants (prototype): `ABS_THRESHOLD=0.90`, `GAP_THRESHOLD=0.15`. Returned payload echoes `confidence`, `threshold` (gap), `decision`, `auto_selected` (metric_name or null), and ranked candidates (doc_id, metric_name, record_type, score, rank).
7. Extensibility: Additional filters (record_type, metric_category) intentionally deferred until MetricsTool exists to avoid premature coupling; metadata already present to support quick addition. Confidence thresholds will become configurable (env or request overrides) in a future revision; design keeps response shape stable.

Heuristic Rationale:
* Absolute threshold guards against uniformly high scores where top2 nearly equals top1 (avoids false-positive auto selects when query is broad).
* Gap threshold aids disambiguation for specific intents where one metric clearly dominates semantically.
* Returning raw scores enables downstream adaptive UI (e.g., calibrating thresholds via user feedback without server change).

Error Handling Changes Summary:
* Previous behavior (reject mismatched embedding dimensions with 400) replaced by adaptive projection.
* New explicit 400 for empty provided embedding (`[]`).
* Other validation paths unchanged (unknown doc id → 404, missing metric doc returns 200 with `doc: null`).

Planned Follow-ups (Not Yet Implemented):
* Allow client-specified `abs_threshold` / `gap_threshold` override in request body (bounded by server min/max policy).
* Add optional `filters` object to `/docs/search/metrics` (fields: `record_type[]`, `metric_category[]`).
* Provide `explanations` array (e.g., short reason strings) when `decision=ambiguous` to help display disambiguation cues (e.g., differing record_type, differing unit).
* Expose strict dimension validation via `?strict_dims=true` query parameter for integration tests once artifact embeddings finalized.

Migration Plan to MCP Tools:
* docs.search_docs → dispatcher invokes same search function; add pagination & filters.
* docs.get_doc / docs.get_metric / docs.resolve_alias unify responses into standard envelope (see §7.7 for MCP envelope design).
* Remove REST exposure (or retain under /internal) once MCP adapter stable; design doc will track any divergence.

Open Follow-ups:
* Add L3 alias cluster embeddings (currently placeholder – search still finds aliases via `legacy_aliases`).
* Add filter parameters (record_type, metric_category) to search endpoints.
* Add related_docs (nearest neighbors) endpoint/tool post alias cluster implementation.

This subsection documents rationale and mapping so the REST stop-gap does not drift from intended MCP interface contract.

### 7.7 Docs (Documentation) Tool API (Detailed)
Namespace: `docs` (logical tool name exposed via MCP). Requests no longer include tenant scoping; documentation corpus is global.

Common envelope (implicit here):
```
Request: { ... }
Success Response: { data: <payload>, meta?: { generated_at:int, warnings?: [string] } }
Error Response: { error: { code:string, message:string, details?:any } }
```

#### 7.7.1 search_docs
Find top-k documentation or metric definition snippets using vector + keyword hybrid search.
Request:
```
{ query:string, k?:int=5, filter?:{ record_type?:[string], metric_category?:[string], metric_kind?:[string] }, page_token?:string }
```
Response:
```
{ results:[ { doc_id, title, snippet, score:number, record_type?, metric_name?, metric_kind? } ], next_page_token? }
```

#### 7.7.2 get_doc
Retrieve full document / field definition.
Request: `{ doc_id:string }`
Response:
```
{ doc_id, title, content, metadata:{ record_type?, metric_name?, metric_kind?, version?, provenance_hash? }, related_doc_ids?:[string] }
```

#### 7.7.3 related_docs
Semantic nearest neighbors to a given doc.
Request: `{ doc_id, k?:int=5 }`
Response: `{ doc_id, related:[ { doc_id, title, score } ] }`

#### 7.7.4 list_topics
High-level topical groupings (derived from documentation taxonomy / record_type / categories).
Request: (none)
Response: `{ topics:[ { topic:string, doc_count:int } ] }`

#### 7.7.5 explain_term
Resolve an unknown or shorthand term to canonical docs (e.g. "queue_len" → DISK avg_queue_len).
Request: `{ term:string }`
Response:
```
{ term, normalized_term?:string, matches:[ { doc_id, definition:string, score:number } ] }
```

#### 7.7.6 enrich_metrics (optional Phase 2)
Bulk attach doc snippets to metric names.
Request: `{ metrics:[string] }`
Response: `{ enriched:[ { metric, doc_id?, snippet? } ] }`

### 7.8 Metrics Tool API (Detailed)
Namespace: `metrics`.

#### 7.8.1 list_metrics
Enumerate metric names currently present (filtered by optional prefix).
Request: `{ prefix?:string, limit?:int=50, page_token?:string }`
Response: `{ metrics:[ { name:string, type?:string, help?:string } ], next_page_token? }`

#### 7.8.2 list_label_values (Promoted to Phase 1)
Request: `{ metric:string, label:string, limit?:int=100, page_token?:string }`
Response: `{ values:[string], next_page_token? }`

#### 7.8.3 metric_metadata
Request: `{ metric:string }`
Response:
```
{ metric, type?, unit?, help?, labels_example?:{ [k:string]:string }, related_docs?:[string] }
```

#### 7.8.4 query_metrics
Low-level expression query (PromQL / VictoriaMetrics compatible subset).
Request:
```
{ expr:string, start:int, end:int, step_ms:int, max_series?:int }
```
Response:
```
{ series:[ { metric:string, labels:{[k:string]:string}, points:[ [ts_ms:int, value:number] ] } ], query_stats?:{ fetched_series:int, step_ms:int, warnings?:[string] } }
```

#### 7.8.5 graph_timeseries
High-level convenience wrapper returning GraphSpec (see §7.4 for schema).
Request:
```
{ metrics: [string]|string, start:int, end:int, step_ms:int, functions?:[string], align_mode?:"drop_missing"|"fill_forward"|"zero_fill" }
```
Response: `{ graph: GraphSpec }`

#### 7.8.6 graph_compare
Request:
```
{ primary:string, comparators:[string], start:int, end:int, step_ms:int, normalize?:boolean }
```
Response: `{ graph: GraphSpec }`

#### 7.8.7 graph_pointset
Sparse timestamps delta/ratio quick view.
Request: `{ metrics:[string], timestamps:[int], tolerance_ms?:int }`
Response:
```
{ pointset:{ metrics:[string], timestamps:[int], values:{ [metric:string]:[number|null] }, deltas?:{ [metric:string]:[number] }, percent_change?:{ [metric:string]:[number] } } }
```

#### 7.8.8 graph_scatter
Correlation between two metrics.
Request:
```
{ x_metric:string, y_metric:string, start:int, end:int, step_ms:int, x_transform?:string, y_transform?:string }
```
Response: `{ graph: GraphSpec }` (scatter series uses point format `[x_value,y_value, ts_ms?]`).

#### 7.8.9 graph_histogram_dbwr
Bucket evolution (DBWR/DBWA/DBRD).
Request:
```
{ prefix:"dbwr"|"dbwa"|"dbrd", start:int, end:int, step_ms:int, bucket_filter?:[string] }
```
Response: `{ graph: GraphSpec }`

#### 7.8.10 graph_topk
Dynamic top-K entities per interval.
Request:
```
{ metric:string, by_label:string, k:int, start:int, end:int, step_ms:int, normalize?:boolean }
```
Response: `{ graph: GraphSpec }`

#### 7.8.11 top_n (tabular shortcut)
Request:
```
{ metric:string, label:string, n:int, time_range:{ start:int, end:int }, agg?:string="avg_over_time" }
```
Response:
```
{ ranking:[ { label_value:string, value:number } ], metric, label, agg }
```

#### 7.8.12 correlate_metrics (Phase 2)
Request:
```
{ metrics:[string], start:int, end:int, step_ms:int, method?:"pearson"|"spearman" }
```
Response:
```
{ correlations:[ { pair:[string,string], coefficient:number, p_value?:number } ], best_lead_lag?:[ { pair:[string,string], lag_ms:int, improved_coeff:number } ] }
```

#### 7.8.13 anomalies (Phase 2)
Request:
```
{ metric:string, start:int, end:int, step_ms:int, method?:"zscore"|"iqr", threshold?:number }
```
Response:
```
{ metric, anomalies:[ { ts_ms:int, value:number, score:number } ], method }
```

### 7.9 Endpoint Interaction Examples (Illustrative)
1. User loads bundle: `support.load_bundle { path:"/import/...tar.gz" }` → metrics auto-ingested.
2. UI polls: `support.active_context {}` → shows time range & counts.
3. User explores docs: `docs.search_docs { query:"disk queue length" }` → picks doc.
4. User graphs metric: `metrics.graph_timeseries { metrics:["disk_avg_queue_len{device=\"sda\"}"] , start:..., end:..., step_ms:60000 }`.
5. Comparative: `metrics.graph_compare { primary:"cpu_user_percent{cpu=\"cpu0\"}", comparators:["cpu_user_percent{cpu=\"cpu1\"}"], start:..., end:..., step_ms:60000, normalize:true }`.

### 7.10 Security / Multi-Tenancy Enforcement Notes
- Each metrics query injects `{bundle_id="<active>"}` before sending to VictoriaMetrics unless expression already fixes bundle_id.
- Docs queries are global; no scoping applied.
- Active bundle context stored per tenant; attempts to query metrics with no active bundle and no historical data yield empty series + warning.
- Guard: if user expression specifies a different bundle_id than active (future multi-bundle compare) we accept only if explicit override tool parameter set.

### 7.11 Phase Tagging Summary (Updated)
Phase 1 (MVP):
- support: load_bundle (path optional, auto-discovery), get_active_context, get_ingest_status, unload_active_bundle (tenant purge),
- docs: search_docs, get_doc,
- metrics: list_metrics, list_label_values, metric_metadata, query_metrics, graph_timeseries, graph_compare (optional).

Phase 2:
- docs: related_docs, list_topics, explain_term, enrich_metrics,
- metrics: graph_pointset, graph_scatter, graph_histogram_dbwr, graph_topk, top_n.

Phase 3:
- metrics: correlate_metrics, anomalies.

Rationale for Phase Adjustments:
- `list_label_values` moved to Phase 1 to enable discoverability of dynamic label dimensions (process exec names, device IDs) without users memorizing values, lowering friction in VS Code prompts.
- Optional bundle path auto-discovery reduces cognitive load; users can start with only the ticket ID, aligning with real support workflows.
- Added `reused` flag prevents misinterpretation of instantaneous returns when bundle already ingested (clarity for idempotent operations).
- Host extraction fallback order documented to ensure consistent labeling for metrics queries even when filenames diverge from the expected pattern.
- `purged_tenant` result clarifies cleanup semantics on unload (so UI can hide tenant-scoped commands immediately).
- Keeping advanced analytical endpoints (correlation, anomalies, histogram visualization) deferred preserves MVP implementation velocity while core investigative loop (load → inspect metrics & docs → graph → unload) is intact.

CLI Examples (conceptual) (Phase 1 only implements graph_timeseries & optional graph_compare):
```
graph_timeseries metrics=["cpu_utilization{cpu=\"cpu0\"}","cpu_utilization{cpu=\"cpu1\"}"] start=... end=... step=60s
graph_compare primary_metric="disk_busy_percent{device=\"sda\"}" comparison_metrics=["disk_busy_percent{device=\"sdb\"}"] start=... end=... step=300s normalize=true
graph_pointset metrics=["top_cpu_percent{exec=\"bash\",pid=11404}" ] timestamps=[t1,t2]
graph_scatter x_metric="disk_avg_queue_len{device=\"sda\"}" y_metric="disk_busy_percent{device=\"sda\"}" start=... end=... step=60s
graph_histogram_dbwr prefix=dbwr start=... end=... bucket_filter=[17,19,21]
```

UI / Consumer Guidance:
- The MCP client can render GraphSpec directly or translate to front-end chart config (e.g., Plotly, Vega) without server changes.

- `requests` (HTTP) or stdlib `urllib` (prefer requests if allowed; fallback to urllib if dependency constraints strict).
- `chromadb` Python client.
- No heavy frameworks.

### 7.12 VictoriaMetrics Integration Plan (Phase 1 Baseline)

Scope: Enable direct ingestion of parsed PTOPS metrics into VictoriaMetrics (VM) using the native JSON import endpoint, with strong idempotency and multi-tenant isolation via labels. This augments earlier placeholder ingestion counts.

#### 7.12.1 Ingestion Path
1. After extraction & bundle row creation, perform strict TIME-based log discovery (§7.5.2a).
2. For each selected log (chronological), stream parse via existing parser → MetricSample objects.
3. Append mandatory labels: `bundle_id`, optional `sptid`, `source="ptops"`, plus host labels from IDENT parsing.
4. Buffer into NDJSON lines until batch triggers flush (size or count threshold).
5. POST batch to `POST ${VM_BASE_URL}/api/v1/import`.
6. On success: increment metrics_ingested by batch size. On failure: retry (3 attempts); final failure increments failure count.
7. After final log: update `bundles` row (logs_processed, metrics_ingested, start_ts, end_ts, ingested=1) if at least one successful sample flush.
8. If VM disabled (no `VM_BASE_URL`): skip steps 4–7, warning `vm_disabled`, leave `ingested=0` so future re-load (force or new VM config) can ingest.

#### 7.12.2 Batch Format
Each MetricSample serialized as:
```
{"metric":"<name>","values":[<float>],"timestamps":[<ts_ms>],"labels":{...}}
```
One sample per line (simple; avoids grouping logic). Future optimization: coalesce same metric+labels with parallel `values[]`/`timestamps[]` arrays.

#### 7.12.3 Label Schema (VM)
Core labels (always present):
| Label | Purpose |
|-------|---------|
| sptid | Informational ticket / support case display |
| bundle_id | Distinguish bundles for same tenant |
| host | Human-friendly host identifier |
| host_id | Stable appliance/system id |
| ptop_version | Parser provenance / versioning |
| record_type | Original PTOPS prefix (CPU, DISK, TOP, etc.) |
| metric_category | Normalized grouping (cpu, disk, network, process, etc.) |
| source | Constant `ptops` |
Entity labels added case-by-case (cpu, device, interface, pid, exec, bucket, etc.).

#### 7.12.4 Idempotency & Reuse
* Hash-based bundle reuse avoids double ingestion: if bundle hash exists with `ingested=1` and not `force`, ingestion short-circuits.
* If `ingested=0` (e.g., earlier VM disabled), loading again attempts ingestion.
* Force mode re-processes metrics even if hash unchanged (for backfill / parser evolution), issuing a new bundle_id (old remains until unloaded or purged).

#### 7.12.5 Error Handling & Warnings
| Warning | Condition |
|---------|-----------|
| vm_disabled | VM_BASE_URL absent |
| vm_ingest_failed_batches=<n> | One or more batch POSTs failed after retries |
| vm_delete_failed:<code> | Hard delete returned non-2xx |
| vm_delete_unsupported | Delete endpoint disabled or disallowed |
| no_valid_ptop_logs | No logs after discovery filtering |
| skipped_file_no_time:<f> | File missing TIME anchor |
| log_cap_applied:<n> | File count truncated after day window |
| ingest_days_override:<d> | User provided max_days narrower than default |
| max_days_clamped_to_default | Provided max_days exceeded default window |
| reused_metrics_skipped | Reuse path avoided ingestion |

#### 7.12.6 Cleanup Strategy
Logical scoping uses only `bundle_id`. Optional physical deletion:
* If `VM_ALLOW_DELETE=true`, on unload or purge_all issue delete-series selector targeting all series with matching `bundle_id`.
* Follow with tombstone clean if environment policy permits (POST `/api/v1/admin/tsdb/clean_tombstones`).
* Failure degrades gracefully to logical removal (row deletion & extraction dir purge).

#### 7.12.7 Configuration
| Env | Default | Description |
|-----|---------|-------------|
| VM_BASE_URL | (unset) | Enables VM ingestion when set |
| VM_BATCH_SIZE | 500 | Max samples per POST |
| VM_TIMEOUT_MS | 5000 | HTTP timeout per batch |
| VM_ALLOW_DELETE | false | Enable hard delete on unload |
| VM_DEBUG_JSONL | false | Mirror NDJSON batches for debugging |
| DEFAULT_INGEST_DAYS | 10 | Default day window for log selection |

#### 7.12.8 Performance Targets
* Per-batch latency target < 150ms p95 for local VM.
* Retry overhead bounded (< 3 * timeout per failed batch).
* Memory footprint: O(batch_size * sample_object_size) — streaming ensures independence from total logs volume.

#### 7.12.9 Future Extensions
* Batch coalescing (multi-sample arrays) to reduce HTTP overhead.
* Gzip compression of NDJSON payloads when beneficial (Content-Encoding: gzip).
* Async ingestion pipeline with progress endpoint.
* Partial re-ingest / incremental updates (only new logs) using per-log content hash state.
* Automatic metric schema change detection (compare emitted field set across bundles).
* Aggregated derivations (e.g., building cpu_total_percent) server-side before storage.

#### 7.12.10 Security & Multi-Tenancy
* All user queries inject `bundle_id` matcher to scope to active bundle.
* Hard delete gated by explicit env flag to avoid accidental data loss.
* Label whitelist logic (future) can strip unexpected user-supplied labels before query execution.

#### 7.12.11 Observability
Internal counters (exportable later):
| Counter | Meaning |
|---------|---------|
| ptops_ingest_samples_total | Total samples attempted |
| ptops_ingest_samples_failed_total | Samples in failed batches |
| ptops_ingest_batches_total | Batches sent |
| ptops_ingest_batches_failed_total | Batches failed after retries |
| ptops_ingest_duration_ms_sum | Cumulative ingestion elapsed |
| ptops_delete_requests_total | Delete attempts |
| ptops_delete_requests_failed_total | Delete failures |

#### 7.12.12 Testing Focus
* Mock VM endpoint to assert batch payload correctness & label completeness.
* Reuse test: ensure no POSTs when reused=true.
* Fail injection test: simulate 500 responses → warning & batch_fail counts.
* Delete path test: verify correct selector formed for tenant+bundle.
* Day-window / max_days override tests (ties to §7.5.2a discovery logic).

This plan formalizes integration details so implementation can proceed without ambiguity and stays aligned with multi-bundle + strict TIME-based discovery semantics.

## 8. Naming & Label Mapping Examples
| PTOPS Line | Sample Metric(s) |
|------------|------------------|
 | `DBWR 17 3 0.00008 19 1 0.00028` | `dbwr_bucket_count{bucket="17"}=3`, `dbwr_bucket_avg_latency_seconds{bucket="17"}=0.00008` |
| `DISK 24 sda rkxt 1.0 4.124 4.000 110.0 ...` | `disk_reads_per_sec{device="sda"}=1.0`, `disk_read_kb_per_sec{device="sda"}=4.124`, `disk_busy_percent{device="sda"}=100.0` |
| `TOP 11393 11404 80.3% 4.7 (4.6 0.1) 0 bash` | `top_cpu_percent{pid="11404",ppid="11393",exec="bash"}=80.3` |
| `DBWR 17 3 0.00008 19 1 0.00028` | `dbwr_bucket_count{bucket="17"}=3`, `dbwr_avg_latency_seconds{bucket="17"}=0.00008` |

## 9. Performance Considerations
- Streaming parse avoids loading entire log into memory.
- Batch size tuning for VictoriaMetrics (default 500 samples or 250KB, whichever first) to keep request payload reasonable.
- Optional parallelization: parsing in one pass then asynchronous ingestion (phase 2 optimization).
 - Parse error threshold: abort ingestion with BACKEND_ERROR if >5% of non-TIME lines fail (config `PARSE_ERROR_MAX_PCT`, default 5). Partial warnings otherwise aggregated.
 - VM write retry policy: 3 attempts per batch (backoff 0.5s,1s,2s) with 5s timeout; failures contribute to warnings & abort if overall success ratio <95%.

## 10. Testing Strategy
Unit tests:
- Line parsing per prefix (fixtures from sample log).
- Schema completeness: every documented `metric_kind != (text|opaque)` has a parse rule.
- VictoriaMetrics payload shape correctness.
- Chroma embedding count vs expected field rows.
 - Golden NDJSON serialization fixture.
 - Concurrency idempotency test: two simultaneous load_bundle calls same tenant/path → one ingests, one reused.
Integration tests:
- Parse sample log → ingest into a mock VictoriaMetrics endpoint (capture HTTP bodies).
- Search queries return relevant doc IDs.

## 11. Future Enhancements
- Counter reconstruction for rate fields (derive cumulative series for long-term aggregations).
- Adaptive sampling / downsampling heuristics during ingestion (especially for TOP & SMAPS).
- Grafana dashboards auto-generation via JSON templates.
- Alert rule scaffolding (e.g., sustained `disk_avg_queue_len > X`).
- Multi-log merge tool with host label normalization.
- Binary format or parquet output for long-term cold storage.

## 12. Open Questions
- Should we collapse per-bucket DBWR latency averages into exemplars or keep separate metrics? (Currently separate gauge.)
- How to handle IMCDR_NODES nodeid@ip list explosion? (Maybe as a dedicated text doc embedding only.)
- Provide optional compression when sending to VictoriaMetrics import endpoint?

## 13. Approval Checklist
- [ ] Metric name conventions confirmed
- [ ] Label taxonomy accepted
- [ ] Histogram strategy agreed
- [ ] Opaque plugin handling approved
- [ ] Parser options minimal initial set OK
- [ ] MCP tool list sufficient

---

## 14. Storage & Deployment Architecture (Updated)

### 14.1 Role Separation Summary
| Concern | Chosen Component | Rationale |
|---------|------------------|-----------|
| Time‑series metrics (large append, range queries) | VictoriaMetrics (single-node) | Purpose-built TSDB: efficient compression, fast range & aggregation, PromQL-compatible. |
| Operational state (bundles, active context, idempotency hashes, ingestion offsets, label cache) | SQLite (embedded) | Strong ACID, zero extra service, simple file backup, UNIQUE constraints & transactions. |
| Semantic search (docs, metric definitions, optional memory snippets) | ChromaDB | Vector similarity + metadata filters; lightweight local persistence. |
| Transient in-process caches (recent GraphSpec, doc search results) | In-memory (LRU) | Latency reduction; rebuildable on restart. |
| Future scalable state (optional) | Postgres (migration target) | Multi-writer, horizontal scaling & richer SQL if needed. |

Separation ensures each workload uses a store optimized for its access pattern; avoids overloading VictoriaMetrics with non-time-series state and avoids forcing relational constraints onto a vector store.

### 14.2 SQLite Schema (Indicative DDL)
```
CREATE TABLE bundles (
  bundle_id TEXT PRIMARY KEY,
  sptid TEXT,
  bundle_hash TEXT NOT NULL,
  host TEXT,
  host_id TEXT,
  ptop_version TEXT,
  logs_processed INTEGER,
  metrics_ingested INTEGER,
  start_ts INTEGER,
  end_ts INTEGER,
  reused INTEGER DEFAULT 0,
  created_at INTEGER NOT NULL,
  UNIQUE(sptid, bundle_hash)
);

CREATE TABLE active_context (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  bundle_id TEXT NOT NULL,
  activated_at INTEGER NOT NULL,
  FOREIGN KEY(bundle_id) REFERENCES bundles(bundle_id)
);

CREATE TABLE ingest_offsets (
  bundle_id TEXT,
  log_path TEXT,
  last_byte INTEGER,
  updated_at INTEGER,
  PRIMARY KEY(bundle_id, log_path)
);

CREATE TABLE label_index (
  bundle_id TEXT,
  metric TEXT,
  label_name TEXT,
  label_value TEXT,
  PRIMARY KEY(bundle_id, metric, label_name, label_value)
);

CREATE TABLE docs (
  doc_id TEXT PRIMARY KEY,
  provenance_hash TEXT,
  record_type TEXT,
  metric_name TEXT,
  metric_kind TEXT,
  version TEXT,
  updated_at INTEGER
);

-- Embeddings live in Chroma; docs table tracks provenance for refresh decisions.
```
WAL mode enabled (`PRAGMA journal_mode=WAL;`) for read concurrency; periodic `VACUUM` during maintenance windows only.

### 14.3 State Access Patterns
1. load_bundle transaction: upsert bundle row, set active_context, bulk insert label_index (batched with `INSERT OR IGNORE`).
2. Query-time label discovery: read-only SELECT on label_index filtered by metric + label_name.
3. Idempotency: compute bundle_hash (SHA256 of sorted log file names + sizes + mtimes); if UNIQUE constraint violation, mark `reused=1` and fetch existing row.
4. Ingestion resume (future): consult ingest_offsets for partially parsed large logs.

### 14.4 Container Deployment (Chosen: All-in-One Only)
Decision: For the prototype (and early internal adoption) we standardize exclusively on a single "all-in-one" container image. Alternative multi-container (compose) or Kubernetes orchestrations are explicitly out-of-scope for now and not maintained.

All-in-One Image Contents:
- MCP server process (Python)
- Embedded SQLite DB file (mounted volume)
- VictoriaMetrics single-node binary
- Chroma vector DB (embedded / local server mode)
- Minimal init supervisor (tini or dumb-init)

Startup Order: VictoriaMetrics → Chroma → MCP server (which blocks). Health probes performed by MCP against the two internal services before declaring readiness on its exposed port.

Advantages (why chosen):
1. Fastest path to a runnable artifact (single build, single distribution).
2. Simplified documentation & support (one version tag, one checksum).
3. Lower operational friction for analysts (run container, bind support bundle directory, query).
4. Consistent local + CI usage (same invocation pattern).

Accepted Trade-offs:
- Tight coupling: restarting any component restarts all.
- No independent resource scaling (acceptable at current data volumes).
- Larger image size; mitigated via multi-stage build and pruning dev dependencies.

Out-of-Scope (Deferred Until Scaling Need):
- Multi-container compose layouts.
- Kubernetes deployment manifests.
- Externalizing VictoriaMetrics or Chroma to managed services.

Migration Path (documented for future but not implemented now):
1. Introduce environment flags to disable internal VictoriaMetrics/Chroma startup and point to external endpoints.
2. Split Dockerfile into base layers (server) + thin orchestrated services.
3. Provide dedicated Docker Compose file only after interface stability.

### 14.5 Volumes & Mount Points
| Path Inside Container | Host Mount Suggestion | Purpose |
|-----------------------|-----------------------|---------|
| /data/state           | ./data/state          | SQLite file(s) (bundles.db) |
| /data/victoria        | ./data/victoria       | VictoriaMetrics TSDB data |
| /data/chroma          | ./data/chroma         | Chroma collection persistence |
| /var/log/mcp          | ./logs                | MCP server logs |
| /config               | ./config              | YAML/ENV config, schema version markers |
| /import/support       | ./support             | Mounted support bundles (read-only) |

Expose MCP server HTTP on internal port (default 8085) mapped to host (e.g., 18085). Keep VictoriaMetrics (8428) & Chroma (8000) internal only except for debugging.

### 14.6 Environment Variables
| Variable | Description | Default |
|----------|-------------|---------|
| MCP_HTTP_PORT | MCP server listen port | 8085 |
| VM_WRITE_URL | VictoriaMetrics base URL | http://victoriametrics:8428 |
| VECTOR_DB_URL (optional) | External vector DB endpoint | (unset) |
| SQLITE_PATH | Path to SQLite DB file | /data/state/bundles.db |
| PARSER_BATCH_SIZE | Metric batch size per HTTP push | 500 |
| PARSER_MAX_PAYLOAD_KB | Max payload size (approx) | 256 |
| INCLUDE_SMAPS_FULL | Enable full SMAPS ingestion | false |
| INCLUDE_OPAQUE | Ingest opaque plugin lines as annotations | false |
| LOG_LEVEL | Logging verbosity | INFO |

### 14.7 Sample docker-compose (Illustrative Only)
```
version: "3.9"
services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:v1.103.0
    command: ["--retentionPeriod=30d", "--storageDataPath=/data/victoria"]
    volumes:
      - ./data/victoria:/data/victoria
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8428/health"]
    restart: unless-stopped

  chroma:
    image: ghcr.io/chroma-core/chroma:latest
    volumes:
      - ./data/chroma:/data
    restart: unless-stopped

  mcp-server:
    build: ./mcp_server
    environment:
      MCP_HTTP_PORT: 8085
      VM_WRITE_URL: http://victoriametrics:8428
  # VECTOR_DB_URL: http://chroma:8000 (example when reintroducing external store)
      SQLITE_PATH: /data/state/bundles.db
    volumes:
      - ./data/state:/data/state
      - ./support:/import/support:ro
      - ./config:/config:ro
      - ./logs:/var/log/mcp
    ports:
      - "18085:8085"
    depends_on:
      - victoriametrics
      - chroma
    restart: unless-stopped
```
Notes: Reference only (NOT part of MVP deliverable). Pin image tags for reproducibility.

### 14.8 All-in-One Container Entrypoint
Supervisory script outline:
```
#!/usr/bin/env bash
set -euo pipefail
victoria-metrics --retentionPeriod=30d --storageDataPath=/data/victoria &
chroma run --path /data/chroma &
python -m mcp_server.app --port "$MCP_HTTP_PORT" --db "$SQLITE_PATH"
```
Ensure `tini` or `dumb-init` is PID 1 for signal forwarding.

### 14.9 Operational Considerations
- Backups: snapshot `/data/state` (SQLite) + `/data/victoria` + `/data/chroma`. Quiesce with brief pause or rely on WAL & consistent FS snapshots.
- Resource Sizing (prototype):
  - VictoriaMetrics: allocate ~512MB RAM (limit ingestion concurrency until needed).
  - Chroma: small corpus (< few thousand docs) -> negligible (<100MB) footprint.
  - SQLite: <10MB unless many bundles retained; single file.
- Security: run as non-root UID; restrict exposed ports to MCP only; optional basic auth / token on MCP endpoints.
- Chroma hydration failure: mark docs subsystem unavailable (warning `docs_unavailable`) but proceed with metrics ingestion.
- Upgrades: migrate SQLite using Alembic (optional) or manual migrations (DDL version table). Rebuild Chroma embeddings if provenance_hash changes.
- Observability: Add lightweight `/healthz` endpoint in MCP; enable VictoriaMetrics internal metrics (scraped optionally by external Prom scrapers).

### 14.10 Future Evolutions
- Swap SQLite to Postgres: introduce abstraction layer (StateStore) now; implement Postgres adapter later.
- Vector Store Alternatives: Allow plugging in pgvector / Milvus if corpus size explodes.
- Remote VictoriaMetrics Cluster: Repoint VM_WRITE_URL, disable local victoria service; container then only houses MCP + SQLite + optional Chroma.
- Multi-Tenant Scaling: Shard VictoriaMetrics by tenant or use label-based retention policies.

### 14.11 Rationale Recap
Keeping operational state out of the TSDB and vector DB avoids complex, inefficient queries and preserves clarity of responsibilities. An all-in-one container maximizes immediate usability and minimizes operational surface area for the prototype phase. Future scale concerns (independent scaling, HA) can be addressed by externalizing VictoriaMetrics and Chroma once data or concurrency pressures justify the added operational cost—without changing the MCP server APIs because storage responsibilities are already abstracted.

## 15. Low-Level Module & Tool Design (Approved)

Purpose: Concrete module decomposition and interfaces for implementing bundle (support), docs, and metrics tools as plain Python services, then wrapping them via an MCP adapter. Mirrors Phase 1 scope while allowing later backend substitutions (Postgres, external VM, different vector store) without API churn.

### 15.1 Directory Layout
```
mcp_server/
  core/
    types.py        # Shared dataclasses (MetricSample, BundleSummary, ActiveContext, etc.)
    errors.py       # ToolError + standard error codes
    config.py       # Config loader / env parsing
    logging.py      # Structured logging setup
    util.py         # Small helpers (hashing, time, pagination)
  state/
    store.py        # StateStore protocol / abstract base
    sqlite_store.py # SQLite implementation
  ingestion/
    parser.py       # PTOPSParser + PTOPSParserOptions
    hashing.py      # Bundle/log hashing helpers
  external/
    vm_client.py    # VictoriaMetrics client (write/query)
    chroma_client.py# Chroma wrapper
  tools/
    bundle_tool.py  # BundleTool (load/unload/status)
    docs_tool.py    # DocsTool (search/get)
    metrics_tool.py # MetricsTool (list/query/graph)
  graphs/
    spec.py         # GraphSpec & GraphSeries dataclasses
    transforms.py   # Graph transforms (normalize, derivative, ratio)
  mcp_adapter/
    schemas.py      # Pydantic request/response models
    dispatcher.py   # Tool name → callable mapping
    server.py       # MCP server runtime (HTTP or protocol bridge)
  tests/            # Mirrors structure with fixtures
```

### 15.2 Core Types (core/types.py)
```
@dataclass(frozen=True)
class MetricSample:
    name: str
    value: float
    ts_ms: int
    labels: dict[str, str]

@dataclass
class BundleSummary:
    bundle_id: str
  sptid: str
    host: str
    host_id: str
    ptop_version: str
    logs_processed: int
    metrics_ingested: int
    start_ts: int
    end_ts: int
    replaced_previous: bool
    reused: bool = False
    warnings: list[str] = field(default_factory=list)

@dataclass
class ActiveContext:
  sptid: str
    bundle_id: str
    host: str
    loaded_at: int
    metrics_ingested: int
    start_ts: int
    end_ts: int
```
Graph types (graphs/spec.py): `GraphSeries`, `GraphSpec` as in §7.4.

Parser options (ingestion/parser.py):
```
@dataclass
class PTOPSParserOptions:
    include_smaps_full: bool = False
    include_top: bool = True
    include_opaque: bool = False
    max_top_execs: int | None = None
```

### 15.3 Errors (core/errors.py)
`ToolError(code, message, details)` with codes: `INVALID_ARGUMENT`, `NOT_FOUND`, `CONFLICT`, `ALREADY_EXISTS`, `STATE_UNAVAILABLE`, `INGEST_IN_PROGRESS`, `BACKEND_ERROR`, `UNSUPPORTED` (extendable).

### 15.4 State Store Abstraction (state/store.py)
Protocol methods (subset):
```
insert_bundle(summary: BundleSummary)
get_bundle(bundle_id) -> BundleSummary | None
get_bundle_by_hash(sptid, bundle_hash) -> BundleSummary | None
set_global_active(bundle_id, loaded_at)
get_global_active() -> ActiveContext | None
unload_global_active() -> bool
bulk_insert_label_values(rows: list[tuple[bundle_id, metric, label_name, label_value]])
list_label_values(bundle_id, metric, label, limit, offset) -> list[str]
upsert_doc_meta(doc_id, meta)
get_doc_meta(doc_id) -> dict | None
```
SQLite implementation wraps operations in transactions; use WAL mode.

### 15.5 External Clients
VictoriaMetricsClient: `write_samples(samples)`, `query_range(expr,start_ms,end_ms,step_ms)`, `list_metrics()`.
ChromaDocsStore: `ensure_collection()`, `upsert_docs(docs)`, `search(query,k,filters)`, `get(doc_id)`.

### 15.6 Parser (ingestion/parser.py)
`PTOPSParser.iter_samples()` yields `MetricSample` streaming across all log paths; attaches standard labels; collects unique label values via an internal `LabelCollector` for later bulk insert.

### 15.7 Bundle Tool (tools/bundle_tool.py)
Key public methods:
```
load_bundle(path, force=False, options=None) -> BundleSummary
active_context() -> ActiveContext | None
ingest_status() -> dict  # MVP always idle
unload_bundle(bundle_id=None, purge_all=False) -> dict {unloaded, purged}
```
Flow (load_bundle): resolve path → compute bundle_hash → idempotency check → parse IDENT → instantiate parser → stream batches (size/env controlled) to VictoriaMetrics → accumulate counts & label values → transaction: insert bundle + set active + bulk label index → append provenance / parse warnings → return summary (docs static).

### 15.8 Docs Tool (tools/docs_tool.py)
```
search_docs(query, k=5, filter=None, page_token=None) -> {results, next_page_token?}
get_doc(doc_id) -> {doc_id, title, content, metadata}
```
Pagination: simple base64 offset token. Docs ingested once per process (provenance hash) by BundleTool bootstrapping.

### 15.9 Metrics Tool (tools/metrics_tool.py)
```
list_metrics(prefix=None, limit=50, page_token=None)
list_label_values(metric, label, limit=100, page_token=None)
metric_metadata(metric)
query_metrics(expr, start, end, step_ms, max_series=None)
graph_timeseries(metrics, start, end, step_ms, functions=None, align_mode='drop_missing')
graph_compare(primary, comparators, start, end, step_ms, normalize=False)
```
Graph methods construct PromQL expressions, fetch series, align timestamps, apply requested transforms (graphs/transforms.py).

### 15.10 MCP Adapter
`mcp_adapter/schemas.py`: Pydantic models per request/response (serialization boundary only). `dispatcher.py` maps tool names (e.g., `support.load_bundle`) to internal methods via a wrapper performing validation and error translation. `server.py` implements the MCP protocol transport (HTTP listener) and invokes dispatcher.

### 15.11 Concurrency & Ingestion
Phase 1 synchronous ingestion (request blocks until done). Placeholder `IngestTask` structure reserved for future async mode (thread-based) with status polling. Current `get_ingest_status` always returns `state=idle` immediately after load.

### 15.12 Dependency Injection & Configuration
Factory wiring in server startup:
```
config = load_config()
store = SQLiteStateStore(config.sqlite_path)
vm_client = VictoriaMetricsClient(config.vm_url)
# vector_store = ExternalVectorStore(config.vector_db_url)  # future conditional; current mode uses in-memory embeddings
bundle_tool = BundleTool(store, parser_factory, vm_client, chroma_store)
docs_tool = DocsTool(store, chroma_store)
metrics_tool = MetricsTool(store, vm_client)
dispatcher = MCPDispatcher(bundle_tool, docs_tool, metrics_tool)
```
Environment variables enumerated in §14.6.

### 15.13 Logging & Observability
Structured JSON logs: fields (ts, level, component, bundle_id?, sptid?, message, duration_ms?). Health endpoint: checks SQLite writable, VictoriaMetrics `/health`, Chroma collection existence.

### 15.14 Testing Strategy (Expanded)
Unit tests for parser, state store, bundle tool (idempotency), metrics graph alignment, docs search fallback. Integration test: load sample log → list_metrics → query_metrics → search_docs. Error path tests for invalid expressions, missing bundle, reused bundle scenario.

### 15.15 Edge Cases
- Empty logs → metrics_ingested=0 (not error).
- Missing IDENT → host_id/ptop_version set to "unknown".
- Chroma unreachable → warn during load; docs operations later raise BACKEND_ERROR.
- Partial parse warnings aggregated into BundleSummary.warnings.
- Tenant unload purges label_index for that bundle preventing stale high-cardinality residue.

### 15.16 Extension Points
| Extension | Mechanism |
|-----------|-----------|
| Postgres migration | Implement new StateStore; adjust config |
| External VM | Override VM_WRITE_URL; disable internal startup script |
| Alternate vector DB | New adapter implementing search/get/upsert |
| Async ingestion | Add IngestManager + update BundleTool load path |
| Extra graph ops | Register transform in graphs/transforms.py |

### 15.17 Implementation Order
1. core (types, errors, config, logging)
2. sqlite_store
3. vm_client & chroma_client
4. parser & hashing
5. bundle_tool (basic ingestion)
6. docs_tool
7. metrics_tool (list/query)
8. graphs (spec + transforms + graph methods integration)
9. MCP adapter (schemas + dispatcher + server)
10. Tests & sample fixtures

### 15.18 Risk & Mitigation Summary
| Risk | Mitigation |
|------|------------|
| Over-coupling tools to MCP transport | Keep tools pure Python, transport only in adapter |
| Idempotency race (simultaneous loads) | SQLite UNIQUE(bundle_hash) + transaction rollback detection |
| Large memory during parse | Streaming batches; no full retention of samples |
| Label cardinality explosion (TOP) | max_top_execs option + PTOPS intrinsic top filtering |
| Future backend swap friction | Protocol interfaces for StateStore, vector & VM clients |

This low-level design is now the implementation blueprint; deviations should be documented with rationale in commit messages and (if structural) reflected here.

## 16. Documentation Embeddings (Static Artifact – Expanded Design)

Decision (unchanged): Use a prebuilt static embedding artifact (Option B) for now. We postpone implementation (data collection phase first) but fully specify the artifact so downstream work is unambiguous. No runtime embedding generation in MVP; ingestion path only hydrates if empty.

### 16.1 Granularity Levels (L1–L4)
We will persist four distinct granularities to maximize retrieval quality:

| Level | Scope | ID Pattern | Purpose | Cardinality (est.) |
|-------|-------|-----------|---------|--------------------|
| L1 | Per metric field row (each table row in metrics doc) | field:<record_type>:<metric_name> | Precise resolution of single metric semantics | ~400–600 |
| L2 | Per plugin summary (Description + Notes + Metric Kind list) | plugin:<record_type> | Broad topical / exploratory queries | ~20–40 |
| L3 | Alias / legacy mapping clusters (e.g., NET rk/tk, DISK kib→kb) | alias:<primary_metric_name> | Bridge legacy user queries to canonical fields | ~10–20 |
| L4 | Cross-cutting design concepts (label taxonomy, GraphSpec, ingestion pipeline, histogram strategy) | concept:<slug> | Supports "how does ingestion work" or "what is metric_category" queries | ~10–15 |

All four levels are embedded. Search combines: first retrieve top-k across unified collection (with optional filter biasing toward L1 for metric-like queries), then optionally re-rank (future).

### 16.2 Source Materials & Provenance Capture
Primary sources:
1. `ptop/ptop_plugin_metrics_doc.md` (authoritative field rows & plugin sections).
2. `mcp_server/docs/ptops_integration_design.md` (this design; only selected conceptual subsections for L4).

Provenance fields recorded per embedded doc (before hashing):
```
provenance: {
  source_file: "ptop_plugin_metrics_doc.md" | "ptops_integration_design.md",
  section_anchor: <markdown heading or table anchor>,
  record_type?: <CPU|DISK|...>,
  original_tokens?: [ raw table tokens for L1 ],
  origin_field?: <origin column value>,
  computation?: <computation formula>,
  semantics?: <semantics text>,
  notes_excerpt?: <notes cell or plugin notes excerpt>,
  external_command?: <command path if field derives from external tool>,
  extracted_at: <iso8601> 
}
```

If a metric depends on a custom / external command pipeline (e.g., fastpath CLI invocations) and we lack a definitive provenance explanation at build time, we insert a placeholder `external_command:"UNKNOWN"` and flag it in a build report so SMEs can supply the missing details before finalizing the artifact. (We can request that info explicitly—see action item below.)

### 16.3 Static Embedding Artifact Format
JSONL file: `/config/docs_embeddings.jsonl`.
Each line:
```
{
  "id": "field:DISK:disk_read_kb_per_sec",
  "level": "L1",
  "text": "<embedding text chunk>",
  "metadata": {
    "record_type": "DISK",
    "metric_name": "disk_read_kb_per_sec",
    "metric_kind": "rate",
    "metric_category": "disk",
    "legacy_aliases": ["disk_read_kib_per_sec"],
    "synonyms": ["read throughput","disk read kb/s"],
    "version": "2025-08-27",
    "provenance_hash": "sha256:<hash>",
    "canonical": true,
    "level": "L1"
  },
  "embedding": [ <float>, ... ]
}
```
Rules:
* L2/L3/L4 use analogous schema with `metric_name` null (except L3 where one canonical metric anchors cluster) and `canonical=false` for alias entries.
* `provenance_hash` = SHA256 of canonicalized JSON of {id, level, normalized_text, provenance subset}.
* `normalized_text` includes:
  - Title line (e.g., "Metric disk_read_kb_per_sec (DISK)")
  - Canonical description synthesized: semantics + computation + origin + category + kind + any alias pointer.
  - Legacy alias lines (for L1 containing alias list if present).
  - For L3 alias cluster: enumerated mapping table.

### 16.4 Text Normalization & Chunking (Confirmed)
Adopt previously outlined normalization: lowercase copy + original case, unit expansion ("kb/s" -> "kb per second"), underscore and space token variants, abbreviation expansions (rps->requests per second). Hard cap 2048 chars per doc (current content far below). No splitting required at current scale.

### 16.5 Build Pipeline (Planned – Not Implemented Yet)
Script `scripts/build_docs_embeddings.py` steps:
1. Parse metrics doc into structured rows (state machine scanning tables).
2. Generate L1 docs from each row (skip type=label rows? NO: include label rows with kind=label for disambiguation; they help queries like "what is bucket label").
3. Generate L2 plugin summaries: description + notes + metric kind classification list.
4. Generate L3 alias clusters (NET, DISK currently; future additions appended without breaking existing IDs).
5. Generate L4 conceptual docs from selected headings in this design: Goals & Scope (summary), Metric Category Mapping, GraphSpec schema, Ingestion Pipeline (7.5.2 summary), Label Taxonomy, Histogram strategy, Embedding design (this section) for meta self-description.
6. Normalize & compute provenance hashes.
7. Embed (offline) using chosen model (initial: higher-quality offline model e5-base or bge-small-en). Store vectors.
8. Emit JSONL; produce build report (counts per level, missing provenance, alias coverage) stored at `/config/docs_embeddings_report.json`.

### 16.6 Hydration & Runtime (Unchanged Mechanics)
Startup hydration remains: if collection empty → load artifact; else verify sample of `provenance_hash`. Mismatch yields warning `docs_version_mismatch`. No dynamic additions in MVP.

### 16.7 Alias & Legacy Handling (Consolidated Reference)
Current alias clusters (must be reflected in artifact):
* NET rate metrics: rk/tk/rd/td + kib → normalized rx/tx + kb (see plugin net Legacy Naming & Aliases table).
* DISK throughput: *_kib_per_sec → *_kb_per_sec.
During artifact build, for each canonical L1 metric with aliases:
 - Add `legacy_aliases` list.
 - Add synonyms tokens into `synonyms` (alias variants plus decomposed forms).
 - Create L3 alias cluster document enumerating mapping (one per primary canonical root metric group).

### 16.8 Quality & Accuracy Verification Plan
Before shipping artifact version, run automated checks:
| Check | Method | Pass Criteria |
|-------|--------|---------------|
| Field coverage | Count parsed table rows vs L1 docs | 100% match |
| Alias resolution | For each legacy name ensure appears in either L1 `legacy_aliases` or an L3 doc | 100% |
| Provenance completeness | External-command fields without origin flagged | 0 UNKNOWN entries (or documented exceptions) |
| Deterministic hash | Re-running builder yields identical global hash | Stable hash |
| Sample semantic retrieval | Gold query set recall@5 >= target (manual threshold set later) | >= baseline |

Build report enumerates failures and blocks artifact publication if any strict criteria fails (except retrieval metrics which are advisory initially).

### 16.9 Custom / External Command Provenance
Some fastpath or DOH/DOT/TCP_DCA derived metrics rely on wrapper commands (e.g., `fp-cli`). For those we will:
1. Capture command string under `external_command`.
2. If explanation (semantics of command’s output transformation) is not available, include placeholder text: "Provenance explanation pending – requires SME input".
3. Mark such docs with metadata flag `needs_provenance_review=true` so they can be queried explicitly (`needs_provenance_review:true`) for remediation.
SME Action Item: Provide detailed provenance narrative for any `UNKNOWN` entries prior to finalizing artifact version `2025-08-XX`.
\n+Status Update: Initial detailed provenance for DOT / DOH / TCP_DCA session & packet fields has been captured in §18.13 (rx, tx, dp, qd, os, cs, as). Remaining function‑level attributions for DOT/TCP (where not explicitly enumerated in code comments) are still subject to confirmation.

### 16.10 Global Hash & Versioning
Define `GLOBAL_PROVENANCE_HASH` = SHA256(sorted list of per-doc `provenance_hash`). This plus `DOCS_EMBED_VERSION` stored in SQLite and used for drift detection. Increment version when either source doc changes structurally (new/removed fields) or embedding model changes.

### 16.11 Deferred (Future Enhancements)
* Hybrid retrieval (vector + lexical re-rank) – design placeholder, not implemented now.
* Dynamic per-bundle context (see earlier 16.5 original) – still deferred.
* User-added annotations (L5 potential) – out of scope.

### 16.12 Summary of Current Status
Design finalized for artifact structure; implementation intentionally deferred pending data collection & provenance completion. No code changes yet; this document now serves as the authoritative spec for the embedding build script.

Action Needed (Outside This Commit): Provide any missing external command provenance details; curate initial gold query set for evaluation.

## 17. MVP Implementation Checklist

Core (Phase 1):
- [ ] Core modules (types, errors, config, logging)
- [ ] SQLiteStateStore (bundles, active_context, label_index CRUD, purge on unload)
- [ ] PTOPS parser (CPU, DISK, NET, TOP, TIME, IDENT, DBWR minimal) + label collector + error stats
- [ ] VictoriaMetrics client (write/query) with retry/backoff
- [ ] BundleTool (idempotent load, parse threshold, provenance warnings)
- [ ] Static embeddings hydration & DocsTool (search/get)
- [ ] MetricsTool (list_metrics, list_label_values, metric_metadata, query_metrics, graph_timeseries, graph_compare)
- [ ] Tenant label guard (inject + mismatch rejection)
- [ ] Tests: parser unit, golden NDJSON, idempotent concurrency, docs search
- [ ] Entrypoint script & health endpoint

Deferred (Phase 2+):
- [ ] Additional graph endpoints (scatter, histogram_dbwr, topk, pointset)
- [ ] Context snippet embeddings
- [ ] Correlation & anomaly endpoints
- [ ] Postgres StateStore adapter
- [ ] Async ingestion mode

Operational Nice-to-haves:
- [ ] Structured JSON logging with correlation ids
- [ ] Internal self-metrics (ingest batch count, parse error rate)
- [ ] README quickstart & container run example

---

## 18. Product Architecture: Fast Path, Exception Path & DNS/Data Plane Flow (Embedding Concept L4)

### 18.1 Overview
Certain feature combinations (VDCA, ADC, DOT, DOH) enable an accelerated "fast path" dataplane implemented as a dedicated DPDK process `fp_rte`. This process offloads select network interfaces (e.g., `eth1`, `eth2`, `eth3` ...) from the Linux kernel IP stack, binding them directly via the 6WIND DPDK stack for low‑latency packet processing and DNS response caching. When the fast path is disabled or not present on an installation, Linux kernel networking processes packets directly from the NICs; all logic (DNS resolution, DHCP, other services) then occurs on the traditional code path without acceleration.

### 18.2 Components
| Component | Type | Role | Notes |
|-----------|------|------|-------|
| fp_rte | Primary DPDK process | Fast path runtime (packet I/O, cache, queue & session management) | Owns NIC queues, bypasses kernel for bound ports. |
| 6WIND DPDK stack | Library/runtime | Provides high‑performance poll‑mode drivers and network stack primitives | Abstracts hardware specifics. |
| virtio_net exception path | Virtual interface / channel | Tunnel / queue bridging packets between fast path and Linux kernel when cache miss or non‑accelerated traffic occurs | Maintains routing & forwarding synchronization. |
| Routing table sync module | Control-plane sync | Mirrors Linux routing table (FIB) and relevant policy into DPDK fast path | Ensures cache & forwarding decisions align with system routes. |
| DNS cache (fast path) | In‑memory structure | Stores recently resolved DNS answers for direct response | Hit returns immediately to client; miss triggers exception path. |
| BIND | Resolver / authoritative daemon | Handles cache misses (recursive resolve or forward) and authoritative answers for local zones | Returns response which is streamed back to fast path for optional caching. |
| ADP (Suricata binary) | Secondary DPDK process | DNS server protection: rate limiting, signature / rule based blocking / dropping malicious queries or responses | Runs only when enabled; attaches to fast path data. |
| DOT / DOH modules | Protocol handlers | Provide DNS over TLS / HTTPS termination/processing possibly within or adjacent to fast path | Their enablement triggers inclusion of fast path metrics (DOT_STAT / DOH_STAT). |
| VDCA / ADC feature flags | Configuration toggles | Enable value‑added DNS acceleration / security capabilities | Drive whether fp_rte and ADP spawn. |
| DHCPD and other Linux daemons | Kernel/userland services | Process DHCP and non-DNS traffic on the exception path | Always Linux‑side; unaffected by cache hits. |

### 18.3 Data Flow (DNS Query Lifecycle)
1. Packet ingress on accelerated interface bound to fp_rte.
2. Fast path classifier extracts 5‑tuple + DNS header → checks internal DNS response cache.
3. Cache Hit: Response crafted directly from cached RRSet and returned to client (bypass Linux). Optionally updates hit counters and latency stats.
4. Cache Miss: Packet forwarded over virtio_net exception path into Linux kernel network stack.
5. BIND receives query: either performs recursive resolution (using forwarders if configured) or returns authoritative data for local zones.
6. BIND emits response which is streamed back to fast path (virtio path) for:
  - Optional insertion into fast path DNS cache (subject to TTL, policy, ADP decisions).
  - Transmission out original fast path interface to client.
7. If ADP enabled, Suricata-based rules may rate limit, drop, or tag queries/responses (e.g., suspected amplification, malformed, signature match) at either pre-cache (ingress) or post-BIND (egress) inspection points.

### 18.4 Exception Path Semantics
The term "exception path" designates the fallback data plane crossing from fast path to the Linux network stack. All non-DNS traffic (e.g., DHCP, management protocols) traverses exception path by default. If only ADP is enabled (without broader fast path acceleration), all DNS traffic is forwarded via exception path for protection & resolution—fast path does not deliver direct cache hits in that mode.

### 18.5 Deployment Modes
| Mode | fp_rte | ADP | DNS Cache | Cache Hits Served | Traffic Path Summary |
|------|-------|-----|-----------|-------------------|----------------------|
| Baseline (no fast path) | Disabled | Disabled | N/A | 0% | All packets handled by Linux kernel/BIND directly. |
| Fast Path Only | Enabled | Disabled | Enabled | High (subject to workload & TTL) | Cache hits in fast path; misses via exception path to BIND. |
| Fast Path + ADP (Full) | Enabled | Enabled | Enabled | High | ADP inspects; cache hit short circuit; misses go to BIND, responses re‑inspected/cached. |
| ADP Only | Disabled | Enabled | Disabled | 0% | All DNS flows through exception path; ADP inspects before BIND. |

### 18.6 Metrics Mapping (Current & Planned)
Existing or partially implemented fast path related PTOPS prefixes:
| Prefix | Relationship | Example Metrics (canonical) | Notes |
|--------|-------------|-----------------------------|-------|
| DOT_STAT / DOH_STAT | Protocol termination stats | dot_queries_total, doh_queries_total (future normalization) | Reflects secure transport DNS query counts / sessions. |
| TCP_DCA_STAT | TCP DNS connection acceleration stats | tcp_dca_rx_packets_total, tcp_dca_active_sessions | Sessions & packet handling in acceleration context. |
| FPPORTS | Fast path port counters | fpports_<counter>_total | Per-port packet / drop counters. |
| FPMBUF | Memory buffer pool stats | fpm_alloc_fail_total, fpm_free_buffers | Buffer pressure & pool health. |
| FPC | Fast path CPU usage | fpc_cycles_total, fpc_busy_percent | Performance capacity and per-core utilization. |
| (Planned) FPVI | Virtual interface ingress/egress | fpvi_rx_packets_total, fpvi_tx_packets_total | Will expose exception path bridging volumes. |
| (Planned) ADP | Protection stats | adp_blocked_queries_total, adp_rate_limited_queries_total | Derived from Suricata rule matches. |

Alias/legacy naming considerations (see Section 16.7) also apply to any future fast path throughput metrics (e.g., ensure kb vs kib consistency early).

### 18.7 Operational Insights via Metrics
Sample correlation queries analysts might run (justifying embedding detail):
| Question | Series / Labels Needed |
|----------|------------------------|
| Are cache hit ratios degrading? | (fast path cache hit counter)/(hit+miss) – planned counters. |
| Is ADP causing elevated drops? | adp_blocked_queries_total delta vs fpports drops. |
| Are exception path queues saturated? | fpvi_rx_packets_total rate vs cpu_user_percent (Linux cores). |
| Are secure transports (DOH/DOT) dominating traffic? | doh_queries_total vs dot_queries_total vs total DNS queries. |
| Is packet processing CPU bound? | fpc_busy_percent vs net_rx_packets_per_sec. |

### 18.8 Provenance & External Commands
Some fast path stats originate from helper CLIs (e.g., `fp-cli ib_dca get doh_stats_ptop`). For each such metric cluster we record:
| Field | Example Value |
|-------|---------------|
| external_command | `/usr/bin/fp-cli fp ib_dca get doh_stats_ptop` |
| collection_method | "pipe_read+parse" |
| reliability | "best_effort" (fallback if CLI unavailable) |
| needs_provenance_review | false (set true if semantics unclear) |

Missing provenance entries are flagged during embedding artifact build (Section 16.9) for SME completion prior to release.

### 18.9 Failure & Degradation Modes
| Scenario | Symptom | Likely Metrics Signal | Potential Mitigation |
|----------|---------|-----------------------|---------------------|
| Cache disabled unexpectedly | Increased latency, higher BIND CPU | Drop in (planned) cache hit counter; stable/high BIND related CPU | Restart fp_rte; validate feature flags. |
| ADP overload | Elevated drops incl. benign queries | Spike in adp_rate_limited_queries_total + high fpc_busy_percent | Tune rules / adjust rate thresholds. |
| Virtio exception congestion | Increased end-to-end latency | High fpvi queue depth (planned) & rising net_tx_drops_per_sec | Rebalance cores; reduce per-query work. |
| Buffer exhaustion | Packet loss on fast path | fpm_alloc_fail_total rising | Increase pool size or fix leak. |

### 18.10 Embedding Focus
This section will produce:
* L4 concept doc `concept:fast_path_architecture` summarizing components & flow.
* L4 concept doc `concept:fast_path_metrics_mapping` enumerating current/planned metric mappings.
* Future L1 field docs will link back via metadata `related_concepts=["fast_path_architecture"]` to unify semantic retrieval.

### 18.11 Future Extensions
Planned metrics (not yet parsed): cache_hit_total, cache_miss_total, fpvi_queue_depth, adp_* counters. Parser & docs must be updated before embedding artifacts include them — they will be version gated by `metric_name` presence.

### 18.12 Summary
Fast path introduces an accelerated DNS servicing layer with optional security (ADP). Embedding its architecture ensures user queries like "why are rk drops high when ADP is enabled" can be semantically associated with protective dropping logic and exception path congestion, improving automated explanations.

---

### 18.13 Metrics Field Definitions Relocated
All per-listener fast path session packet counters (DOT_STAT, DOH_STAT, TCP_DCA_STAT: rx, tx, dp, qd, os, cs, as) and their canonical metric mappings now reside in `ptop_plugin_metrics_doc.md` under their respective plugin sections. This design document no longer duplicates field-by-field semantics; see metrics reference for authoritative naming, units, and provenance metadata.





### 18.14 Metrics Field Definitions Relocated (FPMBUF)

Command:
`/usr/bin/fp-cli fp ib_dis get mbuf_stats ptop`

(If the path segment `ib_dis` is a typo or variant, capture the exact executed command string in provenance; adjust once confirmed.)

Output Tokens (observed): `muc` `mac` `pmu`

Field Semantics (Authoritative):
| Token | Canonical Metric (planned) | Type | Unit | Derivation / Increment Semantics | Provenance Functions | Notes |
|-------|---------------------------|------|------|----------------------------------|----------------------|-------|
| muc | fpm_mbufs_in_use | gauge | mbufs | Current allocated (checked‑out / in‑flight) objects: value of `rte_mempool_in_use_count(mp_local)` at sampling time | `rte_mempool_in_use_count` | Represents instantaneous usage; not cumulative. Legacy alias: muc. |
| mac | fpm_mbufs_available | gauge | mbufs | Current free objects remaining in pool: `rte_mempool_avail_count(mp_local)` | `rte_mempool_avail_count` | Despite label, NOT cumulative allocated. Alias: mac. |
| pmu | fpm_mbuf_utilization_percent | gauge | percent | `(muc / (muc + mac)) * 100` computed at emit (two decimal precision). Denominator `(muc+mac)` is total pool size. | Derived from muc & mac | Alias: pmu. Consider also emitting raw pool size for clarity (future). |

Planned Additional Derived Metric (optional future):
| Proposed Metric | Type | Rationale |
|-----------------|------|-----------|
| fpm_mbuf_pool_size | gauge | Helpful to make denominator explicit for consumers; equals muc+mac at sample time. |

Canonical Naming Rationale:
- Prefix `fpm_` chosen to align with existing FPMBUF metrics (e.g., `fpm_alloc_fail_total`) while keeping mbuf concepts grouped.
- Use descriptive suffixes (`_in_use`, `_available`, `_utilization_percent`) instead of raw abbreviations to improve semantic retrieval and reduce ambiguity between counts vs percentages.
- Percent metric keeps `_percent` suffix consistent with broader naming convention.

Embedding & Alias Handling:
1. Each metric will have L1 doc with `legacy_aliases` set to the CLI token (muc/mac/pmu).
2. A single L3 alias cluster `alias:fpm_mbuf_pool` will map tokens → canonical names.
3. Include provenance functions (`rte_mempool_in_use_count`, `rte_mempool_avail_count`) in metadata for muc/mac; pmu lists `derived:true` and `depends_on:[fpm_mbufs_in_use,fpm_mbufs_available]`.

Provenance JSON Snippet Example (muc):
```json
{
  "id": "field:FPMBUF:fpm_mbufs_in_use",
  "level": "L1",
  "metadata": {
    "record_type": "FPMBUF",
    "metric_name": "fpm_mbufs_in_use",
    "metric_kind": "gauge",
    "metric_category": "fast_path",
    "legacy_aliases": ["muc"],
    "external_command": "/usr/bin/fp-cli fp ib_dis get mbuf_stats ptop",
    "provenance_functions": ["rte_mempool_in_use_count"],
    "planned": true,
    "needs_provenance_review": false
  },
  "text": "Fast path mempool mbufs in use (current allocated objects). Derived from rte_mempool_in_use_count(mp_local). Alias token: muc. Collected via fp-cli mbuf_stats command."
}
```

Status & Actions:
- Parser Support: Not yet implemented (planned). Add recognition of mbuf_stats line prefix or integrate via existing FPMBUF parsing path.
- Add to §18.6 Metrics Mapping once implemented (row group under FPMBUF). Current entry already lists FPMBUF examples; will expand with these three + optional pool size.
- Consider emission of `fpm_mbuf_pool_size` to avoid downstream re-summation; if omitted, embedding doc will describe computation explicitly.

Consistency Checks (Pre-implementation):
- Verify pool size stability: ensure `(muc+mac)` equals configured mempool size; if dynamic resizing is possible (unlikely), document variability.
- Confirm that multiple mempools (per core / NUMA) are aggregated prior to CLI print; if per-pool lines exist, incorporate `pool_id` label.

Open Questions:
1. Are there failure counters (e.g., allocation failures) tied to the same CLI that we should capture concurrently to contextualize high utilization? (Potential existing `fpm_alloc_fail_total`).
2. Are there multiple mempool classes (e.g., small vs large mbufs) requiring disambiguation labels? If yes, extend canonical metric with `{pool_type="small"|"large"}` label.

Once sample output is captured and parser changes merged, update aggregation status in §16.9 (External Command Provenance) to mark FPMBUF mbuf_stats tokens as fully covered.

### 18.15 Metrics Field Definitions Relocated (FPPORTS)

Command:
`/usr/bin/fp-shmem-ports -S`

Description:
Reads fast path shared memory region containing per‑port DPDK `rte_eth_stats` structures and emits one compact line per port beginning with `FPPORTS`. Each token is a shortened alias of a struct member.

Tokens → `rte_eth_stats` Mapping:
| Token | Struct Member | Canonical Metric (planned) | Type | Unit | Semantics | Notes |
|-------|---------------|----------------------------|------|------|----------|-------|
| ip | ipackets | fpports_rx_packets_total | counter | packets | Total successfully received packets | Monotonically increasing. |
| op | opackets | fpports_tx_packets_total | counter | packets | Total successfully transmitted packets | Monotonically increasing. |
| ib | ibytes | fpports_rx_bytes_total | counter | bytes | Total received bytes | Consider future kb/sec rate derivation separately. |
| ob | obytes | fpports_tx_bytes_total | counter | bytes | Total transmitted bytes | — |
| ie | ierrors | fpports_rx_errors_total | counter | packets | Total RX errors (driver reported) | Aggregated error categories. |
| oe | oerrors | fpports_tx_errors_total | counter | packets | Total TX errors | — |
| mc | imcasts | fpports_rx_multicast_packets_total | counter | packets | Total received multicast packets | Subset of ip. |
| im | imissed | fpports_rx_missed_packets_total | counter | packets | RX packets missed by HW (overflow) | Loss category. |
| in | rx_nombuf | fpports_rx_nombuf_drops_total | counter | packets | RX drops due to no mbufs available | Indicates buffer pressure; correlates with fpm_mbuf_utilization. |

Relationship / Loss Insight:
`ip` counts successful receptions; `im` (missed) and `in` (no mbuf) represent loss categories not included in `ip`. Therefore `(ip + im + in)` can exceed hardware ingress attempt counts because `im` and `in` are not successes. For packet loss rate analysis, compute `(im + in) / (ip + im + in)` over interval.

Labeling Plan:
- Add `port` label (e.g., numeric or interface name as exposed by CLI line) to each emitted sample.
- If driver exposes human-friendly port name separate from numeric ID, include second label `port_name` (future – needs CLI enhancement).

Embedding & Alias Handling:
1. Each planned canonical metric will L1 embed with `legacy_aliases` = original short token (ip, op, etc.).
2. Create L3 alias cluster `alias:fpports_port_counters` enumerating all token→canonical mappings.
3. Provide cross-concept links to `concept:fast_path_architecture` and planned mempool utilization docs (for `in` correlation).

Provenance JSON Snippet Example (ip):
```json
{
  "id": "field:FPPORTS:fpports_rx_packets_total",
  "level": "L1",
  "metadata": {
    "record_type": "FPPORTS",
    "metric_name": "fpports_rx_packets_total",
    "metric_kind": "counter",
    "metric_category": "fast_path",
    "legacy_aliases": ["ip"],
    "external_command": "/usr/bin/fp-shmem-ports -S",
    "provenance_struct": "rte_eth_stats.ipackets",
    "planned": true,
    "needs_provenance_review": false
  },
  "text": "Fast path per-port received packets (successful) from DPDK rte_eth_stats.ipackets. Alias token: ip. Collected via fp-shmem-ports -S (FPPORTS line)."
}
```

Open Items:
- Confirm whether CLI already outputs port identifier (e.g., port=0) – parser will need to extract for label injection.
- Determine if byte counters exceed 64-bit wrap in long uptimes (DPDK typically uses 64-bit) – document wrap handling if needed.

Post-Implementation Tasks:
- Update §18.6 Metrics Mapping to add FPPORTS canonical metrics once parser emits them.
- Add utilization / loss rate example queries to §18.7 referencing new counters (e.g., `rate(fpports_rx_missed_packets_total + fpports_rx_nombuf_drops_total) / rate(fpports_rx_packets_total + fpports_rx_missed_packets_total + fpports_rx_nombuf_drops_total)`).

This section finalizes provenance for FPPORTS token mappings and prepares embedding + parser alignment.

### 18.16 Metrics Field Definitions Relocated (FPPRXY)

Command Source:
`/usr/bin/fp-cli fp ib_pxyall get pxyall_client_stats ptop` (Format function: `display_pxyall_client_stats()` in `ib-fp-pxyall-list-buffer.c` – ptop branch). Sample emitted line (one per MSP index):
```
FPPRXY hsc 0 ccs 1 cnc 42 qah 1050 caqh 37 qfah 2 qpb 960 qth 11 rmsp 940 mrsc 930 rfsc 5 rpfse 1 rpfst 0 rpftn 0 rpse 0 rpvnf 0 rrhf 0 rpv6nf 0 rpv4nf 0
```

Token Semantics Provided (authoritative) – includes cumulative vs instantaneous and success vs failure classification.

Design Decisions:
- Treat `hsc` (MSP index) as a label `msp_index` (string/integer) rather than a metric.
- Provide explicit success_category classification for embedding & potential automated health summaries: `success`, `failure`, `timeout`, `fallback`, `state` (for connection state), `derived`.
- Counters are monotonically increasing unless noted (gauges instantaneous).

Canonical Metric Mapping (planned):
| Token | Label/Metric | Canonical Metric Name | Type | Unit | success_category | Instant? | Semantics | Notes / Relationships |
|-------|--------------|-----------------------|------|------|------------------|----------|-----------|------------------------|
| hsc | label | msp_index | label | n/a | state | yes | MSP index (loop variable) | Becomes `msp_index` label for all FPPRXY samples. |
| ccs | metric | fpprxy_connected_cores | gauge | cores | state | yes | Count of cores reporting connection up (sum of `connected` flags) | >0 implies connection established. |
| cnc | metric | fpprxy_reconnects_total | counter | events | mixed | no | Total successful reconnects (lifecycle churn indicator) | Not pure success or failure; reflects connection churn. |
| qah | metric | fpprxy_queries_added_to_hash_total | counter | queries | success | no | Successful query hash insertions | Success path; paired with qfah & caqh. |
| caqh | metric | fpprxy_active_queries | gauge | queries | state | yes | Current active queries in hash (live entries) | Instantaneous occupancy. |
| qfah | metric | fpprxy_query_add_failures_total | counter | queries | failure | no | Failed query hash insert attempts (bucket full / alloc fail) | Partner to qah; use for insertion failure rate. |
| qpb | metric | fpprxy_queries_passed_to_bind_total | counter | queries | fallback | no | Queries diverted to local BIND instead of MSP | Indicates fallback load. |
| qth | metric | fpprxy_query_timeouts_total | counter | queries | timeout | no | Queries aged out of hash table without response | Timeout failure class. |
| rmsp | metric | fpprxy_responses_from_msp_total | counter | responses | success | no | Responses received from MSP | Upstream success reception. |
| mrsc | metric | fpprxy_responses_sent_to_client_total | counter | responses | success | no | MSP responses forwarded to client | Subset of rmsp (successful send). |
| rfsc | metric | fpprxy_response_send_failures_total | counter | responses | failure | no | Failures sending response to client | Compare vs mrsc. |
| rpfse | metric | fpprxy_response_parse_status_failures_total | counter | responses | failure | no | Parse failures (status / generic error) | One parse failure category. |
| rpfst | metric | fpprxy_response_parse_txid_failures_total | counter | responses | failure | no | Parse failures (subs/txid mismatch) | Distinct failure reason. |
| rpftn | metric | fpprxy_response_txid_not_found_total | counter | responses | failure | no | Response txid not found in hash | Lookup failure. |
| rpse | metric | fpprxy_response_subid_empty_total | counter | responses | failure | no | Response had empty subid | Anomaly. |
| rpvnf | metric | fpprxy_response_vip_not_found_total | counter | responses | failure | no | VIP not found | Lookup/config failure. |
| rrhf | metric | fpprxy_response_hash_remove_failures_total | counter | responses | failure | no | Failures removing hash entry | Resource/logic issue; ensure not accumulating rapidly. |
| rpv6nf | metric | fpprxy_response_pvipv6_not_found_total | counter | responses | failure | no | pvipv6 parse/lookup failure | IPv6-specific. |
| rpv4nf | metric | fpprxy_response_pvipv4_not_found_total | counter | responses | failure | no | pvipv4 parse/lookup failure | IPv4-specific. |

Aggregations & Derived Ratios (to document in embedding L4 or metric help):
- Query insertion failure rate = qfah / (qah + qfah).
- Query timeout rate = qth / (qah + qpb) (approx – refine if fallback queries excluded from timeout denominator).
- Response send failure rate = rfsc / (mrsc + rfsc).
- Overall response parse failure rate = (rpfse + rpfst + rpftn + rpse + rpvnf + rpv6nf + rpv4nf) / rmsp.
- Active query load factor = caqh relative to configured hash capacity (capacity metric not yet exposed – consider future `fpprxy_query_hash_capacity`).

Embedding / Alias Strategy:
1. Each counter/gauge will have L1 doc with `legacy_aliases` containing original token.
2. L3 alias cluster `alias:fpprxy_proxy_stats` enumerates token → canonical name mapping and groups failure categories.
3. Add metadata: `success_category` for quick semantic grouping (enables retrieval for queries like "proxy failures" or "proxy timeouts").

Provenance JSON Snippet Example (qah):
```json
{
  "id": "field:FPPRXY:fpprxy_queries_added_to_hash_total",
  "level": "L1",
  "metadata": {
    "record_type": "FPPRXY",
    "metric_name": "fpprxy_queries_added_to_hash_total",
    "metric_kind": "counter",
    "metric_category": "fast_path",
    "legacy_aliases": ["qah"],
    "external_command": "/usr/bin/fp-cli fp ib_pxyall get pxyall_client_stats ptop",
    "success_category": "success",
    "planned": true,
    "needs_provenance_review": false
  },
  "text": "Total queries successfully inserted into proxy aggregate hash table (MSP path). Monotonic counter; alias token: qah. Used with qfah to compute insertion failure rate." 
}
```

Open Items / Future Considerations:
- Expose hash capacity to support saturation metrics (will enable `fpprxy_query_hash_capacity` gauge).
- Consider collapsing fine-grained parse failure counters into grouped categories if cardinality or user comprehension becomes an issue (retain raw counters but provide aggregated derived metrics in docs).
- Determine if reconnect events correlate with elevated failure rates (potential anomaly heuristic: spike in `reconnects_total` + simultaneous increase in parse failures).

Post-Implementation Updates:
- Update §18.6 Metrics Mapping adding FPPRXY group after parser support.
- Add retrieval examples to §18.7 demonstrating failure rate computations.

Classification Summary (authoritative):
- Instantaneous (gauges): ccs (fpprxy_connected_cores), caqh (fpprxy_active_queries)
- Cumulative monotonic counters (reset only via `reset_pxyall_client_stats()`): all others (cnc, qah, qfah, qpb, qth, rmsp, mrsc, rfsc, rpfse, rpfst, rpftn, rpse, rpvnf, rrhf, rpv6nf, rpv4nf)

Success Path Counters: qah, qpb, rmsp, mrsc
Failure / Error / Timeout Counters: qfah, qth, rfsc, rpfse, rpfst, rpftn, rpse, rpvnf, rrhf, rpv6nf, rpv4nf
Mixed / Neutral Lifecycle: cnc (reconnects)

Status: All token semantics & classifications covered with updated success/mixed delineation; ready for parser & embedding inclusion (planned metrics stage).

### 18.17 Metrics Field Definitions Relocated (FPPREF)

Command Source:
`/usr/bin/fp-cli fp ib_pcp_prefetch get pcp_prefetch_stats ptop` (function emitting stats uses variables listed below; sample line pattern):
```
FPPREF ti 47 qc 128 mfat 2 4aqc 123 mf4a 3 ftxn 1 stxn 251 raq 120 r4aq 118
```

All counters are cumulative (monotonic) until `clear_pcp_prefetch_stats()` resets them; none are instantaneous gauges.

Token Mapping & Semantics (Authoritative):
| Token | Variable | Increment Site | Actual Meaning | Notes |
|-------|----------|----------------|----------------|-------|
| ti | pcp_timer_iterations | end of `create_pcp_cname_query()` timer loop | Number of prefetch timer iterations executed (operational progress) | Increments even if `pcp_flag` == 0. |
| qc | a_type_pcp_query_created | `fill_dns_a_type_load_and_send_to_host()` (mbuf alloc success) | Successful A (CNAME A) prefetch queries constructed | Success counter. |
| mfat | m_buf_failure_for_a_type | `fill_dns_aaaa_type_load_and_send_to_host()` (mbuf alloc fail) | AAAA query mbuf allocation failures | Name misaligned: counts AAAA failures. |
| 4aqc | quad_a_type_pcp_query_created | `fill_dns_aaaa_type_load_and_send_to_host()` (alloc success) | Successful AAAA (quad A) prefetch queries constructed | Success counter. |
| mf4a | m_buf_failure_for_quad_a_type | `fill_dns_a_type_load_and_send_to_host()` (mbuf alloc fail) | A query mbuf allocation failures | Name misaligned: counts A failures. |
| ftxn | pcp_query_sent_failure | `forward_dns_pcp_query_to_host()` (enqueue returns 0) | Enqueue/transmit failures (A or AAAA) | Failure counter. |
| stxn | pcp_query_sent_success | `forward_dns_pcp_query_to_host()` (enqueue success) | Successfully enqueued prefetch queries (A + AAAA) | Success counter. |
| raq | pcp_response_for_a_type_query | `extract_pcp_prefetch_response()` (on A response success) | Successful handled A prefetch responses | Success counter. |
| r4aq | pcp_response_for_quad_a_type_query | `extract_pcp_prefetch_response()` (on AAAA response success) | Successful handled AAAA prefetch responses | Success counter. |

Success vs Failure Classification:
- Success / Progress: ti, qc, 4aqc, stxn, raq, r4aq
- Failure: mfat, mf4a, ftxn
- Neutral / Mixed: (none additional; ti treated as progress)

Canonical Metric Naming (Planned):
| Token | Canonical Metric | Type | success_category | Rationale |
|-------|------------------|------|------------------|-----------|
| ti | fppref_timer_iterations_total | counter | progress | Explicit “iterations” semantic. |
| qc | fppref_a_query_created_total | counter | success | A query construction success. |
| mfat | fppref_aaaa_query_mbuf_alloc_failures_total | counter | failure | Rename to actual meaning (AAAA alloc failures). Alias preserves mfat. |
| 4aqc | fppref_aaaa_query_created_total | counter | success | AAAA construction success. |
| mf4a | fppref_a_query_mbuf_alloc_failures_total | counter | failure | Actual meaning (A alloc failures). Alias preserves mf4a. |
| ftxn | fppref_query_enqueue_failures_total | counter | failure | Generic enqueue failure for A/AAAA. |
| stxn | fppref_query_enqueued_total | counter | success | Successful enqueue (A+AAAA). |
| raq | fppref_a_response_handled_total | counter | success | A response processed. |
| r4aq | fppref_aaaa_response_handled_total | counter | success | AAAA response processed. |

Alias Strategy & Naming Corrections:
- Maintain original token aliases (mfat, mf4a) but canonical names reflect true semantics (swapped variable usage). Embedding L1 docs include a NOTE describing the historical mismatch.
- Provide L3 alias cluster `alias:fppref_prefetch_stats` mapping raw tokens → canonical metrics plus `alias_note` for mfat/mf4a swap.

Derived Ratios (to document in help / embedding):
- A query build failure rate = fppref_a_query_mbuf_alloc_failures_total / (fppref_a_query_created_total + fppref_a_query_mbuf_alloc_failures_total)
- AAAA query build failure rate = fppref_aaaa_query_mbuf_alloc_failures_total / (fppref_aaaa_query_created_total + fppref_aaaa_query_mbuf_alloc_failures_total)
- Enqueue failure rate = fppref_query_enqueue_failures_total / (fppref_query_enqueued_total + fppref_query_enqueue_failures_total)
- Response success coverage (A) = fppref_a_response_handled_total / fppref_a_query_created_total (approx; ignores timeout path) – similarly for AAAA.

Provenance JSON Snippet Example (mfat / canonical AAAA alloc failures):
```json
{
  "id": "field:FPPREF:fppref_aaaa_query_mbuf_alloc_failures_total",
  "level": "L1",
  "metadata": {
    "record_type": "FPPREF",
    "metric_name": "fppref_aaaa_query_mbuf_alloc_failures_total",
    "metric_kind": "counter",
    "metric_category": "fast_path",
    "legacy_aliases": ["mfat"],
    "external_command": "/usr/bin/fp-cli fp ib_pcp_prefetch get pcp_prefetch_stats ptop",
    "provenance_functions": ["fill_dns_aaaa_type_load_and_send_to_host"],
    "success_category": "failure",
    "alias_note": "Original token name suggests A-type; actually counts AAAA allocation failures (swapped usage).",
    "planned": true,
    "needs_provenance_review": false
  },
  "text": "Prefetch AAAA query mbuf allocation failures (legacy token mfat – historically mislabeled as A-type). Increments when fill_dns_aaaa_type_load_and_send_to_host() mbuf allocation returns NULL." 
}
```

Open Items:
- Confirm no additional hidden counters (e.g., timeouts separate from enqueue failures) exist for prefetch path – if discovered, extend mapping.
- Determine if timer iterations (ti) should also export an interval rate gauge to simplify scheduling anomaly detection (optional derived metric `fppref_timer_iteration_rate` in docs only).

Status: Complete mapping & classification recorded; ready for parser and embedding inclusion (planned stage).

### 18.18 Metrics Field Definitions Relocated (FPDCA)

Command Source:
Likely ptop fast path summary line (compact) or verbose mode from a DCA stats helper; sample compact line:
```
FPDCA dca_pass_to_bind 123 pcp_pass_to_bind 45 dca_non_cacheable_response 67
```
Verbose multi-line equivalent:
```
dca_pass_to_bind: 123
pcp_pass_to_bind: 45
dca_non_cacheable_response: 67
```

All displayed values are cumulative monotonic counters (reset by corresponding clear/reset function if available; name not yet confirmed).

Token / Field Meanings (Authoritative):
| Token | Canonical Metric (planned) | Type | success_category | Semantics | Notes |
|-------|---------------------------|------|------------------|-----------|-------|
| dca_pass_to_bind | fpdca_queries_passed_to_bind_total | counter | decision | Queries the DCA fast path declined to answer and forwarded to BIND (unsupported type, policy, limits). | Indicates fallback load; not a failure. |
| pcp_pass_to_bind | fpdca_pcp_queries_passed_to_bind_total | counter | decision | PCP (prefetch/policy) related queries sent to BIND when fast path cannot synthesize or needs authoritative path. | Helps differentiate generic vs PCP-specific fallback. |
| dca_non_cacheable_response | fpdca_non_cacheable_responses_total | counter | decision | Responses deemed non-cacheable (negative RCODEs, malformed, truncated without retry, zero/negative TTL, unsupported answer composition) and thus not inserted into cache. | Elevated counts may indicate upstream instability or many negative responses. |

Classification:
- All three are action/decision outcome counters (neither explicit success nor failure). Use `success_category=decision` to allow semantic grouping distinct from error or success metrics.

Canonical Naming Rationale:
- Prefix `fpdca_` to align with fast path DCA domain while disambiguating from PCP prefetch (already in FPPREF) and proxy (FPPRXY) metrics.
- Suffix `_total` for cumulative counters; descriptive nouns (`queries_passed_to_bind`, `non_cacheable_responses`).

Embedding & Alias Handling:
1. Each field becomes L1 doc with `legacy_aliases` containing original token.
2. A small L3 alias cluster `alias:fpdca_decision_counters` will map the three tokens to canonical names.
3. Provide retrieval examples linking elevated `fpdca_non_cacheable_responses_total` to potential root causes (negative caching, upstream SERVFAIL storms) once implemented.

Provenance JSON Snippet Example (dca_pass_to_bind):
```json
{
  "id": "field:FPDCA:fpdca_queries_passed_to_bind_total",
  "level": "L1",
  "metadata": {
    "record_type": "FPDCA",
    "metric_name": "fpdca_queries_passed_to_bind_total",
    "metric_kind": "counter",
    "metric_category": "fast_path",
    "legacy_aliases": ["dca_pass_to_bind"],
    "external_command": "(fp-cli DCA stats line - exact command TBD)",
    "success_category": "decision",
    "planned": true,
    "needs_provenance_review": true
  },
  "text": "Total DNS queries the DCA fast path declined to answer and forwarded to BIND (unsupported type, policy, or resource constraints). Alias: dca_pass_to_bind. Decision outcome, not a failure." 
}
```

Open Items:
- Confirm exact invocation command for compact vs verbose output (replace placeholder command string; then set `needs_provenance_review=false`).
- Determine whether split counters for cause (policy vs unsupported type vs resource) exist or are desirable (if yes, extend with additional planned metrics before parser work).
- Assess need for a cacheable vs non-cacheable ratio derived metric; could be implemented in doc guidance only.

Status: Decision counters documented; awaiting command string confirmation & parser inclusion (planned metrics stage).

### 18.19 Metrics Field Definitions Relocated (FPRRSTATS)

Sample Compact Line (one line snapshot; fabricated counts for illustration):
```
FPRRSTATS REQ a 152340 aaaa 30450 mx 120 ptr 980 cname 412 t64 0 t65 0 other 275 RES a 151980 aaaa 30390 mx 118 ptr 976 cname 410 t64 0 t65 0 other 270
```

Structure:
- Tag `FPRRSTATS` then two logical blocks: `REQ` (incoming client DNS query counts by QTYPE) and `RES` (responses sent counts by QTYPE).
- Each block lists explicit RR types (a, aaaa, mx, ptr, cname) plus `t64`, `t65`, and `other` aggregate.
- `t64` = TYPE64 = SVCB; `t65` = TYPE65 = HTTPS (per IANA). Tokens appear as `t64` / `t65` (or `TYPE64` / `TYPE65` in some variants) and are aliases for SVCB / HTTPS.

Counter Semantics:
- All counters cumulative (monotonic) until an associated reset function clears them (function name pending capture – mark provenance review until confirmed).
- REQ increments when a valid query of that type is accepted for processing (pre cache lookup).
- RES increments when a response of that RR type is sent (cache hit or forwarded). Synthesized answers (e.g., DNS64 AAAA) contribute to the corresponding RES RR type counter.
- `other` aggregates all RR types not explicitly broken out (TXT, SRV, NS, SOA, DS, DNSKEY, NAPTR, etc.).

Canonical Metric Naming (Planned):
Pattern: `fprrstats_<dimension>_<rrlabel>_total` where `<dimension>` ∈ {`queries`, `responses`} and `<rrlabel>` canonicalized (`a`, `aaaa`, `mx`, `ptr`, `cname`, `svcb`, `https`, `other`).

| Token (REQ) | Canonical Metric | Type | success_category | Aliases | Notes |
|-------------|------------------|------|------------------|---------|-------|
| a | fprrstats_queries_a_total | counter | traffic | ["a"] | TYPE 1 queries. |
| aaaa | fprrstats_queries_aaaa_total | counter | traffic | ["aaaa"] | TYPE 28 queries. |
| mx | fprrstats_queries_mx_total | counter | traffic | ["mx"] | TYPE 15 queries. |
| ptr | fprrstats_queries_ptr_total | counter | traffic | ["ptr"] | TYPE 12 queries. |
| cname | fprrstats_queries_cname_total | counter | traffic | ["cname"] | TYPE 5 queries. |
| t64 | fprrstats_queries_svcb_total | counter | traffic | ["t64","type64"] | SVCB (TYPE64). |
| t65 | fprrstats_queries_https_total | counter | traffic | ["t65","type65"] | HTTPS (TYPE65). |
| other | fprrstats_queries_other_total | counter | traffic | ["other"] | Aggregate of remaining types. |

| Token (RES) | Canonical Metric | Type | success_category | Aliases | Notes |
|-------------|------------------|------|------------------|---------|-------|
| a | fprrstats_responses_a_total | counter | traffic | ["a"] | Response RR type counts. |
| aaaa | fprrstats_responses_aaaa_total | counter | traffic | ["aaaa"] | May include synthesized DNS64 AAAA. |
| mx | fprrstats_responses_mx_total | counter | traffic | ["mx"] | — |
| ptr | fprrstats_responses_ptr_total | counter | traffic | ["ptr"] | — |
| cname | fprrstats_responses_cname_total | counter | traffic | ["cname"] | — |
| t64 | fprrstats_responses_svcb_total | counter | traffic | ["t64","type64"] | SVCB responses. |
| t65 | fprrstats_responses_https_total | counter | traffic | ["t65","type65"] | HTTPS responses. |
| other | fprrstats_responses_other_total | counter | traffic | ["other"] | Aggregate of remaining types. |

Labels:
- Add optional `rr_group="explicit"|"other"` if grouping later needed (initially omit to reduce cardinality; embedding docs describe composition of `other`).
- Potential future label `synthesized="true"|"false"` if DNS64 or other synthesis distinctions exposed (deferred).

Derived / Analytical Metrics (document only):
- Query type share (e.g., A share) = rate(fprrstats_queries_a_total) / sum_rate(all query metrics).
- SVCB+HTTPS adoption ratio = rate(fprrstats_queries_svcb_total + fprrstats_queries_https_total) / sum_rate(all query metrics).
- Cache efficiency proxy (type-specific) approximated by comparing queries vs responses deltas if future cache-hit counters per type added.

Embedding & Alias Handling:
1. Each canonical metric L1 doc: `legacy_aliases` includes original token; for SVCB/HTTPS include `t64`/`t65` & `type64`/`type65` for robustness.
2. Create L3 alias cluster `alias:fprrstats_rrtype_counters` with mapping tokens → canonical names plus explanation of `other` composition.
3. Provide concept cross-link to `concept:fast_path_architecture` for DNS traffic pattern analysis.

Provenance JSON Snippet Example (queries AAAA):
```json
{
  "id": "field:FPRRSTATS:fprrstats_queries_aaaa_total",
  "level": "L1",
  "metadata": {
    "record_type": "FPRRSTATS",
    "metric_name": "fprrstats_queries_aaaa_total",
    "metric_kind": "counter",
    "metric_category": "fast_path",
    "legacy_aliases": ["aaaa"],
    "external_command": "(fp-cli rr type stats line - exact command TBD)",
    "success_category": "traffic",
    "planned": true,
    "needs_provenance_review": true
  },
  "text": "Cumulative AAAA (TYPE28) DNS queries accepted for processing by fast path (pre cache lookup). Alias token: aaaa. Part of FPRRSTATS REQ block." 
}
```

Open Items:
- Confirm exact CLI invocation & whether multiline verbose variant appears in support bundles (replace placeholder command string; then clear `needs_provenance_review`).
- Determine if additional RR types should be broken out (e.g., TXT, SRV) based on frequency thresholds; if added later, maintain backward compatibility by keeping existing IDs.
- Consider emitting total queries/responses across all types for direct ratio computations (else compute via sum in queries).

Status: RR type distribution counters specified; awaiting command provenance confirmation and parser implementation (planned stage).

### 18.20 Metrics Field Definitions Relocated (FPDNCR)

Sample (fabricated) compact line (single aggregate across cores):
```
FPDNCR neq 3450 ewl 1280 ewp 640 eu 75 sh 2900 psh 1100 s4p 870 s6p 420 spcp 12 swpcp 3
```

All tokens are cumulative monotonic counters summed over all cores until explicitly cleared (init/restart or dedicated reset function). No instantaneous gauges.

Token Mapping & Semantics:
| Token | Canonical Metric (planned) | Type | success_category | Semantics | Notes |
|-------|----------------------------|------|------------------|-----------|-------|
| neq | fpdncr_non_edns0_queries_total | counter | classification | Queries without any EDNS0 OPT RR | Baseline classification. |
| ewl | fpdncr_edns0_with_localid_total | counter | classification | EDNS0 queries carrying Local ID option | Attribute presence. |
| ewp | fpdncr_edns0_with_policyid_total | counter | classification | EDNS0 queries carrying Policy ID option | Attribute presence. |
| eu | fpdncr_edns0_unknown_options_total | counter | classification | EDNS0 queries with unsupported/unrecognized options (excluding PCP/WPCP violations) | May indicate interoperability issues. |
| sh | fpdncr_subscriber_hits_total | counter | classification | Queries matched to any subscriber (post policy lookup) | Superset encompassing psh/s4p/s6p categories. |
| psh | fpdncr_policy_id_subscriber_hits_total | counter | classification | Subscriber identified via Policy ID | Subset of sh. |
| s4p | fpdncr_subscriber_ipv4_prefix_hits_total | counter | classification | Subscriber identified via IPv4 prefix match | Subset of sh. |
| s6p | fpdncr_subscriber_ipv6_prefix_hits_total | counter | classification | Subscriber identified via IPv6 prefix match | Subset of sh. |
| spcp | fpdncr_subscriber_pcp_violations_total | counter | decision | PCP policy control violations detected | Actionable policy events. |
| swpcp | fpdncr_subscriber_wpcp_violations_total | counter | decision | WPCP (whitelist PCP) violations detected | Actionable policy events. |

Classification:
- All counters denote classification / decision events; none are failures in the sense of internal errors. Use `success_category=classification` except explicit violation detections `spcp`, `swpcp` where `success_category=decision` (policy decision outcome).

Canonical Naming Rationale:
- Prefix `fpdncr_` for fast path DNS classification & resolution layer (avoids collision with other fast path metrics); maintain explicit, readable suffixes.
- Include `_total` for cumulative semantics; pluralize category nouns (queries, hits, violations) for clarity.

Derived Metrics / Ratios (documentation only):
- EDNS0 adoption = (ewl + ewp + eu) / (ewl + ewp + eu + neq)
- LocalID prevalence = ewl / (ewl + ewp + eu)
- Subscriber identification rate = sh / total_queries (need total queries from FPRRSTATS sum)
- PCP violation rate = spcp / sh (if sh>0)
- WPCP violation rate = swpcp / sh (if sh>0)

Embedding & Alias Handling:
1. L1 docs for each metric with `legacy_aliases` listing original short token.
2. L3 alias cluster `alias:fpdncr_classification_counters` enumerating tokens and grouping (classification vs decision violations).
3. Cross-link to RR type distribution (`FPRRSTATS`) for combined subscriber / type analyses.

Provenance JSON Snippet Example (ewl):
```json
{
  "id": "field:FPDNCR:fpdncr_edns0_with_localid_total",
  "level": "L1",
  "metadata": {
    "record_type": "FPDNCR",
    "metric_name": "fpdncr_edns0_with_localid_total",
    "metric_kind": "counter",
    "metric_category": "fast_path",
    "legacy_aliases": ["ewl"],
    "external_command": "(fp-cli edns/subscriber stats line - exact command TBD)",
    "success_category": "classification",
    "planned": true,
    "needs_provenance_review": true
  },
  "text": "Cumulative EDNS0 DNS queries containing Local ID option. Alias token: ewl. Used to assess subscriber identification via Local ID EDNS0 mechanism." 
}
```

Open Items:
- Confirm exact CLI command string and presence (or not) of any additional classification tokens in some builds.
- Determine if a total queries counter should accompany this line to reduce external joins (else rely on FPRRSTATS sums).
- Decide whether violation counters (spcp, swpcp) should appear in security/alerting dashboards with thresholds.

Status: Classification & policy decision counters specified; awaiting command provenance confirmation and parser implementation (planned stage).

### 18.21 Metrics Field Definitions Relocated (FPVLSTATS)

Sample ptop line:
```
FPVLSTATS F-P 123 F-W 45 F-B 12 F-BA 3 N-P 110 N-W 40 N-B 15 N-R 22 N-BA 2 N-DD 18 T-F 9 T-B 4
```

All counters are cumulative (monotonic) since start or last `reset_all_dnstap_stats` invocation (which zeros `ib_dnstap_stats`). No instantaneous gauges.

Prefix Groups:
- F-* (Fastpath/DCA path classification)
- N-* (Named/BIND resolver path classification)
- T-* (Total policy violation event counters; separated by fastpath vs bind namespaces)

Token Mapping & Semantics:
| Token | Canonical Metric (planned) | Type | success_category | Semantics | Notes |
|-------|----------------------------|------|------------------|-----------|-------|
| F-P | fpvlstats_fastpath_pcp_queries_total | counter | classification | Incoming fastpath DNS queries carrying PCP (Policy Control) EDNS0 option | Early detection / classification. |
| F-W | fpvlstats_fastpath_wpcp_queries_total | counter | classification | Fastpath queries with WPCP option | — |
| F-B | fpvlstats_fastpath_blacklist_queries_total | counter | decision | Queries matched blacklist policy on fastpath | Indicates early policy enforcement. |
| F-BA | fpvlstats_fastpath_block_all_queries_total | counter | decision | Queries triggering block-all action on fastpath | Strong deny; should correlate with traffic filtering. |
| N-P | fpvlstats_named_pcp_queries_total | counter | classification | PCP-option queries processed by named | Those not handled on fastpath or needing resolver logic. |
| N-W | fpvlstats_named_wpcp_queries_total | counter | classification | WPCP-option queries reaching named | — |
| N-B | fpvlstats_named_blacklist_queries_total | counter | decision | Blacklist matches identified on named path | Late detection. |
| N-R | fpvlstats_named_rpz_queries_total | counter | decision | RPZ rule match queries in named | RPZ detection occurs after recursion/policy chain. |
| N-BA | fpvlstats_named_block_all_queries_total | counter | decision | Block-all policy decisions on named | — |
| N-DD | fpvlstats_named_device_discovery_queries_total | counter | classification | Device discovery traffic identified (RR type/pattern based) | Useful for inventory context. |
| T-F | fpvlstats_fastpath_violations_total | counter | decision | Total fastpath policy violation events (PCP/WPCP misuse, malformed, disallowed) | Monitor for surge. |
| T-B | fpvlstats_named_violations_total | counter | decision | Total named-side policy violation events | Late-stage violations. |

Operational Interpretation:
- Differences between F-* and N-* for a category show offload efficacy (higher F- implies earlier handling).
- Rising `fpvlstats_fastpath_violations_total` or `fpvlstats_named_violations_total` indicates increasing policy violation rate; compare rate deltas post reset.
- If blacklist enforcement shifts from fastpath to named (F-B drops while N-B rises), investigate fastpath policy sync.

Derived Ratios (doc guidance only):
- Fastpath PCP handling ratio = F-P / (F-P + N-P)
- Fastpath blacklist enforcement ratio = F-B / (F-B + N-B)
- Violation distribution = T-F / (T-F + T-B)
- Block-all proportion (fastpath) = F-BA / F-P (approx; refine denominator if needed)

Embedding & Alias Handling:
1. Each metric L1 doc with `legacy_aliases` containing original token (exact form with dash, e.g., `F-P`). Provide normalized alias variant without dash (e.g., `FP`) for vector robustness only if it appears in logs (otherwise skip to reduce noise).
2. L3 alias cluster `alias:fpvlstats_policy_counters` mapping tokens → canonical names with grouping (classification vs decision).
3. Cross-link to EDNS0 classification (§18.20) for combined policy analysis (PCP vs subscriber/LocalID).

Provenance JSON Snippet Example (F-P):
```json
{
  "id": "field:FPVLSTATS:fpvlstats_fastpath_pcp_queries_total",
  "level": "L1",
  "metadata": {
    "record_type": "FPVLSTATS",
    "metric_name": "fpvlstats_fastpath_pcp_queries_total",
    "metric_kind": "counter",
    "metric_category": "fast_path",
    "legacy_aliases": ["F-P"],
    "external_command": "(fp-cli dnstap policy stats line - exact command TBD)",
    "success_category": "classification",
    "planned": true,
    "needs_provenance_review": true
  },
  "text": "Cumulative fastpath DNS queries carrying PCP EDNS0 option (policy control). Alias token: F-P. Used to evaluate early policy classification offload." 
}
```

Open Items:
- Confirm exact CLI invocation command and dash token formatting stability (ensure tokens always include hyphen or adapt parser flexibility).
- Determine if additional violation sub-categories exist (e.g., malformed vs disallowed) worth separate counters (currently lumped into T-F / T-B).
- Consider exposing a unified total queries counter in same line for simpler offload ratios; else rely on RR type or other query counters.

Status: FPVLSTATS policy/violation counters defined; awaiting command provenance confirmation and parser implementation (planned stage).


+-------------------------+            +------------------------------+
|     MCP Client          |  Tools     |      FastMCP Transport       |
|  (VS Code / others)     | calls ---> |  Dispatcher (tool routing)   |
+------------+------------+            +---------------+--------------+
             |                                         |
             | support.*                               | docs.* / metrics.*
             v                                         v
   +--------------------+                 +--------------------+      +---------------------+
   |  Bundle Tool       |                 |  Docs/Search Tool  |      | Metrics / Graph Tool|
   | (load/unload/context)               | (semantic+keyword) |      | (list/query/graph)  |
   +----+---------+------+                 +----+---------+----+      +-----+----------+----+
        |         |                            |         |                 |          |
        | parse   | hash/idempotency           | embeds  | aliases         | query VM | align / transforms
        v         |                            v         v                 v          v
   +---------+  +---------+              +----------+ +---------+    +-----------+  +-----------------+
   | Parser  |  | Hashing |              | Embeds   | | Alias    |    | VM Query  |  | GraphSpec Build |
   +----+----+  +----+----+              +----+-----+ +----+-----+    +-----+-----+  +----+------------+
        |             |                       |             |               |             |
  samples|       bundle hash             doc meta          |         time-series           |
        v             v                       v             |               v             v
   +-------------------------------+   +----------------------+   +------------------------------+
   |  VictoriaMetrics (Import API) |   |   State Store (SQLite)|   | VictoriaMetrics (Query API) |
   +---------------+---------------+   +----+---------+--------+   +---------------+-------------+
                   |                        |         |                        |
                   | labels/index           | bundles | doc provenance         |
                   v                        v         v                        |
            +----------------+       +----------+ +---------+                  |
            | label_index    |       | bundles  | | docmeta |<-----------------+
            +----------------+       +----------+ +---------+

Optional: External Vector Store (future) <-> Embeds

Filesystem mount (support bundles):
   /import/...  --> read tar → extract → Parser streams logs

Core flows:
1. load_bundle: FS → Parser → VM Import + labels → SQLite (bundle & active)
2. docs/search: memory embeddings (+ optional vector store) + alias index
3. metrics query/graph: VM Query → align → GraphSpec
4. state reads: active context, label values, doc metadata





