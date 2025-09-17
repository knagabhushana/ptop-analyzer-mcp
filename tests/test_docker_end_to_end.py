import os, time, json, pathlib, pytest, tempfile, tarfile
from datetime import datetime

# In-process MCP integration test using test data
TENANT = 'NIOSSPT-TEST'

def _make_test_bundle():
    """Create a temporary test bundle for integration testing"""
    work = tempfile.mkdtemp(prefix="integration_test_")
    log_dir = os.path.join(work, 'var', 'log')
    os.makedirs(log_dir, exist_ok=True)
    
    # Create a simple test log with some basic metrics
    test_log_content = """TIME 1700000000 0 0 0
CPU 1700000000 0 50.5 10.2 5.1 30.0 4.2 0.0 0.0 0.0
MEM 1700000000 0 8192 4096 2048 1024
"""
    
    with open(os.path.join(log_dir, 'ptop-test.log'), 'w') as f:
        f.write(test_log_content)
    
    # Create tar.gz
    ts = time.strftime('%Y%m%d_%H%M')
    fd, tar_path = tempfile.mkstemp(prefix=f'sb_{TENANT}_{ts}_', suffix='.tar.gz')
    os.close(fd)
    with tarfile.open(tar_path, 'w:gz') as tf:
        tf.add(log_dir, arcname='var/log')
    
    import shutil
    shutil.rmtree(work)
    return tar_path

def test_end_to_end_mcp():
    """Integration test using test bundle data instead of external directories"""
    debug('mode: in-process MCP tools with test data')
    
    try:
        from mcp_server import mcp_app
    except Exception as e:
        pytest.skip(f'mcp_app import failed: {e}')

    def _tool(t):
        return getattr(t, 'fn', t)

    # Create test bundle
    test_bundle_path = _make_test_bundle()
    
    try:
        # Initialize embeddings / VM check (non-fatal if VM disabled)
        init_status = mcp_app.init_server()
        debug(f'init_server status={init_status}')

        # Load test bundle
        load_resp = _tool(mcp_app.load_bundle)(path=test_bundle_path, tenant_id=TENANT)
        debug(f'load_bundle -> {load_resp}')
        
        # List bundles
        bundles = _tool(mcp_app.list_bundles_tool)(TENANT)
        debug(f'list_bundles_tool count={len(bundles)}')
        assert any(b.get('active') for b in bundles), 'No active bundle after load'

        # Test basic functionality that should work without external dependencies
        active_ctx = _tool(mcp_app.active_context)(TENANT)
        debug(f'active_context -> {active_ctx}')
        assert active_ctx.get('bundle_id'), 'Expected active bundle_id'
        
        # Test metric discovery
        metric_discover = _tool(mcp_app.metric_discover)('cpu utilization')
        debug(f'metric_discover -> {metric_discover}')
        assert 'candidates' in metric_discover
        
        debug('✅ Integration test completed successfully')
        
    finally:
        # Cleanup
        try:
            os.unlink(test_bundle_path)
            _tool(mcp_app.unload_bundle)(tenant_id=TENANT)
        except:
            pass


def debug(msg: str):
    if os.environ.get('DEBUG_INTEGRATION','1') == '0':
        return
    ts = datetime.utcnow().strftime('%H:%M:%S.%f')[:-3]
    print(f'[itest][{ts}] {msg}', flush=True)
