"""Tests for Mycel orchestrator: defensive hot-reload and the aborted guard."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mycel import Mycel

CONFIG_YAML = """\
forges:
  dev:
    channel: dev
    ritual: [step-a, step-b]
    familiar: claude
    workspace_group: g
workspace_groups:
  g:
    base_env: NOPE
    repos: []
"""

SPELLS_YAML = """\
spells:
  step-a:
    prompt: do {task}
    runner: claude
  step-b:
    prompt: then {task}
    runner: claude
"""


def _make_mycel(tmp: str, config: str = CONFIG_YAML, spells: str = SPELLS_YAML) -> Mycel:
    cfg = os.path.join(tmp, "config.yaml")
    sp = os.path.join(tmp, "spells.yaml")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(config)
    with open(sp, "w", encoding="utf-8") as fh:
        fh.write(spells)
    return Mycel(config_path=cfg, spells_path=sp, bus_dir=os.path.join(tmp, "bus"))


class TestReloadConfig:
    def test_reload_picks_up_new_spell(self) -> None:
        tmp = tempfile.mkdtemp()
        m = _make_mycel(tmp)
        assert set(m.spells_config) == {"step-a", "step-b"}

        new_spells = SPELLS_YAML + "  step-c:\n    prompt: also {task}\n    runner: claude\n"
        with open(m.spells_path, "w", encoding="utf-8") as fh:
            fh.write(new_spells)

        result = m.reload_config()
        assert "reloaded" in result.lower()
        assert "step-c" in m.spells_config
        # Idle forge picks up the new spells dict.
        assert m.forges["dev"].spells is m.spells_config

    def test_malformed_yaml_leaves_config_unchanged(self) -> None:
        tmp = tempfile.mkdtemp()
        m = _make_mycel(tmp)
        good_spells = m.spells_config
        good_config = m.config

        with open(m.spells_path, "w", encoding="utf-8") as fh:
            fh.write("spells:\n  step-a: [unterminated\n")

        result = m.reload_config()
        assert "error" in result.lower()
        # Live state untouched — same objects, not a half-built dict.
        assert m.spells_config is good_spells
        assert m.config is good_config

    def test_reload_rejects_structurally_invalid_config(self) -> None:
        tmp = tempfile.mkdtemp()
        m = _make_mycel(tmp)
        good_config = m.config

        # A forge missing the required `channel` field.
        with open(m.config_path, "w", encoding="utf-8") as fh:
            fh.write("forges:\n  dev:\n    workspace_group: g\n")

        result = m.reload_config()
        assert "rejected" in result.lower()
        assert m.config is good_config  # not committed

    def test_spells_deferred_for_running_forge(self) -> None:
        tmp = tempfile.mkdtemp()
        m = _make_mycel(tmp)
        running_forge = m.forges["dev"]
        old_spells = running_forge.spells
        running_forge.state["status"] = "running"

        new_spells = SPELLS_YAML + "  step-c:\n    prompt: also {task}\n    runner: claude\n"
        with open(m.spells_path, "w", encoding="utf-8") as fh:
            fh.write(new_spells)

        result = m.reload_config()
        # New config is committed globally...
        assert "step-c" in m.spells_config
        # ...but the running forge keeps the spells it started with.
        assert running_forge.spells is old_spells
        assert "dev" in result


class TestAbortedGuard:
    @pytest.mark.asyncio
    async def test_resume_refused_when_aborted(self) -> None:
        tmp = tempfile.mkdtemp()
        m = _make_mycel(tmp)
        m.forges["dev"].state["status"] = "aborted"
        m.forges["dev"].state["aborted_at_skill"] = "step-a"
        with pytest.raises(ValueError, match="aborted"):
            await m.resume_forge("dev")

    @pytest.mark.asyncio
    async def test_retry_refused_when_aborted(self) -> None:
        tmp = tempfile.mkdtemp()
        m = _make_mycel(tmp)
        m.forges["dev"].state["status"] = "aborted"
        with pytest.raises(ValueError, match="aborted"):
            await m.retry_forge("dev")
