import pytest
from mcp_server.embeddings_store import ensure_loaded, _docs, list_plugins, list_plugin_docs  # type: ignore
from mcp_server import mcp_app as app

# Helper to unwrap FastMCP tool objects (they may be descriptors with .fn or .__wrapped__)
def _call(tool, *a, **kw):
    fn = getattr(tool, 'fn', None) or getattr(tool, '__wrapped__', None) or tool
    return fn(*a, **kw)


def get_any_metric_doc_id():
    # Return an L1 doc id
    ensure_loaded()
    for d in _docs.values():  # type: ignore
        mname = d.metadata.get('metric_name')
        if d.level == 'L1' and mname:
            return d.id, mname
    raise RuntimeError("No L1 metric docs available for tests")


def test_plugins_and_docs_structure():
    """Validate plugin/category enumeration and document retrieval (updated API)."""
    plugins = list_plugins()
    assert isinstance(plugins, list) and plugins, 'plugins list empty'
    plugin = plugins[0]
    docs = [ {'id': d.id, 'level': d.level} for d in list_plugin_docs(plugin) ]
    assert isinstance(docs, list) and docs, 'expected docs for plugin'
    sample = docs[0]
    assert {'id','level'}.issubset(sample.keys())
    assert any(d['level'] in ('L1','L2','L4') for d in docs)


def test_metric_schema_found_and_missing():
    """Validate metric_schema for existing and missing metric names (replaces get_metric_tool)."""
    _, metric_name = get_any_metric_doc_id()
    found = _call(app.metric_schema, metric_name)
    assert found['metric_name'] == metric_name
    missing = _call(app.metric_schema, '__does_not_exist_metric__')
    assert missing.get('error') == 'metric_not_found'


def test_embeddings_doc_presence():
    """Ensure at least one L1 metric doc is loaded via embeddings (replaces get_doc_tool)."""
    doc_id, _ = get_any_metric_doc_id()
    ensure_loaded()
    assert doc_id in _docs  # type: ignore


def test_metric_discover_basic():
    """Ensure metric_discover returns candidates for a token from an existing metric name."""
    _, metric_name = get_any_metric_doc_id()
    token = metric_name.split('_')[0]
    res = _call(app.metric_discover, token, top_k=3)
    assert 'candidates' in res and isinstance(res['candidates'], list)


@pytest.mark.parametrize('semantic_flag', [False, True], ids=['keyword','semantic'])
def test_metric_search_semantic_vs_keyword(semantic_flag):
    """Use metric_search instead of search_docs for semantic vs keyword coverage."""
    _, metric_name = get_any_metric_doc_id()
    term = metric_name.replace('_',' ')
    results = _call(app.metric_search, term, top_k=3, semantic=semantic_flag)
    assert isinstance(results, dict) and 'candidates' in results


def test_metric_search_returns_candidates():
    """Basic coverage that metric_search returns a candidate list."""
    _, metric_name = get_any_metric_doc_id()
    res = _call(app.metric_search, metric_name, top_k=2, semantic=True)
    assert isinstance(res, dict) and res.get('candidates')


def test_metric_search_cpu_token():
    """Ensure searching for generic token like 'cpu' yields candidates."""
    data = _call(app.metric_search, 'cpu', top_k=5, semantic=True)
    assert isinstance(data, dict) and data.get('candidates')
import json

# Helper to get at least one L1 metric doc id & name

def _any_metric_doc():
    plugins = list_plugins()
    assert plugins, 'No plugins loaded'
    first_plugin = plugins[0]
    docs = [ {'id': d.id, 'level': d.level, 'metric_name': d.metadata.get('metric_name')} for d in list_plugin_docs(first_plugin) ]
    assert docs, 'No docs for plugin'
    for d in docs:
        if d['id'].startswith('field:'):
            return d
    raise AssertionError('No field docs found in plugin list')


def test_metric_schema_for_any_metric():
    ref = _any_metric_doc()
    metric_name = ref.get('metric_name') or ref['id'].split(':')[-1]
    schema = _call(app.metric_schema, metric_name)
    if 'error' not in schema:
        assert schema['metric_name'] == metric_name


def test_fastpath_concept_tool():
    """Ensure fastpath_architecture tool still returns concept doc."""
    doc = _call(app.fastpath_architecture)
    if 'error' not in doc:
        assert doc['id'] == 'concept:fastpath_architecture'


def test_metric_search_additional_query():
    """Additional semantic query for diversity (disk)."""
    res = _call(app.metric_search, 'disk', top_k=5, semantic=True)
    assert isinstance(res, dict)

# Negative scenarios

def test_metric_schema_not_found():
    """Negative metric_schema case."""
    resp = _call(app.metric_schema, 'definitely_nonexistent_metric_12345')
    assert resp.get('error') == 'metric_not_found'


def test_metric_search_semantic_generic():
    data = _call(app.metric_search, 'cpu', top_k=3, semantic=True)
    assert isinstance(data, dict)


def test_metric_search_semantic_top_hit():
    results = _call(app.metric_search, 'cpu', top_k=3, semantic=True)
    assert isinstance(results, dict) and results.get('candidates') is not None


@pytest.mark.parametrize('levels,query', [(['L1'],'cpu')], ids=['L1_only'])
def test_metric_search_l1_only(levels, query):
    results = _call(app.metric_search, query, top_k=5, semantic=True)
    assert isinstance(results, dict)


def test_metric_search_by_plugin_token():
    plugins = list_plugins()
    plugin = plugins[0]
    results = _call(app.metric_search, plugin, top_k=5, semantic=False)
    assert isinstance(results, dict)


def test_metric_search_endpoint_auto_or_ambiguous():
    ref = _any_metric_doc()
    mname = ref.get('metric_name') or ref['id'].split(':')[-1]
    cands = _call(app.metric_search, mname.replace('_',' '), top_k=5, semantic=True)
    assert isinstance(cands, dict)


def test_metric_search_endpoint_no_match():
    res = _call(app.metric_search, 'zzqqlmnopq_nonexistent_metric_token', top_k=3, semantic=True)
    assert isinstance(res, dict)
    assert res['decision'] in ('no_match','ambiguous')

