"""
Task router — picks a healthy account, executes work, rotates on failure.

Rotation rules
--------------
1. Prefer accounts with the lowest `total_actions_today`
2. Skip flood_wait / banned / paused / spambot / daily-limit exhausted
3. On FloodWait → persist status, re-enqueue task for another account
4. On fatal ban → persist, alert (via SessionWrapper), rotate
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from telethon.errors import FloodWaitError

from tg_pool.clients.session_wrapper import FatalSessionError, SessionWrapper
from tg_pool.clients.spambot import check_spambot
from tg_pool.config import Settings, get_settings
from tg_pool.db.session import session_scope
from tg_pool.taskqueue.broker import PoolTask, RedisTaskBroker
from tg_pool.services.account_service import AccountService
from tg_pool.services.alerts import AlertService

logger = logging.getLogger(__name__)

# Custom action: async (SessionWrapper, payload) -> Any
ActionFn = Callable[[SessionWrapper, dict[str, Any]], Awaitable[Any]]


class TaskRouter:
    def __init__(
        self,
        broker: RedisTaskBroker,
        alerts: AlertService,
        *,
        settings: Optional[Settings] = None,
    ) -> None:
        self.broker = broker
        self.alerts = alerts
        self.settings = settings or get_settings()
        self._actions: dict[str, ActionFn] = {}

        # Built-in kinds
        self.register_action("spambot_check", self._action_spambot_check)
        self.register_action("ping_me", self._action_ping_me)

        broker.register("spambot_check", self.handle_task)
        broker.register("ping_me", self.handle_task)
        broker.register("custom", self.handle_task)

    def register_action(self, kind: str, fn: ActionFn) -> None:
        self._actions[kind] = fn
        # Ensure broker routes this kind to handle_task
        self.broker.register(kind, self.handle_task)

    async def handle_task(self, task: PoolTask) -> None:
        """
        Entry point for the Redis worker.

        Selects an account (or uses pinned account_id), runs the action,
        increments daily counter, and on recoverable failure re-queues
        without the failed account.
        """
        exclude_ids: set[int] = set(task.payload.get("_exclude_account_ids", []))
        action = self._actions.get(task.kind)
        if action is None and task.kind != "custom":
            # custom actions must be registered explicitly
            raise RuntimeError(f"Unknown action kind: {task.kind}")
        if task.kind == "custom":
            action_name = task.payload.get("action")
            action = self._actions.get(action_name or "")
            if action is None:
                raise RuntimeError(f"Unknown custom action: {action_name}")

        assert action is not None

        async with session_scope() as session:
            svc = AccountService(session)
            account = None

            if task.account_id and task.account_id not in exclude_ids:
                account = await svc.get_account(task.account_id)

            if account is None:
                available = await svc.list_available_accounts(
                    self.settings.daily_action_limit
                )
                available = [a for a in available if a.id not in exclude_ids]
                if not available:
                    # Nobody free — delay and retry later
                    logger.warning(
                        "No available accounts for task %s — delay 60s",
                        task.task_id,
                    )
                    await self.broker.enqueue_delayed(task, 60)
                    return
                account = available[0]

            wrapper = SessionWrapper(
                account,
                proxy=account.proxy,
                settings=self.settings,
                on_alert=self.alerts.as_callback,
            )

            try:
                await action(wrapper, task.payload)
                await svc.increment_actions(account.id)
                # Persist any status mutations from wrapper (usually none on success)
                await svc.persist_runtime_state(wrapper.account)
            except FloodWaitError:
                # SessionWrapper already mutated account fields
                await svc.persist_runtime_state(wrapper.account)
                exclude_ids.add(account.id)
                task.payload["_exclude_account_ids"] = list(exclude_ids)
                task.account_id = None  # force rotation
                # Delay a bit, then another account picks it up
                await self.broker.enqueue_delayed(task, delay_sec=5)
                logger.info(
                    "Task %s rotated away from account #%s (FloodWait)",
                    task.task_id,
                    account.id,
                )
            except FatalSessionError:
                await svc.persist_runtime_state(wrapper.account)
                exclude_ids.add(account.id)
                task.payload["_exclude_account_ids"] = list(exclude_ids)
                task.account_id = None
                await self.broker.enqueue_delayed(task, delay_sec=2)
            finally:
                await wrapper.disconnect()

    # ------------------------------------------------------------------ built-in actions

    async def _action_spambot_check(
        self,
        wrapper: SessionWrapper,
        payload: dict[str, Any],
    ) -> None:
        # Status flags are mutated on wrapper.account; TaskRouter persists them.
        report = await check_spambot(wrapper, settings=self.settings)
        if report.restricted:
            await self.alerts.send(
                wrapper.account.id,
                wrapper.account.phone_number,
                "critical",
                f"SpamBot RESTRICTED\n{report.raw_text[:400]}",
            )

    async def _action_ping_me(
        self,
        wrapper: SessionWrapper,
        payload: dict[str, Any],
    ) -> None:
        """Sanity action: fetch own identity (counts as 1 daily action)."""

        async def _call(client):
            me = await client.get_me()
            wrapper.account.telegram_user_id = me.id
            wrapper.account.display_name = getattr(me, "username", None) or str(me.id)
            return me

        await wrapper.execute(_call)
