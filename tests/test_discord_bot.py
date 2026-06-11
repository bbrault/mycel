from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord_bot import MycelBot, check_permission, chunk_message


def _user(*role_names: str) -> SimpleNamespace:
    return SimpleNamespace(roles=[SimpleNamespace(name=r) for r in role_names])


class TestCheckPermission:
    CONFIG = {
        "permissions": {
            "lead": "all",
            "dev-team": ["dev", "bugfix"],
            "default": ["review"],
        }
    }

    def test_no_permissions_configured_allows_all(self) -> None:
        assert check_permission({}, _user(), "dev") is True

    def test_role_with_all_access(self) -> None:
        assert check_permission(self.CONFIG, _user("lead"), "sentry") is True

    def test_role_with_forge_list(self) -> None:
        assert check_permission(self.CONFIG, _user("dev-team"), "dev") is True
        assert check_permission(self.CONFIG, _user("dev-team"), "sentry") is False

    def test_unmatched_role_falls_back_to_default(self) -> None:
        assert check_permission(self.CONFIG, _user("guest"), "review") is True
        assert check_permission(self.CONFIG, _user("guest"), "dev") is False

    def test_user_without_roles_uses_default(self) -> None:
        # Interactions can carry a plain User (no .roles) — only `default` applies.
        plain_user = SimpleNamespace()
        assert check_permission(self.CONFIG, plain_user, "review") is True
        assert check_permission(self.CONFIG, plain_user, "dev") is False


class TestChunkMessage:
    def test_short_message_single_chunk(self) -> None:
        text = "hello world"
        assert chunk_message(text) == ["hello world"]

    def test_exact_limit(self) -> None:
        text = "a" * 1900
        assert chunk_message(text) == [text]

    def test_splits_on_newline(self) -> None:
        line_a = "x" * 1000
        line_b = "y" * 1000
        text = f"{line_a}\n{line_b}"  # 2001 chars → split at newline
        chunks = chunk_message(text)
        assert len(chunks) == 2
        assert chunks[0] == line_a
        assert chunks[1] == line_b

    def test_splits_without_newline(self) -> None:
        text = "a" * 3800
        chunks = chunk_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 1900
        assert chunks[1] == "a" * 1900

    def test_empty_message(self) -> None:
        assert chunk_message("") == [""]

    def test_multi_chunk(self) -> None:
        text = ("line\n") * 1000  # ~5000 chars
        chunks = chunk_message(text)
        assert all(len(c) <= 1900 for c in chunks)
        # Reassemble should give back original content (minus stripped newlines)
        reassembled = "\n".join(chunks)
        assert "line" in reassembled


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_message(content, mentions, channel):
    return SimpleNamespace(
        author=object(),
        content=content,
        mentions=mentions,
        channel=channel,
        add_reaction=AsyncMock(),
    )


class TestOnMessageRouting:
    """on_message is exercised with a fake `self` so no real bot/Discord is needed."""

    @pytest.mark.asyncio
    async def test_mention_routes_to_concierge(self) -> None:
        user = SimpleNamespace(id=42)
        channel = SimpleNamespace(id=777, typing=lambda: _Typing(), send=AsyncMock())
        concierge = SimpleNamespace(handle_message=AsyncMock(return_value="dev is idle."))
        bot = SimpleNamespace(
            user=user,
            concierge=concierge,
            orchestrator=SimpleNamespace(forges={}, config={}),
            _send_to_target=AsyncMock(),
        )
        msg = _fake_message("<@42> what's up with dev?", [user], channel)

        await MycelBot.on_message(bot, msg)

        concierge.handle_message.assert_awaited_once_with("777", "what's up with dev?")
        bot._send_to_target.assert_awaited_once_with("dev is idle.", channel)

    @pytest.mark.asyncio
    async def test_text_prefix_triggers_concierge(self) -> None:
        # User types "@Mycel ..." as plain text (no real mention) — must still route.
        user = SimpleNamespace(id=42)
        channel = SimpleNamespace(id=888, typing=lambda: _Typing(), send=AsyncMock())
        concierge = SimpleNamespace(handle_message=AsyncMock(return_value="here you go"))
        bot = SimpleNamespace(
            user=user,
            concierge=concierge,
            orchestrator=SimpleNamespace(forges={}, config={}),
            _send_to_target=AsyncMock(),
        )
        msg = _fake_message("@Mycel list the forges", [], channel)  # mentions empty

        await MycelBot.on_message(bot, msg)

        concierge.handle_message.assert_awaited_once_with("888", "list the forges")

    @pytest.mark.asyncio
    async def test_freeform_in_forge_thread_still_injects_feedback(self) -> None:
        user = SimpleNamespace(id=42)
        thread = SimpleNamespace(id=555)
        channel = thread  # message posted in the forge thread
        forge = SimpleNamespace(name="dev", state={"status": "running"})
        orchestrator = SimpleNamespace(
            forges={"dev": forge},
            config={},
            inject_feedback=MagicMock(),
            task_running=True,
        )
        bot = SimpleNamespace(
            user=user,
            concierge=SimpleNamespace(handle_message=AsyncMock()),
            orchestrator=orchestrator,
            _forge_threads={"dev": thread},
            _channels={},
        )
        msg = _fake_message("please also handle edge cases", [], channel)

        await MycelBot.on_message(bot, msg)

        orchestrator.inject_feedback.assert_called_once_with("dev", "please also handle edge cases")
        bot.concierge.handle_message.assert_not_awaited()
