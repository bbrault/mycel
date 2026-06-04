"""Conversational concierge — talk to Mycel in natural language.

Routes a free-form Discord message (e.g. "@Mycel where did my dev run go?") to a
``claude -p`` subprocess that can call the read-only Mycel MCP tools
(``mcp__mycel__*``). Conversation history is persisted per Discord
channel/thread to ``bus/agent/<conversation_id>.jsonl`` so each turn replays the
prior turns (``claude -p`` is stateless across invocations) and leaves an audit
trail.

Phase 1 only wires read-only tools, so ``permission_mode: auto`` is safe.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List

from runner import get_runner

if TYPE_CHECKING:
    from mycel import Mycel

logger = logging.getLogger("mycel.agent")

SYSTEM_FRAMING = (
    "You are Mycel's concierge, a helpful assistant embedded in a Discord bot that "
    "drives a multi-agent AI development orchestrator. Users talk to you in natural "
    "language to understand what their forges and workflows are doing.\n\n"
    "Answer using the read-only `mycel` tools (status, logs, forges, steps, metrics, "
    "mcp health). Prefer calling a tool over guessing. Be concise and direct — you are "
    "replying in a Discord message, so keep it short, use plain prose, and avoid "
    "headers. You can only read state; you cannot start, retry, abort or change "
    "anything. If a user asks you to take an action, explain the matching `!` command "
    "they can run instead."
)


class Concierge:
    """Stateless-per-turn conversational agent over the read-only Mycel tools."""

    def __init__(self, orchestrator: "Mycel", config: Dict[str, Any]) -> None:
        self.orchestrator = orchestrator
        self.config = config or {}
        self.repo_dir = os.path.dirname(os.path.abspath(__file__))
        self.agent_dir = os.path.join(orchestrator.bus_dir, "agent")

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _transcript_path(self, conversation_id: str) -> str:
        # conversation_id is a Discord snowflake (digits) — safe as a filename.
        return os.path.join(self.agent_dir, f"{conversation_id}.jsonl")

    def _load_transcript(self, conversation_id: str) -> List[Dict[str, str]]:
        path = self._transcript_path(conversation_id)
        if not os.path.exists(path):
            return []
        turns: List[Dict[str, str]] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return turns

    def _append_turn(self, conversation_id: str, role: str, content: str) -> None:
        os.makedirs(self.agent_dir, exist_ok=True)
        path = self._transcript_path(conversation_id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"role": role, "content": content}, ensure_ascii=False) + "\n")

    def _build_prompt(self, turns: List[Dict[str, str]], user_text: str) -> str:
        parts = [SYSTEM_FRAMING, ""]
        if turns:
            parts.append("Conversation so far:")
            for turn in turns:
                speaker = "User" if turn.get("role") == "user" else "You"
                parts.append(f"{speaker}: {turn.get('content', '')}")
            parts.append("")
        parts.append(f"User: {user_text}")
        parts.append("You:")
        return "\n".join(parts)

    async def handle_message(self, conversation_id: str, user_text: str) -> str:
        """Process one user turn and return the assistant's reply text."""
        if not self.enabled:
            return "The Mycel concierge is currently disabled. Use `!mycel status` instead."

        turns = self._load_transcript(conversation_id)
        prompt = self._build_prompt(turns, user_text)

        runner = get_runner(
            "claude",
            timeout=int(self.config.get("timeout", 120)),
            allowed_tools=self.config.get("allowed_tools", "mcp__mycel__*"),
            mcp_config=self.config.get("mcp_config", "mycel_mcp.json"),
            permission_mode=self.config.get("permission_mode", "auto"),
        )

        # The MCP child process reads the live ControlServer socket path from env.
        os.environ["MYCEL_CONTROL_SOCKET"] = self.orchestrator.control_socket_path

        result = await runner.run(prompt, cwd=self.repo_dir)
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning("Concierge run failed (code=%s): %s", result.returncode, result.stderr[:300])
            return "Sorry, I couldn't reach Mycel right now. Try `!mycel status`."

        reply = result.stdout.strip()
        self._append_turn(conversation_id, "user", user_text)
        self._append_turn(conversation_id, "assistant", reply)
        return reply
