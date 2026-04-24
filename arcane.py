from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import yaml

from forge import Circle
from message_bus import Message, MessageBus
from runner import check_runners
from sentry_monitor import SentryMonitor

logger = logging.getLogger("arcane.core")


class _QueueItem:
    """A pending task in the queue."""

    def __init__(
        self,
        circle_name: str,
        task: str,
        instructions: Optional[str] = None,
        spell_name: Optional[str] = None,
        from_spell: Optional[str] = None,
    ) -> None:
        self.circle_name = circle_name
        self.task = task
        self.instructions = instructions
        self.spell_name = spell_name
        self.from_spell = from_spell


class Arcane:
    """The grimoire -- loads config, instantiates circles, routes commands."""

    def __init__(
        self,
        config_path: str = "arcane_config.yaml",
        spells_path: str = "skills.yaml",
        bus_dir: str = "bus",
    ) -> None:
        self.config_path = config_path
        self.spells_path = spells_path
        self.bus_dir = bus_dir

        self.config: Dict[str, Any] = {}
        self.spells_config: Dict[str, Dict[str, Any]] = {}
        self.workspace: Dict[str, str] = {}
        self.docs_path: str = ""
        self.issues_dir: str = ""
        self.claude_config: Dict[str, Any] = {}
        self.circles: Dict[str, Circle] = {}
        self.bus = MessageBus(bus_dir=bus_dir)
        self.familiar_status: Dict[str, bool] = {}
        self.sentry_monitor: Optional[SentryMonitor] = None

        self._circle_queues: Dict[str, asyncio.Queue[_QueueItem]] = {}
        self._active_items: Dict[str, Optional[_QueueItem]] = {}
        self._worker_started = False

        self._load_config()
        self._load_spells()
        self._build_circles()
        self._check_familiars()
        self._init_sentry_monitor()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as fh:
            self.config = yaml.safe_load(fh)

        self.docs_path = os.path.expanduser(os.environ.get("DOCS_PATH", self.config.get("docs_path", "")))
        self.issues_dir = os.path.expanduser(os.environ.get("ISSUES_DIR", self.config.get("issues_dir", "")))

        self._repo_folders: Dict[str, str] = self.config.get("repos", {})
        self._workspace_groups: Dict[str, Dict[str, Any]] = self.config.get("workspace_groups", {})

        self.workspace = {}
        for group_cfg in self._workspace_groups.values():
            base_env = group_cfg.get("base_env", "")
            base_path = os.path.expanduser(os.environ.get(base_env, ""))
            if not base_path:
                continue
            for repo_key in group_cfg.get("repos", []):
                folder = self._repo_folders.get(repo_key, repo_key)
                if repo_key not in self.workspace:
                    self.workspace[repo_key] = os.path.join(base_path, folder)

        self.claude_config = self.config.get("claude", {})
        logger.info("Config chargee depuis %s (%d circles, %d repos)", self.config_path, len(self.config.get("circles", {})), len(self._repo_folders))

    def _load_spells(self) -> None:
        with open(self.spells_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        # Support both "spells" and legacy "skills" key
        self.spells_config = data.get("spells", data.get("skills", {}))

        loaded_external = 0
        for spell_name, spell in self.spells_config.items():
            prompt_file = spell.get("prompt_file")
            if prompt_file and os.path.isfile(prompt_file):
                try:
                    with open(prompt_file, "r", encoding="utf-8") as pf:
                        spell["prompt"] = pf.read()
                    loaded_external += 1
                except OSError as exc:
                    logger.warning("Impossible de lire %s pour spell %s: %s", prompt_file, spell_name, exc)

        logger.info("Spells charges depuis %s (%d spells, %d prompts externes)", self.spells_path, len(self.spells_config), loaded_external)

    def reload_config(self) -> str:
        """Hot-reload config and spells without losing circle state."""
        try:
            self._load_config()
            self._load_spells()

            for circle in self.circles.values():
                circle.skills = self.spells_config

            for circle_name in self.config.get("circles", {}):
                if circle_name not in self.circles:
                    self._build_single_circle(circle_name)

            self._check_familiars()
            return f"Config rechargee : {len(self.spells_config)} spells, {len(self.circles)} circles"
        except Exception as exc:
            logger.error("Erreur reload: %s", exc)
            return f"Erreur reload : {exc}"

    def _resolve_circle_workspace(self, circle_cfg: Dict[str, Any]) -> Dict[str, str]:
        """Resolve the workspace for a circle from its workspace_group."""
        group_name = circle_cfg.get("workspace_group")
        if not group_name or group_name not in self._workspace_groups:
            return {}

        group_cfg = self._workspace_groups[group_name]
        base_env = group_cfg.get("base_env", "")
        base_path = os.path.expanduser(os.environ.get(base_env, ""))
        if not base_path:
            logger.warning("Env var %s non definie pour le groupe %s", base_env, group_name)
            return {}

        repo_keys: List[str] = group_cfg.get("repos", [])
        result: Dict[str, str] = {}
        for repo_key in repo_keys:
            folder = self._repo_folders.get(repo_key, repo_key)
            result[repo_key] = os.path.join(base_path, folder)
        return result

    def _build_single_circle(self, circle_name: str) -> None:
        defaults = self.config.get("defaults", {})
        circle_cfg = self.config["circles"][circle_name]
        circle = Circle(
            name=circle_name,
            description=circle_cfg.get("description", ""),
            workflow=circle_cfg.get("ritual", []),
            skills=self.spells_config,
            workspace=self._resolve_circle_workspace(circle_cfg),
            bus=self.bus,
            default_runner=circle_cfg.get("familiar", defaults.get("familiar", "claude")),
            max_retries=defaults.get("max_retries", 3),
            timeout=defaults.get("timeout", 180),
            bus_dir=self.bus_dir,
            docs_path=self.docs_path,
            issues_dir=self.issues_dir,
            runner_kwargs=self.claude_config,
        )
        self.circles[circle_name] = circle

    def _build_circles(self) -> None:
        for circle_name in self.config.get("circles", {}):
            self._build_single_circle(circle_name)

    def _check_familiars(self) -> None:
        self.familiar_status = check_runners()
        if not self.familiar_status.get("claude"):
            logger.warning("Claude CLI absent -- les circles ne pourront pas s'executer.")

    def _init_sentry_monitor(self) -> None:
        sentry_config = self.config.get("sentry_monitor", {})
        if sentry_config.get("enabled", False):
            self.sentry_monitor = SentryMonitor(
                bus=self.bus, config=sentry_config, bus_dir=self.bus_dir,
            )

    async def start_sentry_monitor(self) -> None:
        if self.sentry_monitor is None:
            return
        async def _enqueue_callback(circle_name: str, task: str) -> None:
            await self.enqueue_circle(circle_name, task)
        await self.sentry_monitor.start(enqueue_callback=_enqueue_callback)

    async def stop_sentry_monitor(self) -> None:
        if self.sentry_monitor:
            await self.sentry_monitor.stop()

    async def run_sentry_check(self) -> int:
        if self.sentry_monitor is None:
            sentry_config = self.config.get("sentry_monitor", {})
            self.sentry_monitor = SentryMonitor(bus=self.bus, config=sentry_config, bus_dir=self.bus_dir)
        async def _enqueue_callback(circle_name: str, task: str) -> None:
            await self.enqueue_circle(circle_name, task)
        return await self.sentry_monitor.run_check(enqueue_callback=_enqueue_callback)

    # ------------------------------------------------------------------
    # Task queue
    # ------------------------------------------------------------------

    async def start_worker(self) -> None:
        if not self._worker_started:
            for circle_name in self.circles:
                queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
                self._circle_queues[circle_name] = queue
                self._active_items[circle_name] = None
                asyncio.create_task(self._circle_worker(circle_name, queue))
            self._worker_started = True
            logger.info("Workers demarres (%d circles)", len(self.circles))
            await self._recover_circles()

    async def _recover_circles(self) -> None:
        for circle_name, circle in self.circles.items():
            status = circle.state.get("status")
            if status not in ("running", "paused"):
                continue
            task = circle.state.get("task")
            current_spell = circle.state.get("current_skill")
            if not task or not current_spell:
                circle.state["status"] = "idle"
                circle.state["error"] = "Interrompu"
                circle._save_state()
                continue

            logger.info("Recovery: circle %s etait %s sur /%s", circle_name, status, current_spell)
            await self.bus.publish(Message(
                source="arcane",
                content=f"\U0001f504 **Arcane** \u2192 Reprise automatique du circle **{circle_name}** (spell /{current_spell})",
                level="info",
                forge_name=circle_name,
            ))
            circle.state["status"] = "idle"
            circle._save_state()
            await self.enqueue_from_spell(circle_name, current_spell)

    async def _circle_worker(self, circle_name: str, queue: asyncio.Queue[_QueueItem]) -> None:
        while True:
            item = await queue.get()
            self._active_items[circle_name] = item

            try:
                circle = self.circles[circle_name]
                await self.bus.publish(Message(
                    source="arcane",
                    content=f"\U0001f52e **Arcane** \u2192 Activation du circle **{circle_name}**",
                    level="info",
                    forge_name=circle_name,
                ))

                if item.from_spell:
                    logger.info("Worker %s: reprise depuis /%s", circle_name, item.from_spell)
                    await circle.run_from_skill(item.from_spell, item.instructions)
                elif item.spell_name:
                    logger.info("Worker %s: execution spell /%s", circle_name, item.spell_name)
                    await circle.run_single_skill(item.spell_name, item.task, item.instructions)
                else:
                    logger.info("Worker %s: execution ritual", circle_name)
                    await circle.run_workflow(item.task, item.instructions)

            except Exception as exc:
                logger.error("Worker %s: erreur: %s", circle_name, exc, exc_info=True)
                await self.bus.publish(Message(
                    source="arcane",
                    content=f"\U0001f4a5 **Arcane** \u2192 Erreur sur le circle **{circle_name}** : {exc}",
                    level="error",
                    forge_name=circle_name,
                ))
            finally:
                self._active_items[circle_name] = None
                queue.task_done()

                circle = self.circles[circle_name]
                if circle.state.get("status") == "completed":
                    await self._trigger_chain(circle_name, item)

                remaining = queue.qsize()
                if remaining > 0:
                    logger.info("Worker %s: %d tache(s) restante(s)", circle_name, remaining)

    @property
    def task_running(self) -> bool:
        return any(item is not None for item in self._active_items.values())

    @property
    def queue_size(self) -> int:
        return sum(q.qsize() for q in self._circle_queues.values())

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _trigger_chain(self, circle_name: str, completed_item: _QueueItem) -> None:
        circle_cfg = self.config.get("circles", {}).get(circle_name, {})
        next_circle = circle_cfg.get("on_complete")
        if not next_circle or next_circle not in self.circles:
            return
        logger.info("Chain: circle %s termine, declenchement de %s", circle_name, next_circle)
        await self.bus.publish(Message(
            source="arcane",
            content=f"\U0001f517 **Arcane** \u2192 Chain : **{circle_name}** termine, invocation de **{next_circle}**",
            level="info",
            forge_name=next_circle,
        ))
        await self.enqueue_circle(next_circle, completed_item.task)

    def _get_circle_queue(self, circle_name: str) -> asyncio.Queue[_QueueItem]:
        queue = self._circle_queues.get(circle_name)
        if queue is None:
            raise ValueError(f"Circle inconnu : {circle_name}")
        return queue

    async def enqueue_circle(self, circle_name: str, task: str, instructions: Optional[str] = None) -> int:
        if circle_name not in self.circles:
            raise ValueError(f"Circle inconnu : {circle_name}")
        await self.start_worker()
        queue = self._get_circle_queue(circle_name)
        item = _QueueItem(circle_name, task, instructions)
        await queue.put(item)
        position = queue.qsize()
        if position > 1 or self._active_items.get(circle_name) is not None:
            await self.bus.publish(Message(
                source="arcane",
                content=f"\U0001f4cb **Arcane** \u2192 Tache en attente pour **{circle_name}** (position {position})",
                level="info",
                forge_name=circle_name,
            ))
        logger.info("Tache enfilee pour circle %s (position %d)", circle_name, position)
        return position

    async def enqueue_circle_spell(self, circle_name: str, spell_name: str, task: str, instructions: Optional[str] = None) -> int:
        if circle_name not in self.circles:
            raise ValueError(f"Circle inconnu : {circle_name}")
        if spell_name not in self.spells_config:
            raise ValueError(f"Spell inconnu : {spell_name}")
        await self.start_worker()
        queue = self._get_circle_queue(circle_name)
        item = _QueueItem(circle_name, task, instructions, spell_name=spell_name)
        await queue.put(item)
        position = queue.qsize()
        if position > 1 or self._active_items.get(circle_name) is not None:
            await self.bus.publish(Message(
                source="arcane",
                content=f"\U0001f4cb **Arcane** \u2192 Spell /{spell_name} en attente pour **{circle_name}** (position {position})",
                level="info",
                forge_name=circle_name,
            ))
        return position

    async def enqueue_from_spell(self, circle_name: str, from_spell: str, instructions: Optional[str] = None) -> int:
        if circle_name not in self.circles:
            raise ValueError(f"Circle inconnu : {circle_name}")
        await self.start_worker()
        queue = self._get_circle_queue(circle_name)
        task = self.circles[circle_name].state.get("task") or ""
        item = _QueueItem(circle_name, task, instructions, from_spell=from_spell)
        await queue.put(item)
        position = queue.qsize()
        logger.info("Reprise circle %s depuis /%s (position %d)", circle_name, from_spell, position)
        return position

    def resume_circle(self, circle_name: str, instructions: Optional[str] = None) -> None:
        if circle_name not in self.circles:
            raise ValueError(f"Circle inconnu : {circle_name}")
        self.circles[circle_name].resume(instructions)

    def retry_circle(self, circle_name: str, instructions: Optional[str] = None) -> None:
        if circle_name not in self.circles:
            raise ValueError(f"Circle inconnu : {circle_name}")
        self.circles[circle_name].retry(instructions)

    def abort_circle(self, circle_name: str) -> bool:
        if circle_name not in self.circles:
            raise ValueError(f"Circle inconnu : {circle_name}")
        return self.circles[circle_name].abort()

    def reset_circle(self, circle_name: str) -> None:
        if circle_name not in self.circles:
            raise ValueError(f"Circle inconnu : {circle_name}")
        self.circles[circle_name].reset()

    def reset_all(self) -> None:
        for circle in self.circles.values():
            circle.reset()

    def inject_feedback(self, circle_name: str, feedback: str) -> None:
        if circle_name not in self.circles:
            return
        self.circles[circle_name].inject_feedback(feedback)

    # ------------------------------------------------------------------
    # Status & logs
    # ------------------------------------------------------------------

    def get_circle_status(self, circle_name: str) -> str:
        if circle_name not in self.circles:
            return f"Circle inconnu : {circle_name}"
        return self.circles[circle_name].status_summary

    def get_global_status(self) -> str:
        lines = ["\U0001f52e **Arcane \u2014 Grimoire**\n"]
        for circle in self.circles.values():
            lines.append(circle.progress_bar)
        lines.append(f"\nRitual en cours : {'oui' if self.task_running else 'non'}")
        lines.append(f"File d'attente : {self.queue_size} tache(s)")
        check = "\u2705"
        cross = "\u274c"
        familiars = ", ".join(f"{k}: {check if v else cross}" for k, v in self.familiar_status.items())
        lines.append(f"Familiars : {familiars}")
        if self.sentry_monitor:
            lines.append("")
            lines.append(self.sentry_monitor.status)
        return "\n".join(lines)

    def list_circles(self) -> str:
        lines = ["\U0001f52e **Circles disponibles**\n"]
        for name, circle in self.circles.items():
            ritual_str = " \u2192 ".join(circle._all_skill_names())
            lines.append(f"**{name}** \u2014 {circle.description}")
            lines.append(f"  Ritual : {ritual_str}")
            lines.append(f"  Familiar : {circle.default_runner}")
            lines.append(f"  Commande : `!{name} <description>`")
            lines.append("")
        return "\n".join(lines)

    def list_spells(self) -> str:
        lines = ["\U0001f52e **Spells disponibles**\n"]
        for name, spell in self.spells_config.items():
            lines.append(f"**/{name}** \u2014 {spell.get('description', '')}")
            lines.append(f"  Familiar : {spell.get('runner', 'claude')} | Timeout : {spell.get('timeout', 180)}s")
            auto = "oui" if spell.get("auto_advance", True) else "non (pause)"
            lines.append(f"  Auto-advance : {auto}")
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
        prices = Arcane._TOKEN_PRICES.get(base_runner, Arcane._TOKEN_PRICES["claude"])
        inp = tokens.get("input_tokens", 0)
        out = tokens.get("output_tokens", 0)
        return (inp * prices["input"] + out * prices["output"]) / 1_000_000

    def get_metrics(self) -> str:
        lines = ["\U0001f4ca **Arcane \u2014 Metriques**\n"]
        grand_total_cost = 0.0
        grand_total_s = 0.0
        for circle_name, circle in self.circles.items():
            metrics = circle.state.get("skill_metrics", {})
            if not metrics:
                continue
            run_num = circle.state.get("run_number", 0)
            total_s = sum(m.get("duration_s", 0) for m in metrics.values())
            total_cost = 0.0
            spell_lines: List[str] = []
            for spell, m in metrics.items():
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
                spell_lines.append(f"  `/{spell}` \u2014 {dur}s ({familiar}{token_str}{cost_str})")
            grand_total_cost += total_cost
            grand_total_s += total_s
            cost_display = f", ~${total_cost:.3f}" if total_cost > 0 else ""
            lines.append(f"**{circle_name}** (run #{run_num}) \u2014 {total_s}s{cost_display}")
            lines.extend(spell_lines)
            lines.append("")
        if grand_total_cost > 0:
            lines.append(f"**Total** \u2014 {grand_total_s:.0f}s, ~${grand_total_cost:.3f}")
        elif len(lines) == 1:
            lines.append("Aucune metrique disponible \u2014 lancez un ritual d'abord.")
        return "\n".join(lines)

    def get_circle_log(self, circle_name: str, limit: int = 20) -> str:
        if circle_name not in self.circles:
            return f"Circle inconnu : {circle_name}"
        messages = self.bus.read_log(circle_name, limit=limit)
        if not messages:
            return f"Aucun message pour le circle **{circle_name}**."
        lines = [f"\U0001f4dc **Circle {circle_name}** \u2014 {len(messages)} derniers messages\n"]
        for msg in messages:
            ts = msg.get("timestamp", "?")[:19]
            content = msg.get("content", "")[:200]
            lines.append(f"`{ts}` {content}")
        return "\n".join(lines)
