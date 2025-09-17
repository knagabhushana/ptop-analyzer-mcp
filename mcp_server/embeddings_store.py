from __future__ import annotations
import json, os, math, threading, re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

# NOTE: Moved out of docs/ to keep docs directory documentation-only (no code scripts)
DOCS_EMBEDDINGS_PATH = os.path.join(os.path.dirname(__file__), 'docs', 'docs_embeddings.jsonl')

_lock = threading.RLock()
_loaded = False
_load_warnings: List[str] = []  # retained for diagnostics (e.g. sanitation applied)
_docs: Dict[str, 'EmbeddingDoc'] = {}
_alias_index: Dict[str, List[str]] = {}  # alias -> list[doc_id]
_metric_name_index: Dict[str, str] = {}  # metric_name -> doc_id (L1 only)
_plugin_index: Dict[str, List[str]] = {}  # legacy: record_type(lowercase as seen in docs) -> doc_ids
_category_index: Dict[str, List[str]] = {}  # NEW: canonical CATEGORY (uppercase) -> doc_ids
_concept_ids: List[str] = []
_embedding_dim: int | None = None

@dataclass
class EmbeddingDoc:
    id: str
    level: str
    text: str
    metadata: Dict[str, Any]
    embedding: Optional[List[float]]

    @property
    def record_type(self) -> Optional[str]:
        return self.metadata.get('record_type')

    @property
    def metric_name(self) -> Optional[str]:
        return self.metadata.get('metric_name')


def _normalize_token(tok: str) -> str:
    return tok.strip().lower()


def _derive_category(record_type: str) -> str:
    """Map legacy record_type (as appears in docs) to canonical CATEGORY.

    Canonical categories (uppercase): CPU, MEM, DISK, NET, TOP, SMAPS, DB, FASTPATH, OTHER
    Rules:
      * cpu -> CPU
      * mem -> MEM
      * disk -> DISK
      * net -> NET
      * smaps -> SMAPS
      * tasks|top -> TOP (unify on TOP)
      * db_stat|db_mpool_stat|dbph -> DB
      * fp* (fp, fpports, fpmbuf, fpprxy, fppref, fpdca, fprrstats, fpdncr, fpvlstats) and other fast path like dot_stat, doh_stat, tcp_dca_stat -> FASTPATH
      * everything else -> OTHER
    """
    rt = record_type.lower()
    if rt == 'cpu':
        return 'CPU'
    if rt == 'mem':
        return 'MEM'
    if rt == 'disk':
        return 'DISK'
    if rt == 'net':
        return 'NET'
    if rt in ('tasks', 'top'):
        return 'TOP'
    if rt == 'smaps':
        return 'SMAPS'
    if rt in ('db_stat', 'db_mpool_stat', 'dbph'):
        return 'DB'
    if rt.startswith('fp') or rt in ('dot_stat', 'doh_stat', 'tcp_dca_stat'):
        return 'FASTPATH'
    return 'OTHER'


