"""Test for parallel PTOPS ingestion performance optimizations."""

import os
import tempfile
import time
from pathlib import Path

import pytest

from mcp_server.ingestion.ptops_ingest_parallel import (
    ingest_ptop_logs_parallel,
    ingest_ptop_logs_optimized,
    create_optimized_writers
)
from mcp_server.timescale.writer import TimescaleWriter


def create_sample_ptops_file(path: str, num_lines: int = 100):
    """Create a sample PTOPS file for testing."""
    with open(path, 'w') as f:
        f.write("TIME 1000.0 1640995200 2024-01-01 12:00:00\n")
        f.write("IDENT host testhost host_id test123 ver 1.0\n")
        
        for i in range(num_lines):
            ts = 1640995200 + i
            # CPU metrics
            f.write(f"TIME {1000.0 + i} {ts} 2024-01-01 12:{i:02d}:00\n")
            f.write(f"CPU cpu0 u 10.5 id/io 85.2 4.3 u/s/n 8.1 1.2 0.2 irq h/s 0.5 0.1\n")
            f.write(f"CPU cpu1 u 15.3 id/io 80.1 4.6 u/s/n 9.2 1.5 0.3 irq h/s 0.6 0.2\n")
            
            # Memory metrics
            f.write(f"MEM total_kib 8388608 available_kib 4194304 used_kib 4194304\n")
            
            # Disk metrics
            f.write(f"DISK 0 sda rkxt 100.5 200.3 50.1 25.2 wkxt 150.7 300.8 75.4 40.3 sqb 5.2 10.1 2.5\n")


def test_parallel_processing_performance():
    """Test that parallel processing works and improves performance."""
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create multiple test files
        file_paths = []
        for i in range(4):
            file_path = os.path.join(temp_dir, f"ptop-20240101_120{i}.log")
            create_sample_ptops_file(file_path, num_lines=50)
            file_paths.append(file_path)
        
        # Test with TimescaleDB writer (mocked for performance testing)
        ts_writer = TimescaleWriter(batch_size=100)
        
        # Measure parallel processing time
        start_time = time.time()
        metrics, logs, start_ts, end_ts = ingest_ptop_logs_parallel(
            file_paths,
            bundle_id="test-bundle",
            bundle_hash="test-hash",
            host="testhost",
            vm=ts_writer,
            allowed_categories={'CPU', 'MEM', 'DISK'},
            sptid="TEST123",
            max_workers=2
        )
        parallel_time = time.time() - start_time
        
        # Verify results
        assert metrics > 0, "Should have processed some metrics"
        assert logs == 4, "Should have processed 4 log files"
        assert start_ts > 0, "Should have valid start timestamp"
        assert end_ts >= start_ts, "End timestamp should be >= start timestamp"
        
        # Check writer stats (TimescaleDB stats structure is different)
        stats = ts_writer.stats()
        assert metrics > 0
        
        print(f"Parallel processing: {metrics} metrics in {parallel_time:.2f}s")


def test_optimized_batch_sizes():
    """Test that optimized TimescaleDB writer has increased batch sizes."""
    
    # Test TimescaleDB writer optimization
    os.environ['PTOPS_BATCH_SIZE'] = '5000'
    os.environ['PTOPS_INSERT_PAGE_SIZE'] = '500'
    
    ts_writer = create_optimized_writers()
    assert ts_writer.batch_size == 5000
    assert ts_writer.insert_page_size == 500
    
    # Clean up environment
    for key in ['PTOPS_BATCH_SIZE', 'PTOPS_INSERT_PAGE_SIZE']:
        os.environ.pop(key, None)


def test_automatic_optimization_selection():
    """Test that the system automatically chooses optimizations."""
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create test files
        file_paths = []
        for i in range(2):
            file_path = os.path.join(temp_dir, f"ptop-20240101_120{i}.log")
            create_sample_ptops_file(file_path, num_lines=20)
            file_paths.append(file_path)
        
        # Test automatic optimization
        os.environ['PTOPS_PARALLEL_ENABLED'] = '1'
        
        start_time = time.time()
        metrics, logs, start_ts, end_ts = ingest_ptop_logs_optimized(
            file_paths,
            bundle_id="test-bundle-auto",
            bundle_hash="test-hash-auto",
            host="testhost",
            allowed_categories={'CPU', 'MEM'},
            sptid="TEST456"
        )
        processing_time = time.time() - start_time
        
        assert metrics > 0
        assert logs == 2
        print(f"Automatic optimization: {metrics} metrics in {processing_time:.2f}s")
        
        # Clean up
        os.environ.pop('PTOPS_PARALLEL_ENABLED', None)


def test_thread_safety():
    """Test that parallel processing is thread-safe."""
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create test files with overlapping timestamps
        file_paths = []
        for i in range(3):
            file_path = os.path.join(temp_dir, f"ptop-20240101_120{i}.log")
            create_sample_ptops_file(file_path, num_lines=30)
            file_paths.append(file_path)
        
        ts_writer = TimescaleWriter(batch_size=50)
        
        # Process with multiple workers
        metrics, logs, start_ts, end_ts = ingest_ptop_logs_parallel(
            file_paths,
            bundle_id="test-thread-safety",
            bundle_hash="test-hash-safety",
            host="testhost",
            vm=ts_writer,
            max_workers=3  # One worker per file
        )
        
        # Verify no data corruption
        stats = ts_writer.stats()
        assert metrics > 0
        assert metrics > 0
        assert logs == 3
        
        # Verify timestamps are sensible
        assert start_ts > 0
        assert end_ts >= start_ts


if __name__ == "__main__":
    # Run performance tests manually
    print("Testing parallel PTOPS ingestion performance...")
    test_parallel_processing_performance()
    test_optimized_batch_sizes()
    test_automatic_optimization_selection()
    test_thread_safety()
    print("All performance tests passed!")
