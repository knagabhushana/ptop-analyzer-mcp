import os, tempfile, time
from mcp_server.mcp_app import load_bundle, unload_bundle

def _tool(t):
    return getattr(t, 'fn', t)

def make_fake_log(dir_path: str, ts_str: str, epoch: int):
    varlog = os.path.join(dir_path, 'var', 'log')
    os.makedirs(varlog, exist_ok=True)
    path = os.path.join(varlog, f'ptop-{ts_str}.log')
    with open(path,'w') as f:
        f.write(f"IDENT host hostA ver 1.0\n")
        f.write(f"TIME 0 {epoch}\n")
        # Provide CPU line matching simplified fallback regex used for test synthetic content
        f.write("CPU cpu u 10 90 5 3 1 0 0 irq h/s 0 0\n")
    return path

def make_fake_bundle(root: str, timestamps: list[str]):
    base_epoch = int(time.time()) - 3600
    for idx, ts in enumerate(timestamps):
        make_fake_log(root, ts, base_epoch + idx*60)

def _load(tenant: str, root: str, **extra):
    return _tool(load_bundle)(tenant_id=tenant, path=root, **extra)

def test_discovery_limits_default(monkeypatch):
    td = tempfile.mkdtemp(prefix='ptops_')
    tenant = 'NIOSSPT-12345'
    monkeypatch.setenv('SUPPORT_BASE_DIR', td)
    bundle_dir = os.path.join(td, tenant, 'fake_bundle')
    os.makedirs(bundle_dir, exist_ok=True)
    make_fake_bundle(bundle_dir, ['20250101_1200','20250102_1200','20250103_1200','20250104_1200'])
    body = _load(tenant, bundle_dir)
    # Default DEFAULT_MAX_FILES=1 so only 1 log ingested despite 4 present
    assert body['logs_processed'] == 1
    import shutil; shutil.rmtree(td, ignore_errors=True)

def test_discovery_override(monkeypatch):
    td = tempfile.mkdtemp(prefix='ptops_')
    try:
        tenant = 'NIOSSPT-12346'
        monkeypatch.setenv('SUPPORT_BASE_DIR', td)
        bundle_dir = os.path.join(td, tenant, 'fake_bundle')
        os.makedirs(bundle_dir, exist_ok=True)
        make_fake_bundle(bundle_dir, ['20250101_1200','20250102_1200'])
        body = _load(tenant, bundle_dir, max_files=5)
        assert body['logs_processed'] == 2
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)

def test_discovery_truncate(monkeypatch):
    td = tempfile.mkdtemp(prefix='ptops_')
    try:
        tenant = 'NIOSSPT-12347'
        monkeypatch.setenv('SUPPORT_BASE_DIR', td)
        bundle_dir = os.path.join(td, tenant, 'fake_bundle')
        os.makedirs(bundle_dir, exist_ok=True)
        make_fake_bundle(bundle_dir, ['20250101_1200','20250102_1200','20250103_1200','20250104_1200','20250105_1200'])
        body = _load(tenant, bundle_dir, max_files=2)
        assert body['logs_processed'] == 2
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)

def test_reuse_behavior(monkeypatch):
    td = tempfile.mkdtemp(prefix='ptops_')
    try:
        tenant = 'NIOSSPT-12348'
        monkeypatch.setenv('SUPPORT_BASE_DIR', td)
        bundle_dir = os.path.join(td, tenant, 'fake_bundle')
        os.makedirs(bundle_dir, exist_ok=True)
        make_fake_bundle(bundle_dir, ['20250101_1200'])
        r1 = _load(tenant, bundle_dir)
        b1 = r1['bundle_id']
        r2 = _load(tenant, bundle_dir)
        assert r2['reused'] is True
        assert r2['bundle_id'] == b1
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)

def test_optional_vm_delete(monkeypatch):
    td = tempfile.mkdtemp(prefix='ptops_')
    tenant = 'NIOSSPT-12349'
    try:
        monkeypatch.setenv('SUPPORT_BASE_DIR', td)
        bundle_dir = os.path.join(td, tenant, 'fake_bundle')
        os.makedirs(bundle_dir, exist_ok=True)
        make_fake_bundle(bundle_dir, ['20250101_1200'])
        body = _load(tenant, bundle_dir)
        monkeypatch.setenv('VM_ALLOW_DELETE','1')
        # unload by bundle id (path param deprecated/removed)
        unload = _tool(unload_bundle)(tenant_id=tenant, bundle_id=body['bundle_id'])
        assert unload['unloaded'] is True
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)