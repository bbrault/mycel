from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runner import ClaudeRunner, CursorRunner, GeminiRunner, RunnerResult, get_runner


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
