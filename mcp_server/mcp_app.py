import os, time, datetime, glob, shutil, tarfile, uuid, re
import psycopg
from typing import List, Optional, Dict, Any

# Reuse existing stores & ingestion
from .support_store import (
    file_bundle_hash, get_bundle_by_hash, insert_bundle, set_active_context,
    get_active_context, unload_active, list_bundles, get_bundle, delete_all_bundles_for_tenant,
    set_global_active, get_global_active, list_all_bundles, unload_global_active, promote_random_bundle
)
from .embeddings_store import load_embeddings, list_categories, get_metric, cheap_text_embedding, semantic_search, keyword_search, get_embeddings_status  # minimal subset for metric_search
from .ingestion.ptops_ingest import discover_ptop_logs, DEFAULT_MAX_FILES
from .ingestion.ptops_ingest_parallel import ingest_ptop_logs_optimized, create_optimized_writers
from .timescale.bootstrap import bootstrap_timescale
from .timescale.writer import TimescaleWriter
from .timescale.schema_spec import SCHEMA_SPEC
from .debug_util import dbg
from fastmcp import FastMCP

# ----------------- System Prompt Guidance -----------------
SYSTEM_PROMPT = (
    "Workflow (Bundle-ID centric):\n"
    "1. load_bundle(path=..., force=optional, max_files=optional, categories=[...]).\n"
    "2. Exactly one active bundle at a time (hash-based id).\n"
    "3. active_context() -> {bundle_id,time_range{start_ms,end_ms}}. Always use that time window.\n"
    "4. list_bundles_tool() shows all bundles + active flag.\n"
    "5. Metrics & queries must filter by bundle_id; sptid is informational.\n"
    "6. unload_bundle() removes a bundle; active auto-promotes another if available.\n"
    "7. Use metric_discover / metric_search first to find metric view names.\n"
    "8. PTOPS_CLEAN_START=1 wipes previous Timescale state (destructive).\n"
    "9. Each metric exposes a Timescale view named exactly after the metric with columns: ts, value, bundle_id, sptid, metric_category, host, plus local labels (e.g. cpu_id).\n"
    "10. Use metric_schema(metric_name) to get column roles & an example query template.\n"
    "11. Constrain all analytical SQL: ts BETWEEN to_timestamp(start_ms/1000) AND to_timestamp(end_ms/1000).\n"
    "12. timescale_sql(sql=...) executes arbitrary read-only SELECT / CTE / time_bucket / Toolkit queries (SELECT-only, auto LIMIT).\n"
    "13. Compose CTEs, window functions, aggregates, percentiles freely—no mutation statements allowed.\n"
    "Domain Guidance: CPU category metrics are per-CPU (one row per timestamp per cpu_id). Per-process metrics live in the TOP category (process-centric: pid, command, cpu%, mem%). If a user asks for per-process CPU/memory stats, direct discovery/search toward TOP (not CPU). If TOP metrics aren't present yet, respond that process-level metrics are not ingested in the current dataset.\n"
    "Fast Path Guidance: If the user asks about fast path / fastpath / fpc / packet processing efficiency, FIRST call fastpath_architecture (concept doc) to ground the response, then cite relevant metrics (e.g. fpc_cycles_per_packet, fpc_cpu_busy_percent). If no fast path metrics ingested, state that FASTPATH category is absent.\n"
)

mcp = FastMCP("ptops-mcp")
TIMESCALE_WRITER_LAST: Optional[TimescaleWriter] = None  # updated on ingestion when TS enabled
TIMESCALE_DIRECT_CONN = None  # fallback read-only connection if no writer yet

TENANT_PATTERN = re.compile(r"(NIOSSPT[-_]?\d+)", re.IGNORECASE)
SB_FILE_PATTERN = re.compile(r"sb-(\d{8})_(\d{4}).*\.tar\.gz$", re.IGNORECASE)
SB_FILE_TRAILING_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})")
SUPPORT_BASE_DIR = os.environ.get("SUPPORT_BASE_DIR", "/import/customer_data/support")

# ----------------- Helpers reused from FastAPI version -----------------

