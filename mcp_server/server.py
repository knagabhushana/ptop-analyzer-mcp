from fastapi import FastAPI, HTTPException
from mcp_server import mcp_app

app = FastAPI(title="ptops-mcp-shim")

@app.get("/")
def root():
    return {"status": "ok", "service": "ptops-mcp-shim"}

@app.get("/healthz")
def healthz():
    return mcp_app.healthz()

@app.post("/support/load_bundle")
def load_bundle(body: dict):
    try:
        max_files = body.get('max_files') if isinstance(body.get('max_files'), int) else None
        return mcp_app.load_bundle(path=body.get('path'), tenant_id=body.get('tenant_id'), force=body.get('force', False), max_files=max_files or 0)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/support/active_context/{tenant_id}")
def active_context(tenant_id: str):
    try:
        return mcp_app.active_context(tenant_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/support/list_bundles/{tenant_id}")
def list_bundles(tenant_id: str):
    return mcp_app.list_bundles_tool(tenant_id)

"""Minimal FastAPI shim only if fastmcp package absent.
This file intentionally kept tiny; full typing lives in mcp_app if needed.
"""
    candidates: List[MetricSearchCandidate]
    decision: str  # auto | ambiguous | no_match
    auto_selected: Optional[str] = None  # metric_name if auto
    confidence: float  # heuristic gap metric
    threshold: float  # threshold used
    total_considered: int

# ----------------- Metrics (VictoriaMetrics) API Models -----------------

class MetricsListResponse(BaseModel):
    metrics: List[str]
    warnings: List[str] = []

class LabelValuesResponse(BaseModel):
    label: str
    values: List[str]
    warnings: List[str] = []

class MetricMetadataResponse(BaseModel):
    metric: str
    doc: Optional[DocDetail] = None
    present: bool = False  # whether any series currently exist for this tenant
    warnings: List[str] = []

class QueryRangeRequest(BaseModel):
    tenant_id: str
    query: str
    start_ms: int
    end_ms: int
    step_ms: int

class InstantSample(BaseModel):
    ts_ms: int
    value: Optional[float]

class SeriesData(BaseModel):
    expression: str
    labels: Dict[str, Any]
    samples: List[InstantSample]

class QueryRangeResponse(BaseModel):
    expression: str
    series: List[SeriesData]
    warnings: List[str] = []

class GraphTimeseriesRequest(BaseModel):
    tenant_id: str
    metrics: List[str]  # list of metric names or expressions
    start_ms: int
    end_ms: int
    step_ms: int

class GraphTimeseriesResponse(BaseModel):
    series: List[SeriesData]
    warnings: List[str] = []

class GraphCompareRequest(BaseModel):
    tenant_id: str
    primary: str
    comparators: List[str]
    start_ms: int
    end_ms: int
    step_ms: int
    normalize: bool = False  # if true compute each comparator value / sum at ts

class GraphCompareResponse(BaseModel):
    primary: List[SeriesData]
    comparators: List[SeriesData]
    warnings: List[str] = []

def _doc_to_ref(d, score: Optional[float] = None) -> DocRef:
    return DocRef(
        id=d.id,
        level=d.level,
        record_type=d.metadata.get('record_type'),
        metric_name=d.metadata.get('metric_name'),
        score=score
    )

def _doc_to_detail(d, score: Optional[float] = None) -> DocDetail:
    return DocDetail(
        id=d.id,
        level=d.level,
        record_type=d.metadata.get('record_type'),
        metric_name=d.metadata.get('metric_name'),
        text=d.text,
        metadata=d.metadata,
        score=score
    )

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "time": int(time.time()*1000)}

TENANT_PATTERN = re.compile(r"(NIOSSPT[-_]?\d+)", re.IGNORECASE)
SB_FILE_PATTERN = re.compile(r"sb-(\d{8})_(\d{4}).*\.tar\.gz$", re.IGNORECASE)
SB_FILE_TRAILING_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})")  # YYYY-MM-DD-HH-MM-SS
SUPPORT_BASE_DIR = os.environ.get("SUPPORT_BASE_DIR", "/import/customer_data/support")

