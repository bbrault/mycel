"""Integration tests for the Forge workflow engine (async).

These tests mock the runner to return controlled outputs and verify
the full workflow state machine: sequential execution, pass_condition
routing, retries, pauses, and partial restarts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import Forge
from message_bus import MessageBus
from runner import RunnerResult


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_runner_result(output: str, success: bool = True) -> RunnerResult:
    """Create a RunnerResult with controlled output."""
    if success:
        return RunnerResult(stdout=output, stderr="", returncode=0, runner_used="claude")
    return RunnerResult(stdout="", stderr=output, returncode=1, runner_used="claude")


def _make_circle(
    bus_dir: str,
    workflow: List[str],
    skills: Dict[str, Dict[str, Any]],
) -> Forge:
    """Create a Forge with the given workflow and skills for testing."""
    bus = MessageBus(bus_dir=bus_dir)
    return Forge(
        name="test",
        description="test forge",
        workflow=workflow,
        spells=skills,
        workspace={},
        bus=bus,
        bus_dir=bus_dir,
        max_retries=3,
        timeout=60,
    )


# ------------------------------------------------------------------
# Skills fixtures
# ------------------------------------------------------------------

SIMPLE_SKILLS = {
    "step-a": {
        "prompt": "Do step A. Task: {task}",
        "runner": "claude",
        "auto_advance": True,
        "next_on_pass": "step-b",
        "next_on_fail": None,
        "pass_condition": None,
    },
    "step-b": {
        "prompt": "Do step B. Previous: {previous_output}",
        "runner": "claude",
        "auto_advance": True,
        "next_on_pass": "step-c",
        "next_on_fail": None,
        "pass_condition": None,
    },
    "step-c": {
        "prompt": "Do step C. Previous: {previous_output}",
        "runner": "claude",
        "auto_advance": True,
        "next_on_pass": None,
        "next_on_fail": None,
        "pass_condition": None,
    },
}

REVIEW_SKILLS = {
    "implement": {
        "prompt": "Implement. Task: {task}",
        "runner": "claude",
        "auto_advance": True,
        "next_on_pass": "review",
        "next_on_fail": None,
        "pass_condition": None,
    },
    "review": {
        "prompt": "Review. Previous: {previous_output}",
        "runner": "claude",
        "auto_advance": False,
        "next_on_pass": None,
        "next_on_fail": "implement",
        "pass_condition": "verdict == 'approved'",
    },
}

MULTI_GATE_SKILLS = {
    "plan": {
        "prompt": "Plan. Task: {task}",
        "runner": "claude",
        "auto_advance": True,
        "next_on_pass": "gate",
        "next_on_fail": None,
        "pass_condition": None,
    },
    "gate": {
        "prompt": "Gate. Previous: {previous_output}",
        "runner": "claude",
        "auto_advance": True,
        "next_on_pass": "build",
        "next_on_fail": "plan",
        "pass_condition": "verdict == 'approved'",
    },
    "build": {
        "prompt": "Build. Previous: {previous_output}",
        "runner": "claude",
        "auto_advance": True,
        "next_on_pass": None,
        "next_on_fail": None,
        "pass_condition": None,
    },
}


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestWorkflowComplete:
    """Test a simple 3-step workflow that runs to completion."""

    @pytest.mark.asyncio
    async def test_full_workflow_completes(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a", "step-b", "step-c"], SIMPLE_SKILLS)

        outputs = [
            _make_runner_result('{"result": "A done"}'),
            _make_runner_result('{"result": "B done"}'),
            _make_runner_result('{"result": "C done"}'),
        ]
        call_count = 0

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal call_count
            result = outputs[call_count]
            call_count += 1
            return result

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Test task")
            await circle.bus.stop()

        assert circle.state["status"] == "completed"
        assert call_count == 3
        assert "step-a" in circle.state["step_outputs"]
        assert "step-b" in circle.state["step_outputs"]
        assert "step-c" in circle.state["step_outputs"]

    @pytest.mark.asyncio
    async def test_previous_output_chaining(self) -> None:
        """Verify that each skill receives the previous skill's output."""
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a", "step-b", "step-c"], SIMPLE_SKILLS)

        prompts_received: List[str] = []

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            prompts_received.append(prompt)
            return _make_runner_result(f'{{"step": "{len(prompts_received)}"}}')

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("My task")
            await circle.bus.stop()

        # step-a gets the task
        assert "My task" in prompts_received[0]
        # step-b gets step-a's output as previous_output
        assert '"step": "1"' in prompts_received[1]
        # step-c gets step-b's output
        assert '"step": "2"' in prompts_received[2]


