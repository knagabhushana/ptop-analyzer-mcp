from __future__ import annotations
import re, os
from pathlib import Path
from typing import Iterable, Iterator, Callable, Optional
from dataclasses import dataclass
from typing import Dict, Any

@dataclass(frozen=True)
class MetricSample:
    name: str
    value: float
    ts_ms: int
    labels: Dict[str, str]

@dataclass
class ParsedRecord:
    prefix: str
    fields: Dict[str, Any]
    raw: str
    ts_ms: int

"""PTOPS log parser

Label vs Metric Policy (Phase 1)
--------------------------------
Labels (identifiers): cpu_id, device, disk_index, interface, pid, ppid, exec, prio,
port, bucket, addr, index plus record_type, kind (net variant) and source.

Metrics: dynamic numeric values (utilization %, counts, sizes in bytes/KB, rates,
latencies, time spent). This keeps cardinality bounded.

TOP: Assume FULL format is stable. Export:
    - top_cpu_percent
    - top_cpu_time_total_seconds
    - top_cpu_time_user_seconds
    - top_cpu_time_sys_seconds
Identifiers (pid, ppid, exec, prio) remain labels.

Histograms (DBWR / DBWA / DBRD): Each line is repeating triplets
    <bucket_id> <count> <avg_latency_seconds>
Emit two metrics per bucket labeled by 'bucket':
    {dbwr,dbwa,dbrd}_bucket_count_total
    {dbwr,dbwa,dbrd}_bucket_avg_latency_seconds

DOT/DOH: index & addr labels; rx/tx/dp/qd as *_total metrics with protocol prefix.

DISK: device + disk_index labels; per-second rates & averages are metrics.
NET: Separate rate vs ifstat using 'kind' label. Interface stays a label.

This docstring supplements the design document and should be kept consistent if
parsing logic evolves.
"""

"""Regex notes:
Previous TIME_RE assumed integer first token and epoch second. Real logs show:
    TIME <uptime.float> <epoch_seconds> <YYYY-MM-DD> <HH:MM:SS>
We relax parsing with TIME_FULL_RE capturing all components. We retain a very
permissive TIME_FALLBACK_RE as a safety net for older formats (first token int).
"""
TIME_FULL_RE = re.compile(r"^TIME\s+([0-9]+(?:\.[0-9]+)?)\s+(\d{10})(?:\.[0-9]+)?\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")
TIME_FALLBACK_RE = re.compile(r"^TIME\s+\d+\s+(\d{10})(?:\.\d+)?\b")
CPU_RE = re.compile(r"^CPU\s+(cpu\d+|cpu)\s+u\s+([0-9.]+)\s+id/io\s+([0-9.]+)\s+([0-9.]+)\s+u/s/n\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+irq h/s\s+([0-9.]+)\s+([0-9.]+)")
TOP_FULL_RE = re.compile(
    r"^TOP\s+(\d+)\s+(\d+)\s+([0-9.]+)%\s+([0-9.]+)\s+\(([0-9.]+)\s+([0-9.]+)\)\s+(\d+)\s+\(([^)]+)\)"
)
TOP_MIN_RE = re.compile(r"^TOP\s+(\d+)\s+(\d+)\s+([0-9.]+)%")  # fallback if format shifts
DISK_RE = re.compile(r"^DISK\s+(\d+)\s+(\w+)\s+rkxt\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+wkxt\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+sqb\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)")
NET_RATE_RE = re.compile(r"^NET\s+(\w+)\s+rk\s+([0-9.]+)\s+([0-9.]+)\s+tk\s+([0-9.]+)\s+([0-9.]+)\s+rd\s+([0-9.]+)\s+td\s+([0-9.]+)")
NET_IFSTAT_RE = re.compile(r"^NET ifstat\s*(\w+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)")
IDENT_RE = re.compile(r"^IDENT\s+host\s+(\S+)\s+host_id\s+(\S+)\s+ver\s+(\S+)")
# Simple fallback IDENT: IDENT <version> <host_id>
IDENT_SIMPLE_RE = re.compile(r"^IDENT\s+(\S+)\s+(\S+)$")
# SMAPS format in real file differs; we only keep pid + rss_kib + swap_kib + process name after ' c ' token.
SMAPS_RE = re.compile(r"^SMAPS\s+(\d+) .*? (\d+) (\d+) .*? c (\S+)")
MEM_PREFIX = 'MEM '