def _deduce_tenant_and_path(path: str) -> tuple[str, str, list[str]]:
    """Attempt to deduce tenant id and possibly refine path.
    Returns (tenant_id, resolved_path, warnings).
    Heuristics:
      * If path is file: extract NIOSSPT-<id> from filename; else try tar members.
      * If path is dir containing 'NIOSSPT' pattern in its name use that name.
      * If path is a base support directory (no pattern): pick latest child directory or tar file.
      * Fallback: synthetic tenant 'anon-' + first 12 chars of sha256(path).
    """
    warnings: list[str] = []
    import tarfile, hashlib
    original_path = path
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="path not found")
    def _hash_id(p: str) -> str:
        return 'anon-' + hashlib.sha256(p.encode()).hexdigest()[:12]
    # If directory
    if os.path.isdir(path):
        base_name = os.path.basename(os.path.normpath(path))
        m = TENANT_PATTERN.search(base_name)
        if m:
            return (m.group(1).upper(), path, warnings)
        # choose latest child (dir or tar.gz) containing support data
        entries = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
            except FileNotFoundError:
                continue
            entries.append((st.st_mtime, full))
        if not entries:
            warnings.append('empty_directory_no_children')
            return (_hash_id(path), path, warnings)
        entries.sort(reverse=True)
        chosen = entries[0][1]
        # Recurse once if directory
        if os.path.isdir(chosen):
            cbase = os.path.basename(chosen)
            m2 = TENANT_PATTERN.search(cbase)
            if m2:
                return (m2.group(1).upper(), chosen, warnings)
            # If still no match use hash
            warnings.append('no_tenant_pattern_in_latest_dir')
            return (_hash_id(chosen), chosen, warnings)
        else:
            # file
            path = chosen
            # fall through to file handling
    # File path (.tar.gz or log)
    fname = os.path.basename(path)
    m = TENANT_PATTERN.search(fname)
    if m:
        return (m.group(1).upper(), path, warnings)
    # attempt tar member scan if tar.gz
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
    """Select latest sb-*.tar.gz for tenant based on timestamp in filename or mtime.
    Returns full path or raises HTTPException."""
    base_dir = os.environ.get("SUPPORT_BASE_DIR", SUPPORT_BASE_DIR)
    tenant_dir = os.path.join(base_dir, tenant_id)
    if not os.path.isdir(tenant_dir):
        raise HTTPException(status_code=404, detail=f"tenant directory not found: {tenant_dir}")
    candidates = []
    try:
        for name in os.listdir(tenant_dir):
            if not name.lower().endswith('.tar.gz'):
                continue
            lower = name.lower()
            if not (lower.startswith('sb-') or lower.startswith('sb_')):
                continue
            full = os.path.join(tenant_dir, name)
            m = SB_FILE_PATTERN.match(name)
            if m:
                try:
                    ts = datetime.datetime.strptime(m.group(1)+m.group(2), "%Y%m%d%H%M")
                    score = int(ts.timestamp())
                except Exception:
                    score = int(os.path.getmtime(full))
            else:
                # attempt trailing date pattern anywhere in name: *_YYYY-MM-DD-HH-MM-SS*
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
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="tenant directory inaccessible")
    if not candidates:
        raise HTTPException(status_code=404, detail="no support bundles (sb-*.tar.gz) found for tenant")
    candidates.sort(reverse=True)
    return candidates[0][1]


def _extract_bundle(tar_path: str, tenant_id: str, bundle_hash: str, force: bool, reused: bool) -> tuple[str, int, list[str]]:
    """Extract tar bundle into /tmp/<tenant_id>/<hashprefix>. Returns (extract_dir, logs_processed, warnings)."""
    warnings: list[str] = []
    # If caller passed an already-extracted directory (development / tests), just treat it as dest root
    if os.path.isdir(tar_path) and not tar_path.lower().endswith(('.tar.gz','.tgz')):
        # ensure expected layout (var/log) exists or create minimal
        dest = tar_path
        log_dir = os.path.join(dest, 'var', 'log')
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        ptop_logs = sorted(glob.glob(os.path.join(log_dir, 'ptop-*.log')))
        return dest, len(ptop_logs), warnings
    tenant_root = os.path.join('/tmp', tenant_id)
    os.makedirs(tenant_root, exist_ok=True)
    dest = os.path.join(tenant_root, bundle_hash[:12])
    need_extract = force or not reused or not os.path.isdir(dest)
    if need_extract:
        if os.path.isdir(dest):
            try:
                shutil.rmtree(dest)
            except Exception as e:
                warnings.append(f'extract_cleanup_failed:{e.__class__.__name__}')
        os.makedirs(dest, exist_ok=True)
        try:
            with tarfile.open(tar_path, 'r:*') as tf:
                tf.extractall(dest)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"failed to extract bundle: {e}")
    # Scan for ptop logs
    log_dir = os.path.join(dest, 'var', 'log')
    ptop_logs = []
    if os.path.isdir(log_dir):
        ptop_logs = sorted(glob.glob(os.path.join(log_dir, 'ptop-*.log')))
    logs_processed = len(ptop_logs)
    return dest, logs_processed, warnings


