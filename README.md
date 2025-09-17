# PTOPS MCP Server

A Model Context Protocol (MCP) server for analyzing performance data from PTOPS logs. This server integrates with TimescaleDB for time-series analytics and provides tools for metric discovery, documentation search, and data analysis through Visual Studio Code and Claude Desktop.

## Architecture

- **TimescaleDB Integration**: Primary storage for time-series metrics with optimized hypertables
- **MCP Protocol**: HTTP-based Model Context Protocol for seamless integration with AI tools
- **Static Doc Embeddings**: JSONL-based documentation store with semantic search capabilities
- **Multi-Category Metrics**: CPU, Memory, Disk, Network, Process (TOP), SMAPS, Database metrics
- **Bundle Management**: Support bundle ingestion with tenant isolation and versioning

## Quick Start

### 1. Build the MCP Server

```bash
# Build Docker image
make build

# Or build manually
docker build -t perf-mcp-server:dev .
```

### 2. Run the MCP Server

```bash
# Run with Docker Compose (recommended - includes TimescaleDB)
make compose-up IMPORT_DIR=/path/to/your/data

# Or run standalone container
make run IMPORT_DIR=/path/to/your/data PORT=8085

# Or run locally for development
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
DEBUG_VERBOSE=1 LOG_LEVEL=DEBUG uvicorn mcp_server.server:app --reload --port 8085
```

### 3. Verify Installation

```bash
# Check health endpoint
curl http://localhost:8085/healthz

# Or test load bundle
curl -s -X POST http://localhost:8085/support/load_bundle \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"TICKET-123"}' | jq
```

## Client Setup

### Visual Studio Code Configuration

1. **Start the MCP Server**: Use one of the proper deployment methods:

```bash
# Recommended: Docker Compose (includes TimescaleDB)
make compose-up IMPORT_DIR=/path/to/your/data

# Alternative: Make run (standalone container)
make run IMPORT_DIR=/path/to/your/data PORT=8085
```

⚠️ **Important**: Do not run the container directly with `docker run`. Always use `docker compose` or `make run` with `IMPORT_DIR` to ensure proper volume mounting.

2. **Install MCP Extension**: Install an MCP-compatible extension in VS Code (such as the Claude extension)

3. **Configure MCP Server**: Add the following to your VS Code settings or extension configuration:

```json
{
    "servers": {    
        "ptops-analyzer": {
            "url": "http://localhost:8085/mcp",
            "type": "http"
        }
    },
    "inputs": []
}
```

4. **Verify Connection**: Use the command palette to test MCP tools or check that the server appears in your MCP client list

### Claude Desktop Configuration

1. **Start the MCP Server**: Use one of the proper deployment methods:

```bash
# Recommended: Docker Compose (includes TimescaleDB)
make compose-up IMPORT_DIR=/path/to/your/data

# Alternative: Make run (standalone container)
make run IMPORT_DIR=/path/to/your/data PORT=8085
```

2. **Locate Claude Config**: Find your Claude Desktop configuration file:
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **Linux**: `~/.config/Claude/claude_desktop_config.json`

3. **Add MCP Server**: Edit the configuration file to include:

```json
{
    "servers": {    
        "ptops-analyzer": {
            "url": "http://localhost:8085/mcp",
            "type": "http"
        }
    },
    "inputs": []
}
```

4. **Restart Claude Desktop**: Close and reopen Claude Desktop to load the new configuration

5. **Verify Installation**: In a Claude conversation, try using one of the MCP tools like `load_bundle` or `active_context`

### Alternative Configuration (Environment Variables)

You can also configure the server using environment variables:

```bash
export MCP_HTTP_PORT=8085
export SUPPORT_BASE_DIR=/path/to/support/bundles
export LOG_LEVEL=DEBUG
```

## Metric Categories & Data Schema

Each metric category is stored in optimized TimescaleDB hypertables with dedicated views for easy querying:

| Category | Purpose | Key Local Labels | Example Metrics |
|----------|---------|------------------|-----------------|
| CPU      | Per-CPU utilization & breakdown | `cpu_id`, `cpu_index` | `cpu_util_percent`, `cpu_user_percent` |
| TOP      | Per-process CPU & memory | `pid`, `ppid`, `exec`, `prio` | `process_cpu_percent`, `process_rss_kb` |
| SMAPS    | Per-process memory details | `pid`, `exec` | `smaps_rss_kb`, `smaps_swap_kb` |
| DISK     | Per-device I/O performance | `device_name`, `disk_index` | `disk_read_rate`, `disk_write_rate` |
| NET      | Network interface statistics | `interface`, `kind` | `net_rx_rate`, `net_tx_rate` |
| DB       | Database performance metrics | Various | `db_connections`, `db_query_time` |

### Filtering by Category

Ingestion can be limited to specific categories:

```bash
# Load only CPU and memory metrics
load_bundle {"path": "/data/bundle.tar.gz", "categories": ["CPU", "TOP", "SMAPS"]}
```

## Available MCP Tools

### Bundle Management
- **`load_bundle`**: Ingest and activate a support bundle
- **`active_context`**: Get current active bundle information
- **`list_bundles_tool`**: List all available bundles
- **`unload_bundle`**: Remove bundles from the system
- **`ingest_status`**: Check ingestion status and statistics

### Metric Discovery & Analysis
- **`metric_discover`**: Fast lexical search for metric names
- **`metric_search`**: Semantic search with auto-disambiguation
- **`metric_schema`**: Get schema details for specific metrics
- **`timescale_sql`**: Execute read-only SQL queries against TimescaleDB

### Documentation & Search
- **`search_docs`**: Lightweight document search
- **`search_docs_detail`**: Detailed document search with full text
- **`get_doc_tool`**: Retrieve specific documentation
- **`concepts`**: List available concept documentation
- **`workflow_help`**: Get workflow guidance

## Example Usage Prompts

### Loading and Exploring Data

```
Load a support bundle for analysis:
load_bundle {"tenant_id": "NIOSSPT1234"}

Check what's currently active:
active_context {}

See all available bundles:
list_bundles_tool {}
```

### Metric Discovery

```
Find CPU-related metrics:
metric_discover {"query": "cpu utilization percent", "top_k": 5}

Search for process memory metrics:
metric_search {"query": "process memory rss"}

Get schema for a specific metric:
metric_schema {"metric_name": "cpu_util_percent"}
```

### Data Analysis Queries

```
Analyze CPU utilization over time:
timescale_sql {
  "sql": "SELECT time_bucket('5 minutes', ts) as bucket, 
          AVG(value) as avg_cpu, MAX(value) as max_cpu 
          FROM cpu_util_percent 
          WHERE ts BETWEEN to_timestamp(1640995200) AND to_timestamp(1641000000)
          GROUP BY bucket ORDER BY bucket"
}

Find top processes by CPU usage:
timescale_sql {
  "sql": "SELECT exec, pid, AVG(value) as avg_cpu_percent 
          FROM process_cpu_percent 
          WHERE ts BETWEEN to_timestamp(1640995200) AND to_timestamp(1641000000)
          GROUP BY exec, pid 
          ORDER BY avg_cpu_percent DESC 
          LIMIT 10"
}

Memory usage trends:
timescale_sql {
  "sql": "SELECT time_bucket('1 hour', ts) as bucket,
          SUM(value)/1024/1024 as total_rss_gb
          FROM smaps_rss_kb
          WHERE ts BETWEEN to_timestamp(1640995200) AND to_timestamp(1641000000)
          GROUP BY bucket ORDER BY bucket"
}
```

### Advanced Analytics

```
Disk I/O patterns with percentiles:
timescale_sql {
  "sql": "SELECT device_name,
          percentile_disc(0.5) WITHIN GROUP (ORDER BY value) as median_read_rate,
          percentile_disc(0.95) WITHIN GROUP (ORDER BY value) as p95_read_rate,
          MAX(value) as max_read_rate
          FROM disk_read_rate 
          WHERE ts BETWEEN to_timestamp(1640995200) AND to_timestamp(1641000000)
          GROUP BY device_name"
}

Correlate CPU and memory usage:
timescale_sql {
  "sql": "WITH cpu_data AS (
            SELECT time_bucket('5 minutes', ts) as bucket, AVG(value) as avg_cpu
            FROM cpu_util_percent GROUP BY bucket
          ),
          mem_data AS (
            SELECT time_bucket('5 minutes', ts) as bucket, SUM(value)/1024/1024 as total_mem_gb
            FROM smaps_rss_kb GROUP BY bucket
          )
          SELECT c.bucket, c.avg_cpu, m.total_mem_gb
          FROM cpu_data c JOIN mem_data m ON c.bucket = m.bucket
          ORDER BY c.bucket"
}
```