def _deduce_tenant_and_path(path: str):
    import hashlib
    warnings: List[str] = []
    original_path = path
    if not os.path.exists(path):
        raise ValueError("path not found")
    def _hash_id(p: str) -> str:
        return 'anon-' + hashlib.sha256(p.encode()).hexdigest()[:12]
    # Scan parent directories early for a tenant pattern (e.g. /.../NIOSSPT-1234/...)
    try:
        cur = os.path.abspath(path)
        for _ in range(6):  # limit upward traversal
            base = os.path.basename(cur)
            m_parent = TENANT_PATTERN.search(base)
            if m_parent:
                return (m_parent.group(1).upper(), path, warnings)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    except Exception:
        pass  # non-fatal
    if os.path.isdir(path):
        base_name = os.path.basename(os.path.normpath(path))
        m = TENANT_PATTERN.search(base_name)
        if m:
            return (m.group(1).upper(), path, warnings)
        entries = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try: st = os.stat(full)
            except FileNotFoundError: continue
            entries.append((st.st_mtime, full))
        if not entries:
            warnings.append('empty_directory_no_children')
            return (_hash_id(path), path, warnings)
        entries.sort(reverse=True)
        chosen = entries[0][1]
        if os.path.isdir(chosen):
            cbase = os.path.basename(chosen)
            m2 = TENANT_PATTERN.search(cbase)
            if m2:
                return (m2.group(1).upper(), chosen, warnings)
            warnings.append('no_tenant_pattern_in_latest_dir')
            return (_hash_id(chosen), chosen, warnings)
        else:
            path = chosen
    fname = os.path.basename(path)
    m = TENANT_PATTERN.search(fname)
    if m:
        return (m.group(1).upper(), path, warnings)
    if fname.endswith(('.tar.gz', '.tgz')):
        try:
            with tarfile.open(path, 'r:*') as tf:
                for member in tf.getmembers():
                    mm = TENANT_PATTERN.search(member.name)
                    if mm:
                        return (mm.group(1).upper(), path, warnings)
        except Exception as e:
            warnings.append(f'tar_scan_failed:{e.__class__.__name__}')
    warnings.append('tenant_id_deduced_fallback_hash')
    return (_hash_id(original_path), path, warnings)

def _auto_select_bundle_tar(tenant_id: str) -> str:
    base_dir = os.environ.get("SUPPORT_BASE_DIR", SUPPORT_BASE_DIR)
    tenant_dir = os.path.join(base_dir, tenant_id)
    if not os.path.isdir(tenant_dir):
        raise ValueError(f"tenant directory not found: {tenant_dir}")
    candidates = []
    for name in os.listdir(tenant_dir):
        if not name.lower().endswith('.tar.gz'): continue
        lower = name.lower()
        if not (lower.startswith('sb-') or lower.startswith('sb_')): continue
        full = os.path.join(tenant_dir, name)
        m = SB_FILE_PATTERN.match(name)
        if m:
            try:
                ts = datetime.datetime.strptime(m.group(1)+m.group(2), "%Y%m%d%H%M")
                score = int(ts.timestamp())
            except Exception:
                score = int(os.path.getmtime(full))
        else:
            tm = SB_FILE_TRAILING_DATE.search(name)
            if tm:
                try:
                    dt = datetime.datetime(int(tm.group(1)), int(tm.group(2)), int(tm.group(3)), int(tm.group(4)), int(tm.group(5)), int(tm.group(6)))
                    score = int(dt.timestamp())
                except Exception:
                    score = int(os.path.getmtime(full))
            else:
                score = int(os.path.getmtime(full))
        candidates.append((score, full))
    if not candidates:
        raise ValueError("no support bundles (sb-*.tar.gz) found for tenant")
    candidates.sort(reverse=True)
    return candidates[0][1]

def _extract_bundle(tar_path: str, tenant_id: str, bundle_hash: str, force: bool, reused: bool):
    warnings: List[str] = []
    if os.path.isdir(tar_path) and not tar_path.lower().endswith(('.tar.gz','.tgz')):
        dest = tar_path
        log_dir = os.path.join(dest, 'var', 'log')
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        ptop_logs = glob.glob(os.path.join(log_dir, 'ptop-*.log'))
        return dest, len(ptop_logs), warnings
    tenant_root = os.path.join('/tmp', tenant_id)
    os.makedirs(tenant_root, exist_ok=True)
    dest = os.path.join(tenant_root, bundle_hash[:12])
    need_extract = force or not reused or not os.path.isdir(dest)
    if need_extract:
        if os.path.isdir(dest):
            try: shutil.rmtree(dest)
            except Exception as e: warnings.append(f'extract_cleanup_failed:{e.__class__.__name__}')
        os.makedirs(dest, exist_ok=True)
        try:
            # Safe extraction with member filtering to avoid upcoming Python 3.14 default changes
            def _safe_members(members):
                for m in members:
                    # Prevent absolute paths or path traversal
                    if m.name.startswith('/') or '..' in m.name.split('/'):
                        continue
                    yield m
            with tarfile.open(tar_path, 'r:*') as tf:
                tf.extractall(dest, members=_safe_members(tf.getmembers()))
        except Exception as e:
            raise ValueError(f"failed to extract bundle: {e}")
    log_dir = os.path.join(dest, 'var', 'log')
    ptop_logs = []
    if os.path.isdir(log_dir):
        ptop_logs = glob.glob(os.path.join(log_dir, 'ptop-*.log'))
    return dest, len(ptop_logs), warnings