@app.post("/support/load_bundle", response_model=LoadBundleResponse)
async def api_load_bundle(req: LoadBundleRequest):
    # If only tenant_id provided and matches pattern, auto-discover path
    if req.path is None and req.tenant_id and TENANT_PATTERN.fullmatch(req.tenant_id):
        req.path = _auto_select_bundle_tar(req.tenant_id)
    if not req.path and not req.tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id or path required")
    # If tenant not given attempt previous heuristic
    if not req.tenant_id and req.path:
        tenant_warnings: list[str] = []
        try:
            deduced_tenant, resolved_path, w = _deduce_tenant_and_path(req.path)
            req.tenant_id = deduced_tenant
            req.path = resolved_path
            tenant_warnings = w
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"failed to deduce tenant: {e}")
    else:
        tenant_warnings = []
    tenant_warnings: list[str] = []
    if req.path is None:
        raise HTTPException(status_code=400, detail="resolved path missing")
    if not os.path.exists(req.path):
        raise HTTPException(status_code=400, detail="path not found")
    if not req.tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id deduction failed")
    # compute hash and check reuse
    b_hash = file_bundle_hash(req.path)
    reused = False
    replaced_previous = False
    existing = get_bundle_by_hash(req.tenant_id, b_hash)  # type: ignore[arg-type]
    if existing and not req.force:
        reused = True
        bundle_id = existing['bundle_id']
        # ensure active context points to this bundle
        ac = get_active_context(req.tenant_id)  # type: ignore[arg-type]
        if not ac or ac['bundle_id'] != bundle_id:
            set_active_context(req.tenant_id, bundle_id)  # type: ignore[arg-type]
        return LoadBundleResponse(
            bundle_id=bundle_id,
            tenant_id=req.tenant_id,
            logs_processed=existing['logs_processed'],
            metrics_ingested=existing['metrics_ingested'],
            time_range={"start": existing['start_ts'], "end": existing['end_ts']},
            replaced_previous=False,
            reused=True,
            warnings=[]
        )
    if existing and req.force:
        # remove existing row to permit re-insert with new bundle_id under same hash
        from .support_store import _get_conn  # type: ignore
        conn = _get_conn()
        conn.execute("DELETE FROM bundles WHERE bundle_id=?", (existing['bundle_id'],))
        conn.commit()
    # initial defaults before ingestion
    now = int(time.time()*1000)
    logs_processed = 0
    metrics_ingested = 0
    start_ts = now
    end_ts = now
    bundle_id = f"b-{uuid.uuid4().hex[:10]}"
    # detect previous active
    prev = get_active_context(req.tenant_id)  # type: ignore[arg-type]
    if prev and prev['bundle_id'] != bundle_id:
        replaced_previous = True
    record = {
        'bundle_id': bundle_id,
        'tenant_id': req.tenant_id,
        'bundle_hash': b_hash,
        'path': req.path,
        'host': None,
        'logs_processed': logs_processed,
        'metrics_ingested': metrics_ingested,
        'start_ts': start_ts,
        'end_ts': end_ts,
        'replaced_previous': 1 if replaced_previous else 0,
        'reused': 1 if reused else 0,
        'created_at': now
    }
    insert_bundle(record)
    set_active_context(req.tenant_id, bundle_id)  # type: ignore[arg-type]
    # Perform extraction and logging AFTER registration so status endpoints work even if extraction slow
    extract_warnings: list[str] = []
    try:
        extract_dir, _, extract_warnings = _extract_bundle(req.path, req.tenant_id, b_hash, req.force, reused)  # type: ignore[arg-type]
        max_files = req.max_files if req.max_files and req.max_files > 0 else DEFAULT_MAX_FILES
        selected_logs, discover_warnings = discover_ptop_logs(extract_dir, max_files=max_files)
        vm = VictoriaMetricsWriter()
        metrics_ingested, logs_processed, start_ts, end_ts = ingest_ptop_logs(selected_logs, req.tenant_id, bundle_id, b_hash, host=None, vm=vm)  # type: ignore[arg-type]
        from .support_store import _get_conn  # type: ignore
        conn = _get_conn()
        conn.execute("UPDATE bundles SET logs_processed=?, metrics_ingested=?, start_ts=?, end_ts=?, ingested=1 WHERE bundle_id=?", (logs_processed, metrics_ingested, start_ts, end_ts, bundle_id))
        conn.commit()
        extract_warnings.extend(discover_warnings)
    except HTTPException:
        raise
    except Exception as e:
        tenant_warnings.append(f'ingest_failed:{e.__class__.__name__}')
    warnings = tenant_warnings + extract_warnings
    return LoadBundleResponse(
        bundle_id=bundle_id,
        tenant_id=req.tenant_id,
        logs_processed=logs_processed,
        metrics_ingested=metrics_ingested,
        time_range={"start": start_ts, "end": end_ts},
        replaced_previous=replaced_previous,
        reused=reused,
        warnings=warnings
    )