### Documentation Search

```
Find documentation about specific topics:
search_docs {"query": "database connection pooling", "top_k": 3}

Get detailed documentation:
search_docs_detail {"query": "memory management", "semantic": true}

Look up specific concepts:
concepts {}
```

## Generic MCP Client Prompts

These are natural language prompts you can use with any MCP client (VS Code, Claude Desktop, etc.) that will automatically invoke the appropriate tools:

### Bundle Management & Setup

```
"Load a bundle from /data/support/NIOSSPT1234/xyz.tgz for 3 days of ptop logs"

"Load the latest support bundle for tenant NIOSSPT5678 and show me what metrics are available"

"Show me the current active bundle and its time range"

"List all available bundles and their processing status"

"Unload the current bundle and load a fresh one from the same tenant"
```

### Performance Analysis

```
"Analyze the top 5 average CPU usage processes in 1 hour time buckets between 2024-01-01 10:00 and 2024-01-01 18:00"

"Show me memory usage trends over the last 24 hours with 30-minute intervals"

"Find the highest disk I/O spikes and correlate them with CPU usage patterns"

"Correlate network RX traffic with CPU usage to identify potential bottlenecks"

"Show me the 95th percentile response times for all database operations"

"Identify processes consuming the most memory and their CPU impact"
```

### Metric Discovery & Exploration

```
"Find all CPU-related metrics available in the current dataset"

"Search for metrics related to process memory consumption"

"What network interface metrics are available for analysis?"

"Show me all available database performance metrics"

"Find metrics related to disk write operations and their schemas"
```

### Time-Series Analysis

```
"Create a time-series analysis of CPU utilization with 15-minute buckets for the entire dataset"

"Show memory usage patterns by process over time with hourly aggregation"

"Analyze network traffic patterns and identify peak usage periods"

"Generate a correlation analysis between disk I/O and system load"

"Create percentile analysis (50th, 90th, 95th) for database query response times"
```

### Troubleshooting & Investigation

```
"Identify the time periods with highest CPU usage and the processes responsible"

"Find memory leaks by analyzing processes with continuously increasing RSS"

"Investigate network anomalies by comparing RX/TX patterns across interfaces"

"Analyze disk I/O bottlenecks and their impact on system performance"

"Find processes that started during high-load periods"

"Correlate database connection spikes with system resource usage"
```

### Advanced Analytics

```
"Perform gap-fill analysis for missing metric data points with 5-minute intervals"

"Create a moving average analysis of CPU usage with a 1-hour window"

"Generate a heat map of resource usage patterns by hour of day"

"Calculate resource efficiency ratios (CPU vs memory vs disk I/O)"

"Identify outliers in system performance using statistical analysis"

"Create forecasting models based on historical resource usage trends"
```

### Documentation & Schema Exploration

```
"Search documentation for database connection pooling best practices"

"Find information about memory management configurations"

"Show me the schema and example queries for CPU utilization metrics"

"Get documentation about network interface monitoring"

"Find troubleshooting guides for high disk I/O scenarios"
```

### Example Workflows

```
"Load bundle NIOSSPT1234, find the top 3 CPU-intensive processes, and analyze their memory usage correlation over 4-hour windows"

"Investigate performance issues: load the latest bundle, identify peak CPU periods, correlate with network traffic, and show process details"

"Performance baseline: analyze CPU, memory, and disk I/O patterns over the full dataset with hourly aggregation and percentile analysis"

"Troubleshoot memory issues: find processes with high RSS, track their growth over time, and correlate with system load patterns"
```

## Troubleshooting

### Common Issues