# ----------------- Tools -----------------



def _load_bundle_impl(path: Optional[str]=None, sptid: Optional[str]=None, force: bool=False, max_files: int=DEFAULT_MAX_FILES, categories: Optional[List[str]]=None) -> dict:
    dbg(f'_load_bundle_impl: path={path} sptid={sptid} force={force} cats={categories}')
    if path is None and sptid and TENANT_PATTERN.fullmatch(sptid):
        path = _auto_select_bundle_tar(sptid)
    if not path and not sptid:
        raise ValueError('sptid or path required')
    tenant_warnings: List[str] = []
    if not sptid and path:
        deduced_tenant, resolved_path, w = _deduce_tenant_and_path(path)
        sptid = deduced_tenant; path = resolved_path; tenant_warnings = w
    if path is None or not os.path.exists(path):
        raise ValueError('path not found')
    if not sptid:
        raise ValueError('sptid deduction failed')
    existing = get_bundle_by_hash(sptid, file_bundle_hash(path))
    if existing and not force:
        set_global_active(existing['bundle_id'])
        return {
            'bundle_id': existing['bundle_id'], 'sptid': existing['sptid'], 'logs_processed': existing['logs_processed'],
            'metrics_ingested': existing['metrics_ingested'], 'time_range': {'start': existing['start_ts'], 'end': existing['end_ts']},
            'reused': True, 'replaced_previous': False, 'warnings': tenant_warnings + []
        }
    if existing and force:
        from .support_store import _get_conn  # type: ignore
        conn=_get_conn(); conn.execute("DELETE FROM bundles WHERE bundle_id=?", (existing['bundle_id'],)); conn.commit()
    now=int(time.time()*1000); bundle_id=f"b-{uuid.uuid4().hex[:10]}"; set_global_active(bundle_id)
    rec={ 'bundle_id': bundle_id, 'sptid': sptid, 'bundle_hash': file_bundle_hash(path), 'path': path, 'host': None,
          'logs_processed': 0, 'metrics_ingested': 0, 'start_ts': now, 'end_ts': now, 'replaced_previous': 0, 'reused': 0,
          'created_at': now, 'plugins': '' }
    insert_bundle(rec)
    metrics_ingested=0; logs_processed=0; start_ts=now; end_ts=now; extract_warnings: List[str]=[]
    try:
        extract_dir, _, extract_warnings = _extract_bundle(path, sptid, rec['bundle_hash'], force, False)
        sel_logs, disc_w = discover_ptop_logs(extract_dir, max_files=max_files)
        cat_set = {c.strip().upper() for c in (categories or [])} or {'CPU'}
        
        # Check if COPY command should be used
        use_copy = os.environ.get('PTOPS_USE_COPY_COMMAND', '').lower() in ('true', '1', 'yes')
        
        # Use optimized TimescaleDB writer with improved batch sizes
        writer = TimescaleWriter(
            batch_size=int(os.environ.get('PTOPS_BATCH_SIZE', '8000')),
            insert_page_size=int(os.environ.get('PTOPS_INSERT_PAGE_SIZE', '800')),
            use_copy=use_copy
        )
        global TIMESCALE_WRITER_LAST
        TIMESCALE_WRITER_LAST = writer
        
        # Use optimized parallel ingestion
        metrics_ingested, logs_processed, start_ts, end_ts = ingest_ptop_logs_optimized(
            sel_logs, bundle_id, rec['bundle_hash'], host=None, vm=writer, allowed_categories=cat_set, sptid=sptid
        )
        from .support_store import _get_conn  # type: ignore
        conn = _get_conn()
        conn.execute(
            "UPDATE bundles SET logs_processed=?, metrics_ingested=?, start_ts=?, end_ts=?, ingested=1, plugins=? WHERE bundle_id=?",
            (logs_processed, metrics_ingested, start_ts, end_ts, ','.join(sorted(cat_set)), bundle_id)
        )
        conn.commit()
        extract_warnings.extend(disc_w)
    except Exception as e:
        dbg(f'load_bundle_impl_error {e.__class__.__name__}:{e}')
        tenant_warnings.append(f'ingest_failed:{e.__class__.__name__}')
    warnings = tenant_warnings + extract_warnings
    return {'bundle_id': bundle_id, 'sptid': sptid, 'logs_processed': logs_processed, 'metrics_ingested': metrics_ingested, 'time_range': {'start': start_ts, 'end': end_ts}, 'reused': False, 'replaced_previous': False, 'warnings': warnings }

