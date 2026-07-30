"""Cross-account reply coordination — cancel competing delayed replies."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PendingReply:
    agent_id: int
    chat_id: int
    message_id: int
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


class ReplyCoordinator:
    """In-memory registry of in-flight delayed replies keyed by chat/message."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pending: dict[tuple[int, int], PendingReply] = {}
        # Fast membership set for O(1) duplicate checks
        self._seen: set[tuple[int, int]] = set()

    def is_seen(self, chat_id: int, message_id: int) -> bool:
        return (chat_id, message_id) in self._seen

    async def try_register(
        self,
        agent_id: int,
        chat_id: int,
        message_id: int,
    ) -> Optional[PendingReply]:
        """Register ownership of a delayed reply.

        Returns PendingReply if this agent won; None if another agent already owns it.
        If another agent had a pending timer, it is cancelled.
        """
        key = (chat_id, message_id)
        async with self._lock:
            existing = self._pending.get(key)
            if existing is not None:
                if existing.agent_id == agent_id:
                    return existing
                # First claimant keeps the timer; late agent aborts itself.
                logger.info(
                    "Agent %s skips %s/%s — already queued by agent %s",
                    agent_id,
                    chat_id,
                    message_id,
                    existing.agent_id,
                )
                self._seen.add(key)
                return None

            if key in self._seen:
                return None

            pending = PendingReply(
                agent_id=agent_id,
                chat_id=chat_id,
                message_id=message_id,
            )
            self._pending[key] = pending
            self._seen.add(key)
            return pending

    async def bind_task(self, chat_id: int, message_id: int, task: asyncio.Task) -> None:
        key = (chat_id, message_id)
        async with self._lock:
            pending = self._pending.get(key)
            if pending is not None:
                pending.task = task

    async def complete(self, chat_id: int, message_id: int, agent_id: int) -> None:
        key = (chat_id, message_id)
        async with self._lock:
            pending = self._pending.get(key)
            if pending is not None and pending.agent_id == agent_id:
                self._pending.pop(key, None)
            self._seen.add(key)

    async def cancel(self, chat_id: int, message_id: int, agent_id: int) -> None:
        key = (chat_id, message_id)
        async with self._lock:
            pending = self._pending.get(key)
            if pending is None or pending.agent_id != agent_id:
                return
            pending.cancel_event.set()
            if pending.task and not pending.task.done():
                pending.task.cancel()
            self._pending.pop(key, None)

    async def emergency_cancel_all(self) -> int:
        async with self._lock:
            count = len(self._pending)
            for pending in list(self._pending.values()):
                pending.cancel_event.set()
                if pending.task and not pending.task.done():
                    pending.task.cancel()
            self._pending.clear()
            return count
