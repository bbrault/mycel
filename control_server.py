"""Local IPC server exposing read-only orchestrator queries over a Unix socket.

The conversational concierge runs Claude as a `claude -p` subprocess; that
subprocess talks to a stdio MCP server (``mycel_mcp_server.py``) which in turn
needs to reach the *live* orchestrator running inside the Discord bot process.
A Unix-domain socket bridges the two: the MCP server sends one JSON request per
line and reads one JSON response per line.

Phase 1 exposes Tier R (read-only) operations only. The dispatch table is the
single allow-list — any op not listed is rejected, so the socket cannot be used
to drive control/git actions before those tiers are deliberately added.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict

if TYPE_CHECKING:
    from mycel import Mycel

logger = logging.getLogger("mycel.control")

OpHandler = Callable[[Dict[str, Any]], Any]


class ControlServer:
    """AF_UNIX line-JSON server bridging the MCP server to the live Mycel."""

    def __init__(self, orchestrator: "Mycel", bus_dir: str = "bus") -> None:
        self.orchestrator = orchestrator
        self.socket_path = os.path.join(bus_dir, "agent", "control.sock")
        self._server: asyncio.AbstractServer | None = None
        # Tier R (read-only) allow-list. Later phases extend this table; an op
        # absent here is refused, which keeps the surface read-only by default.
        self._ops: Dict[str, OpHandler] = {
            "global_status": lambda a: self.orchestrator.get_global_status(),
            "forge_status": lambda a: self.orchestrator.get_forge_status(a["forge"]),
            "list_forges": lambda a: self.orchestrator.list_forges(),
            "list_steps": lambda a: self.orchestrator.list_spells(),
            "metrics": lambda a: self.orchestrator.get_metrics(),
            "log": lambda a: self.orchestrator.get_forge_log(a["forge"], int(a.get("limit", 20))),
            "mcp_status": lambda a: self.orchestrator.get_mcp_status(),
        }

    async def start(self) -> None:
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        # Remove a stale socket left by a previous (crashed) run.
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError as exc:
                logger.warning("Could not remove stale socket %s: %s", self.socket_path, exc)
        self._server = await asyncio.start_unix_server(self._handle_client, path=self.socket_path)
        logger.info("ControlServer listening on %s", self.socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        logger.info("ControlServer stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                response = await self._dispatch(line)
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error("ControlServer client error: %s", exc, exc_info=True)
        finally:
            writer.close()

    async def _dispatch(self, line: bytes) -> Dict[str, Any]:
        try:
            request = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {"ok": False, "error": f"invalid request: {exc}"}

        op = request.get("op")
        args = request.get("args") or {}
        handler = self._ops.get(op)
        if handler is None:
            return {"ok": False, "error": f"unknown op: {op!r}"}

        try:
            result = handler(args)
            if inspect.isawaitable(result):
                result = await result
            return {"ok": True, "result": result}
        except KeyError as exc:
            return {"ok": False, "error": f"missing argument: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.error("ControlServer op %s failed: %s", op, exc, exc_info=True)
            return {"ok": False, "error": str(exc)}
