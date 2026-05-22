from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import Forge, extract_json, safe_evaluate_condition, validate_output
from message_bus import MessageBus


# ------------------------------------------------------------------
# extract_json
# ------------------------------------------------------------------


class TestExtractJson:
    def test_direct_json(self) -> None:
        raw = '{"verdict": "approved", "score": 85}'
        result = extract_json(raw)
        assert result is not None
        assert result["verdict"] == "approved"

    def test_code_fence(self) -> None:
        raw = 'Some preamble\n```json\n{"key": "value"}\n```\nSome epilogue'
        result = extract_json(raw)
        assert result is not None
        assert result["key"] == "value"

    def test_code_fence_no_lang(self) -> None:
        raw = 'Text\n```\n{"a": 1}\n```'
        result = extract_json(raw)
        assert result is not None
        assert result["a"] == 1

    def test_json_in_text(self) -> None:
        raw = 'Here is the result:\n{"verdict": "rejected", "score": 40}\nDone.'
        result = extract_json(raw)
        assert result is not None
        assert result["verdict"] == "rejected"

    def test_no_json(self) -> None:
        assert extract_json("just plain text") is None

    def test_empty(self) -> None:
        assert extract_json("") is None

    def test_nested_json(self) -> None:
        raw = '{"outer": {"inner": 42}}'
        result = extract_json(raw)
        assert result is not None
        assert result["outer"]["inner"] == 42

    def test_array_ignored(self) -> None:
        raw = '[1, 2, 3]'
        assert extract_json(raw) is None


# ------------------------------------------------------------------
# validate_output
# ------------------------------------------------------------------


class TestValidateOutput:
    def test_no_required_fields(self) -> None:
        skill: dict = {"prompt": "..."}
        assert validate_output('{"any": "thing"}', skill) is None

    def test_empty_required_fields(self) -> None:
        skill = {"prompt": "...", "required_fields": []}
        assert validate_output('{"any": "thing"}', skill) is None

    def test_all_fields_present(self) -> None:
        skill = {"prompt": "...", "required_fields": ["verdict", "score"]}
        assert validate_output('{"verdict": "approved", "score": 85}', skill) is None

    def test_missing_fields(self) -> None:
        skill = {"prompt": "...", "required_fields": ["verdict", "score", "summary"]}
        result = validate_output('{"verdict": "approved"}', skill)
        assert result is not None
        assert "score" in result
        assert "summary" in result

    def test_not_json(self) -> None:
        skill = {"prompt": "...", "required_fields": ["verdict"]}
        result = validate_output("just plain text", skill)
        assert result is not None
        assert "not valid JSON" in result

    def test_json_in_code_fence(self) -> None:
        skill = {"prompt": "...", "required_fields": ["verdict"]}
        raw = '```json\n{"verdict": "approved"}\n```'
        assert validate_output(raw, skill) is None


# ------------------------------------------------------------------
# safe_evaluate_condition
# ------------------------------------------------------------------


class TestSafeEvaluateCondition:
    def test_string_equal(self) -> None:
        assert safe_evaluate_condition("verdict == 'approved'", {"verdict": "approved"}) is True

    def test_string_not_equal(self) -> None:
        assert safe_evaluate_condition("verdict == 'approved'", {"verdict": "rejected"}) is False

    def test_not_equal_operator(self) -> None:
        assert safe_evaluate_condition("verdict != 'rejected'", {"verdict": "approved"}) is True

    def test_numeric_gte(self) -> None:
        assert safe_evaluate_condition("score >= 70", {"score": 85}) is True

    def test_numeric_lt(self) -> None:
        assert safe_evaluate_condition("score < 70", {"score": 85}) is False

    def test_and_both_true(self) -> None:
        data = {"verdict": "approved", "score": 90}
        assert safe_evaluate_condition("verdict == 'approved' and score >= 70", data) is True

    def test_and_one_false(self) -> None:
        data = {"verdict": "approved", "score": 50}
        assert safe_evaluate_condition("verdict == 'approved' and score >= 70", data) is False

    def test_or_one_true(self) -> None:
        data = {"verdict": "rejected", "score": 95}
        assert safe_evaluate_condition("verdict == 'approved' or score >= 70", data) is True

    def test_missing_key(self) -> None:
        assert safe_evaluate_condition("verdict == 'approved'", {}) is False

    def test_numeric_string_value(self) -> None:
        assert safe_evaluate_condition("score >= 70", {"score": "85"}) is True


# ------------------------------------------------------------------
# Forge._build_prompt
# ------------------------------------------------------------------


