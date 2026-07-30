"""
Userbot Manager — production-grade async pool of Telethon client agents.

Features
--------
* Stable per-agent device fingerprints + sticky proxy
* Sliding-window rate limits and inter-action pauses
* Cross-account pending-reply coordination
* Stop-words / length / bot filters
* FloodWait → cooldown (no crash) + admin alert
* Nested spintax replies with optional humanization
* Emergency kill-switch and graceful shutdown
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient, events, functions
from telethon.errors import (
    AuthKeyDuplicatedError,
    FloodWaitError,
    RPCError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.types import MessageService, SendMessageTypingAction

from brand_monitor.config import Settings, get_settings
from brand_monitor.core.rate_limiter import SlidingWindowRateLimiter
from brand_monitor.core.reply_coordinator import ReplyCoordinator
from brand_monitor.database.models import Agent, Keyword, KnowledgeEntry
from brand_monitor.database.repository import Database
from brand_monitor.utils.backoff import ExponentialBackoff
from brand_monitor.utils.templates import render_template

logger = logging.getLogger(__name__)

AdminNotifier = Callable[[int, str, str], Awaitable[None]]

FATAL_TELETHON_ERRORS: tuple[type[BaseException], ...] = (
    AuthKeyDuplicatedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    SessionPasswordNeededError,
)


class FatalAgentError(Exception):
    """Raised when an agent must be deactivated and its coroutine terminated."""


def _is_fatal_error(exc: BaseException) -> bool:
    if isinstance(exc, FatalAgentError):
        return True
    if isinstance(exc, FATAL_TELETHON_ERRORS):
        return True
    name = type(exc).__name__.lower()
    return "authkey" in name or "userdeactivated" in name


def _is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, FloodWaitError):
        return False
    if isinstance(exc, RPCError) and not _is_fatal_error(exc):
        return True
    name = type(exc).__name__.lower()
    return any(
        token in name
        for token in ("timeout", "disconnect", "connection", "network", "proxy")
    )


@dataclass
class AgentRuntime:
    agent: Agent
    client: Optional[TelegramClient] = None
    task: Optional[asyncio.Task] = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    reconnect_backoff: ExponentialBackoff = field(default_factory=ExponentialBackoff)
    live_status: str = "starting"  # active|cooldown|banned|scheduled_off|paused
    pending_flood_wait: Optional[int] = None


class UserbotManager:
    """Coordinates a pool of Telethon userbots that monitor brand mentions."""

    def __init__(
        self,
        db: Database,
        settings: Optional[Settings] = None,
        admin_notifier: Optional[AdminNotifier] = None,
    ) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.admin_notifier = admin_notifier

        self._runtimes: dict[int, AgentRuntime] = {}
        self._keywords_cache: list[Keyword] = []
        self._stop_words_cache: list[str] = []
        self._keywords_lock = asyncio.Lock()
        self._claim_lock = asyncio.Lock()
        self._started = False
        self._paused = False
        self._reload_task: Optional[asyncio.Task] = None
        self.coordinator = ReplyCoordinator()
        self.rate_limiter = SlidingWindowRateLimiter(
            max_per_hour=self.settings.max_replies_per_hour,
            max_per_day=self.settings.max_replies_per_day,
            min_pause_sec=self.settings.min_action_pause_sec,
            max_pause_sec=self.settings.max_action_pause_sec,
        )

    # ------------------------------------------------------------------ life-cycle

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def start(self) -> None:
        if self._started:
            logger.warning("UserbotManager already started")
            return

        await self.reload_filters()
        agents = await self.db.get_runnable_agents()
        logger.info("Starting UserbotManager with %s agent(s)", len(agents))

        for agent in agents:
            await self._spawn_agent(agent)

        self._started = True
        self._reload_task = asyncio.create_task(
            self._periodic_reload(),
            name="filter-reload",
        )

    async def stop(self) -> None:
        """Graceful shutdown: cancel pending replies, disconnect all clients."""
        logger.info("Stopping UserbotManager (graceful)…")
        self._started = False
        await self.coordinator.emergency_cancel_all()

        if self._reload_task is not None:
            self._reload_task.cancel()
            try:
                await self._reload_task
            except asyncio.CancelledError:
                pass
            self._reload_task = None

        agent_ids = list(self._runtimes.keys())
        await asyncio.gather(
            *(self.stop_agent(agent_id) for agent_id in agent_ids),
            return_exceptions=True,
        )
        logger.info("UserbotManager stopped — all sessions disconnected")

    async def emergency_stop(self) -> int:
        """Kill switch: pause pool, cancel pending replies, disconnect clients."""
        self._paused = True
        cancelled = await self.coordinator.emergency_cancel_all()
        updated = await self.db.set_all_agents_paused(True)
        agent_ids = list(self._runtimes.keys())
        await asyncio.gather(
            *(self.stop_agent(aid) for aid in agent_ids),
            return_exceptions=True,
        )
        for aid in agent_ids:
            runtime = self._runtimes.get(aid)
            if runtime:
                runtime.live_status = "paused"
        logger.warning(
            "EMERGENCY STOP — paused_db=%s cancelled_pending=%s",
            updated,
            cancelled,
        )
        return updated

    async def resume_all(self) -> int:
        """Clear kill-switch and restart paused agents that have sessions."""
        self._paused = False
        count = await self.db.set_all_agents_paused(False)
        agents = await self.db.get_runnable_agents()
        for agent in agents:
            if agent.id not in self._runtimes:
                await self._spawn_agent(agent)
        return count

    async def start_agent(self, agent_id: int) -> bool:
        agent = await self.db.ensure_fingerprint(agent_id)
        if not agent.session_string:
            logger.error("Cannot start agent %s: empty session_string", agent_id)
            return False

        if agent_id in self._runtimes:
            await self.stop_agent(agent_id)

        if agent.status in {"inactive", "paused", "banned"}:
            await self.db.update_agent_status(agent_id, "active", last_error=None)
            agent.status = "active"

        self._paused = False
        await self._spawn_agent(agent)
        return True

    async def stop_agent(self, agent_id: int) -> None:
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            return

        runtime.stop_event.set()
        runtime.live_status = "paused"
        if runtime.task is not None and not runtime.task.done():
            runtime.task.cancel()
            try:
                await runtime.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.exception("Error while stopping agent %s: %s", agent_id, exc)

        await self._disconnect_client(runtime)
        self._runtimes.pop(agent_id, None)
        logger.info("Agent %s stopped", agent_id)

    async def reload_filters(self) -> None:
        async with self._keywords_lock:
            self._keywords_cache = await self.db.get_active_keywords()
            self._stop_words_cache = await self.db.get_active_stop_words()
        logger.debug(
            "Filters reloaded: keywords=%s stop_words=%s",
            len(self._keywords_cache),
            len(self._stop_words_cache),
        )

    async def reload_keywords(self) -> None:
        await self.reload_filters()

    def running_agent_ids(self) -> list[int]:
        return list(self._runtimes.keys())

    # ------------------------------------------------------------------ spawning

    async def _spawn_agent(self, agent: Agent) -> None:
        agent = await self.db.ensure_fingerprint(agent.id)
        backoff = ExponentialBackoff(
            base=self.settings.backoff_base,
            maximum=self.settings.backoff_max,
            max_retries=self.settings.backoff_max_retries,
        )
        runtime = AgentRuntime(
            agent=agent,
            reconnect_backoff=backoff,
            live_status="starting",
        )
        self._runtimes[agent.id] = runtime
        runtime.task = asyncio.create_task(
            self._agent_loop(runtime),
            name=f"agent-{agent.id}-{agent.phone}",
        )
        logger.info(
            "Spawned agent id=%s phone=%s device=%s proxy=%s window=%s-%s",
            agent.id,
            agent.phone,
            agent.device_model,
            bool(agent.proxy_host),
            agent.work_window_start,
            agent.work_window_end,
        )

    async def _periodic_reload(self) -> None:
        try:
            while self._started:
                await asyncio.sleep(30)
                try:
                    await self.reload_filters()
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to reload filters")
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ agent loop

    async def _agent_loop(self, runtime: AgentRuntime) -> None:
        agent = runtime.agent
        logger.info("Agent loop started: id=%s phone=%s", agent.id, agent.phone)

        try:
            while not runtime.stop_event.is_set() and not self._paused:
                try:
                    # Honour persisted cooldown before reconnecting
                    fresh = await self.db.get_agent(agent.id)
                    if fresh is not None:
                        runtime.agent = fresh
                        agent = fresh
                    if agent.is_in_cooldown():
                        await self._sleep_cooldown(runtime)
                        if runtime.stop_event.is_set() or self._paused:
                            break

                    await self._run_client_session(runtime)
                    if runtime.pending_flood_wait is not None:
                        seconds = runtime.pending_flood_wait
                        runtime.pending_flood_wait = None
                        await self._enter_flood_cooldown(runtime, seconds)
                        continue
                    break
                except asyncio.CancelledError:
                    raise
                except FloodWaitError as exc:
                    await self._enter_flood_cooldown(runtime, int(exc.seconds))
                    continue
                except Exception as exc:  # noqa: BLE001
                    if runtime.stop_event.is_set() or self._paused:
                        break

                    if runtime.pending_flood_wait is not None:
                        seconds = runtime.pending_flood_wait
                        runtime.pending_flood_wait = None
                        await self._enter_flood_cooldown(runtime, seconds)
                        continue

                    if _is_fatal_error(exc):
                        await self._handle_fatal_failure(runtime, exc)
                        break

                    if isinstance(exc, FloodWaitError):
                        await self._enter_flood_cooldown(runtime, int(exc.seconds))
                        continue

                    if _is_network_error(exc) or isinstance(exc, RPCError):
                        if runtime.reconnect_backoff.exhausted:
                            await self._handle_fatal_failure(
                                runtime,
                                RuntimeError(f"Reconnect attempts exhausted after: {exc}"),
                            )
                            break
                        delay = await runtime.reconnect_backoff.wait()
                        logger.warning(
                            "Agent %s network/RPC (%s) — reconnect in %.1fs (%s/%s)",
                            agent.id,
                            type(exc).__name__,
                            delay,
                            runtime.reconnect_backoff.attempt,
                            runtime.reconnect_backoff.max_retries,
                        )
                        await self._disconnect_client(runtime)
                        continue

                    logger.exception("Unexpected error in agent %s: %s", agent.id, exc)
                    if runtime.reconnect_backoff.exhausted:
                        await self._handle_fatal_failure(runtime, exc)
                        break
                    await runtime.reconnect_backoff.wait()
                    await self._disconnect_client(runtime)

        except asyncio.CancelledError:
            logger.info("Agent %s cancelled", agent.id)
            raise
        finally:
            await self._disconnect_client(runtime)
            current = self._runtimes.get(agent.id)
            if current is runtime:
                self._runtimes.pop(agent.id, None)
            logger.info("Agent loop finished: id=%s", agent.id)

    async def _sleep_cooldown(self, runtime: AgentRuntime) -> None:
        agent = runtime.agent
        runtime.live_status = "cooldown"
        until = agent.cooldown_until
        if not until:
            return
        try:
            target = datetime.fromisoformat(until)
        except ValueError:
            return
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        wait = max(0.0, (target - now).total_seconds())
        if wait <= 0:
            await self.db.update_agent_status(agent.id, "active", last_error=None, cooldown_until=None)
            runtime.live_status = "active"
            return
        logger.info("Agent %s sleeping remaining cooldown %.0fs", agent.id, wait)
        try:
            await asyncio.wait_for(runtime.stop_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        if not runtime.stop_event.is_set():
            await self.db.update_agent_status(agent.id, "active", last_error=None, cooldown_until=None)
            runtime.agent.status = "active"
            runtime.agent.cooldown_until = None
            runtime.live_status = "active"

    async def _enter_flood_cooldown(self, runtime: AgentRuntime, seconds: int) -> None:
        """FloodWait: mark cooldown, alert admin, sleep seconds+extra — do not crash."""
        agent = runtime.agent
        wait_for = int(seconds) + int(self.settings.flood_wait_extra_sec)
        until = datetime.now(timezone.utc) + timedelta(seconds=wait_for)
        until_iso = until.isoformat()
        reason = f"FloodWaitError: frozen for {wait_for}s (until {until_iso})"
        logger.warning("Agent %s → cooldown: %s", agent.id, reason)
        runtime.live_status = "cooldown"
        await self.db.update_agent_status(
            agent.id,
            "cooldown",
            last_error=reason,
            cooldown_until=until_iso,
        )
        runtime.agent.status = "cooldown"
        runtime.agent.cooldown_until = until_iso
        await self._notify_admin(agent, f"🟡 COOLDOWN {wait_for}s\n{reason}")
        await self._disconnect_client(runtime)
        try:
            await asyncio.wait_for(runtime.stop_event.wait(), timeout=wait_for)
        except asyncio.TimeoutError:
            pass
        if not runtime.stop_event.is_set() and not self._paused:
            await self.db.update_agent_status(
                agent.id, "active", last_error=None, cooldown_until=None
            )
            runtime.agent.status = "active"
            runtime.agent.cooldown_until = None
            runtime.live_status = "active"
            runtime.reconnect_backoff.reset()

    async def _run_client_session(self, runtime: AgentRuntime) -> None:
        agent = await self.db.ensure_fingerprint(runtime.agent.id)
        runtime.agent = agent

        if not agent.session_string:
            raise FatalAgentError("Empty session_string — agent cannot connect")
        if not agent.has_fingerprint:
            raise FatalAgentError("Fingerprint missing after ensure")

        try:
            proxy = agent.proxy_tuple
        except Exception as exc:  # noqa: BLE001
            raise FatalAgentError(f"Invalid proxy configuration: {exc}") from exc

        client = TelegramClient(
            StringSession(agent.session_string),
            agent.api_id,
            agent.api_hash,
            proxy=proxy,
            connection_retries=1,
            retry_delay=1,
            auto_reconnect=False,
            device_model=agent.device_model or "Smartphone",
            system_version=agent.system_version or "SDK 33",
            app_version=agent.app_version or "10.0.0",
            lang_code=agent.lang_code or "ru",
            system_lang_code=agent.lang_code or "ru",
        )
        runtime.client = client

        @client.on(events.NewMessage(incoming=True))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            if runtime.stop_event.is_set() or self._paused:
                return
            try:
                await self._handle_new_message(runtime, event)
            except FloodWaitError as exc:
                # Signal agent loop; disconnect so session waiter wakes up.
                runtime.pending_flood_wait = int(exc.seconds)
                await self._disconnect_client(runtime)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Handler error agent=%s chat=%s msg=%s",
                    agent.id,
                    getattr(event, "chat_id", None),
                    getattr(getattr(event, "message", None), "id", None),
                )

        logger.info(
            "Connecting agent %s device=%s/%s/%s proxy=%s",
            agent.id,
            agent.device_model,
            agent.system_version,
            agent.app_version,
            bool(proxy),
        )
        await client.connect()

        if not await client.is_user_authorized():
            raise FatalAgentError("Session is not authorized")

        me = await client.get_me()
        logger.info(
            "Agent %s online as %s (id=%s)",
            agent.id,
            getattr(me, "username", None) or me.id,
            me.id,
        )
        runtime.reconnect_backoff.reset()
        runtime.live_status = (
            "active" if agent.is_within_work_window() else "scheduled_off"
        )

        disconnect_task = asyncio.create_task(client.disconnected)
        stop_task = asyncio.create_task(runtime.stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {disconnect_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if stop_task in done or self._paused:
                logger.info("Stop requested for agent %s", agent.id)
                return

            raise ConnectionError(f"Telethon client disconnected for agent {agent.id}")
        finally:
            if not disconnect_task.done():
                disconnect_task.cancel()
            if not stop_task.done():
                stop_task.cancel()

    # ------------------------------------------------------------------ messaging

    async def _handle_new_message(
        self,
        runtime: AgentRuntime,
        event: events.NewMessage.Event,
    ) -> None:
        if self._paused or runtime.stop_event.is_set():
            return

        agent = runtime.agent
        message = event.message
        if message is None:
            return

        # System / service messages
        if isinstance(message, MessageService) or getattr(message, "action", None):
            return

        if event.is_private:
            return

        if message.out:
            return

        sender = await event.get_sender()
        if sender is not None and getattr(sender, "bot", False):
            return

        text = (message.message or "").strip()
        if not text:
            return

        # Length gate
        if len(text) < self.settings.min_message_length:
            return
        if len(text) > self.settings.max_message_length:
            return

        # Stop-words
        if self._contains_stop_word(text):
            logger.debug(
                "Stop-word hit — skip chat=%s msg=%s",
                event.chat_id,
                message.id,
            )
            return

        keyword = await self._match_keyword(text)
        if keyword is None:
            return

        fresh = await self.db.get_agent(agent.id)
        if fresh is not None:
            runtime.agent = fresh
            agent = fresh

        if agent.status in {"paused", "banned", "inactive"}:
            return
        if agent.is_in_cooldown() or agent.status == "cooldown":
            runtime.live_status = "cooldown"
            return

        if not agent.is_within_work_window():
            runtime.live_status = "scheduled_off"
            return
        runtime.live_status = "active"

        # Rate limits (sliding window + min pause)
        decision = self.rate_limiter.check(agent.id)
        if not decision.allowed:
            logger.debug(
                "Agent %s rate-limited (%s) retry_after=%.0fs",
                agent.id,
                decision.reason,
                decision.retry_after_seconds,
            )
            return

        # Cross-account memory + DB claim
        async with self._claim_lock:
            if self.coordinator.is_seen(event.chat_id, message.id):
                return
            if await self.db.is_message_processed(event.chat_id, message.id):
                return

            knowledge = await self._resolve_knowledge(keyword)
            claimed = await self.db.try_claim_message(
                chat_id=event.chat_id,
                message_id=message.id,
                agent_id=agent.id,
                keyword_id=keyword.id,
                knowledge_base_id=knowledge.id if knowledge else None,
                trigger_keyword=keyword.keyword,
                source_text=text[:500],
            )
            if not claimed:
                return

            pending = await self.coordinator.try_register(
                agent.id, event.chat_id, message.id
            )
            if pending is None:
                await self.db.mark_interaction_cancelled(
                    event.chat_id, message.id, agent.id
                )
                return

        if knowledge is None:
            await self.db.mark_interaction_cancelled(event.chat_id, message.id, agent.id)
            await self.coordinator.cancel(event.chat_id, message.id, agent.id)
            return

        # Delayed reply task — competitors cancel via coordinator
        task = asyncio.create_task(
            self._delayed_reply(
                runtime=runtime,
                pending_cancel=pending.cancel_event,
                chat_id=event.chat_id,
                message_id=message.id,
                keyword=keyword,
                knowledge=knowledge,
            ),
            name=f"reply-{agent.id}-{event.chat_id}-{message.id}",
        )
        await self.coordinator.bind_task(event.chat_id, message.id, task)

    async def _delayed_reply(
        self,
        *,
        runtime: AgentRuntime,
        pending_cancel: asyncio.Event,
        chat_id: int,
        message_id: int,
        keyword: Keyword,
        knowledge: KnowledgeEntry,
    ) -> None:
        agent = runtime.agent
        delay = random.uniform(
            self.settings.pre_reply_delay_min,
            self.settings.pre_reply_delay_max,
        )
        try:
            # Wait delay, abort if cancelled by another account / kill-switch
            wait_task = asyncio.create_task(asyncio.sleep(delay))
            cancel_task = asyncio.create_task(pending_cancel.wait())
            done, pending = await asyncio.wait(
                {wait_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if cancel_task in done or self._paused or runtime.stop_event.is_set():
                await self.db.mark_interaction_cancelled(chat_id, message_id, agent.id)
                await self.coordinator.cancel(chat_id, message_id, agent.id)
                logger.info(
                    "Reply cancelled agent=%s chat=%s msg=%s",
                    agent.id,
                    chat_id,
                    message_id,
                )
                return

            # Re-check rate limit after delay
            decision = self.rate_limiter.check(agent.id)
            if not decision.allowed:
                await self.db.mark_interaction_cancelled(chat_id, message_id, agent.id)
                await self.coordinator.complete(chat_id, message_id, agent.id)
                return

            reply_text = render_template(
                knowledge.response_template,
                context={
                    "keyword": keyword.keyword,
                    "category": keyword.category,
                    "agent": agent.display_name or agent.phone,
                },
                emoji_chance=self.settings.emoji_chance,
                typo_enabled=self.settings.typo_enabled,
                case_randomize=self.settings.case_randomize,
                zwsp_enabled=self.settings.zwsp_enabled,
            )
            if not reply_text:
                await self.db.mark_interaction_cancelled(chat_id, message_id, agent.id)
                await self.coordinator.complete(chat_id, message_id, agent.id)
                return

            await self._send_humanized_reply(runtime, chat_id, reply_text)
            await self.db.mark_interaction_sent(
                chat_id, message_id, agent.id, reply_text
            )
            await self.db.touch_last_action(agent.id)
            pause = self.rate_limiter.register_action(agent.id)
            await self.coordinator.complete(chat_id, message_id, agent.id)
            logger.info(
                "Agent %s replied chat=%s msg=%s kw='%s' next_pause=%.0fs",
                agent.id,
                chat_id,
                message_id,
                keyword.keyword,
                pause,
            )
        except asyncio.CancelledError:
            await self.db.mark_interaction_cancelled(chat_id, message_id, agent.id)
            await self.coordinator.cancel(chat_id, message_id, agent.id)
            raise
        except FloodWaitError as exc:
            await self.db.mark_interaction_cancelled(chat_id, message_id, agent.id)
            await self.coordinator.cancel(chat_id, message_id, agent.id)
            await self._enter_flood_cooldown(runtime, int(exc.seconds))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Delayed reply failed agent=%s chat=%s msg=%s",
                agent.id,
                chat_id,
                message_id,
            )
            await self.db.mark_interaction_cancelled(chat_id, message_id, agent.id)
            await self.coordinator.cancel(chat_id, message_id, agent.id)

    def _contains_stop_word(self, text: str) -> bool:
        lowered = text.lower()
        return any(sw in lowered for sw in self._stop_words_cache)

    async def _match_keyword(self, text: str) -> Optional[Keyword]:
        lowered = text.lower()
        async with self._keywords_lock:
            keywords = list(self._keywords_cache)
        for kw in keywords:
            if kw.keyword.lower() in lowered:
                return kw
        return None

    async def _resolve_knowledge(self, keyword: Keyword) -> Optional[KnowledgeEntry]:
        if keyword.knowledge_base_id is not None:
            entry = await self.db.get_knowledge(keyword.knowledge_base_id)
            if entry is not None:
                return entry
        by_category = await self.db.get_knowledge_by_category(keyword.category)
        if by_category:
            return random.choice(by_category)
        all_entries = [e for e in await self.db.list_knowledge() if e.is_active]
        if all_entries:
            return random.choice(all_entries)
        return None

    async def _send_humanized_reply(
        self,
        runtime: AgentRuntime,
        chat_id: int,
        text: str,
    ) -> None:
        client = runtime.client
        if client is None or not client.is_connected():
            raise ConnectionError("Client is not connected")

        delay = random.uniform(
            self.settings.typing_delay_min,
            self.settings.typing_delay_max,
        )
        typing_chunk = min(delay, 4.5)
        elapsed = 0.0
        while elapsed < delay:
            if runtime.stop_event.is_set() or self._paused:
                raise asyncio.CancelledError()
            try:
                await client(
                    functions.messages.SetTypingRequest(
                        peer=chat_id,
                        action=SendMessageTypingAction(),
                    )
                )
            except Exception:  # noqa: BLE001
                try:
                    await client.send_chat_action(chat_id, "typing")
                except Exception:  # noqa: BLE001
                    pass
            sleep_for = min(typing_chunk, delay - elapsed)
            await asyncio.sleep(sleep_for)
            elapsed += sleep_for

        await client.send_message(chat_id, text)

    # ------------------------------------------------------------------ failure handling

    async def _handle_fatal_failure(
        self,
        runtime: AgentRuntime,
        exc: BaseException,
    ) -> None:
        agent = runtime.agent
        reason = f"{type(exc).__name__}: {exc}"
        status = "banned" if "deactivated" in type(exc).__name__.lower() else "inactive"
        runtime.live_status = "banned" if status == "banned" else "paused"
        logger.error(
            "Fatal failure for agent %s (%s): %s — %s",
            agent.id,
            agent.phone,
            reason,
            status,
        )
        try:
            await self.db.update_agent_status(agent.id, status, last_error=reason)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to update status for agent %s", agent.id)

        await self._disconnect_client(runtime)
        await self._notify_admin(agent, f"🔴 {status.upper()}\n{reason}")

    async def _notify_admin(self, agent: Agent, reason: str) -> None:
        if self.admin_notifier is None:
            logger.warning(
                "Admin notifier not configured. Agent %s: %s",
                agent.id,
                reason,
            )
            return
        try:
            await self.admin_notifier(agent.id, agent.phone, reason)
        except Exception:  # noqa: BLE001
            logger.exception("Admin notification failed for agent %s", agent.id)

    async def _disconnect_client(self, runtime: AgentRuntime) -> None:
        client = runtime.client
        runtime.client = None
        if client is None:
            return
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("Error during client disconnect", exc_info=True)

    # ------------------------------------------------------------------ diagnostics

    def status_snapshot(self) -> list[dict]:
        now = datetime.now()
        snapshot = []

        # Include DB agents even if not in runtime (paused / banned)
        runtime_ids = set(self._runtimes.keys())
        for agent_id, runtime in self._runtimes.items():
            agent = runtime.agent
            hour_c, day_c = self.rate_limiter.counts(agent_id)
            live = runtime.live_status
            if self._paused:
                live = "paused"
            elif agent.is_in_cooldown() or agent.status == "cooldown":
                live = "cooldown"
            elif not agent.is_within_work_window(now):
                live = "scheduled_off"
            elif runtime.client and runtime.client.is_connected():
                live = "active"
            snapshot.append(
                {
                    "agent_id": agent_id,
                    "phone": agent.phone,
                    "status_db": agent.status,
                    "live_status": live,
                    "connected": bool(
                        runtime.client is not None and runtime.client.is_connected()
                    ),
                    "within_work_window": agent.is_within_work_window(now),
                    "work_window": f"{agent.work_window_start}-{agent.work_window_end}",
                    "device_model": agent.device_model,
                    "cooldown_until": agent.cooldown_until,
                    "replies_hour": hour_c,
                    "replies_day": day_c,
                    "reconnect_attempt": runtime.reconnect_backoff.attempt,
                    "task_done": runtime.task.done() if runtime.task else True,
                }
            )
        _ = runtime_ids
        return snapshot

    async def full_status(self) -> list[dict]:
        """Merge runtime snapshot with offline DB agents for admin live panel."""
        snap = {item["agent_id"]: item for item in self.status_snapshot()}
        agents = await self.db.list_agents()
        result = []
        for agent in agents:
            if agent.id in snap:
                result.append(snap[agent.id])
                continue
            live = {
                "inactive": "paused",
                "paused": "paused",
                "banned": "banned",
                "cooldown": "cooldown",
                "active": "scheduled_off",
            }.get(agent.status, "paused")
            result.append(
                {
                    "agent_id": agent.id,
                    "phone": agent.phone,
                    "status_db": agent.status,
                    "live_status": live,
                    "connected": False,
                    "within_work_window": agent.is_within_work_window(),
                    "work_window": f"{agent.work_window_start}-{agent.work_window_end}",
                    "device_model": agent.device_model,
                    "cooldown_until": agent.cooldown_until,
                    "replies_hour": 0,
                    "replies_day": 0,
                    "reconnect_attempt": 0,
                    "task_done": True,
                }
            )
        return result
