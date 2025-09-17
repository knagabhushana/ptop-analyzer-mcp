"""TimescaleWriter with optional direct COPY into TimescaleDB.

Maintains a minimal surface compatible with VictoriaMetricsWriter used elsewhere:
 attributes: base_url (unused here)
 methods: add(sample), flush(), stats()
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone

from ..ingestion.parser import MetricSample
from .schema_spec import SCHEMA_SPEC, TableGroup
from ..debug_util import dbg


@dataclass
class _PendingRow:
    table: str
    key: Tuple[Any, ...]  # composite key identifying a coalesced logical row
    values: Dict[str, Any]  # column -> value


class TimescaleWriter:
    def __init__(self, batch_size: int = 2000, dsn: Optional[str] = None, insert_page_size: int = 200, use_copy: Optional[bool] = None):
        """Timescale writer accumulating logical coalesced rows then inserting in batches.

        Parameters:
            batch_size: number of coalesced logical rows kept in memory before an automatic flush.
            dsn: PostgreSQL/Timescale connection string.
            insert_page_size: page size passed to execute_values; keeps individual INSERT statements
                reasonably small (memory friendly) while still amortizing round trips.
            use_copy: if True, use PostgreSQL COPY command for maximum performance. 
                     If None, reads from PTOPS_USE_COPY_COMMAND environment variable (default: False).
        """
        import os
        # Allow environment overrides (constructor args still take precedence when explicitly passed)
        if 'PTOPS_BATCH_SIZE' in os.environ and batch_size == 2000:  # only override default, not explicit caller value
            try:
                batch_size = int(os.environ.get('PTOPS_BATCH_SIZE', batch_size))
            except Exception:
                pass
        if 'PTOPS_INSERT_PAGE_SIZE' in os.environ and insert_page_size == 200:
            try:
                insert_page_size = int(os.environ.get('PTOPS_INSERT_PAGE_SIZE', insert_page_size))
            except Exception:
                pass
        self.batch_size = batch_size
        self.insert_page_size = max(25, insert_page_size)  # guard against extremely tiny sizes
        
        # Configure COPY vs INSERT method
        if use_copy is None:
            use_copy = os.environ.get('PTOPS_USE_COPY_COMMAND', '').lower() in ('true', '1', 'yes')
        self.use_copy = use_copy
        
        # Pending rows now kept as a dict keyed by (table, ts, bundle_id, sptid, host, *local_labels)
        self._pending: Dict[Tuple[Any, ...], _PendingRow] = {}
        self._last_key: Optional[Tuple[Any, ...]] = None  # track last logical row key
        self.total_rows_added = 0
        self.total_flushes = 0
        self.total_rows_flushed = 0
        self.last_flush_payload: Dict[str, str] = {}
        self.dsn = dsn or __import__('os').environ.get('TIMESCALE_DSN')
        self.base_url = None  # API compatibility placeholder
        self._conn = None
        self._ensure_connection()

        # Instrumentation / profiling
        self._flush_durations: List[float] = []  # recent flush durations (seconds)
        self._total_flush_time: float = 0.0
        self._last_flush_seconds: float = 0.0
        self._max_flush_seconds: float = 0.0
        self._last_flush_rows: int = 0
        self._adaptive_enabled = os.environ.get('PTOPS_ADAPTIVE_BATCH', '').lower() in ('1','true','yes')
        self._max_batch_size = int(os.environ.get('PTOPS_MAX_BATCH_SIZE', '50000'))
        self._adaptive_upscales = 0

    def _ensure_connection(self):
        if self.dsn and self._conn is None:
            try:
                import psycopg
                self._conn = psycopg.connect(self.dsn)
                dbg(f'timescale_connect_ok dsn={self.dsn}')
            except Exception as e:
                dbg(f'timescale_connect_fail err={e.__class__.__name__}:{e}')
                self._conn = None

    def _resolve_group_and_column(self, metric_name: str) -> Tuple[Optional[TableGroup], Optional[str], bool]:
        """Return (group, column_name, is_alias)."""
        for grp in SCHEMA_SPEC.values():
            for mname, meta in grp.metrics.items():
                if metric_name == mname:
                    return grp, (meta.column or mname), False
                if metric_name in (meta.aliases or []):
                    return grp, (meta.column or mname), True
        return None, None, False

    def add(self, sample: MetricSample):
        grp, metric_column, is_alias = self._resolve_group_and_column(sample.name)
        if not grp or not metric_column:
            return
        labels = sample.labels or {}
        iso_ts = datetime.fromtimestamp(sample.ts_ms/1000.0, tz=timezone.utc).isoformat()
        key_parts = [grp.table, iso_ts, labels.get('bundle_id'), labels.get('sptid'), grp.category, labels.get('host')]
        for lbl in grp.local_labels:
            key_parts.append(labels.get(lbl))
        key = tuple(key_parts)
        pending = self._pending.get(key)
        # Flush only when starting a NEW logical row AND batch size threshold reached.
        if pending is None and self._pending and len(self._pending) >= self.batch_size and self._last_key != key:
            self.flush()
            pending = self._pending.get(key)
        if not pending:
            base: Dict[str, Any] = {
                'ts': iso_ts,
                'bundle_id': labels.get('bundle_id'),
                'sptid': labels.get('sptid'),
                'metric_category': grp.category,
                'host': labels.get('host'),
            }
            for lbl in grp.local_labels:
                base[lbl] = labels.get(lbl)
            # initialize metric columns present in schema to None
            for mname, meta in grp.metrics.items():
                col = meta.column or mname
                base.setdefault(col, None)
            pending = _PendingRow(grp.table, key, base)
            self._pending[key] = pending
            self.total_rows_added += 1  # count logical rows
        # If alias and column already has a value, skip
        if is_alias and pending.values.get(metric_column) is not None:
            self._last_key = key
            return
        pending.values[metric_column] = sample.value
        self._last_key = key  # mark last updated logical row

    def serialize_batches(self) -> Dict[str, List[_PendingRow]]:
        per_table: Dict[str, List[_PendingRow]] = {}
        for r in self._pending.values():
            per_table.setdefault(r.table, []).append(r)
        return per_table

    def _flush_with_copy(self, table: str, rows: List[_PendingRow], grp: TableGroup, col_list: List[str]) -> None:
        """Use PostgreSQL COPY command for maximum bulk insert performance."""
        import io
        import csv
        
        try:
            # Prepare CSV data in memory
            output = io.StringIO()
            writer = csv.writer(output, delimiter='\t', quoting=csv.QUOTE_MINIMAL)
            
            for r in rows:
                csv_row = []
                for col in col_list:
                    value = r.values.get(col)
                    if value is None or value == '':
                        csv_row.append('\\N')  # PostgreSQL NULL marker
                    elif isinstance(value, str):
                        # Escape any special characters
                        csv_row.append(value.replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r'))
                    else:
                        csv_row.append(str(value))
                writer.writerow(csv_row)
            
            # Use COPY command
            output.seek(0)
            with self._conn.cursor() as cur:
                # Ensure columns exist first
                for col in col_list:
                    if col not in ('ts','bundle_id','sptid','metric_category','host', *grp.local_labels):
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION")
                
                # Use COPY FROM STDIN
                copy_sql = f"COPY {table} ({','.join(col_list)}) FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t', NULL '\\N')"
                cur.copy_expert(copy_sql, output)
                
            dbg(f'timescale_copy_ok table={table} rows={len(rows)} mode=copy_from_stdin')
            
        except Exception as e:
            dbg(f'timescale_copy_fail table={table} err={e.__class__.__name__}:{e}')
            # Fallback to INSERT method
            dbg(f'timescale_copy_fallback table={table} falling_back_to_insert')
            self._flush_with_insert(table, rows, grp, col_list)

    def _flush_with_insert(self, table: str, rows: List[_PendingRow], grp: TableGroup, col_list: List[str]) -> None:
        """Use traditional INSERT method with execute_values optimization."""
        try:
            from psycopg import extras as _pg_extras  # local import; available via psycopg[binary]
        except Exception:  # pragma: no cover - fallback path
            _pg_extras = None
            
        try:
            with self._conn.cursor() as cur:
                # Ensure columns exist first
                for col in col_list:
                    if col not in ('ts','bundle_id','sptid','metric_category','host', *grp.local_labels):
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION")
                        
                value_tuples: List[Tuple[Any, ...]] = []
                for r in rows:
                    tup: List[Any] = []
                    for c in col_list:
                        v = r.values.get(c)
                        tup.append(v if v != '' else None)
                    value_tuples.append(tuple(tup))
                    
                if _pg_extras and hasattr(_pg_extras, 'execute_values'):
                    _pg_extras.execute_values(
                        cur,
                        f"INSERT INTO {table} ({','.join(col_list)}) VALUES %s",
                        value_tuples,
                        page_size=self.insert_page_size
                    )
                    dbg(f'timescale_insert_ok table={table} rows={len(rows)} mode=execute_values page_size={self.insert_page_size}')
                else:
                    placeholders = '(' + ','.join(['%s']*len(col_list)) + ')'
                    sql = f"INSERT INTO {table} ({','.join(col_list)}) VALUES " + ','.join([placeholders]*len(value_tuples))
                    flat_params: List[Any] = []
                    for vt in value_tuples:
                        flat_params.extend(vt)
                    cur.execute(sql, flat_params)
                    dbg(f'timescale_insert_ok table={table} rows={len(rows)} mode=multi_values')
                    
        except Exception as e:
            dbg(f'timescale_insert_fail table={table} err={e.__class__.__name__}:{e}')
            raise  # Re-raise to be handled by flush()

    def flush(self):
        if not self._pending:
            return
        import time as _time
        flush_start = _time.time()
        batches = self.serialize_batches()
        self.last_flush_payload.clear()
        
        for table, rows in batches.items():
            self.total_rows_flushed += len(rows)
            grp = None
            for g in SCHEMA_SPEC.values():
                if g.table == table:
                    grp = g
                    break
            if not grp:
                continue
                
            # Determine required columns
            required_metric_cols = set()
            for mname, meta in grp.metrics.items():
                required_metric_cols.add(meta.column or mname)
            for r in rows:
                for k in r.values.keys():
                    if k not in ('ts','bundle_id','sptid','metric_category','host', *grp.local_labels):
                        required_metric_cols.add(k)
            col_list = ["ts","bundle_id","sptid","metric_category","host"] + grp.local_labels + sorted(required_metric_cols)
            
            if self._conn is not None:
                try:
                    # Choose method based on configuration
                    if self.use_copy:
                        self._flush_with_copy(table, rows, grp, col_list)
                    else:
                        self._flush_with_insert(table, rows, grp, col_list)
                        
                    self._conn.commit()
                except Exception as e:
                    dbg(f'timescale_flush_fail table={table} err={e.__class__.__name__}:{e}')
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                        
        self.total_flushes += 1
        self._pending.clear()
        self._last_key = None
        # Timing capture
        flush_duration = _time.time() - flush_start
        self._last_flush_seconds = flush_duration
        self._total_flush_time += flush_duration
        if flush_duration > self._max_flush_seconds:
            self._max_flush_seconds = flush_duration
        # Keep only last 50 samples to bound memory
        self._flush_durations.append(flush_duration)
        if len(self._flush_durations) > 50:
            self._flush_durations.pop(0)
        # Adaptive batch sizing (INSERT mode only)
        if self._adaptive_enabled and not self.use_copy:
            # Record rows flushed this cycle
            rows_this_flush = self.total_rows_flushed - self._last_flush_rows
            self._last_flush_rows = self.total_rows_flushed
            # If we always hit the current batch_size exactly, scale up (simple heuristic)
            if rows_this_flush >= self.batch_size and self.batch_size < self._max_batch_size:
                new_size = min(self.batch_size * 2, self._max_batch_size)
                if new_size != self.batch_size:
                    self.batch_size = new_size
                    self._adaptive_upscales += 1
                    dbg(f'adaptive_batch_upscale new_batch_size={self.batch_size} upscales={self._adaptive_upscales}')
        else:
            # still update last flush rows for stat reporting
            self._last_flush_rows = self.total_rows_flushed

    def stats(self) -> Dict[str, Any]:
        avg_flush = (self._total_flush_time / self.total_flushes) if self.total_flushes else 0.0
        return {
            'total_rows_added': self.total_rows_added,
            'total_rows_flushed': self.total_rows_flushed,
            'total_flushes': self.total_flushes,
            'connected': bool(self._conn),
            'use_copy': self.use_copy,
            'insert_method': 'COPY' if self.use_copy else 'INSERT',
            'batch_size': self.batch_size,
            'insert_page_size': self.insert_page_size,
            'adaptive_enabled': self._adaptive_enabled,
            'adaptive_upscales': self._adaptive_upscales,
            'avg_flush_seconds': round(avg_flush, 6),
            'last_flush_seconds': round(self._last_flush_seconds, 6),
            'max_flush_seconds': round(self._max_flush_seconds, 6),
        }

__all__ = ["TimescaleWriter"]