@mcp.tool()
def metric_discover(query: str, top_k: int = 3) -> dict:
    """Fast lexical metric discovery.

    Token substring scoring + small CPU category bonus. Returns {query,candidates[]} truncated to top_k.
    No semantic or alias resolution (see metric_search for that)."""
    q = query.lower().strip()
    tokens = {t for t in q.replace('-', ' ').replace(':', ' ').split() if t}
    if top_k <= 0:
        return {'query': query, 'candidates': []}
    candidates = []
    for grp in SCHEMA_SPEC.values():
        for mname, meta in grp.metrics.items():
            score = 0
            if any(tok in mname for tok in tokens):
                score += sum(1 for tok in tokens if tok in mname)
            if 'cpu' in tokens and grp.category == 'cpu':
                score += 1
            if score == 0:
                continue
            candidates.append({
                'metric_name': mname,
                'table': grp.table,
                'view': mname,
                'metric_category': grp.category,
                'local_labels': list(grp.local_labels),
                'score': score
            })
    candidates.sort(key=lambda x: x['score'], reverse=True)
    return {'query': query, 'candidates': candidates[:top_k]}

@mcp.tool()
def metric_schema(metric_name: str) -> dict:
    """Lookup a metric's canonical schema + columns + example SQL.

    Resolves name or alias. Returns {metric_name, view, table, category, columns[], description, example_query}
    or {'error':'metric_not_found'}. Example query placeholders: {bundle_id},{start_ms},{end_ms}."""
    name = metric_name.strip().lower()
    target_group=None; target_metric=None; canonical=None
    for grp in SCHEMA_SPEC.values():
        for mname, meta in grp.metrics.items():
            if mname == name:
                target_group=grp; target_metric=meta; canonical=mname; break
        if target_group: break
    if not target_group:
        for grp in SCHEMA_SPEC.values():
            for mname, meta in grp.metrics.items():
                if name in (meta.aliases or []):
                    target_group=grp; target_metric=meta; canonical=mname; break
            if target_group: break
    if not target_group:
        return {'error': 'metric_not_found', 'metric_name': metric_name}
    cols=[
        {'name':'ts','role':'timestamp','type':'TIMESTAMPTZ','description':'Event timestamp (UTC, high resolution)'},
        {'name':'value','role':'value','type':'DOUBLE PRECISION','description': target_metric.description or 'Primary metric value'},
        {'name':'bundle_id','role':'global','type':'TEXT','description':'Opaque ingestion bundle identifier (filter required)'},
        {'name':'sptid','role':'global','type':'TEXT','description':'Source tenant / support identifier (informational)'},
        {'name':'metric_category','role':'global','type':'TEXT','description':'High-level category (cpu, top, mem, etc.)'},
        {'name':'host','role':'global','type':'TEXT','description':'Host or node name if available'}
    ]
    for lbl in target_group.local_labels:
        desc = 'CPU identifier label (e.g. cpu0, cpu1)' if lbl == 'cpu_id' else f'Local label: {lbl}'
        cols.append({'name': lbl, 'role':'local_label','type':'TEXT','description': desc})
    # Computed helper columns that appear in views but not in base table DDL
    if target_group.category == 'cpu' and 'cpu_id' in target_group.local_labels:
        cols.append({'name': 'cpu_index', 'role': 'local_label', 'type': 'INTEGER', 'description': 'Numeric CPU index derived from cpu_id (cpu0->0) for simplified filtering'})
    example=(
        "-- Fill {bundle_id},{start_ms},{end_ms}\n"
        f"SELECT time_bucket('1 minute', ts) AS bucket, avg(value) AS avg_{canonical}\n"
        f"FROM {canonical}\n"
        "WHERE bundle_id='{bundle_id}'\n"
        "  AND ts BETWEEN to_timestamp({start_ms}/1000.0) AND to_timestamp({end_ms}/1000.0)\n"
        "GROUP BY 1 ORDER BY 1;"
    )
    return {
        'metric_name': canonical,
        'view': canonical,
        'table': target_group.table,
        'category': target_group.category,
        'columns': cols,
        'description': target_metric.description,
        'example_query': example
    }

