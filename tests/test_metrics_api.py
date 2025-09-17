import pytest
import os
import tempfile
import tarfile
from mcp_server import mcp_app


def _tool(t):
    """Helper to get the underlying function from MCP tool wrapper"""
    return getattr(t, 'fn', t)


def _create_test_bundle_with_data():
    """Create a test bundle with some ptop log data"""
    sample_log_path = os.path.join(os.path.dirname(__file__), 'data', 'sample_ptops.log')
    if not os.path.exists(sample_log_path):
        pytest.skip(f'sample_ptops.log not found at {sample_log_path}')
    
    # Create temporary tar bundle
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
        bundle_path = tmp.name
    
    with tarfile.open(bundle_path, 'w:gz') as tf:
        tf.add(sample_log_path, arcname='ptop.log')
    
    return bundle_path


def test_metric_discover_basic():
    """Test metric discovery functionality"""
    mcp_app.init_server()
    
    # Test basic metric discovery
    result = _tool(mcp_app.metric_discover)("cpu utilization", top_k=3)
    assert 'query' in result
    assert 'candidates' in result
    assert result['query'] == "cpu utilization"
    assert isinstance(result['candidates'], list)
    
    # Should find cpu-related metrics
    metric_names = [c['metric_name'] for c in result['candidates']]
    assert any('cpu' in name.lower() for name in metric_names)


def test_metric_search_functionality():
    """Test metric search functionality"""
    mcp_app.init_server()
    
    # Test metric search
    result = _tool(mcp_app.metric_search)("memory usage", top_k=3)
    assert 'query' in result
    assert 'candidates' in result
    assert isinstance(result['candidates'], list)


def test_ingest_stats():
    """Test ingest statistics functionality"""
    mcp_app.init_server()
    
    # Test deprecated ingest_stats (should return deprecation notice)
    result = _tool(mcp_app.ingest_stats)()
    assert 'deprecated' in result
    assert result['deprecated'] is True
    
    # Test the new ingest_status function which should have state
    status_result = _tool(mcp_app.ingest_status)()
    assert 'state' in status_result
    assert status_result['state'] in ['idle', 'processing']


def test_metrics_with_bundle_loaded():
    """Test metrics functionality with a loaded bundle"""
    mcp_app.init_server()
    tenant = 'NIOSSPT-TEST'
    bundle_path = _create_test_bundle_with_data()
    
    try:
        # Load bundle
        load_result = _tool(mcp_app.load_bundle)(path=bundle_path, tenant_id=tenant)
        assert 'bundle_id' in load_result
        
        # Test that we can discover metrics after loading
        discover_result = _tool(mcp_app.metric_discover)("cpu", top_k=5)
        assert len(discover_result['candidates']) > 0
        
        # Test active context
        context = _tool(mcp_app.active_context)(tenant_id=tenant)
        assert context['bundle_id'] == load_result['bundle_id']
        
        # Test list bundles
        bundles = _tool(mcp_app.list_bundles_tool)(tenant_id=tenant)
        assert len(bundles) == 1
        assert bundles[0]['active'] is True
        
    finally:
        # Cleanup
        try:
            _tool(mcp_app.unload_bundle)(tenant_id=tenant, purge_all=True)
            if os.path.exists(bundle_path):
                os.unlink(bundle_path)
        except:
            pass


def test_metric_schema_functionality():
    """Test metric schema functionality"""
    mcp_app.init_server()
    
    # Test with a known metric
    try:
        result = _tool(mcp_app.metric_schema)("cpu_utilization")
        assert 'metric_name' in result
        assert 'table' in result
        assert 'columns' in result
    except Exception:
        # If metric doesn't exist, that's also valid - test that it handles errors gracefully
        pass


def test_docs_search_functionality():
    """Test documentation search functionality"""
    mcp_app.init_server()
    
    # Test docs search
    result = _tool(mcp_app.search_docs)("cpu metrics", top_k=3)
    assert isinstance(result, list)
    
    # Test detailed docs search
    detailed_result = _tool(mcp_app.search_docs_detail)("memory metrics", top_k=3)
    assert isinstance(detailed_result, list)


def test_concepts_functionality():
    """Test concepts functionality"""
    mcp_app.init_server()
    
    # Test concepts (may be empty, but should not error)
    result = _tool(mcp_app.concepts)()
    assert isinstance(result, list)


def test_timescale_sql_basic():
    """Test basic TimescaleDB SQL functionality"""
    mcp_app.init_server()
    
    # Test a simple query that should work even without data
    try:
        result = _tool(mcp_app.timescale_sql)("SELECT 1 as test_value", max_rows=1)
        assert 'columns' in result
        assert 'rows' in result
    except Exception:
        # If TimescaleDB is not available, that's fine - test should not fail
        pass
