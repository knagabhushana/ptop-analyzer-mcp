#!/usr/bin/env python3
"""
Build static documentation embeddings artifact per Section 16 of ptops_integration_design.md.
Generates JSONL at mcp_server/docs/docs_embeddings.jsonl and report JSON at mcp_server/docs/docs_embeddings_report.json.
Phase 1 scope (minimal viable):
 - Parse metrics plugin doc tables (L1)
 - Build plugin summaries (L2)
 - Stub alias clusters & concept docs (L3/L4) with TODO markers (will be enriched later)
 - Use sentence-transformers 'all-MiniLM-L6-v2' if available else fallback to dummy embedding (zeros)
 - Deterministic provenance hash (sha256 of canonical JSON subset)

Deferred in this first implementation:
 - Fast path planned metrics (we tag planned=true if not present in metrics doc)
 - Full alias cluster population from design doc sections (only placeholder)
 - Concept doc extraction (add selected headings later)
"""
from __future__ import annotations
import re, os, json, hashlib, datetime, argparse, sys
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
DOCS_DIR = os.path.join(REPO_ROOT, 'mcp_server', 'docs')
METRICS_DOC = os.path.join(DOCS_DIR, 'ptop_plugin_metrics_doc.md')
OUTPUT_JSONL = os.path.join(DOCS_DIR, 'docs_embeddings.jsonl')
REPORT_JSON = os.path.join(DOCS_DIR, 'docs_embeddings_report.json')
EMBED_MODEL_NAME = 'sentence-transformers/all-MiniLM-L6-v2'

try:
    from sentence_transformers import SentenceTransformer  # type: ignore
    _model = None  # lazy-loaded model instance
except Exception:  # pragma: no cover
    SentenceTransformer = None  # type: ignore
    _model = None

@dataclass
class EmbeddingDoc:
    id: str
    level: str
    text: str
    metadata: Dict[str, Any]
    embedding: Optional[List[float]] = None

    def provenance_hash(self) -> str:
        payload = {
            'id': self.id,
            'level': self.level,
            'text': self.text,
            'metadata': {k: self.metadata.get(k) for k in sorted(self.metadata.keys()) if k not in {'provenance_hash','embedding'}}
        }
        raw = json.dumps(payload, sort_keys=True, separators=(',',':')).encode()
        return 'sha256:' + hashlib.sha256(raw).hexdigest()

_TABLE_ROW_RE = re.compile(r'^\|([^\n]*?)\|$')
_SPLIT_PIPE_RE = re.compile(r'\s*\|\s*')

PLUGIN_HEADER_RE = re.compile(r'^##\s+PLUGIN\s+([a-zA-Z0-9_]+)\b')

@dataclass
class FieldRow:
    record_type: str
    raw_name: str
    normalized_metric_name: str
    units: str
    metric_kind: str
    origin: str
    semantics: str
    computation: str
    notes: str


def normalize_metric_name(record_type: str, raw: str) -> str:
    # Basic normalization: lowercase, spaces/dashes -> underscores
    n = raw.strip().lower().replace('%','percent')
    n = re.sub(r'[^a-z0-9_]+','_', n)
    if not n.startswith(record_type.lower() + '_'):
        n = f"{record_type.lower()}_{n}"
    return n


def parse_provenance_line(line: str) -> Dict[str, List[str]]:
    """Parse a provenance line of the form 'Provenance: `path` (`details`), `other` ...'
    Heuristics:
      - Backtick-delimited tokens collected first.
      - Tokens containing '/' treated as source_files.
      - Tokens starting with 'ptop' or containing '(' function-like treated as source_functions (sans arg list).
      - Remaining alphanumeric single words treated as external_commands.
    Returns dict with lists (may be empty)."""
    raw = line.split(':',1)[1].strip() if ':' in line else line.strip()
    backtick_tokens = re.findall(r'`([^`]+)`', raw)
    source_files: List[str] = []
    source_functions: List[str] = []
    external_commands: List[str] = []
    for tok in backtick_tokens:
        cleaned = tok.strip()
        if '/' in cleaned:
            source_files.append(cleaned)
        elif cleaned.startswith('ptop') or '(' in cleaned or cleaned.endswith(')'):
            source_functions.append(re.sub(r'\(.*?\)$','', cleaned))
        elif cleaned:
            external_commands.append(cleaned)
    return {
        'plugin_provenance_raw': raw,
        'source_files': sorted(set(source_files)),
        'source_functions': sorted(set(source_functions)),
        'external_commands': sorted(set(external_commands))
    }

