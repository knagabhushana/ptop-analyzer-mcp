"""Parser metric coverage tests (Phase 3 deduplicated).

We previously had many single-assert tests each checking the presence of a metric
or prefix. Those are now consolidated into a single parametrized test while a
few specialized label / multi‑metric assertions remain explicit for clarity.

Coverage preserved:
 - CPU metric + label presence
 - TOP process metrics + pid/ppid labels
 - SMAPS metrics + host label propagation
 - NET, DISK, memory, histograms, dbmpool, fpports, fpm(buf), dot, doh, tcp_dca,
   fpc metrics and variants (prefix or exact) via parametrization
 - IDENT derived labels (host, ptop_version)
"""

import os
import pytest
from mcp_server.ingestion.parser import PTOPSParser

SAMPLE_LOG = os.path.abspath(os.path.join(os.path.dirname(__file__), 'data', 'sample_ptops.log'))

# Parse once (cost is low) and build indices used across tests
parser = PTOPSParser(SAMPLE_LOG)
all_samples = list(parser.iter_metric_samples())
by_name: dict[str, list] = {}
for s in all_samples:
    by_name.setdefault(s.name, []).append(s)


def test_cpu_metric_labels():
    util = by_name.get('cpu_utilization_percent') or []
    assert util, 'Missing CPU utilization metric'
    for s in util[:2]:  # spot-check a couple
        assert 'cpu' in s.labels and s.ts_ms > 0


def test_top_metrics_with_process_labels():
    assert 'top_cpu_percent' in by_name, 'Missing TOP cpu percent'
    assert 'top_cpu_time_total_seconds' in by_name, 'Missing TOP total cpu time'
    sample = by_name['top_cpu_percent'][0]
    assert {'pid', 'ppid'} <= set(sample.labels), 'Missing process labels on TOP sample'


def test_smaps_metrics_and_host_label():
    assert 'smaps_rss_kb' in by_name and 'smaps_swap_kb' in by_name, 'Missing SMAPS metrics'
    for s in by_name['smaps_rss_kb'][:2]:
        assert s.labels.get('host') == 'myhost'


def test_disk_and_net_metrics_present():
    # DISK
    disk_any = [n for n in by_name if n.startswith('disk_')]
    assert disk_any, 'No DISK metrics parsed'
    # NET normalized
    net_norm = [n for n in by_name if n.startswith('net_rx_packets_per_sec') or n.startswith('net_tx_packets_per_sec')]
    assert net_norm, 'No NET rate metrics parsed'
    # Legacy alias presence (rk/tk) optional; if present ensure canonical also present
    legacy_any = [n for n in by_name if n.startswith('net_rk_packets_per_sec')]
    if legacy_any:
        assert 'net_rx_packets_per_sec' in by_name


def test_ident_label_propagation():
    any_metric = next(iter(all_samples))
    assert any_metric.labels.get('host') == 'myhost'
    assert any_metric.labels.get('ptop_version') == '1.2.3'


# Parametrized simple presence checks (exact, prefix, either of several exacts)
PARAM_CASES = [
    ('memory prefix', 'prefix', 'mem_', None),
    ('disk read exact', 'exact', 'disk_reads_per_sec', None),
    ('disk write prefix', 'prefix', 'disk_writes_per_sec', None),
    ('net rx rate either normalized or legacy', 'either_exact', ['net_rx_packets_per_sec', 'net_rk_packets_per_sec'], None),
    ('net rx total exact', 'exact', 'net_rx_packets_total', None),
    ('dbwr histogram prefix', 'prefix', 'dbwr_bucket_count_total', None),
    ('dbwa histogram prefix', 'prefix', 'dbwa_bucket_count_total', None),
    ('dbrd histogram prefix', 'prefix', 'dbrd_bucket_count_total', None),
    ('dbmpool prefix', 'prefix', 'dbmpool_', None),
    ('fpports prefix', 'prefix', 'fpports_', None),
    ('fpm(buf) prefix', 'prefix', 'fpm_', None),
    ('dot_stat prefix', 'prefix', 'dot_', None),
    ('doh_stat prefix', 'prefix', 'doh_', None),
    ('tcp_dca interfaces', 'exact', 'tcp_dca_interfaces', None),
    ('tcp_dca rx packets', 'exact', 'tcp_dca_rx_packets_total', None),
    ('fpc busy percent', 'exact', 'fpc_cpu_busy_percent', None),
    ('fpc cycles total', 'exact', 'fpc_cycles_total', None),
]


@pytest.mark.parametrize('desc,mode,value,extra', PARAM_CASES, ids=[c[0] for c in PARAM_CASES])
def test_metric_presence_parametrized(desc, mode, value, extra):  # noqa: D103 (descriptions via ids)
    names = by_name.keys()
    if mode == 'exact':
        assert value in names, f"Missing metric: {value}"
    elif mode == 'prefix':
        assert any(n.startswith(value) for n in names), f"Missing metric prefix: {value}"
    elif mode == 'either_exact':
        assert any(v in names for v in value), f"Missing any of expected metrics: {value}"
    else:  # pragma: no cover - defensive
        raise AssertionError(f'Unknown mode {mode}')

