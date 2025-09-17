# ptop Collector & Plugin Metrics Reference

Version: 2025-08-25

Purpose: Canonical structured description of every ptop output record / field for downstream vector DB + metrics DB ingestion and LLM grounding. Each section is deliberately verbose to maximize semantic recall while keeping a consistent schema that is straightforward to parse. Provenance (exact kernel / procfs / shared memory / external command source) is explicitly captured so lineage can be stored alongside numeric values.

Parsing Guidance:
- Top-level sections start with `## PLUGIN <name>`.
- Subsections use `###` with fixed labels.
- Field lists appear under `### Fields` with one field per line, pipe-delimited: `PREFIX | token | name | units | type | origin | semantics | computation | notes`.
- Options appear under `### Command-Line Options`.
- Examples under `### Example Records` show one or more literal lines exactly as ptop prints them.
- Container-related plugins (names starting with c*) report per-container metrics; fields include container identifiers.
- Percentages are expressed as floating point with one decimal unless noted.
- Rates are per second unless specified (e.g., per ms) and are derived over the interval between successive snapshots of the same collector.

Legend:
- origin: kernel file, derived, container api, procfs, cgroupfs, syscall, computed
- type: int, float, string, bytes, percent
- units: explicit (%, KiB/s, ms, ticks, bytes, kb, count)
- computation: brief formula; `Δ` denotes difference between current and previous snapshot; `Δt` denotes elapsed seconds between snapshots (high-resolution monotonic time); CLK_TCK = system clock ticks per second.
- metric_kind (introduced in this revision, recorded per field in dedicated subsection per plugin):
	- label: identifier / dimension value, not a numeric measurement
	- gauge: instantaneous value that can move arbitrarily up/down (memory usage, queue depth, percentages that are point-in-time composition, active session counts)
	- counter: monotonically increasing value since process / subsystem start (packets, bytes, events). Wraps should be handled with unsigned logic if required.
	- delta: value already printed by ptop as the per-interval difference of an underlying monotonic counter (e.g., DBMPOOL page creations). Downstream storage MAY still store as counter with aggregation=sum.
	- rate: per-second (or percentage-of-total over interval) derived from deltas divided by elapsed time; not directly accumulatable; derivation requires two snapshots.
	- histogram_bucket: count in a latency (or size) bucket over the interval (usually a delta)
	- histogram_avg: average latency (or value) for operations that fell into that bucket during interval
	- text: opaque human text payload
	- opaque: structured but intentionally unspecified blob (future schema TBD)

Metric kinds appear under a `### Metric Kind Classification` subsection for each plugin to avoid breaking existing field tables parsers.

---

## Product Architecture & Metric Domains (Overview)

This section summarizes the high-level architecture to anchor metric semantics. It is intentionally concise so the design document can focus on software design details while this reference remains the authoritative source for metric group meaning.

### Data Plane Paths
- Fast Path: Accelerated DNS query handling (TCP/DoT/DoH) with in‑memory caches, proxy (FPPRXY), prefetch (FPPREF), DCA decision logic (FPDCA), RR type distribution (FPRRSTATS), EDNS0 & subscriber classification (FPDNCR), and DNS policy / violation tracking (FPVLSTATS). Goals: low latency, offload from named, early policy enforcement.
- Exception / Resolver Path ("named" / BIND): Handles cache misses, complex recursion, policy evaluation not satisfied on fast path, and late-stage violation detection (N-* counters in FPVLSTATS).

### Core Fast Path Components
- Listener / Session Layer: DOT_STAT, DOH_STAT, TCP_DCA_STAT expose per-socket packet + session lifecycle metrics (rx, tx, dropped, overflow, opened/closed/active sessions).
- Port I/O: FPPORTS maps to DPDK `rte_eth_stats` giving link-layer packets/bytes/errors and buffer pressure signals (missed, nombuf).
- Memory Pool: FPMBUF tracks mbuf usage (in use, available, utilization percent) correlating with FPPORTS `rx_nombuf` drops.
- Proxy Aggregate: FPPRXY mediates queries to MSP upstream; counters differentiate success (added, responses) vs failure (parse/send/timeouts) vs fallback (passed to bind) vs state (connected cores, active queries).
- Prefetch Engine: FPPREF proactively constructs A/AAAA queries; symmetric success/failure counters allow build/enqueue/response coverage and failure ratios.
- DCA Decision Layer: FPDCA captures fallback and non-cacheable response decisions, informing cache efficiency and authoritative load shifting.
- RR Type Distribution: FPRRSTATS splits query/response traffic by RR type enabling adoption analyses (e.g., SVCB/HTTPS) and composition baselines.
- Classification & Policy: FPDNCR surfaces EDNS0 option presence & subscriber identification; FPVLSTATS contrasts early (fast path) and late (named) policy actions & violations.

### Cross-Cutting Concepts
- Success Category Taxonomy: success, failure, timeout, fallback, decision, classification, progress, state used across fast path metrics for consistent semantic grouping.
- Derived Ratios (documented in plugin notes) unify troubleshooting heuristics: insertion failure rate (FPPRXY), enqueue failure rate (FPPREF), violation distribution (FPVLSTATS), EDNS0 adoption (FPDNCR), packet loss (FPPORTS), session overflow impact (DOT/DOH/TCP_DCA).
- Alias Strategy: Short CLI tokens preserved as `legacy_aliases` for embeddings; canonical names provide explicit domain + action nouns for clarity (`fpprxy_query_timeouts_total`).
- Provenance Dimensions: Each metric ties to external command and (where confirmed) function/struct origins enabling lineage tracking.

### Relationships & Correlations
- High mbuf utilization (`fpm_mbuf_utilization_percent`) with rising `fpports_rx_nombuf_drops_total` implies buffer sizing pressure; correlate with traffic spikes in FPPORTS rx.
- Elevated `fpprxy_query_timeouts_total` often precedes increased `fpprxy_reconnects_total` if upstream MSP instability; check parse failure counters for protocol issues.
- Growth in `fpdca_non_cacheable_responses_total` can degrade cache hit efficiency; evaluate RR type mix (FPRRSTATS) for negative response trends.
- Rising `fpvlstats_fastpath_block_all_queries_total` with stable RR distribution suggests policy tightening rather than traffic pattern shift.
- Subscriber classification coverage = `fpdncr_subscriber_hits_total` / Σ(FPRRSTATS query counters); low coverage may reduce effectiveness of policy personalization.

### Embedding Notes
- This architecture block is exported as an L4 concept doc (`concept:fast_path_architecture`) to semantically link field-level docs via shared phrases (e.g., "timeout", "fallback", "policy violation").
- L1 docs set `metric_category` (e.g., `fast_path`) allowing vector queries to scope retrieval.

---

## Parsing & Verification Overview

This specification has been empirically validated against the sample capture `ptop-20250628_1351.log` located in the repository. For every documented prefix (CPU, MEM, DISK, NET, TOP, SMAPS, DBWR/DBWA/DBRD, DBMPOOL, FP*, DOT_STAT, DOH_STAT, FPMBUF, FPPORTS, NODE, etc.) at least one line appears in the log except those that are feature‑gated (BALLOON, UDP, VADP_, SNIC_, TCP_DCA_STAT, IMCDR_*) depending on runtime flags / hardware. Conditional plugins still have defined schemas here so a parser can support them proactively.

Recommended generic parsing algorithm:
1. Read line; trim newline. If blank continue.
2. Identify primary token (split on whitespace first). If it matches a multi-token header (e.g. `NET ifstat` or `SNMP.Ip`) treat that composite as the prefix key.
3. Dispatch to handler table keyed by prefix. Handlers either:
	 - Expect fixed positional pattern with literal anchor tokens (e.g., CPU, MEM, DISK, NET rate, DBWR buckets as repeating triplets) OR
	 - Treat remaining tokens as alternating key/value pairs (fastpath FP* lines, DOT/DOH/TCP_DCA style groups) OR
	 - Pass-through opaque payload (DB, VADP, SNIC raw multi-line blocks).
4. For delta / rate fields requiring history, consult previous ParsedRecord with identical identity key (e.g., device, interface, cpu id). If absent, emit null/NaN for rate fields on first observation.
5. Tag each extracted metric with provenance metadata from its plugin section (proc path, shared memory object, external command) to facilitate governance and observability lineage.

Reference ParsedRecord structure:
```
ParsedRecord {
	prefix: string,          // e.g. CPU, DISK, NET, DBWR, FPPORTS
	subtype: string|null,    // interface, disk name, bucket index grouping, etc.
	fields: { <canonical_field_name>: number|string },
	raw: string,             // original line
	ts: datetime,            // ingestion timestamp
	provenance: { proc:[], sysfs:[], shm:[], external:[], notes:string }
}
```

Edge cases handled:
- Lines that are headers for FP CPU usage (e.g. `FPC Fast path CPU usage:`) can be skipped or stored as annotation (no numeric payload); subsequent numeric FPC lines follow described schema.
- Multi-line opaque plugins (VADP, SNIC, IMCDR, TCP_DCA_STAT when future variants appear) may output multiple lines per cycle; each line begins with its identifying prefix (`VADP_`, `SNIC`, `IMCDR_`, `TCP_DCA_STAT`). Preserve ordering.
- Histogram plugins (DBWR/DBWA/DBRD) contain repeating triplets; parse until tokens exhausted.
- SMAPS command field may contain spaces; treat everything after token `c` as one raw command string.

Validation sample counts (approximate from log excerpt): CPU (~30+ per interval), MEM (1 per interval), DISK (per active device), NET (rate + ifstat pairs), TOP (hundreds), SMAPS (dozens), DBWR/DBWA/DBRD (1 each), DBMPOOL (1 each), FPMBUF (1), FPPORTS (N ports), DOT_STAT/DOH_STAT (1 each), FPC/FPP (multiple lines), NODE (present in other runs— not in captured slice if disabled by -Ms). This breadth exercises all parsing modes.

---

## PLUGIN cpu

### Description
Reports system CPU usage summary and optionally per-CPU utilization percentages derived from /proc/stat jiffy counters. Enabled by default (unless disabled by grouped options). Each CPU line is differential (between consecutive samples). Idle breakdown distinguishes iowait. Percentages are normalized to the active plus idle time for the interval.

Provenance: `/proc/stat` (fields: user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice). Only a subset printed; steal & guest excluded from output but included implicitly in `Δtotal` denominator if present.

### Record Prefix
`CPU`

