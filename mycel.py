from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml

from config_models import ConfigValidationError, cross_reference_warnings, validate_structure
from control_server import ControlServer
from forge import Forge
from message_bus import Message, MessageBus
from runner import check_runners
from aikido_monitor import AikidoMonitor
from sentry_monitor import SentryMonitor

logger = logging.getLogger("mycel.core")


def _format_duration(seconds: float) -> str:
    """Render a duration as a short human-readable string (e.g., '12 min', '3 h')."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        h = seconds / 3600
        return f"{h:.1f} h" if h < 10 else f"{int(h)} h"
    return f"{int(seconds // 86400)} d"


class _QueueItem:
    """A pending task in the queue."""

    def __init__(
        self,
        forge_name: str,
        task: str,
        instructions: Optional[str] = None,
        spell_name: Optional[str] = None,
        from_spell: Optional[str] = None,
    ) -> None:
        self.forge_name = forge_name
        self.task = task
        self.instructions = instructions
        self.spell_name = spell_name
        self.from_spell = from_spell


class Mycel:
    """Loads config, instantiates forges (YAML `forges:`), routes commands."""

    def __init__(
        self,
        config_path: str = "mycel_config.yaml",
        spells_path: str = "spells.yaml",
        bus_dir: str = "bus",
    ) -> None:
        # Backwards-compat: fall back to the legacy filenames if the new ones
        # don't exist, so deployments can rename at their own pace.
        if not os.path.exists(config_path) and os.path.exists("dispatch_config.yaml"):
            config_path = "dispatch_config.yaml"
        if not os.path.exists(spells_path) and os.path.exists("skills.yaml"):
            spells_path = "skills.yaml"

        self.config_path = config_path
        self.spells_path = spells_path
        self.bus_dir = bus_dir

        self.config: Dict[str, Any] = {}
        self.spells_config: Dict[str, Dict[str, Any]] = {}
        self.workspace: Dict[str, str] = {}
        self.docs_path: str = ""
        self.issues_dir: str = ""
        self.claude_config: Dict[str, Any] = {}
        self.forges: Dict[str, Forge] = {}
        self.bus = MessageBus(bus_dir=bus_dir)
        self.control_server = ControlServer(self, bus_dir=bus_dir)
        self.familiar_status: Dict[str, bool] = {}
        self.sentry_monitor: Optional[SentryMonitor] = None
        self.aikido_monitor: Optional[AikidoMonitor] = None

        self._forge_queues: Dict[str, asyncio.Queue[_QueueItem]] = {}
        self._active_items: Dict[str, Optional[_QueueItem]] = {}
        self._worker_started = False

        self._load_config()
        self._load_spells()
        # Fail fast on a structurally invalid config; log soft inconsistencies.
        validate_structure(self.config, self.spells_config)
        self._log_config_warnings()
        self._build_forges()
        self._check_familiars()
        self._init_sentry_monitor()
        self._init_aikido_monitor()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _forges_section(self) -> Dict[str, Any]:
        """Read `forges:` from config, falling back to legacy `circles:`."""
        return self.config.get("forges", self.config.get("circles", {})) or {}

    def _parse_config_file(self) -> Dict[str, Any]:
        """Read + parse the config YAML. Pure: returns a dict, mutates nothing."""
        with open(self.config_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _parse_spells_file(self) -> Dict[str, Dict[str, Any]]:
        """Read + parse the spells YAML, resolving external prompt files.

        Pure: builds and returns a fresh dict, mutates no instance state — so a
        parse failure can't leave a half-built spells_config behind.
        """
        with open(self.spells_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        # Prefer `spells:`; fall back to legacy `skills:`
        spells = data.get("spells", data.get("skills", {})) or {}

        loaded_external = 0
        for sp_name, sp in spells.items():
            prompt_file = sp.get("prompt_file")
            if prompt_file:
                prompt_file = os.path.expanduser(prompt_file)
                if os.path.isfile(prompt_file):
                    try:
                        with open(prompt_file, "r", encoding="utf-8") as pf:
                            content = pf.read()
                        # Translate Claude-Code slash-command convention `$ARGUMENTS`
                        # to the Forge template variable `{task}` so the same
                        # SKILL.md works both as a slash command and as a spell.
                        content = content.replace("$ARGUMENTS", "{task}\n\n{instructions}")
                        sp["prompt"] = content
                        loaded_external += 1
                    except OSError as exc:
                        logger.warning("Could not read %s for spell %s: %s", prompt_file, sp_name, exc)
                else:
                    logger.warning("prompt_file not found for spell %s: %s", sp_name, prompt_file)

        logger.info("Loaded spells from %s (%d spells, %d external prompts)", self.spells_path, len(spells), loaded_external)
        return spells

    def _apply_config(self, config: Dict[str, Any]) -> None:
        """Commit a parsed config dict to instance state and derive lookups."""
        self.config = config
        self.docs_path = os.path.expanduser(os.environ.get("DOCS_PATH", self.config.get("docs_path", "")))
        self.issues_dir = os.path.expanduser(os.environ.get("ISSUES_DIR", self.config.get("issues_dir", "")))

        self._repo_folders: Dict[str, str] = self.config.get("repos", {})
        self._workspace_groups: Dict[str, Dict[str, Any]] = self.config.get("workspace_groups", {})

        workspace: Dict[str, str] = {}
        for group_cfg in self._workspace_groups.values():
            base_env = group_cfg.get("base_env", "")
            base_path = os.path.expanduser(os.environ.get(base_env, ""))
            if not base_path:
                continue
            for repo_key in group_cfg.get("repos", []):
                folder = self._repo_folders.get(repo_key, repo_key)
                if repo_key not in workspace:
                    workspace[repo_key] = os.path.join(base_path, folder)
        self.workspace = workspace

        self.claude_config = self.config.get("claude", {})
        logger.info("Loaded config from %s (%d forges, %d repos)", self.config_path, len(self._forges_section()), len(self._repo_folders))

    def _log_config_warnings(self) -> None:
        """Log non-fatal config consistency warnings (unknown references etc.)."""
        for warning in cross_reference_warnings(self.config, self.spells_config):
            logger.warning("Config: %s", warning)

    def _load_config(self) -> None:
        self._apply_config(self._parse_config_file())

    def _load_spells(self) -> None:
        self.spells_config = self._parse_spells_file()

    def reload_config(self) -> str:
        """Hot-reload config and spells without losing in-memory forge state.

        Transactional: both files are parsed into temporaries *before* any live
        state is touched, so a malformed YAML leaves the running config intact.
        Forges that are mid-workflow (running/paused) keep the spell definitions
        they started with — swapping under them could KeyError on a removed step;
        they pick up the new spells on their next run.
        """
        try:
            new_config = self._parse_config_file()
            new_spells = self._parse_spells_file()
            # Validate the parsed temporaries before committing anything.
            validate_structure(new_config, new_spells)
        except ConfigValidationError as exc:
            logger.error("Reload rejected (config left unchanged):\n%s", exc)
            return f"Reload rejected (config left unchanged):\n{exc}"
        except Exception as exc:
            logger.error("Reload error (config left unchanged): %s", exc)
            return f"Reload error (config left unchanged): {exc}"

        self._apply_config(new_config)
        self.spells_config = new_spells
        self._log_config_warnings()

        deferred: List[str] = []
        for forge_name, forge in self.forges.items():
            if forge.state.get("status") in ("running", "paused"):
                deferred.append(forge_name)
                continue
            forge.spells = self.spells_config

        for forge_name in self._forges_section():
            if forge_name not in self.forges:
                self._build_single_forge(forge_name)

        self._check_familiars()
        msg = f"Config reloaded: {len(self.spells_config)} spells, {len(self.forges)} forges"
        if deferred:
            msg += f" — spells kept for active forge(s) until idle: {', '.join(deferred)}"
        return msg

    def _resolve_forge_workspace(self, forge_cfg: Dict[str, Any]) -> Dict[str, str]:
        """Resolve the workspace for a forge from its workspace_group."""
        group_name = forge_cfg.get("workspace_group")
        if not group_name or group_name not in self._workspace_groups:
            return {}

        group_cfg = self._workspace_groups[group_name]
        base_env = group_cfg.get("base_env", "")
        base_path = os.path.expanduser(os.environ.get(base_env, ""))
        if not base_path:
            logger.warning("Env var %s not set for group %s", base_env, group_name)
            return {}

        repo_keys: List[str] = group_cfg.get("repos", [])
        result: Dict[str, str] = {}
        for repo_key in repo_keys:
            folder = self._repo_folders.get(repo_key, repo_key)
            result[repo_key] = os.path.join(base_path, folder)
        return result

    def _forge_use_git_worktree(self, forge_cfg: Dict[str, Any]) -> bool:
        """Per-forge `git_worktree` overrides the workspace group default."""
        group_name = forge_cfg.get("workspace_group", "")
        group = self._workspace_groups.get(group_name, {})
        if "git_worktree" in forge_cfg:
            return bool(forge_cfg.get("git_worktree"))
        return bool(group.get("git_worktree", False))

    def _build_single_forge(self, forge_name: str) -> None:
        defaults = self.config.get("defaults", {})
        forge_cfg = self._forges_section()[forge_name]
        forge = Forge(
            name=forge_name,
            description=forge_cfg.get("description", ""),
            workflow=forge_cfg.get("ritual", []),
            spells=self.spells_config,
            workspace=self._resolve_forge_workspace(forge_cfg),
            bus=self.bus,
            default_runner=forge_cfg.get("familiar", defaults.get("familiar", "claude")),
            max_retries=defaults.get("max_retries", 3),
            timeout=defaults.get("timeout", 180),
            bus_dir=self.bus_dir,
            docs_path=self.docs_path,
            issues_dir=self.issues_dir,
            runner_kwargs=self.claude_config,
            use_git_worktree=self._forge_use_git_worktree(forge_cfg),
            dynamic_workspace=bool(forge_cfg.get("dynamic_workspace", False)),
            gitlab_config=self.config.get("gitlab", {}),
            docker_env_mapping=self.config.get("docker_env_mapping", {}),
            repo_folders=self._repo_folders,
        )
        self.forges[forge_name] = forge

    def _build_forges(self) -> None:
        for forge_name in self._forges_section():
            self._build_single_forge(forge_name)

    def _check_familiars(self) -> None:
        self.familiar_status = check_runners()
        if not self.familiar_status.get("claude"):
            logger.warning("Claude CLI not found — forges cannot run.")

    @staticmethod
    def _remediation_auto_fix_override() -> Optional[bool]:
        """Read REMEDIATION_AUTO_FIX from env. Returns None if unset.

        true/1/yes  → auto-fix on  (monitor enqueues forge automatically)
        false/0/no  → auto-fix off (alert only, with a button to launch /fix)
        """
        raw = os.environ.get("REMEDIATION_AUTO_FIX", "").strip().lower()
        if raw in ("true", "1", "yes", "on"):
            return True
        if raw in ("false", "0", "no", "off"):
            return False
        return None

    def _apply_auto_fix_override(self, monitor_config: Dict[str, Any]) -> Dict[str, Any]:
        override = self._remediation_auto_fix_override()
        if override is None:
            return monitor_config
        return {**monitor_config, "auto_fix": override}

    def _init_sentry_monitor(self) -> None:
        sentry_config = self._apply_auto_fix_override(self.config.get("sentry_monitor", {}))
        if sentry_config.get("enabled", False):
            self.sentry_monitor = SentryMonitor(
                bus=self.bus, config=sentry_config, bus_dir=self.bus_dir,
            )

    async def start_sentry_monitor(self) -> None:
        if self.sentry_monitor is None:
            return
        async def _enqueue_callback(forge_name: str, task: str) -> None:
            await self.enqueue_forge(forge_name, task)
        await self.sentry_monitor.start(enqueue_callback=_enqueue_callback)

    async def stop_sentry_monitor(self) -> None:
        if self.sentry_monitor:
            await self.sentry_monitor.stop()

    async def run_sentry_check(self) -> int:
        if self.sentry_monitor is None:
            sentry_config = self._apply_auto_fix_override(self.config.get("sentry_monitor", {}))
            self.sentry_monitor = SentryMonitor(bus=self.bus, config=sentry_config, bus_dir=self.bus_dir)
        async def _enqueue_callback(forge_name: str, task: str) -> None:
            await self.enqueue_forge(forge_name, task)
        return await self.sentry_monitor.run_check(enqueue_callback=_enqueue_callback)

    def _init_aikido_monitor(self) -> None:
        aikido_config = self._apply_auto_fix_override(self.config.get("aikido_monitor", {}))
        if aikido_config.get("enabled", False):
            self.aikido_monitor = AikidoMonitor(
                bus=self.bus, config=aikido_config, bus_dir=self.bus_dir,
            )

    async def start_aikido_monitor(self) -> None:
        if self.aikido_monitor is None:
            return
        async def _enqueue_callback(forge_name: str, task: str) -> None:
            await self.enqueue_forge(forge_name, task)
        await self.aikido_monitor.start(enqueue_callback=_enqueue_callback)

    async def stop_aikido_monitor(self) -> None:
        if self.aikido_monitor:
            await self.aikido_monitor.stop()

    async def run_aikido_check(self) -> int:
        if self.aikido_monitor is None:
            aikido_config = self._apply_auto_fix_override(self.config.get("aikido_monitor", {}))
            self.aikido_monitor = AikidoMonitor(bus=self.bus, config=aikido_config, bus_dir=self.bus_dir)
        async def _enqueue_callback(forge_name: str, task: str) -> None:
            await self.enqueue_forge(forge_name, task)
        return await self.aikido_monitor.run_check(enqueue_callback=_enqueue_callback)

    # ------------------------------------------------------------------
    # Task queue
    # ------------------------------------------------------------------

    async def start_worker(self) -> None:
        if not self._worker_started:
            for forge_name in self.forges:
                queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
                self._forge_queues[forge_name] = queue
                self._active_items[forge_name] = None
                asyncio.create_task(self._forge_worker(forge_name, queue))
            self._worker_started = True
            logger.info("Workers started (%d forges)", len(self.forges))
            await self._recover_forges()

    async def _recover_forges(self) -> None:
        # Auto-resume window: a forge paused longer than this (in seconds) is
        # considered stale — likely the user finished the work outside mycel
        # or moved on to something else. We require explicit confirmation
        # rather than silently re-running spells (which can re-create MRs).
        # Set to 0 in config to disable auto-resume entirely.
        max_age_s = float(self.config.get("auto_resume_max_age_s", 1800))  # 30 min

        for forge_name, forge in self.forges.items():
            status = forge.state.get("status")
            if status not in ("running", "paused"):
                continue
            task = forge.state.get("task")
            current_spell = forge.state.get("current_skill")
            if not task or not current_spell:
                forge.state["status"] = "idle"
                forge.state["error"] = "Interrupted"
                forge._save_state()
                continue

            try:
                age_s = time.time() - os.path.getmtime(forge._state_path)
            except OSError:
                age_s = 0.0

            # Safeguard 1: a "running" state at restart means the bot was
            # killed mid-spell. The workspace may already have side effects
            # (commits, files written) and the user might have continued
            # manually. Never silently re-run — pause and let them decide.
            if status == "running":
                forge.state["status"] = "paused"
                forge.state["error"] = f"Interrupted mid-/{current_spell}"
                forge._save_state()
                await self.bus.publish(Message(
                    source="mycel",
                    content=(
                        f"⚠️ **Mycel** → Forge **{forge_name}** was interrupted "
                        f"while running /{current_spell} ({_format_duration(age_s)} ago).\n"
                        f"Not auto-resuming — the workspace may have changed since.\n"
                        f"• `!{forge_name} resume` → continue from where it stopped\n"
                        f"• `!{forge_name} retry` → re-run /{current_spell}\n"
                        f"• `!{forge_name} reset` → discard the run"
                    ),
                    level="warning",
                    forge_name=forge_name,
                    data={"recovery_pending": True, "current_skill": current_spell},
                ))
                continue

            # Safeguard 2: stale paused state. The user may have finished the
            # task manually or switched to something else. Don't auto-resume
            # — surface the choice instead.
            if max_age_s > 0 and age_s > max_age_s:
                await self.bus.publish(Message(
                    source="mycel",
                    content=(
                        f"⚠️ **Mycel** → Forge **{forge_name}** has been paused "
                        f"after /{current_spell} for {_format_duration(age_s)} — not "
                        f"auto-resuming.\n"
                        f"• `!{forge_name} resume` → continue\n"
                        f"• `!{forge_name} reset` → discard"
                    ),
                    level="warning",
                    forge_name=forge_name,
                    data={"recovery_pending": True, "current_skill": current_spell},
                ))
                continue

            # Smart resume: when paused after a spell that already completed
            # (output is in step_outputs), advance to the next spell instead of
            # re-running the (potentially expensive) current one.
            resume_from = current_spell
            if current_spell in forge.state.get("step_outputs", {}):
                spell_cfg = self.spells_config.get(current_spell, {})
                next_spell = spell_cfg.get("next_on_pass")
                if next_spell and next_spell in self.spells_config:
                    resume_from = next_spell
                    logger.info(
                        "Recovery: forge %s paused after /%s (already done) → advancing to /%s",
                        forge_name, current_spell, next_spell,
                    )

            logger.info(
                "Recovery: forge %s was %s on /%s (%.0fs ago), resuming at /%s",
                forge_name, status, current_spell, age_s, resume_from,
            )
            await self.bus.publish(Message(
                source="mycel",
                content=(
                    f"\U0001f504 **Mycel** → Auto-resuming forge **{forge_name}** "
                    f"(spell /{resume_from}, paused {_format_duration(age_s)} ago)"
                ),
                level="info",
                forge_name=forge_name,
            ))
            forge.state["status"] = "idle"
            forge._save_state()
            await self.enqueue_from_spell(forge_name, resume_from)

    async def _forge_worker(self, forge_name: str, queue: asyncio.Queue[_QueueItem]) -> None:
        while True:
            item = await queue.get()
            self._active_items[forge_name] = item

            try:
                forge = self.forges[forge_name]
                await self.bus.publish(Message(
                    source="mycel",
                    content=f"\U0001f344 **Mycel** → Starting forge **{forge_name}**",
                    level="info",
                    forge_name=forge_name,
                ))

                if item.from_spell:
                    logger.info("Worker %s: resume from /%s", forge_name, item.from_spell)
                    coro = forge.run_from_skill(item.from_spell, item.instructions)
                elif item.spell_name:
                    logger.info("Worker %s: run spell /%s", forge_name, item.spell_name)
                    coro = forge.run_single_skill(item.spell_name, item.task, item.instructions)
                else:
                    logger.info("Worker %s: run ritual", forge_name)
                    coro = forge.run_workflow(item.task, item.instructions)

                forge._running_task = asyncio.create_task(coro)
                try:
                    await forge._running_task
                except asyncio.CancelledError:
                    logger.info("Worker %s: run cancelled (abort)", forge_name)

            except Exception as exc:
                logger.error("Worker %s: error: %s", forge_name, exc, exc_info=True)
                await self.bus.publish(Message(
                    source="mycel",
                    content=f"\U0001f4a5 **Mycel** → Error on forge **{forge_name}** : {exc}",
                    level="error",
                    forge_name=forge_name,
                ))
            finally:
                self._active_items[forge_name] = None
                queue.task_done()

                forge = self.forges[forge_name]
                forge._running_task = None
                if forge.state.get("status") == "completed":
                    await self._trigger_chain(forge_name, item)

                remaining = queue.qsize()
                if remaining > 0:
                    logger.info("Worker %s: %d task(s) remaining", forge_name, remaining)

    @property
    def task_running(self) -> bool:
        return any(item is not None for item in self._active_items.values())

    @property
    def queue_size(self) -> int:
        return sum(q.qsize() for q in self._forge_queues.values())

    @property
    def control_socket_path(self) -> str:
        """Path to the ControlServer Unix socket the concierge MCP server connects to."""
        return self.control_server.socket_path

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _trigger_chain(self, forge_name: str, completed_item: _QueueItem) -> None:
        forge_cfg = self._forges_section().get(forge_name, {})
        next_forge = forge_cfg.get("on_complete")
        if not next_forge or next_forge not in self.forges:
            return
        logger.info("Chain: forge %s done, triggering %s", forge_name, next_forge)
        await self.bus.publish(Message(
            source="mycel",
            content=f"\U0001f517 **Mycel** → Chain: **{forge_name}** completed, enqueuing **{next_forge}**",
            level="info",
            forge_name=next_forge,
        ))
        await self.enqueue_forge(next_forge, completed_item.task)

    def _get_forge_queue(self, forge_name: str) -> asyncio.Queue[_QueueItem]:
        queue = self._forge_queues.get(forge_name)
        if queue is None:
            raise ValueError(f"Unknown forge: {forge_name}")
        return queue

    async def enqueue_forge(self, forge_name: str, task: str, instructions: Optional[str] = None) -> int:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        await self.start_worker()
        queue = self._get_forge_queue(forge_name)
        item = _QueueItem(forge_name, task, instructions)
        await queue.put(item)
        position = queue.qsize()
        if position > 1 or self._active_items.get(forge_name) is not None:
            await self.bus.publish(Message(
                source="mycel",
                content=f"\U0001f4cb **Mycel** → Task queued for forge **{forge_name}** (position {position})",
                level="info",
                forge_name=forge_name,
            ))
        logger.info("Task queued for forge %s (position %d)", forge_name, position)
        return position

    async def enqueue_forge_spell(self, forge_name: str, spell_name: str, task: str, instructions: Optional[str] = None) -> int:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        if spell_name not in self.spells_config:
            raise ValueError(f"Unknown spell: {spell_name}")
        await self.start_worker()
        queue = self._get_forge_queue(forge_name)
        item = _QueueItem(forge_name, task, instructions, spell_name=spell_name)
        await queue.put(item)
        position = queue.qsize()
        if position > 1 or self._active_items.get(forge_name) is not None:
            await self.bus.publish(Message(
                source="mycel",
                content=f"\U0001f4cb **Mycel** → Spell /{spell_name} queued for forge **{forge_name}** (position {position})",
                level="info",
                forge_name=forge_name,
            ))
        return position

    async def enqueue_from_spell(self, forge_name: str, from_spell: str, instructions: Optional[str] = None) -> int:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        await self.start_worker()
        queue = self._get_forge_queue(forge_name)
        task = self.forges[forge_name].state.get("task") or ""
        item = _QueueItem(forge_name, task, instructions, from_spell=from_spell)
        await queue.put(item)
        position = queue.qsize()
        logger.info("Resume forge %s from /%s (position %d)", forge_name, from_spell, position)
        return position

    @staticmethod
    def _guard_not_aborted(forge: Forge, action: str) -> None:
        """Refuse resume/retry on an aborted forge — its last step may be half-done."""
        if forge.state.get("status") == "aborted":
            at = forge.state.get("aborted_at_skill", "?")
            raise ValueError(
                f"Forge {forge.name} was aborted at /{at}. "
                f"Cannot {action} — the step may be partially applied. "
                f"Run `reset` to start fresh, or `from <step>` to restart from a chosen step."
            )

    async def resume_forge(self, forge_name: str, instructions: Optional[str] = None) -> None:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        forge = self.forges[forge_name]
        self._guard_not_aborted(forge, "resume")

        # Active worker awaiting evt.wait() → just signal it
        running_task = forge._running_task
        if running_task is not None and not running_task.done():
            forge.resume(instructions)
            return

        # No live worker (bot was restarted while paused) → re-enqueue.
        # If the current spell already completed (output in step_outputs), advance
        # to the next spell rather than re-running the expensive one.
        current_spell = forge.state.get("current_skill")
        if not current_spell:
            forge.resume(instructions)
            return

        resume_from = current_spell
        if current_spell in forge.state.get("step_outputs", {}):
            spell_cfg = self.spells_config.get(current_spell, {})
            next_spell = spell_cfg.get("next_on_pass")
            if next_spell and next_spell in self.spells_config:
                resume_from = next_spell

        forge.state["status"] = "idle"
        forge._save_state()
        await self.enqueue_from_spell(forge_name, resume_from, instructions)

    async def retry_forge(self, forge_name: str, instructions: Optional[str] = None) -> None:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        forge = self.forges[forge_name]
        self._guard_not_aborted(forge, "retry")

        running_task = forge._running_task
        if running_task is not None and not running_task.done():
            forge.retry(instructions)
            return

        # No live worker → re-enqueue the current spell to retry it.
        current_spell = forge.state.get("current_skill")
        if not current_spell:
            forge.retry(instructions)
            return

        forge.state["status"] = "idle"
        forge._save_state()
        await self.enqueue_from_spell(forge_name, current_spell, instructions)

    def abort_forge(self, forge_name: str) -> bool:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        return self.forges[forge_name].abort()

    def reset_forge(self, forge_name: str) -> None:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        self.forges[forge_name].reset()

    def reset_all(self) -> None:
        for forge in self.forges.values():
            forge.reset()

    def reset_forge_metrics(self, forge_name: str) -> None:
        if forge_name not in self.forges:
            raise ValueError(f"Unknown forge: {forge_name}")
        self.forges[forge_name].reset_metrics()

    def reset_all_metrics(self) -> None:
        for forge in self.forges.values():
            forge.reset_metrics()

    def inject_feedback(self, forge_name: str, feedback: str) -> None:
        if forge_name not in self.forges:
            return
        self.forges[forge_name].inject_feedback(feedback)

    # ------------------------------------------------------------------
    # Status & logs
    # ------------------------------------------------------------------

    async def get_forge_status(self, forge_name: str) -> str:
        if forge_name not in self.forges:
            return f"Unknown forge: {forge_name}"
        forge = self.forges[forge_name]
        # Refresh git snapshot so manual commits outside Mycel show up
        try:
            await forge.refresh_git_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Forge %s: git snapshot refresh failed: %s", forge_name, exc)
        return forge.status_summary

    async def sync_forge(self, forge_name: str) -> str:
        """Refresh both git and remote (GitLab) snapshots, then return status."""
        if forge_name not in self.forges:
            return f"Unknown forge: {forge_name}"
        forge = self.forges[forge_name]
        try:
            await forge.refresh_git_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Forge %s: git snapshot refresh failed: %s", forge_name, exc)
        try:
            await forge.refresh_external_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Forge %s: external snapshot refresh failed: %s", forge_name, exc)
        return forge.status_summary

    async def get_global_status(self) -> str:
        # Refresh snapshots for forges that are running/paused/failed
        refresh_tasks = [
            forge.refresh_git_snapshot()
            for forge in self.forges.values()
            if forge.state.get("status") in ("running", "paused", "failed", "error")
        ]
        if refresh_tasks:
            await asyncio.gather(*refresh_tasks, return_exceptions=True)
        lines = ["\U0001f344 **Mycel — Dashboard**\n"]
        for forge in self.forges.values():
            lines.append(forge.progress_bar)
        lines.append(f"\nForge running: {'yes' if self.task_running else 'no'}")
        lines.append(f"Queue: {self.queue_size} task(s)")
        check = "✅"
        cross = "❌"
        familiars = ", ".join(f"{k}: {check if v else cross}" for k, v in self.familiar_status.items())
        lines.append(f"Agents: {familiars}")
        if self.sentry_monitor:
            lines.append("")
            lines.append(self.sentry_monitor.status)
        if self.aikido_monitor:
            lines.append("")
            lines.append(self.aikido_monitor.status)
        return "\n".join(lines)

    def list_forges(self) -> str:
        lines = ["⚒️ **Forges**\n"]
        for name, forge in self.forges.items():
            ritual_str = forge.format_workflow(with_backticks=False)
            lines.append(f"**{name}** — {forge.description}")
            lines.append(f"  Workflow: {ritual_str}")
            lines.append(f"  Default agent: {forge.default_runner}")
            lines.append(f"  Command: `!{name} <description>`")
            lines.append("")
        return "\n".join(lines)

    async def get_mcp_status(self, timeout: int = 30) -> str:
        """Run `claude mcp list` and return a Discord-formatted health summary.

        Each line from the CLI looks like:
          `claude.ai Sentry: https://mcp.sentry.dev/mcp - ✓ Connected`
        We bucket by status (connected / auth-needed / failed) and add totals.
        """
        claude_path = shutil.which("claude")
        if not claude_path:
            return "\U0001f50c **MCP servers** — Claude Code CLI not found in PATH"

        try:
            proc = await asyncio.create_subprocess_exec(
                claude_path, "mcp", "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return f"\U0001f50c **MCP servers** — `claude mcp list` timed out after {timeout}s"
        except Exception as exc:
            return f"\U0001f50c **MCP servers** — error: {exc}"

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        connected: List[Tuple[str, str]] = []
        auth_needed: List[Tuple[str, str]] = []
        failed: List[Tuple[str, str]] = []

        for raw in stdout.splitlines():
            line = raw.strip()
            if not line or line.startswith("Checking"):
                continue
            # Format: "<name>: <url> - <status>"
            if " - " not in line or ":" not in line:
                continue
            head, status = line.rsplit(" - ", 1)
            name, _, url = head.partition(":")
            entry = (name.strip(), url.strip())
            status_low = status.strip().lower()
            if "connected" in status_low and "✓" in status:
                connected.append(entry)
            elif "auth" in status_low or status.strip().startswith("!"):
                auth_needed.append(entry)
            else:
                failed.append((name.strip(), status.strip()))

        total = len(connected) + len(auth_needed) + len(failed)
        if total == 0:
            msg = stderr.strip() or stdout.strip() or "no servers configured"
            return f"\U0001f50c **MCP servers** — {msg}"

        lines = [
            f"\U0001f50c **MCP servers** — {len(connected)} connected, "
            f"{len(auth_needed)} need auth, {len(failed)} failed (total: {total})\n"
        ]
        if connected:
            lines.append("✅ **Connected**")
            for name, url in connected:
                lines.append(f"  • {name} — `{url}`")
            lines.append("")
        if auth_needed:
            lines.append("⚠️ **Needs authentication**")
            for name, url in auth_needed:
                lines.append(f"  • {name} — `{url}`")
            lines.append("")
        if failed:
            lines.append("❌ **Failed**")
            for name, status in failed:
                lines.append(f"  • {name} — {status}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def list_spells(self) -> str:
        lines = ["\U0001f344 **Steps**\n"]
        for name, sp in self.spells_config.items():
            lines.append(f"**/{name}** — {sp.get('description', '')}")
            lines.append(f"  Agent: {sp.get('runner', 'claude')} | Timeout: {sp.get('timeout', 180)}s")
            auto = "yes" if sp.get("auto_advance", True) else "no (pauses)"
            lines.append(f"  Auto-advance: {auto}")
            lines.append("")
        return "\n".join(lines)

    _TOKEN_PRICES: Dict[str, Dict[str, float]] = {
        "claude": {"input": 3.0, "output": 15.0},
        "gemini": {"input": 0.5, "output": 1.5},
        "cursor": {"input": 3.0, "output": 15.0},
    }

    @staticmethod
    def _estimate_cost(tokens: Dict[str, int], runner: str) -> float:
        base_runner = runner.split()[0].lower() if runner else "claude"
        prices = Mycel._TOKEN_PRICES.get(base_runner, Mycel._TOKEN_PRICES["claude"])
        inp = tokens.get("input_tokens", 0)
        out = tokens.get("output_tokens", 0)
        return (inp * prices["input"] + out * prices["output"]) / 1_000_000

    def get_metrics(self) -> str:
        lines = ["\U0001f4ca **Mycel — Metrics**\n"]
        grand_total_cost = 0.0
        grand_total_s = 0.0
        for forge_name, forge in self.forges.items():
            metrics = forge.state.get("skill_metrics", {})
            if not metrics:
                continue
            run_num = forge.state.get("run_number", 0)
            total_s = sum(m.get("duration_s", 0) for m in metrics.values())
            total_cost = 0.0
            spell_lines: List[str] = []
            for sp, m in metrics.items():
                dur = m.get("duration_s", 0)
                familiar = m.get("runner_used", "?")
                tokens = m.get("tokens", {})
                token_str = ""
                cost_str = ""
                if tokens:
                    inp = tokens.get("input_tokens", 0)
                    out = tokens.get("output_tokens", 0)
                    cost = self._estimate_cost(tokens, familiar)
                    total_cost += cost
                    token_str = f", {inp}+{out} tok"
                    cost_str = f", ${cost:.4f}"
                spell_lines.append(f"  `/{sp}` — {dur}s ({familiar}{token_str}{cost_str})")
            grand_total_cost += total_cost
            grand_total_s += total_s
            cost_display = f", ~${total_cost:.3f}" if total_cost > 0 else ""
            lines.append(f"**{forge_name}** (run #{run_num}) — {total_s}s{cost_display}")
            lines.extend(spell_lines)
            lines.append("")
        if grand_total_cost > 0:
            lines.append(f"**Total** — {grand_total_s:.0f}s, ~${grand_total_cost:.3f}")
        elif len(lines) == 1:
            lines.append("No metrics yet — run a forge first.")
        return "\n".join(lines)

    def get_forge_log(self, forge_name: str, limit: int = 20) -> str:
        if forge_name not in self.forges:
            return f"Unknown forge: {forge_name}"
        messages = self.bus.read_log(forge_name, limit=limit)
        if not messages:
            return f"No messages for forge **{forge_name}**."
        lines = [f"\U0001f4dc **Forge {forge_name}** — last {len(messages)} message(s)\n"]
        for msg in messages:
            ts = msg.get("timestamp", "?")[:19]
            content = msg.get("content", "")[:200]
            lines.append(f"`{ts}` {content}")
        return "\n".join(lines)
