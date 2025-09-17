"""Timescale bootstrap: create required tables & views if ENABLE_TIMESCALE=1.

Safe to run repeatedly (IF NOT EXISTS semantics applied post‑hoc: we catch duplicate errors).
"""
from __future__ import annotations
import os, psycopg
from .schema_spec import generate_all_ddls

def bootstrap_timescale(dsn: str | None = None, create_hypertables: bool = True) -> dict:
    dsn = dsn or os.environ.get('TIMESCALE_DSN')
    if not dsn:
        return {'enabled': False, 'reason': 'no_dsn'}
    ddls = generate_all_ddls()
    created: list[str] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Ensure extension
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            except Exception:
                pass
            for stmt in ddls['tables']:
                try:
                    cur.execute(stmt)
                    created.append(stmt.split()[2])
                except Exception:
                    conn.rollback()
            if create_hypertables:
                for stmt in ddls['tables']:
                    tbl = stmt.split()[2]
                    try:
                        cur.execute(f"SELECT create_hypertable('{tbl}','ts', if_not_exists => TRUE)")
                    except Exception:
                        conn.rollback()
            for v in ddls['views']:
                try:
                    cur.execute(v)
                except Exception:
                    conn.rollback()
            # Indexes (unique + secondary)
            for idx in ddls.get('indexes', []):
                try:
                    cur.execute(idx)
                except Exception:
                    conn.rollback()
        conn.commit()
    return {'enabled': True, 'created': created}

__all__ = ["bootstrap_timescale"]