### Record Forms
1. Summary: `CPU  cpu u <u> id/io <idle> <iowait> u/s/n <user> <sys> <nice> irq h/s <hardirq> <softirq>`
2. Per-CPU: `CPU cpuN u <u> id/io <idle> <iowait> u/s/n <user> <sys> <nice> irq h/s <hardirq> <softirq>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
CPU|cpu|cpu_id|n/a|string|literal|Identifier 'cpu' summary or 'cpuN' per logical CPU|n/a|Summary line uses literal 'cpu'.
CPU|u|utilization|percent|float|computed|Overall non-idle, non-iowait CPU percent for interval|100 * (Δtotal - Δidle - Δiowait)/Δtotal|Excludes iowait.
CPU|id|idle_percent|percent|float|computed|Idle time percent|100 * Δidle / Δtotal|First number after id/io.
CPU|io|iowait_percent|percent|float|computed|I/O wait time percent|100 * Δiowait / Δtotal|Second number after id/io.
CPU|u/s/n_user|user_percent|percent|float|computed|User mode time percent|100 * Δuser / Δtotal|First number after u/s/n.
CPU|u/s/n_sys|system_percent|percent|float|computed|Kernel mode time percent|100 * Δsystem / Δtotal|Second number after u/s/n.
CPU|u/s/n_nice|nice_percent|percent|float|computed|User time at altered priority|100 * Δuser_low_priority / Δtotal|Third number after u/s/n.
CPU|irq|hardirq_percent|percent|float|computed|Hardware interrupt handling time|100 * Δirq / Δtotal|Labelled under irq h/s.
CPU|h/s_soft|softirq_percent|percent|float|computed|Software interrupt handling time|100 * Δsoftirq / Δtotal|Second number after irq h/s.

### Command-Line Options
`-C[s]` enable CPU stats (default on) and optional suboption 's' for summary only; `-D`, `-M`, `-N` disable via grouped options (mutually disabling CPU along with others in group).

### Example Records
CPU  cpu u  5.7 id/io 47.4 46.9 u/s/n  0.0  3.6  2.1 irq h/s  0.0  0.0
CPU cpu0 u 14.9 id/io  0.0 85.1 u/s/n  0.0 10.9  4.0 irq h/s  0.0  0.0

### Notes
- Total = sum of all component jiffy deltas (user + nice + system + idle + iowait + irq + softirq + steal + guest when present though guest/steal excluded from printed breakdown). Steal & guest not printed.
- Interval length defined by ptop cycle; percentages independent of cycle length.

### Metric Kind Classification
- cpu_id: label
- utilization: rate
- idle_percent: rate
- iowait_percent: rate
- user_percent: rate
- system_percent: rate
- nice_percent: rate
- hardirq_percent: rate
- softirq_percent: rate

---

## PLUGIN mem

### Description
System memory usage and paging/swap activity derived from /proc/meminfo, /proc/vmstat and /proc/sysvipc/shm. Enabled by default. Outputs one MEM line per snapshot (always). Includes instantaneous composition percentages relative to total physical memory plus swap/huge page counts and optional paging & swap rates if previous snapshot exists.

Provenance: `/proc/meminfo` (MemTotal, MemFree, Buffers, Cached, Active(file), Inactive(file), AnonPages, Shmem, SwapTotal, SwapFree, HugePages_Total, HugePages_Free, MemAvailable), `/proc/vmstat` (pgpgin, pgpgout, pswpin, pswpout), `/proc/sysvipc/shm` (shared segment sizes). Page size assumed 4KiB.

### Record Prefix
`MEM`

### Record Form
`MEM t <total_bytes> f <free_pct> b <buffers_pct> c <pagecache_pct> s <slab_pct> a <anon_pct> sh <sysvshm_pct> sw <swap_used_pct> <swap_total_bytes> h <huge_total> <huge_free> A <available_pct> [pio <pgpgin_per_sec> <pgpgout_per_sec> sio <pswpin_per_sec> <pswpout_per_sec>]`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
MEM|t|total_memory|bytes|int|/proc/meminfo|Total installed physical memory|MemTotal * 1024|Constant during run.
MEM|f|free_percent|percent|float|/proc/meminfo|Percent free (unused)|100 * MemFree / MemTotal|
MEM|b|buffers_percent|percent|float|/proc/meminfo|Buffer cache percent|100 * Buffers / MemTotal|
MEM|c|cached_percent|percent|float|/proc/meminfo|File cache percent (Active(file)+Inactive(file))|100 * (Active(file)+Inactive(file))/MemTotal|Excludes anon.
MEM|s|slab_percent|percent|float|/proc/meminfo|Slab memory percent|100 * Slab / MemTotal|
MEM|a|anon_percent|percent|float|/proc/meminfo|Anonymous pages percent|100 * AnonPages / MemTotal|
MEM|sh|sysv_shm_percent|percent|float|/proc/sysvipc/shm|System V shared memory percent|100 * (sum shm segment pages *4)/ MemTotal|Segments aggregated & converted kB -> pct.
MEM|sw|swap_used_percent|percent|float|/proc/meminfo|Swap utilization percent|100 * (SwapTotal - SwapFree)/SwapTotal|If SwapTotal==0 prints 0.
MEM|<after sw>|swap_total_bytes|bytes|int|/proc/meminfo|Total swap capacity|SwapTotal * 1024|
MEM|h_total|hugepages_total|count|int|/proc/meminfo|Configured HugePages_Total|Raw value|
MEM|h_free|hugepages_free|count|int|/proc/meminfo|Free huge pages|Raw value|
MEM|A|available_percent|percent|float|/proc/meminfo|MemAvailable percent|100 * MemAvailable / MemTotal|Added (shows reclaimable memory estimate).
MEM|pio_pgpgin|pgpgin_rate|pages_per_sec|float|/proc/vmstat|Page in operations per second|Δpgpgin / Δt|Printed if previous snapshot.
MEM|pio_pgpgout|pgpgout_rate|pages_per_sec|float|/proc/vmstat|Page out operations per second|Δpgpgout / Δt|
MEM|sio_pswpin|swapin_rate|ops_per_sec|float|/proc/vmstat|Swap-in events per second|Δpswpin / Δt|
MEM|sio_pswpout|swapout_rate|ops_per_sec|float|/proc/vmstat|Swap-out events per second|Δpswpout / Δt|

### Command-Line Options
`-M` enable; `-C`, `-D`, `-N` disable (group). Enabled by default.

### Example Records
MEM t 8381067264 f 2.9 b 9.9 c 66.3 s 0.0 a 3.6 sh 0.0 sw 0.0 4294959104 h 16215 275 A 73.4 pio 12.0 8.0 sio 0.0 0.0

### Notes
- Percent fields sum will not equal 100% because categories overlap or omit some types (e.g., slab part of cached in some kernel views, shared memory separate).
- Paging & swapping rates require at least two samples.

### Metric Kind Classification
- total_memory: gauge
- free_percent: gauge
- buffers_percent: gauge
- cached_percent: gauge
- slab_percent: gauge
- anon_percent: gauge
- sysv_shm_percent: gauge
- swap_used_percent: gauge
- swap_total_bytes: gauge
- hugepages_total: gauge
- hugepages_free: gauge
- available_percent: gauge
- pgpgin_rate: rate
- pgpgout_rate: rate
- swapin_rate: rate
- swapout_rate: rate

---

## PLUGIN disk

### Description
Per-disk and per-partition block I/O performance statistics derived from /proc/diskstats. Differential metrics over interval providing operation rates, throughput, average request size and latency/queue metrics. Enabled by default. Only prints lines for disks with I/O during interval (non-zero ops).

Provenance: `/proc/diskstats` (major minor name reads_completed reads_merged sectors_read ms_reading writes_completed writes_merged sectors_written ms_writing ios_in_progress ms_doing_io weighted_ms_doing_io). Conversions: sectors * 512 -> bytes; bytes / 1024 -> KiB.

### Record Prefix
`DISK`

### Record Form
`DISK <index> <devname> rkxt <rps> <r_kib_s> <r_avg_kb> <r_avg_ms> wkxt <wps> <w_kib_s> <w_avg_kb> <w_avg_ms> sqb <svc_ms> <queue_len> <busy_pct>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
disk|index|disk_index|count|int|internal|Stable ordering per run based on iteration|loop index|Arbitrary but consistent during run.
DISK|devname|device_name|n/a|string|/proc/diskstats|Block device or partition name|Parsed field 3|
DISK|rps|reads_per_sec|ops_per_sec|float|computed|Read operations rate|1000 * Δreads_completed / Δms|Δms = elapsed ms between samples.
DISK|r_kib_s|read_kib_per_sec|KiB/s|float|computed|Read throughput|1000 * Δsectors_read / (2 * Δms)|512B sectors -> KiB.
DISK|r_avg_kb|read_avg_kb|KiB|float|computed|Average read size|Δsectors_read / (Δreads_completed * 2)|Guard when Δreads_completed>0.
DISK|r_avg_ms|read_avg_ms|ms|float|computed|Average service time per read op|Δtime_reading / Δreads_completed|Field 7 delta / reads.
DISK|wps|writes_per_sec|ops_per_sec|float|computed|Write operations rate|1000 * Δwrites_completed / Δms|
DISK|w_kib_s|write_kib_per_sec|KiB/s|float|computed|Write throughput|1000 * Δsectors_written / (2 * Δms)|
DISK|w_avg_kb|write_avg_kb|KiB|float|computed|Average write size|Δsectors_written / (Δwrites_completed * 2)|Guard zero writes.
DISK|w_avg_ms|write_avg_ms|ms|float|computed|Average service time per write op|Δtime_writing / Δwrites_completed|
DISK|svc_ms|service_time_ms|ms|float|computed|Average op service time overall|Δtime_in_io / (Δreads_completed + Δwrites_completed)|Small is good.
DISK|queue_len|avg_queue_len|ops|float|computed|Average outstanding I/O queue depth|Δweighted_time_in_io / Δms|Correlates to backlog.
DISK|busy_pct|device_busy_percent|percent|float|computed|Percent time any I/O in progress|100 * Δtime_in_io / Δms|Capped at 100.

### Command-Line Options
`-D` enable; `-C`, `-M`, `-N` disable. Enabled by default.

### Example Records
DISK 24  sda rkxt 1.0 4.124 4.000 110.0 wkxt 261.9 21958.763 83.858 480.3 sqb 3.9 184.5 100.0

### Notes
- Large queue_len with modest service_time_ms implies saturation above device capacity.
- Average sizes reflect mixture of merged and individual requests.

### Legacy Naming & Aliases
Earlier drafts and historical stored data used KiB-based throughput metric suffixes (`read_kib_per_sec`, `write_kib_per_sec`).

Canonical emission now uses `read_kb_per_sec` / `write_kb_per_sec` (decimal kilobytes). The parser performs this rename at emission time; the source tokens (`r_kib_s`, `w_kib_s`) and field names in this table still reflect original units for provenance. Vector search MUST treat queries containing `*_kib_per_sec` as synonyms for `*_kb_per_sec`.

Alias Mapping:
| Legacy Metric Name        | Canonical Metric Name   | Notes |
|---------------------------|-------------------------|-------|
| disk_read_kib_per_sec     | disk_read_kb_per_sec    | Unit label normalized |
| disk_write_kib_per_sec    | disk_write_kb_per_sec   | Unit label normalized |

### Metric Kind Classification
- disk_index: label
- device_name: label
- reads_per_sec: rate
- read_kib_per_sec: rate
- read_avg_kb: gauge
- read_avg_ms: gauge
- writes_per_sec: rate
- write_kib_per_sec: rate
- write_avg_kb: gauge
- write_avg_ms: gauge
- service_time_ms: gauge
- avg_queue_len: gauge
- device_busy_percent: rate

---

## PLUGIN net

### Description
Per-network-interface packet & byte rates plus drop rates derived from /proc/net/dev (or alternate path via fps wrapper when flag present). Two lines per interface: rate summary (if traffic >0) and a raw counter snapshot line for diagnostic correlation.

Provenance: `/proc/net/dev` (rx_bytes rx_packets rx_errs rx_drop rx_fifo rx_frame rx_compressed rx_multicast tx_bytes tx_packets tx_errs tx_drop tx_fifo tx_colls tx_carrier tx_compressed). Only subset used.

### Record Prefix
`NET` (rate) and `NET ifstat` (raw counters)

### Record Forms
1. Rate: `NET<iface> rk <rx_pps> <rx_kib_s> tk <tx_pps> <tx_kib_s> rd <rx_drop_ps> td <tx_drop_ps>`
2. Snapshot: `NET ifstat<iface> <rx_pkts> <rx_bytes> <tx_pkts> <tx_bytes> <rx_drops> <tx_drops>`

### Fields (Rate Line)
PREFIX|token|name|units|type|origin|semantics|computation|notes
NET|iface|interface|n/a|string|/proc/net/dev|Interface name|Parsed before colon|Concatenated to prefix (no space).
NET|rk_rx_pps|rx_packets_per_sec|pps|float|computed|Receive packet rate|1000 * Δrecv_packets / Δms|
NET|rk_rx_kib|rx_kib_per_sec|KiB/s|float|computed|Receive throughput|1000 * Δrecv_bytes /(Δms *1024)|
NET|tk_tx_pps|tx_packets_per_sec|pps|float|computed|Transmit packet rate|1000 * Δxmt_packets / Δms|
NET|tk_tx_kib|tx_kib_per_sec|KiB/s|float|computed|Transmit throughput|1000 * Δxmt_bytes /(Δms *1024)|
NET|rd_rx_drop_ps|rx_drops_per_sec|pps|float|computed|Receive drops rate|1000 * Δrecv_packets_dropped / Δms|Requires carrier support.
NET|td_tx_drop_ps|tx_drops_per_sec|pps|float|computed|Transmit drops rate|1000 * Δxmt_packets_dropped / Δms|

### Fields (Snapshot Line)
PREFIX|token|name|units|type|origin|semantics|computation|notes
NET ifstat|rx_pkts|rx_packets|count|int|/proc/net/dev|Cumulative received packets|raw|Monotonic.
NET ifstat|rx_bytes|rx_bytes|bytes|int|/proc/net/dev|Cumulative received bytes|raw|
NET ifstat|tx_pkts|tx_packets|count|int|/proc/net/dev|Cumulative transmitted packets|raw|
NET ifstat|tx_bytes|tx_bytes|bytes|int|/proc/net/dev|Cumulative transmitted bytes|raw|
NET ifstat|rx_drops|rx_dropped_packets|count|int|/proc/net/dev|Cumulative receive drops|raw|
NET ifstat|tx_drops|tx_dropped_packets|count|int|/proc/net/dev|Cumulative transmit drops|raw|

### Command-Line Options
`-N[nus]`: enable; suboptions: `n` interface stats (default), `u` UDP stats (handled by separate udp plugin), `s` SNMP stats (snmp plugin). `-C -D -M` disable via group.

### Example Records
NET eth0 rk 3.0 0.2 tk 0.0 0.0 rd 0.0 td 0.0
NET ifstateth0 120394 9832456 34011 5643321 0 0

### Notes
- Snapshot line always printed after rate line loop for each interface (even if no traffic) in current implementation.
- Rate line suppressed if no packet delta (sum recv+tx zero).

### Legacy Naming & Aliases
The NET rate line originally produced only short-form metric names with `rk_`, `tk_`, `rd_`, `td_` segments and binary unit suffix `_kib_per_sec`. To preserve backward compatibility, the parser currently emits BOTH the legacy and normalized forms simultaneously, distinguished by label `name_variant` = `legacy` or `normalized`.

Alias Mapping (Rate Metrics):
| Legacy Metric Name              | Normalized Metric Name         | Normalization |
|---------------------------------|--------------------------------|---------------|
| net_rk_packets_per_sec          | net_rx_packets_per_sec         | rk -> rx |
| net_rk_kib_per_sec              | net_rx_kb_per_sec              | rk->rx + kib->kb |
| net_tk_packets_per_sec          | net_tx_packets_per_sec         | tk -> tx |
| net_tk_kib_per_sec              | net_tx_kb_per_sec              | tk->tx + kib->kb |
| net_rd_drops_per_sec            | net_rx_drops_per_sec           | rd->rx |
| net_td_drops_per_sec            | net_tx_drops_per_sec           | td->tx |

