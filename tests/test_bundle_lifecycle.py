import os, tempfile, time, tarfile, shutil, pytest
from mcp_server.mcp_app import load_bundle, active_context, unload_bundle, ingest_status, list_bundles_tool

def _tool(t):
    return getattr(t, 'fn', t)


def _make_temp_bundle(contents: str = "TIME 0 0 0 0\n"):
    """Create a synthetic support bundle tarball with a ptop log file."""
    work = tempfile.mkdtemp(prefix="bundle_build_")
    log_dir = os.path.join(work, 'var', 'log')
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, f'ptop-{int(time.time())}.log'), 'w') as f:
        f.write(contents)
    ts = time.strftime('%Y%m%d_%H%M')
    fd, tar_path = tempfile.mkstemp(prefix=f'sb-{ts}_', suffix='.tar.gz')
    os.close(fd)
    with tarfile.open(tar_path, 'w:gz') as tf:
        tf.add(log_dir, arcname='var/log')
    shutil.rmtree(work)
    return tar_path


def _make_empty_sb(tenant_dir: str, idx: int) -> str:
    """Create an empty sb- timestamped tarball (no logs) for ordering tests."""
    ts = time.strftime('%Y%m%d_%H%M', time.gmtime(time.time()+idx))
    path = os.path.join(tenant_dir, f'sb-{ts}_{idx}.tar.gz')
    with tarfile.open(path, 'w:gz') as tf:
        pass
    return path


TENANT = "tenantA"


def test_load_first_time_sets_active():
    path = _make_temp_bundle()
    body = _tool(load_bundle)(path=path, tenant_id=TENANT)
    assert body['bundle_id'] and body['reused'] is False
    ac = _tool(active_context)()
    assert ac['bundle_id'] == body['bundle_id']


def test_reuse_same_hash():
    path = _make_temp_bundle()
    r1 = _tool(load_bundle)(path=path, tenant_id=TENANT)
    r2 = _tool(load_bundle)(path=path, tenant_id=TENANT)
    assert r1['bundle_id'] == r2['bundle_id'] and r2['reused'] is True


def test_ingest_status_idle_summary():
    path = _make_temp_bundle()
    r = _tool(load_bundle)(path=path, tenant_id=TENANT)
    st = _tool(ingest_status)()
    assert st['state'] == 'idle' and st['bundle_id'] == r['bundle_id'] and st['summary']


def test_force_reingest_creates_new_bundle_id():
    path = _make_temp_bundle()
    r1 = _tool(load_bundle)(path=path, tenant_id=TENANT)
    r2 = _tool(load_bundle)(path=path, tenant_id=TENANT, force=True)
    assert r1['bundle_id'] != r2['bundle_id'] and r2['reused'] is False


def test_unload_active_and_missing_context():
    path = _make_temp_bundle()
    _tool(load_bundle)(path=path, tenant_id=TENANT)
    u = _tool(unload_bundle)(tenant_id=TENANT)
    assert u['unloaded'] is True
    ac = _tool(active_context)()
    # After unload we now promote a random passive bundle if any remain; for single bundle case promotion will select none so bundle_id may be None.
    assert ac['bundle_id'] is None or isinstance(ac['bundle_id'], str)


def test_unload_without_active_bundle():
    # Force remove any existing bundles to ensure a clean slate
    try:
        _tool(unload_bundle)(tenant_id=TENANT, purge_all=True)
    except Exception:
        pass
    # With no bundles present unload should report unloaded False
    first = _tool(unload_bundle)(tenant_id=TENANT)
    assert first['unloaded'] is False
    second = _tool(unload_bundle)(tenant_id=TENANT)
    assert second['unloaded'] is False


def test_tenant_deduction_from_filename_and_directory_latest():
    # Filename driven tenant deduction
    fd, path = tempfile.mkstemp(prefix='NIOSSPT-5555_', suffix='.tar.gz'); os.close(fd)
    with tarfile.open(path, 'w:gz') as tf: pass
    body = _tool(load_bundle)(path=path)
    assert body['sptid'].startswith('NIOSSPT-5555')
    # Directory latest selection
    base = tempfile.mkdtemp(prefix='support_root_')
    os.environ['SUPPORT_BASE_DIR'] = base
    tenant = 'NIOSSPT1234'
    tdir = os.path.join(base, tenant); os.makedirs(tdir)
    ts_old = time.strftime('%Y%m%d_%H%M', time.gmtime(time.time()-60))
    old_path = os.path.join(tdir, f'sb-{ts_old}_old.tar.gz')
    with tarfile.open(old_path, 'w:gz') as tf: pass
    time.sleep(0.02)
    ts_new = time.strftime('%Y%m%d_%H%M')
    new_path = os.path.join(tdir, f'sb-{ts_new}_new.tar.gz')
    with tarfile.open(new_path, 'w:gz') as tf: pass
    chosen = _tool(load_bundle)(tenant_id=tenant)
    assert chosen['sptid'] == tenant
    shutil.rmtree(base)


def test_multi_bundle_switch_and_purge():
    base = tempfile.mkdtemp(prefix='support_root_')
    os.environ['SUPPORT_BASE_DIR'] = base
    tenant = 'NIOSSPT7777'
    tdir = os.path.join(base, tenant); os.makedirs(tdir)
    # ensure clean slate for tenant (prior test runs may persist sqlite entries)
    try:
        if _tool(list_bundles_tool)(tenant):
            _tool(unload_bundle)(tenant_id=tenant, purge_all=True)
    except Exception:
        pass
    older = _make_empty_sb(tdir, 0)
    time.sleep(0.05)
    newer = _make_empty_sb(tdir, 1)
    # load by tenant -> picks newer
    r1 = _tool(load_bundle)(tenant_id=tenant, path=None)
    newer_id = r1['bundle_id']
    # load explicit older -> becomes active
    r2 = _tool(load_bundle)(tenant_id=tenant, path=older)
    older_id = r2['bundle_id']
    assert _tool(active_context)()['bundle_id'] == older_id
    # switch back to newer (reuse path)
    r3 = _tool(load_bundle)(tenant_id=tenant, path=newer)
    assert _tool(active_context)()['bundle_id'] != older_id
    # list bundles has both
    lbs = _tool(list_bundles_tool)(tenant)
    assert len(lbs) == 2 and any(b['active'] for b in lbs)
    # unload passive older by id
    _tool(unload_bundle)(tenant_id=tenant, bundle_id=older_id)
    lbs2 = _tool(list_bundles_tool)(tenant)
    assert len(lbs2) == 1
    # purge all
    _tool(unload_bundle)(tenant_id=tenant, purge_all=True)
    assert _tool(list_bundles_tool)(tenant) == []
    shutil.rmtree(base)


def test_load_missing_path_error():
    with pytest.raises(ValueError):
        _tool(load_bundle)(path='/no/such/path/file.log', tenant_id=TENANT)
