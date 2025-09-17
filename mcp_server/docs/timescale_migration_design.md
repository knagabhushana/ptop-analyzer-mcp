# TimescaleDB Implementation for perf_mcp_server

Version: 2025-09-10
Owner: MCP Server Platform
Status: Draft (for review)

## 1. Objective
Implement TimescaleDB as the primary time‑series backend for perf_mcp_server with:
1. Existing metric names preserved (e.g. `cpu_user_percent`, `disk_busy_percent`, `dbwr_bucket_count_total`).
2. Simplified global metadata: `bundle_id`, `sptid`, `metric_category`, `host` only.
3. Local labels/metadata preserved as parsed from individual metric lines (`device`, `pid`, `bucket`, etc.).
4. Enhanced schema discovery: MCP client can discover views, schemas, and query capabilities dynamically.
5. Direct SQL-native implementation optimized for TimescaleDB without legacy VM compatibility layers.

## 2. High-Level Approach
1. **Table per PTOPS record group** (CPU, DISK, NET, TOP, DBWR, etc.) as TimescaleDB hypertables.
2. **View per metric** - each metric becomes a discoverable SQL view with standardized column interface.
3. **Enhanced schema discovery** - new MCP tools for view enumeration, column introspection, and query construction assistance.
4. **Minimal global metadata** - reduce storage overhead and complexity by limiting common columns to essential identifiers.
5. **Native SQL queries** - direct TimescaleDB SQL with time_bucket functions, no PromQL translation layer.

Rationale for group-hypertable + per-metric view (vs one generic EAV fact table):
* Parsing already produces strongly typed, sparse‑light metrics per group (LIMITED column growth). 
* Minimizes row explosion and avoids heavy join cost for reconstructions.
* Allows Timescale compression + chunk pruning using narrow composite indexes.

## 3. Data Model
### 3.1 Common Columns (ALL hypertables) - Simplified
```
ts              TIMESTAMPTZ NOT NULL,        -- sample timestamp (ms precision)
bundle_id       TEXT NOT NULL,               -- unique bundle identifier
sptid           TEXT,                        -- support ticket ID (nullable)
metric_category TEXT NOT NULL,               -- cpu, disk, network, process, etc.
host            TEXT NOT NULL,               -- host identifier
-- Local labels/metadata (varies by record group, preserved as-is from parsing)
-- Examples: cpu, device, interface, pid, ppid, exec, command, bucket, container_id, etc.
-- These are defined per-table based on what the parser extracts for each group
```

Indexes (baseline per hypertable):
```
PRIMARY KEY (bundle_id, ts, <local_discriminators>) -- implemented via Timescale chunking
INDEX ON (bundle_id, ts DESC)
INDEX ON (metric_category, ts DESC)
INDEX ON (host, ts DESC)
-- Local label indexes (per table as needed)
PARTIAL INDEX ON (pid) WHERE pid IS NOT NULL (TOP/SMAPS)
PARTIAL INDEX ON (device) WHERE device IS NOT NULL (DISK)
PARTIAL INDEX ON (interface) WHERE interface IS NOT NULL (NET)
```

### 3.2 Example Hypertable Schemas
#### CPU Hypertable: `ptops_cpu`
```
CREATE TABLE ptops_cpu (
  ts TIMESTAMPTZ NOT NULL,
  bundle_id TEXT NOT NULL,
  sptid TEXT,
  metric_category TEXT NOT NULL,  -- 'cpu'
  host TEXT NOT NULL,
  -- Local labels from parsing
  cpu TEXT,                       -- cpu label (cpu / cpuN)
  -- Metric columns
  user_percent DOUBLE PRECISION,
  system_percent DOUBLE PRECISION,
  idle_percent DOUBLE PRECISION,
  irq_percent DOUBLE PRECISION,
  utilization DOUBLE PRECISION
);
SELECT create_hypertable('ptops_cpu','ts', chunk_time_interval => interval '1 day');
```

