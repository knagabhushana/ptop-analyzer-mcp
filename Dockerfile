########## All-in-One PTOPS MCP Server Image ##########
# Base: Official Python slim image to avoid apt hash mismatch issues
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HTTP_PORT=8085 \
    ENABLE_TIMESCALE=1 \
    TIMESCALE_DSN=postgresql://tsdev:tsdev@timescaledb:5432/ptops

# Install runtime utilities only (no need for full build toolchain yet)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates sqlite3 tini; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies layer
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt --break-system-packages \
    && echo "[build] Installed Python deps system-wide (no venv)"

RUN mkdir -p /import/support /config /var/log/mcp
VOLUME ["/import/support"]

# Copy application code
COPY mcp_server ./mcp_server
COPY ptop ./ptop
COPY mcp_server/docs ./mcp_server/docs
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Build-time syntax verification (fail early on indentation / syntax errors)
RUN python -m py_compile mcp_server/mcp_app.py || (echo 'Syntax check failed for mcp_app.py' && exit 1)

EXPOSE 8085

ENTRYPOINT ["tini","--"]
CMD ["./docker-entrypoint.sh"]
# (Optional) For interactive debugging you can override entrypoint to /bin/bash at runtime.
