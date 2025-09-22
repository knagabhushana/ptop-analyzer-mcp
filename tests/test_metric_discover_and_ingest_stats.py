from mcp_server import mcp_app

mcp_app.init_server()

def _tool(t):
    return getattr(t, 'fn', t)


def test_metric_discover_cpu_utilization():
    out = _tool(mcp_app.metric_discover)("cpu utilization percent")
    assert out['candidates'], 'expected at least one candidate'
    names = [c['metric_name'] for c in out['candidates']]
    assert any('utilization' in name for name in names), f"Expected 'utilization' in one of: {names}"


def test_ingest_status_initial():
    st = _tool(mcp_app.ingest_status)()
    assert 'state' in st and 'stats' in st
    assert st['stats']['enabled'] is True


def test_ingest_status_after_init_server():
    mcp_app.init_server()
    st = _tool(mcp_app.ingest_status)()
    assert 'stats' in st