def parse_metrics_doc(path: str) -> Tuple[Dict[str, List[FieldRow]], Dict[str, Dict[str, Any]]]:
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    current_plugin: Optional[str] = None
    in_table = False
    header_columns: list[str] = []
    in_fields_section = False
    plugins: Dict[str, List[FieldRow]] = {}
    plugin_prov: Dict[str, Dict[str, Any]] = {}
    in_description = False

    for line in lines:
        m = PLUGIN_HEADER_RE.match(line)
        if m:
            current_plugin = m.group(1)
            if current_plugin is not None:
                plugins.setdefault(current_plugin, [])
                plugin_prov.setdefault(current_plugin, {'plugin_provenance_raw':'', 'source_files':[], 'source_functions':[], 'external_commands':[]})
            in_table = False
            in_fields_section = False
            in_description = False
            continue
        if current_plugin is None:
            continue
        # Track description block until next blank line after first non-blank line post header
        if line.strip().startswith('### Description'):
            in_description = True
            continue
        if in_description:
            if not line.strip():
                in_description = False
            # Capture provenance lines inside description
            if line.strip().startswith('Provenance:'):
                plugin_prov[current_plugin] = parse_provenance_line(line.strip())
            # continue scanning description regardless
        
        # Detect Fields section
        if line.strip().lower().startswith('### fields'):
            in_fields_section = True
            header_columns = []
            in_table = False
            continue
        if in_fields_section and not line.strip():
            # blank line ends fields section
            in_fields_section = False
            continue
        if in_fields_section and line.startswith('PREFIX|'):
            # header row for plain pipe list (not markdown table)
            header_columns = [c.strip().lower() for c in line.strip().split('|')]
            continue
        if in_fields_section and header_columns and '|' in line and not line.lstrip().startswith('#'):
            cols = [c.strip() for c in line.strip().split('|')]
            if len(cols) == len(header_columns):
                colmap = dict(zip(header_columns, cols))
                raw_field = colmap.get('name') or colmap.get('field') or ''
                token = colmap.get('token') or ''
                if not raw_field and token:
                    raw_field = token
                if raw_field:
                    kind = (colmap.get('type') or '').lower()
                    units = colmap.get('units','')
                    origin = colmap.get('origin','')
                    semantics = colmap.get('semantics','')
                    computation = colmap.get('computation','')
                    notes = colmap.get('notes','')
                    norm = normalize_metric_name(current_plugin, raw_field)
                    row = FieldRow(current_plugin, raw_field, norm, units, kind, origin, semantics, computation, notes)
                    plugins[current_plugin].append(row)
            else:
                # end of fields list
                in_fields_section = False
            continue
        if line.strip().startswith('|'):
            # Potential table row
            if line.strip().startswith('| Field'):
                in_table = True
                header_columns = [c.strip().lower() for c in line.strip().strip('|').split('|')]
                continue
            if in_table:
                if line.strip().startswith('|---'):
                    continue
                cols = [c.strip() for c in line.strip().strip('|').split('|')]
                if len(cols) != len(header_columns):
                    # table ended
                    in_table = False
                    continue
                colmap = dict(zip(header_columns, cols))
                raw_field = colmap.get('field') or colmap.get('name') or ''
                if not raw_field:
                    continue
                kind = (colmap.get('metric_kind') or colmap.get('kind') or '').lower()
                units = colmap.get('units','')
                origin = colmap.get('origin','')
                semantics = colmap.get('semantics','')
                computation = colmap.get('computation','')
                notes = colmap.get('notes','')
                norm = normalize_metric_name(current_plugin, raw_field)
                row = FieldRow(current_plugin, raw_field, norm, units, kind, origin, semantics, computation, notes)
                plugins[current_plugin].append(row)
    return plugins, plugin_prov


def build_l1_docs(plugins: Dict[str, List[FieldRow]], plugin_prov: Dict[str, Dict[str, Any]], version: str) -> List[EmbeddingDoc]:
    docs: List[EmbeddingDoc] = []
    for plugin, rows in plugins.items():
        for r in rows:
            text_parts = [f"Metric {r.normalized_metric_name} ({plugin})", r.semantics]
            if r.computation:
                text_parts.append(f"Computation: {r.computation}")
            if r.origin:
                text_parts.append(f"Origin: {r.origin}")
            if r.notes:
                text_parts.append(f"Notes: {r.notes}")
            text_parts.append(f"Units: {r.units or 'n/a'} Kind: {r.metric_kind}")
            text = '\n'.join(p for p in text_parts if p)
            plugin_prov_meta = plugin_prov.get(plugin, {})
            meta = {
                'record_type': plugin,
                'metric_name': r.normalized_metric_name,
                'metric_kind': r.metric_kind,
                'metric_category': infer_metric_category(plugin),
                'version': version,
                'legacy_aliases': [],
                'field_origin': r.origin,
                'provenance': {
                    'field_origin': r.origin,
                    **plugin_prov_meta
                }
            }
            docs.append(EmbeddingDoc(id=f"field:{plugin}:{r.normalized_metric_name}", level='L1', text=text, metadata=meta))
    return docs