def _collect_ingest_stats() -> dict:
    """Internal helper to gather low-level writer stats; separated for reuse."""
    global TIMESCALE_WRITER_LAST
    if not TIMESCALE_WRITER_LAST:
        return {'enabled': True, 'initialized': False}
    w = TIMESCALE_WRITER_LAST
    active = get_global_active()
    bundle_id = active.get('bundle_id') if active else None
    row_count = None
    try:
        if getattr(w, '_conn', None) and bundle_id:
            with w._conn.cursor() as cur:  # type: ignore
                cur.execute("SELECT count(*) FROM ptops_cpu WHERE bundle_id=%s", (bundle_id,))
                row_count = cur.fetchone()[0]
    except Exception as e:
        row_count = f'error:{e.__class__.__name__}'
    stats = w.stats() if hasattr(w, 'stats') else {}
    stats.update({'enabled': True, 'initialized': True, 'active_bundle_id': bundle_id, 'timescale_rows_current_bundle': row_count})
    return stats

def init_server() -> dict:
    """Initialize embeddings and (optionally) Timescale.

    Returns status dict with embeddings + timescale (if enabled) + vm stub.
    """
    status: Dict[str,Any] = {}
    try:
        load_embeddings()
        status['embeddings'] = get_embeddings_status()
    except Exception as e:
        status['embeddings'] = f'error:{e.__class__.__name__}'
    try:
        status['timescale'] = bootstrap_timescale()
    except Exception as e:
        status['timescale'] = {'enabled': True, 'error': str(e)}
    return status

@mcp.tool()
def fastpath_architecture() -> dict:
    """Return fast path architecture concept doc (id: concept:fastpath_architecture).

    Use first when question mentions fast path / packet efficiency. Returns {id, level, text, metadata}
    or {'error':...}. Lazy-loads embeddings if needed."""
    from .embeddings_store import ensure_loaded, get_doc  # type: ignore
    try:
        ensure_loaded()
        doc = get_doc('concept:fastpath_architecture')
        if not doc:
            return {'error': 'not_found'}
        return {'id': doc.id, 'level': doc.level, 'text': doc.text, 'metadata': doc.metadata}
    except Exception as e:
        return {'error': e.__class__.__name__, 'detail': str(e).split('\n')[0]}

## Legacy doc/search/alias tools removed; tests migrated to metric_schema/metric_search & fastpath_architecture.