class TestPassCondition:
    """Test pass_condition evaluation and retry routing."""

    @pytest.mark.asyncio
    async def test_pass_condition_approved(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["plan", "gate", "build"], MULTI_GATE_SKILLS)

        outputs = [
            _make_runner_result('{"result": "plan done"}'),
            _make_runner_result('{"verdict": "approved", "score": 90}'),
            _make_runner_result('{"result": "build done"}'),
        ]
        call_count = 0

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal call_count
            result = outputs[call_count]
            call_count += 1
            return result

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Feature X")
            await circle.bus.stop()

        assert circle.state["status"] == "completed"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_pass_condition_rejected_retries(self) -> None:
        """Gate rejects → goes back to plan → gate approves → build runs."""
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["plan", "gate", "build"], MULTI_GATE_SKILLS)

        outputs = [
            _make_runner_result('{"result": "plan v1"}'),            # plan
            _make_runner_result('{"verdict": "rejected", "score": 40}'),  # gate FAIL
            _make_runner_result('{"result": "plan v2"}'),            # plan (retry)
            _make_runner_result('{"verdict": "approved", "score": 85}'),  # gate PASS
            _make_runner_result('{"result": "build done"}'),         # build
        ]
        call_count = 0

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal call_count
            result = outputs[call_count]
            call_count += 1
            return result

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Feature X")
            await circle.bus.stop()

        assert circle.state["status"] == "completed"
        assert call_count == 5

    @pytest.mark.asyncio
    async def test_max_retries_reached(self) -> None:
        """Gate rejects 3 times → workflow fails."""
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["plan", "gate", "build"], MULTI_GATE_SKILLS)

        rejected = _make_runner_result('{"verdict": "rejected", "score": 20}')
        plan_output = _make_runner_result('{"result": "plan"}')

        # plan, gate(fail), plan, gate(fail), plan, gate(fail) = 6 calls
        outputs = [plan_output, rejected, plan_output, rejected, plan_output, rejected]
        call_count = 0

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal call_count
            result = outputs[call_count]
            call_count += 1
            return result

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Feature X")
            await circle.bus.stop()

        assert circle.state["status"] == "failed"
        assert "gate" in circle.state.get("error", "")

    @pytest.mark.asyncio
    async def test_reviewer_feedback_injected_on_retry(self) -> None:
        """When gate rejects, the plan skill receives reviewer feedback."""
        tmp = tempfile.mkdtemp()
        # Use a prompt with {reviewer_feedback} for plan
        skills = dict(MULTI_GATE_SKILLS)
        skills["plan"] = dict(skills["plan"])
        skills["plan"]["prompt"] = "Plan. Task: {task}\nFeedback: {reviewer_feedback}"

        circle = _make_circle(tmp, ["plan", "gate", "build"], skills)

        prompts_received: List[str] = []
        call_count = 0
        outputs = [
            _make_runner_result('{"result": "plan v1"}'),                        # plan (1st)
            _make_runner_result('{"verdict": "rejected", "score": 40, "issues": [{"severity": "critical", "description": "Missing Repository Interface"}], "summary": "Plan incomplet"}'),  # gate FAIL
            _make_runner_result('{"result": "plan v2 fixed"}'),                  # plan (retry)
            _make_runner_result('{"verdict": "approved", "score": 85}'),         # gate PASS
            _make_runner_result('{"result": "build done"}'),                     # build
        ]

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal call_count
            prompts_received.append(prompt)
            result = outputs[call_count]
            call_count += 1
            return result

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Feature X")
            await circle.bus.stop()

        assert circle.state["status"] == "completed"
        # First plan call: no feedback
        assert "first pass" in prompts_received[0]
        # Second plan call (after gate rejection): should contain reviewer feedback
        assert "Missing Repository Interface" in prompts_received[2]
        assert "Plan incomplet" in prompts_received[2]


