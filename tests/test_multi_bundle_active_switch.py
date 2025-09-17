import os, tarfile, time, tempfile, shutil, pytest
from mcp_server.mcp_app import load_bundle, active_context, list_bundles_tool, unload_bundle

def _tool(t):
    return getattr(t, 'fn', t)


def _make_sb(tenant_dir: str, idx: int) -> str:
    # creates sb-YYYYMMDD_HHMM_idx.tar.gz
    ts = time.strftime('%Y%m%d_%H%M', time.gmtime(time.time()+idx))
    path = os.path.join(tenant_dir, f'sb-{ts}_{idx}.tar.gz')
    with tarfile.open(path, 'w:gz') as tf:
        pass
    return path


def test_load_latest_when_only_tenant_id_provided_and_switch_active():
    base = tempfile.mkdtemp(prefix='support_root_')
    os.environ['SUPPORT_BASE_DIR'] = base
    tenant = 'NIOSSPT7777'
    tdir = os.path.join(base, tenant)
    os.makedirs(tdir)
    # Purge all bundles for this tenant to ensure a clean state
    _tool(unload_bundle)(tenant_id=tenant, purge_all=True)
    # create two bundles (older then newer)
    older = _make_sb(tdir, 0)
    time.sleep(0.05)
    newer = _make_sb(tdir, 1)
    # load using only tenant id (should pick newer)
    r1 = _tool(load_bundle)(tenant_id=tenant, path=None)
    active1 = r1['bundle_id']
    # load explicit older bundle path
    r2 = _tool(load_bundle)(tenant_id=tenant, path=older)
    b_older = r2['bundle_id']
    # After loading explicit older, it should now be active
    ac = _tool(active_context)(tenant)
    assert ac['bundle_id'] == b_older
    # Switch back to newer by reloading newer path (reuse scenario)
    r3 = _tool(load_bundle)(tenant_id=tenant, path=newer)
    ac2 = _tool(active_context)(tenant)
    assert ac2['bundle_id'] != b_older
    # list bundles shows both
    lbs = _tool(list_bundles_tool)(tenant)
    assert len(lbs) == 2, f"Expected 2 bundles, found {len(lbs)}: {lbs}"
    assert any(b['active'] for b in lbs)
    # unload passive older bundle via bundle_id
    u1 = _tool(unload_bundle)(tenant_id=tenant, bundle_id=b_older)
    lbs2 = _tool(list_bundles_tool)(tenant)
    assert len(lbs2) == 1
    # purge remaining
    uall = _tool(unload_bundle)(tenant_id=tenant, purge_all=True)
    lbs3 = _tool(list_bundles_tool)(tenant)
    assert lbs3 == []
    shutil.rmtree(base)


def test_unload_active_then_queries_fail():
    base = tempfile.mkdtemp(prefix='support_root_')
    os.environ['SUPPORT_BASE_DIR'] = base
    tenant = 'NIOSSPT8888'
    tdir = os.path.join(base, tenant); os.makedirs(tdir)
    p = _make_sb(tdir, 0)
    # Purge all bundles for this tenant to ensure a clean state
    _tool(unload_bundle)(tenant_id=tenant, purge_all=True)
    r = _tool(load_bundle)(tenant_id=tenant, path=None)
    bid = r['bundle_id']
    # unload active by bundle id
    u = _tool(unload_bundle)(tenant_id=tenant, bundle_id=bid)
    ac = _tool(active_context)(tenant)
    assert ac['bundle_id'] is None, 'Expected no active bundle after unload'
    shutil.rmtree(base)