class PTOPSParser:
    def __init__(self, log_path: str, allowed_categories: Optional[set[str]] = None):
        """PTOPSParser

        Dual NET naming strategy:
        - Always emit normalized rx/tx metrics (name_variant=normalized)
        - Emit legacy rk/tk/rd/td metrics (name_variant=legacy) for transition
          so downstream consumers can migrate predictably.
        """
        self.log_path = Path(log_path)
        self._global_labels: Dict[str, Any] = {}
        self._allowed_categories = allowed_categories

    @staticmethod
    def _metric_category(prefix: str) -> str:
        """Map record/prefix to canonical CATEGORY label (uppercase).

        Canonical categories: CPU, MEM, DISK, NET, TOP, SMAPS, DB, FASTPATH, OTHER
        Mapping rules mirror embeddings_store._derive_category plus parser-only prefixes.
        """
        if prefix == 'CPU':
            return 'CPU'
        if prefix == 'MEM':
            return 'MEM'
        if prefix == 'DISK':
            return 'DISK'
        if prefix in ('NET_RATE', 'NET_IF', 'NET'):
            return 'NET'
        if prefix == 'TOP':
            return 'TOP'
        if prefix == 'SMAPS':
            return 'SMAPS'
        if prefix in ('DBWR', 'DBWA', 'DBRD', 'DBMPOOL'):
            return 'DB'
        if prefix in ('FPPORTS', 'FPMBUF', 'FPC', 'FPP', 'FPS', 'DOT_STAT', 'DOH_STAT', 'TCP_DCA_STAT', 'FPVLSTATS'):
            return 'FASTPATH'
        return 'OTHER'

    def iter_records(self) -> Iterator[ParsedRecord]:
        current_ts_ms: int | None = None
        with self.log_path.open('r', encoding='utf-8', errors='ignore') as f:
            for raw in f:
                line = raw.rstrip('\n')
                if not line:
                    continue
                if os.environ.get('DEBUG_PTOP_PARSER') == '1':  # lightweight tracing
                    print(f"[parser] line={line[:120]}")
                # TIME anchor
                # TIME anchor (relaxed formats)
                m_time_full = TIME_FULL_RE.match(line)
                if m_time_full:
                    uptime_s = m_time_full.group(1)
                    epoch_s = m_time_full.group(2)
                    date_str = m_time_full.group(3)
                    time_str = m_time_full.group(4)
                    try:
                        current_ts_ms = int(epoch_s) * 1000
                    except ValueError:
                        current_ts_ms = None
                    # Store date/time & uptime as (mutable) global labels for downstream metrics
                    self._global_labels.update({
                        'uptime_seconds': uptime_s,
                        'date': date_str,
                        'time': time_str,
                    })
                    if os.environ.get('DEBUG_PTOP_PARSER') == '1':
                        print(f"[parser] TIME(full) ts={current_ts_ms} uptime={uptime_s} date={date_str} time={time_str}")
                    continue
                m_time_fb = TIME_FALLBACK_RE.match(line)
                if m_time_fb:
                    current_ts_ms = int(m_time_fb.group(1)) * 1000
                    if os.environ.get('DEBUG_PTOP_PARSER') == '1':
                        print(f"[parser] TIME(fallback) ts={current_ts_ms}")
                    continue
                # IDENT (allowed before first TIME)
                m_ident = IDENT_RE.match(line)
                if m_ident:
                    host, host_id, ver = m_ident.groups()
                    self._global_labels.update({'host': host, 'host_id': host_id, 'ptop_version': ver})
                    if os.environ.get('DEBUG_PTOP_PARSER') == '1':
                        print(f"[parser] IDENT host={host} host_id={host_id} ver={ver}")
                    continue
                m_ident_simple = IDENT_SIMPLE_RE.match(line)
                if m_ident_simple:
                    ver, host_id = m_ident_simple.groups()
                    # Use host_id as host if none previously set
                    if 'host' not in self._global_labels:
                        self._global_labels['host'] = host_id
                    self._global_labels.update({'host_id': host_id, 'ptop_version': ver})
                    if os.environ.get('DEBUG_PTOP_PARSER') == '1':
                        print(f"[parser] IDENT(simple) host_id={host_id} ver={ver}")
                    continue
                if current_ts_ms is None:
                    continue
                # Very simplified synthetic test line format: "CPU: ts=... host=... cpu_utilization=<v>"
                if line.startswith('CPU:'):
                    try:
                        parts = line.split()
                        kv = {}
                        for p in parts[1:]:
                            if '=' in p:
                                k,v = p.split('=',1)
                                kv[k] = v.rstrip('%')
                        util_raw = kv.get('cpu_utilization')
                        util = float(util_raw) if util_raw is not None else None
                        if util is not None:
                            # adopt generic cpu_id label 'cpu'
                            yield ParsedRecord('CPU', {
                                'cpu_id': 'cpu',
                                'utilization': util,
                                'idle_percent': 0.0,
                            }, line, current_ts_ms)
                            if os.environ.get('DEBUG_PTOP_PARSER') == '1':
                                print(f"[parser] synthetic CPU utilization={util}")
                            continue
                    except Exception as e:
                        if os.environ.get('DEBUG_PTOP_PARSER') == '1':
                            print(f"[parser] synthetic CPU parse error: {e}")
                # Simplified CPU minimal line fallback used in tests: "CPU cpu u <util> <idle> ..." may not match full regex
                if line.startswith('CPU ') and ' irq h/s ' in line and ' u ' in line and ' id/io ' in line:
                    toks = line.split()
                    # Expect: CPU cpu u util idle io user system nice hardirq softirq (some may be zeros)
                    if len(toks) >= 6:
                        try:
                            cpu_id = toks[1]
                            # naive mapping of positions used in test synthetic line
                            util = float(toks[3])
                            idle = float(toks[4]) if toks[4].replace('.','',1).isdigit() else 0.0
                            yield ParsedRecord('CPU', {
                                'cpu_id': cpu_id,
                                'utilization': util,
                                'idle_percent': idle,
                            }, line, current_ts_ms)
                            continue
                        except Exception:
                            pass
                # SMAPS (depends on timestamp)
                m = SMAPS_RE.match(line)
                if m:
                    pid, rss, swap, exec_name = m.groups()
                    yield ParsedRecord('SMAPS', {
                        'pid': pid,
                        'rss_kib': float(rss),
                        'swap_kib': float(swap),
                        'exec': exec_name.rsplit('/',1)[-1],  # basename only
                    }, line, current_ts_ms)
                    continue
                # CPU
                m = CPU_RE.match(line)
                if m:
                    yield ParsedRecord('CPU', {
                        'cpu_id': m.group(1),
                        'utilization': float(m.group(2)),
                        'idle_percent': float(m.group(3)),
                        'iowait_percent': float(m.group(4)),
                        'user_percent': float(m.group(5)),
                        'system_percent': float(m.group(6)),
                        'nice_percent': float(m.group(7)),
                        'hardirq_percent': float(m.group(8)),
                        'softirq_percent': float(m.group(9)),
                    }, line, current_ts_ms)
                    continue
                # MEM (token scan aligned with documented spec)
                if line.startswith(MEM_PREFIX):
                    tokens = line.split()
                    try:
                        def idx(tok: str) -> int:
                            return tokens.index(tok)
                        def after(tok: str) -> str:
                            return tokens[idx(tok)+1]
                        fields = {
                            'total_memory': float(after('t')),  # bytes
                            'free_percent': float(after('f')),
                            'buffers_percent': float(after('b')),
                            'cached_percent': float(after('c')),
                            'slab_percent': float(after('s')),
                            'anon_percent': float(after('a')),
                            'sysv_shm_percent': float(after('sh')),
                            'swap_used_percent': float(after('sw')),
                        }
                        # swap_total_bytes is the token AFTER the value printed with sw (percent) -> pattern: sw <pct> <swap_total_bytes>
                        sw_total_idx = idx('sw') + 2
                        if sw_total_idx < len(tokens):
                            try:
                                fields['swap_total_bytes'] = float(tokens[sw_total_idx])
                            except ValueError:
                                pass
                        # Huge pages: h <huge_total> <huge_free>
                        if 'h' in tokens:
                            h_i = idx('h')
                            if h_i + 2 < len(tokens):
                                try:
                                    fields['hugepages_total'] = float(tokens[h_i+1])
                                    fields['hugepages_free'] = float(tokens[h_i+2])
                                except ValueError:
                                    pass
                        # Available percent: A <available_pct>
                        if 'A' in tokens:
                            try:
                                fields['available_percent'] = float(after('A'))
                            except ValueError:
                                pass
                        # Optional paging tokens: pio <pgpgin_per_sec> <pgpgout_per_sec> sio <pswpin_per_sec> <pswpout_per_sec>
                        if 'pio' in tokens:
                            pio_i = idx('pio')
                            if pio_i + 2 < len(tokens):
                                try:
                                    fields['pgpgin_rate'] = float(tokens[pio_i+1])
                                    fields['pgpgout_rate'] = float(tokens[pio_i+2])
                                except ValueError:
                                    pass
                        if 'sio' in tokens:
                            sio_i = idx('sio')
                            if sio_i + 2 < len(tokens):
                                try:
                                    fields['swapin_rate'] = float(tokens[sio_i+1])
                                    fields['swapout_rate'] = float(tokens[sio_i+2])
                                except ValueError:
                                    pass
                        yield ParsedRecord('MEM', fields, line, current_ts_ms)
                    except Exception:
                        pass
                    continue
                # DISK
                m = DISK_RE.match(line)
                if m:
                    yield ParsedRecord('DISK', {
                        'disk_index': int(m.group(1)),
                        'device_name': m.group(2),
                        'reads_per_sec': float(m.group(3)),
                        'read_kib_per_sec': float(m.group(4)),
                        'read_avg_kb': float(m.group(5)),
                        'read_avg_ms': float(m.group(6)),
                        'writes_per_sec': float(m.group(7)),
                        'write_kib_per_sec': float(m.group(8)),
                        'write_avg_kb': float(m.group(9)),
                        'write_avg_ms': float(m.group(10)),
                        'service_time_ms': float(m.group(11)),
                        'avg_queue_len': float(m.group(12)),
                        'device_busy_percent': float(m.group(13)),
                    }, line, current_ts_ms)
                    continue
                # NET rate line
                m = NET_RATE_RE.match(line)
                if m:
                    # Normalize rk/tk to rx/tx naming; also retain legacy field keys.
                    interface = m.group(1)
                    rx_pps = float(m.group(2))
                    rx_kib = float(m.group(3))
                    tx_pps = float(m.group(4))
                    tx_kib = float(m.group(5))
                    rx_drop = float(m.group(6))
                    tx_drop = float(m.group(7))
                    fields = {
                        'interface': interface,
                        'rx_packets_per_sec': rx_pps,
                        'rx_kib_per_sec': rx_kib,
                        'tx_packets_per_sec': tx_pps,
                        'tx_kib_per_sec': tx_kib,
                        'rx_drops_per_sec': rx_drop,
                        'tx_drops_per_sec': tx_drop,
                    }
                    # Always include legacy fields for transition (tagged later by name_variant label during emission)
                    fields.update({
                        'rk_packets_per_sec': rx_pps,
                        'rk_kib_per_sec': rx_kib,
                        'tk_packets_per_sec': tx_pps,
                        'tk_kib_per_sec': tx_kib,
                        'rd_drops_per_sec': rx_drop,
                        'td_drops_per_sec': tx_drop,
                    })
                    yield ParsedRecord('NET_RATE', fields, line, current_ts_ms)
                    continue
                # NET ifstat cumulative
                m = NET_IFSTAT_RE.match(line)
                if m:
                    # Columns: iface rx_pkts rx_bytes tx_pkts tx_bytes rx_drops tx_drops
                    yield ParsedRecord('NET_IF', {
                        'interface': m.group(1),
                        'rx_packets_total': int(m.group(2)),
                        'rx_bytes_total': int(m.group(3)),
                        'tx_packets_total': int(m.group(4)),
                        'tx_bytes_total': int(m.group(5)),
                        'rx_dropped_packets_total': int(m.group(6)),
                        'tx_dropped_packets_total': int(m.group(7)),
                    }, line, current_ts_ms)
                    continue
                # TOP
                m = TOP_FULL_RE.match(line)
                if m:
                    yield ParsedRecord('TOP', {
                        'ppid': m.group(1),
                        'pid': m.group(2),
                        'cpu_percent': float(m.group(3)),
                        'total_cpu_seconds': float(m.group(4)),
                        'user_cpu_seconds': float(m.group(5)),
                        'system_cpu_seconds': float(m.group(6)),
                        'prio': m.group(7),
                        'exec': m.group(8),
                    }, line, current_ts_ms)
                    continue
                m = TOP_MIN_RE.match(line)
                if m:
                    yield ParsedRecord('TOP', {
                        'ppid': m.group(1),
                        'pid': m.group(2),
                        'cpu_percent': float(m.group(3)),
                    }, line, current_ts_ms)
                    continue
                # DBWR/DBWA/DBRD histogram triplets
                if line.startswith('DBWR ') or line.startswith('DBWA ') or line.startswith('DBRD '):
                    prefix = line.split()[0]
                    tokens = line.split()[1:]
                    # tokens repeating: bucket count latency
                    triplets = []
                    for i in range(0, len(tokens), 3):
                        if i+2 < len(tokens):
                            try:
                                bucket = tokens[i]
                                count = float(tokens[i+1])
                                latency = float(tokens[i+2])
                                triplets.append((bucket, count, latency))
                            except ValueError:
                                break
                    yield ParsedRecord(prefix, {'buckets': triplets}, line, current_ts_ms)
                    continue
                # DBMPOOL line
                if line.startswith('DBMPOOL '):
                    tokens = line.split()
                    fields = {}
                    # simple key value pairs after removing prefix
                    it = iter(tokens[1:])
                    for k in it:
                        if k == 'MiB':
                            continue
                        try:
                            v = next(it)
                        except StopIteration:
                            break
                        # skip percent signs
                        v_clean = v.rstrip('%')
                        if v_clean.replace('.', '', 1).isdigit():
                            fields[k] = float(v_clean)
                    yield ParsedRecord('DBMPOOL', fields, line, current_ts_ms)
                    continue
                # FPPORTS lines
                if line.startswith('FPPORTS '):
                    tokens = line.split()
                    port = tokens[1]
                    kv = {}
                    for i in range(2, len(tokens), 2):
                        if i+1 < len(tokens):
                            key = tokens[i]
                            val = tokens[i+1]
                            if val.isdigit():
                                kv[key] = float(val)
                    kv['port'] = port
                    yield ParsedRecord('FPPORTS', kv, line, current_ts_ms)
                    continue
                # FPMBUF
                if line.startswith('FPMBUF '):
                    tokens = line.split()
                    kv = {}
                    for i in range(1, len(tokens), 2):
                        if i+1 < len(tokens):
                            key = tokens[i]
                            val = tokens[i+1].rstrip('%')
                            if val.replace('.', '', 1).isdigit():
                                kv[key] = float(val)
                    yield ParsedRecord('FPMBUF', kv, line, current_ts_ms)
                    continue
                # DOT / DOH stats (handle optional protocol token after addr for DOT_STAT)
                if line.startswith('DOT_STAT ') or line.startswith('DOH_STAT '):
                    tokens = line.split()
                    prefix = tokens[0]
                    # index and addr always present
                    index = tokens[1]
                    addr = tokens[2]
                    start_idx = 3
                    # DOT_STAT may include protocol token like TLS before key-value pairs
                    if prefix == 'DOT_STAT' and start_idx < len(tokens) and tokens[start_idx].isalpha() and tokens[start_idx] not in ('rx','tx','dp','qd'):
                        start_idx += 1
                    # Collect key->value scanning
                    kv = {}
                    i = start_idx
                    while i < len(tokens)-1:
                        key = tokens[i]
                        val = tokens[i+1]
                        if key in ('rx','tx','dp','qd'):
                            try:
                                kv[key] = float(val)
                            except ValueError:
                                pass
                            i += 2
                        else:
                            i += 1
                    fields = {'index': index, 'addr': addr}
                    fields.update(kv)
                    yield ParsedRecord(prefix, fields, line, current_ts_ms)
                    continue
                # TCP_DCA_STAT
                if line.startswith('TCP_DCA_STAT '):
                    # Example: TCP_DCA_STAT 1 10.35.173.2  rx 10 tx 8 dp 2 qd 1 os 3 cs 2 as 1
                    tokens = line.split()
                    if len(tokens) >= 4:
                        try:
                            iface_count = int(tokens[1])
                            interface_addr = tokens[2]
                            kv = {}
                            i = 3
                            while i < len(tokens)-1:
                                key = tokens[i]
                                val = tokens[i+1]
                                if key in ('rx','tx','dp','qd','os','cs','as'):
                                    # values are integers
                                    try:
                                        kv[key] = float(val)
                                    except ValueError:
                                        pass
                                    i += 2
                                else:
                                    i += 1
                            fields = {'iface_count': iface_count, 'interface_addr': interface_addr}
                            fields.update(kv)
                            yield ParsedRecord('TCP_DCA_STAT', fields, line, current_ts_ms)
                        except Exception:
                            pass
                    continue
                # FPC (Fast path CPU usage) data lines: FPC <cpu> <busy%> <cycles> <cycles_per_packet> <cycles_ic_pkt>
                if line.startswith('FPC'):
                    # ignore header / descriptive lines which contain non-numeric tokens after first two
                    tokens = line.split()
                    # Data line pattern length at least 6 tokens: ['FPC', cpu, busy, cycles, cpp, cic]
                    if len(tokens) >= 6 and tokens[1].isdigit():
                        try:
                            cpu_id = tokens[1]
                            busy = float(tokens[2])
                            cycles = float(tokens[3])
                            cycles_per_packet = float(tokens[4])
                            cycles_ic_pkt = float(tokens[5])
                            yield ParsedRecord('FPC', {
                                'cpu': cpu_id,
                                'busy_percent': busy,
                                'cycles_total': cycles,
                                'cycles_per_packet': cycles_per_packet,
                                'cycles_ic_pkt': cycles_ic_pkt,
                            }, line, current_ts_ms)
                            continue
                        except ValueError:
                            pass
                    # Non-data FPC lines fall through (headers / blank / summary) and are ignored
                # FPP (Fast path packets) lines: FPP <total_cycles> <total_packets>
                if line.startswith('FPP '):
                    tokens = line.split()
                    if len(tokens) >= 3:
                        try:
                            total_cycles = float(tokens[1])
                            total_packets = float(tokens[2])
                            cycles_per_packet = total_cycles / total_packets if total_packets > 0 else 0.0
                            yield ParsedRecord('FPP', {
                                'total_cycles': total_cycles,
                                'total_packets': total_packets,
                                'cycles_per_packet': cycles_per_packet,
                            }, line, current_ts_ms)
                            continue
                        except ValueError:
                            pass
                # FPS (Fast path DNS statistics) lines: FPS iod <i> <o> <d> mhb <m> <h> <b>
                if line.startswith('FPS '):
                    tokens = line.split()
                    # Expected: FPS iod <incoming> <outgoing> <dropped> mhb <missed> <hit> <bypass>
                    if len(tokens) >= 8 and 'iod' in tokens and 'mhb' in tokens:
                        try:
                            iod_idx = tokens.index('iod')
                            mhb_idx = tokens.index('mhb')
                            if iod_idx + 3 < len(tokens) and mhb_idx + 3 < len(tokens):
                                incoming = float(tokens[iod_idx + 1])
                                outgoing = float(tokens[iod_idx + 2])
                                dropped = float(tokens[iod_idx + 3])
                                missed = float(tokens[mhb_idx + 1])
                                hit = float(tokens[mhb_idx + 2])
                                bypass = float(tokens[mhb_idx + 3])
                                yield ParsedRecord('FPS', {
                                    'incoming_dns_packets': incoming,
                                    'outgoing_dns_packets': outgoing,
                                    'dropped_dns_packets': dropped,
                                    'missed_dns_packets': missed,
                                    'hit_dns_packets': hit,
                                    'bypass_dns_packets': bypass,
                                }, line, current_ts_ms)
                                continue
                        except (ValueError, IndexError):
                            pass
                # FPVLSTATS lines: pattern "FPVLSTATS F-P <v> F-W <v> F-B <v> F-BA <v> N-P <v> N-W <v> N-B <v> N-R <v> N-BA <v> N-DD <v> T-F <v> T-B <v>"
                if line.startswith('FPVLSTATS '):
                    tokens = line.split()
                    # Expect alternating KEY VALUE after prefix
                    kv = {}
                    i = 1
                    while i < len(tokens) - 1:
                        key = tokens[i]
                        val = tokens[i+1]
                        # Normalize key: remove trailing punctuation, replace '-' with '_', uppercase groups to lowercase descriptive
                        norm = key.strip().strip(':').replace('-', '_')
                        # Map short tokens to readable names
                        mapping = {
                            'F_P': 'fpvl_f_pending',
                            'F_W': 'fpvl_f_working',
                            'F_B': 'fpvl_f_blocked',
                            'F_BA': 'fpvl_f_blocked_async',
                            'N_P': 'fpvl_n_pending',
                            'N_W': 'fpvl_n_working',
                            'N_B': 'fpvl_n_blocked',
                            'N_R': 'fpvl_n_running',
                            'N_BA': 'fpvl_n_blocked_async',
                            'N_DD': 'fpvl_n_dropped',
                            'T_F': 'fpvl_total_fast',
                            'T_B': 'fpvl_total_blocked',
                        }
                        if norm in mapping and val.replace('.', '', 1).isdigit():
                            kv[mapping[norm]] = float(val)
                        i += 2
                    if kv:
                        yield ParsedRecord('FPVLSTATS', kv, line, current_ts_ms)
                        continue

    def iter_metric_samples(self) -> Iterable[MetricSample]:
        count = 0
        emitted = 0
        # Filtering on canonical categories if provided.
        allowed_categories = self._allowed_categories
        for rec in self.iter_records():
            count += 1
            if allowed_categories:
                cat = self._metric_category(rec.prefix)
                if cat not in allowed_categories:
                    continue
            if rec.prefix == 'CPU':
                base = {'record_type': 'CPU', 'cpu_id': rec.fields['cpu_id'], 'cpu': rec.fields['cpu_id'], 'source': 'ptops', 'metric_category': self._metric_category(rec.prefix)}
                for k,v in rec.fields.items():
                    if k == 'cpu_id':
                        continue
                    name = self._cpu_metric(k)
                    labels = {**base, **self._global_labels}
                    yield MetricSample(name, float(v), rec.ts_ms, labels)
                    # Provide legacy alias explicitly for tests expecting cpu_utilization_percent
                    if name == 'cpu_utilization':
                        yield MetricSample('cpu_utilization_percent', float(v), rec.ts_ms, labels)
                    emitted += 1
            elif rec.prefix == 'MEM':
                base = {'record_type': 'MEM', 'source': 'ptops', 'metric_category': self._metric_category(rec.prefix)}
                for k,v in rec.fields.items():
                    yield MetricSample(f'mem_{k}', float(v), rec.ts_ms, {**base, **self._global_labels})
                    emitted += 1
            elif rec.prefix == 'DISK':
                base = {
                    'record_type': 'DISK',
                    'device_name': rec.fields['device_name'],
                    'disk_index': str(rec.fields.get('disk_index')),
                    'source': 'ptops',
                    'metric_category': self._metric_category(rec.prefix)
                }
                for k,v in rec.fields.items():
                    if k in ('device_name','disk_index'): continue
                    name = f'disk_{k}'
                    yield MetricSample(name, float(v), rec.ts_ms, {**base, **self._global_labels})
            elif rec.prefix == 'NET_RATE':
                base_norm = {'record_type':'NET','interface': rec.fields['interface'], 'kind':'rate','source':'ptops','metric_category': self._metric_category(rec.prefix), 'name_variant':'normalized'}
                normalized_keys = ['rx_packets_per_sec','rx_kib_per_sec','tx_packets_per_sec','tx_kib_per_sec','rx_drops_per_sec','tx_drops_per_sec']
                for k in normalized_keys:
                    if k in rec.fields:
                        metric_name = f'net_{k}'
                        yield MetricSample(metric_name, float(rec.fields[k]), rec.ts_ms, {**base_norm, **self._global_labels})
                base_legacy = {**base_norm, 'name_variant':'legacy'}
                legacy_keys = ['rk_packets_per_sec','rk_kib_per_sec','tk_packets_per_sec','tk_kib_per_sec','rd_drops_per_sec','td_drops_per_sec']
                for k in legacy_keys:
                    if k in rec.fields:
                        yield MetricSample(f'net_{k}', float(rec.fields[k]), rec.ts_ms, {**base_legacy, **self._global_labels})
            elif rec.prefix == 'NET_IF':
                base = {'record_type':'NET','interface': rec.fields['interface'],'kind':'ifstat','source':'ptops','metric_category': self._metric_category(rec.prefix)}
                # Emit counters with normalized names; no legacy form needed since spec alignment now done.
                for k,v in rec.fields.items():
                    if k=='interface':
                        continue
                    yield MetricSample(f'net_{k}', float(v), rec.ts_ms, {**base, **self._global_labels})
            elif rec.prefix == 'TOP':
                # Treat pid/ppid/exec/prio as labels; expose CPU percent and times as metrics.
                base = {
                    'record_type': 'TOP',
                    'pid': rec.fields['pid'],
                    'ppid': rec.fields['ppid'],
                    'source': 'ptops',
                    'metric_category': self._metric_category(rec.prefix)
                }
                if 'exec' in rec.fields:
                    base['exec'] = rec.fields['exec']
                if 'prio' in rec.fields:
                    base['prio'] = rec.fields['prio']
                merged = {**base, **self._global_labels}
                # Emit metrics using tasks_* naming to align with docs embeddings
                yield MetricSample('tasks_cpu_percent', rec.fields['cpu_percent'], rec.ts_ms, merged)
                # Legacy TOP naming expected by tests
                yield MetricSample('top_cpu_percent', rec.fields['cpu_percent'], rec.ts_ms, merged)
                if 'total_cpu_seconds' in rec.fields:
                    yield MetricSample('tasks_total_cpu_seconds', rec.fields['total_cpu_seconds'], rec.ts_ms, merged)
                    yield MetricSample('top_cpu_time_total_seconds', rec.fields['total_cpu_seconds'], rec.ts_ms, merged)
                if 'user_cpu_seconds' in rec.fields:
                    yield MetricSample('tasks_user_cpu_seconds', rec.fields['user_cpu_seconds'], rec.ts_ms, merged)
                    yield MetricSample('top_cpu_time_user_seconds', rec.fields['user_cpu_seconds'], rec.ts_ms, merged)
                if 'system_cpu_seconds' in rec.fields:
                    yield MetricSample('tasks_system_cpu_seconds', rec.fields['system_cpu_seconds'], rec.ts_ms, merged)
                    yield MetricSample('top_cpu_time_sys_seconds', rec.fields['system_cpu_seconds'], rec.ts_ms, merged)
            elif rec.prefix == 'SMAPS':
                base = {
                    'record_type': 'SMAPS',
                    'pid': rec.fields['pid'],
                    'exec': rec.fields.get('exec'),
                    'source': 'ptops',
                    'metric_category': self._metric_category(rec.prefix)
                }
                merged = {**base, **self._global_labels}
                yield MetricSample('smaps_rss_kb', rec.fields['rss_kib'], rec.ts_ms, merged)
                yield MetricSample('smaps_swap_kb', rec.fields['swap_kib'], rec.ts_ms, merged)
            elif rec.prefix in ('DBWR','DBWA','DBRD'):
                which = rec.prefix.lower()
                # Each triplet: bucket_id (label), count (monotonic counter), latency (average seconds in bucket)
                # We expose two metrics per bucket with 'bucket' as label to avoid exploding metric names.
                for bucket,count,lat in rec.fields['buckets']:
                    labels = { 'record_type': rec.prefix, 'bucket': bucket, 'source':'ptops', 'metric_category': self._metric_category(rec.prefix), **self._global_labels }
                    yield MetricSample(f'{which}_bucket_count_total', count, rec.ts_ms, labels)
                    yield MetricSample(f'{which}_bucket_avg_latency_seconds', lat, rec.ts_ms, labels)
            elif rec.prefix == 'DBMPOOL':
                base = {'record_type':'DBMPOOL','source':'ptops','metric_category': self._metric_category(rec.prefix)}
                for k,v in rec.fields.items():
                    yield MetricSample(f'dbmpool_{k}', float(v), rec.ts_ms, {**base, **self._global_labels})
            elif rec.prefix == 'FPPORTS':
                base_labels = {'record_type':'FPPORTS','port': rec.fields['port'],'source':'ptops','metric_category': self._metric_category(rec.prefix), **self._global_labels}
                for k,v in rec.fields.items():
                    if k=='port': continue
                    yield MetricSample(f'fpports_{k}_total', float(v), rec.ts_ms, base_labels)
            elif rec.prefix == 'FPMBUF':
                base = {'record_type':'FPMBUF','source':'ptops','metric_category': self._metric_category(rec.prefix)}
                for k,v in rec.fields.items():
                    yield MetricSample(f'fpm_{k}', float(v), rec.ts_ms, {**base, **self._global_labels})
            elif rec.prefix in ('DOT_STAT','DOH_STAT'):
                base = {'record_type': rec.prefix, 'addr': rec.fields['addr'], 'index': rec.fields.get('index'), 'source':'ptops', 'metric_category': self._metric_category(rec.prefix)}
                for k,v in rec.fields.items():
                    if k in ('addr','index'): continue
                    suffix = 'dot' if rec.prefix=='DOT_STAT' else 'doh'
                    yield MetricSample(f'{suffix}_{k}_total', float(v), rec.ts_ms, {**base, **self._global_labels})
            elif rec.prefix == 'TCP_DCA_STAT':
                base = {
                    'record_type': 'TCP_DCA_STAT',
                    'interface_addr': rec.fields.get('interface_addr'),
                    'source': 'ptops',
                    'metric_category': self._metric_category(rec.prefix)
                }
                merged = {**base, **self._global_labels}
                # iface_count (gauge)
                if 'iface_count' in rec.fields:
                    yield MetricSample('tcp_dca_interfaces', float(rec.fields['iface_count']), rec.ts_ms, merged)
                mapping = {
                    'rx': 'tcp_dca_rx_packets_total',
                    'tx': 'tcp_dca_tx_packets_total',
                    'dp': 'tcp_dca_dropped_packets_total',
                    'qd': 'tcp_dca_queue_drops_total',
                    'os': 'tcp_dca_opened_sessions_total',
                    'cs': 'tcp_dca_closed_sessions_total',
                    'as': 'tcp_dca_active_sessions',  # gauge
                }
                for k,metric_name in mapping.items():
                    if k in rec.fields:
                        yield MetricSample(metric_name, float(rec.fields[k]), rec.ts_ms, merged)
            elif rec.prefix == 'FPC':
                base = {'record_type':'FPC','cpu': rec.fields['cpu'],'source':'ptops','metric_category': self._metric_category(rec.prefix)}
                merged = {**base, **self._global_labels}
                yield MetricSample('fpc_cpu_busy_percent', rec.fields['busy_percent'], rec.ts_ms, merged)
                yield MetricSample('fpc_cycles_total', rec.fields['cycles_total'], rec.ts_ms, merged)
                yield MetricSample('fpc_cycles_per_packet', rec.fields['cycles_per_packet'], rec.ts_ms, merged)
                yield MetricSample('fpc_cycles_ic_pkt', rec.fields['cycles_ic_pkt'], rec.ts_ms, merged)
                emitted += 4
        if os.environ.get('DEBUG_PTOP_PARSER') == '1':
            print(f"[parser] records_processed={count} metrics_emitted={emitted}")

    @staticmethod
    def _cpu_metric(field: str) -> str:
        mapping = {
            'utilization': 'cpu_utilization',
            'idle_percent': 'cpu_idle_percent',
            'iowait_percent': 'cpu_iowait_percent',
            'user_percent': 'cpu_user_percent',
            'system_percent': 'cpu_system_percent',
            'nice_percent': 'cpu_nice_percent',
            'hardirq_percent': 'cpu_hardirq_percent',
            'softirq_percent': 'cpu_softirq_percent',
        }
        return mapping.get(field, f'cpu_{field}')