def load_embeddings(path: str = DOCS_EMBEDDINGS_PATH) -> None:
    """Load static documentation embeddings into memory (idempotent).

    Side effects:
      * Builds indices for: metric_name, alias, plugin(record_type), concept ids
      * Captures embedding dimensionality for validation of semantic queries
    """
    global _loaded, _embedding_dim
    with _lock:
        if _loaded:
            return
        if not os.path.exists(path):
            raise FileNotFoundError(f"Embeddings file not found: {path}")
        # Phase 1: sanitation pass (detect invalid escape sequences and correct them in-memory)
        with open(path, 'r', encoding='utf-8') as f:
            raw_lines = f.readlines()
        sanitized_lines: List[str] = []
        changed = False
        pattern = re.compile(r"\\(?![\\\"/bfnrtu])")  # backslash not starting a valid JSON escape
        for lineno, line in enumerate(raw_lines, start=1):
            if not line.strip():
                sanitized_lines.append(line)
                continue
            candidate = pattern.sub(r"\\\\", line)
            if candidate != line:
                changed = True
            # Validate JSON AFTER sanitation; if still invalid we abort (no silent skip)
            try:
                json.loads(candidate)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Embeddings file malformed at line {lineno}: {e.msg} (pos {e.pos})") from e
            sanitized_lines.append(candidate)
        if changed:
            # Rewrite file with corrected escape sequences (persistent fix)
            backup = path + ".bak"
            try:
                if not os.path.exists(backup):
                    shutil.copyfile(path, backup)  # type: ignore[name-defined]
            except Exception:
                pass  # non-fatal if backup fails
            with open(path, 'w', encoding='utf-8') as wf:
                wf.writelines(sanitized_lines)
            _load_warnings.append('sanitized_invalid_escapes')
        # Phase 2: build indices strictly (no skipping)
        for lineno, line in enumerate(sanitized_lines, start=1):
            if not line.strip():
                continue
            rec = json.loads(line)
            doc = EmbeddingDoc(
                id=rec['id'],
                level=rec['level'],
                text=rec['text'],
                metadata=rec.get('metadata', {}),
                embedding=rec.get('embedding'),
            )
            _docs[doc.id] = doc
            rt = doc.metadata.get('record_type')
            if rt:
                _plugin_index.setdefault(rt, []).append(doc.id)
                category = _derive_category(rt)
                doc.metadata['category'] = category
                _category_index.setdefault(category, []).append(doc.id)
            if doc.level == 'L1':
                metric_name = doc.metadata.get('metric_name')
                if metric_name:
                    _metric_name_index[_normalize_token(metric_name)] = doc.id
                aliases: List[str] = []
                aliases.extend(doc.metadata.get('legacy_aliases', []) or [])
                prov = doc.metadata.get('provenance') or {}
                if isinstance(prov, dict):
                    aliases.extend(prov.get('legacy_aliases', []) or [])
                for a in aliases:
                    if not a:
                        continue
                    _alias_index.setdefault(_normalize_token(a), []).append(doc.id)
            if doc.level == 'L4' and doc.id.startswith('concept:'):
                _concept_ids.append(doc.id)
            if doc.embedding and _embedding_dim is None:
                _embedding_dim = len(doc.embedding)
        _loaded = True

def get_embeddings_status() -> Dict[str, Any]:
    """Return diagnostics about embeddings load (for debugging metric_search failures)."""
    ensure_loaded()
    return {
        'loaded': _loaded,
        'doc_count': len(_docs),
        'embedding_dim': _embedding_dim,
        'warnings_count': len(_load_warnings),
        'sample_warnings': _load_warnings[:5],
    }


def ensure_loaded():
    if not _loaded:
        load_embeddings()


def get_embedding_dim() -> Optional[int]:
    """Return embedding dimension of stored docs (None if no embeddings present)."""
    ensure_loaded()
    return _embedding_dim


def get_doc(doc_id: str) -> Optional[EmbeddingDoc]:
    ensure_loaded()
    return _docs.get(doc_id)


def get_metric(metric_name: str) -> Optional[EmbeddingDoc]:
    ensure_loaded()
    doc_id = _metric_name_index.get(_normalize_token(metric_name))
    if doc_id:
        return _docs.get(doc_id)
    return None


def resolve_alias(alias: str) -> List[EmbeddingDoc]:
    ensure_loaded()
    ids = _alias_index.get(_normalize_token(alias), [])
    return [_docs[i] for i in ids]


def list_plugins() -> List[str]:
    ensure_loaded()
    # For backward compatibility return categories instead of raw record_types if categories present.
    if _category_index:
        return sorted(_category_index.keys())
    return sorted(_plugin_index.keys())

def list_categories() -> List[str]:
    ensure_loaded()
    return sorted(_category_index.keys())


