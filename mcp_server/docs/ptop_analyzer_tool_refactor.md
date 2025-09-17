# ptop_analyzer MCP Tool Refactor

Date: 2025-09-02

Author: Refactor plan generated to align tool surface with simplified category-centric taxonomy.

## Objectives
Implement the following user-requested changes:

1. Replace `list_plugins_tool` with `list_category` returning metric categories (taxonomy-aligned).
2. Extend `active_context` to include the active bundle time range.
3. Remove `healthz` (heartbeat deemed unnecessary).
4. Merge `search_docs` and `search_docs_detail` behind a single `search_docs` with an `include_bodies` flag.
5. Replace `plugin_docs` with `category_docs` (return docs grouped/queryable by metric_category instead of record_type).
6. Redefine `label_values` so that it takes a metric name and returns label KEYS (optionally with sample values) rather than values-for-a-label.
7. Remove `alias_resolve` (alias handling to be folded into `metric_search`).

## Current vs Target Mapping

| Current Tool | Action | Replacement / Change |
|--------------|--------|----------------------|
| mcp_ptop-analyzer_list_plugins_tool | Remove | `list_category` (new) |
| mcp_ptop-analyzer_active_context | Extend | Add `time_range: { start_ms, end_ms }` |
| mcp_ptop-analyzer_healthz | Remove | None (liveness implicit) |
| mcp_ptop-analyzer_search_docs | Merge | Unified `search_docs` (keep name) |
| mcp_ptop-analyzer_search_docs_detail | Merge | Unified `search_docs` (flag) |
| mcp_ptop-analyzer_plugin_docs | Replace | `category_docs` |
| mcp_ptop-analyzer_label_values | Redefine | New semantics: metric -> label keys (rename to `metric_labels`) |
| mcp_ptop-analyzer_alias_resolve | Remove | Aliases resolved inside `metric_search` |

Retained without change (for now): `load_bundle`, `list_bundles_tool`, `unload_bundle`, `get_doc_tool`, `metric_search`, `metric_metadata`, `metrics_list`, `concepts`, `label_values` (will be retired upon rollout), `query_range`, `graph_timeseries`, `graph_compare` (later may merge), `graph_compare` not in immediate scope.

## New / Updated Tool Specifications

### 1. list_category
Purpose: Enumerate distinct metric categories present in the active bundle (or optionally across all loaded bundles) with counts.

Request:
```json
{}
```
Optional future params: `{ "include_counts": true, "levels": ["L1","L2"], "tenant_id": "..." }`

Response:
```json
{
  "categories": [
    {
      "name": "FASTPATH",
      "counts": {"L1": 132, "L2": 11, "L4": 1},
      "total": 144
    },
    {
      "name": "CPU",
      "counts": {"L1": 24, "L2": 1},
      "total": 25
    }
  ],
  "generated_at_ms": 1693663200000
}
```

### 2. active_context (extended)
Add time range fields sourced from the active bundle metadata (already available in `load_bundle` return).

Current:
```json
{ "tenant_id": "T1", "path": "/abs/path" }
```
New:
```json
{
  "tenant_id": "T1",
  "path": "/abs/path",
  "time_range": { "start_ms": 1693662000000, "end_ms": 1693662600000 },
  "metrics_ingested": 8421
}
```
(Extra `metrics_ingested` recommended for quick cardinality insight.)

### 3. search_docs (merged)
Single endpoint with union of prior capabilities.

Request:
```json
{
  "query": "fastpath violations",
  "top_k": 10,
  "semantic": true,
  "include_bodies": true,
  "levels": ["L1","L2"],
  "categories": ["FASTPATH"],
  "tenant_id": "T1"
}
```

Response (include_bodies true):
```json
{
  "results": [
    {"id": "field:fpvlstats:fpvlstats_fastpath_violations_total", "score": 0.93, "level": "L1", "category": "FASTPATH", "text": "Metric fpvlstats_fastpath_violations_total...", "metadata": {"record_type": "fpvlstats"}},
    {"id": "plugin:fpvlstats", "score": 0.71, "level": "L2", "category": "FASTPATH", "text": "Plugin fpvlstats summary...", "metadata": {"record_type": "fpvlstats"}}
  ]
}
```
If `include_bodies` false, omit `text` and maybe compress metadata.

### 4. category_docs (replaces plugin_docs)
Purpose: Retrieve doc refs (and optionally bodies) filtered by metric_category. Supports pagination to avoid large payloads.

Request:
```json
{
  "category": "FASTPATH",
  "levels": ["L1"],
  "offset": 0,
  "limit": 100,
  "include_bodies": false
}
```

Response:
```json
{
  "category": "FASTPATH",
  "count": 132,
  "docs": [
    {"id": "field:fpports:fpports_rx_packets_total", "level": "L1", "record_type": "fpports"},
    {"id": "field:fpprxy:fpprxy_query_timeouts_total", "level": "L1", "record_type": "fpprxy"}
  ],
  "next_offset": 100
}
```

### 5. metric_labels (redefines label_values intent)
Purpose: Given a metric name, return the label keys (schema) and optional sample distinct values (capped) retrieved from the underlying TSDB / VM.

Request:
```json
{
  "metric": "fpports_rx_packets_total",
  "include_samples": true,
  "sample_limit_per_label": 5
}
```

Response:
```json
{
  "metric": "fpports_rx_packets_total",
  "label_keys": ["instance", "job", "port", "tenant"],
  "samples": {
    "instance": ["node1", "node2"],
    "job": ["ptop"],
    "port": ["0", "1"],
    "tenant": ["T1"]
  }
}
```
(If `include_samples` false, omit `samples`.)

### 6. Deprecations & Removal
- Remove immediately: `healthz`, `alias_resolve` (internal alias normalization will live in `metric_search`).
- Soft deprecate (support for one release): `list_plugins_tool`, `plugin_docs`, `label_values`, `search_docs_detail`.
  - Each returns a warning field: `{ "warning": "DEPRECATED: use list_category" }` until removed.

### 7. metric_search (alias integration)
Extend existing response to include `resolved_alias` when the query matched a legacy alias:
```json
{
  "query": "fpprxy_qto",
  "decision": "auto_select",
  "metric": {
    "name": "fpprxy_query_timeouts_total",
    "id": "field:fpprxy:fpprxy_query_timeouts_total"
  },
  "resolved_alias": "qto"
}
```

## Transition Plan
1. Release N: Introduce new endpoints + add deprecation warnings to old ones.
2. Release N+1: Remove old endpoints (`healthz`, `alias_resolve` entirely; others still warn).
3. Release N+2: Remove deprecated endpoints fully; update client SDK.

## Validation & Backward Compatibility
| Risk | Mitigation |
|------|-----------|
| Clients hardcoded to old tool names | Provide deprecation warnings + mapping table in release notes |
| Increased payload size from unified search | `include_bodies` default false to preserve lightweight behavior |
| Category enumeration cost | Cache category list per active bundle hash |
| Label key discovery latency | Limit sample retrieval; parallelize underlying TSDB queries |

## Open Questions / Future Enhancements
| Topic | Notes |
|-------|-------|
| Merge graph_compare | Convert to `graph_timeseries` with `comparators` & `normalize` params |
| Category-level diff | Potential `category_diff` tool for comparing bundles |
| Taxonomy audit | Could formalize as `taxonomy_audit` tool (separate proposal) |

## Summary
This refactor streamlines the tool surface around categories, reduces duplication (single search endpoint), clarifies label schema discovery, and removes low-value endpoints. It aligns with ongoing metric taxonomy normalization (FASTPATH migration) and prepares for automated audit tooling.
