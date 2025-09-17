import random, string, pytest
from mcp_server import mcp_app

# Ensure embeddings loaded
mcp_app.init_server()

def _tool(t):
    return getattr(t, 'fn', t)

def _rand_unknown_token():
    return 'zzz_' + ''.join(random.choices(string.ascii_lowercase, k=10))


def test_metric_search_known_metric():
    for candidate in ['cpu_utilization', 'cpu_utilization_percent', 'mem_total_memory']:
        doc = _tool(mcp_app.get_metric_tool)(candidate)
        if doc.get('doc'):
            metric_name = candidate
            break
    else:
        pytest.skip('no known metric docs present')
    res = _tool(mcp_app.metric_search)(metric_name, top_k=3)
    assert res['candidates'], 'expected at least one candidate'
    assert res['decision'] in ['auto', 'ambiguous', 'no_match']
    if res['decision'] == 'auto':
        assert res['auto_selected'] == res['candidates'][0]['metric_name']


def test_metric_search_unknown_token():
    token = _rand_unknown_token()
    res = _tool(mcp_app.metric_search)(token, top_k=3)
    assert res['decision'] in ('no_match', 'ambiguous')
    if res['decision'] == 'no_match':
        assert res['candidates'] == []
    else:
        assert res['auto_selected'] is None
        assert res['confidence'] < res['threshold']