Snapshot (ifstat) metrics never had legacy short forms beyond their raw counter tokens; no dual emission occurs there.

Deprecation Plan: After downstream consumers migrate, legacy emissions may be disabled (keeping these alias rows for semantic recall so vector DB can answer legacy queries by linking to canonical docs). Queries containing legacy names should resolve to the same documentation snippet as their normalized counterparts.

### Metric Kind Classification
Rate line:
- interface: label
- rx_packets_per_sec: rate
- rx_kib_per_sec: rate
- tx_packets_per_sec: rate
- tx_kib_per_sec: rate
- rx_drops_per_sec: rate
- tx_drops_per_sec: rate

Snapshot line:
- rx_packets: counter
- rx_bytes: counter
- tx_packets: counter
- tx_bytes: counter
- rx_dropped_packets: counter
- tx_dropped_packets: counter

---

## PLUGIN tasks (TOP)

### Description
Shows most CPU-active threads (tasks) between samples. Enabled only when -T specified. Sorts by descending CPU percent (single-core normalized). Each line is a thread (not aggregated process) and includes parent PID and CPU usage since last sample plus cumulative totals.

Provenance: `/proc/[pid]/task/[tid]/stat` (utime, stime, ppid, processor), global `/proc/stat` for total jiffies denominator. Command from comm field in stat (between parentheses); may be truncated or contain spaces for kernel threads.

### Record Prefix
`TOP`

### Record Form
`TOP <ppid> <pid> <pct_cpu>% <total_cpu_sec> (<user_sec> <sys_sec>) <last_cpu> [cntr <container_id> <nspid> <container_name>] <exec_name>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
TOP|ppid|parent_pid|pid|int|/proc/[pid]/task/*/stat|Parent process ID|field 4 of stat|Converted int.
TOP|pid|pid|pid|int|/proc/...|Thread ID (TID) / PID|field 1|Thread-level.
TOP|pct_cpu|cpu_percent|percent|float|computed|CPU usage as percent of single CPU over interval|1000 * Δtotal_time_ticks / Δms|Δtotal_time_ticks = Δ(user+sys) ticks; denominator scaled using ms & CLK_TCK=100 -> 1000*(Δticks)/Δms.
TOP|total_cpu_sec|total_cpu_seconds|seconds|float|computed|Cumulative CPU seconds (user+sys)|total_time_ticks / CLK_TCK|Since process start.
TOP|user_sec|user_cpu_seconds|seconds|float|computed|Cumulative user (including nice) CPU seconds|user_time / CLK_TCK|
TOP|sys_sec|system_cpu_seconds|seconds|float|computed|Cumulative system CPU seconds|sys_time / CLK_TCK|
TOP|last_cpu|last_run_cpu|index|int|/proc/*/stat|Last CPU number executed on|field 39|Scheduling info.
TOP|container_id|container_short_id|n/a|string|container API|Short identifier of container hosting PID|lookup via ptopcntr_cntr_info|Present only if containerized & differs.
TOP|nspid|container_pid|pid|int|container API|PID inside container namespace|ptopcntr_local_to_cntr_pid|Optional.
TOP|container_name|container_name|n/a|string|container API|Human-friendly container name|ptopcntr_cntr_info|Optional.
TOP|exec_name|executable_name|string|string|/proc/*/stat|Command/executable (may include brackets)|parsed before state|Truncated to buffer size.

### Command-Line Options
`-T#` enable tasks with optional integer limit (#) of lines (default unlimited). Higher # just increases output; minor overhead for sorting. Without -T tasks plugin disabled.

### Example Records
TOP 11393 11404 80.3% 4.7 (4.6 0.1) 0 bash

### Notes
- Percent relative to single CPU; on multi-core boxes a thread can exceed 100% only if accounting drift (normally capped by per-core). Aggregating across threads requires summing Δticks first then recomputing percentage.
- Sorting performed each sample; tasks missing in next sample disappear.

### Metric Kind Classification
- parent_pid: label
- pid: label
- cpu_percent: rate
- total_cpu_seconds: counter
- user_cpu_seconds: counter
- system_cpu_seconds: counter
- last_run_cpu: label (categorical)
- container_short_id: label
- container_pid: label
- container_name: label
- executable_name: label

---

## PLUGIN balloon

### Description
Reports guest physical memory balloon usage (MB) on supported hypervisors (VMware, KVM, Hyper-V). Only runs where detection logic succeeds and memory used >0.

Provenance: Hypervisor-specific interfaces (e.g., `vmware-toolbox-cmd stat mem balloon`, `/proc/vmstat` entries). Exact command path varies by environment.

### Record Prefix
`BALLOON`

### Record Form
`BALLOON  <memory_used_mb>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
BALLOON|memory_used|balloon_memory|MB|int|hypervisor tools & kernel|Memory currently held by balloon driver|VMware: vmware-toolbox-cmd; KVM: (/proc/vmstat inflate-deflate)/512; Hyper-V: trace balloon_status pages_ballooned /256|Zero omitted (no line) if memory_used==0.

### Command-Line Options
None explicit; auto-enabled when supported hypervisor detected; part of default set.

### Example Records
BALLOON  196

### Notes
- Interpret as reclaimed from guest OS and returned to host; high sustained value may indicate host memory pressure.

### Metric Kind Classification
- balloon_memory: gauge

---

## PLUGIN ccpu (Container CPU)

### Description
Container-wide CPU usage by container (optionally per-CPU). Differential percentages per container user and system time between samples.

Provenance: cgroup CPU accounting (`cpuacct.usage*` / `cpu.stat`), resolved via internal `ptopcntr` library mapping container IDs.

### Record Prefix
`CCPU`

### Record Form
`CCPU cpu[<n>] <cntrid> u/s <user_pct> <sys_pct>  <cntrnm>` (per-CPU form) or without `cpu[<n>]` when summarized

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
CCPU|cpu_n|cpu_index|index|int|cgroupfs|Logical CPU index when per-CPU option used|loop index|Omitted in summary mode.
CCPU|cntrid|container_id|n/a|string|container API|Short container ID|ptopcntr APIs|
CCPU|user_pct|user_percent|percent|float|computed|User mode CPU percent during interval|Δuser_jiffies / Δtotal_jiffies *100 (within container scope)|Normalization across all CPUs unless per-CPU mode.
CCPU|sys_pct|system_percent|percent|float|computed|System mode CPU percent during interval|Δsys_jiffies / Δtotal_jiffies *100|Add user_pct+sys_pct may be < total due to other modes.
CCPU|cntrnm|container_name|n/a|string|container API|Container name|Lookup|Trailing spaces trimmed.

### Command-Line Options
`-G` disable containerwide stats globally; `-J` enable per-CPU container stats instead of summary.

### Example Records
CCPU cpu[0] abc123 u/s 3.1 1.4  my-container
CCPU abc123 u/s 6.2 2.7  my-container

### Notes
- Percent normalization matches system CPU semantics; per-CPU mode allows hotspot mapping.

### Metric Kind Classification
- cpu_index: label
- container_id: label
- user_percent: rate
- system_percent: rate
- container_name: label

---

## PLUGIN cmem (Container Memory)

### Description
Per-container memory composition and paging activity via cgroup memory.stat and related counters. Differential page in/out rates.

Provenance: cgroup memory controller (memory.stat, memory.current, memory.swap.current / legacy memory.usage_in_bytes) + per-cgroup vmstat events.

### Record Prefix
`CMEM`

### Record Form
`CMEM <cntrid> c <cache_kb> fi <file_backed_kb> r <rss_kb> h <rss_huge_kb> sh <shmem_kb> sw <swap_kb> u <unevictable_kb> pio <pin_rate> <pout_rate> <cntrnm>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
CMEM|cntrid|container_id|n/a|string|cgroupfs|Short container ID|cgroup path mapping|
CMEM|c|page_cache_kb|kB|int|cgroupfs memory.stat|Page cache memory|total_cache /1024|Includes file cache.
CMEM|fi|file_backed_kb|kB|int|cgroupfs|Active+Inactive file-backed pages| (total_af + total_if)/1024|
CMEM|r|rss_kb|kB|int|cgroupfs|Anonymous + swap cache memory|total_rss /1024|
CMEM|h|rss_huge_kb|kB|int|cgroupfs|Transparent huge page RSS|total_rss_huge /1024|
CMEM|sh|shmem_kb|kB|int|cgroupfs|Shared memory|total_shmem /1024|
CMEM|sw|swap_kb|kB|int|cgroupfs|Container swap usage|total_swap /1024|
CMEM|u|unevictable_kb|kB|int|cgroupfs|Unevictable (mlocked) memory|unevictable /1024|
CMEM|pio_pin|charge_rate|events_per_sec|float|cgroupfs|Charging (memory allocation) events per sec|Δpgpgin / Δt|Labelled pin.
CMEM|pio_pout|unchg_rate|events_per_sec|float|cgroupfs|Uncharging events per sec|Δpgpgout / Δt|Labelled pout.
CMEM|cntrnm|container_name|n/a|string|container API|Container name|Lookup.

### Command-Line Options
`-G` disable containerwide stats.

### Example Records
CMEM ab12c3 c 204800 fi 102400 r 51200 h 0 sh 4096 sw 0 u 0 pio 12.000 10.000 my-container

### Notes
- Pin/pout approximate memory churn; large pin with small RSS growth suggests cache turnover.

### Metric Kind Classification
- container_id: label
- page_cache_kb: gauge
- file_backed_kb: gauge
- rss_kb: gauge
- rss_huge_kb: gauge
- shmem_kb: gauge
- swap_kb: gauge
- unevictable_kb: gauge
- charge_rate: rate
- unchg_rate: rate
- container_name: label

---

## PLUGIN cdisk (Container Disk)

### Description
Per-container block device I/O usage aggregated per whole disk using cgroup blkio counters (and /proc/diskstats for context). Differential rates for read/write operations, throughput and average transfer sizes.

Provenance: cgroup blkio (v1) or io.stat (v2) files; device metadata from `/proc/diskstats`.

### Record Prefix
`CDISK`