class TestAutoAdvancePause:
    """Test auto_advance=false pauses the workflow."""

    @pytest.mark.asyncio
    async def test_pause_and_resume(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["implement", "review"], REVIEW_SKILLS)

        outputs = [
            _make_runner_result('{"code": "done"}'),
            _make_runner_result('{"verdict": "approved", "score": 95}'),
        ]
        call_count = 0

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal call_count
            result = outputs[call_count]
            call_count += 1
            return result

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()

            # Run workflow in a task so we can check intermediate state
            task = asyncio.create_task(circle.run_workflow("Build feature"))

            # Wait for the review skill to pause
            for _ in range(50):
                await asyncio.sleep(0.05)
                if circle.state["status"] == "paused":
                    break

            assert circle.state["status"] == "paused"
            assert circle.state["current_skill"] == "review"
            assert call_count == 2  # both skills ran

            # Resume
            circle.resume(instructions="Looks good, continue")
            await task

            await circle.bus.stop()

        assert circle.state["status"] == "completed"


class TestRunFromSkill:
    """Test partial restart from a specific skill."""

    @pytest.mark.asyncio
    async def test_restart_keeps_prior_outputs(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a", "step-b", "step-c"], SIMPLE_SKILLS)

        # First: run a full workflow
        full_outputs = [
            _make_runner_result('{"a": "original"}'),
            _make_runner_result('{"b": "original"}'),
            _make_runner_result('{"c": "original"}'),
        ]
        call_count = 0

        async def mock_run_full(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal call_count
            result = full_outputs[call_count]
            call_count += 1
            return result

        mock_runner = AsyncMock()
        mock_runner.run = mock_run_full

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Initial task")

        assert circle.state["status"] == "completed"

        # Now restart from step-b
        restart_outputs = [
            _make_runner_result('{"b": "rerun"}'),
            _make_runner_result('{"c": "rerun"}'),
        ]
        restart_count = 0

        async def mock_run_restart(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            nonlocal restart_count
            result = restart_outputs[restart_count]
            restart_count += 1
            return result

        mock_runner2 = AsyncMock()
        mock_runner2.run = mock_run_restart

        with patch("forge.get_runner", return_value=mock_runner2):
            await circle.run_from_skill("step-b")
            await circle.bus.stop()

        assert circle.state["status"] == "completed"
        # step-a output should be preserved from original run
        assert '"a": "original"' in circle.state["step_outputs"]["step-a"]
        # step-b and step-c should have new outputs
        assert '"b": "rerun"' in circle.state["step_outputs"]["step-b"]
        assert '"c": "rerun"' in circle.state["step_outputs"]["step-c"]


class TestRunnerError:
    """Test that runner failures are handled gracefully."""

    @pytest.mark.asyncio
    async def test_runner_error_sets_error_state(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a", "step-b", "step-c"], SIMPLE_SKILLS)

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            return _make_runner_result("Connection refused", success=False)

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Failing task")
            await circle.bus.stop()

        assert circle.state["status"] == "error"
        assert circle.state["error"] is not None


class TestParallelSkills:
    """Test parallel skill execution within a workflow."""

    PARALLEL_SKILLS = {
        "fetch": {
            "prompt": "Fetch. Task: {task}",
            "runner": "claude",
            "auto_advance": True,
            "next_on_pass": None,  # parallel step handles advancement
            "next_on_fail": None,
            "pass_condition": None,
        },
        "review-a": {
            "prompt": "Review A. Data: {step_output_fetch}",
            "runner": "claude",
            "auto_advance": True,
            "next_on_pass": None,
            "next_on_fail": None,
            "pass_condition": None,
        },
        "review-b": {
            "prompt": "Review B. Data: {step_output_fetch}",
            "runner": "gemini",
            "auto_advance": True,
            "next_on_pass": None,
            "next_on_fail": None,
            "pass_condition": None,
        },
        "summary": {
            "prompt": "Summary. A: {step_output_review-a} B: {step_output_review-b}",
            "runner": "claude",
            "auto_advance": True,
            "next_on_pass": None,
            "next_on_fail": None,
            "pass_condition": None,
        },
    }

    @pytest.mark.asyncio
    async def test_parallel_workflow_completes(self) -> None:
        tmp = tempfile.mkdtemp()
        workflow = [
            "fetch",
            {"parallel": ["review-a", "review-b"]},
            "summary",
        ]
        circle = _make_circle(tmp, workflow, self.PARALLEL_SKILLS)

        call_order: List[str] = []

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            # Identify which skill is running by checking the prompt
            if "Fetch" in prompt:
                call_order.append("fetch")
                return _make_runner_result('{"data": "fetched"}')
            elif "Review A" in prompt:
                call_order.append("review-a")
                return _make_runner_result('{"verdict": "ok", "source": "A"}')
            elif "Review B" in prompt:
                call_order.append("review-b")
                return _make_runner_result('{"verdict": "ok", "source": "B"}')
            else:
                call_order.append("summary")
                return _make_runner_result('{"overall": "done"}')

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Test parallel")
            await circle.bus.stop()

        assert circle.state["status"] == "completed"
        # fetch runs first, then review-a and review-b (order may vary), then summary
        assert call_order[0] == "fetch"
        assert set(call_order[1:3]) == {"review-a", "review-b"}
        assert call_order[3] == "summary"
        # All outputs stored
        assert "fetch" in circle.state["step_outputs"]
        assert "review-a" in circle.state["step_outputs"]
        assert "review-b" in circle.state["step_outputs"]
        assert "summary" in circle.state["step_outputs"]

    @pytest.mark.asyncio
    async def test_parallel_one_failure_stops_workflow(self) -> None:
        tmp = tempfile.mkdtemp()
        workflow = [
            "fetch",
            {"parallel": ["review-a", "review-b"]},
            "summary",
        ]
        circle = _make_circle(tmp, workflow, self.PARALLEL_SKILLS)

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            if "Fetch" in prompt:
                return _make_runner_result('{"data": "fetched"}')
            elif "Review A" in prompt:
                return _make_runner_result('{"verdict": "ok"}')
            elif "Review B" in prompt:
                return _make_runner_result("Runner timeout", success=False)
            else:
                return _make_runner_result('{"overall": "done"}')

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Test parallel fail")
            await circle.bus.stop()

        assert circle.state["status"] == "error"
        assert "Parallel" in (circle.state.get("error") or "")

    @pytest.mark.asyncio
    async def test_find_skill_in_parallel_group(self) -> None:
        tmp = tempfile.mkdtemp()
        workflow = [
            "fetch",
            {"parallel": ["review-a", "review-b"]},
            "summary",
        ]
        circle = _make_circle(tmp, workflow, self.PARALLEL_SKILLS)

        assert circle._find_skill_workflow_index("fetch") == 0
        assert circle._find_skill_workflow_index("review-a") == 1
        assert circle._find_skill_workflow_index("review-b") == 1
        assert circle._find_skill_workflow_index("summary") == 2

    @pytest.mark.asyncio
    async def test_all_skill_names(self) -> None:
        tmp = tempfile.mkdtemp()
        workflow = [
            "fetch",
            {"parallel": ["review-a", "review-b"]},
            "summary",
        ]
        circle = _make_circle(tmp, workflow, self.PARALLEL_SKILLS)

        names = circle._all_skill_names()
        assert names == ["fetch", "review-a", "review-b", "summary"]


class TestStatePersistence:
    """Test that state is persisted across operations."""

    @pytest.mark.asyncio
    async def test_state_saved_after_completion(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], {
            "step-a": {
                "prompt": "Task: {task}",
                "runner": "claude",
                "auto_advance": True,
                "next_on_pass": None,
                "next_on_fail": None,
                "pass_condition": None,
            },
        })

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            return _make_runner_result('{"done": true}')

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Persist test")
            await circle.bus.stop()

        # Verify state file exists
        state_path = os.path.join(tmp, "test", "state.json")
        assert os.path.exists(state_path)

        with open(state_path) as f:
            saved = json.load(f)
        assert saved["status"] == "completed"
        assert saved["task"] == "Persist test"

    @pytest.mark.asyncio
    async def test_skill_output_saved(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], {
            "step-a": {
                "prompt": "Task: {task}",
                "runner": "claude",
                "auto_advance": True,
                "next_on_pass": None,
                "next_on_fail": None,
                "pass_condition": None,
            },
        })

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            return _make_runner_result('{"answer": 42}')

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            await circle.run_workflow("Save test")
            await circle.bus.stop()

        output_path = os.path.join(tmp, "test", "skill_step-a.json")
        assert os.path.exists(output_path)

        with open(output_path) as f:
            saved = json.load(f)
        assert saved["skill"] == "step-a"


class TestHookQuoting:
    """Discord-controlled template variables must be shell-quoted in hooks."""

    @pytest.mark.asyncio
    async def test_task_cannot_inject_shell_commands(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], SIMPLE_SKILLS)
        circle.state["task"] = "x; echo PWNED"
        await circle.bus.start()
        try:
            output = await circle._exec_hook("step-a", "pre_run", "echo {task}", timeout_s=5)
            # Quoted: echo prints the whole task literally. Unquoted, the
            # injected `echo PWNED` would run and print PWNED on its own line.
            assert output.strip() == "x; echo PWNED"
        finally:
            await circle.bus.stop()

    @pytest.mark.asyncio
    async def test_empty_variables_stay_empty(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], SIMPLE_SKILLS)
        await circle.bus.start()
        try:
            output = await circle._exec_hook("step-a", "pre_run", "echo [{mr_iid}]", timeout_s=5)
            assert output.strip() == "[]"
        finally:
            await circle.bus.stop()