def _metric_search_impl(query: str, top_k: int=5, semantic: bool=True) -> dict:
    """Implementation for metric_search tool (separated for testability)."""
    levels=["L1"]
    dbg(f'metric_search: q={query!r} top_k={top_k} semantic={semantic}')
    if semantic:
        emb = cheap_text_embedding(query)
        matches = semantic_search(emb, top_k=top_k, levels=levels)
    else:
        matches = keyword_search(query, top_k=top_k, levels=levels)
    # alias integration: naive exact alias match (lowercased) using internal resolve_alias if present
    resolved_alias=None
    try:
        from .embeddings_store import resolve_alias as _res_alias  # type: ignore
        alias_docs = _res_alias(query)
        if alias_docs:
            resolved_alias=query
            # ensure alias target docs surface at top (simple boost)
            alias_ids={d.id for d in alias_docs}
            matches = [(d,score+0.05 if d.id in alias_ids else score) for d,score in matches]
            matches.sort(key=lambda x:x[1], reverse=True)
    except Exception:
        pass
    candidates=[]
    for rank,(d,score) in enumerate(matches, start=1):
        candidates.append({
            'doc_id': d.id,
            'metric_name': d.metadata.get('metric_name'),
            'record_type': d.metadata.get('record_type'),
            'score': score or 0.0,
            'rank': rank
        })
    # Heuristic hint injection: user asking for per-process stats -> point to TOP category
    q_l = query.lower()
    if any(tok in q_l for tok in ['process', 'pid', 'per-process', 'per process']) and not any(
        (c.get('metric_name') or '').startswith('process_') for c in candidates
    ):
        candidates.append({
            'doc_id': 'hint:top_process_stats',
            'metric_name': 'top_process_stats',
            'record_type': 'hint',
            'score': 0.01,  # very low so it won't auto-select
            'rank': len(candidates)+1,
            'hint': 'Per-process metrics live under TOP category; ingest with categories=["TOP"] to access process CPU/memory.'
        })
    # Memory-specific per-process hint (SMAPS) when user mentions rss/swap or memory per pid
    if any(tok in q_l for tok in ['rss', 'smaps', 'swap']) and not any(c.get('metric_name') == 'smaps_rss_kb' for c in candidates):
        candidates.append({
            'doc_id': 'hint:smaps_process_memory',
            'metric_name': 'smaps_process_memory',
            'record_type': 'hint',
            'score': 0.01,
            'rank': len(candidates)+1,
            'hint': 'Per-process memory metrics (RSS, swap) live under SMAPS category; ingest with categories=["SMAPS"] to enable.'
        })
    # Disambiguation logic
    decision='no_match'; auto_selected=None; confidence=0.0
    GAP=0.15; ABS=0.90  # thresholds documented in docstring
    if candidates:
        # Ensure ordering by score desc already but recompute just in case
        candidates.sort(key=lambda c: c['score'], reverse=True)
        top1=candidates[0]['score']
        top2=candidates[1]['score'] if len(candidates) > 1 else 0.0
        confidence=top1
        if top1 >= ABS or (top1 - top2) >= GAP:
            decision='auto_select'
            auto_selected=candidates[0]['metric_name']
        else:
            decision='ambiguous'
    out={
        'query': query,
        'candidates': candidates,
        'decision': decision,
        'auto_selected': auto_selected,
        'confidence': confidence,
        'gap_threshold': GAP,
        'abs_threshold': ABS,
        'total_considered': len(candidates),
        'resolved_alias': resolved_alias
    }
    dbg(f"metric_search: decision={decision} auto={auto_selected} cand={len(candidates)} conf={confidence:.3f}")
    return out

@mcp.tool()
def metric_search(query: str, top_k: int=5, semantic: bool=True) -> dict:  # wrapper
    """Search metrics (semantic or lexical) with disambiguation.

    Returns ranked candidates + decision: auto | ambiguous | no_match. Auto if score>=0.90 or gap>=0.15.
    Injects low-score 'hint' entries for missing process/memory categories. Output includes
    {candidates[], decision, auto_selected, confidence, gap_threshold, abs_threshold}."""
    out = _metric_search_impl(query=query, top_k=top_k, semantic=semantic)
    mapping = {'auto_select': 'auto', 'ambiguous': 'ambiguous', 'no_match': 'no_match'}
    out['decision'] = mapping.get(out['decision'], out['decision'])
    out['threshold'] = out.get('gap_threshold')
    return out
@mcp.tool()
def load_bundle(path: Optional[str]=None, tenant_id: Optional[str]=None, force: bool=False, max_files: int=DEFAULT_MAX_FILES, categories: Optional[List[str]]=None) -> dict:
    """Ingest a bundle (directory or sb-*.tar.gz) and activate it.

    Reuses existing bundle if hash matches unless force=True. Optionally restrict categories.
    Returns summary: {bundle_id, sptid, logs_processed, metrics_ingested, time_range, reused, warnings, workflow_prompt}."""
    all_categories = ['CPU','MEM','DISK','NET','TOP','SMAPS','DB','FASTPATH','OTHER']
    eff_categories = categories if categories else all_categories
    out = _load_bundle_impl(path=path, sptid=tenant_id, force=force, max_files=max_files, categories=eff_categories)
    # Attach workflow/system guidance so clients immediately know how to proceed without extra call
    out['workflow_prompt'] = SYSTEM_PROMPT
    out['workflow_version'] = 1  # bump if semantics change
    return out

@mcp.tool()
def active_context(tenant_id: Optional[str]=None) -> dict:
    """Return current active bundle metadata or null placeholders.

    Provides bundle_id, path, time_range, metrics_ingested, sptid. Use before queries."""
    ga = get_global_active()
    if not ga:
        return {'bundle_id': None, 'path': None, 'time_range': None, 'metrics_ingested': 0}
    b = get_bundle(ga['bundle_id'])
    if not b:
        return {'bundle_id': ga['bundle_id'], 'path': None, 'time_range': None, 'metrics_ingested': 0}
    return {
        'bundle_id': b['bundle_id'],
        'path': os.path.abspath(b['path']) if b.get('path') else None,
        'time_range': {'start_ms': b['start_ts'], 'end_ms': b['end_ts']},
        'metrics_ingested': b.get('metrics_ingested'),
        'sptid': b.get('sptid')
    }

