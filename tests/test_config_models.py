"""Tests for config schema validation (config_models.py)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config_models import (
    ConfigValidationError,
    cross_reference_warnings,
    validate_structure,
)

VALID_CONFIG = {
    "repos": {"api": "kanta-api", "front": "kanta-front"},
    "workspace_groups": {
        "feature": {"base_env": "WS_FEATURE", "repos": ["api", "front"]},
    },
    "forges": {
        "dev": {
            "channel": "dev",
            "workspace_group": "feature",
            "ritual": ["plan", {"parallel": ["review-a", "review-b"]}, "implement"],
        }
    },
}

VALID_SPELLS = {
    "plan": {"prompt": "plan {task}"},
    "implement": {"prompt": "do {task}"},
    "review-a": {"prompt": "review"},
    "review-b": {"prompt": "review"},
}


class TestValidateStructure:
    def test_valid_config_passes(self) -> None:
        validate_structure(VALID_CONFIG, VALID_SPELLS)  # must not raise

    def test_forge_missing_required_channel(self) -> None:
        bad = {"forges": {"dev": {"workspace_group": "feature"}}}
        with pytest.raises(ConfigValidationError, match="channel"):
            validate_structure(bad, VALID_SPELLS)

    def test_workspace_group_missing_base_env(self) -> None:
        bad = {
            "workspace_groups": {"feature": {"repos": ["api"]}},
            "forges": {},
        }
        with pytest.raises(ConfigValidationError, match="base_env"):
            validate_structure(bad, VALID_SPELLS)

    def test_wrong_type_raises(self) -> None:
        bad = {"repos": ["not", "a", "mapping"], "forges": {}}
        with pytest.raises(ConfigValidationError):
            validate_structure(bad, VALID_SPELLS)

    def test_spell_without_prompt_rejected(self) -> None:
        bad_spells = {"plan": {"runner": "claude"}}
        with pytest.raises(ConfigValidationError, match="prompt"):
            validate_structure(VALID_CONFIG, bad_spells)

    def test_spell_with_prompt_file_ok(self) -> None:
        spells = {"plan": {"prompt_file": "~/plan.md"}}
        cfg = {"forges": {}}
        validate_structure(cfg, spells)  # must not raise

    def test_unknown_keys_allowed(self) -> None:
        cfg = {
            "forges": {"dev": {"channel": "dev", "workspace_group": "g", "future_key": 1}},
            "some_future_section": {"x": 1},
        }
        validate_structure(cfg, VALID_SPELLS)  # extra keys tolerated


class TestCrossReferenceWarnings:
    def test_clean_config_no_warnings(self) -> None:
        assert cross_reference_warnings(VALID_CONFIG, VALID_SPELLS) == []

    def test_unknown_workspace_group(self) -> None:
        cfg = {
            "workspace_groups": {},
            "forges": {"dev": {"channel": "dev", "workspace_group": "ghost", "ritual": []}},
        }
        warnings = cross_reference_warnings(cfg, {})
        assert any("ghost" in w for w in warnings)

    def test_repo_not_in_repos(self) -> None:
        cfg = {
            "repos": {"api": "x"},
            "workspace_groups": {"g": {"base_env": "E", "repos": ["api", "missing"]}},
            "forges": {},
        }
        warnings = cross_reference_warnings(cfg, {})
        assert any("missing" in w for w in warnings)

    def test_ritual_step_without_spell(self) -> None:
        cfg = {
            "workspace_groups": {"g": {"base_env": "E", "repos": []}},
            "forges": {"dev": {"channel": "dev", "workspace_group": "g", "ritual": ["ghost-step"]}},
        }
        warnings = cross_reference_warnings(cfg, {"plan": {"prompt": "x"}})
        assert any("ghost-step" in w for w in warnings)

    def test_parallel_substep_without_spell(self) -> None:
        cfg = {
            "workspace_groups": {"g": {"base_env": "E", "repos": []}},
            "forges": {
                "dev": {
                    "channel": "dev",
                    "workspace_group": "g",
                    "ritual": [{"parallel": ["ghost-parallel"]}],
                }
            },
        }
        warnings = cross_reference_warnings(cfg, {})
        assert any("ghost-parallel" in w for w in warnings)

    def test_unknown_on_complete(self) -> None:
        cfg = {
            "workspace_groups": {"g": {"base_env": "E", "repos": []}},
            "forges": {
                "dev": {"channel": "dev", "workspace_group": "g", "ritual": [], "on_complete": "nope"}
            },
        }
        warnings = cross_reference_warnings(cfg, {})
        assert any("nope" in w for w in warnings)
