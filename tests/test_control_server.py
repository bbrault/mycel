from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

import pytest
import pytest_asyncio

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from control_server import ControlServer


class FakeMycel:
    """Minimal stand-in exposing the read methods ControlServer maps to."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_global_status(self) -> str:
        self.calls.append(("global_status",))
        return "GLOBAL"

    async def get_forge_status(self, forge: str) -> str:
        self.calls.append(("forge_status", forge))
        return f"STATUS:{forge}"

    def list_forges(self) -> str:
        self.calls.append(("list_forges",))
        return "FORGES"

    def list_spells(self) -> str:
        self.calls.append(("list_steps",))
        return "STEPS"

    def get_metrics(self) -> str:
        self.calls.append(("metrics",))
        return "METRICS"

    def get_forge_log(self, forge: str, limit: int) -> str:
        self.calls.append(("log", forge, limit))
        return f"LOG:{forge}:{limit}"

    async def get_mcp_status(self) -> str:
        self.calls.append(("mcp_status",))
        return "MCP"


async def _roundtrip(server: ControlServer, request: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(path=server.socket_path)
    writer.write((json.dumps(request) + "\n").encode("utf-8"))
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line.decode("utf-8"))


@pytest_asyncio.fixture
async def server():
    tmp = tempfile.mkdtemp()
    orchestrator = FakeMycel()
    srv = ControlServer(orchestrator, bus_dir=tmp)
    await srv.start()
    srv.orchestrator = orchestrator  # type: ignore[assignment]
    yield srv, orchestrator
    await srv.stop()


class TestControlServer:
    @pytest.mark.asyncio
    async def test_socket_created_on_start(self, server) -> None:
        srv, _ = server
        assert os.path.exists(srv.socket_path)

    @pytest.mark.asyncio
    async def test_global_status(self, server) -> None:
        srv, orch = server
        resp = await _roundtrip(srv, {"op": "global_status"})
        assert resp == {"ok": True, "result": "GLOBAL"}
        assert ("global_status",) in orch.calls

    @pytest.mark.asyncio
    async def test_forge_status_passes_arg(self, server) -> None:
        srv, orch = server
        resp = await _roundtrip(srv, {"op": "forge_status", "args": {"forge": "dev"}})
        assert resp == {"ok": True, "result": "STATUS:dev"}

    @pytest.mark.asyncio
    async def test_log_defaults_limit(self, server) -> None:
        srv, orch = server
        resp = await _roundtrip(srv, {"op": "log", "args": {"forge": "dev"}})
        assert resp["result"] == "LOG:dev:20"
        assert ("log", "dev", 20) in orch.calls

    @pytest.mark.asyncio
    async def test_sync_ops(self, server) -> None:
        srv, _ = server
        for op, expected in (("list_forges", "FORGES"), ("list_steps", "STEPS"), ("metrics", "METRICS")):
            resp = await _roundtrip(srv, {"op": op})
            assert resp == {"ok": True, "result": expected}

    @pytest.mark.asyncio
    async def test_unknown_op_rejected(self, server) -> None:
        srv, _ = server
        resp = await _roundtrip(srv, {"op": "abort", "args": {"forge": "dev"}})
        assert resp["ok"] is False
        assert "unknown op" in resp["error"]

    @pytest.mark.asyncio
    async def test_missing_argument(self, server) -> None:
        srv, _ = server
        resp = await _roundtrip(srv, {"op": "forge_status"})
        assert resp["ok"] is False
        assert "missing argument" in resp["error"]

    @pytest.mark.asyncio
    async def test_invalid_json(self, server) -> None:
        srv, _ = server
        reader, writer = await asyncio.open_unix_connection(path=srv.socket_path)
        writer.write(b"not json\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        resp = json.loads(line.decode("utf-8"))
        assert resp["ok"] is False
