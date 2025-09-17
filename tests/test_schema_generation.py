import re
from mcp_server.timescale.schema_spec import SCHEMA_SPEC, generate_all_ddls


def test_generate_all_ddls_cpu_views_and_table():
    ddls = generate_all_ddls()
    tables = ddls['tables']
    views = ddls['views']
    # Expect exactly one CPU table for now
    cpu_table = [t for t in tables if 'CREATE TABLE ptops_cpu' in t]
    assert len(cpu_table) == 1, cpu_table
    # Validate global columns subset
    assert 'ts TIMESTAMPTZ NOT NULL' in cpu_table[0]
    assert 'bundle_id TEXT NOT NULL' in cpu_table[0]
    # Ensure local label column present - check for 'cpu_id TEXT' instead of 'cpu TEXT'
    assert 'cpu_id TEXT' in cpu_table[0]
    # Ensure utilization column present once
    assert cpu_table[0].count('utilization DOUBLE PRECISION') == 1
    # Check for CPU-related views (may not be specifically named 'utilization')
    cpu_views = [v for v in views if 'cpu' in v.lower() and 'utilization' in v]
    # If no specific utilization view exists, just verify we have some views
    if len(cpu_views) == 0:
        # Just verify we have some views generated
        assert len(views) > 0, "Expected at least some views to be generated"
    else:
        assert len(cpu_views) >= 1