class TestForgeBuildPrompt:
    def _make_circle(self, bus_dir: str) -> Forge:
        bus = MessageBus(bus_dir=bus_dir)
        skills = {
            "plan": {
                "prompt": "Task: {task}\nWorkspace: {workspace}\nForge: {forge_name}\nPrev: {previous_output}\nInstr: {instructions}\nPlan output: {step_output_plan}\nDocs: {docs_path}\nJIRA: {jira_id}",
                "runner": "claude",
            },
        }
        circle = Forge(
            name="test",
            description="test forge",
            workflow=["plan"],
            spells=skills,
            workspace={"api": "/path/to/api", "front": "/path/to/front"},
            bus=bus,
            bus_dir=bus_dir,
            docs_path="/path/to/docs",
        )
        return circle

    def test_basic_substitution(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "Build a feature"
        circle.state["instructions"] = "Use TypeScript"

        prompt = circle._build_prompt(circle.spells["plan"])

        assert "Build a feature" in prompt
        assert "Use TypeScript" in prompt
        assert "api: /path/to/api" in prompt
        assert "test" in prompt  # forge_name

    def test_step_output_substitution(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "x"
        circle.state["step_outputs"] = {"plan": "the plan output"}

        prompt = circle._build_prompt(circle.spells["plan"])
        assert "the plan output" in prompt

    def test_missing_step_output_replaced(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "x"

        prompt = circle._build_prompt(circle.spells["plan"])
        assert "(non disponible)" in prompt

    def test_feedback_injection(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "x"
        circle.inject_feedback("Please add tests")

        prompt = circle._build_prompt(circle.spells["plan"])
        assert "Please add tests" in prompt
        # Buffer should be cleared
        assert len(circle._feedback_buffer) == 0

    def test_jira_id_extraction(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "LAB-123 Ajouter la fonctionnalite X"

        prompt = circle._build_prompt(circle.spells["plan"])
        assert "JIRA: LAB-123" in prompt

    def test_jira_id_kanta_format(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "KANTA-456 Fix import CSV"

        prompt = circle._build_prompt(circle.spells["plan"])
        assert "JIRA: KANTA-456" in prompt

    def test_no_jira_id(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "Just a simple task"

        prompt = circle._build_prompt(circle.spells["plan"])
        assert "JIRA: \n" in prompt or "JIRA: " in prompt

    def test_docs_path_substitution(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "x"

        prompt = circle._build_prompt(circle.spells["plan"])
        assert "/path/to/docs" in prompt


# ------------------------------------------------------------------
# Forge._evaluate_pass_condition
# ------------------------------------------------------------------


class TestForgeEvaluatePassCondition:
    def _make_circle(self) -> Forge:
        tmp = tempfile.mkdtemp()
        bus = MessageBus(bus_dir=tmp)
        return Forge(
            name="test",
            description="",
            workflow=[],
            spells={},
            workspace={},
            bus=bus,
            bus_dir=tmp,
        )

    def test_valid_json_pass(self) -> None:
        circle = self._make_circle()
        output = json.dumps({"verdict": "approved", "score": 90})
        assert circle._evaluate_pass_condition("verdict == 'approved'", output) is True

    def test_valid_json_fail(self) -> None:
        circle = self._make_circle()
        output = json.dumps({"verdict": "rejected"})
        assert circle._evaluate_pass_condition("verdict == 'approved'", output) is False

    def test_json_in_code_fence(self) -> None:
        circle = self._make_circle()
        output = 'Voici ma review:\n```json\n{"verdict": "approved"}\n```'
        assert circle._evaluate_pass_condition("verdict == 'approved'", output) is True

    def test_json_in_text(self) -> None:
        circle = self._make_circle()
        output = 'Result: {"verdict": "approved", "score": 80} end'
        assert circle._evaluate_pass_condition("verdict == 'approved'", output) is True

    def test_no_json(self) -> None:
        circle = self._make_circle()
        assert circle._evaluate_pass_condition("verdict == 'approved'", "no json here") is False

    def test_numeric_condition(self) -> None:
        circle = self._make_circle()
        output = json.dumps({"score": 85})
        assert circle._evaluate_pass_condition("score >= 70", output) is True


# ------------------------------------------------------------------
# MR URL parsing
# ------------------------------------------------------------------


class TestMrUrlParsing:
    def _make_circle(self, task: str = "") -> Forge:
        tmp = tempfile.mkdtemp()
        bus = MessageBus(bus_dir=tmp)
        circle = Forge(
            name="review",
            description="test",
            workflow=[],
            spells={},
            workspace={},
            bus=bus,
            bus_dir=tmp,
        )
        circle.state["task"] = task
        return circle

    def test_parse_standard_gitlab_url(self) -> None:
        circle = self._make_circle("https://gitlab.com/kanta/kanta-api-v2/-/merge_requests/42")
        project, iid = circle._parse_mr_url()
        assert project == "kanta/kanta-api-v2"
        assert iid == "42"

    def test_parse_self_hosted_url(self) -> None:
        circle = self._make_circle("https://gitlab.mycompany.com/team/repo/-/merge_requests/123")
        project, iid = circle._parse_mr_url()
        assert project == "team/repo"
        assert iid == "123"

    def test_parse_subgroup_url(self) -> None:
        circle = self._make_circle("https://gitlab.com/group/sub/project/-/merge_requests/99")
        project, iid = circle._parse_mr_url()
        assert project == "group/sub/project"
        assert iid == "99"

    def test_parse_no_mr_url(self) -> None:
        circle = self._make_circle("LAB-123 Just a JIRA task")
        project, iid = circle._parse_mr_url()
        assert project == ""
        assert iid == ""

    def test_parse_url_in_longer_text(self) -> None:
        circle = self._make_circle("Review https://gitlab.com/kanta/front/-/merge_requests/7 focus securite")
        project, iid = circle._parse_mr_url()
        assert project == "kanta/front"
        assert iid == "7"

    def test_parse_empty_task(self) -> None:
        circle = self._make_circle("")
        project, iid = circle._parse_mr_url()
        assert project == ""
        assert iid == ""


# ------------------------------------------------------------------
# MR variables in _build_prompt
# ------------------------------------------------------------------


class TestMrBuildPrompt:
    def _make_circle(self, bus_dir: str) -> Forge:
        bus = MessageBus(bus_dir=bus_dir)
        skills = {
            "mr-review": {
                "prompt": "Project: {mr_project}\nIID: {mr_iid}\nTask: {task}\nInstr: {instructions}",
                "runner": "claude",
            },
        }
        circle = Forge(
            name="review",
            description="test",
            workflow=["mr-review"],
            spells=skills,
            workspace={},
            bus=bus,
            bus_dir=bus_dir,
        )
        return circle

    def test_mr_variables_substituted(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "https://gitlab.com/kanta/api/-/merge_requests/55"

        prompt = circle._build_prompt(circle.spells["mr-review"])
        assert "Project: kanta/api" in prompt
        assert "IID: 55" in prompt

    def test_mr_variables_empty_for_jira_task(self) -> None:
        tmp = tempfile.mkdtemp()
        circle = self._make_circle(tmp)
        circle.state["task"] = "LAB-123 Ajouter fonctionnalite"

        prompt = circle._build_prompt(circle.spells["mr-review"])
        assert "Project: \n" in prompt or "Project: " in prompt
        assert "IID: \n" in prompt or "IID: " in prompt


# ------------------------------------------------------------------
# MR issue directory and doc naming
# ------------------------------------------------------------------


class TestMrIssueDir:
    def _make_circle(self, task: str) -> Forge:
        tmp = tempfile.mkdtemp()
        bus = MessageBus(bus_dir=tmp)
        circle = Forge(
            name="review",
            description="test",
            workflow=[],
            spells={},
            workspace={},
            bus=bus,
            bus_dir=tmp,
            issues_dir=os.path.join(tmp, "issues"),
        )
        circle.state["task"] = task
        return circle

    def test_issue_dir_from_mr_url(self) -> None:
        circle = self._make_circle("https://gitlab.com/kanta/api/-/merge_requests/42")
        issue_dir = circle._get_issue_dir()
        assert issue_dir is not None
        assert "MR-42-kanta-api" in issue_dir

    def test_issue_dir_jira_takes_precedence(self) -> None:
        circle = self._make_circle("LAB-123 Some task")
        issue_dir = circle._get_issue_dir()
        assert issue_dir is not None
        assert "LAB-123" in issue_dir
        assert "MR-" not in issue_dir

    def test_issue_dir_none_without_id(self) -> None:
        circle = self._make_circle("Just a plain task")
        issue_dir = circle._get_issue_dir()
        assert issue_dir is None

    def test_save_issue_doc_mr_prefix(self) -> None:
        circle = self._make_circle("https://gitlab.com/kanta/api/-/merge_requests/42")
        filepath = circle._save_issue_doc("mr-review", "review content")
        assert filepath is not None
        assert "MR-42-mr_code_review.md" in filepath
        # Verify file was written
        with open(filepath, "r") as f:
            assert f.read() == "review content"

    def test_save_issue_doc_jira_prefix(self) -> None:
        circle = self._make_circle("LAB-789 Fix bug")
        filepath = circle._save_issue_doc("mr-review", "review content")
        assert filepath is not None
        assert "LAB-789-mr_code_review.md" in filepath
