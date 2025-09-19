"""Pytest bootstrap ensuring the in-repo mcp_server package is imported.

Without this, if an older installed version of mcp_server exists in site-packages,
pytest (especially when running a single test file directly) may resolve that one
first, hiding newly added attributes like fastpath_architecture and updated
SYSTEM_PROMPT text. We keep this file intentionally tiny.
"""

import os, sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    # Prepend so it wins over any site-packages installation
    sys.path.insert(0, REPO_ROOT)
