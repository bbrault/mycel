from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import concierge as concierge_mod
from concierge import Concierge


class FakeOrchestrator:
    def __init__(self, bus_dir: str) -> None:
        self.bus_dir = bus_dir
        self.control_socket_path = os.path.join(bus_dir, "agent", "control.sock")


class FakeResult:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class FakeRunner:
    def __init__(self, stdout: str = "All quiet.", returncode: int = 0) -> None:
        self._stdout = stdout
        self._returncode = returncode
        self.prompts: list[str] = []

    async def run(self, prompt: str, cwd: str = None) -> FakeResult:  # noqa: ARG002
        self.prompts.append(prompt)
        return FakeResult(self._stdout, self._returncode)


def _make(monkeypatch, stdout="All quiet.", returncode=0, config=None):
    tmp = tempfile.mkdtemp()
    orch = FakeOrchestrator(tmp)
    runner = FakeRunner(stdout, returncode)
    monkeypatch.setattr(concierge_mod, "get_runner", lambda *a, **k: runner)
    c = Concierge(orch, config if config is not None else {"enabled": True})
    return c, orch, runner, tmp


class TestConcierge:
    @pytest.mark.asyncio
    async def test_disabled_short_circuits(self, monkeypatch) -> None:
        c, _, runner, _ = _make(monkeypatch, config={"enabled": False})
        reply = await c.handle_message("123", "hi")
        assert "disabled" in reply.lower()
        assert runner.prompts == []  # runner never invoked

    @pytest.mark.asyncio
    async def test_reply_returned_and_persisted(self, monkeypatch) -> None:
        c, _, _, tmp = _make(monkeypatch, stdout="dev is idle.")
        reply = await c.handle_message("999", "what's up with dev?")
        assert reply == "dev is idle."

        path = os.path.join(tmp, "agent", "999.jsonl")
        assert os.path.exists(path)
        with open(path) as fh:
            turns = [json.loads(line) for line in fh if line.strip()]
        assert turns[0] == {"role": "user", "content": "what's up with dev?"}
        assert turns[1] == {"role": "assistant", "content": "dev is idle."}

    @pytest.mark.asyncio
    async def test_history_replayed_in_prompt(self, monkeypatch) -> None:
        c, _, runner, _ = _make(monkeypatch, stdout="ok")
        await c.handle_message("42", "first question")
        await c.handle_message("42", "second question")
        # Second prompt must contain the prior turn.
        assert "first question" in runner.prompts[1]
        assert "second question" in runner.prompts[1]

    @pytest.mark.asyncio
    async def test_socket_env_set(self, monkeypatch) -> None:
        c, orch, _, _ = _make(monkeypatch)
        await c.handle_message("7", "ping")
        assert os.environ["MYCEL_CONTROL_SOCKET"] == orch.control_socket_path

    @pytest.mark.asyncio
    async def test_failed_run_not_persisted(self, monkeypatch) -> None:
        c, _, _, tmp = _make(monkeypatch, stdout="", returncode=1)
        reply = await c.handle_message("5", "hello")
        assert "couldn't reach" in reply.lower()
        assert not os.path.exists(os.path.join(tmp, "agent", "5.jsonl"))
