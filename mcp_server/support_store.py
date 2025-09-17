import os, sqlite3, time, hashlib
from typing import Optional, Dict, Any

DB_PATH = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "bundles.db"))

_SCHEMA = [
    # New canonical schema uses sptid (formerly tenant_id) as informational label.
    "CREATE TABLE IF NOT EXISTS bundles (bundle_id TEXT PRIMARY KEY, sptid TEXT NOT NULL, bundle_hash TEXT NOT NULL, path TEXT NOT NULL, host TEXT, logs_processed INTEGER, metrics_ingested INTEGER, start_ts INTEGER, end_ts INTEGER, replaced_previous INTEGER, reused INTEGER, created_at INTEGER, ingested INTEGER DEFAULT 0, plugins TEXT DEFAULT '', UNIQUE(sptid, bundle_hash))",
    # Single-row table storing the currently active bundle id (bundle only model)
    "CREATE TABLE IF NOT EXISTS global_active (id INTEGER PRIMARY KEY CHECK (id=1), bundle_id TEXT, activated_at INTEGER, FOREIGN KEY(bundle_id) REFERENCES bundles(bundle_id))"
]

_connection: Optional[sqlite3.Connection] = None
_clean_start_done = False

def _maybe_clean_start():
    """If PTOPS_CLEAN_START=1 is set, remove existing sqlite DB file before opening.

    This is executed lazily on first connection open so that import order does not matter.
    The flag is consumed only once per process lifetime.
    """
    global _clean_start_done
    if _clean_start_done:
        return
    if os.environ.get('PTOPS_CLEAN_START') == '1':
        try:
            if os.path.exists(DB_PATH):
                os.remove(DB_PATH)
        except Exception:
            pass
    _clean_start_done = True

def _get_conn() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        _maybe_clean_start()
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        cur = _connection.cursor()
        for stmt in _SCHEMA:
            cur.execute(stmt)
        # Migration path from legacy schema (tenant_id + active_context) to sptid + global_active.
        try:
            cur.execute("PRAGMA table_info(bundles)")
            cols = [r[1] for r in cur.fetchall()]
            # If legacy has tenant_id but no sptid, perform table rebuild rename.
            if 'tenant_id' in cols and 'sptid' not in cols:
                cur.execute("ALTER TABLE bundles RENAME TO bundles_old")
                cur.execute(_SCHEMA[0])  # recreate bundles with new schema
                # Copy data mapping tenant_id -> sptid
                copy_sql = ("INSERT OR IGNORE INTO bundles (bundle_id, sptid, bundle_hash, path, host, logs_processed, metrics_ingested, start_ts, end_ts, replaced_previous, reused, created_at, ingested, plugins) "
                            "SELECT bundle_id, tenant_id as sptid, bundle_hash, path, host, logs_processed, metrics_ingested, start_ts, end_ts, replaced_previous, reused, created_at, IFNULL(ingested,0), IFNULL(plugins,'') FROM bundles_old")
                cur.execute(copy_sql)
                cur.execute("DROP TABLE bundles_old")
            # Ensure auxiliary columns for very old deployments
            if 'ingested' not in cols:
                try: cur.execute("ALTER TABLE bundles ADD COLUMN ingested INTEGER DEFAULT 0")
                except Exception: pass
            if 'plugins' not in cols:
                try: cur.execute("ALTER TABLE bundles ADD COLUMN plugins TEXT DEFAULT ''")
                except Exception: pass
            # Drop legacy active_context table if present
            try:
                cur.execute("DROP TABLE IF EXISTS active_context")
            except Exception:
                pass
        except Exception:
            pass
        _connection.commit()
    return _connection


