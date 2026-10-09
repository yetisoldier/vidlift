"""``vidlift mcp-server`` entry point (mcp-bridge.md Sections 1, 5.1).

Stdio-only MCP server for AI agents: spawned as a child process by the
MCP client (OpenClaw's ``mcp.servers.vidlift`` registration pattern),
speaking MCP framing on stdin/stdout. No network listener ever opens.

Server startup resolves NO media tools (Section 5.1): tool discovery
happens per job exactly as ``run_file`` does, so a bridge
misconfiguration surfaces as a job/tool error naming the tool, never a
silent hang. Registration as ``mcp.servers.vidlift`` happens only
after Karen's validation — never at build time.
"""

from __future__ import annotations

import argparse
import sys

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vidlift mcp-server",
        description=(
            "VidLift MCP bridge: stdio server exposing the runner state "
            "contract (probe, plan, benchmark, run, status, cancel, list, "
            "manifest) to a local MCP client"
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help=(
            "state directory for the job registry and benchmark cache "
            "(default: $VIDLIFT_STATE_DIR or ~/.vidlift)"
        ),
    )
    args = parser.parse_args(argv)

    from .mcp_bridge import serve_stdio

    try:
        serve_stdio(state_dir=args.state_dir)
    except KeyboardInterrupt:  # a client killing the process is a stop
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())