### Record Form
`CDISK <cntrid> <devid> <devnm> rkx <rps> <rkps> <r_avg_kb> wkx <wps> <wkps> <w_avg_kb>  <cntrnm>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
CDISK|cntrid|container_id|n/a|string|cgroup blkio|Short container ID|Mapping|
CDISK|devid|device_major|major|min|int|/proc/diskstats|Major device ID|Field 1|Only whole-disk entries.
CDISK|devnm|device_name|n/a|string|/proc/diskstats|Block device name|Field 3|
CDISK|rps|reads_per_sec|ops_per_sec|float|computed|Container read op rate|Δreads / Δt|Container-scoped.
CDISK|rkps|read_kb_per_sec|KiB/s|float|computed|Container read throughput|Δsectors_read /(Δt*2)|512B->KiB.
CDISK|r_avg_kb|read_avg_kb|KiB|float|computed|Avg read size|Δsectors_read /(Δreads*2)|Guard zero.
CDISK|wps|writes_per_sec|ops_per_sec|float|computed|Container write op rate|Δwrites / Δt|
CDISK|wkps|write_kb_per_sec|KiB/s|float|computed|Container write throughput|Δsectors_written /(Δt*2)|
CDISK|w_avg_kb|write_avg_kb|KiB|float|computed|Avg write size|Δsectors_written /(Δwrites*2)|Guard zero.
CDISK|cntrnm|container_name|n/a|string|container API|Container name|Trailing spaces trimmed.

### Command-Line Options
`-G` disable containerwide stats.

### Example Records
CDISK ab12c3 8 sda rkx 5.0 640.0 128.0 wkx 3.0 384.0 128.0  my-container

### Notes
- Service times and queue metrics not container-scoped here (unlike global DISK plugin).

### Metric Kind Classification
- container_id: label
- device_major: label
- device_name: label
- reads_per_sec: rate
- read_kb_per_sec: rate
- read_avg_kb: gauge
- writes_per_sec: rate
- write_kb_per_sec: rate
- write_avg_kb: gauge
- container_name: label

---

## PLUGIN snmp

### Description
Extended IP & UDP protocol counters & rates from /proc/net/snmp (enabled with -Ns). Only prints lines when there is delta (activity) between snapshots for given protocol subset. Provides packet rates, drops, errors.

Provenance: `/proc/net/snmp` structured header/value pairs (Ip: / Udp: lines).

### Record Prefix
`SNMP.Ip` and `SNMP.Udp`

### Record Forms
1. IP: `SNMP.Ip rx/del/dis <rx_ps> <delivered_ps> <in_discards_ps> tx/dis <tx_ps> <out_discards_ps> rh/ra/rp <hdr_err> <addr_err> <unk_proto>`
2. UDP: `SNMP.Udp rx/re <udp_rx_ps> <rcvbuf_errs> tx/te <udp_tx_ps> <sndbuf_errs> np <no_ports> ie <in_errors>`

### Fields (IP)
PREFIX|token|name|units|type|origin|semantics|computation|notes
SNMP.Ip|rx_ps|ip_in_receives_per_sec|pps|float|/proc/net/snmp|Incoming IP packets rate|ΔIpInReceives / Δt|rx part.
SNMP.Ip|delivered_ps|ip_delivered_per_sec|pps|float|/proc/net/snmp|Packets delivered to upper layers rate|ΔIpInDelivers / Δt|
SNMP.Ip|in_discards_ps|ip_in_discards_per_sec|pps|float|/proc/net/snmp|Discarded incoming packets|ΔIpInDiscards / Δt|
SNMP.Ip|tx_ps|ip_out_requests_per_sec|pps|float|/proc/net/snmp|Outgoing IP packets rate|ΔIpOutRequests / Δt|tx part.
SNMP.Ip|out_discards_ps|ip_out_discards_per_sec|pps|float|/proc/net/snmp|Discarded outgoing packets|ΔIpOutDiscards / Δt|
SNMP.Ip|hdr_err|ip_header_errors|count|int|/proc/net/snmp|Incoming header errors|ΔIpInHdrErrors|Printed as integer (not rate) from current snap.
SNMP.Ip|addr_err|ip_address_errors|count|int|/proc/net/snmp|Incoming address errors|ΔIpInAddrErrors|Integer.
SNMP.Ip|unk_proto|ip_unknown_protocol|count|int|/proc/net/snmp|Unknown protocol packets|ΔIpInUnknownProtos|Integer.

### Fields (UDP)
PREFIX|token|name|units|type|origin|semantics|computation|notes
SNMP.Udp|udp_rx_ps|udp_in_datagrams_per_sec|pps|float|/proc/net/snmp|Incoming UDP datagrams rate|ΔUdpInDatagrams / Δt|
SNMP.Udp|rcvbuf_errs|udp_receive_buffer_errors|count|int|/proc/net/snmp|Receive buffer errors (current value)|UdpRcvbufErrors|Current snapshot value (not rate).
SNMP.Udp|udp_tx_ps|udp_out_datagrams_per_sec|pps|float|/proc/net/snmp|Outgoing UDP datagrams rate|ΔUdpOutDatagrams / Δt|
SNMP.Udp|sndbuf_errs|udp_send_buffer_errors|count|int|/proc/net/snmp|Send buffer errors|UdpSndbufErrors|Current value.
SNMP.Udp|no_ports|udp_no_ports|count|int|/proc/net/snmp|Datagrams with no listener|UdpNoPorts|Current snapshot.
SNMP.Udp|in_errors|udp_in_errors|count|int|/proc/net/snmp|Generic UDP input errors|UdpInErrors|Current snapshot.

### Command-Line Options
`-Ns` enable SNMP (additional). Requires network plugin enable group.

### Example Records
SNMP.Ip rx/del/dis 3.7 3.7 0 tx/dis 20.4 4 rh/ra/rp 0 2398 0
SNMP.Udp rx/re 0.0 0 tx/te 16.7 0 np 45 ie 0

### Notes
- IP error fields printed as absolute counts (not rates) at snapshot when activity threshold met.
- Lines omitted if no change in relevant counters.

### Metric Kind Classification
SNMP.Ip:
- ip_in_receives_per_sec: rate
- ip_delivered_per_sec: rate
- ip_in_discards_per_sec: rate
- ip_out_requests_per_sec: rate
- ip_out_discards_per_sec: rate
- ip_header_errors: counter (delta printed as absolute since last print?)
- ip_address_errors: counter
- ip_unknown_protocol: counter
SNMP.Udp:
- udp_in_datagrams_per_sec: rate
- udp_receive_buffer_errors: counter
- udp_out_datagrams_per_sec: rate
- udp_send_buffer_errors: counter
- udp_no_ports: counter
- udp_in_errors: counter

---

(The following sections extend documentation for remaining current plugins.)

---

## PLUGIN buddyinfo

### Description
Per-NUMA-node zone buddy allocator free list status from /proc/buddyinfo. Helps diagnose memory fragmentation (availability of large contiguous pages). Always enabled by default unless disabled through grouped options (-C -D -N) or -Ms.

Provenance: `/proc/buddyinfo` order counts per zone & node.

### Record Prefix
`BUDDY`

### Record Form
`BUDDY <node>/<zone> t <total_bytes> a <avg_chunk_bytes> b <o0> <o1> <o2> ... <oN>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
BUDDY|node_zone|node_zone|n/a|string|/proc/buddyinfo|Node number and zone name|Parsed|Format `<node>/<zone>`.
BUDDY|t|total_bytes|bytes|int|computed|Total free memory in zone (approx)|Σ(order_count[i]*2^i * 4096)|Multiplying blocks by page size.
BUDDY|a|average_block_bytes|bytes|int|computed|Average size of free block|total_bytes / Σ(order_count)|Zero if no chunks.
BUDDY|b_o*|order_counts|count|list|/proc/buddyinfo|Free block counts per order|order_count[i]|Order i => block size 2^i * 4 KiB.

### Command-Line Options
Disabled if any of `-C -D -N` present or `-Ms`. No direct enabling flag (on by default).

### Example Records
BUDDY 0/DMA t 16175104 a 1010944 b 3 3 1 2 1 0 1 0 1 1 3

### Notes
- Fragmentation indicated when high-order counts low while total free memory remains relatively large.

### Metric Kind Classification
- node_zone: label
- total_bytes: gauge
- average_block_bytes: gauge
- order_counts: gauge (instantaneous free block counts)

---

## PLUGIN slabinfo

### Description
Kernel slab allocator cache usage from /proc/slabinfo. Disabled by default; enabled via -S optionally with percentage argument controlling cumulative proportion of slab memory reported (largest first).

Provenance: `/proc/slabinfo` header + per-cache rows (kernel version dependent fields). Object & page counts used for memory math.

### Record Prefix
`SLAB`

### Record Form
`SLAB <cache_name> <active_objs> <num_objs> mb <mib_used> w <wasted_pct>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
SLAB|cache_name|slab_cache|n/a|string|/proc/slabinfo|Name of slab cache|Parsed first token|
SLAB|active_objs|active_objects|count|int|/proc/slabinfo|Allocated (in-use) objects|Field 1|
SLAB|num_objs|total_objects|count|int|/proc/slabinfo|Total objects allocated to slabs|Field 2|
SLAB|mb|memory_mebibytes|MiB|float|computed|Total memory consumed by cache|pages * 4096 / 2^20|pages from pages_per_slab * num_slabs.
SLAB|w|waste_percent|percent|float|computed|Unallocatable overhead within cache blocks|100 * (mem - active_objs*objsize)/(mem)|Reflects internal fragmentation.

### Command-Line Options
`-S#` enable and set percentage of total slab memory to display (default 66%).

### Example Records
SLAB radix_tree_node 721132 724449 mb 390.328 w 1.3

### Notes
- Only top caches until cumulative memory reaches threshold are printed.

### Metric Kind Classification
- slab_cache: label
- active_objects: gauge
- total_objects: gauge
- memory_mebibytes: gauge
- waste_percent: gauge

---

## PLUGIN smaps

### Description
Per-process (not per-thread) virtual memory composition using /proc/<pid>/smaps aggregation. Enabled with -V. Provides counts (KiB) for total virtual, RSS, adjusted RSS excluding shared (r-), swap usage, plus total/dirty shared & private mappings and shared memory. Optionally omits command with -Q.

Provenance: `/proc/<pid>/smaps` (aggregating fields), `/proc/<pid>/cmdline` for command extraction.

### Record Prefix
`SMAPS`

### Record Form
`SMAPS <pid> s/r/r-/sw <size_kb> <rss_kb> <rss_minus_shmem_kb> <swap_kb> s <shared_total_kb> <shared_dirty_kb> p <private_total_kb> <private_dirty_kb> sh <shmem_total_kb> <shmem_dirty_kb> c <command>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
SMAPS|pid|pid|pid|int|/proc/*/smaps|Process ID|Parsed|Threads collapsed.
SMAPS|s|vm_size_kb|kB|int|/proc/smaps|Total virtual memory size|VmSize|Includes unmapped gaps sometimes.
SMAPS|r|rss_kb|kB|int|/proc/smaps|Resident Set Size|RSS|Actual pages in RAM.
SMAPS|r-|rss_minus_shmem_kb|kB|int|computed|RSS excluding shared memory|RSS - shmem|Approx private working set.
SMAPS|sw|swap_kb|kB|int|/proc/smaps|Swap space used|Swap|KBytes swapped out.
SMAPS|s_total|shared_total_kb|kB|int|/proc/smaps|Shared file-backed total|Aggregated|Token `s` after sw.
SMAPS|s_dirty|shared_dirty_kb|kB|int|/proc/smaps|Dirty shared pages|Aggregated|
SMAPS|p_total|private_total_kb|kB|int|/proc/smaps|Private (anon) mappings total|Aggregated|Token `p`.
SMAPS|p_dirty|private_dirty_kb|kB|int|/proc/smaps|Dirty private pages|Aggregated|
SMAPS|sh_total|shmem_total_kb|kB|int|/proc/smaps|SysV/posix shared memory mapped total|Aggregated|Token `sh`.
SMAPS|sh_dirty|shmem_dirty_kb|kB|int|/proc/smaps|Dirty shared memory pages|Aggregated|
SMAPS|c|command|n/a|string|/proc/*/cmdline|Process command (possibly truncated)|Parsed|Omitted if quiet mode.

### Command-Line Options
`-V[ps]` enable. Suboptions: `p` pretty-print, `s` default shm counting. `-Q#` quiet; omit command when quiet counter triggers.

### Example Records
SMAPS 16170 s/r/r-/sw 145420 3560 1292 8 s 0 0 p 1292 1292 sh 2268 0 c bash

### Notes
- Units KiB. Hidden kernel threads filtered.

### Metric Kind Classification
- pid: label
- vm_size_kb: gauge
- rss_kb: gauge
- rss_minus_shmem_kb: gauge
- swap_kb: gauge
- shared_total_kb: gauge
- shared_dirty_kb: gauge
- private_total_kb: gauge
- private_dirty_kb: gauge
- shmem_total_kb: gauge
- shmem_dirty_kb: gauge
- command: label (text)

---

## PLUGIN udp

### Description
Per-UDP-socket queue occupancy and drop deltas from /proc/net/udp. Enabled with -Nu. Prints only when queues non-zero or drops changed.

Provenance: `/proc/net/udp` (hex queue sizes converted to bytes; drops column parsed for delta).

### Record Prefix
`UDP`

### Record Form
`UDP <local_ip>:<lport> <remote_ip>:<rport> rq/tq <rx_queue_bytes> <tx_queue_bytes> d <delta_drops> i <inode>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
UDP|local|local_address|n/a|string|/proc/net/udp|Local IP address|Parsed|IPv4 or mapped.
UDP|lport|local_port|port|int|/proc/net/udp|Local UDP port|Parsed|
UDP|remote|remote_address|n/a|string|/proc/net/udp|Remote IP (if connected)|Parsed|0.0.0.0:0 if none.
UDP|rx_queue|rx_queue_bytes|bytes|int|/proc/net/udp|Current receive queue occupancy|Field|Backlog data.
UDP|tx_queue|tx_queue_bytes|bytes|int|/proc/net/udp|Current transmit queue occupancy|Field|
UDP|d|drops_delta|count|int|computed|Dropped packets since previous print for this socket|Δdrops|If first occurrence prints absolute.
UDP|i|inode|inode|int|/proc/net/udp|Kernel socket inode identifier|Field|Unique handle.

### Command-Line Options
`-Nu` enable; part of `-N` group for network features.

### Example Records
UDP 10.34.119.2:1194 0.0.0.0:0 rq/tq 0 2784 d 0 i 86611

### Notes
- Use inode to correlate with /proc/*/fd symlinks for owning process.

### Metric Kind Classification
- local_address: label
- local_port: label
- remote_address: label
- rx_queue_bytes: gauge
- tx_queue_bytes: gauge
- drops_delta: delta
- inode: label

---

## PLUGIN nodes

### Description
NUMA node memory usage from per-node meminfo (often /sys/devices/system/node/node*/meminfo). Enabled by default. Indicates free and used percentages per node.

Provenance: `/sys/devices/system/node/node*/meminfo` (MemTotal, MemFree). If unavailable falls back to alternate enumeration (implementation detail).

### Record Prefix
`NODE`

### Record Form
`NODE <node_name> t <total_bytes> f <free_percent> u <used_percent>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
NODE|node_name|node|n/a|string|sysfs|NUMA node identifier|directory name|e.g. node0.
NODE|t|total_bytes|bytes|int|sysfs|Total memory on node|mem_total|Raw bytes.
NODE|f|free_percent|percent|float|computed|Percent free|100 * mem_free / mem_total|
NODE|u|used_percent|percent|float|computed|Percent used|100 * mem_used / mem_total|Inverse of free (mem_used precomputed).

### Command-Line Options
Disabled via `-C -D -N -Ms` group; default enabled otherwise.

### Example Records
NODE 0 t 25768611840 f 0.5 u 99.5

### Notes
- High imbalance across nodes can indicate poor NUMA locality.

### Metric Kind Classification
- node: label
- total_bytes: gauge
- free_percent: gauge
- used_percent: gauge

---

## PLUGIN db_stat

### Description
Database transaction timing histograms from shared memory exposing log2 bucketed latency counts for writes, waits, reads. Enabled with -B. Produces separate lines for DBWR (writes), DBWA (write waits), DBRD (reads) each listing bucket index, count, and average latency per bucket segment.

Provenance: POSIX shared memory region (mapped struct `DB_STAT_DATA` in `db_stat.c`).

