IMAGE?=perf-mcp-server:dev
# Docker CLI (override with DOCKER=docker-rootless or podman if desired)
DOCKER?=docker
PORT?=8085
PY=python3
IMPORT_DIR?=/import
# Default logical location inside container where bundles live; override as needed.
SUPPORT_BASE_DIR?=/import/customer_data/support
DOCKER_ENV?=-e LOG_LEVEL=DEBUG -e DEBUG_VERBOSE=1 -e SUPPORT_BASE_DIR=$(SUPPORT_BASE_DIR) -e ENABLE_TIMESCALE=1

.PHONY: help build run run-import logs push clean venv compose-up compose-down compose-restart compose-logs clean-state test

help:
	@echo "Targets:"
	@echo "  build        Build docker image"
	@echo "  run          (legacy single-container) Run image directly mapping MCP $(PORT)->8085"
	@echo "  compose-up   docker compose up -d (timescaledb + mcp)"
	@echo "  compose-down Stop and remove compose services"
	@echo "  compose-restart Restart compose services"
	@echo "  compose-logs Tail MCP logs via compose"
	@echo "               Mounts IMPORT_DIR ($(IMPORT_DIR)) to /import with debug logging (Timescale enabled)"
	@echo "               Usage override: make run IMPORT_DIR=/abs/path PORT=$(PORT)"
	@echo "  run-import    (alias) Same as run"
	@echo "  push         Push image (requires IMG_REG)"
	@echo "  clean        Remove dangling images"
	@echo "  venv         Create local venv & install deps"
	@echo "  clean-state  Remove local runtime state DB (bundles.db)"
	@echo "  test         Clean state then run pytest with PTOPS_CLEAN_START=1"

build:
	$(DOCKER) build -t $(IMAGE) .

run:
	@if [ ! -d "$(IMPORT_DIR)" ]; then echo "IMPORT_DIR $(IMPORT_DIR) does not exist"; exit 1; fi
	@echo "[make] Running with host $(IMPORT_DIR) -> /import (SUPPORT_BASE_DIR=$(SUPPORT_BASE_DIR))"
	@echo "[make] Port mappings: MCP $(PORT):8085"
	$(DOCKER) run --rm \
		-p $(PORT):8085 \
		-v $(IMPORT_DIR):/import $(DOCKER_ENV) --name perf_mcp_run $(IMAGE)

run-import: run

compose-up:
	@if [ ! -d "$(IMPORT_DIR)" ]; then echo "IMPORT_DIR $(IMPORT_DIR) does not exist"; exit 1; fi
	@echo "[compose] Using host $(IMPORT_DIR) -> /import"
	IMPORT_DIR=$(IMPORT_DIR) docker compose -f docker-compose.timescale.yml up -d

compose-down:
	docker compose -f docker-compose.timescale.yml down

compose-restart:
	docker compose -f docker-compose.timescale.yml restart

compose-logs:
	docker compose -f docker-compose.timescale.yml logs -f mcp

logs:
	@$(DOCKER) logs -f perf_mcp_run


push:
	@if [ -z "$(IMG_REG)" ]; then echo "IMG_REG not set"; exit 1; fi
	$(DOCKER) tag $(IMAGE) $(IMG_REG)/$(IMAGE)
	$(DOCKER) push $(IMG_REG)/$(IMAGE)

clean:
	docker image prune -f

venv:
	$(PY) -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

# Remove runtime SQLite state (honors SQLITE_PATH env var if set, else default location)
clean-state:
	@STATE_FILE=$${SQLITE_PATH:-mcp_server/bundles.db}; \
	if [ -f "$$STATE_FILE" ]; then \
		printf "[clean-state] Removing %s\n" "$$STATE_FILE"; rm -f "$$STATE_FILE"; \
	else \
		printf "[clean-state] No state file at %s\n" "$$STATE_FILE"; \
	fi

# Run tests with a guaranteed clean state
# PTOPS_CLEAN_START=1 forces code to delete existing DB on first open (defensive) in addition to rm above
# Using python -m pytest allows venv shebang consistency
TEST_ENV=PTOPS_CLEAN_START=1

test: clean-state
	@echo "[test] Ensuring clean state and running pytest";
	$(TEST_ENV) pytest -q || (echo "[test] pytest failed" && exit 1)
