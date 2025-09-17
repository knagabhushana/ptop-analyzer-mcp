"""Optimized parallel PTOPS ingestion with improved performance.

This module provides a drop-in replacement for the sequential ptops_ingest
with parallel file processing and optimized batch sizes.
"""

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Dict, Any, Optional, Set

from .ptops_ingest import (
    discover_ptop_logs, DEFAULT_MAX_FILES
)
from .parser import PTOPSParser, MetricSample
from ..debug_util import dbg


def ingest_ptop_logs_parallel(
    log_paths: List[str], 
    bundle_id: str, 
    bundle_hash: str, 
    host: Optional[str], 
    vm,  # Writer instance (TimescaleWriter)
    allowed_categories: Optional[Set[str]] = None, 
    sptid: Optional[str] = None,
    max_workers: int = None
) -> Tuple[int, int, int, int]:
    """Parallel version of ingest_ptop_logs for improved performance.
    
    Args:
        log_paths: List of PTOPS log file paths
        bundle_id: Bundle identifier
        bundle_hash: Bundle hash
        host: Host identifier
        vm: Writer instance (TimescaleWriter)
        allowed_categories: Set of allowed metric categories
        sptid: Support ticket ID
        max_workers: Number of parallel workers (default: min(4, len(log_paths)))
    
    Returns:
        Tuple of (metrics_ingested, logs_processed, start_ts_ms, end_ts_ms)
    """
    
    if not log_paths:
        return 0, 0, int(time.time() * 1000), int(time.time() * 1000)
    
    # Determine optimal number of workers
    if max_workers is None:
        max_workers = min(4, len(log_paths), os.cpu_count() or 1)
    
    # Thread-safe writer access
    writer_lock = threading.Lock()
    
    # Global labels to be added to all samples
    global_labels = {
        'bundle_id': bundle_id,
        'bundle_hash': bundle_hash,
        'source': 'ptops'
    }
    if sptid:
        global_labels['sptid'] = sptid
    if host:
        global_labels['host'] = host
    
    dbg(f'ingest_ptop_logs_parallel start bundle={bundle_id} sptid={sptid} paths={len(log_paths)} workers={max_workers}')
    
    def process_single_file(path: str) -> Tuple[int, Optional[int], Optional[int], str, List[str]]:
        """Process a single PTOPS file and return metrics and timing info."""
        local_metrics = 0
        local_start_ts = None
        local_end_ts = None
        warnings = []
        
        try:
            if not os.path.isfile(path):
                warnings.append(f'file_missing:{path}')
                return 0, None, None, path, warnings
            
            file_size = os.path.getsize(path)
            dbg(f'parallel_processing path={path} size={file_size} worker={threading.current_thread().name}')
            
            # Create parser for this file
            parser = PTOPSParser(path, allowed_categories=allowed_categories)
            
            # Batch samples before sending to writer to reduce lock contention
            batch = []
            batch_size = 500  # Smaller batches for better parallelism
            
            for sample in parser.iter_metric_samples():
                # Add global labels to sample
                sample.labels.update(global_labels)
                local_metrics += 1
                
                # Track time range
                if local_start_ts is None or sample.ts_ms < local_start_ts:
                    local_start_ts = sample.ts_ms
                if local_end_ts is None or sample.ts_ms > local_end_ts:
                    local_end_ts = sample.ts_ms
                
                batch.append(sample)
                
                # Send batch to writer when full
                if len(batch) >= batch_size:
                    with writer_lock:
                        for s in batch:
                            vm.add(s)
                    batch.clear()
            
            # Send remaining samples
            if batch:
                with writer_lock:
                    for s in batch:
                        vm.add(s)
            
            dbg(f'parallel_file_done path={path} metrics={local_metrics} ts_range=[{local_start_ts}, {local_end_ts}]')
            
            # Show preview if no metrics found
            if local_metrics == 0:
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                        preview_lines = []
                        for line in fh:
                            line = line.strip()
                            if line:
                                preview_lines.append(line[:160])
                                if len(preview_lines) >= 3:
                                    break
                    warnings.append(f'empty_file_preview:{preview_lines[:2]}')
                except Exception:
                    warnings.append('empty_file_no_preview')
            
            return local_metrics, local_start_ts, local_end_ts, path, warnings
            
        except Exception as e:
            error_msg = f'{e.__class__.__name__}:{e}'
            warnings.append(f'processing_error:{error_msg}')
            dbg(f'parallel_file_error path={path} err={error_msg}')
            return 0, None, None, path, warnings
    
    # Process files in parallel
    total_metrics = 0
    global_start_ts = None
    global_end_ts = None
    logs_processed = 0
    all_warnings = []
    
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="PTOPSWorker") as executor:
        # Submit all files for processing
        future_to_path = {executor.submit(process_single_file, path): path for path in log_paths}
        
        # Collect results as they complete
        for future in as_completed(future_to_path):
            try:
                metrics, start_ts, end_ts, path, warnings = future.result()
                
                total_metrics += metrics
                logs_processed += 1
                all_warnings.extend(warnings)
                
                # Update global time range
                if start_ts and (global_start_ts is None or start_ts < global_start_ts):
                    global_start_ts = start_ts
                if end_ts and (global_end_ts is None or end_ts > global_end_ts):
                    global_end_ts = end_ts
                
                dbg(f'parallel_completed file={logs_processed}/{len(log_paths)} path={path} metrics={metrics}')
                
            except Exception as e:
                path = future_to_path[future]
                error_msg = f'{e.__class__.__name__}:{e}'
                all_warnings.append(f'future_error:{path}:{error_msg}')
                dbg(f'parallel_future_error path={path} err={error_msg}')
    
    # Final flush to ensure all data is written
    with writer_lock:
        vm.flush()
    
    processing_time = time.time() - start_time
    
    # Get writer stats
    vm_stats = vm.stats()
    
    # Set default timestamps if no data was processed
    now_ms = int(time.time() * 1000)
    if global_start_ts is None:
        global_start_ts = now_ms
    if global_end_ts is None:
        global_end_ts = now_ms
    
    dbg(f'ingest_ptop_logs_parallel done bundle={bundle_id} sptid={sptid} '
        f'metrics={total_metrics} logs={logs_processed} workers={max_workers} '
        f'time={processing_time:.2f}s start={global_start_ts} end={global_end_ts} '
        f'timescale_stats={vm_stats} warnings={len(all_warnings)}')
    
    return total_metrics, logs_processed, global_start_ts, global_end_ts


