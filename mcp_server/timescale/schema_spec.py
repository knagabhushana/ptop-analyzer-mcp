"""Declarative schema specification and DDL generation for Timescale migration.

This module intentionally keeps a very small surface area first; we grow it as tests
add requirements. The goal is deterministic DDL text output we can snapshot-test.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Iterable

GLOBAL_COLUMNS = [
    ("ts", "TIMESTAMPTZ NOT NULL"),
    ("bundle_id", "TEXT NOT NULL"),
    ("sptid", "TEXT"),
    ("metric_category", "TEXT NOT NULL"),
    ("host", "TEXT NOT NULL"),
]

@dataclass(frozen=True)
class Metric:
    kind: str  # gauge|counter|histogram_bucket etc.
    unit: Optional[str] = None
    description: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    column: Optional[str] = None  # explicit column name if different from metric name

@dataclass(frozen=True)
class TableGroup:
    table: str
    category: str
    local_labels: List[str]
    metrics: Dict[str, Metric]
    # Optional uniqueness (enforced via unique index) over columns; caller must include 'ts'
    unique_key: List[str] = field(default_factory=list)
    # Additional secondary indexes (list of column lists) for performance
    indexes: List[List[str]] = field(default_factory=list)

SCHEMA_SPEC: Dict[str, TableGroup] = {
    # CPU: Align column names with parser-emitted metric names (cpu_*) so a single log line maps to one row.
    # Legacy previously created columns (utilization,user_percent,system_percent) may still exist; they are ignored
    # going forward but left in place to avoid destructive migrations. The writer will dynamically ADD any missing
    # new columns when first seen.
    "CPU": TableGroup(
        table="ptops_cpu",
        category="cpu",
        local_labels=["cpu_id"],
        metrics={
            # Canonical utilization (retain legacy alias emitted by parser)
            "cpu_utilization": Metric(kind="gauge", unit="percent", description="Overall CPU utilization", aliases=["cpu_utilization_percent","utilization"]),
            "cpu_idle_percent": Metric(kind="gauge", unit="percent", description="CPU idle percent"),
            "cpu_iowait_percent": Metric(kind="gauge", unit="percent", description="CPU iowait percent"),
            "cpu_user_percent": Metric(kind="gauge", unit="percent", description="CPU user time percent"),
            "cpu_system_percent": Metric(kind="gauge", unit="percent", description="CPU system time percent"),
            "cpu_nice_percent": Metric(kind="gauge", unit="percent", description="CPU nice time percent"),
            "cpu_hardirq_percent": Metric(kind="gauge", unit="percent", description="CPU hard IRQ time percent"),
            "cpu_softirq_percent": Metric(kind="gauge", unit="percent", description="CPU soft IRQ time percent"),
        },
    ),
    # Per-process CPU usage (TOP). We store both canonical "tasks_*" and legacy "top_*" forms; canonical names own the
    # column, legacy forms are exposed as alias metrics so ingestion collapsing avoids duplicate overwrite.
    "TOP": TableGroup(
        table="ptops_top",
        category="top",
        local_labels=["pid","ppid","exec","prio"],  # exec/prio may be NULL for some rows
        metrics={
            # Percent CPU over interval
            "tasks_cpu_percent": Metric(kind="gauge", unit="percent", description="Per-process CPU percent over sample interval", aliases=["top_cpu_percent"]),
            # Cumulative CPU seconds (total, user, system) – monotonic counters but stored as gauge for now.
            "tasks_total_cpu_seconds": Metric(kind="counter", unit="seconds", description="Per-process accumulated total CPU time (user+system) seconds", aliases=["top_cpu_time_total_seconds"]),
            "tasks_user_cpu_seconds": Metric(kind="counter", unit="seconds", description="Per-process accumulated user CPU time seconds", aliases=["top_cpu_time_user_seconds"]),
            "tasks_system_cpu_seconds": Metric(kind="counter", unit="seconds", description="Per-process accumulated system CPU time seconds", aliases=["top_cpu_time_sys_seconds"]),
        },
        # Uniqueness: one row per ts,bundle,host,pid (ppid/exec/prio may fluctuate or be NULL; exclude to avoid NULL uniqueness gaps)
        unique_key=["ts","bundle_id","host","pid"],
        indexes=[
            ["pid","ts DESC"],
            ["host","ts DESC"],
        ]
    ),
    # Per-process memory (SMAPS). rss + swap (kB). Labels limited to pid & exec for cardinality control.
    "SMAPS": TableGroup(
        table="ptops_smaps",
        category="smaps",
        local_labels=["pid","exec"],
        metrics={
            "smaps_rss_kb": Metric(kind="gauge", unit="kB", description="Per-process resident set size (kB)"),
            "smaps_swap_kb": Metric(kind="gauge", unit="kB", description="Per-process swap usage (kB)"),
        },
        unique_key=["ts","bundle_id","host","pid"],
        indexes=[["pid","ts DESC"]]
    ),
    # Memory metrics (single row per host per timestamp). Parser emits dynamic fields mapped to mem_* names.
    "MEM": TableGroup(
        table="ptops_mem",
        category="mem",
        local_labels=[],
        metrics={
            "mem_total_memory": Metric(kind="gauge", unit="bytes", description="Total system memory bytes"),
            "mem_free_percent": Metric(kind="gauge", unit="percent", description="Free memory percent"),
            "mem_buffers_percent": Metric(kind="gauge", unit="percent", description="Buffers percent"),
            "mem_cached_percent": Metric(kind="gauge", unit="percent", description="Cached memory percent"),
            "mem_slab_percent": Metric(kind="gauge", unit="percent", description="Slab percent"),
            "mem_anon_percent": Metric(kind="gauge", unit="percent", description="Anonymous memory percent"),
            "mem_sysv_shm_percent": Metric(kind="gauge", unit="percent", description="SYSV shared memory percent"),
            "mem_swap_used_percent": Metric(kind="gauge", unit="percent", description="Swap used percent"),
            "mem_swap_total_bytes": Metric(kind="gauge", unit="bytes", description="Total swap space bytes"),
            "mem_hugepages_total": Metric(kind="gauge", unit="count", description="Huge pages total"),
            "mem_hugepages_free": Metric(kind="gauge", unit="count", description="Huge pages free"),
            "mem_available_percent": Metric(kind="gauge", unit="percent", description="Available memory percent"),
            "mem_pgpgin_rate": Metric(kind="gauge", unit="pages_per_sec", description="Page in rate"),
            "mem_pgpgout_rate": Metric(kind="gauge", unit="pages_per_sec", description="Page out rate"),
            "mem_swapin_rate": Metric(kind="gauge", unit="pages_per_sec", description="Swap in rate"),
            "mem_swapout_rate": Metric(kind="gauge", unit="pages_per_sec", description="Swap out rate"),
        },
        unique_key=["ts","bundle_id","host"],
        indexes=[["host","ts DESC"]]
    ),
    # Per-disk metrics. Parser labels: device_name, disk_index. Unique row per ts,bundle,host,device_name.
    "DISK": TableGroup(
        table="ptops_disk",
        category="disk",
        local_labels=["device_name","disk_index"],
        metrics={
            # We don't enumerate every possible disk_* metric (dynamic); define known common ones for early schema.
            "disk_reads_per_sec": Metric(kind="gauge", unit="ops_per_sec", description="Disk read operations per second"),
            "disk_writes_per_sec": Metric(kind="gauge", unit="ops_per_sec", description="Disk write operations per second"),
            "disk_read_kib_per_sec": Metric(kind="gauge", unit="kib_per_sec", description="Disk read KiB per second"),
            "disk_write_kib_per_sec": Metric(kind="gauge", unit="kib_per_sec", description="Disk write KiB per second"),
            "disk_avg_queue_len": Metric(kind="gauge", unit="requests", description="Average queue length"),
            "disk_utilization_percent": Metric(kind="gauge", unit="percent", description="Disk utilization percent"),
            "disk_device_busy_percent": Metric(kind="gauge", unit="percent", description="Percentage of time device was busy"),
            "disk_read_avg_ms": Metric(kind="gauge", unit="milliseconds", description="Average read latency (ms)"),
            "disk_write_avg_ms": Metric(kind="gauge", unit="milliseconds", description="Average write latency (ms)"),
            "disk_read_avg_kb": Metric(kind="gauge", unit="kilobytes", description="Average KB per read op"),
            "disk_write_avg_kb": Metric(kind="gauge", unit="kilobytes", description="Average KB per write op"),
            "disk_service_time_ms": Metric(kind="gauge", unit="milliseconds", description="Average device service time (ms)"),
        },
        unique_key=["ts","bundle_id","host","device_name"],
        indexes=[["device_name","ts DESC"],["host","ts DESC"]]
    ),
    # Network metrics: rate + interface counters merged into one table via interface label.
    "NET": TableGroup(
        table="ptops_net",
        category="net",
        local_labels=["interface","kind","name_variant"],  # kind=rate|ifstat, name_variant=normalized|legacy (optional)
        metrics={
            # Normalized rate metrics
            "net_rx_packets_per_sec": Metric(kind="gauge", unit="packets_per_sec", description="Receive packets per second", aliases=["net_rk_packets_per_sec"]),
            "net_rx_kib_per_sec": Metric(kind="gauge", unit="kib_per_sec", description="Receive KiB per second", aliases=["net_rk_kib_per_sec"]),
            "net_tx_packets_per_sec": Metric(kind="gauge", unit="packets_per_sec", description="Transmit packets per second", aliases=["net_tk_packets_per_sec"]),
            "net_tx_kib_per_sec": Metric(kind="gauge", unit="kib_per_sec", description="Transmit KiB per second", aliases=["net_tk_kib_per_sec"]),
            "net_rx_drops_per_sec": Metric(kind="gauge", unit="drops_per_sec", description="Receive packet drops per second", aliases=["net_rd_drops_per_sec"]),
            "net_tx_drops_per_sec": Metric(kind="gauge", unit="drops_per_sec", description="Transmit packet drops per second", aliases=["net_td_drops_per_sec"]),
            # Interface counters (subset; dynamic extension allowed)
            "net_rx_packets_total": Metric(kind="counter", unit="packets", description="Cumulative RX packets"),
            "net_tx_packets_total": Metric(kind="counter", unit="packets", description="Cumulative TX packets"),
            "net_rx_errors_total": Metric(kind="counter", unit="errors", description="Cumulative RX errors"),
            "net_tx_errors_total": Metric(kind="counter", unit="errors", description="Cumulative TX errors"),
            "net_rx_bytes_total": Metric(kind="counter", unit="bytes", description="Cumulative RX bytes"),
            "net_tx_bytes_total": Metric(kind="counter", unit="bytes", description="Cumulative TX bytes"),
            "net_rx_dropped_packets_total": Metric(kind="counter", unit="packets", description="Cumulative dropped RX packets"),
            "net_tx_dropped_packets_total": Metric(kind="counter", unit="packets", description="Cumulative dropped TX packets"),
        },
        unique_key=["ts","bundle_id","host","interface","kind","name_variant"],
        indexes=[["interface","ts DESC"],["host","ts DESC"]]
    ),
    # Fast path metrics: separate table per record type for clarity and to keep sparse NULL columns minimal.
    "FPPORTS": TableGroup(
        table="ptops_fpports",
        category="fastpath",
        local_labels=["port"],
        metrics={
            "fpports_ip_total": Metric(kind="counter", unit="packets", description="FP ports input packets total"),
            "fpports_op_total": Metric(kind="counter", unit="packets", description="FP ports output packets total"),
            "fpports_ib_total": Metric(kind="counter", unit="bytes", description="FP ports input bytes total"),
            "fpports_ob_total": Metric(kind="counter", unit="bytes", description="FP ports output bytes total"),
            "fpports_ie_total": Metric(kind="counter", unit="errors", description="FP ports input errors total"),
            "fpports_oe_total": Metric(kind="counter", unit="errors", description="FP ports output errors total"),
            "fpports_mc_total": Metric(kind="counter", unit="packets", description="FP ports multicast packets total"),
            "fpports_im_total": Metric(kind="counter", unit="packets", description="FP ports imiss packets total (DPDK cache misses)"),
            "fpports_in_total": Metric(kind="counter", unit="events", description="FP ports input events total"),
        },
        unique_key=["ts","bundle_id","host","port"],
        indexes=[["port","ts DESC"]]
    ),
    "FPMBUF": TableGroup(
        table="ptops_fpmbuf",
        category="fastpath",
        local_labels=[],
        metrics={
            "fpm_muc": Metric(kind="gauge", unit="count", description="FPMBUF muc metric"),
        },
        unique_key=["ts","bundle_id","host"],
        indexes=[["host","ts DESC"]]
    ),
    "TCP_DCA_STAT": TableGroup(
        table="ptops_tcp_dca_stat",
        category="fastpath",
        local_labels=["interface_addr"],
        metrics={
            "tcp_dca_interfaces": Metric(kind="gauge", unit="count", description="TCP DCA interface count"),
            "tcp_dca_rx_packets_total": Metric(kind="counter", unit="packets", description="TCP DCA RX packets total"),
            "tcp_dca_tx_packets_total": Metric(kind="counter", unit="packets", description="TCP DCA TX packets total"),
            "tcp_dca_dropped_packets_total": Metric(kind="counter", unit="packets", description="TCP DCA dropped packets total"),
            "tcp_dca_queue_drops_total": Metric(kind="counter", unit="drops", description="TCP DCA queue drops total"),
            "tcp_dca_opened_sessions_total": Metric(kind="counter", unit="sessions", description="TCP DCA opened sessions total"),
            "tcp_dca_closed_sessions_total": Metric(kind="counter", unit="sessions", description="TCP DCA closed sessions total"),
            "tcp_dca_active_sessions": Metric(kind="gauge", unit="sessions", description="TCP DCA active sessions"),
        },
        unique_key=["ts","bundle_id","host","interface_addr"],
        indexes=[["interface_addr","ts DESC"]]
    ),
    "FPC": TableGroup(
        table="ptops_fpc",
        category="fastpath",
        local_labels=["cpu"],
        metrics={
            "fpc_cpu_busy_percent": Metric(kind="gauge", unit="percent", description="Fast path CPU busy percent"),
            "fpc_cycles_total": Metric(kind="counter", unit="cycles", description="Fast path CPU cycles total"),
            "fpc_cycles_per_packet": Metric(kind="gauge", unit="cycles_per_packet", description="Cycles per packet"),
            "fpc_cycles_ic_pkt": Metric(kind="gauge", unit="cycles_per_packet", description="Cycles per inner packet"),
        },
        unique_key=["ts","bundle_id","host","cpu"],
        indexes=[["cpu","ts DESC"]]
    ),
    "FPP": TableGroup(
        table="ptops_fpp",
        category="fastpath", 
        local_labels=[],
        metrics={
            "fpp_total_cycles": Metric(kind="counter", unit="cycles", description="Fast path total CPU cycles for packet processing"),
            "fpp_total_packets": Metric(kind="counter", unit="packets", description="Fast path total packets received from NIC"),
            "fpp_cycles_per_packet": Metric(kind="gauge", unit="cycles_per_packet", description="Fast path average cycles per packet from NIC"),
        },
        unique_key=["ts","bundle_id","host"],
        indexes=[["ts DESC"]]
    ),
    "FPS": TableGroup(
        table="ptops_fps",
        category="fastpath",
        local_labels=[],
        metrics={
            "fps_incoming_dns_packets": Metric(kind="counter", unit="packets", description="Fast path incoming DNS packets"),
            "fps_outgoing_dns_packets": Metric(kind="counter", unit="packets", description="Fast path outgoing DNS packets"), 
            "fps_dropped_dns_packets": Metric(kind="counter", unit="packets", description="Fast path dropped DNS packets"),
            "fps_missed_dns_packets": Metric(kind="counter", unit="packets", description="Fast path missed DNS packets"),
            "fps_hit_dns_packets": Metric(kind="counter", unit="packets", description="Fast path hit DNS packets"),
            "fps_bypass_dns_packets": Metric(kind="counter", unit="packets", description="Fast path bypass DNS packets"),
        },
        unique_key=["ts","bundle_id","host"],
        indexes=[["ts DESC"]]
    ),
    "DOT_STAT": TableGroup(
        table="ptops_dot_stat",
        category="fastpath",
        local_labels=["addr","index"],
        metrics={
            "dot_rx_total": Metric(kind="counter", unit="packets", description="DOT rx packets total"),
            "dot_tx_total": Metric(kind="counter", unit="packets", description="DOT tx packets total"),
            "dot_dp_total": Metric(kind="counter", unit="packets", description="DOT dropped packets total"),
            "dot_qd_total": Metric(kind="counter", unit="packets", description="DOT queued drops total"),
        },
        unique_key=["ts","bundle_id","host","addr","index"],
        indexes=[["addr","ts DESC"]]
    ),
    "DOH_STAT": TableGroup(
        table="ptops_doh_stat",
        category="fastpath",
        local_labels=["addr","index"],
        metrics={
            "doh_rx_total": Metric(kind="counter", unit="packets", description="DOH rx packets total"),
            "doh_tx_total": Metric(kind="counter", unit="packets", description="DOH tx packets total"),
            "doh_dp_total": Metric(kind="counter", unit="packets", description="DOH dropped packets total"),
            "doh_qd_total": Metric(kind="counter", unit="packets", description="DOH queued drops total"),
        },
        unique_key=["ts","bundle_id","host","addr","index"],
        indexes=[["addr","ts DESC"]]
    ),
    "FPVLSTATS": TableGroup(
        table="ptops_fpvlstats",
        category="fastpath",
        local_labels=[],
        metrics={
            "fpvl_f_pending": Metric(kind="gauge", unit="count", description="Fast path F pending"),
            "fpvl_f_working": Metric(kind="gauge", unit="count", description="Fast path F working"),
            "fpvl_f_blocked": Metric(kind="gauge", unit="count", description="Fast path F blocked"),
            "fpvl_f_blocked_async": Metric(kind="gauge", unit="count", description="Fast path F blocked async"),
            "fpvl_n_pending": Metric(kind="gauge", unit="count", description="Fast path N pending"),
            "fpvl_n_working": Metric(kind="gauge", unit="count", description="Fast path N working"),
            "fpvl_n_blocked": Metric(kind="gauge", unit="count", description="Fast path N blocked"),
            "fpvl_n_running": Metric(kind="gauge", unit="count", description="Fast path N running"),
            "fpvl_n_blocked_async": Metric(kind="gauge", unit="count", description="Fast path N blocked async"),
            "fpvl_n_dropped": Metric(kind="gauge", unit="count", description="Fast path N dropped"),
            "fpvl_total_fast": Metric(kind="gauge", unit="count", description="Fast path total fast"),
            "fpvl_total_blocked": Metric(kind="gauge", unit="count", description="Fast path total blocked"),
        },
        unique_key=["ts","bundle_id","host"],
        indexes=[["host","ts DESC"]]
    ),
    # Database histogram write (DBWR) buckets
    "DBWR": TableGroup(
        table="ptops_dbwr",
        category="db",
        local_labels=["bucket"],
        metrics={
            "dbwr_bucket_count_total": Metric(kind="counter", unit="events", description="DBWR bucket event count total"),
            "dbwr_bucket_avg_latency_seconds": Metric(kind="gauge", unit="seconds", description="DBWR bucket average latency seconds"),
        },
        unique_key=["ts","bundle_id","host","bucket"],
        indexes=[["bucket","ts DESC"],["host","ts DESC"]]
    ),
    # Database histogram write (async) DBWA
    "DBWA": TableGroup(
        table="ptops_dbwa",
        category="db",
        local_labels=["bucket"],
        metrics={
            "dbwa_bucket_count_total": Metric(kind="counter", unit="events", description="DBWA bucket event count total"),
            "dbwa_bucket_avg_latency_seconds": Metric(kind="gauge", unit="seconds", description="DBWA bucket average latency seconds"),
        },
        unique_key=["ts","bundle_id","host","bucket"],
        indexes=[["bucket","ts DESC"],["host","ts DESC"]]
    ),
    # Database histogram read (DBRD)
    "DBRD": TableGroup(
        table="ptops_dbrd",
        category="db",
        local_labels=["bucket"],
        metrics={
            "dbrd_bucket_count_total": Metric(kind="counter", unit="events", description="DBRD bucket event count total"),
            "dbrd_bucket_avg_latency_seconds": Metric(kind="gauge", unit="seconds", description="DBRD bucket average latency seconds"),
        },
        unique_key=["ts","bundle_id","host","bucket"],
        indexes=[["bucket","ts DESC"],["host","ts DESC"]]
    ),
    # DBMPOOL memory pool statistics (dynamic metrics subset captured explicitly for early schema)
    "DBMPOOL": TableGroup(
        table="ptops_dbmpool",
        category="db",
        local_labels=[],
        metrics={
            "dbmpool_total": Metric(kind="gauge", unit="mib", description="DB memory pool total MiB"),
            "dbmpool_used": Metric(kind="gauge", unit="mib", description="DB memory pool used MiB"),
            "dbmpool_free": Metric(kind="gauge", unit="mib", description="DB memory pool free MiB"),
            "dbmpool_used_percent": Metric(kind="gauge", unit="percent", description="DB memory pool used percent"),
        },
        unique_key=["ts","bundle_id","host"],
        indexes=[["host","ts DESC"]]
    ),
}


def generate_table_ddl(group: TableGroup) -> str:
    cols: List[str] = [f"{name} {decl}" for name, decl in GLOBAL_COLUMNS]
    for lbl in group.local_labels:
        cols.append(f"{lbl} TEXT")
    # metric columns (DOUBLE PRECISION default until specialized types needed)
    for mname, meta in group.metrics.items():
        col = meta.column or mname
        cols.append(f"{col} DOUBLE PRECISION")
    col_sql = ",\n  ".join(cols)
    return f"CREATE TABLE {group.table} (\n  {col_sql}\n);"  # hypertable creation separate


def generate_view_ddl(group: TableGroup) -> Iterable[str]:
    for mname, meta in group.metrics.items():
        col = meta.column or mname
        view_name = mname if mname != col else mname  # canonical name
        # Optional computed helpers (additive, no table schema change)
        extra = ""
        # Provide numeric cpu_index for easier numeric filtering (e.g. cpu_index=0) while retaining original cpu_id
        if group.category == 'cpu' and 'cpu_id' in group.local_labels:
            extra = ", CASE WHEN cpu_id ~ '^cpu[0-9]+$' THEN substring(cpu_id from '[0-9]+')::int END AS cpu_index"
        yield (
            f"CREATE VIEW {view_name} AS SELECT ts, {col} AS value, "
            f"bundle_id, sptid, metric_category, host" + (
                ("," + ",".join(group.local_labels)) if group.local_labels else ""
            ) + extra + f" FROM {group.table} WHERE {col} IS NOT NULL;"
        )
        # alias views removed (deferred)


def _index_name(table: str, cols: List[str], unique: bool=False) -> str:
    base = table + '_' + '_'.join([c.split()[0] for c in cols])  # strip DESC for name
    if unique:
        base = 'uniq_' + base
    return base[:60]  # safety truncate


def generate_all_ddls() -> Dict[str, List[str]]:
    tables: List[str] = []
    views: List[str] = []
    indexes: List[str] = []
    for grp in SCHEMA_SPEC.values():
        tables.append(generate_table_ddl(grp))
        views.extend(list(generate_view_ddl(grp)))
        # Unique index
        if grp.unique_key:
            idx_name = _index_name(grp.table, grp.unique_key, unique=True)
            cols = ','.join(grp.unique_key)
            indexes.append(f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON {grp.table} ({cols});")
        # Secondary indexes
        for cols in grp.indexes:
            idx_name = _index_name(grp.table, cols)
            col_sql = ','.join(cols)
            indexes.append(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {grp.table} ({col_sql});")
    return {"tables": tables, "views": views, "indexes": indexes}

__all__ = [
    "Metric",
    "TableGroup",
    "SCHEMA_SPEC",
    "generate_table_ddl",
    "generate_view_ddl",
    "generate_all_ddls",
]