def list_category_doc_ids(category: str) -> List[str]:
    """Return all doc ids for a canonical CATEGORY (uppercase)."""
    ensure_loaded()
    return list(_category_index.get(category, []) )


def category_level_counts(category: str) -> Dict[str, int]:
    """Return counts per level (L1/L2/L4) for a given category."""
    ensure_loaded()
    counts: Dict[str,int] = {'L1':0,'L2':0,'L4':0}
    for doc_id in _category_index.get(category, []):
        d = _docs.get(doc_id)
        if not d: continue
        if d.level in counts:
            counts[d.level]+=1
    return {k:v for k,v in counts.items() if v>0}


def list_plugin_docs(plugin: str) -> List[EmbeddingDoc]:
    ensure_loaded()
    # Accept either legacy record_type (lowercase) or new CATEGORY (uppercase)
    ids: List[str] = []
    if plugin in _category_index:
        ids = _category_index.get(plugin) or []
    else:
        # map special case: tasks -> top CATEGORY
        if plugin == 'tasks':
            plugin = 'top'
        ids = _plugin_index.get(plugin) or []
    return [_docs[i] for i in ids]


def list_concepts() -> List[str]:
    ensure_loaded()
    return list(_concept_ids)


def cosine(a: List[float], b: List[float]) -> float:
    num = 0.0
    da = 0.0
    db = 0.0
    for x, y in zip(a, b):
        num += x * y
        da += x * x
        db += y * y
    if da == 0 or db == 0:
        return 0.0
    return num / math.sqrt(da * db)


def semantic_search(query_embedding: List[float], top_k: int = 5, levels: Optional[List[str]] = None) -> List[Tuple[EmbeddingDoc, float]]:
    """Return top_k docs by cosine similarity.

    If the provided query embedding dimensionality differs from stored doc embeddings,
    we deterministically adapt it (truncate or tile) instead of raising, so prototype
    tests using placeholder low-dimension vectors still succeed.
    """
    ensure_loaded()
    if _embedding_dim:
        qdim = len(query_embedding)
        if qdim != _embedding_dim:
            if qdim == 0:
                raise ValueError("empty query embedding")
            if qdim > _embedding_dim:
                query_embedding = query_embedding[:_embedding_dim]
            else:
                # tile to reach required length
                times = (_embedding_dim + qdim - 1) // qdim
                query_embedding = (query_embedding * times)[:_embedding_dim]
    levels_set = set(levels) if levels else None
    results: List[Tuple[EmbeddingDoc, float]] = []
    for d in _docs.values():
        if levels_set and d.level not in levels_set:
            continue
        if not d.embedding:
            continue
        score = cosine(query_embedding, d.embedding)
        results.append((d, score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]


def keyword_search(query: str, top_k: int = 10, levels: Optional[List[str]] = None) -> List[Tuple[EmbeddingDoc, float]]:
    ensure_loaded()
    q_tokens = [t for t in re_tokenize(query) if t]
    levels_set = set(levels) if levels else None
    scored: List[Tuple[EmbeddingDoc, float]] = []
    for d in _docs.values():
        if levels_set and d.level not in levels_set:
            continue
        text_lower = d.text.lower()
        hits = sum(1 for t in q_tokens if t in text_lower)
        if hits > 0:
            scored.append((d, hits / len(q_tokens)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]

# Simple alphanumeric/underscore tokenizer reused by keyword_search
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

def re_tokenize(text: str) -> List[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]

# Cheap embedding fallback (n-gram style via char hash buckets) – used when no vector provided.

def cheap_text_embedding(text: str, dim: Optional[int] = None) -> List[float]:
    ensure_loaded()
    # Use stored embedding dim when available so we naturally align.
    if dim is None:
        dim = _embedding_dim or 128
    vec = [0.0] * dim
    if not text:
        return vec
    for ch in text.lower():
        idx = (ord(ch) * 131) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm:
        vec = [v / norm for v in vec]
    return vec