def infer_metric_category(record_type: str) -> str:
    mapping = {
        'CPU':'cpu', 'MEM':'memory', 'DISK':'disk', 'NET':'network', 'TOP':'process', 'SMAPS':'memory_map',
        'DBWR':'db_histogram', 'DBWA':'db_histogram', 'DBRD':'db_histogram', 'DBMPOOL':'db'
    }
    return mapping.get(record_type.upper(), 'other')


def build_l2_plugin_docs(plugins: Dict[str, List[FieldRow]], plugin_prov: Dict[str, Dict[str, Any]], version: str) -> List[EmbeddingDoc]:
    docs: List[EmbeddingDoc] = []
    for plugin, rows in plugins.items():
        summary_lines = [f"Plugin {plugin} summary", f"Metric count: {len(rows)}"]
        kinds = sorted({r.metric_kind for r in rows})
        summary_lines.append('Kinds: ' + ', '.join(kinds))
        text = '\n'.join(summary_lines)
        meta = {
            'record_type': plugin,
            'metric_name': None,
            'metric_kind': 'plugin_summary',
            'metric_category': infer_metric_category(plugin),
            'version': version,
            'provenance': plugin_prov.get(plugin, {})
        }
        docs.append(EmbeddingDoc(id=f"plugin:{plugin}", level='L2', text=text, metadata=meta))
    return docs


ARCH_HEADING_RE = re.compile(r'^## Product Architecture & Metric Domains')

def extract_architecture_concept(version: str, metrics_doc_path: str) -> List[EmbeddingDoc]:
    try:
        with open(metrics_doc_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    capturing = False
    buf: List[str] = []
    for line in lines:
        if ARCH_HEADING_RE.match(line):
            capturing = True
            buf.append(line.strip())
            continue
        if capturing:
            if line.startswith('---') and buf:  # stop at next horizontal rule after section
                break
            # Stop if another H2 plugin header begins
            if line.startswith('## PLUGIN '):
                break
            buf.append(line.rstrip())
    if not buf:
        return []
    text = '\n'.join(buf)
    meta = {'version': version, 'concept': 'fast_path_architecture'}
    return [EmbeddingDoc(id='concept:fast_path_architecture', level='L4', text=text, metadata=meta)]


def load_model():
    global _model
    if _model is not None:
        return _model
    if SentenceTransformer is None:
        return None
    try:
        _model = SentenceTransformer(EMBED_MODEL_NAME)
    except Exception:
        _model = None
    return _model


def embed_texts(texts: List[str]) -> List[List[float]]:
    model = load_model()
    if model is None:
        # Fallback deterministic zero vectors sized 384 (MiniLM dim)
        return [[0.0]*384 for _ in texts]
    vecs = model.encode(texts, normalize_embeddings=True)  # type: ignore
    return [v.tolist() for v in vecs]


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-embed', action='store_true', help='Skip embedding model (store empty vectors)')
    ap.add_argument('--output', default=OUTPUT_JSONL)
    ap.add_argument('--report', default=REPORT_JSON)
    args = ap.parse_args(argv)

    version = datetime.date.today().isoformat()
    if not os.path.exists(METRICS_DOC):
        print(f"Metrics doc not found at {METRICS_DOC}", file=sys.stderr)
        return 1

    plugins, plugin_prov = parse_metrics_doc(METRICS_DOC)
    l1 = build_l1_docs(plugins, plugin_prov, version)
    l2 = build_l2_plugin_docs(plugins, plugin_prov, version)
    l4_arch = extract_architecture_concept(version, METRICS_DOC)
    all_docs = l1 + l2 + l4_arch

    # Compute embeddings
    if not args.no_embed:
        embeddings = embed_texts([d.text for d in all_docs])
    else:
        embeddings = [[0.0]*8 for _ in all_docs]  # tiny placeholder dim

    for d, vec in zip(all_docs, embeddings):
        d.embedding = vec
        d.metadata['provenance_hash'] = d.provenance_hash()
        d.metadata['level'] = d.level

    # Write JSONL
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as out:
        for d in all_docs:
            out.write(json.dumps({
                'id': d.id,
                'level': d.level,
                'text': d.text,
                'metadata': d.metadata,
                'embedding': d.embedding
            }) + '\n')

    report = {
        'version': version,
        'counts': {
            'L1': len(l1),
            'L2': len(l2),
            'L3': 0,
            'L4': len(l4_arch)
        },
        'missing_provenance': [],
        'notes': [
            'L3 alias clusters pending.',
            'Architecture concept extracted from metrics doc.'
        ]
    }
    with open(args.report, 'w', encoding='utf-8') as rf:
        json.dump(report, rf, indent=2)

    print(f"Wrote {len(all_docs)} docs: {args.output}")
    print(f"Report: {args.report}")
    return 0

if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
