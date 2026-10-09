"""VidLift MCP bridge package (binding spec: docs/designs/mcp-bridge.md).

Stdio-only local MCP server exposing the runner state contract 1:1
onto the same pipeline code the CLI/GUI use. No network listener, no
second implementation, no new policy inside the bridge.
"""

from .registry import JobRecord, Registry, RESTART_ERROR
from .jobs import JobExecutor
from .schemas import InvalidInput, TOOL_SCHEMAS, validate_tool_args
from .server import McpServer, serve_stdio
from .tools import BridgeServices, TOOL_HANDLERS

__all__ = [
    "BridgeServices",
    "InvalidInput",
    "JobExecutor",
    "JobRecord",
    "McpServer",
    "RESTART_ERROR",
    "Registry",
    "TOOL_HANDLERS",
    "TOOL_SCHEMAS",
    "serve_stdio",
    "validate_tool_args",
]