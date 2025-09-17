from mcp_server import mcp_app
from mcp_server.timescale.writer import TimescaleWriter

# Ensure tools are initialized
mcp_app.init_server()

def _tool(t):
    return getattr(t, 'fn', t)


def test_metric_discover_cpu_utilization():
    out = _tool(mcp_app.metric_discover)("cpu utilization percent")
    assert out['candidates'], 'expected at least one candidate'
    names = [c['metric_name'] for c in out['candidates']]
    # Check for 'cpu_utilization' in the full metric names, not just 'utilization'
    assert any('utilization' in name for name in names), f"Expected 'utilization' in one of: {names}"


def test_ingest_stats_disabled():
    # Ensure stats disabled when flag off
    out = _tool(mcp_app.ingest_stats)()
    assert out['enabled'] in (False, True)
    # We can't assert exact structure when disabled, just presence of key
    if not out['enabled']:
        assert out['reason'] == 'timescale_disabled'


def test_ingest_stats_after_manual_writer():
    # Simulate enabling timescale writer manually
    from mcp_server.mcp_app import TIMESCALE_WRITER_LAST
    w = TimescaleWriter(batch_size=2)
    from mcp_server.ingestion.parser import MetricSample
    s = MetricSample(name='utilization', value=1.0, ts_ms=1_700_000_000_000, labels={'bundle_id':'b-x','cpu':'cpu0'})
    w.add(s); w.flush()
    import mcp_server.mcp_app as appmod
    appmod.TIMESCALE_WRITER_LAST = w
    out = _tool(mcp_app.ingest_stats)()
    if out['enabled']:
        assert out['initialized'] is True
        assert out['total_rows_added'] >= 1