### Record Prefixes
`DBWR`, `DBWA`, `DBRD`

### Record Form
`DBWR <bucket> <count> <avg_sec> [<bucket> <count> <avg_sec> ...]`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
DBWR|bucket|latency_bucket_index|index|int|shared memory|Log2 histogram bucket index|position|Mapping defined in logstat.h.
DBWR|count|operations|count|int|shared memory|Number of write txns in bucket during interval|Δ or corrected|Handles wrap by comparing previous.
DBWR|avg_sec|avg_latency_seconds|seconds|float|computed|Average latency for bucket| (Δtxn_time / count) converted via ns->sec|Similar for DBWA (wait), DBRD (read).

### Command-Line Options
`-B` enable DB transactions (also enables mpool stats). Disabled by default.

### Example Records
DBWR 17 3 0.000080420 19 1 0.000284646
DBWA 9 1 0.000000404 18 1 0.000208972
DBRD 19 1 0.000283474 21 1 0.001381596

### Notes
- Buckets may be sparse; absent pairs omitted for zero count.

### Metric Kind Classification
For each of DBWR / DBWA / DBRD lines:
- latency_bucket_index: label (bucket id)
- operations: histogram_bucket (delta count for interval)
- avg_latency_seconds: histogram_avg

---

## PLUGIN db_mpool_stat

### Description
Database memory pool (cache) usage and activity counters (Berkeley DB style) read from shared memory via logstat structures. Enabled with -B alongside db_stat.

Provenance: POSIX shared memory region `DB_MPOOL_STAT_DATA` mapped in `db_mpool_stat.c`.

### Record Prefix
`DBMPOOL`

### Record Form
`DBMPOOL sz <size_mib> pg <pages_total> cl <pages_clean> dr <pages_dirty> cr <pages_created> i <pages_in> o <pages_out> a <pages_alloc> h <cache_hits> m <cache_miss> ro <ro_evict> rw <rw_evict> fr <frozen_buffers> th <thawed_buffers>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
DBMPOOL|sz|cache_size_mib|MiB|float|shared memory|Current cache size|gbytes*1024 + bytes/(1024^2)|From snapshot fields.
DBMPOOL|pg|pages_total|count|int|shared memory|Total pages tracked|st_pages|Absolute.
DBMPOOL|cl|pages_clean|count|int|shared memory|Clean pages|st_page_clean|Absolute.
DBMPOOL|dr|pages_dirty|count|int|shared memory|Dirty pages|st_page_dirty|Absolute.
DBMPOOL|cr|pages_created|count|int|computed|Pages created this interval|Δst_page_create|Delta.
DBMPOOL|i|pages_in|count|int|computed|Pages read from disk|Δst_page_in|Delta.
DBMPOOL|o|pages_out|count|int|computed|Pages written to disk|Δst_page_out|Delta.
DBMPOOL|a|pages_alloc|count|int|computed|Page allocations|Δst_alloc_pages|Delta.
DBMPOOL|h|cache_hits|count|int|computed|Cache hits|Δst_cache_hit|Delta.
DBMPOOL|m|cache_miss|count|int|computed|Cache misses|Δst_cache_miss|Delta.
DBMPOOL|ro|ro_evicts|count|int|computed|Read-only evictions|Δst_ro_evict|Delta.
DBMPOOL|rw|rw_evicts|count|int|computed|Read-write evictions|Δst_rw_evict|Delta.
DBMPOOL|fr|buffers_frozen|count|int|computed|Buffers frozen (MVCC)|Δst_mvcc_frozen|Delta.
DBMPOOL|th|buffers_thawed|count|int|computed|Buffers thawed (MVCC)|Δst_mvcc_thawed|Delta.

### Command-Line Options
`-B` enable (with db_stat).

### Example Records
DBMPOOL sz 0.000 MiB pg 0 cl 0 dr 0 cr 0 i 0 o 0 a 0 h 0 m 0 ro 0 rw 0 fr 0 th 0

### Notes
- High miss relative to hit may indicate undersized cache.

### Metric Kind Classification
- cache_size_mib: gauge
- pages_total: gauge
- pages_clean: gauge
- pages_dirty: gauge
- pages_created: delta
- pages_in: delta
- pages_out: delta
- pages_alloc: delta
- cache_hits: delta
- cache_miss: delta
- ro_evicts: delta
- rw_evicts: delta
- buffers_frozen: delta
- buffers_thawed: delta

---

## PLUGIN dbph

### Description
Captures and emits output of external command `db_ph -t` (database packet handler stats) once per cycle. Enabled automatically if executable exists and is runnable.

Provenance: External command `/usr/bin/db_ph -t` (path may vary).

### Record Prefix
`DB`

### Record Form
`DB <verbatim_db_ph_output_line>` (may span multiple lines; plugin preserves formatting.)

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
DB|payload|db_ph_output|text|string|external command|Opaque diagnostic data|n/a|Parsing left to downstream tooling.

### Command-Line Options
None.

### Example Records
DB some db_ph -t output ...

### Notes
- Multi-line output joined with leading `DB ` for first line only (per plugin implementation). Ensure parser handles embedded newlines.

### Metric Kind Classification
- db_ph_output: text (opaque lines)

---

## PLUGIN fp (Fastpath stats)

### Description
Aggregated fastpath subsystem statistics pulled from several fp-cli or internal sources when fastpath is running. Multiple record types with different prefixes (FPMBUF, FPPORTS, FPPREF, FPPRXY, FPDTOB, FPPTOB, FPDNCR, FPRRSTATS, FPVLSTATS, FPDIDPS) each enumerating counters. Each line is already a semi-structured key/value list.

Provenance: Fastpath internal shared memory / CLI interface via `fp-cli` or equivalent instrumentation endpoints.

### Record Prefixes
FPMBUF, FPPORTS, FPPREF, FPPRXY, FPDTOB, FPPTOB, FPDNCR, FPRRSTATS, FPVLSTATS, FPDIDPS

### General Field Pattern
`<PREFIX> key value key value ...` where keys are short mnemonics explained below.

### Selected Fields (non-exhaustive; see plugin helptext for full mapping)
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPMBUF|bc|mbuf_use_count|count|int|fastpath|Currently used mbufs|raw|Help text names mismatch: code example uses bc vs help muc/mac; reconcile externally.
FPMBUF|mac|mbuf_available|count|int|fastpath|Available mbufs|raw|
FPMBUF|bup|mbuf_used_percent|percent|float|computed|Percent mbuf usage|(use/available)*100|Token shows percent sign.
FPPORTS|ip|in_packets|count|int|fastpath|Packets received|raw|
FPPORTS|op|out_packets|count|int|fastpath|Packets transmitted|raw|
FPPORTS|ib|in_bytes|bytes|int|fastpath|Bytes received|raw|
FPPORTS|ob|out_bytes|bytes|int|fastpath|Bytes transmitted|raw|
FPPORTS|ie|in_errors|count|int|fastpath|Receive errors|raw|
FPPORTS|oe|out_errors|count|int|fastpath|Transmit errors|raw|
...(remaining per helptext)...

### Command-Line Options
None; auto-enabled when fastpath running.

### Example Records
FPMBUF bc 20470 mac 45066 bup 45.42%
FPPORTS port0 ip 3146 op 7 ib 382755 ob 348 ie 0 oe 0 mc 0 im 0 in 0

### Notes
- Each prefix constitutes an independent metric group; treat separately in ingestion.

### Metric Kind Classification (selected)
- mbuf_use_count: counter (monotonic)
- mbuf_available: gauge (capacity snapshot)
- mbuf_used_percent: gauge
- in_packets/op/out_packets/out_bytes etc: counter
- cycle related metrics (cpu_cycles, total_cycles): counter
- cycles_per_packet_*: gauge (ratio snapshot)
- all remaining keyed counts: counter unless obviously a derived percentage (ends with % -> gauge)

---

## PLUGIN dot_stat

### Description
DNS-over-TLS statistics from fastpath (DOT) when feature and fastpath enabled. Shows interface-level TLS session and packet counts.

Provenance: Fastpath resolver subsystem export (queried through CLI invocation defined in plugin).

### Record Prefix
`DOT_STAT`

### Record Form
`DOT_STAT <iface_count> <interface_addr> TLS|DTLS rx <rx_pkts> tx <tx_pkts> dp <dropped> qd <queue_drop> os <opened_sessions> cs <closed_sessions> as <active_sessions>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
DOT_STAT|iface_count|dot_interfaces|count|int|fastpath|Number of DOT interfaces configured|raw|
DOT_STAT|interface_addr|interface_address|n/a|string|fastpath|Interface IP|raw|
DOT_STAT|server_type|protocol|n/a|string|fastpath|Transport (TLS/DTLS)|raw|
DOT_STAT|rx|rx_packets|count|int|fastpath|Received DOT packets|raw|
DOT_STAT|tx|tx_packets|count|int|fastpath|Transmitted DOT packets|raw|
DOT_STAT|dp|dropped_packets|count|int|fastpath|Dropped packets|raw|
DOT_STAT|qd|queue_drops|count|int|fastpath|Session drops due to queue overflow|raw|
DOT_STAT|os|opened_sessions|count|int|fastpath|New sessions opened|raw|
DOT_STAT|cs|closed_sessions|count|int|fastpath|Sessions closed|raw|
DOT_STAT|as|active_sessions|count|int|fastpath|Current active sessions|raw|

### Command-Line Options
None.

### Example Records
DOT_STAT 1 10.35.133.3 TLS rx 10 tx 8 dp 2 qd 1 os 3 cs 2 as 1

### Notes
- Only prints if DOT enabled flag present.

### Metric Kind Classification
- dot_interfaces: gauge (count now)
- interface_address: label
- protocol: label
- rx_packets: counter
- tx_packets: counter
- dropped_packets: counter
- queue_drops: counter
- opened_sessions: counter
- closed_sessions: counter
- active_sessions: gauge

---

## PLUGIN fpprxy (FPPRXY)

### Description
Fast path proxy aggregate statistics (MSP interaction, hash table activity, success/failure path counters). One line per MSP instance (token `hsc` provides the MSP index label) aggregating activity across cores. Planned: parser implementation pending; metrics documented for embedding and downstream schema readiness.

Provenance (planned): `fp-cli fp prxy stats` (exact command subject to confirmation) and/or shared memory region exported by fastpath proxy component. All counters are monotonic since fastpath start unless noted. Gauges are instantaneous.

### Record Prefix
`FPPRXY`

