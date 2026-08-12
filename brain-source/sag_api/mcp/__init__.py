"""MCP layer of sag - a source is an MCP endpoint, and the agent mounts as an MCP client.

- `server`: wraps one source's retrieval / entity / raw-text capabilities as an MCP server (for an
  external Claude Desktop / Cursor to mount, and for the in-process agent to reuse a warm engine).
- `mount`: mounts the Streamable-HTTP endpoint into FastAPI (`/mcp?source_id=...`).
- The client adapter lives in `sag_api.tools.mcp`: it adapts remote MCP tools to the unified `Tool` interface.
"""

from sag_api.mcp.server import MCPScope, build_source_mcp, use_scope

__all__ = ["MCPScope", "build_source_mcp", "use_scope"]