@app.get("/support/active_context", response_model=ActiveContextResponse)
async def api_active_context(tenant_id: str):
    ac = get_active_context(tenant_id)
    if not ac:
        raise HTTPException(status_code=404, detail="no active context")
    return ActiveContextResponse(
        tenant_id=tenant_id,
        bundle_id=ac['bundle_id'],
        activated_at=ac['activated_at'],
        logs_processed=ac['logs_processed'],
        metrics_ingested=ac['metrics_ingested'],
        time_range={"start": ac['start_ts'], "end": ac['end_ts']}
    )


@app.get("/support/ingest_status", response_model=IngestStatusResponse)
async def api_ingest_status(tenant_id: str):
    ac = get_active_context(tenant_id)
    if not ac:
        return IngestStatusResponse(state="idle", bundle_id=None, progress=None, summary=None)
    # synchronous ingestion: always idle with summary
    b = get_bundle(ac['bundle_id'])
    if not b:
        return IngestStatusResponse(state="idle", bundle_id=ac['bundle_id'], progress=None, summary=None)
    summary = LoadBundleResponse(
        bundle_id=b['bundle_id'], tenant_id=b['tenant_id'], logs_processed=b['logs_processed'],
        metrics_ingested=b['metrics_ingested'], time_range={"start": b['start_ts'], "end": b['end_ts']},
        replaced_previous=bool(b['replaced_previous']), reused=bool(b['reused']), warnings=[])
    return IngestStatusResponse(state="idle", bundle_id=ac['bundle_id'], progress=None, summary=summary)