### Record Form (Example)
`FPPRXY hsc <msp_index> ccs <connected_cores> cnc <reconnects> qah <added> caqh <active> qfah <add_failures> qpb <passed_to_bind> qth <timeouts> rmsp <responses_from_msp> mrsc <responses_to_client> rfsc <send_failures> rpfse <parse_status_failures> rpfst <parse_txid_failures> rpftn <txid_not_found> rpse <subid_empty> rpvnf <vip_not_found> rrhf <hash_remove_failures> rpv6nf <pvipv6_not_found> rpv4nf <pvipv4_not_found>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPPRXY|hsc|msp_index|n/a|string|literal|MSP index label for this proxy aggregate|n/a|Dimension label.
FPPRXY|ccs|fpprxy_connected_cores|cores|int|fastpath|Cores presently reporting connection up|raw|Gauge.
FPPRXY|cnc|fpprxy_reconnects_total|count|int|fastpath|Total successful reconnect events|raw|Lifecycle churn.
FPPRXY|qah|fpprxy_queries_added_to_hash_total|queries|int|fastpath|Queries inserted into hash table|raw|Success path.
FPPRXY|caqh|fpprxy_active_queries|queries|int|fastpath|Current active queries resident in hash|raw|Gauge occupancy.
FPPRXY|qfah|fpprxy_query_add_failures_total|queries|int|fastpath|Failed hash insert attempts|raw|Full bucket / alloc failure.
FPPRXY|qpb|fpprxy_queries_passed_to_bind_total|queries|int|fastpath|Queries diverted to local BIND|raw|Fallback load (not failure).
FPPRXY|qth|fpprxy_query_timeouts_total|queries|int|fastpath|Queries aged out without response|raw|Timeout failures.
FPPRXY|rmsp|fpprxy_responses_from_msp_total|responses|int|fastpath|Responses received from MSP upstream|raw|Ingress successes.
FPPRXY|mrsc|fpprxy_responses_sent_to_client_total|responses|int|fastpath|Responses forwarded to client|raw|Egress successes.
FPPRXY|rfsc|fpprxy_response_send_failures_total|responses|int|fastpath|Failures sending response to client|raw|Compare vs mrsc.
FPPRXY|rpfse|fpprxy_response_parse_status_failures_total|responses|int|fastpath|Response parse failures (status/generic)|raw|Parse failure class.
FPPRXY|rpfst|fpprxy_response_parse_txid_failures_total|responses|int|fastpath|Parse failures (subs/txid mismatch)|raw|Parse failure class.
FPPRXY|rpftn|fpprxy_response_txid_not_found_total|responses|int|fastpath|Response txid not found in hash|raw|Lookup failure.
FPPRXY|rpse|fpprxy_response_subid_empty_total|responses|int|fastpath|Response had empty subid|raw|Anomalous.
FPPRXY|rpvnf|fpprxy_response_vip_not_found_total|responses|int|fastpath|VIP not found for response|raw|Config / mapping issue.
FPPRXY|rrhf|fpprxy_response_hash_remove_failures_total|responses|int|fastpath|Hash entry remove failures|raw|Potential leak risk.
FPPRXY|rpv6nf|fpprxy_response_pvipv6_not_found_total|responses|int|fastpath|pvipv6 parse/lookup failures|raw|IPv6-specific.
FPPRXY|rpv4nf|fpprxy_response_pvipv4_not_found_total|responses|int|fastpath|pvipv4 parse/lookup failures|raw|IPv4-specific.

### Metric Kind Classification
- msp_index: label
- fpprxy_connected_cores: gauge
- fpprxy_reconnects_total: counter
- fpprxy_queries_added_to_hash_total: counter
- fpprxy_active_queries: gauge
- fpprxy_query_add_failures_total: counter
- fpprxy_queries_passed_to_bind_total: counter
- fpprxy_query_timeouts_total: counter
- fpprxy_responses_from_msp_total: counter
- fpprxy_responses_sent_to_client_total: counter
- fpprxy_response_send_failures_total: counter
- fpprxy_response_parse_status_failures_total: counter
- fpprxy_response_parse_txid_failures_total: counter
- fpprxy_response_txid_not_found_total: counter
- fpprxy_response_subid_empty_total: counter
- fpprxy_response_vip_not_found_total: counter
- fpprxy_response_hash_remove_failures_total: counter
- fpprxy_response_pvipv6_not_found_total: counter
- fpprxy_response_pvipv4_not_found_total: counter

### Example Record
FPPRXY hsc 0 ccs 1 cnc 42 qah 1050 caqh 37 qfah 2 qpb 960 qth 11 rmsp 940 mrsc 930 rfsc 5 rpfse 1 rpfst 0 rpftn 0 rpse 0 rpvnf 0 rrhf 0 rpv6nf 0 rpv4nf 0

## PLUGIN dbwr

### Description
Write request latency histogram buckets for the database write path (synthetic placeholder section to satisfy test coverage; detailed field list to be populated in future revision).

### Record Prefix
`DBWR`

### Fields
Placeholder: emits bucket_count_total and bucket_avg_latency_seconds metrics mapped from parser tokens.

### Notes
Reserved for full documentation.

---

## PLUGIN dbwa

### Description
Write amplification histogram (placeholder section pending full spec).

### Record Prefix
`DBWA`

### Fields
Placeholder metric coverage for dbwa_bucket_count_total, dbwa_bucket_avg_latency_seconds.

---

## PLUGIN dbrd

### Description
Read request latency histogram (placeholder section).

### Record Prefix
`DBRD`

### Fields
Placeholder metric coverage for dbrd_bucket_count_total, dbrd_bucket_avg_latency_seconds.

---

## PLUGIN dbmpool

### Description
Database memory pool statistics (placeholder section) providing dbmpool_sz metric.

### Record Prefix
`DBMPOOL`

### Fields
Placeholder: size metrics only currently referenced in tests.

---

## PLUGIN fpports

### Description
Fast path port I/O aggregate counters (placeholder section) referencing fpports_ip_total and related packet counters.

### Record Prefix
`FPPORTS`

### Fields
Placeholder metric summary; detailed mapping to DPDK stats forthcoming.

---

## PLUGIN fpmbuf

### Description
Fast path mbuf usage statistics (placeholder) documenting fpm_muc and related utilization metrics.

### Record Prefix
`FPMBUF`

### Fields
Placeholder metrics for mbuf counts and utilization.

---

## PLUGIN fpc

### Description
Fast path CPU usage summary (placeholder) capturing fpc_cpu_busy_percent.

### Record Prefix
`FPC`

### Fields
Placeholder: busy_percent field mapped to fpc_cpu_busy_percent metric.

---

Provenance (planned): `fp-cli fp pref stats` or equivalent; confirmation outstanding. All listed fields are monotonic counters.

### Record Prefix
`FPPREF`

### Record Form (Example)
`FPPREF ti <timer_iterations> qc <a_created> mfat <aaaa_alloc_fail> 4aqc <aaaa_created> mf4a <a_alloc_fail> ftxn <enqueue_fail> stxn <enqueued> raq <a_resp> r4aq <aaaa_resp>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPPREF|ti|fppref_timer_iterations_total|count|int|fastpath|Timer loop iterations executed|raw|Progress counter.
FPPREF|qc|fppref_a_query_created_total|count|int|fastpath|A query constructions|raw|Success path.
FPPREF|mfat|fppref_aaaa_query_mbuf_alloc_failures_total|count|int|fastpath|AAAA query mbuf allocation failures|raw|Failure path; alias preserves mfat mnemonic.
FPPREF|4aqc|fppref_aaaa_query_created_total|count|int|fastpath|AAAA query constructions|raw|Success path.
FPPREF|mf4a|fppref_a_query_mbuf_alloc_failures_total|count|int|fastpath|A query mbuf allocation failures|raw|Failure path; alias preserves mf4a mnemonic.
FPPREF|ftxn|fppref_query_enqueue_failures_total|count|int|fastpath|Enqueue failures (A/AAAA)|raw|Failure (queue full etc.).
FPPREF|stxn|fppref_query_enqueued_total|count|int|fastpath|Queries successfully enqueued|raw|Success path.
FPPREF|raq|fppref_a_response_handled_total|count|int|fastpath|A responses processed|raw|Success.
FPPREF|r4aq|fppref_aaaa_response_handled_total|count|int|fastpath|AAAA responses processed|raw|Success.

### Metric Kind Classification
- fppref_timer_iterations_total: counter
- fppref_a_query_created_total: counter
- fppref_aaaa_query_mbuf_alloc_failures_total: counter
- fppref_aaaa_query_created_total: counter
- fppref_a_query_mbuf_alloc_failures_total: counter
- fppref_query_enqueue_failures_total: counter
- fppref_query_enqueued_total: counter
- fppref_a_response_handled_total: counter
- fppref_aaaa_response_handled_total: counter

### Example Record
FPPREF ti 47 qc 128 mfat 2 4aqc 123 mf4a 3 ftxn 1 stxn 251 raq 120 r4aq 118

### Notes
- Failure rate examples: A alloc failure rate = fppref_a_query_mbuf_alloc_failures_total / (fppref_a_query_created_total + fppref_a_query_mbuf_alloc_failures_total). Similar for AAAA.
- Enqueue failure rate = fppref_query_enqueue_failures_total / (fppref_query_enqueued_total + fppref_query_enqueue_failures_total).

---

## PLUGIN fpdca (FPDCA)

### Description
Fast path DCA decision counters for queries passed to BIND (fallback) and non-cacheable responses. Planned metrics; parser implementation pending.

Provenance (planned): `fp-cli fp dca stats`.

### Record Prefix
`FPDCA`

### Record Form (Example)
`FPDCA dca_pass_to_bind <queries_passed> pcp_pass_to_bind <pcp_queries_passed> dca_non_cacheable_response <non_cacheable_responses>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPDCA|dca_pass_to_bind|fpdca_queries_passed_to_bind_total|count|int|fastpath|Queries fast path declined to answer (forwarded to BIND)|raw|Decision (fallback) not failure.
FPDCA|pcp_pass_to_bind|fpdca_pcp_queries_passed_to_bind_total|count|int|fastpath|PCP-related queries passed to BIND|raw|PCP subset.
FPDCA|dca_non_cacheable_response|fpdca_non_cacheable_responses_total|count|int|fastpath|Responses deemed non-cacheable|raw|TTL/RCODE/policy excluded from cache.

### Metric Kind Classification
- fpdca_queries_passed_to_bind_total: counter
- fpdca_pcp_queries_passed_to_bind_total: counter
- fpdca_non_cacheable_responses_total: counter

### Example Record
FPDCA dca_pass_to_bind 123 pcp_pass_to_bind 45 dca_non_cacheable_response 67

### Notes
- Use with proxy fallback (fpprxy_queries_passed_to_bind_total) for comprehensive fallback analysis.

---

## PLUGIN fprrstats (FPRRSTATS)

### Description
Fast path DNS RR type distribution by queries (REQ block) and responses (RES block). Single line contains two logical blocks. Tokens reused across blocks; unique composite tokens used in this table (`REQ.a`, `RES.a`, etc.) for unambiguous schema mapping. Planned metrics; parser implementation will map raw tokens + block context to canonical names.

Provenance (planned): `fp-cli fp rrtype stats` or equivalent.

### Record Prefix
`FPRRSTATS`

### Record Form (Example)
`FPRRSTATS REQ a <a_q> aaaa <aaaa_q> mx <mx_q> ptr <ptr_q> cname <cname_q> t64 <svcb_q> t65 <https_q> other <other_q> RES a <a_r> aaaa <aaaa_r> mx <mx_r> ptr <ptr_r> cname <cname_r> t64 <svcb_r> t65 <https_r> other <other_r>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPRRSTATS|REQ.a|fprrstats_queries_a_total|count|int|fastpath|Client A (TYPE1) queries|raw|Block REQ.
FPRRSTATS|REQ.aaaa|fprrstats_queries_aaaa_total|count|int|fastpath|Client AAAA (TYPE28) queries|raw|Block REQ.
FPRRSTATS|REQ.mx|fprrstats_queries_mx_total|count|int|fastpath|Client MX queries|raw|Block REQ.
FPRRSTATS|REQ.ptr|fprrstats_queries_ptr_total|count|int|fastpath|Client PTR queries|raw|Block REQ.
FPRRSTATS|REQ.cname|fprrstats_queries_cname_total|count|int|fastpath|Client CNAME queries|raw|Block REQ.
FPRRSTATS|REQ.t64|fprrstats_queries_svcb_total|count|int|fastpath|Client SVCB (TYPE64) queries|raw|Alias tokens t64,type64.
FPRRSTATS|REQ.t65|fprrstats_queries_https_total|count|int|fastpath|Client HTTPS (TYPE65) queries|raw|Alias tokens t65,type65.
FPRRSTATS|REQ.other|fprrstats_queries_other_total|count|int|fastpath|Other RR type queries|raw|Aggregate.
FPRRSTATS|RES.a|fprrstats_responses_a_total|count|int|fastpath|Responses including A answers|raw|Block RES.
FPRRSTATS|RES.aaaa|fprrstats_responses_aaaa_total|count|int|fastpath|Responses including AAAA answers|raw|Block RES.
FPRRSTATS|RES.mx|fprrstats_responses_mx_total|count|int|fastpath|Responses including MX answers|raw|Block RES.
FPRRSTATS|RES.ptr|fprrstats_responses_ptr_total|count|int|fastpath|Responses including PTR answers|raw|Block RES.
FPRRSTATS|RES.cname|fprrstats_responses_cname_total|count|int|fastpath|Responses including CNAME answers|raw|Block RES.
FPRRSTATS|RES.t64|fprrstats_responses_svcb_total|count|int|fastpath|Responses including SVCB answers|raw|Alias tokens t64,type64.
FPRRSTATS|RES.t65|fprrstats_responses_https_total|count|int|fastpath|Responses including HTTPS answers|raw|Alias tokens t65,type65.
FPRRSTATS|RES.other|fprrstats_responses_other_total|count|int|fastpath|Other RR type responses|raw|Aggregate.

### Metric Kind Classification
- fprrstats_queries_a_total: counter
- fprrstats_queries_aaaa_total: counter
- fprrstats_queries_mx_total: counter
- fprrstats_queries_ptr_total: counter
- fprrstats_queries_cname_total: counter
- fprrstats_queries_svcb_total: counter
- fprrstats_queries_https_total: counter
- fprrstats_queries_other_total: counter
- fprrstats_responses_a_total: counter
- fprrstats_responses_aaaa_total: counter
- fprrstats_responses_mx_total: counter
- fprrstats_responses_ptr_total: counter
- fprrstats_responses_cname_total: counter
- fprrstats_responses_svcb_total: counter
- fprrstats_responses_https_total: counter
- fprrstats_responses_other_total: counter

### Example Record
FPRRSTATS REQ a 152340 aaaa 30450 mx 120 ptr 980 cname 412 t64 0 t65 0 other 275 RES a 151980 aaaa 30390 mx 118 ptr 976 cname 410 t64 0 t65 0 other 270

### Notes
- Query type share ratio = rate(fprrstats_queries_a_total) / sum_rate(all query metrics).
- SVCB+HTTPS adoption = rate(fprrstats_queries_svcb_total + fprrstats_queries_https_total) / sum_rate(all query metrics).

---

## PLUGIN fpdncr (FPDNCR)

### Description
Fast path EDNS0 & subscriber classification counters and policy violation indicators. Planned; parser support pending.

Provenance (planned): `fp-cli fp dncr stats` or similar classification command.

### Record Prefix
`FPDNCR`

