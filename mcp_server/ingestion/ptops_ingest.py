import os, re, datetime, time
from typing import List, Tuple, Optional, Set

from .parser import PTOPSParser, MetricSample
from ..debug_util import dbg

PTOP_LOG_PATTERN = re.compile(r"ptop-(\d{8})_(\d{4})\.log$")

DEFAULT_MAX_FILES = 1  # user request: limit default processed PTOPS log files to 1 to reduce load / memory

def discover_ptop_logs(root: str, max_files: int = DEFAULT_MAX_FILES) -> Tuple[List[str], List[str]]:
    dbg(f'discover_ptop_logs start root={root} max_files={max_files}')
    """Discover PTOPS logs under extracted support bundle root.

    We expect logs in <root>/var/log/ named ptop-YYYYMMDD_HHMM.log .
    Ordering strategy:
      * Collect all matching files.
      * Parse datetime from filename; skip if invalid.
      * Sort descending (latest first) by timestamp.
      * Select up to max_files (clamped >=1) and then return them in chronological order (oldest -> newest)
        for ingestion so that time increases monotonically.
    Returns (selected_files_chronological, warnings)
    """
    warnings: List[str] = []
    log_dir = os.path.join(root, 'var', 'log')
    if not os.path.isdir(log_dir):
        dbg(f'discover_ptop_logs missing log_dir={log_dir}')
        return [], ['log_dir_missing']
    candidates: List[Tuple[int,str]] = []
    for name in os.listdir(log_dir):
        m = PTOP_LOG_PATTERN.match(name)
        if not m:
            continue
        full = os.path.join(log_dir, name)
        try:
            dt = datetime.datetime.strptime(m.group(1)+m.group(2), "%Y%m%d%H%M")
            candidates.append((int(dt.timestamp()), full))
        except Exception:
            warnings.append(f'bad_filename_datetime:{name}')
    if not candidates:
        dbg(f'discover_ptop_logs no candidates in {log_dir}')
        return [], ['no_ptop_logs'] + warnings
    candidates.sort(reverse=True)
    if max_files < 1:
        max_files = 1
        warnings.append('max_files_clamped_min1')
    original_requested = max_files
    if len(candidates) > max_files:
        warnings.append('max_files_truncated')
        candidates = candidates[:max_files]
    # chronological order for ingestion
    selected = [p for _,p in sorted(candidates)]
    # encode meta warning for counts
    warnings.append(f'selected_{len(selected)}_of_{len(candidates)}_candidates_requested_{original_requested}')
    dbg(f'discover_ptop_logs selected={selected} warnings={warnings}')
    return selected, warnings


def ingest_ptop_logs(log_paths: List[str], bundle_id: str, bundle_hash: str, host: Optional[str], writer, allowed_categories: Optional[Set[str]] = None, sptid: Optional[str] = None) -> Tuple[int,int,int,int]:
    """Ingest selected PTOPS logs with TimescaleDB writer.

    Args:
        log_paths: List of PTOPS log file paths to process
        bundle_id: Bundle identifier for scoping
        bundle_hash: Bundle hash for identification
        host: Host identifier (optional)
        writer: TimescaleWriter instance for database storage
        allowed_categories: Set of allowed metric categories (e.g., {'CPU', 'MEM'})
        sptid: Support ticket ID (optional, informational)
    
    Returns:
        Tuple of (metrics_ingested, logs_processed, start_ts_ms, end_ts_ms)
    """
    metrics = 0
    start_ts: Optional[int] = None
    end_ts: Optional[int] = None
    logs_processed = 0
    global_labels = {
        'bundle_id': bundle_id,
        'bundle_hash': bundle_hash,
        'source': 'ptops'
    }
    # Add sptid if provided
    if sptid:
        global_labels['sptid'] = sptid
    if host:
        global_labels['host'] = host
    
    dbg(f'ingest_ptop_logs start bundle={bundle_id} sptid={sptid} paths={len(log_paths)}')
    
    for path in log_paths:
        file_metric_count = 0
        earliest = None
        latest = None
        try:
            if not os.path.isfile(path):
                dbg(f'ingest_ptop_logs skip_missing path={path}')
                continue
            size = os.path.getsize(path)
            dbg(f'ingest_ptop_logs parsing path={path} size={size}')
            parser = PTOPSParser(path, allowed_categories=allowed_categories)
            for sample in parser.iter_metric_samples():
                sample.labels.update(global_labels)
                metrics += 1
                file_metric_count += 1
                if start_ts is None or sample.ts_ms < start_ts:
                    start_ts = sample.ts_ms
                if end_ts is None or sample.ts_ms > end_ts:
                    end_ts = sample.ts_ms
                if earliest is None or sample.ts_ms < earliest:
                    earliest = sample.ts_ms
                if latest is None or sample.ts_ms > latest:
                    latest = sample.ts_ms
                writer.add(sample)
            logs_processed += 1
            dbg(f'ingest_ptop_logs file_done path={path} metrics={file_metric_count} ts_range={[earliest, latest]}')
            
            # Diagnostic preview for files with no metrics
            if file_metric_count == 0:
                try:
                    with open(path,'r',encoding='utf-8',errors='ignore') as fh:
                        preview_lines = []
                        for line in fh:
                            line = line.strip('\n')
                            if not line:
                                continue
                            preview_lines.append(line[:160])
                            if len(preview_lines) >= 3:
                                break
                    dbg(f'ingest_ptop_logs file_empty_metrics_preview path={path} preview={preview_lines}')
                except Exception:
                    pass
        except FileNotFoundError:
            dbg(f'ingest_ptop_logs missing_file path={path}')
            continue
        except Exception as e:
            dbg(f'ingest_ptop_logs error path={path} err={e.__class__.__name__}:{e}')
    
    # Flush any remaining data to database
    writer.flush()
    writer_stats = writer.stats()
    
    dbg(f'ingest_ptop_logs done bundle={bundle_id} sptid={sptid} metrics={metrics} logs={logs_processed} start={start_ts} end={end_ts} writer_stats={writer_stats}')
    
    # Set default timestamps if no data was processed
    now_ms = int(time.time() * 1000)
    if start_ts is None:
        start_ts = now_ms
    if end_ts is None:
        end_ts = now_ms
    
    return metrics, logs_processed, start_ts, end_ts
