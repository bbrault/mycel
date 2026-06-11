"""Provisioning backend that delegates to the `kanta-stack` CLI.

`bin/kanta-stack up <task>` (in the kanta-docker repo) spins up a fully isolated
per-task dev stack: git worktrees of the 4 repos under `dev-tasks/<task>/`, a
dedicated MySQL database (migrated + seeded), a docker-compose project on its own
port, and a symlinked AIDD workspace (.claude/.cursor/AGENTS.md/...). It is the
maintained, Kanta-specific replacement for the old ad-hoc `git clone` provisioning.

This module is a thin async wrapper: it shells out to the CLI (streaming output to
the message bus), then reads the generated instance env file to discover the
worktree paths and port so a Forge can run its steps inside them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Dict, Optional

from message_bus import Message, MessageBus
from runner import kill_proc_group

logger = logging.getLogger("mycel.core")

# JIRA ids like KAN-341 are the natural stack name; otherwise slugify free text.
_JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# kanta-stack instance env var -> worktree folder name.
_PATH_VARS = (
    ("KANTA_LAB_API_PATH", "kanta-api"),
    ("KANTA_LAB_API_V2_PATH", "kanta-api-v2"),
    ("KANTA_LAB_FRONT_PATH", "kanta-front"),
    ("KANTA_LAB_FRONT_V2_PATH", "kanta-front-v2"),
)


def slugify_task(task: str, fallback: str) -> str:
    """Derive a valid kanta-stack task name from a mycel task string.

    Prefers an embedded JIRA id (KAN-341 -> kan-341); otherwise slugifies the
    text. Falls back to *fallback* (e.g. ``"<forge>-<run>"``) when the result
    would be empty or the reserved name ``default``.
    """
    task = task or ""
    match = _JIRA_RE.search(task)
    if match:
        slug = match.group(1).lower()
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40]

    if not slug or not _SLUG_OK.match(slug) or slug == "default":
        slug = re.sub(r"[^a-z0-9]+", "-", (fallback or "").lower()).strip("-") or "task"
    return slug


def parse_env_file(path: str) -> Dict[str, str]:
    """Parse a simple KEY=VALUE env file (kanta-stack instance env)."""
    result: Dict[str, str] = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"')
    return result


class KantaStack:
    """Async wrapper over the `kanta-stack` CLI for a single forge."""

    def __init__(
        self,
        bin_path: str,
        bus: Optional[MessageBus] = None,
        forge_name: str = "",
    ) -> None:
        self.bin_path = os.path.expanduser(bin_path or "")
        self.bus = bus
        self.forge_name = forge_name
        # <kanta-docker>/bin/kanta-stack -> <kanta-docker>
        bin_dir = os.path.dirname(self.bin_path)
        self.kanta_docker_dir = os.path.dirname(bin_dir)
        self.instances_dir = os.path.join(self.kanta_docker_dir, "dev", "lab", "instances")
        self.lab_env_path = os.path.join(self.kanta_docker_dir, "dev", "lab", ".env")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def prerequisite_error(self) -> Optional[str]:
        """Return a human-readable reason kanta-stack can't run, or None if OK."""
        if not self.bin_path or not os.path.isfile(self.bin_path):
            return f"kanta-stack binary not found at {self.bin_path!r}"
        if not os.access(self.bin_path, os.X_OK):
            return f"kanta-stack binary not executable: {self.bin_path}"
        if not os.path.isfile(self.lab_env_path):
            return f"missing {self.lab_env_path} (lab tokens + shared lib paths)"
        return None

    def instance_env_path(self, slug: str) -> str:
        return os.path.join(self.instances_dir, f"{slug}.env")

    def instance_exists(self, slug: str) -> bool:
        return os.path.isfile(self.instance_env_path(slug))

    def workspace_paths(self, slug: str) -> Dict[str, str]:
        """Return {worktree-folder-name: absolute-path} for the task's repos."""
        env = parse_env_file(self.instance_env_path(slug))
        paths: Dict[str, str] = {}
        for var, folder in _PATH_VARS:
            path = env.get(var)
            if path:
                paths[folder] = path
        return paths

    def port(self, slug: str) -> Optional[int]:
        env = parse_env_file(self.instance_env_path(slug))
        raw = env.get("KANTA_LAB_PORT")
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _publish(self, content: str, level: str = "info") -> None:
        if self.bus is not None:
            await self.bus.publish(Message(
                source="forge", content=content, level=level, forge_name=self.forge_name,
            ))

    async def _run(self, args: list, timeout: int) -> tuple:
        """Run `kanta-stack <args>`; return (returncode, combined_output).

        Streams nothing line-by-line (kanta-stack is chatty but bounded); the
        full output is captured and the tail surfaced on failure. Kills the
        process group on timeout so a hung docker/composer can't wedge the loop.
        """
        proc = await asyncio.create_subprocess_exec(
            self.bin_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group → kill children too
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await kill_proc_group(proc)
            return -1, f"(kanta-stack {args[0]} timed out after {timeout}s)"
        except asyncio.CancelledError:
            await kill_proc_group(proc)
            raise
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace")

    async def up(
        self,
        slug: str,
        branch: Optional[str] = None,
        clone_from: Optional[str] = None,
        timeout: int = 1800,
    ) -> Dict[str, str]:
        """Provision (or reuse) the stack for *slug*. Returns the workspace paths.

        Raises RuntimeError on a missing prerequisite or a failed `up`.
        """
        err = self.prerequisite_error()
        if err:
            raise RuntimeError(f"kanta-stack unavailable: {err}")

        args = ["up", slug]
        if branch:
            args += ["--branch", branch]
        if clone_from:
            args += ["--clone-from", clone_from]

        await self._publish(
            f"📦 **Forge {self.forge_name}** — `kanta-stack up {slug}` "
            f"(worktrees + DB + containers, this can take a few minutes)…"
        )
        code, output = await self._run(args, timeout=timeout)
        if code != 0:
            tail = output.strip()[-1500:]
            raise RuntimeError(f"kanta-stack up {slug} failed (code {code}):\n{tail}")

        paths = self.workspace_paths(slug)
        if not paths:
            raise RuntimeError(
                f"kanta-stack up {slug} reported success but no instance env at "
                f"{self.instance_env_path(slug)}"
            )
        port = self.port(slug)
        await self._publish(
            f"✅ **Forge {self.forge_name}** — stack `{slug}` ready"
            + (f" on http://localhost:{port}" if port else "")
            + f"\n  📦 {', '.join(sorted(paths))}"
        )
        return paths

    async def down(self, slug: str, timeout: int = 300) -> bool:
        """Stop the stack's containers, keeping worktrees, DB and config."""
        if not self.instance_exists(slug):
            return False
        code, output = await self._run(["down", slug], timeout=timeout)
        if code == 0:
            await self._publish(f"🛑 **Forge {self.forge_name}** — stack `{slug}` stopped (data kept)")
            return True
        logger.warning("Forge %s: kanta-stack down %s failed: %s", self.forge_name, slug, output[-300:])
        return False

    async def destroy(self, slug: str, timeout: int = 600) -> bool:
        """Tear the stack down completely (containers, DB, worktrees, branch)."""
        if not self.instance_exists(slug):
            return False
        code, output = await self._run(["destroy", slug, "--force"], timeout=timeout)
        if code == 0:
            await self._publish(f"🧹 **Forge {self.forge_name}** — stack `{slug}` destroyed")
            return True
        logger.warning("Forge %s: kanta-stack destroy %s failed: %s", self.forge_name, slug, output[-300:])
        return False