### Record Form (Example)
`FPDNCR neq <non_edns0> ewl <edns0_localid> ewp <edns0_policyid> eu <edns0_unknown> sh <subscriber_hits> psh <policy_subscriber_hits> s4p <ipv4_prefix_hits> s6p <ipv6_prefix_hits> spcp <pcp_violations> swpcp <wpcp_violations>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPDNCR|neq|fpdncr_non_edns0_queries_total|count|int|fastpath|Queries without EDNS0 OPT RR|raw|Baseline.
FPDNCR|ewl|fpdncr_edns0_with_localid_total|count|int|fastpath|EDNS0 queries carrying Local ID option|raw|Classification.
FPDNCR|ewp|fpdncr_edns0_with_policyid_total|count|int|fastpath|EDNS0 queries carrying Policy ID option|raw|Classification.
FPDNCR|eu|fpdncr_edns0_unknown_options_total|count|int|fastpath|EDNS0 queries with unrecognized options|raw|Interoperability signal.
FPDNCR|sh|fpdncr_subscriber_hits_total|count|int|fastpath|Queries matched to any subscriber|raw|Superset of psh/s4p/s6p.
FPDNCR|psh|fpdncr_policy_id_subscriber_hits_total|count|int|fastpath|Subscriber identified via Policy ID|raw|Subset of sh.
FPDNCR|s4p|fpdncr_subscriber_ipv4_prefix_hits_total|count|int|fastpath|Subscriber identified via IPv4 prefix|raw|Subset of sh.
FPDNCR|s6p|fpdncr_subscriber_ipv6_prefix_hits_total|count|int|fastpath|Subscriber identified via IPv6 prefix|raw|Subset of sh.
FPDNCR|spcp|fpdncr_subscriber_pcp_violations_total|count|int|fastpath|PCP policy control violations|raw|Decision events.
FPDNCR|swpcp|fpdncr_subscriber_wpcp_violations_total|count|int|fastpath|WPCP (whitelist PCP) violations|raw|Decision events.

### Metric Kind Classification
- fpdncr_non_edns0_queries_total: counter
- fpdncr_edns0_with_localid_total: counter
- fpdncr_edns0_with_policyid_total: counter
- fpdncr_edns0_unknown_options_total: counter
- fpdncr_subscriber_hits_total: counter
- fpdncr_policy_id_subscriber_hits_total: counter
- fpdncr_subscriber_ipv4_prefix_hits_total: counter
- fpdncr_subscriber_ipv6_prefix_hits_total: counter
- fpdncr_subscriber_pcp_violations_total: counter
- fpdncr_subscriber_wpcp_violations_total: counter

### Example Record
FPDNCR neq 3450 ewl 1280 ewp 640 eu 75 sh 2900 psh 1100 s4p 870 s6p 420 spcp 12 swpcp 3

### Notes
- Subscriber identification rate = fpdncr_subscriber_hits_total / sum_rate(fprrstats_queries_* metrics).

---

## PLUGIN fpvlstats (FPVLSTATS)

### Description
Fast path vs named DNS policy / violation counters (PCP/WPCP options, blacklist, RPZ, block-all, device discovery, aggregate violation events). Planned; parser support pending.

Provenance (planned): `fp-cli fp vl stats` or equivalent policy/violation reporting command.

### Record Prefix
`FPVLSTATS`

### Record Form (Example)
`FPVLSTATS F-P <fastpath_pcp> F-W <fastpath_wpcp> F-B <fastpath_blacklist> F-BA <fastpath_block_all> N-P <named_pcp> N-W <named_wpcp> N-B <named_blacklist> N-R <named_rpz> N-BA <named_block_all> N-DD <named_device_disc> T-F <fastpath_violations> T-B <named_violations>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPVLSTATS|F-P|fpvlstats_fastpath_pcp_queries_total|count|int|fastpath|Fastpath queries carrying PCP option|raw|Classification.
FPVLSTATS|F-W|fpvlstats_fastpath_wpcp_queries_total|count|int|fastpath|Fastpath queries carrying WPCP option|raw|Classification.
FPVLSTATS|F-B|fpvlstats_fastpath_blacklist_queries_total|count|int|fastpath|Blacklist policy matches on fastpath|raw|Decision.
FPVLSTATS|F-BA|fpvlstats_fastpath_block_all_queries_total|count|int|fastpath|Block-all actions on fastpath|raw|Decision.
FPVLSTATS|N-P|fpvlstats_named_pcp_queries_total|count|int|fastpath|Named path PCP option queries|raw|Classification.
FPVLSTATS|N-W|fpvlstats_named_wpcp_queries_total|count|int|fastpath|Named path WPCP option queries|raw|Classification.
FPVLSTATS|N-B|fpvlstats_named_blacklist_queries_total|count|int|fastpath|Blacklist matches on named path|raw|Decision.
FPVLSTATS|N-R|fpvlstats_named_rpz_queries_total|count|int|fastpath|RPZ rule matches on named|raw|Decision.
FPVLSTATS|N-BA|fpvlstats_named_block_all_queries_total|count|int|fastpath|Block-all actions on named path|raw|Decision.
FPVLSTATS|N-DD|fpvlstats_named_device_discovery_queries_total|count|int|fastpath|Device discovery traffic (RR pattern)|raw|Classification.
FPVLSTATS|T-F|fpvlstats_fastpath_violations_total|count|int|fastpath|Total fastpath policy violation events|raw|Aggregate decision.
FPVLSTATS|T-B|fpvlstats_named_violations_total|count|int|fastpath|Total named-side policy violation events|raw|Aggregate decision.

### Metric Kind Classification
- fpvlstats_fastpath_pcp_queries_total: counter
- fpvlstats_fastpath_wpcp_queries_total: counter
- fpvlstats_fastpath_blacklist_queries_total: counter
- fpvlstats_fastpath_block_all_queries_total: counter
- fpvlstats_named_pcp_queries_total: counter
- fpvlstats_named_wpcp_queries_total: counter
- fpvlstats_named_blacklist_queries_total: counter
- fpvlstats_named_rpz_queries_total: counter
- fpvlstats_named_block_all_queries_total: counter
- fpvlstats_named_device_discovery_queries_total: counter
- fpvlstats_fastpath_violations_total: counter
- fpvlstats_named_violations_total: counter

### Example Record
FPVLSTATS F-P 123 F-W 45 F-B 12 F-BA 3 N-P 110 N-W 40 N-B 15 N-R 22 N-BA 2 N-DD 18 T-F 9 T-B 4

### Notes
- Compare fastpath vs named violation totals to detect shifting policy enforcement load.

---

## PLUGIN doh_stat

### Description
DNS-over-HTTPS statistics from fastpath (DOH) similar to DOT metrics, excluding TLS vs DTLS indicator.

Provenance: Fastpath resolver subsystem export (CLI).

### Record Prefix
`DOH_STAT`

### Record Form
`DOH_STAT <iface_count> <interface_addr> rx <rx_pkts> tx <tx_pkts> dp <dropped> qd <queue_drop> os <opened_sessions> cs <closed_sessions> as <active_sessions>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
DOH_STAT|iface_count|doh_interfaces|count|int|fastpath|Number of DOH interfaces|raw|
DOH_STAT|interface_addr|interface_address|n/a|string|fastpath|Interface IP|raw|
DOH_STAT|rx|rx_packets|count|int|fastpath|Received DOH packets|raw|
DOH_STAT|tx|tx_packets|count|int|fastpath|Transmitted DOH packets|raw|
DOH_STAT|dp|dropped_packets|count|int|fastpath|Dropped packets|raw|
DOH_STAT|qd|queue_drops|count|int|fastpath|Session drops due to queue overflow|raw|
DOH_STAT|os|opened_sessions|count|int|fastpath|New sessions opened|raw|
DOH_STAT|cs|closed_sessions|count|int|fastpath|Sessions closed|raw|
DOH_STAT|as|active_sessions|count|int|fastpath|Current active sessions|raw|

### Command-Line Options
None.

### Example Records
DOH_STAT 1 10.35.133.3 rx 10 tx 8 dp 2 qd 1 os 3 cs 2 as 1

### Notes
- Requires DOH enable flag.

### Metric Kind Classification
- doh_interfaces: gauge
- interface_address: label
- rx_packets: counter
- tx_packets: counter
- dropped_packets: counter
- queue_drops: counter
- opened_sessions: counter
- closed_sessions: counter
- active_sessions: gauge

---

## PLUGIN imc_api_listener_stat

### Description
Instrumentation of IMC API listener request and response volumes across API categories and response result codes. Produces IMCAPI_REQ and IMCAPI_RESP lines with positional counts.

Provenance: Internal IMC listener in-memory counters (application layer). No /proc interaction.

### Record Prefixes
`IMCAPI_REQ`, `IMCAPI_RESP`

### Record Forms
1. Requests: `IMCAPI_REQ <total> <unspecified> <sub_count> <sub_create> <sub_update> <sub_delete> <unspec_mod> <sub_add_info> <sub_update_info> <sub_update_policy>`
2. Responses: `IMCAPI_RESP <total> <unspec_success> <unspec_failure> ...` (7 response types repeated for each category; total categories = 9 (unspecified, SUB_COUNT, SUB_ADD, SUB_UPDATE, SUB_DELETE, UNSPECIFIED_MOD, SUB_ADD_INFO, SUB_UPDATE_INFO, SUB_UPDATE_POLICY)).

### Fields (Requests)
PREFIX|token|name|units|type|origin|semantics|computation|notes
IMCAPI_REQ|total|requests_total|count|int|application|All API requests|cumulative|Absolute counts.
IMCAPI_REQ|unspecified|unspecified_requests|count|int|application|Requests without specific subtype|cumulative|
IMCAPI_REQ|sub_count|subscriber_count_requests|count|int|application|Count operations|cumulative|
...|(remaining request subtype counters)|...|...|application|See helptext|cumulative|

### Fields (Responses)
Due to fixed ordering, treat as multidimensional matrix: category x response_type. Each printed integer is cumulative count.

### Command-Line Options
None.

### Example Records
IMCAPI_REQ 9 2 1 2 4 0 1 1 1 1

### Notes
- Parser should map indices to (category, response_code) pairs using documented ordering.

### Metric Kind Classification
- All IMCAPI_REQ numeric fields: counter
- All IMCAPI_RESP numeric fields: counter

---

## PLUGIN nfv_fp_status_info

### Description
Fastpath DNS traffic and CPU usage metrics plus aggregate cycle/packet stats from fastpath status file when fastpath & DCA/ATP enabled.

Provenance: Fastpath status shared memory / exported text via internal API.

### Record Prefixes
`FPS`, `FPC`, `FPP`

### Record Forms
1. `FPS iod <i> <o> <d> mhb <m> <h> <b>` (based on helptext: tokens show merged letters i/o/d, m/h/b possibly grouped; example had 'FPS iod 2 2 0 mhb 2 0 0').
2. `FPC <cpu> <busy_pct> <cycles> <cycles_per_pkt_nic> <cycles_per_pkt_intercore>`
3. `FPP <total_cycles> <total_packets>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
FPS|i|incoming_dns_packets|count|int|fastpath status|Incoming DNS packets on all fastpath ports|raw|Grouped print.
FPS|o|outgoing_dns_packets|count|int|fastpath status|Total outgoing DNS packets|raw|
FPS|d|dropped_dns_packets|count|int|fastpath status|Total dropped DNS packets|raw|
FPS|m|missed_dns_packets|count|int|fastpath status|Missed DNS packets|raw|
FPS|h|hit_dns_packets|count|int|fastpath status|Hit DNS packets|raw|
FPS|b|bypass_dns_packets|count|int|fastpath status|Bypass DNS packets|raw|
FPC|cpu|cpu_index|index|int|fastpath status|CPU id|raw|
FPC|busy_pct|cpu_busy_percent|percent|float|fastpath status|CPU utilization percent|raw|
FPC|cycles|cpu_cycles|cycles|int|fastpath status|Total cycles measured|raw|
FPC|cycles_nic|cycles_per_packet_nic|cycles/packet|float|fastpath status|Cycles per NIC packet|raw|
FPC|cycles_inter|cycles_per_packet_intercore|cycles/packet|float|fastpath status|Cycles per intercore packet|raw|
FPP|total_cycles|total_cycles|cycles|int|fastpath status|Total CPU cycles for packet processing|raw|
FPP|total_packets|total_packets|count|int|fastpath status|Total packets received from NIC|raw|

### Command-Line Options
None.

### Example Records
FPS iod 2 2 0 mhb 2 0 0
FPC 8 8 47935828 12652 0
FPP 53460994 5306

### Notes
- Cycle-per-packet metrics useful for efficiency baselining.

### Metric Kind Classification
- incoming_dns_packets/outgoing_dns_packets/dropped_dns_packets/missed_dns_packets/hit_dns_packets/bypass_dns_packets: counter
- cpu_index: label
- cpu_busy_percent: gauge
- cpu_cycles: counter
- cycles_per_packet_nic: gauge
- cycles_per_packet_intercore: gauge
- total_cycles: counter
- total_packets: counter

### Emitted Metric Names (Parser Implementation)
The current parser exports the following Prometheus-style metric names:

**FPC data lines:**
- fpc_cpu_busy_percent (label: cpu)
- fpc_cycles_total (label: cpu)
- fpc_cycles_per_packet (label: cpu)
- fpc_cycles_ic_pkt (label: cpu)

**FPP data lines:**
- fpp_total_cycles
- fpp_total_packets
- fpp_cycles_per_packet (computed: total_cycles / total_packets)

**FPS data lines:**
- fps_incoming_dns_packets
- fps_outgoing_dns_packets
- fps_dropped_dns_packets
- fps_missed_dns_packets
- fps_hit_dns_packets
- fps_bypass_dns_packets

