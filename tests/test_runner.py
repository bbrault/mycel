from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runner import ClaudeRunner, CursorRunner, GeminiRunner, RunnerResult, get_runner, kill_proc_group


class TestRunnerResult:
    def test_success(self) -> None:
        r = RunnerResult(stdout="ok", stderr="", returncode=0, runner_used="claude")
        assert r.success is True
        assert r.output == "ok"

    def test_failure(self) -> None:
        r = RunnerResult(stdout="", stderr="error msg", returncode=1, runner_used="claude")
        assert r.success is False
        assert r.output == "error msg"

    def test_output_strips_whitespace(self) -> None:
        r = RunnerResult(stdout="  hello  \n", stderr="", returncode=0, runner_used="claude")
        assert r.output == "hello"

    def test_output_falls_back_to_stderr(self) -> None:
        r = RunnerResult(stdout="", stderr="fallback", returncode=0, runner_used="claude")
        assert r.output == "fallback"


class TestGetRunner:
    def test_claude_runner(self) -> None:
        runner = get_runner("claude")
        assert isinstance(runner, ClaudeRunner)

    def test_cursor_runner(self) -> None:
        runner = get_runner("cursor")
        assert isinstance(runner, CursorRunner)

    def test_default_is_claude(self) -> None:
        runner = get_runner("unknown")
        assert isinstance(runner, ClaudeRunner)

    def test_timeout_propagated(self) -> None:
        runner = get_runner("claude", timeout=300)
        assert runner.timeout == 300


class TestClaudeRunner:
    def test_available_reflects_path(self) -> None:
        runner = ClaudeRunner()
        # We can't guarantee claude is installed, but the property shouldn't crash
        assert isinstance(runner.available, bool)


class TestCursorRunner:
    def test_has_fallback(self) -> None:
        runner = CursorRunner()
        assert isinstance(runner._fallback, ClaudeRunner)


class TestGeminiRunner:
    def test_has_fallback(self) -> None:
        runner = GeminiRunner()
        assert isinstance(runner._fallback, ClaudeRunner)

    def test_available_reflects_path(self) -> None:
        runner = GeminiRunner()
        assert isinstance(runner.available, bool)

    def test_get_runner_gemini(self) -> None:
        runner = get_runner("gemini")
        assert isinstance(runner, GeminiRunner)


class TestKillProcGroup:
    @pytest.mark.asyncio
    async def test_none_is_noop(self) -> None:
        await kill_proc_group(None)  # must not raise

    @pytest.mark.asyncio
    async def test_already_exited_is_noop(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "true", start_new_session=True,
        )
        await proc.wait()
        assert proc.returncode is not None
        await kill_proc_group(proc)  # returncode set → no-op

    @pytest.mark.asyncio
    async def test_kills_running_process(self) -> None:
        # A process that ignores SIGTERM must still be killed (via SIGKILL).
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            start_new_session=True,
        )
        start = time.monotonic()
        await kill_proc_group(proc)
        elapsed = time.monotonic() - start
        assert proc.returncode is not None  # reaped, not a zombie
        # SIGTERM ignored → escalates to SIGKILL after the 5s budget, well under 30s.
        assert elapsed < 15

    @pytest.mark.asyncio
    async def test_kills_child_processes(self) -> None:
        # Parent spawns a child then sleeps; killpg must reach the whole group.
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", "sleep 30 & sleep 30",
            start_new_session=True,
        )
        await kill_proc_group(proc)
        assert proc.returncode is not None
