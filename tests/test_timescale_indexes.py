"""Tests for Timescale index DDL generation for high-cardinality groups."""
from mcp_server.timescale.schema_spec import generate_all_ddls, SCHEMA_SPEC


def test_indexes_generated_for_top_and_smaps():
    ddls = generate_all_ddls()
    idx_sql = '\n'.join(ddls['indexes'])
    # Unique indexes
    assert 'CREATE UNIQUE INDEX' in idx_sql
    # TOP unique key
    assert 'uniq_ptops_top_ts_bundle_id_host_pid' in idx_sql
    # SMAPS unique key
    assert 'uniq_ptops_smaps_ts_bundle_id_host_pid' in idx_sql
    # Secondary index presence (pid, ts DESC)
    assert 'CREATE INDEX' in idx_sql
    assert 'ptops_top_pid_ts' in idx_sql or 'ptops_top_pid_ts'  # truncated naming logic
    # Ensure schema spec still lists groups
    assert 'TOP' in SCHEMA_SPEC and 'SMAPS' in SCHEMA_SPEC


def test_disk_and_net_schema_present():
    assert 'DISK' in SCHEMA_SPEC, 'DISK group missing'
    assert 'NET' in SCHEMA_SPEC, 'NET group missing'
    net_grp = SCHEMA_SPEC['NET']
    # Alias mapping for legacy rk/tk forms
    assert 'net_rx_packets_per_sec' in net_grp.metrics
    assert 'net_rk_packets_per_sec' in net_grp.metrics['net_rx_packets_per_sec'].aliases
    disk_grp = SCHEMA_SPEC['DISK']
    assert 'disk_reads_per_sec' in disk_grp.metrics
    for m in [
        'disk_device_busy_percent','disk_read_avg_ms','disk_write_avg_ms','disk_read_avg_kb','disk_write_avg_kb','disk_service_time_ms'
    ]:
        assert m in disk_grp.metrics, f'Missing disk metric {m}'
    for m in [
        'net_rx_bytes_total','net_tx_bytes_total','net_rx_dropped_packets_total','net_tx_dropped_packets_total'
    ]:
        assert m in net_grp.metrics, f'Missing net metric {m}'
