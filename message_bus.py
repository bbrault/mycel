from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("mycel.bus")

Callback = Callable[["Message"], Coroutine[Any, Any, None]]


def atomic_write_json(path: str, data: Any, indent: int = 2) -> None:
    """Write JSON to *path* atomically (tmp file + os.replace).

    A crash mid-write leaves the previous file intact instead of a truncated one.
    """
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class Message:
    """A single message on the bus."""

    def __init__(
        self,
        source: str,
        content: str,
        level: str = "info",
        forge_name: Optional[str] = None,
        skill_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.source = source
        self.content = content
        self.level = level
        self.forge_name = forge_name
        self.skill_name = skill_name
        self.data = data or {}
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "content": self.content,
            "level": self.level,
            "forge_name": self.forge_name,
            "skill_name": self.skill_name,
            "data": self.data,
            "timestamp": self.timestamp,
        }


class MessageBus:
    """Async message bus with subscriber callbacks."""

    def __init__(self, bus_dir: str = "bus") -> None:
        self.bus_dir = bus_dir
        self._callbacks: List[Callback] = []
        # Queue created lazily in start() to bind to the running event loop (Python 3.9)
        self._queue: Optional[asyncio.Queue[Message]] = None
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    def subscribe(self, callback: Callback) -> None:
        self._callbacks.append(callback)

    async def publish(self, message: Message) -> None:
        if self._queue is None:
            logger.warning("MessageBus not started, message dropped: %s", message.content[:80])
            return
        await self._queue.put(message)

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        self._running = True
        self._task = asyncio.create_task(self._process_loop())
        logger.info("MessageBus started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("MessageBus stopped")

    async def _process_loop(self) -> None:
        while self._running:
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Persist off the event loop — appending to the JSONL log shouldn't
            # block delivery of the message to subscribers on a slow disk.
            await asyncio.to_thread(self._persist_message, message)

            for callback in self._callbacks:
                try:
                    await callback(message)
                except Exception as exc:
                    logger.error("Callback error: %s", exc, exc_info=True)

    def _persist_message(self, message: Message) -> None:
        if message.forge_name is None:
            return
        forge_dir = os.path.join(self.bus_dir, message.forge_name)
        os.makedirs(forge_dir, exist_ok=True)
        log_path = os.path.join(forge_dir, "messages.jsonl")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")

    def read_log(self, forge_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Read the last *limit* messages from a forge's JSONL log."""
        log_path = os.path.join(self.bus_dir, forge_name, "messages.jsonl")
        if not os.path.exists(log_path):
            return []

        messages: List[Dict[str, Any]] = []
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        return messages[-limit:]

    async def read_log_async(self, forge_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Async wrapper over :meth:`read_log` — the JSONL log grows over a run,
        so read it off the event loop."""
        return await asyncio.to_thread(self.read_log, forge_name, limit)