| Problem | Solution |
|---------|----------|
| MCP tools not visible | Verify server is running: `curl http://localhost:8085/healthz` |
| Bundle load fails | Check path exists and is mounted in container: `IMPORT_DIR` |
| Empty metric results | Ensure bundle has data and correct categories are loaded |
| SQL query errors | Use `metric_schema` to check available columns and data types |
| Slow performance | Check performance optimization environment variables (see below) |

### Debug Mode

Enable verbose logging for troubleshooting:

```bash
export DEBUG_VERBOSE=1
export LOG_LEVEL=DEBUG
```

### Log Locations

- **Container logs**: `docker logs <container_name>`
- **Compose logs**: `make compose-logs`
- **Local development**: Console output with uvicorn

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HTTP_PORT` | `8085` | HTTP port for MCP server |
| `SUPPORT_BASE_DIR` | `/import/customer_data/support` | Base directory for support bundles |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARN, ERROR) |
| `DEBUG_VERBOSE` | `0` | Enable verbose debug output |
| `TIMESCALE_HOST` | `timescale` | TimescaleDB hostname |
| `TIMESCALE_PORT` | `5432` | TimescaleDB port |
| `TIMESCALE_DB` | `ptops` | TimescaleDB database name |
| `PTOPS_MAX_WORKERS` | `4` | Number of parallel workers for PTOPS file processing |
| `PTOPS_BATCH_SIZE` | `8000` | TimescaleDB batch size for bulk inserts |
| `PTOPS_INSERT_PAGE_SIZE` | `800` | PostgreSQL page size for execute_values |
| `PTOPS_PARALLEL_ENABLED` | `1` | Enable parallel file processing (0 to disable) |
| `PTOPS_USE_COPY_COMMAND` | `false` | Enable PostgreSQL COPY command for maximum performance |

## Performance Optimizations

### Parallel File Processing

The system automatically uses parallel processing for multiple PTOPS files to significantly improve loading performance:

- **Automatic Detection**: Parallel processing is enabled automatically when loading multiple files
- **Worker Threads**: Configurable number of worker threads (default: 4)
- **Thread Safety**: Safe concurrent access to database writers with proper locking
- **Performance Gain**: 4-6x faster loading for 10+ files

### PostgreSQL COPY Command (Advanced)

For maximum database performance, enable the PostgreSQL COPY command:

```bash
# Enable COPY command for bulk inserts
export PTOPS_USE_COPY_COMMAND=true
```

**Benefits:**
- **Maximum Performance**: COPY is the fastest way to insert bulk data into PostgreSQL
- **Reduced CPU**: Lower CPU overhead compared to individual INSERT statements
- **Memory Efficient**: Streams data directly to database without intermediate processing

**Considerations:**
- **Automatic Fallback**: If COPY fails, automatically falls back to INSERT method
- **Production Ready**: Safe for production use with comprehensive error handling
- **Configurable**: Can be enabled/disabled without code changes via environment variable

### Optimized Batch Sizes

Larger batch sizes reduce database round trips and improve throughput:

- **TimescaleDB**: Increased batch size from 2,000 to 8,000 rows, with optimized PostgreSQL page size for bulk inserts

### Configuration for Large Datasets

For large PTOPS datasets, tune these environment variables:

```bash
# Increase parallel processing
export PTOPS_MAX_WORKERS=8

# Enable COPY command for maximum performance
export PTOPS_USE_COPY_COMMAND=true

# Optimize TimescaleDB batch sizes for your hardware
export PTOPS_BATCH_SIZE=15000

# Fine-tune PostgreSQL bulk inserts
export PTOPS_INSERT_PAGE_SIZE=1500
```

### Performance Expectations

| File Count | Original Time | Optimized Time | With COPY | Speedup |
|------------|---------------|----------------|-----------|---------|
| 1 file     | 1 minute      | 1 minute       | 50 sec    | 1.2x    |
| 5 files    | 5 minutes     | 1.5 minutes    | 1 minute  | 5x      |
| 10 files   | 10+ minutes   | 2 minutes      | 1.5 min   | 7x+     |
| 20 files   | 20+ minutes   | 3-4 minutes    | 2-3 min   | 8x+     |

**Note**: Actual performance depends on file sizes, system resources, and database configuration.
