#!/usr/bin/env bash
set -euo pipefail

PORT="${MCP_HTTP_PORT:-8085}"
export PORT
echo "[entrypoint] Starting MCP server (fastmcp) on :$PORT (ENABLE_TIMESCALE=$ENABLE_TIMESCALE TIMESCALE_DSN=$TIMESCALE_DSN)"
exec python -u -m mcp_server.mcp_app