@mcp.tool()
def list_bundles_tool(tenant_id: Optional[str]=None) -> List[dict]:
    """List all bundles with active flag and basic counts.

    Returns list[{bundle_id,sptid,path,created_at,active,logs_processed}]."""
    rows = list_all_bundles()
    ga = get_global_active(); active_id = ga['bundle_id'] if ga else None
    out = []
    for r in rows:
        out.append({
            'bundle_id': r['bundle_id'], 'sptid': r['sptid'], 'path': r['path'], 'created_at': r['created_at'],
            'active': r['bundle_id'] == active_id, 'logs_processed': r['logs_processed']
        })
    return out

@mcp.tool()
def unload_bundle(tenant_id: Optional[str]=None, bundle_id: Optional[str]=None, purge_all: bool=False) -> dict:
    """Unload (delete) a bundle or purge all.

    If bundle_id omitted uses active. purge_all=True removes every bundle and clears active pointer.
    Returns status including promoted_bundle_id if another became active."""
    if purge_all:
        rows = list_all_bundles(); removed=len(rows)
        from .support_store import _get_conn  # type: ignore
        conn=_get_conn(); conn.execute("DELETE FROM bundles"); conn.execute("UPDATE global_active SET bundle_id=NULL WHERE id=1"); conn.commit()
        return {'purged_all': True, 'removed': removed}
    from .support_store import _get_conn  # type: ignore
    conn=_get_conn()
    if not bundle_id:
        ga=get_global_active(); bundle_id=ga['bundle_id'] if ga else None
        if not bundle_id:
            return {'bundle_id': None, 'path': None, 'unloaded': False, 'purged': False, 'active_cleared': False}
    cur=conn.execute("SELECT * FROM bundles WHERE bundle_id=?", (bundle_id,)); row=cur.fetchone()
    if not row: raise ValueError('bundle not found')
    target_bundle_id=row['bundle_id']; target_path=row['path']; bundle_hash=row['bundle_hash']; sptid=row['sptid']
    ga=get_global_active(); active_cleared=bool(ga and ga['bundle_id']==target_bundle_id)
    purged=False
    if target_bundle_id and bundle_hash:
        extract_dir=os.path.join('/tmp', sptid, bundle_hash[:12])
        if os.path.isdir(extract_dir):
            try: shutil.rmtree(extract_dir); purged=True
            except Exception: pass
    if target_bundle_id:
        conn.execute("DELETE FROM bundles WHERE bundle_id=?", (target_bundle_id,)); conn.commit()
    if active_cleared:
        unload_global_active(); promoted_id=promote_random_bundle()
    else:
        promoted_id=None
    return {'bundle_id': target_bundle_id, 'path': target_path, 'unloaded': bool(target_bundle_id), 'purged': purged, 'active_cleared': active_cleared, 'promoted_bundle_id': promoted_id}

@mcp.tool()
def ingest_status(tenant_id: Optional[str]=None) -> dict:
    """Return ingestion summary + writer stats for active bundle (or placeholders).

    Returns {state,bundle_id,summary?,stats,notes}. summary is None if no active bundle."""
    ga = get_global_active()
    if not ga:
        return {'state': 'idle', 'bundle_id': None, 'summary': None, 'stats': _collect_ingest_stats(), 'notes': []}
    b = get_bundle(ga['bundle_id'])
    if not b:
        return {'state': 'idle', 'bundle_id': ga['bundle_id'], 'summary': None, 'stats': _collect_ingest_stats(), 'notes': []}
    summary = {
        'bundle_id': b['bundle_id'], 'sptid': b.get('sptid'), 'logs_processed': b['logs_processed'],
        'metrics_ingested': b['metrics_ingested'], 'time_range': {'start': b['start_ts'], 'end': b['end_ts']},
        'reused': bool(b['reused']), 'warnings': []
    }
    return {'state': 'idle', 'bundle_id': b['bundle_id'], 'summary': summary, 'stats': _collect_ingest_stats(), 'notes': []}


