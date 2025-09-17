from __future__ import annotations
import sys, os
from mcp_server.ingestion.parser import PTOPSParser
from mcp_server.timescale.writer import TimescaleWriter

def main():
    if len(sys.argv) < 3:
        print("usage: python -m mcp_server.timescale.ingest_cpu_demo <ptop_log_path> <bundle_id>")
        sys.exit(1)
    log_path = sys.argv[1]
    bundle_id = sys.argv[2]
    parser = PTOPSParser(log_path)
    writer = TimescaleWriter(batch_size=1000)
    cpu_records = 0
    cpu_samples = 0
    seen_rows = set()
    for rec in parser.iter_records():
        if rec.prefix == 'CPU':
            cpu_records += 1
    # Re-iterate for samples (iter_records exhausts file; recreate parser)
    parser = PTOPSParser(log_path)
    for sm in parser.iter_metric_samples():
        if sm.name.startswith('cpu_'):
            sm.labels['bundle_id'] = bundle_id
            # ensure host label present
            sm.labels['host'] = sm.labels.get('host') or 'demo-host'
            writer.add(sm)
            cpu_samples += 1
            # logical row key (ts,cpu)
            if sm.name == 'cpu_utilization':  # one per CPU line ensures row count comparable
                # timestamp ms + cpu_id label (schema aligned)
                seen_rows.add((sm.ts_ms, sm.labels.get('cpu_id') or sm.labels.get('cpu')))
    writer.flush()
    expected_rows = len(seen_rows)
    # Query DB for actual rows
    rows_in_db = None
    sample_row = None
    if writer._conn:
        with writer._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ptops_cpu WHERE bundle_id=%s", (bundle_id,))
            rows_in_db = cur.fetchone()[0]
            cur.execute("SELECT * FROM ptops_cpu WHERE bundle_id=%s ORDER BY ts LIMIT 1", (bundle_id,))
            sample_row = cur.fetchone()
    print({
        'log': log_path,
        'bundle_id': bundle_id,
        'cpu_records': cpu_records,
        'cpu_metric_samples_emitted': cpu_samples,
        'expected_logical_rows': expected_rows,
        'rows_in_db': rows_in_db,
        'writer_stats': writer.stats(),
        'sample_row': sample_row,
    })

if __name__ == '__main__':
    main()
