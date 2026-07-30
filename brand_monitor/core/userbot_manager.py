"""
Userbot Manager — async pool of Telethon client agents.

Responsibilities
----------------
* Launch each support agent in an isolated coroutine
* Filter incoming group messages against monitored keywords
* Enforce per-agent work windows and de-duplicate via interaction_log
* Simulate human typing before sending a knowledge-base reply
* Isolate fatal session/proxy failures so one agent cannot take down the pool
* Reconnect with exponential backoff on transient network errors
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
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
from telethon.tl.types import SendMessageTypingAction

from brand_monitor.config import Settings, get_settings
from brand_monitor.database.models import Agent, Keyword, KnowledgeEntry
from brand_monitor.database.repository import Database
from brand_monitor.utils.backoff import ExponentialBackoff
from brand_monitor.utils.templates import render_template

logger = logging.getLogger(__name__)

# Callback signature: async (agent_id, phone, reason) -> None
AdminNotifier = Callable[[int, str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Fatal vs transient Telethon errors
# ---------------------------------------------------------------------------

FATAL_TELETHON_ERRORS: tuple[type[BaseException], ...] = (
    AuthKeyDuplicatedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    SessionPasswordNeededError,
)


class FatalAgentError(Exception):
    """Raised when an agent must be deactivated and its coroutine terminated."""


def _is_fatal_error(exc: BaseException) -> bool:
    """Return True for errors that require deactivating the agent permanently."""
    if isinstance(exc, FatalAgentError):
        return True
    if isinstance(exc, FATAL_TELETHON_ERRORS):
        return True
    # Auth key / session permanently invalid
    name = type(exc).__name__.lower()
    if "authkey" in name or "userdeactivated" in name:
        return True
    return False


def _is_network_error(exc: BaseException) -> bool:
    """Heuristic for transient connection / RPC failures eligible for backoff."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, RPCError) and not _is_fatal_error(exc):
        # FloodWait is handled separately; other RPC errors may be transient
        if isinstance(exc, FloodWaitError):
            return False
        return True
    name = type(exc).__name__.lower()
    return any(
        token in name
        for token in ("timeout", "disconnect", "connection", "network", "proxy")
    )


# ---------------------------------------------------------------------------
# Runtime state for a single agent coroutine
# ---------------------------------------------------------------------------


@dataclass
class AgentRuntime:
    """In-memory handle for a running Telethon agent."""

    agent: Agent
    client: Optional[TelegramClient] = None
    task: Optional[asyncio.Task] = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    reconnect_backoff: ExponentialBackoff = field(default_factory=ExponentialBackoff)