#### DISK Hypertable: `ptops_disk`
```
CREATE TABLE ptops_disk (
  ts TIMESTAMPTZ NOT NULL,
  bundle_id TEXT NOT NULL,
  sptid TEXT,
  metric_category TEXT NOT NULL,  -- 'disk'
  host TEXT NOT NULL,
  -- Local labels from parsing
  device TEXT,                    -- device name (sda, sdb, etc.)
  -- Metric columns
  reads_per_sec DOUBLE PRECISION,
  read_kb_per_sec DOUBLE PRECISION,
  writes_per_sec DOUBLE PRECISION,
  write_kb_per_sec DOUBLE PRECISION,
  busy_percent DOUBLE PRECISION,
  avg_queue_len DOUBLE PRECISION
);
SELECT create_hypertable('ptops_disk','ts', chunk_time_interval => interval '1 day');
```

#### DBWR Histogram Buckets: `ptops_dbwr`
```
CREATE TABLE ptops_dbwr (
  ts TIMESTAMPTZ NOT NULL,
  bundle_id TEXT NOT NULL,
  sptid TEXT,
  metric_category TEXT NOT NULL,         -- 'db_histogram'
  host TEXT NOT NULL,
  -- Local labels from parsing
  bucket TEXT NOT NULL,                  -- bucket identifier
  -- Metric columns
  bucket_count_total BIGINT,             -- counter
  bucket_avg_latency_seconds DOUBLE PRECISION
);
SELECT create_hypertable('ptops_dbwr','ts', chunk_time_interval => interval '1 day');
```

### 3.3 Wide vs Sparse Considerations
* Most groups have stable column sets; nulls for unused metrics are acceptable (Timescale compresses effectively).
* For fastpath families where planned metrics expand, group them into logical hypertables (e.g., `ptops_fp_ports`, `ptops_fp_proxy`, etc.) to limit null dispersion.

## 4. Metric Views - Standardized Interface
Each metric becomes a discoverable SQL view with consistent column interface:
```
SELECT ts, <value_expr> AS value,
       bundle_id, sptid, metric_category, host,
       <local_labels...>  -- only columns that exist in source table
FROM <group_table>
WHERE <value_expr> IS NOT NULL;
```

Examples:
```
CREATE VIEW cpu_user_percent AS
  SELECT ts, user_percent AS value, 
         bundle_id, sptid, metric_category, host, 
         cpu
  FROM ptops_cpu WHERE user_percent IS NOT NULL;

CREATE VIEW disk_busy_percent AS
  SELECT ts, busy_percent AS value,
         bundle_id, sptid, metric_category, host,
         device
  FROM ptops_disk WHERE busy_percent IS NOT NULL;

CREATE VIEW dbwr_bucket_count_total AS
  SELECT ts, bucket_count_total AS value,
         bundle_id, sptid, metric_category, host,
         bucket
  FROM ptops_dbwr WHERE bucket_count_total IS NOT NULL;
```

Histogram Average Latency:
```
CREATE VIEW dbwr_bucket_avg_latency_seconds AS ... FROM ptops_dbwr ...
```

### 4.1 Enhanced Schema Discovery System
Create comprehensive metadata tables for MCP client discovery:

