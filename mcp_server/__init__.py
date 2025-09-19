"""mcp_server package marker.

Historically re-exported fastpath_architecture for early import paths; no longer needed
since tests import mcp_server.mcp_app directly and sys.path issues are resolved.
"""

__all__: list[str] = []
