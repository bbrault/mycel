from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

# Allow imports from project root
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from message_bus import Message, MessageBus


class TestMessage:
    def test_to_dict_contains_all_fields(self) -> None:
        msg = Message(
            source="forge",
            content="hello",
            level="info",
            forge_name="dev",
            skill_name="plan",
            data={"key": "val"},
        )
        d = msg.to_dict()
        assert d["source"] == "forge"
        assert d["content"] == "hello"
        assert d["level"] == "info"
        assert d["forge_name"] == "dev"
        assert d["skill_name"] == "plan"
        assert d["data"] == {"key": "val"}
        assert "timestamp" in d

    def test_defaults(self) -> None:
        msg = Message(source="cortex", content="test")
        assert msg.level == "info"
        assert msg.forge_name is None
        assert msg.data == {}


class TestMessageBus:
    @pytest.mark.asyncio
    async def test_publish_subscribe(self) -> None:
        bus = MessageBus(bus_dir=tempfile.mkdtemp())
        received: list[Message] = []

        async def callback(msg: Message) -> None:
            received.append(msg)

        bus.subscribe(callback)
        await bus.start()

        msg = Message(source="test", content="hello", forge_name="dev")
        await bus.publish(msg)

        # Give the bus time to process
        await asyncio.sleep(0.2)
        await bus.stop()

        assert len(received) == 1
        assert received[0].content == "hello"

    @pytest.mark.asyncio
    async def test_persist_message_creates_jsonl(self) -> None:
        tmp = tempfile.mkdtemp()
        bus = MessageBus(bus_dir=tmp)
        await bus.start()

        msg = Message(source="test", content="persisted", forge_name="myforge")
        await bus.publish(msg)

        await asyncio.sleep(0.2)
        await bus.stop()

        log_path = os.path.join(tmp, "myforge", "messages.jsonl")
        assert os.path.exists(log_path)

        with open(log_path) as f:
            line = f.readline()
        data = json.loads(line)
        assert data["content"] == "persisted"

    @pytest.mark.asyncio
    async def test_no_persist_without_forge_name(self) -> None:
        tmp = tempfile.mkdtemp()
        bus = MessageBus(bus_dir=tmp)
        await bus.start()

        msg = Message(source="test", content="no forge")
        await bus.publish(msg)

        await asyncio.sleep(0.2)
        await bus.stop()

        # No directory should have been created
        assert os.listdir(tmp) == []

    def test_read_log_empty(self) -> None:
        bus = MessageBus(bus_dir=tempfile.mkdtemp())
        assert bus.read_log("nonexistent") == []

    def test_read_log_returns_last_n(self) -> None:
        tmp = tempfile.mkdtemp()
        forge_dir = os.path.join(tmp, "dev")
        os.makedirs(forge_dir)
        log_path = os.path.join(forge_dir, "messages.jsonl")

        with open(log_path, "w") as f:
            for i in range(10):
                json.dump({"content": f"msg-{i}"}, f)
                f.write("\n")

        bus = MessageBus(bus_dir=tmp)
        result = bus.read_log("dev", limit=3)
        assert len(result) == 3
        assert result[0]["content"] == "msg-7"
        assert result[2]["content"] == "msg-9"

    @pytest.mark.asyncio
    async def test_callback_error_does_not_crash_bus(self) -> None:
        bus = MessageBus(bus_dir=tempfile.mkdtemp())
        ok_received: list[Message] = []

        async def bad_callback(msg: Message) -> None:
            raise ValueError("boom")

        async def good_callback(msg: Message) -> None:
            ok_received.append(msg)

        bus.subscribe(bad_callback)
        bus.subscribe(good_callback)
        await bus.start()

        await bus.publish(Message(source="test", content="x", forge_name="dev"))
        await asyncio.sleep(0.2)
        await bus.stop()

        assert len(ok_received) == 1
