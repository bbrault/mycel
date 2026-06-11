from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from message_bus import Message, MessageBus, atomic_write_json
from runner import ClaudeRunner

logger = logging.getLogger("mycel.aikido")

AIKIDO_CHECK_PROMPT = """You are an Aikido security monitoring agent. Use the available Aikido MCP tools.

## Task

1. List open issues via the Aikido MCP (use the tool that lists issues filtered by open/unresolved status)
2. For each issue found (max 10, prioritize highest severity first), fetch essential details

## Response format

Reply with ONLY valid JSON (no markdown fences):
{{
  "timestamp": "{timestamp}",
  "issues": [
    {{
      "id": "AIK-12345",
      "title": "CVE-2024-XXXXX in lodash",
      "type": "sca|sast|secret|iac|container|license|...",
      "severity": "critical|high|medium|low|info",
      "affected": "package-name / file path / repo",
      "first_detected_at": "2026-04-10T...",
      "url": "https://app.aikido.dev/..."
    }}
  ],
  "total_open": 42,
  "summary": "1-2 sentence summary"
}}

If no open issues, return an empty `issues` array.
"""

SEVERITY_ICONS: Dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}


class AikidoMonitor:
    """Periodic Aikido security-issue checker with Discord alerts.

    Mirrors SentryMonitor: a ClaudeRunner with Aikido MCP tools pulls open
    issues on an interval, dedups via a local seen.json, posts new findings
    to Discord, and can auto-enqueue the `aikido` forge for criticals.
    """

    def __init__(
        self,
        bus: MessageBus,
        config: Dict[str, Any],
        bus_dir: str = "bus",
    ) -> None:
        self.bus = bus
        self.interval = config.get("interval", 3600)
        self.auto_fix = config.get("auto_fix", False)
        self.auto_fix_severities = [
            s.lower() for s in config.get("auto_fix_severities", ["critical"])
        ]
        self.bus_dir = bus_dir

        allowed_tools = config.get("allowed_tools", "mcp__claude_ai_Aikido__*")
        self._runner = ClaudeRunner(
            timeout=config.get("runner_timeout", 300),
            allowed_tools=allowed_tools,
            permission_mode="auto",
        )

        self._state_dir = os.path.join(bus_dir, "aikido-watch")
        self._seen_path = os.path.join(self._state_dir, "seen.json")
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

        self._seen_ids: Dict[str, str] = self._load_seen()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_seen(self) -> Dict[str, str]:
        if os.path.exists(self._seen_path):
            try:
                with open(self._seen_path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_seen(self) -> None:
        os.makedirs(self._state_dir, exist_ok=True)
        atomic_write_json(self._seen_path, self._seen_ids)

    def _save_check_result(self, data: Dict[str, Any]) -> None:
        os.makedirs(self._state_dir, exist_ok=True)
        path = os.path.join(self._state_dir, "last_check.json")
        atomic_write_json(path, data)

    # ------------------------------------------------------------------
    # Check logic
    # ------------------------------------------------------------------

    async def _check_aikido(self) -> Optional[Dict[str, Any]]:
        """Run an Aikido check via Claude + MCP. Returns parsed JSON or None."""
        prompt = AIKIDO_CHECK_PROMPT.replace(
            "{timestamp}", datetime.now(timezone.utc).isoformat()
        )

        logger.info("Aikido check running...")
        result = await self._runner.run(prompt)

        if not result.success:
            logger.error("Aikido check failed: %s", result.stderr[:200])
            return None

        from forge import extract_json
        data = extract_json(result.output)
        if data is None:
            logger.warning("Aikido check: could not parse response")
            return None

        self._save_check_result(data)
        return data

    def _find_new_issues(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues = data.get("issues", [])
        new_issues: List[Dict[str, Any]] = []

        for issue in issues:
            issue_id = issue.get("id", "")
            if issue_id and issue_id not in self._seen_ids:
                new_issues.append(issue)
                self._seen_ids[issue_id] = datetime.now(timezone.utc).isoformat()

        if new_issues:
            self._save_seen()

        return new_issues

    async def _alert_new_issues(self, new_issues: List[Dict[str, Any]]) -> None:
        if not new_issues:
            return

        lines = [f"🛡 **Aikido** — {len(new_issues)} new issue(s) detected\n"]

        actionable: List[Dict[str, str]] = []
        for issue in new_issues:
            icon = SEVERITY_ICONS.get(
                str(issue.get("severity", "medium")).lower(), "🟡"
            )
            issue_id = issue.get("id", "?")
            title = issue.get("title", "?")
            lines.append(
                f"{icon} **{issue_id}** — {title}\n"
                f"  Type: {issue.get('type', '?')} | "
                f"Severity: {issue.get('severity', '?')} | "
                f"Scope: {issue.get('affected', '?')}\n"
                f"  {issue.get('url', '')}"
            )
            actionable.append({"id": issue_id, "title": title, "task": f"{issue_id} {title}"})

        if not self.auto_fix:
            lines.append("\n💡 Click a button below to launch `/fix`, or run `!aikido <id> <title>`.")
        else:
            lines.append("\n💡 Auto-fix on for critical severities — others stay informational.")

        data: Dict[str, Any] = {}
        if not self.auto_fix and actionable:
            data = {
                "monitor_alert": True,
                "forge": "aikido",
                "actionable_issues": actionable[:5],
            }

        await self.bus.publish(Message(
            source="aikido-monitor",
            content="\n".join(lines),
            level="warning",
            forge_name="aikido",
            data=data,
        ))

    async def _auto_fix_critical(
        self,
        new_issues: List[Dict[str, Any]],
        enqueue_callback: Any,
    ) -> None:
        if not self.auto_fix or not enqueue_callback:
            return

        for issue in new_issues:
            if str(issue.get("severity", "")).lower() not in self.auto_fix_severities:
                continue

            issue_id = issue.get("id", "UNKNOWN")
            title = issue.get("title", "Aikido issue")
            task = f"{issue_id} {title}"

            await self.bus.publish(Message(
                source="aikido-monitor",
                content=f"🤖 **Aikido Auto-Fix** → Auto-run for **{issue_id}** ({issue.get('severity')})",
                level="info",
                forge_name="aikido",
            ))

            try:
                await enqueue_callback("aikido", task)
            except Exception as exc:
                logger.error("Auto-fix %s failed: %s", issue_id, exc)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run_check(self, enqueue_callback: Any = None) -> int:
        data = await self._check_aikido()
        if data is None:
            return 0

        new_issues = self._find_new_issues(data)
        total = data.get("total_open", 0)

        if new_issues:
            logger.info("Aikido: %d new issue(s) of %d open", len(new_issues), total)
            await self._alert_new_issues(new_issues)
            await self._auto_fix_critical(new_issues, enqueue_callback)
        else:
            logger.info("Aikido: no new issues (%d open)", total)

        return len(new_issues)

    async def start(self, enqueue_callback: Any = None) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(enqueue_callback))
        logger.info(
            "Aikido monitor started (interval=%ds, auto_fix=%s)",
            self.interval, self.auto_fix,
        )

        await self.bus.publish(Message(
            source="aikido-monitor",
            content=f"🛡 **Aikido Monitor** on — check every {self.interval // 60} minutes",
            level="info",
            forge_name="aikido",
        ))

    async def _loop(self, enqueue_callback: Any) -> None:
        await self.run_check(enqueue_callback)

        while self._running:
            await asyncio.sleep(self.interval)
            if not self._running:
                break
            try:
                await self.run_check(enqueue_callback)
            except Exception as exc:
                logger.error("Aikido monitor error: %s", exc, exc_info=True)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Aikido monitor stopped")

        await self.bus.publish(Message(
            source="aikido-monitor",
            content="🛡 **Aikido Monitor** stopped",
            level="info",
            forge_name="aikido",
        ))

    @property
    def status(self) -> str:
        running = "on" if self._running else "off"
        seen_count = len(self._seen_ids)
        last_check = "never"
        last_check_path = os.path.join(self._state_dir, "last_check.json")
        if os.path.exists(last_check_path):
            try:
                data = json.load(open(last_check_path))
                last_check = data.get("timestamp", "?")[:19]
            except (json.JSONDecodeError, OSError):
                pass

        auto = (
            f"yes ({','.join(self.auto_fix_severities)})" if self.auto_fix else "no"
        )
        return (
            f"🛡 **Aikido Monitor** — {running}\n"
            f"  Interval: {self.interval // 60} min\n"
            f"  Auto-fix: {auto}\n"
            f"  Known issues: {seen_count}\n"
            f"  Last check: {last_check}"
        )
