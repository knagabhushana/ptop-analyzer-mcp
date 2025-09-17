import os, logging, sys

logger = logging.getLogger("ptops_mcp")

def _ensure_logger():
    """Attach a basic StreamHandler if none present.

    We intentionally do this lazily so importing the module does not
    override host application logging configuration. Only when a debug
    message is actually emitted (DEBUG_VERBOSE=1) do we ensure a handler
    exists so the user sees output.
    """
    if logger.handlers:
        return
    level = logging.INFO
    logger.setLevel(level)
    h = logging.StreamHandler(stream=sys.stdout)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    h.setFormatter(fmt)
    logger.addHandler(h)

def dbg(msg: str):
    """Emit a debug info line when DEBUG_VERBOSE=1.

    Enable by exporting DEBUG_VERBOSE=1 before starting the MCP server or
    (if running in an already-started process) setting os.environ then
    triggering code paths again (e.g., re-running load_bundle with force=true).
    """
    if os.environ.get('DEBUG_VERBOSE') == '1':
        _ensure_logger()
        logger.info('[debug] %s', msg)
