"""Mycel MCP server — read-only (Tier R) tools over the orchestrator socket.

Run as a stdio MCP server by the concierge's `claude -p` subprocess (via
``--mcp-config mycel_mcp.json``). Each tool is a thin round-trip to the
``ControlServer`` Unix socket owned by the live Discord-bot process, whose path
is supplied in the ``MYCEL_CONTROL_SOCKET`` environment variable.

The MCP stdio transport is newline-delimited JSON-RPC 2.0. We implement it
directly with the standard library so the server runs on the same Python 3.9
interpreter as the rest of Mycel (the official ``mcp`` SDK requires 3.10+).

Phase 1 exposes read-only operations only: status, logs, state and metrics.
No control or git tools are defined here, so the agent literally cannot mutate
anything through this server.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any, Dict, List

PROTOCOL_VERSION = "2024-11-05"
SOCKET_PATH = os.environ.get("MYCEL_CONTROL_SOCKET", os.path.join("bus", "agent", "control.sock"))

# Tool metadata (name -> (op, description, json-schema properties, required)).
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "mycel_status",
        "op": "global_status",
        "description": "Global Mycel dashboard: every forge's status, queue depth, agent health. "
                       "Use for broad questions like 'what's running?' or 'where did my run go?'.",
        "properties": {},
        "required": [],
    },
    {
        "name": "forge_status",
        "op": "forge_status",
        "description": "Detailed status of one forge (current step, outputs, git state). "
                       "Use when the user asks about a specific forge such as dev, bugfix or sentry.",
        "properties": {"forge": {"type": "string", "description": "Forge name, e.g. dev"}},
        "required": ["forge"],
    },
    {
        "name": "forge_log",
        "op": "log",
        "description": "Recent activity log for a forge. Use to answer 'why did <step> fail?' "
                       "or to inspect what happened during a run.",
        "properties": {
            "forge": {"type": "string", "description": "Forge name"},
            "limit": {"type": "integer", "description": "Max entries (default 20)"},
        },
        "required": ["forge"],
    },
    {
        "name": "list_forges",
        "op": "list_forges",
        "description": "List all forges with their workflow, default agent and invocation command.",
        "properties": {},
        "required": [],
    },
    {
        "name": "list_steps",
        "op": "list_steps",
        "description": "List all available workflow steps (spells) and what they do.",
        "properties": {},
        "required": [],
    },
    {
        "name": "metrics",
        "op": "metrics",
        "description": "Aggregate run metrics per forge/step (durations, token usage, counts).",
        "properties": {},
        "required": [],
    },
    {
        "name": "mcp_health",
        "op": "mcp_status",
        "description": "Health of the underlying Claude MCP servers (connected / needs auth / failed).",
        "properties": {},
        "required": [],
    },
]

_OP_BY_TOOL = {t["name"]: t["op"] for t in TOOLS}


def _call(op: str, args: Dict[str, Any]) -> str:
    """Send one op to the ControlServer and return the result as text."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
    except OSError as exc:
        return f"Mycel is not reachable (control socket {SOCKET_PATH}): {exc}"

    try:
        sock.sendall((json.dumps({"op": op, "args": args}) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()

    if not buf:
        return "Mycel returned an empty response."
    try:
        response = json.loads(buf.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"Mycel returned an invalid response: {exc}"
    if not response.get("ok"):
        return f"Error: {response.get('error', 'unknown error')}"
    return str(response.get("result", ""))


def _tools_list() -> List[Dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": {"type": "object", "properties": t["properties"], "required": t["required"]},
        }
        for t in TOOLS
    ]


def _handle(method: str, params: Dict[str, Any]) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mycel", "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tools_list()}
    if method == "tools/call":
        name = params.get("name")
        op = _OP_BY_TOOL.get(name)
        if op is None:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        text = _call(op, params.get("arguments") or {})
        return {"content": [{"type": "text", "text": text}]}
    raise ValueError(f"Method not found: {method}")


def main() -> None:
    # Explicit readline() loop, not `for line in sys.stdin`: the latter does
    # read-ahead buffering and won't yield a line until its buffer fills, which
    # deadlocks a request/response stdio protocol.
    while True:
        raw = sys.stdin.readline()
        if not raw:  # EOF — client closed the pipe
            break
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = request.get("id")
        method = request.get("method", "")
        # Notifications (no id) get no response.
        if msg_id is None:
            continue

        try:
            result = _handle(method, request.get("params") or {})
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except ValueError as exc:
            response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
