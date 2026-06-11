from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from message_bus import Message, MessageBus, atomic_write_json
from runner import ClaudeRunner

logger = logging.getLogger("mycel.sentry")

SENTRY_CHECK_PROMPT = """You are a Sentry monitoring agent. Use the available Sentry MCP tools.

## Task

1. Call `find_organizations` to find the organization
2. Call `search_issues` with query="is:unresolved" and sort="date" for recent unresolved issues (last hour)
3. For each issue found (max 10), fetch essential details

## Response format

Reply with ONLY valid JSON:
{{
  "timestamp": "{timestamp}",
  "issues": [
    {{
      "id": "SENTRY-12345",
      "title": "TypeError: ...",
      "culprit": "module.function",
      "level": "error|warning|fatal",
      "count": 42,
      "first_seen": "2026-04-10T...",
      "last_seen": "2026-04-10T...",
      "project": "kanta-api-v2",
      "url": "https://sentry.io/..."
    }}
  ],
  "total_unresolved": 15,
  "summary": "1-2 sentence summary"
}}

If no recent issues, return an empty `issues` array.
"""


class SentryMonitor:
    """Periodic Sentry error checker with Discord alerts."""

    def __init__(
        self,
        bus: MessageBus,
        config: Dict[str, Any],
        bus_dir: str = "bus",
    ) -> None:
        self.bus = bus
        self.interval = config.get("interval", 3600)
        self.auto_fix = config.get("auto_fix", False)
        self.auto_fix_levels = config.get("auto_fix_levels", ["fatal"])
        self.bus_dir = bus_dir

        # Runner for Sentry MCP calls
        self._runner = ClaudeRunner(
            timeout=300,
            allowed_tools="mcp__claude_ai_Sentry__*",
            permission_mode="auto",
        )

        self._state_dir = os.path.join(bus_dir, "sentry-watch")
        self._seen_path = os.path.join(self._state_dir, "seen.json")
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

        self._seen_ids: Dict[str, str] = self._load_seen()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_seen(self) -> Dict[str, str]:
        """Load previously seen issue IDs with their last_seen timestamp."""
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

    async def _check_sentry(self) -> Optional[Dict[str, Any]]:
        """Run a Sentry check via Claude + MCP. Returns parsed JSON or None."""
        prompt = SENTRY_CHECK_PROMPT.replace("{timestamp}", datetime.now(timezone.utc).isoformat())

        logger.info("Sentry check running...")
        result = await self._runner.run(prompt)

        if not result.success:
            logger.error("Sentry check failed: %s", result.stderr[:200])
            return None

        # Try to parse JSON from output
        from forge import extract_json
        data = extract_json(result.output)
        if data is None:
            logger.warning("Sentry check: could not parse response")
            return None

        self._save_check_result(data)
        return data

    def _find_new_issues(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Filter issues that haven't been seen before."""
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
        """Post new issues to Discord."""
        if not new_issues:
            return

        lines = [f"🚨 **Sentry** — {len(new_issues)} new error(s) detected\n"]

        actionable: List[Dict[str, str]] = []
        for issue in new_issues:
            level_icon = {"fatal": "🔴", "error": "🟠", "warning": "🟡"}.get(
                issue.get("level", "error"), "🟠"
            )
            issue_id = issue.get("id", "?")
            title = issue.get("title", "?")
            lines.append(
                f"{level_icon} **{issue_id}** — {title}\n"
                f"  Project: {issue.get('project', '?')} | "
                f"Count: {issue.get('count', '?')} | "
                f"Level: {issue.get('level', '?')}\n"
                f"  `{issue.get('culprit', '?')}`"
            )
            actionable.append({"id": issue_id, "title": title, "task": f"{issue_id} {title}"})

        # Manual mode (REMEDIATION_AUTO_FIX=false): expose per-issue
        # buttons that enqueue the sentry forge. Auto mode skips this — the
        # `_auto_fix_critical` path will enqueue critical issues itself.
        if not self.auto_fix:
            lines.append("\n💡 Click a button below to launch `/fix`, or run `!sentry <id> <title>`.")
        else:
            lines.append("\n💡 Auto-fix on for critical levels — others stay informational.")

        data: Dict[str, Any] = {}
        if not self.auto_fix and actionable:
            data = {
                "monitor_alert": True,
                "forge": "sentry",
                "actionable_issues": actionable[:5],  # Discord button row cap
            }

        await self.bus.publish(Message(
            source="sentry-monitor",
            content="\n".join(lines),
            level="warning",
            forge_name="sentry",
            data=data,
        ))

    async def _auto_fix_critical(
        self,
        new_issues: List[Dict[str, Any]],
        enqueue_callback: Any,
    ) -> None:
        """Auto-trigger the sentry forge for critical issues."""
        if not self.auto_fix or not enqueue_callback:
            return

        for issue in new_issues:
            if issue.get("level") not in self.auto_fix_levels:
                continue

            issue_id = issue.get("id", "UNKNOWN")
            title = issue.get("title", "Sentry error")
            task = f"{issue_id} {title}"

            await self.bus.publish(Message(
                source="sentry-monitor",
                content=f"🤖 **Sentry Auto-Fix** → Auto-run for **{issue_id}** ({issue.get('level')})",
                level="info",
                forge_name="sentry",
            ))

            try:
                await enqueue_callback("sentry", task)
            except Exception as exc:
                logger.error("Auto-fix %s failed: %s", issue_id, exc)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run_check(self, enqueue_callback: Any = None) -> int:
        """Run a single check. Returns number of new issues found."""
        data = await self._check_sentry()
        if data is None:
            return 0

        new_issues = self._find_new_issues(data)
        total = data.get("total_unresolved", 0)

        if new_issues:
            logger.info("Sentry: %d new issue(s) of %d unresolved", len(new_issues), total)
            await self._alert_new_issues(new_issues)
            await self._auto_fix_critical(new_issues, enqueue_callback)
        else:
            logger.info("Sentry: no new issues (%d unresolved)", total)

        return len(new_issues)

    async def start(self, enqueue_callback: Any = None) -> None:
        """Start the periodic monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(enqueue_callback))
        logger.info("Sentry monitor started (interval=%ds, auto_fix=%s)", self.interval, self.auto_fix)

        await self.bus.publish(Message(
            source="sentry-monitor",
            content=f"👁 **Sentry Monitor** on — check every {self.interval // 60} minutes",
            level="info",
            forge_name="sentry",
        ))

    async def _loop(self, enqueue_callback: Any) -> None:
        """Background loop."""
        # Run first check immediately
        await self.run_check(enqueue_callback)

        while self._running:
            await asyncio.sleep(self.interval)
            if not self._running:
                break
            try:
                await self.run_check(enqueue_callback)
            except Exception as exc:
                logger.error("Sentry monitor error: %s", exc, exc_info=True)

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Sentry monitor stopped")

        await self.bus.publish(Message(
            source="sentry-monitor",
            content="👁 **Sentry Monitor** stopped",
            level="info",
            forge_name="sentry",
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

        return (
            f"👁 **Sentry Monitor** — {running}\n"
            f"  Interval: {self.interval // 60} min\n"
            f"  Auto-fix: {'yes (' + ','.join(self.auto_fix_levels) + ')' if self.auto_fix else 'no'}\n"
            f"  Known issues: {seen_count}\n"
            f"  Last check: {last_check}"
        )
