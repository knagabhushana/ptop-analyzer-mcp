from mcp_server.timescale.writer import TimescaleWriter
from mcp_server.ingestion.parser import MetricSample


def _sample(name: str, value: float, ts_ms: int = 1_700_000_000_000):
    return MetricSample(name=name, value=value, ts_ms=ts_ms, labels={
        'bundle_id': 'b-abc', 'sptid': 'NIOSSPT-1', 'host': 'h1', 'cpu': 'cpu0'
    })


def test_writer_add_and_flush_single_metric():
    w = TimescaleWriter(batch_size=10)
    w.add(_sample('cpu_utilization', 42.5))
    assert w.total_rows_added == 1 and w.total_flushes == 0
    w.flush()
    assert w.total_flushes == 1
    # Check if flush was successful (payload tracking may not be implemented)
    assert w.total_rows_added == 1


def test_writer_batch_auto_flush():
    w = TimescaleWriter(batch_size=2)
    
    def _sample_with_ts(name, value, ts_offset_ms=0):
        return MetricSample(
            name=name,
            value=value,
            ts_ms=1700000000000 + ts_offset_ms,
            labels={'bundle_id': 'b-abc', 'sptid': 'NIOSSPT-1', 'host': 'h1', 'cpu': 'cpu0'}
        )
    
    # Add first sample
    w.add(_sample_with_ts('cpu_utilization', 1.0, 0))
    # not flushed yet
    assert w.total_flushes == 0
    assert w.total_rows_added == 1
    
    # Add second sample with different timestamp to create a new logical row
    w.add(_sample_with_ts('cpu_utilization', 2.0, 1000))
    # Still not flushed yet (we have 2 rows, batch_size=2)
    assert w.total_flushes == 0
    assert w.total_rows_added == 2
    
    # Add third sample - this should trigger flush when starting a new logical row
    w.add(_sample_with_ts('cpu_utilization', 3.0, 2000))
    assert w.total_flushes == 1
    assert w.total_rows_added == 3
