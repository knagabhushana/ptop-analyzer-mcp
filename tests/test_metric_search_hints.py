"""Tests for metric_search discovery hints (TOP & SMAPS)."""
from mcp_server import mcp_app


def _collect_names(cands):
    return {c.get('metric_name') for c in cands}


def test_process_hint_present():
    # No ingestion context needed; metric_search operates over embeddings and schema only.
    out = mcp_app._metric_search_impl("process cpu usage", semantic=False)
    names = _collect_names(out['candidates'])
    assert 'top_process_stats' in names, 'Expected TOP process hint not present'


def test_smaps_memory_hint_present():
    out = mcp_app._metric_search_impl("process rss memory", semantic=False)
    names = _collect_names(out['candidates'])
    assert 'smaps_process_memory' in names, 'Expected SMAPS memory hint not present'