def create_optimized_writers() -> 'TimescaleWriter':
    """Create optimized TimescaleDB writer instance with improved batch sizes.
    
    Returns:
        TimescaleWriter with optimized settings
    """
    from ..timescale.writer import TimescaleWriter
    
    # Check if COPY command should be used
    use_copy = os.environ.get('PTOPS_USE_COPY_COMMAND', '').lower() in ('true', '1', 'yes')
    
    # Optimized TimescaleWriter with larger batches
    ts_writer = TimescaleWriter(
        batch_size=int(os.environ.get('PTOPS_BATCH_SIZE', '8000')),  # Increased from 2000
        insert_page_size=int(os.environ.get('PTOPS_INSERT_PAGE_SIZE', '800')),  # Increased from 200
        use_copy=use_copy
    )
    return ts_writer


def ingest_ptop_logs_optimized(
    log_paths: List[str], 
    bundle_id: str, 
    bundle_hash: str, 
    host: Optional[str], 
    vm=None,  # Legacy parameter, will be replaced with TimescaleWriter
    allowed_categories: Optional[Set[str]] = None, 
    sptid: Optional[str] = None
) -> Tuple[int, int, int, int]:
    """Drop-in replacement for ingest_ptop_logs with automatic optimization.
    
    This function automatically chooses between parallel and sequential processing
    based on the number of files and system capabilities. Uses TimescaleDB only.
    """
    
    # Determine if we should use parallel processing
    use_parallel = (
        len(log_paths) > 1 and  # Multiple files
        int(os.environ.get('PTOPS_PARALLEL_ENABLED', '1')) and  # Not disabled
        (os.cpu_count() or 1) > 1  # Multi-core system
    )
    
    # Create optimized TimescaleDB writer if none provided
    if vm is None:
        vm = create_optimized_writers()
    
    if use_parallel:
        max_workers = int(os.environ.get('PTOPS_MAX_WORKERS', '4'))
        return ingest_ptop_logs_parallel(
            log_paths, bundle_id, bundle_hash, host, vm, 
            allowed_categories, sptid, max_workers
        )
    else:
        # Fall back to original sequential implementation
        from .ptops_ingest import ingest_ptop_logs
        return ingest_ptop_logs(
            log_paths, bundle_id, bundle_hash, host, vm,
            allowed_categories, sptid
        )


# Export the optimized version as the default
__all__ = [
    'ingest_ptop_logs_optimized',
    'ingest_ptop_logs_parallel', 
    'create_optimized_writers'
]