def file_bundle_hash(path: str) -> str:
    st = os.stat(path)
    h = hashlib.sha256()
    # For directories we hash structural metadata (name, mtime, child entries) so
    # that repeated loads of an unchanged support directory reuse the bundle.
    if os.path.isdir(path):
        meta = f"DIR:{os.path.basename(path)}:{int(st.st_mtime)}".encode()
        h.update(meta)
        try:
            entries = sorted(os.listdir(path))[:200]  # cap to avoid giant hash input
            for e in entries:
                h.update(e.encode())
        except Exception:
            pass
    else:
        # include size + mtime + name for file
        meta = f"FILE:{os.path.basename(path)}:{st.st_size}:{int(st.st_mtime)}".encode()
        h.update(meta)
        # first 1MB sample
        with open(path, 'rb') as f:
            h.update(f.read(1024*1024))
    return h.hexdigest()


def get_bundle_by_hash(sptid: str, bundle_hash: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute("SELECT * FROM bundles WHERE sptid=? AND bundle_hash=?", (sptid, bundle_hash))
    row = cur.fetchone()
    return dict(row) if row else None


def insert_bundle(record: Dict[str, Any]):
    conn = _get_conn()
    cols = ",".join(record.keys())
    placeholders = ":"+",:".join(record.keys())
    sql = f"INSERT INTO bundles ({cols}) VALUES ({','.join(':'+k for k in record.keys())})"
    conn.execute(sql, record)
    conn.commit()


def set_global_active(bundle_id: str):
    """Set the globally active bundle (single active)."""
    conn = _get_conn()
    now = int(time.time()*1000)
    cur = conn.execute("SELECT id FROM global_active WHERE id=1")
    if cur.fetchone():
        conn.execute("UPDATE global_active SET bundle_id=?, activated_at=? WHERE id=1", (bundle_id, now))
    else:
        conn.execute("INSERT INTO global_active(id,bundle_id,activated_at) VALUES(1,?,?)", (bundle_id, now))
    conn.commit()

# Backward compatibility wrapper (ignored tenant id)
def set_active_context(*args, **kwargs):  # legacy no-op wrapper
    if args:
        set_global_active(args[-1])


def get_active_context(*args, **kwargs):  # legacy wrapper, always returns None
    return None


def get_bundle(bundle_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute("SELECT * FROM bundles WHERE bundle_id=?", (bundle_id,))
    r = cur.fetchone()
    return dict(r) if r else None


def unload_global_active() -> Optional[str]:
    conn = _get_conn()
    cur = conn.execute("SELECT bundle_id FROM global_active WHERE id=1")
    row = cur.fetchone()
    if not row or not row['bundle_id']:
        return None
    bid = row['bundle_id']
    conn.execute("UPDATE global_active SET bundle_id=NULL WHERE id=1")
    conn.commit()
    return bid

# Legacy compatibility (returns None always now if called with tenant not active)
def unload_active(*args, **kwargs) -> Optional[str]:  # legacy wrapper
    return unload_global_active()


def list_bundles(*args, **kwargs):  # legacy wrapper returning all
    return list_all_bundles()

def list_all_bundles():
    conn = _get_conn()
    cur = conn.execute("SELECT * FROM bundles ORDER BY created_at DESC")
    return [dict(r) for r in cur.fetchall()]

def get_global_active() -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute("SELECT bundle_id, activated_at FROM global_active WHERE id=1")
    row = cur.fetchone()
    if not row or not row['bundle_id']:
        return None
    return {'bundle_id': row['bundle_id'], 'activated_at': row['activated_at']}

def promote_random_bundle() -> Optional[str]:
    """Promote a random existing bundle to active if none active."""
    conn = _get_conn()
    cur = conn.execute("SELECT bundle_id FROM global_active WHERE id=1 AND bundle_id IS NOT NULL")
    if cur.fetchone():
        return None
    cur = conn.execute("SELECT bundle_id FROM bundles ORDER BY RANDOM() LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    set_global_active(row['bundle_id'])
    return row['bundle_id']


def delete_all_bundles_for_tenant(*args, **kwargs) -> int:  # deprecated
    conn = _get_conn()
    cur = conn.execute("SELECT COUNT(*) FROM bundles")
    count = cur.fetchone()[0]
    conn.execute("DELETE FROM bundles")
    conn.execute("UPDATE global_active SET bundle_id=NULL WHERE id=1")
    conn.commit()
    return count