@mcp.tool()
def timescale_sql(sql: str, max_rows: int = 500) -> dict:
    """Run a safe read-only SELECT / WITH query (single statement) on Timescale views.

    Rejects non-SELECT/with keywords and multiple statements. Auto LIMIT max_rows if none provided.
    Returns {columns, rows, records, row_count, truncated} or {'error':...}."""
    global TIMESCALE_DIRECT_CONN
    q = (sql or '').strip()
    if not q:
        return {'error': 'empty_query'}
    # Normalize and extract the first meaningful keyword (skip comments / whitespace)
    import re
    # Remove leading SQL comments
    tmp = q
    # Strip /* ... */ block comments at start
    while True:
        m = re.match(r"^(\s*/\*.*?\*/\s*)", tmp, flags=re.DOTALL)
        if not m: break
        tmp = tmp[m.end():]
    # Strip leading -- comments
    while True:
        m = re.match(r"^(\s*--[^\n]*\n)", tmp)
        if not m: break
        tmp = tmp[m.end():]
    m = re.match(r"^([a-zA-Z]+)", tmp)
    if not m:
        return {'error': 'parse_error', 'detail': 'could_not_extract_first_token'}
    first_kw = m.group(1).lower()
    # Allow SELECT or WITH (CTEs). Disallow DML/DDL keywords.
    disallowed = {'update','delete','insert','merge','alter','create','drop','truncate','grant','revoke','vacuum','analyze','call'}
    if first_kw in disallowed:
        return {'error': 'only_select_allowed'}
    if first_kw not in {'select','with'}:
        # Any other leading keyword is rejected to keep surface conservative (e.g. EXPLAIN, SHOW)
        return {'error': 'only_select_allowed'}
    core = q.rstrip(';')
    if ';' in core:
        return {'error': 'multiple_statements_disallowed'}
    conn = None
    if TIMESCALE_WRITER_LAST and getattr(TIMESCALE_WRITER_LAST, '_conn', None):  # type: ignore
        conn = TIMESCALE_WRITER_LAST._conn  # type: ignore
    else:
        if TIMESCALE_DIRECT_CONN is None:
            dsn = os.environ.get('TIMESCALE_DSN')
            if not dsn:
                return {'error': 'no_dsn'}
            try:
                TIMESCALE_DIRECT_CONN = psycopg.connect(dsn)
            except Exception as e:  # pragma: no cover
                return {'error': 'connect_failed', 'detail': str(e).split('\n')[0]}
        conn = TIMESCALE_DIRECT_CONN
    enforce_limit = ' limit ' not in sql.lower()
    wrapped = f"WITH _q AS ({core}) SELECT * FROM _q LIMIT {int(max_rows)}" if enforce_limit else core
    try:
        with conn.cursor() as cur:  # type: ignore
            cur.execute(wrapped)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            truncated = enforce_limit and len(rows) == max_rows
            # JSON-friendly records (Plotly etc.). Convert datetimes to ISO8601.
            def _json_val(v):
                import datetime as _dt, decimal as _dec
                if isinstance(v, _dt.datetime):
                    return v.isoformat()
                if isinstance(v, _dec.Decimal):
                    return float(v)
                return v
            records = [ { cols[i]: _json_val(val) for i,val in enumerate(row) } for row in rows ]
            return {'columns': cols, 'rows': rows, 'records': records, 'row_count': len(rows), 'truncated': truncated}
    except Exception as e:
        # Rollback transaction and reset connection on error to prevent stuck transaction state
        try:
            conn.rollback()  # type: ignore
        except:
            pass
        # Reset direct connection if it was used (not from writer)
        if conn is TIMESCALE_DIRECT_CONN:
            try:
                TIMESCALE_DIRECT_CONN.close()  # type: ignore
            except:
                pass
            TIMESCALE_DIRECT_CONN = None
        return {'error': e.__class__.__name__, 'detail': str(e).split('\n')[0]}


# --------------- HTTP SSE Runner via mcp.run ---------------
# We prefer using the fastmcp provided MCP.run() method directly (no separate run_http import).
# This keeps the entrypoint minimal and matches the user's request to avoid auxiliary wrappers.

if __name__ == '__main__':
    print('Initializing server...')
    print(init_server())
    host = os.environ.get('HOST','0.0.0.0')
    port = int(os.environ.get('PORT','8000'))
    print(f'Starting FastMCP on {host}:{port}')
    run_attr = getattr(mcp, 'run', None)
    if callable(run_attr):
        run_attr(transport="http", host=host, port=port, stateless_http=True)
    else:
        import sys
        print('FastMCP run() missing. fastmcp version likely incompatible or not installed correctly.')
        print('fastmcp module version:', getattr(__import__('fastmcp'),'__version__','unknown'))
        print('Available attributes on mcp instance:', [a for a in dir(mcp) if not a.startswith('_')][:40])
        sys.exit(1)

    