@app.post("/support/unload_bundle", response_model=UnloadResponse)
async def api_unload(req: UnloadRequest):
    if req.purge_all:
        removed = delete_all_bundles_for_tenant(req.tenant_id)
        tenant_root = os.path.join('/tmp', req.tenant_id)
        purged_root = False
        if os.path.isdir(tenant_root):
            try:
                shutil.rmtree(tenant_root)
                purged_root = True
            except Exception:
                pass
        return UnloadResponse(tenant_id=req.tenant_id, bundle_id=None, path=None, unloaded=removed>0, purged=purged_root, active_cleared=True, total_removed=removed, all_purged=True)
    # Determine target bundle
    target_bundle_id: Optional[str] = None
    target_path: Optional[str] = None
    from .support_store import _get_conn  # type: ignore
    conn = _get_conn()
    if req.bundle_id:
        cur = conn.execute("SELECT * FROM bundles WHERE bundle_id=? AND tenant_id=?", (req.bundle_id, req.tenant_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="bundle not found")
        target_bundle_id = row['bundle_id']
        target_path = row['path']
        bundle_hash = row['bundle_hash']
    elif req.path:
        cur = conn.execute("SELECT * FROM bundles WHERE path=? AND tenant_id=?", (req.path, req.tenant_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="bundle with path not found")
        target_bundle_id = row['bundle_id']
        target_path = row['path']
        bundle_hash = row['bundle_hash']
    else:
        # fallback: unload active context only
        ac = get_active_context(req.tenant_id)
        if ac:
            target_bundle_id = ac['bundle_id']
            b = get_bundle(ac['bundle_id'])
            target_path = b['path'] if b else None
            bundle_hash = b['bundle_hash'] if b else None
        else:
            return UnloadResponse(tenant_id=req.tenant_id, bundle_id=None, path=None, unloaded=False, purged=False, active_cleared=False)
    # Remove active context if matches
    active_cleared = False
    ac = get_active_context(req.tenant_id)
    if ac and ac['bundle_id'] == target_bundle_id:
        unload_active(req.tenant_id)
        active_cleared = True
    # Purge extraction dir for this bundle
    purged = False
    if target_bundle_id and 'bundle_hash' in locals() and bundle_hash:
        extract_dir = os.path.join('/tmp', req.tenant_id, bundle_hash[:12])
        if os.path.isdir(extract_dir):
            try:
                shutil.rmtree(extract_dir)
                purged = True
            except Exception:
                pass
    # Delete bundle from database
    if target_bundle_id:
        # Delete bundle row
        from .support_store import _get_conn  # type: ignore
        conn = _get_conn()
        conn.execute("DELETE FROM bundles WHERE bundle_id=?", (target_bundle_id,))
        conn.commit()
    return UnloadResponse(tenant_id=req.tenant_id, bundle_id=target_bundle_id, path=target_path, unloaded=bool(target_bundle_id), purged=purged, active_cleared=active_cleared)

@app.get("/support/bundles")
async def api_list_bundles(tenant_id: str):
    rows = list_bundles(tenant_id)
    ac = get_active_context(tenant_id)
    active_id = ac['bundle_id'] if ac else None
    # Return minimal info with original paths
    return [
        {
            'bundle_id': r['bundle_id'],
            'tenant_id': r['tenant_id'],
            'path': r['path'],
            'created_at': r['created_at'],
            'active': r['bundle_id'] == active_id,
            'logs_processed': r['logs_processed']
        } for r in rows
    ]

# ----------------- Documentation / Embeddings APIs -----------------

@app.get("/docs/plugins", response_model=List[str])
async def api_list_plugins():
    """List available plugin / record_type identifiers with embedded documentation.

    Usage: discovery of valid values for subsequent /docs/plugin/{plugin} calls or
    for client-side filtering (e.g. show only CPU or DISK metrics). Returns sorted list.
    """
    return list_plugins()

@app.get("/docs/plugin/{plugin}", response_model=List[DocRef])
async def api_plugin_docs(plugin: str):
    """Return lightweight references (no full text) for all docs belonging to a plugin (record_type).

    Typical caller flow: list plugins -> fetch doc refs -> request specific doc(s) by id.
    """
    docs = list_plugin_docs(plugin)
    return [_doc_to_ref(d) for d in docs]

@app.get("/docs/doc/{doc_id}", response_model=DocDetail)
async def api_get_doc(doc_id: str):
    """Fetch full text + metadata for a documentation embedding by id.

    Supports all levels (L1 metric field, L2 plugin summary, L4 concept etc.).
    Returns 404 if id unknown.
    """
    d = get_doc(doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="doc not found")
    return _doc_to_detail(d)

@app.get("/docs/metric/{metric_name}", response_model=MetricLookupResponse)
async def api_get_metric(metric_name: str):
    """Convenience lookup by canonical metric_name (case-insensitive).

    Returns the L1 metric doc detail if present; null doc when metric not embedded.
    """
    d = get_metric(metric_name)
    return MetricLookupResponse(name=metric_name, doc=_doc_to_detail(d) if d else None)

@app.get("/docs/alias/{alias}", response_model=AliasResolveResponse)
async def api_alias(alias: str):
    """Resolve a legacy or shorthand alias token to one or more canonical docs.

    Example: disk_read_kib_per_sec -> disk_read_kb_per_sec L1 doc.
    Multiple docs may match if alias reused historically across contexts.
    """
    ds = resolve_alias(alias)
    return AliasResolveResponse(alias=alias, docs=[_doc_to_ref(d) for d in ds])

@app.get("/docs/concepts", response_model=List[str])
async def api_concepts():
    """List high-level concept document ids (L4) for architectural / design queries."""
    return list_concepts()

@app.post("/docs/search", response_model=List[DocRef])
async def api_search(req: SearchRequest):
    """Search documentation corpus returning lightweight references.

    Request fields:
      query: free-text query string
      semantic: if true perform vector similarity; else lexical keyword scoring
      levels: optional subset filter (e.g. ["L1","L4"]) to scope search
      top_k: max results
      query_embedding: optional precomputed embedding (must match stored dim)

    Response contains doc refs with similarity scores (semantic) or hit ratio (keyword).
    """
    if req.semantic:
        if req.query_embedding is not None and len(req.query_embedding) == 0:
            raise HTTPException(status_code=400, detail="query_embedding must be non-empty when provided")
        emb = req.query_embedding or cheap_text_embedding(req.query)
        matches = semantic_search(emb, top_k=req.top_k, levels=req.levels)
    else:
        matches = keyword_search(req.query, top_k=req.top_k, levels=req.levels)
    return [_doc_to_ref(d, score) for d, score in matches]

@app.post("/docs/search/detail", response_model=List[DocDetail])
async def api_search_detail(req: SearchRequest):
    """Search documentation corpus returning full document bodies.

    Same parameters as /docs/search but returns full text for each match (higher payload).
    Intended for scenarios where client immediately needs content (e.g., inline explanation).
    """
    if req.semantic:
        if req.query_embedding is not None and len(req.query_embedding) == 0:
            raise HTTPException(status_code=400, detail="query_embedding must be non-empty when provided")
        emb = req.query_embedding or cheap_text_embedding(req.query)
        matches = semantic_search(emb, top_k=req.top_k, levels=req.levels)
    else:
        matches = keyword_search(req.query, top_k=req.top_k, levels=req.levels)
    return [_doc_to_detail(d, score) for d, score in matches]


@app.post("/docs/search/metrics", response_model=MetricSearchResponse)
async def api_search_metrics(req: SearchRequest):
    """Specialized metric discovery endpoint with confidence heuristics.

    Automatically constrains search to L1 (metric field) docs and computes a
    simple confidence score = top1_score - top2_score (or top1_score if only one).
    Heuristic decision:
      * no_match: no candidates returned
      * auto: confidence >= GAP_THRESHOLD or top1_score >= ABS_THRESHOLD
      * ambiguous: otherwise (client should present candidates to user)
    """
    levels = ["L1"]
    if req.semantic:
        if req.query_embedding is not None and len(req.query_embedding) == 0:
            raise HTTPException(status_code=400, detail="query_embedding must be non-empty when provided")
        emb = req.query_embedding or cheap_text_embedding(req.query)
        matches = semantic_search(emb, top_k=req.top_k, levels=levels)
    else:
        matches = keyword_search(req.query, top_k=req.top_k, levels=levels)
    candidates: List[MetricSearchCandidate] = []
    for rank, (d, score) in enumerate(matches, start=1):
        candidates.append(MetricSearchCandidate(
            doc_id=d.id,
            metric_name=d.metadata.get('metric_name'),
            record_type=d.metadata.get('record_type'),
            score=score if score is not None else 0.0,
            rank=rank
        ))
    decision = 'no_match'
    auto_selected = None
    confidence = 0.0
    GAP_THRESHOLD = 0.15
    ABS_THRESHOLD = 0.90
    if candidates:
        decision = 'ambiguous'
        if len(candidates) == 1:
            confidence = candidates[0].score
        else:
            confidence = candidates[0].score - candidates[1].score
        if candidates[0].score >= ABS_THRESHOLD or confidence >= GAP_THRESHOLD:
            decision = 'auto'
            auto_selected = candidates[0].metric_name
            # If metric_name missing, still treat as ambiguous fallback
            if not auto_selected:
                decision = 'ambiguous'
    return MetricSearchResponse(
        query=req.query,
        candidates=candidates,
        decision=decision,
        auto_selected=auto_selected,
        confidence=confidence,
        threshold=GAP_THRESHOLD,
        total_considered=len(candidates)
    )

# NOTE: Full MCP protocol adapter not implemented yet; this REST stub helps container build/run.

# ----------------- Metrics (VictoriaMetrics) API Implementation -----------------

import os, urllib.parse, http.client, json, math, time as _time

VM_BASE = os.environ.get('VM_BASE_URL')
VM_TIMEOUT = float(os.environ.get('VM_TIMEOUT_MS', '5000')) / 1000.0

def _vm_disabled():
    return not VM_BASE  # kept for potential future global checks; not used inside hot paths

def _http_get(path: str, params: Dict[str,str]) -> dict:
    parsed = urllib.parse.urlparse(VM_BASE)  # type: ignore[arg-type]
    host = parsed.netloc or parsed.path
    query = urllib.parse.urlencode(params, doseq=True)
    base_path = parsed.path if isinstance(parsed.path, str) else ''
    full_path = (base_path.rstrip('/') if parsed.netloc else '') + path + ('?' + query if query else '')
    conn_cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    conn = conn_cls(str(host), timeout=VM_TIMEOUT)
    dbg(f'_http_get start path={path} full_path={full_path}')
    try:
        conn.request('GET', full_path)
        resp = conn.getresponse()
        body = resp.read()
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            data = {'status':'error','error':'invalid_json','raw': body[:200].decode('utf-8','ignore')}
        dbg(f'_http_get done path={path} status={data.get("status")} keys={list(data.keys())}')
        return data
    finally:
        try: conn.close()
        except Exception: pass

def _inject_tenant(selector: str, tenant_id: str) -> str:
    # Reject conflicting tenant_id
    if 'tenant_id=' in selector:
        # very simple extraction
        m = re.search(r'tenant_id\s*=\s*"([^"]+)"', selector)
        if m and m.group(1) != tenant_id:
            raise HTTPException(status_code=400, detail='tenant_id_mismatch_in_query')
        return selector  # already present
    # bare metric name?
    if re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', selector):
        return f'{selector}{{tenant_id="{tenant_id}"}}'
    # metric with existing label set
    def repl(match: re.Match):
        inner = match.group(2).strip()
        if not inner:
            return f'{match.group(1)}{{tenant_id="{tenant_id}"}}'
        return f'{match.group(1)}{{{inner},tenant_id="{tenant_id}"}}'
    new_selector, count = re.subn(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{([^}]*)\}', repl, selector, count=1)
    if count:
        return new_selector
    # Fallback: append label matcher at end using AND style (PromQL supports metric *on? simple)
    # Safer: wrap in parentheses and add label filter via bool unless query is a function with [] range.
    return f'{selector} * on(tenant_id) group_left() (vector(1) and count({{tenant_id="{tenant_id}"}}))'

def _query_range(expression: str, tenant_id: str, start_ms: int, end_ms: int, step_ms: int) -> List[SeriesData]:
    dbg(f'_query_range start expr={expression} tenant={tenant_id} window={start_ms}-{end_ms} step={step_ms}')
    if start_ms > end_ms:
        start_ms, end_ms = end_ms, start_ms
    expr = _inject_tenant(expression, tenant_id)
    params = {
        'query': expr,
        'start': str(start_ms/1000.0),
        'end': str(end_ms/1000.0),
        'step': str(max(step_ms/1000.0, 1))
    }
    try:
        data = _http_get('/api/v1/query_range', params)
    except RuntimeError:
        # vm_disabled no longer expected here; treat as empty
        return []
    if data.get('status') != 'success':
        dbg(f'_query_range error status={data.get("status")} expr={expression}')
        return []
    result = []
    for r in data.get('data', {}).get('result', []):
        metric_labels = r.get('metric', {})
        values = []
        for ts,val in r.get('values', []):
            try:
                v = float(val)
            except Exception:
                v = math.nan
            values.append(InstantSample(ts_ms=int(float(ts)*1000), value=v))
        result.append(SeriesData(expression=expression, labels=metric_labels, samples=values))
    dbg(f'_query_range done expr={expression} series={len(result)}')
    return result

@app.get('/metrics/list', response_model=MetricsListResponse)
async def api_metrics_list(tenant_id: str):
    dbg(f'api_metrics_list start tenant={tenant_id}')
    warnings: List[str] = []
    # Use series API scoped to active bundle time range if available
    ac = get_active_context(tenant_id)
    start = ac['start_ts']/1000 if ac else int(_time.time())-3600
    end = ac['end_ts']/1000 if ac else int(_time.time())
    # removed DEBUG_PTOP_DISCOVERY logging
    params = {
        'match[]': f'{{tenant_id="{tenant_id}"}}',
        'start': str(start),
        'end': str(end)
    }
    try:
        data = _http_get('/api/v1/series', params)
        if data.get('status') != 'success':
            warnings.append('vm_series_error')
            dbg(f'api_metrics_list vm_series_error status={data.get("status")}')
            return MetricsListResponse(metrics=[], warnings=warnings)
        names = sorted({s.get('__name__') for s in data.get('data', []) if s.get('__name__')})
        dbg(f'api_metrics_list done tenant={tenant_id} count={len(names)}')
        return MetricsListResponse(metrics=names, warnings=warnings)
    except Exception:
        warnings.append('vm_series_exception')
        dbg('api_metrics_list vm_series_exception')
        return MetricsListResponse(metrics=[], warnings=warnings)

@app.get('/metrics/label_values', response_model=LabelValuesResponse)
async def api_label_values(tenant_id: str, label: str, metric: Optional[str] = None):
    dbg(f'api_label_values start tenant={tenant_id} label={label} metric={metric}')
    warnings: List[str] = []
    match_expr = metric or f'{{tenant_id="{tenant_id}"}}'
    if 'tenant_id=' not in match_expr:
        match_expr = _inject_tenant(match_expr, tenant_id)
    params = {'match[]': match_expr}
    try:
        data = _http_get(f'/api/v1/label/{label}/values', params)
        if data.get('status') != 'success':
            warnings.append('vm_label_error')
            dbg(f'api_label_values vm_label_error status={data.get("status")}')
            return LabelValuesResponse(label=label, values=[], warnings=warnings)
        vals = sorted([v for v in data.get('data', []) if isinstance(v,str)])
        dbg(f'api_label_values done tenant={tenant_id} label={label} count={len(vals)}')
        return LabelValuesResponse(label=label, values=vals, warnings=warnings)
    except Exception:
        warnings.append('vm_label_exception')
        dbg('api_label_values vm_label_exception')
        return LabelValuesResponse(label=label, values=[], warnings=warnings)

@app.get('/metrics/metadata', response_model=MetricMetadataResponse)
async def api_metric_metadata(tenant_id: str, metric: str):
    dbg(f'api_metric_metadata start tenant={tenant_id} metric={metric}')
    # Check doc store for canonical doc
    d = get_metric(metric)
    doc_detail = _doc_to_detail(d) if d else None
    present = False
    warnings: List[str] = []
    # lightweight existence check via series selector limited to small window
    ac = get_active_context(tenant_id)
    start = ac['start_ts']/1000 if ac else int(_time.time())-3600
    end = ac['end_ts']/1000 if ac else int(_time.time())
    params = {'match[]': f'{metric}{{tenant_id="{tenant_id}"}}', 'start': str(start), 'end': str(end)}
    try:
        data = _http_get('/api/v1/series', params)
        if data.get('status') == 'success' and data.get('data'):
            present = True
    except Exception:
        warnings.append('vm_series_exception')
    dbg(f'api_metric_metadata done tenant={tenant_id} metric={metric} present={present}')
    return MetricMetadataResponse(metric=metric, doc=doc_detail, present=present, warnings=warnings)

@app.post('/metrics/query_range', response_model=QueryRangeResponse)
async def api_query_range(req: QueryRangeRequest):
    dbg(f'api_query_range start tenant={req.tenant_id} expr={req.query} window={req.start_ms}-{req.end_ms} step={req.step_ms}')
    ser = _query_range(req.query, req.tenant_id, req.start_ms, req.end_ms, req.step_ms)
    dbg(f'api_query_range done expr={req.query} series={len(ser)}')
    return QueryRangeResponse(expression=req.query, series=ser, warnings=[])

@app.post('/metrics/graph_timeseries', response_model=GraphTimeseriesResponse)
async def api_graph_timeseries(req: GraphTimeseriesRequest):
    dbg(f'api_graph_timeseries start tenant={req.tenant_id} metrics={len(req.metrics)} window={req.start_ms}-{req.end_ms} step={req.step_ms}')
    warnings: List[str] = []
    all_series: List[SeriesData] = []
    for expr in req.metrics:
        ser = _query_range(expr, req.tenant_id, req.start_ms, req.end_ms, req.step_ms)
        all_series.extend(ser)
    dbg(f'api_graph_timeseries done series_total={len(all_series)}')
    return GraphTimeseriesResponse(series=all_series, warnings=warnings)

@app.post('/metrics/graph_compare', response_model=GraphCompareResponse)
async def api_graph_compare(req: GraphCompareRequest):
    dbg(f'api_graph_compare start tenant={req.tenant_id} primary={req.primary} comps={len(req.comparators)} window={req.start_ms}-{req.end_ms} step={req.step_ms} normalize={req.normalize}')
    prim = _query_range(req.primary, req.tenant_id, req.start_ms, req.end_ms, req.step_ms)
    comps = []
    for c in req.comparators:
        comps.extend(_query_range(c, req.tenant_id, req.start_ms, req.end_ms, req.step_ms))
    if req.normalize and prim:
        # Build time index map for primary then adjust comparator values to share-of-total
        # For simplicity we only normalize comparator series relative to sum of all comparator + primary values at each ts.
        ts_values: Dict[int, float] = {}
        for s in prim:
            for p in s.samples:
                ts_values[p.ts_ms] = ts_values.get(p.ts_ms, 0.0) + (p.value or 0.0)
        for s in comps:
            for p in s.samples:
                total = ts_values.get(p.ts_ms, 0.0)
                if total > 0 and p.value is not None:
                    p.value = p.value / total
    dbg(f'api_graph_compare done primary_series={len(prim)} comparator_series={len(comps)}')
    return GraphCompareResponse(primary=prim, comparators=comps, warnings=[])