#### Metric Catalog: `metric_catalog`
```
CREATE TABLE metric_catalog (
  metric_name TEXT PRIMARY KEY,
  base_table TEXT NOT NULL,
  column_name TEXT NOT NULL,
  metric_kind TEXT NOT NULL,  -- gauge, counter, histogram_bucket, etc.
  unit TEXT,                  -- percent, seconds, bytes, etc.
  description TEXT,
  aliases TEXT[] DEFAULT '{}',  -- legacy or synonym names (cpu_utilization_percent -> cpu_utilization)
  local_labels TEXT[],        -- array of label column names available
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

#### Table Schema Registry: `table_schemas`
```
CREATE TABLE table_schemas (
  table_name TEXT PRIMARY KEY,
  record_group TEXT NOT NULL,        -- CPU, DISK, TOP, etc.
  metric_category TEXT NOT NULL,
  global_columns TEXT[],             -- [ts, bundle_id, sptid, metric_category, host]
  local_columns JSONB,               -- {"cpu": "TEXT", "device": "TEXT", ...}
  metric_columns JSONB,              -- {"user_percent": "DOUBLE PRECISION", ...}
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

#### Query Capabilities: `query_patterns`
```
CREATE TABLE query_patterns (
  pattern_name TEXT PRIMARY KEY,
  description TEXT,
  sql_template TEXT,             -- Parameterized SQL template
  required_params TEXT[],        -- [metric_name, bundle_id, start_time, end_time]
  optional_params JSONB,         -- {"step_seconds": 60, "aggregation": "avg"}
  example_usage TEXT
);
```

Population Strategy:
- Auto-generate from declarative schema spec during deployment
- Refresh when parser evolution adds new metrics/tables
- Enable MCP clients to discover available metrics, labels, and query patterns dynamically

## 5. Ingestion Pipeline (TimescaleDB Native)
### 5.1 Flow
1. Parse PTOPS log → structured records grouped by record type (CPU, DISK, etc.).
2. Map records → target hypertable using group prefix mapping.
3. Buffer rows per table with simplified metadata (ts, bundle_id, sptid, metric_category, host + local labels).
4. Bulk insert using COPY with configurable batch size (1000 rows default).
5. Auto-update metadata catalogs when new metrics/labels detected.

### 5.2 COPY Staging
To maximize throughput:
* Use a temp file / memory stream with tab-separated values.
* `COPY ptops_<group> (ts, bundle_id, host, ..., metric_column, ...) FROM STDIN WITH (FORMAT csv, NULL '')`.
* Only include changed metric columns per batch? Simplicity first: always emit full column set with empty strings for nulls (Timescale handles).

### 5.3 Idempotency
Use existing bundle_id semantics: each unique bundle gets a unique bundle_id. Re-ingestion with `force=true` generates a new bundle_id, allowing natural deduplication through primary key constraints.

### 5.4 Error Handling
* Batch COPY failure → retry (configurable attempts). After max attempts, record warning and continue other batches unless failure ratio > threshold (same semantics as VM path).
* Individual row errors (rare if schema stable) cause batch abort; fallback to row-wise inserts for isolation if `COPY` repeatedly fails.

### 5.5 Transaction Boundaries
* Each batch commit independent → streaming ingestion with backpressure minimal.
* Wrap view creation + catalog insertion in a migration transaction at startup / schema upgrade time, not per batch.

## 6. Native SQL Query Interface
### 6.1 Direct SQL Queries
Replace PromQL translation with direct TimescaleDB SQL optimized queries:

#### Basic Time Series Query:
```sql
SELECT time_bucket($step_seconds * INTERVAL '1 second', ts) AS ts_bucket,
       AVG(value) AS value,
       bundle_id, sptid, host, <local_labels>
FROM <metric_view>
WHERE ts BETWEEN $start_time AND $end_time
  AND bundle_id = $bundle_id
  AND <label_filters>
GROUP BY ts_bucket, bundle_id, sptid, host, <local_labels>
ORDER BY ts_bucket;
```

#### Aggregation Patterns:
```sql
-- Rate calculation for counters
WITH windowed AS (
  SELECT ts, value, LAG(value) OVER (ORDER BY ts) AS prev_value,
         LAG(ts) OVER (ORDER BY ts) AS prev_ts,
         <labels>
  FROM <counter_metric_view>
  WHERE bundle_id = $bundle_id AND ts BETWEEN $start AND $end
)
SELECT time_bucket($step * INTERVAL '1 second', ts) AS ts_bucket,
       AVG((value - prev_value) / EXTRACT(EPOCH FROM (ts - prev_ts))) AS rate_per_sec,
       <labels>
FROM windowed 
WHERE prev_value IS NOT NULL
GROUP BY ts_bucket, <labels>
ORDER BY ts_bucket;

-- Top-K queries
SELECT ts_bucket, value, <labels>
FROM (
  SELECT time_bucket($step * INTERVAL '1 second', ts) AS ts_bucket,
         AVG(value) AS value, <labels>,
         ROW_NUMBER() OVER (PARTITION BY time_bucket($step * INTERVAL '1 second', ts) 
                           ORDER BY AVG(value) DESC) as rank
  FROM <metric_view>
  WHERE bundle_id = $bundle_id AND ts BETWEEN $start AND $end
  GROUP BY ts_bucket, <labels>
) ranked
WHERE rank <= $k
ORDER BY ts_bucket, rank;
```

### 6.2 Query Builder Service
Implement SQL query builder that uses metadata catalogs:
1. Validate metric exists in `metric_catalog`
2. Resolve view name and available labels
3. Build parameterized SQL from `query_patterns` templates
4. Execute with proper type conversion and error handling

## 7. Enhanced Discovery Tools
### 7.1 Schema Discovery MCP Tools

#### `schema.list_tables`
Enumerate available hypertables and their record groups:
```sql
SELECT table_name, record_group, metric_category, 
       array_length(local_columns::TEXT[], 1) AS local_label_count,
       (metric_columns::JSONB ? 'bucket_count_total') AS is_histogram
FROM table_schemas 
ORDER BY record_group;
```

#### `schema.list_metrics` 
Enhanced metric enumeration with metadata:
```sql
SELECT metric_name, metric_kind, unit, description,
       base_table, local_labels
FROM metric_catalog 
WHERE ($category IS NULL OR base_table IN (
  SELECT table_name FROM table_schemas WHERE metric_category = $category
))
ORDER BY metric_name;
```

#### `schema.list_label_values`
Dynamic label value discovery:
```sql
SELECT DISTINCT <label_column>
FROM <metric_view>
WHERE bundle_id = $bundle_id
  AND <label_column> IS NOT NULL
ORDER BY 1
LIMIT $limit OFFSET $offset;
```

#### `schema.describe_metric`
Complete metric schema information:
```sql
SELECT mc.metric_name, mc.metric_kind, mc.unit, mc.description,
       mc.base_table, mc.local_labels,
       ts.global_columns, ts.local_columns, ts.metric_columns
FROM metric_catalog mc
JOIN table_schemas ts ON mc.base_table = ts.table_name
WHERE mc.metric_name = $metric_name;
```

### 7.2 Query Construction Tools

#### `query.get_patterns`
Available query patterns for metric type:
```sql
SELECT pattern_name, description, sql_template, 
       required_params, optional_params, example_usage
FROM query_patterns
WHERE ($metric_kind IS NULL OR pattern_name LIKE '%' || $metric_kind || '%')
ORDER BY pattern_name;
```

#### `query.build_sql`
Generate executable SQL from pattern + parameters:
- Validate metric exists and get its metadata
- Select appropriate query pattern
- Substitute parameters into SQL template
- Return executable SQL with parameter bindings

## 8. Client Query Workflow
### 8.1 Discovery-Driven Querying
1. **Discover Metrics**: `schema.list_metrics` → get available metrics with metadata
2. **Explore Schema**: `schema.describe_metric` → understand labels and data types  
3. **Find Patterns**: `query.get_patterns` → get suitable query templates for metric type
4. **Build Query**: `query.build_sql` → generate executable SQL with parameters
5. **Execute**: Run SQL directly against TimescaleDB with proper error handling

### 8.2 Example Client Interaction
```python
# Discover CPU metrics
metrics = client.call("schema.list_metrics", {"category": "cpu"})
# Result: [{"metric_name": "cpu_user_percent", "metric_kind": "gauge", ...}, ...]

# Get schema details
schema = client.call("schema.describe_metric", {"metric_name": "cpu_user_percent"})  
# Result: {"local_labels": ["cpu"], "base_table": "ptops_cpu", ...}

# Get query patterns
patterns = client.call("query.get_patterns", {"metric_kind": "gauge"})
# Result: [{"pattern_name": "timeseries_avg", "sql_template": "SELECT ...", ...}, ...]

# Build executable query
query = client.call("query.build_sql", {
    "pattern_name": "timeseries_avg",
    "metric_name": "cpu_user_percent", 
    "bundle_id": "bundle123",
    "start_time": "2025-01-01T00:00:00Z",
    "end_time": "2025-01-01T01:00:00Z",
    "step_seconds": 60,
    "filters": {"cpu": "cpu0"}
})
# Result: {"sql": "SELECT time_bucket...", "params": {...}}
```

## 9. Implementation Plan 
| Phase | Focus | Deliverables | Success Criteria |
|-------|-------|--------------|------------------|
| 1 | Schema & Discovery | Hypertable schemas, metadata catalogs, discovery MCP tools | `schema.*` tools work, DDL generates correctly |
| 2 | Ingestion Pipeline | PTOPS parser → TimescaleDB ingestion, view generation | Sample data ingests, views queryable |
| 3 | Query Tools | `query.*` MCP tools, SQL builder, basic patterns | Time series queries execute correctly |
| 4 | Advanced Queries | Rate calculations, aggregations, top-K patterns | Complex analytics work |
| 5 | Production Ready | Error handling, performance tuning, monitoring | Production deployment ready |

## 10. Performance & Scaling Considerations
| Concern | Mitigation |
|---------|------------|
| Insert throughput | COPY batching, grouping by table, async writer worker |
| Query latency (wide tables) | Appropriate projection via views; indexes on discriminating labels; chunk pruning by time & bundle_id |
| High cardinality (TOP, SMAPS) | Optional parser flags still limit ingestion; consider separate hypertables `ptops_top` & `ptops_smaps` with compression earlier |
| Histogram fan-out | Single row per bucket per timestamp (already) – rely on bucket label index |
| Storage bloat | Timescale native compression after N days (`ALTER TABLE ... SET (timescaledb.compress)` + policy) |
| Retention | Timescale retention policy per hypertable (`add_retention_policy`) |

Compression Policy Example:
```
SELECT add_compression_policy('ptops_top', INTERVAL '7 days');
SELECT add_retention_policy('ptops_top', INTERVAL '30 days');
```

## 11. Security & Multi-Tenancy
All queries enforce `bundle_id = $active_bundle` injection exactly as with VM. Optional Row Level Security (RLS) future enhancement if multiple users / roles appear. For now, rely on application layer injection & prepared statements.

## 12. Error Handling & Observability
Instrumentation counters (extend existing):
```
timescale_ingest_batches_total
timescale_ingest_batches_failed_total
timescale_copy_duration_ms_sum
timescale_query_duration_ms_sum
timescale_rows_inserted_total            -- raw rows written to hypertables
timescale_query_errors_total             -- failed query executions
timescale_schema_auto_creates_total      -- new tables/views created on-the-fly
timescale_schema_auto_create_errors_total
```
Expose optionally through a self-metrics view or external Prom exporter (pg_stat_statements + custom table).

## 10. Configuration 
| Env Var | Description | Default |
|---------|-------------|---------|
| TIMESCALE_DSN | Postgres DSN (user:pass@host:port/db) | postgresql://localhost/ptops |
| TIMESCALE_BATCH_SIZE | Row batch size per table | 1000 |
| TIMESCALE_CHUNK_DAYS | Chunk time interval days | 1 |
| TIMESCALE_COMPRESSION_AFTER_DAYS | Auto-compression schedule | 7 |
| TIMESCALE_RETENTION_DAYS | Data retention policy | 30 |
| SCHEMA_AUTO_UPDATE | Auto-create new views for detected metrics | true |

## 11. Implementation Order
1. **Schema Foundation**: Metadata catalog tables, hypertable schemas, view generation
2. **Discovery Tools**: `schema.*` MCP tools for introspection
3. **Ingestion Pipeline**: PTOPS parser → TimescaleDB with auto-schema updates  
4. **Query Framework**: SQL builder, `query.*` MCP tools, basic patterns
5. **Advanced Analytics**: Rate calculations, aggregations, histogram queries
6. **Production Hardening**: Error handling, monitoring, performance optimization

## 12. Declarative Schema Specification
```python
SCHEMA_SPEC = {
    'CPU': TableGroup(
        table='ptops_cpu',
        category='cpu',
        local_labels=['cpu'],
        metrics={
            'user_percent': Metric(kind='gauge', unit='percent', description='CPU user time percentage'),
            'system_percent': Metric(kind='gauge', unit='percent', description='CPU system time percentage'),
            'utilization': Metric(kind='gauge', unit='percent', description='Overall CPU utilization')
        }
    ),
    'DISK': TableGroup(
        table='ptops_disk', 
        category='disk',
        local_labels=['device'],
        metrics={
            'reads_per_sec': Metric(kind='rate', unit='ops/sec', description='Disk read operations per second'),
            'busy_percent': Metric(kind='gauge', unit='percent', description='Disk busy percentage')
        }
    ),
    'DBWR': TableGroup(
        table='ptops_dbwr',
        category='db_histogram', 
        local_labels=['bucket'],
        metrics={
            'bucket_count_total': Metric(kind='counter', unit='operations', description='DBWR bucket operation count'),
            'bucket_avg_latency_seconds': Metric(kind='gauge', unit='seconds', description='DBWR bucket average latency')
        }
    )
}
```

Auto-generates:
- CREATE TABLE statements with proper types
- CREATE VIEW statements for each metric  
- Metadata catalog population
- Query pattern templates
- MCP tool parameter validation

## 13. Testing Strategy
| Test Category | Focus | Key Tests |
|---------------|-------|-----------|
| Schema Generation | DDL correctness | Deterministic DDL from spec, view creation, catalog population |
| Discovery Tools | MCP introspection | `schema.*` tools return correct metadata, query patterns work |
| Ingestion | Data integrity | PTOPS parsing → hypertables, label preservation, batch processing |
| Query Building | SQL generation | Pattern templates → executable SQL, parameter binding, error handling |
| Analytics | Query correctness | Time bucketing, aggregations, rate calculations, top-K queries |
| Performance | Scale testing | Bulk ingestion throughput, query latency with large datasets |

## 14. Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|-----------|
| View explosion overhead | Slow metadata queries | Dedicated metadata catalogs, avoid pg_catalog scans |
| Wide table null sparsity | Storage inefficiency | TimescaleDB compression, group related metrics |
| Complex SQL query building | Implementation complexity | Declarative patterns, comprehensive testing |
| Client query construction burden | Poor UX | Rich discovery tools, example patterns, SQL templates |
| Schema evolution complexity | Breaking changes | Versioned specs, backward-compatible view updates |

## 15. Future Enhancements
* **Advanced Analytics**: Continuous aggregates for hourly/daily rollups, materialized views for common queries
* **Performance Optimizations**: Per-table compression policies, adaptive chunk sizing, query result caching  
* **Schema Evolution**: Automated migration tools, backward compatibility management
* **Integration**: pgvector for embeddings consolidation, external analytics tool connectors
* **Monitoring**: Query performance tracking, ingestion health dashboards, capacity planning

## 16. Success Criteria
* All discovered metrics queryable through views with correct metadata
* Discovery tools (`schema.*`, `query.*`) enable client self-service querying
* Ingestion performance comparable to current VM pipeline 
* Query latency acceptable for interactive use (<5s for typical time series)
* Comprehensive test coverage for all query patterns and edge cases

---
This document establishes the authoritative plan to implement TimescaleDB as the native metrics backend for perf_mcp_server with enhanced schema discovery capabilities and streamlined metadata management.

## 17. Functional Parity & Migration Mapping
### 17.1 Inventory of Existing (VM-Centric) Functionality
Captured from current codebase & tests (pre-migration):
1. Support bundle lifecycle: load (with hash reuse + force), unload (single / purge_all), list, active context, automatic path hashing & SPTID deduction.
2. Category-selective ingestion (CPU, MEM, DISK, NET, TOP, SMAPS, DB, FASTPATH, OTHER) with reuse fast-path if categories already covered.
3. Ingestion summaries: metrics_ingested, logs_processed, time range start/end.
4. Metric discovery: metrics_list (via VM /api/v1/series) returning metric names & warnings when VM disabled.
5. Metric metadata: metric_metadata (presence check + doc association).
6. Label enumeration: label_values(metric, label) GET distinct label values.
7. Time range query: query_range(metric, start_ms, end_ms, step_ms) → matrix result.
8. Multi-metric graph: graph_timeseries([metrics], start, end, step) combining results.
9. Comparative graph w/ normalization: graph_compare(primary, comparators, start, end, step, normalize=True) scaling comparator series relative to primary baseline.
10. Embeddings & document search: semantic + keyword search across L1/L2/L4 docs, concept listing, alias resolution placeholder.
11. Metric search (semantic alias to docs) returning candidates + auto/ambiguous/no_match decision logic.
12. Histogram bucket metrics (DBWR) represented as counter + latency metrics with bucket label.
13. High-cardinality per-process metrics (TOP/SMAPS) with selective ingestion & optional limiting (max_files, categories filter).
14. Observability: internal debug logging, ingestion stats, VM connectivity probe, retention age diagnostic.
15. Reuse semantics: path_already_active fast-path & hash-based reuse; force re-ingest produces new bundle_id.
16. Active bundle global (no per-tenant isolation anymore) but SPTID label preserved informationally.

### 17.2 Timescale Replacement Mapping
| Legacy VM Tool / Behavior | Timescale Strategy | Notes |
|---------------------------|--------------------|-------|
| metrics_list | `schema.list_metrics` | Returns same metric names; include warnings array for backward compat (empty if OK, 'timescale_unavailable' on failure). |
| metric_metadata(name) | `schema.describe_metric` + doc lookup | Merge doc + catalog metadata; expose presence flag. |
| label_values(label, metric=...) | `schema.list_label_values` | Same output shape: {'label':..., 'values': [...]} |
| query_range(metric, start, end, step) | `query.build_sql(pattern=timeseries_avg)` executed | Adapter keeps response structure: 'series':[{'metric': {'__name__': metric, <labels>}, 'samples': [...]}]. |
| graph_timeseries(metrics[]) | Multiple pattern builds & union in client | Maintain same series array shape; order preserved. |
| graph_compare(normalize=True) | Execute primary + comparators; apply scaling factor (primary first-sample or max) post-query | Normalization logic replicated in adapter layer (SQL unchanged). |
| Histogram bucket queries | Views per bucket_count_total + avg latency; rate pattern for counters | For percentiles later, add aggregation pattern. |
| Reuse detection & force | Unchanged in bundle lifecycle (SQLite metadata) | Ingestion writer changes only. |
| Category-selective ingestion | Same parameter list; ingestion writes to hypertables instead of VM | Auto-creates hypertables/views if new categories appear. |
| Embeddings search & docs | Unchanged | Independent of metrics backend. |
| Metric search (semantic) | Unchanged | Still uses embeddings corpus. |
| Observability counters | New Timescale counters (section 12) | Exposed via future self-metrics view. |
| VM connectivity warnings | Timescale connection health probe on init | Replace 'vm_disabled' with 'timescale_unavailable'. |
| Legacy alias metric names (e.g. cpu_utilization_percent) | Catalog `aliases` array + optional compatibility views | During migration, create lightweight views named after each alias selecting from canonical metric view to avoid client breakage. |

### 17.3 Adapter Layer (Backward-Compatible Surface)
Provide a thin compatibility module (e.g. `timescale_adapter.py`) that exposes the legacy tool function names (`metrics_list`, `label_values`, `query_range`, etc.) but internally:
1. Resolves metric via `metric_catalog`.
2. Uses appropriate `query_patterns` template.
3. Normalizes SQL results into previous schema (matrix-style).
4. Injects warnings array when backend unreachable or metric missing.

Rationale: Allows UI / clients depending on existing tool names to continue working during transition without immediate client updates. Over time, clients can migrate to new `schema.*` and `query.*` tools for richer functionality.

### 17.4 Query Result Normalization Details
Legacy VM responses (Prometheus matrix) structure:
```
{
  'resultType': 'matrix',
  'result': [ {'metric': {'__name__': 'cpu_utilization','cpu':'cpu0','bundle_id':'b-...'}, 'values': [[unix_ts,'1.0'], ...]} ]
}
```
Adapter maps Timescale rows:
```
ts_bucket | value | bundle_id | sptid | host | <local_labels>
```
into samples list: `[{'ts': ms, 'value': float}]` while retaining metric label dict under 'metric'. Maintains existing test expectations with minimal changes.

### 17.5 Gap Analysis & Actions
| Gap | Action | Priority |
|-----|--------|----------|
| No explicit normalization helper in design | Implement client-side scaling after primary query (pattern independent) | P1 |
| Missing legacy warning semantics | Add warnings field to adapter outputs | P1 |
| Ingestion currently Prom-line -> VM | Implement `TimescaleWriter` with COPY buffering | P1 |
| Rate/top-K patterns not previously exposed | Provide but mark optional; do not break existing tests | P2 |
| Metrics API test shapes expect 'expression' field | Adapter injects 'expression'=metric_name for parity | P1 |
| Retention age diagnostic VM-specific | Replace with Timescale retention policy introspection (`_timescaledb_catalog.hypertable` + policy tables) | P2 |
| Observability counters unset | Instrument ingestion & query code paths | P1 |
| Alias metric names not yet surfaced | Add `aliases` to catalog + generate alias views | P1 |

### 17.6 Migration Execution Checklist
1. Implement schema generator & create initial hypertables + views.
2. Build `TimescaleWriter` mirroring `VictoriaMetricsWriter` interface (`add(sample)`, `flush()`).
3. Replace `VictoriaMetricsWriter` usage in ingestion with factory selecting writer based on env (Timescale vs VM) — initial deployment Timescale-only.
4. Implement metadata catalog population from `SCHEMA_SPEC` + runtime detection of new metrics.
5. Add adapter functions for legacy tool names returning previous response shapes.
6. Update tests (or add parallel tests) to exercise adapter against a seeded Timescale test DB; keep original tests until cutover complete.
7. Remove VM-specific code once confidence achieved (optionally behind feature flag `ENABLE_VM_BACKEND`).
8. Generate alias views & populate `aliases` column; ensure doc embeddings synonyms resolved to canonical names.

### 17.7 Success Validation for Parity
All existing tests that are backend-agnostic (docs, embeddings, bundle lifecycle) pass unchanged. Metrics API tests pass with an injected fake Timescale query engine (or real ephemeral DB) without modifying assertions except possibly warning token change from `vm_disabled` → `timescale_unavailable`.

### 17.8 Non-Goals (Explicit)
* Dual-write or cross-backend comparison (intentionally removed).
* PromQL expression parsing (queries built exclusively via templates & adapters).
* Arbitrary ad-hoc label regex match syntax (can be added later via SQL LIKE / regex operators).

---