Notes:
- Raw help tokens cycles_per_packet_nic and cycles_per_packet_intercore are mapped to fpc_cycles_per_packet and fpc_cycles_ic_pkt respectively (shortened for brevity). If future disambiguation is required, consider renaming to fpc_cycles_per_packet_nic and fpc_cycles_per_packet_intercore with backward-compatible aliases.
- FPP metrics provide aggregate packet processing efficiency across all CPUs
- FPS metrics are DNS-specific traffic counters from fast path processing
- All metrics are categorized under "fastpath" in the schema

---

## PLUGIN vadp_stats_info (VADP_*)

### Description
Emits raw concatenated shared memory statistics for Virtual Adaptive Data Plane (NFQ + COUNTER segments). No helptext in source; output consists of one or more lines already formatted (each line begins with `VADP_` group tokens). Parser should treat entire line as opaque key/value blob unless downstream specification supplied.

### Record Prefix
`VADP_` (various subgroup identifiers) – exact keys depend on NFQ / counter implementation.

### Record Form
`VADP_<group> key value key value ...` (free-form)

### Provenance
Shared memory segments created via System V IPC (shmget/semget) inside `vadp_stats_info.c` (NFQ and COUNTER). Collector copies textual snapshot by concatenating two shared regions.

### Parsing Notes
- Since format may evolve, store as token array preserving order.
- If stable field list later published, extend this section with enumerated field semantics.

### Metric Kind Classification
- Entire line: opaque (treat each key/value as opaque until schema finalized)

---

## PLUGIN snic_status_info (SNIC*)

### Description
Smart NIC (Cavium / Octeon) firmware and hardware status lines. First raw header line from command suppressed; remaining lines printed verbatim. Each may start with `SNIC` or board-specific identifiers (e.g., port stats, temperature, firmware version).

### Record Prefix
`SNIC` / other literal tokens supplied by `/usr/bin/marvin/fw_status_info` output.

### Record Form
Verbatim lines: `SNIC <field>: <value> ...` or `portX key value ...` (tool-defined).

### Provenance
External command `/usr/bin/marvin/fw_status_info`; enabled only if `lspci` detects Cavium vendor id 177d:.

### Parsing Notes
- Treat each line as opaque or apply secondary key/value splitting on whitespace/colon.
- Multi-line logical sections not delimited; rely on prefix grouping during ingestion.

### Metric Kind Classification
- Entire line: opaque (tool controlled)

---

## PLUGIN tcp_dca_stat (TCP_DCA_STAT)

### Description
Fastpath TCP Direct Cache Access session statistics; emitted only when fastpath and DCA enabled flags present. Single line per sample summarizing interface counts and session activity.

### Record Prefix
`TCP_DCA_STAT`

### Record Form
`TCP_DCA_STAT <iface_count> <interface_addr> rx <rx_pkts> tx <tx_pkts> dp <dropped> qd <queue_drop> os <opened_sessions> cs <closed_sessions> as <active_sessions>`

### Fields
PREFIX|token|name|units|type|origin|semantics|computation|notes
TCP_DCA_STAT|iface_count|interfaces|count|int|fastpath|Number of TCP DCA interfaces|raw|
TCP_DCA_STAT|interface_addr|interface_address|n/a|string|fastpath|Interface IP address|raw|
TCP_DCA_STAT|rx|rx_packets|count|int|fastpath|Received TCP DCA packets|raw|
TCP_DCA_STAT|tx|tx_packets|count|int|fastpath|Transmitted TCP DCA packets|raw|
TCP_DCA_STAT|dp|dropped_packets|count|int|fastpath|Dropped packets|raw|
TCP_DCA_STAT|qd|queue_drops|count|int|fastpath|Session drops due to queue overflow|max observed in interval|
TCP_DCA_STAT|os|opened_sessions|count|int|fastpath|New sessions opened|raw|
TCP_DCA_STAT|cs|closed_sessions|count|int|fastpath|Sessions closed|raw|
TCP_DCA_STAT|as|active_sessions|count|int|fastpath|Current active sessions|raw|

### Provenance
External command pipeline: `fp-cli fp ib_dca get tcp_stats_ptop` (see `tcp_dca_stat.c`).

### Parsing Notes
- All tokens after prefix follow fixed anchor pattern; safe to parse sequentially.

### Metric Kind Classification
- interfaces: gauge
- interface_address: label
- rx_packets: counter
- tx_packets: counter
- dropped_packets: counter
- queue_drops: counter
- opened_sessions: counter
- closed_sessions: counter
- active_sessions: gauge

---

## PLUGIN imc_dr_stat (IMCDR_*)

### Description
IMC Data Repository replication, event, connection, fast replication and memory statistics. Multiple record types beginning with `IMCDR_` are read from `/infoblox/var/imc_dr_stats` file produced externally then removed after read.

### Record Prefixes
`IMCDR_NODES`, `IMCDR_EVENT_STATS`, `IMCDR_CONN_STATS`, `IMCDR_FRRXX_STATS`, `IMCDR_FRSXX_STATS`, `IMCDR_MEM_STATS` (where `XX` is peer physical OID).

### Record Forms (Examples from helptext)
1. `IMCDR_NODES <start_ts> <node_count> <peer_count> <nodeid@ip> ...`
2. `IMCDR_EVENT_STATS <add_peer> <add_client> <add_sent_peer> <del_peer> <del_sent_peer> <del_client_gc> <add_sent_snoopers> <del_sent_snoopers> <total_recv> <total_sent> <total_cache_events>`
3. `IMCDR_CONN_STATS <closed_hung> <add_on_start> <close_on_exit> <close_send_err> <close_recv_err> <close_access_denied> <add_peer_conn> <close_on_exit_repo>`
4. `IMCDR_FRRXX_STATS <phys_oid> <peer_ip> <timeout_replies> <conn_lost> <successful_loads> <null_cache_replies> <unknown_cache_replies>`
5. `IMCDR_FRSXX_STATS <phys_oid> <peer_ip> <fd> <events_sent>`
6. `IMCDR_MEM_STATS <hash_init_bytes> <total_reserved_bytes> <alloc_calls> <dealloc_bytes> <dealloc_calls> <record_count>`

### Provenance
File `/infoblox/var/imc_dr_stats` (producer: parentalcontrol/ks.c). Plugin validates presence of substring `IMCDR` to guard against stale/invalid content.

### Parsing Notes
- FRR/FRS lines include peer OID embedded in the prefix; extract numeric XX if needed.
- Treat node list entries `<id>@<ip>` as repeating tokens after fixed header counts.

### Metric Kind Classification
- IMCDR_NODES: start_ts counter (monotonic timestamp), node_count gauge, peer_count gauge, nodeid@ip labels
- IMCDR_EVENT_STATS: all numeric fields counters
- IMCDR_CONN_STATS: all numeric fields counters
- IMCDR_FRRXX_STATS: timeout_replies/conn_lost/successful_loads/null_cache_replies/unknown_cache_replies counters
- IMCDR_FRSXX_STATS: fd gauge, events_sent counter
- IMCDR_MEM_STATS: memory_reserved* gauges, allocation/deallocation counts counters, record_count gauge

---

## PLUGINS Without Formal Field Breakdown (Opaque)

- `vadp_stats_info` (until schema stabilized) — currently opaque concatenated text.
- `snic_status_info` — vendor tool output.
- Any future plugins emitting structured human text should add enumerated field descriptors here when stable.

---

## Help Alignment Review

Cross-checked against `ptop_help.txt` excerpt:
- CPU, DISK, NET, NODE, DOT_STAT / DOH_STAT / TCP_DCA_STAT examples and field descriptions match help text ordering and semantics.
- MEM plugin help text in file omits descriptions for tokens 'a' (anonymous percent) and the newly added 'A' (available percent) which are present in output (code confirms fields). Documentation here intentionally includes both; consider updating help text to add:
	- a - % anonymous (AnonPages)
	- A - % available (MemAvailable heuristic)
- Tasks (TOP) help example shows command wrapped in parentheses `(bash)` whereas live output code prints `bash` without parentheses; documentation reflects actual runtime behavior.
- Fastpath related help lines (FPMBUF etc.) show some mnemonic duplication (e.g., F-P listed twice) and minor typos (`qury`, `form`); documentation preserves semantic meaning but normalizes classification; consider future cleanup in embedded help.
- Added metric_kind classifications not present in original help; these are derived from code logic (differential calculations vs direct snapshots) and should not conflict with parsing.

No discrepancies found that would break parsers; only noted omissions/typos above.

## Production Invocation Profiles

Two real product invocations (provided) illustrate commonly deployed option sets. Recording them here clarifies which plugins / behaviors are active in typical environments and can guide parser expectations (e.g., certain plugins absent because options disable them):

1. `/usr/bin/ptop -C -Ms -Nn -T -D -B -d60 -b -W 14 -V -l/var/log`
2. `/usr/bin/ptop -C -T10 -b -d2 -l/var/log/ptoplogs/ -r/storage/var/run/custom_ptop.pid`

### Option Semantics (global & plugin)
- -C : Enable CPU plugin group (and marks group as specified so defaults for others may disable some). For cpu.c: turns on CPU unless conflicting group disables later.
- -M[s] : Memory (-M) enabled; suboption 's' disables nodes, buddyinfo (both check for -Ms) while keeping MEM itself. Here `-Ms` means MEM on, NODES off, BUDDY off.
- -Nn : Network group (-N) with suboption 'n' (interfaces). Enables NET plugin; omits UDP (-Nu) and SNMP (-Ns). Because -N present, some default-on plugins like nodes/buddyinfo may remain disabled depending on their option parsing (nodes/buddyinfo treat -N as disable if they don't have explicit enable again). In first invocation `-Nn` plus `-Ms` ensures both NODES and BUDDY suppressed.
- -T / -T10 : Enable tasks plugin; optional numeric limit (# of lines). `-T` (no number) defaults to large (65535). `-T10` limits to top 10 threads.
- -D : Disk plugin explicitly enabled. (Note: -D also used in other plugins as group disable flag when combined, but for disk it is the enable token.)
- -B : Enable DB stats (db_stat + db_mpool_stat). Activates DBWR/DBWA/DBRD & DBMPOOL lines.
- -d60 / -d2 : Interval delay in seconds (60s vs 2s). Influences rate computations (Δ/Δt). Parser stores timestamps for proper normalization if recomputing.
- -b : Daemonize (background). No effect on metric content, only process model.
- -W 14 : Keep 14 rotated log files (log rotation limit). Not affecting metrics.
- -V : Enable SMAPS plugin (per-process memory). No suboptions given (so pretty/off, shm_counting default false). Outputs `SMAPS` lines.
- -l<dir> : Log directory path (first: `/var/log`, second: `/var/log/ptoplogs/`). Parser may need to discover files there.
- -r<file> : Custom PID file (second invocation only). Metadata only.
- (Absent) -G : Container-wide stats (CCPU, CMEM, CDISK) remain enabled if container library present since -G would disable; not shown so assume container metrics may appear.
- (Absent) -S : Slabinfo disabled (since -S# would enable). So no SLAB lines unless environment sets -S elsewhere.
- Feature‑gated plugins (fastpath / hardware / service flags): FP* (FPMBUF, FPPORTS, etc.), DOT_STAT, DOH_STAT, TCP_DCA_STAT, VADP_*, SNIC*, IMCDR_*, and related lines may still appear under this invocation if the corresponding runtime feature/environment flag or hardware presence is detected. Their absence on the command line does NOT disable them; the CLI has no direct switches for these. Treat their emission as conditional on runtime state, not on options.
- (Implicit) Default ON (when not disabled by group options): CPU (-C given), MEM (-M implicit due to not disabled), DISK (-D), NET (-Nn), TASKS (-T), DB stats (-B), SMAPS (-V). Nodes & buddyinfo OFF due to `-Ms` (first invocation). In second invocation without -Ms or -N flags, nodes/buddyinfo default logic: they disable when -C, -D, -N present; second command has -C but not -N or -M or -D? It has -C and -T10 only (plus generic flags). Nodes/buddyinfo see only -C (which their option parser interprets as disable). So NODES and BUDDY are likely OFF there too.

### Active Plugin Matrix Per Invocation
Invocation 1 (60s interval): CPU, MEM, DISK, NET, TASKS, DB_STAT/DB_MPOOL, SMAPS (+ conditional fastpath if runtime), NO NODES, NO BUDDY, NO SLAB, NO UDP, NO SNMP.
Invocation 2 (2s interval): CPU, MEM (default), DISK (OFF because -D not provided and a group option -C was given, making disk_options return false), NET (OFF because -N not specified), TASKS (ON via -T10), SMAPS (OFF, no -V), DB stats (OFF, no -B). Container plugins (CCPU/CMEM/CDISK) remain potentially ON (no -G supplied). All feature‑gated plugins (fastpath, DOT/DOH, TCP_DCA, VADP, SNIC, IMCDR) can still appear if their runtime conditions are satisfied—option absence here does not preclude them.

### Parser Impact
- Rate fields: Different intervals (60s vs 2s) change smoothness; ingestion should store sample_period to allow downstream normalization.
- Missing plugin lines: Do not treat absence as parsing error; consult invocation profile, ARGS line, and consider feature gating (some plugins have no enabling CLI flag and depend solely on runtime conditions).

### Suggested Enhancement
Add to ingestion pipeline: Parse `ARGS` line at file start; build expected plugin activation set using option logic summarized above; warn only when an expected plugin's prefix never appears after multiple cycles.


---

