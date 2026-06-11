"""Pydantic schema validation for mycel_config.yaml and spells.yaml.

Validation runs at startup and on every hot-reload. Two layers:

1. **Structural** (Pydantic models) — wrong types, missing required fields.
   Raises :class:`ConfigValidationError` with a readable, aggregated message so
   a malformed config fails fast with a clear explanation instead of a cryptic
   ``KeyError`` deep inside a workflow run.
2. **Cross-reference** (:func:`cross_reference_warnings`) — a forge pointing at
   an unknown ``workspace_group``, a group listing a repo absent from ``repos``,
   a ritual step with no matching spell, an ``on_complete`` to an unknown forge.
   These are returned as warnings (logged, non-fatal): they're common mid-edit
   and only bite when that specific forge runs.

If Pydantic isn't installed, validation degrades to a no-op (logged once) so the
bot still starts — the dependency is declared in requirements.txt.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("mycel.core")

try:
    from pydantic import BaseModel, ConfigDict, model_validator

    _PYDANTIC = True
except ImportError:  # pragma: no cover - exercised only without the dep
    _PYDANTIC = False


class ConfigValidationError(Exception):
    """Raised when the config fails structural (schema) validation."""


# A ritual step is either a plain spell name or a {"parallel": [names]} mapping.
RitualStep = Union[str, Dict[str, List[str]]]


if _PYDANTIC:

    class _Base(BaseModel):
        # Allow unknown keys: the config carries product-specific extensions we
        # don't want to enumerate, and forward-compat shouldn't break startup.
        model_config = ConfigDict(extra="allow")

    class WorkspaceGroupModel(_Base):
        base_env: str
        repos: List[str] = []
        git_worktree: bool = False

    class ForgeModel(_Base):
        description: str = ""
        familiar: str = "claude"
        channel: str
        # Optional: the code degrades to an empty workspace when absent. A
        # workspace_group that points to an unknown group is flagged as a
        # cross-reference warning rather than a hard error.
        workspace_group: Optional[str] = None
        ritual: List[RitualStep] = []
        dynamic_workspace: bool = False
        git_worktree: Optional[bool] = None
        on_complete: Optional[str] = None
        provisioner: Optional[str] = None
        kanta_stack: Dict[str, Any] = {}

        @model_validator(mode="after")
        def _known_provisioner(self) -> "ForgeModel":
            if self.provisioner and self.provisioner not in ("kanta_stack", "clone"):
                raise ValueError(
                    f"unknown provisioner '{self.provisioner}' (expected kanta_stack or clone)"
                )
            return self

    class MycelConfigModel(_Base):
        repos: Dict[str, str] = {}
        workspace_groups: Dict[str, WorkspaceGroupModel] = {}
        # `forges:` is canonical; `circles:` is the accepted legacy alias.
        forges: Dict[str, ForgeModel] = {}
        circles: Dict[str, ForgeModel] = {}
        # Path to the kanta-stack CLI for the kanta_stack provisioner.
        kanta_stack: Dict[str, Any] = {}

    class SpellModel(_Base):
        prompt: Optional[str] = None
        prompt_file: Optional[str] = None
        runner: str = "claude"

        @model_validator(mode="after")
        def _need_a_prompt(self) -> "SpellModel":
            if not self.prompt and not self.prompt_file:
                raise ValueError("a spell needs either `prompt` or `prompt_file`")
            return self


def _format_pydantic_error(exc: Any, source: str) -> str:
    lines = [f"Invalid {source}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"  - {loc or '(root)'}: {err.get('msg')}")
    return "\n".join(lines)


def validate_structure(config: Dict[str, Any], spells: Dict[str, Any]) -> None:
    """Validate the structure of config + spells. Raises ConfigValidationError.

    No-op (with a one-time warning) if Pydantic isn't installed.
    """
    if not _PYDANTIC:
        logger.warning("pydantic not installed — skipping config schema validation")
        return

    from pydantic import ValidationError

    try:
        MycelConfigModel.model_validate(config or {})
    except ValidationError as exc:
        raise ConfigValidationError(_format_pydantic_error(exc, "mycel_config.yaml")) from exc

    errors: List[str] = []
    for name, spell in (spells or {}).items():
        if not isinstance(spell, dict):
            errors.append(f"  - {name}: expected a mapping, got {type(spell).__name__}")
            continue
        try:
            SpellModel.model_validate(spell)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                errors.append(f"  - {name}.{loc}: {err.get('msg')}")
    if errors:
        raise ConfigValidationError("Invalid spells.yaml:\n" + "\n".join(errors))


def _ritual_spell_names(ritual: List[RitualStep]) -> List[str]:
    names: List[str] = []
    for step in ritual or []:
        if isinstance(step, str):
            names.append(step)
        elif isinstance(step, dict):
            for sub in step.values():
                if isinstance(sub, list):
                    names.extend(s for s in sub if isinstance(s, str))
    return names


def cross_reference_warnings(config: Dict[str, Any], spells: Dict[str, Any]) -> List[str]:
    """Return non-fatal consistency warnings (unknown references etc.)."""
    warnings: List[str] = []
    config = config or {}
    spells = spells or {}

    repos = set((config.get("repos") or {}).keys())
    groups = config.get("workspace_groups") or {}
    forges = config.get("forges") or config.get("circles") or {}
    spell_names = set(spells.keys())

    for group_name, group in groups.items():
        if not isinstance(group, dict):
            continue
        for repo_key in group.get("repos", []) or []:
            if repo_key not in repos:
                warnings.append(
                    f"workspace_group '{group_name}' references repo '{repo_key}' "
                    f"not declared in `repos:`"
                )

    for forge_name, forge in forges.items():
        if not isinstance(forge, dict):
            continue
        group = forge.get("workspace_group")
        if group and group not in groups:
            warnings.append(
                f"forge '{forge_name}' references unknown workspace_group '{group}'"
            )
        on_complete = forge.get("on_complete")
        if on_complete and on_complete not in forges:
            warnings.append(
                f"forge '{forge_name}' on_complete points to unknown forge '{on_complete}'"
            )
        for spell_name in _ritual_spell_names(forge.get("ritual", [])):
            if spell_name not in spell_names:
                warnings.append(
                    f"forge '{forge_name}' ritual step '{spell_name}' has no matching spell"
                )

    return warnings