class TestAbortStatus:
    """Abort moves to a distinct `aborted` status, not `paused`."""

    def test_abort_running_sets_aborted_status(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], SIMPLE_SKILLS)
        circle.state["status"] = "running"
        circle.state["current_skill"] = "step-a"
        assert circle.abort() is True
        assert circle.state["status"] == "aborted"
        assert circle.state["aborted_at_skill"] == "step-a"

    def test_abort_when_not_running_is_noop(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], SIMPLE_SKILLS)
        circle.state["status"] = "paused"
        assert circle.abort() is False
        assert circle.state["status"] == "paused"

    def test_reset_clears_aborted_state(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], SIMPLE_SKILLS)
        circle.state["status"] = "running"
        circle.state["current_skill"] = "step-a"
        circle.abort()
        circle.reset()
        assert circle.state["status"] == "idle"
        assert circle.state.get("aborted_at_skill") is None


class TestHookTimeout:
    """Hooks (pre_run/post_run) must kill leaked subprocesses on timeout."""

    @pytest.mark.asyncio
    async def test_post_run_timeout_kills_subprocess(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = _make_circle(tmp, ["step-a"], SIMPLE_SKILLS)
        await circle.bus.start()
        try:
            # A hook that would normally hang forever (sleep 30s) but has a 1s budget.
            output = await circle._exec_hook(
                "step-a",
                "post_run",
                "echo started; sleep 30; echo never",
                timeout_s=1,
            )
            # We get a timeout marker, NOT the "never" line — proof the proc was killed.
            assert "timeout" in output
            assert "never" not in output
        finally:
            await circle.bus.stop()

    @pytest.mark.asyncio
    async def test_post_run_timeout_uses_per_spell_override(self) -> None:
        """The post_run_timeout key on a spell should override the 300s default."""
        tmp = tempfile.mkdtemp()
        skills = {
            "impl": {
                "prompt": "do it",
                "runner": "claude",
                "auto_advance": True,
                "next_on_pass": None,
                "next_on_fail": None,
                "pass_condition": None,
                "post_run": "sleep 5; echo never",
                "post_run_timeout": 1,
            },
        }
        circle = _make_circle(tmp, ["impl"], skills)

        async def mock_run(prompt: str, timeout: int = 60, on_output: Any = None, **kwargs: Any) -> RunnerResult:
            return _make_runner_result('{"ok": true}')

        mock_runner = AsyncMock()
        mock_runner.run = mock_run

        with patch("forge.get_runner", return_value=mock_runner):
            await circle.bus.start()
            try:
                await circle.run_workflow("test")
            finally:
                await circle.bus.stop()

        post = circle.state["step_outputs"].get("_post_run", "")
        assert "timeout" in post
        assert "never" not in post


class TestRecoverySafeguards:
    """Recovery at startup must not silently re-run stale or interrupted state."""

    def _make_orchestrator(self, tmp: str, paused_state: Dict[str, Any], state_age_s: float = 0) -> Any:
        """Build a Mycel with one forge whose state.json reflects `paused_state`."""
        import importlib.util
        # Load mycel.py as a module bypassing the package shadowing.
        spec = importlib.util.spec_from_file_location(
            "_mycel_for_test",
            os.path.join(os.path.dirname(__file__), "..", "mycel.py"),
        )
        mycel_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mycel_mod)

        config_path = os.path.join(tmp, "config.yaml")
        spells_path = os.path.join(tmp, "spells.yaml")
        with open(config_path, "w") as f:
            f.write(
                "forges:\n"
                "  test:\n"
                "    description: t\n"
                "    familiar: claude\n"
                "    channel: t\n"
                "    ritual:\n"
                "      - step-a\n"
                "      - step-b\n"
                "auto_resume_max_age_s: 60\n"
            )
        with open(spells_path, "w") as f:
            f.write(
                "spells:\n"
                "  step-a:\n"
                "    runner: claude\n"
                "    auto_advance: false\n"
                "    next_on_pass: step-b\n"
                "    prompt: a\n"
                "  step-b:\n"
                "    runner: claude\n"
                "    auto_advance: true\n"
                "    next_on_pass: null\n"
                "    prompt: b\n"
            )

        bus_dir = os.path.join(tmp, "bus")
        os.makedirs(os.path.join(bus_dir, "test"), exist_ok=True)
        state_path = os.path.join(bus_dir, "test", "state.json")
        with open(state_path, "w") as f:
            json.dump(paused_state, f)

        if state_age_s > 0:
            past = os.path.getmtime(state_path) - state_age_s
            os.utime(state_path, (past, past))

        return mycel_mod.Mycel(config_path=config_path, spells_path=spells_path, bus_dir=bus_dir)

    async def _setup_for_recovery(self, orch: Any) -> None:
        """Initialize the queues without spawning real worker tasks."""
        for forge_name in orch.forges:
            orch._forge_queues[forge_name] = asyncio.Queue()
            orch._active_items[forge_name] = None
        orch._worker_started = True

    @pytest.mark.asyncio
    async def test_stale_paused_state_is_not_auto_resumed(self) -> None:
        tmp = tempfile.mkdtemp()
        # Paused after step-a (already in step_outputs), but 1 hour old → stale.
        state = {
            "status": "paused",
            "current_skill": "step-a",
            "current_index": 0,
            "task": "old task",
            "step_outputs": {"step-a": '{"done": true}'},
            "previous_output": '{"done": true}',
            "retries": {},
            "run_number": 1,
        }
        orch = self._make_orchestrator(tmp, state, state_age_s=3600)

        await orch.bus.start()
        try:
            await self._setup_for_recovery(orch)
            await orch._recover_forges()
        finally:
            await orch.bus.stop()

        # No queue item should have been enqueued (stale → user must confirm).
        assert orch._forge_queues["test"].qsize() == 0
        # State should remain paused (not flipped to idle / running).
        assert orch.forges["test"].state["status"] == "paused"

    @pytest.mark.asyncio
    async def test_running_state_is_demoted_to_paused(self) -> None:
        """A 'running' state at restart = bot was killed mid-spell. Never auto-resume."""
        tmp = tempfile.mkdtemp()
        state = {
            "status": "running",  # interrupted mid-spell
            "current_skill": "step-a",
            "current_index": 0,
            "task": "task",
            "step_outputs": {},  # spell did NOT complete
            "previous_output": None,
            "retries": {},
            "run_number": 1,
        }
        orch = self._make_orchestrator(tmp, state, state_age_s=10)  # even fresh

        await orch.bus.start()
        try:
            await self._setup_for_recovery(orch)
            await orch._recover_forges()
        finally:
            await orch.bus.stop()

        assert orch._forge_queues["test"].qsize() == 0  # nothing re-enqueued
        assert orch.forges["test"].state["status"] == "paused"
        assert "Interrupted" in (orch.forges["test"].state.get("error") or "")

    @pytest.mark.asyncio
    async def test_fresh_paused_state_does_auto_resume(self) -> None:
        """Sanity check: a recently-paused state still auto-resumes."""
        tmp = tempfile.mkdtemp()
        state = {
            "status": "paused",
            "current_skill": "step-a",
            "current_index": 0,
            "task": "task",
            "step_outputs": {"step-a": '{"done": true}'},
            "previous_output": '{"done": true}',
            "retries": {},
            "run_number": 1,
        }
        orch = self._make_orchestrator(tmp, state, state_age_s=5)  # very fresh

        await orch.bus.start()
        try:
            await self._setup_for_recovery(orch)
            await orch._recover_forges()
        finally:
            await orch.bus.stop()

        queue = orch._forge_queues["test"]
        assert queue.qsize() == 1
        item = queue.get_nowait()
        # step-a already done → should advance to step-b
        assert item.from_spell == "step-b"