# ---------------------------------------------------------------------------
# UserbotManager
# ---------------------------------------------------------------------------


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
        self._keywords_lock = asyncio.Lock()
        self._claim_lock = asyncio.Lock()
        self._started = False
        self._reload_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ life-cycle

    async def start(self) -> None:
        """Load active agents from DB and spawn an isolated coroutine per agent."""
        if self._started:
            logger.warning("UserbotManager already started")
            return

        await self.reload_keywords()
        agents = await self.db.get_active_agents()
        logger.info("Starting UserbotManager with %s active agent(s)", len(agents))

        for agent in agents:
            await self._spawn_agent(agent)

        self._started = True
        self._reload_task = asyncio.create_task(
            self._periodic_keyword_reload(),
            name="keyword-reload",
        )

    async def stop(self) -> None:
        """Gracefully stop every agent coroutine and disconnect clients."""
        logger.info("Stopping UserbotManager…")
        self._started = False

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
        logger.info("UserbotManager stopped")

    async def start_agent(self, agent_id: int) -> bool:
        """Start (or restart) a single agent by id. Returns False if not found/inactive."""
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            logger.error("Cannot start agent %s: not found", agent_id)
            return False
        if not agent.session_string:
            logger.error("Cannot start agent %s: empty session_string", agent_id)
            return False

        if agent_id in self._runtimes:
            await self.stop_agent(agent_id)

        if agent.status != "active":
            await self.db.update_agent_status(agent_id, "active", last_error=None)
            agent.status = "active"

        await self._spawn_agent(agent)
        return True

    async def stop_agent(self, agent_id: int) -> None:
        """Signal stop and await the agent coroutine."""
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            return

        runtime.stop_event.set()
        if runtime.task is not None and not runtime.task.done():
            runtime.task.cancel()
            try:
                await runtime.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — isolate teardown errors
                logger.exception("Error while stopping agent %s: %s", agent_id, exc)

        await self._disconnect_client(runtime)
        self._runtimes.pop(agent_id, None)
        logger.info("Agent %s stopped", agent_id)

    async def reload_keywords(self) -> None:
        """Refresh in-memory keyword list from the database."""
        async with self._keywords_lock:
            self._keywords_cache = await self.db.get_active_keywords()
        logger.debug("Keywords cache reloaded: %s item(s)", len(self._keywords_cache))

    def running_agent_ids(self) -> list[int]:
        return list(self._runtimes.keys())

    # ------------------------------------------------------------------ spawning

    async def _spawn_agent(self, agent: Agent) -> None:
        backoff = ExponentialBackoff(
            base=self.settings.backoff_base,
            maximum=self.settings.backoff_max,
            max_retries=self.settings.backoff_max_retries,
        )
        runtime = AgentRuntime(agent=agent, reconnect_backoff=backoff)
        self._runtimes[agent.id] = runtime
        runtime.task = asyncio.create_task(
            self._agent_loop(runtime),
            name=f"agent-{agent.id}-{agent.phone}",
        )
        logger.info(
            "Spawned agent coroutine id=%s phone=%s window=%s-%s",
            agent.id,
            agent.phone,
            agent.work_window_start,
            agent.work_window_end,
        )

    async def _periodic_keyword_reload(self) -> None:
        """Keep keyword cache reasonably fresh without hammering SQLite."""
        try:
            while self._started:
                await asyncio.sleep(30)
                try:
                    await self.reload_keywords()
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to reload keywords")
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ agent loop

    async def _agent_loop(self, runtime: AgentRuntime) -> None:
        """
        Isolated event loop for a single agent.

        Network / proxy failures trigger exponential backoff reconnects.
        Fatal session errors deactivate the agent and exit the coroutine
        without affecting sibling agents.
        """
        agent = runtime.agent
        logger.info("Agent loop started: id=%s phone=%s", agent.id, agent.phone)

        try:
            while not runtime.stop_event.is_set():
                try:
                    await self._run_client_session(runtime)
                    # Clean disconnect (stop requested) — exit loop
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — per-agent isolation
                    if runtime.stop_event.is_set():
                        break

                    if _is_fatal_error(exc):
                        await self._handle_fatal_failure(runtime, exc)
                        break

                    if isinstance(exc, FloodWaitError):
                        wait_for = int(exc.seconds) + 1
                        logger.warning(
                            "Agent %s FloodWait %ss — sleeping",
                            agent.id,
                            wait_for,
                        )
                        await asyncio.sleep(wait_for)
                        runtime.reconnect_backoff.reset()
                        continue

                    if _is_network_error(exc) or isinstance(exc, RPCError):
                        if runtime.reconnect_backoff.exhausted:
                            await self._handle_fatal_failure(
                                runtime,
                                RuntimeError(
                                    f"Reconnect attempts exhausted after: {exc}"
                                ),
                            )
                            break

                        delay = await runtime.reconnect_backoff.wait()
                        logger.warning(
                            "Agent %s network/RPC error (%s). Reconnecting in %.1fs "
                            "(attempt %s/%s)",
                            agent.id,
                            type(exc).__name__,
                            delay,
                            runtime.reconnect_backoff.attempt,
                            runtime.reconnect_backoff.max_retries,
                        )
                        await self._disconnect_client(runtime)
                        continue

                    # Unexpected non-fatal error — treat like network failure
                    logger.exception(
                        "Unexpected error in agent %s: %s", agent.id, exc
                    )
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
            # Remove from pool if still registered as this runtime
            current = self._runtimes.get(agent.id)
            if current is runtime:
                self._runtimes.pop(agent.id, None)
            logger.info("Agent loop finished: id=%s", agent.id)

    async def _run_client_session(self, runtime: AgentRuntime) -> None:
        """Connect Telethon client, register handlers, and run until disconnect/stop."""
        agent = runtime.agent
        # Refresh agent row so schedule / proxy / session changes apply on reconnect
        fresh = await self.db.get_agent(agent.id)
        if fresh is not None:
            runtime.agent = fresh
            agent = fresh

        if not agent.session_string:
            raise FatalAgentError("Empty session_string — agent cannot connect")

        proxy = None
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
            auto_reconnect=False,  # we own reconnect + backoff
            device_model="BrandMonitor Agent",
            system_version="1.0",
            app_version="1.0",
        )
        runtime.client = client

        @client.on(events.NewMessage(incoming=True))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            if runtime.stop_event.is_set():
                return
            try:
                await self._handle_new_message(runtime, event)
            except Exception:  # noqa: BLE001 — never kill the client on handler bugs
                logger.exception(
                    "Handler error agent=%s chat=%s msg=%s",
                    agent.id,
                    getattr(event, "chat_id", None),
                    getattr(getattr(event, "message", None), "id", None),
                )

        logger.info("Connecting agent %s (proxy=%s)…", agent.id, bool(proxy))
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

        # Block until disconnect or stop request
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

            if stop_task in done:
                logger.info("Stop requested for agent %s", agent.id)
                return

            # Unexpected disconnect — raise so outer loop can backoff
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
        """Validate business rules and, if matched, reply with a KB template."""
        agent = runtime.agent
        message = event.message
        if message is None or not message.message:
            return

        # Ignore private chats — system targets corporate / partner groups
        if event.is_private:
            return

        # Ignore messages from the agent itself / bots
        if message.out:
            return
        sender = await event.get_sender()
        if sender is not None and getattr(sender, "bot", False):
            return

        text = message.message.strip()
        if not text:
            return

        keyword = await self._match_keyword(text)
        if keyword is None:
            return

        # 1) Work window check (reload agent schedule for live admin updates)
        fresh = await self.db.get_agent(agent.id)
        if fresh is not None:
            runtime.agent = fresh
            agent = fresh

        if not agent.is_within_work_window():
            logger.debug(
                "Agent %s skipped msg %s/%s — outside work window %s-%s",
                agent.id,
                event.chat_id,
                message.id,
                agent.work_window_start,
                agent.work_window_end,
            )
            return

        # 2) De-duplication — atomic claim so only one agent answers
        async with self._claim_lock:
            already = await self.db.is_message_processed(event.chat_id, message.id)
            if already:
                logger.debug(
                    "Message %s/%s already processed — skip",
                    event.chat_id,
                    message.id,
                )
                return

            knowledge = await self._resolve_knowledge(keyword)
            claimed = await self.db.try_claim_message(
                chat_id=event.chat_id,
                message_id=message.id,
                agent_id=agent.id,
                keyword_id=keyword.id,
                knowledge_base_id=knowledge.id if knowledge else None,
            )
            if not claimed:
                logger.debug(
                    "Lost race for message %s/%s",
                    event.chat_id,
                    message.id,
                )
                return

        if knowledge is None:
            logger.warning(
                "Keyword '%s' matched but no knowledge entry found",
                keyword.keyword,
            )
            return

        reply_text = render_template(
            knowledge.response_template,
            context={
                "keyword": keyword.keyword,
                "category": keyword.category,
                "agent": agent.display_name or agent.phone,
            },
        )
        if not reply_text:
            logger.warning("Empty rendered template for knowledge id=%s", knowledge.id)
            return

        await self._send_humanized_reply(runtime, event.chat_id, reply_text)
        logger.info(
            "Agent %s replied in chat=%s msg=%s keyword='%s' kb=%s",
            agent.id,
            event.chat_id,
            message.id,
            keyword.keyword,
            knowledge.id,
        )

    async def _match_keyword(self, text: str) -> Optional[Keyword]:
        """Case-insensitive substring match against cached keywords."""
        lowered = text.lower()
        async with self._keywords_lock:
            keywords = list(self._keywords_cache)

        for kw in keywords:
            if kw.keyword.lower() in lowered:
                return kw
        return None

    async def _resolve_knowledge(self, keyword: Keyword) -> Optional[KnowledgeEntry]:
        """Pick a knowledge-base entry linked to the keyword or its category."""
        if keyword.knowledge_base_id is not None:
            entry = await self.db.get_knowledge(keyword.knowledge_base_id)
            if entry is not None:
                return entry

        by_category = await self.db.get_knowledge_by_category(keyword.category)
        if by_category:
            return random.choice(by_category)

        # Fallback: any active entry
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
        """Send typing action for a random human-like delay, then the reply."""
        client = runtime.client
        if client is None or not client.is_connected():
            raise ConnectionError("Client is not connected")

        delay = random.uniform(
            self.settings.typing_delay_min,
            self.settings.typing_delay_max,
        )
        # Cap typing bursts so very long delays still feel natural
        typing_chunk = min(delay, 4.5)
        elapsed = 0.0
        while elapsed < delay:
            try:
                await client(
                    functions.messages.SetTypingRequest(
                        peer=chat_id,
                        action=SendMessageTypingAction(),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # Fallback for older Telethon API surface
                logger.debug("SetTypingRequest failed (%s), using send_chat_action", exc)
                try:
                    await client.send_chat_action(chat_id, "typing")
                except Exception:  # noqa: BLE001
                    logger.debug("send_chat_action also failed", exc_info=True)

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
        """Mark agent inactive, notify admin, disconnect — do not re-raise."""
        agent = runtime.agent
        reason = f"{type(exc).__name__}: {exc}"
        logger.error(
            "Fatal failure for agent %s (%s): %s — deactivating",
            agent.id,
            agent.phone,
            reason,
        )
        try:
            await self.db.update_agent_status(agent.id, "inactive", last_error=reason)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to update status for agent %s", agent.id)

        await self._disconnect_client(runtime)
        await self._notify_admin(agent, reason)

    async def _notify_admin(self, agent: Agent, reason: str) -> None:
        if self.admin_notifier is None:
            logger.warning(
                "Admin notifier not configured. Agent %s inactive: %s",
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
        """Return a lightweight status view for the admin panel."""
        now = datetime.now()
        snapshot = []
        for agent_id, runtime in self._runtimes.items():
            agent = runtime.agent
            snapshot.append(
                {
                    "agent_id": agent_id,
                    "phone": agent.phone,
                    "status_db": agent.status,
                    "connected": bool(
                        runtime.client is not None and runtime.client.is_connected()
                    ),
                    "within_work_window": agent.is_within_work_window(now),
                    "work_window": f"{agent.work_window_start}-{agent.work_window_end}",
                    "reconnect_attempt": runtime.reconnect_backoff.attempt,
                    "task_done": runtime.task.done() if runtime.task else True,
                }
            )
        return snapshot
