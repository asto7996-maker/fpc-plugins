"""
Draft Engine orchestrator.

Flow:
1. Incoming group message matched by trigger regex
2. Gemini generates a draft reply
3. PendingDraft stored; card sent to operator bot
4. Operator Send / Edit / Reject / Auto-mode — or auto_approve if enabled
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import TYPE_CHECKING, Optional, Sequence

from sqlalchemy import select

from tg_pool.db.models import Account, AccountStatus, AutoReplySettings, DraftStatus, PendingDraft
from tg_pool.db.session import session_scope
from tg_pool.services.draft_service import (
    DedupeStore,
    DraftService,
    generate_and_store_draft,
    match_trigger,
    send_draft_via_userbot,
)

if TYPE_CHECKING:
    from aiogram import Bot
    from redis.asyncio import Redis

    from tg_pool.config import Settings
    from tg_pool.services.alerts import AlertService

logger = logging.getLogger(__name__)


def draft_card_text(draft: PendingDraft, *, auto_on: bool) -> str:
    chat = escape(draft.chat_title or str(draft.chat_id))
    source = escape((draft.source_text or "")[:400])
    body = escape(draft.draft_text or "")
    trigger = escape(draft.matched_trigger or "—")
    author = (
        f"@{escape(draft.source_username)}"
        if draft.source_username
        else f"<code>{draft.source_user_id or '—'}</code>"
    )
    auto_label = "ON" if auto_on else "OFF"
    return (
        "📝 <b>Черновик ответа</b>\n\n"
        f"Чат: <b>{chat}</b> (<code>{draft.chat_id}</code>)\n"
        f"Аккаунт: <code>#{draft.account_id}</code>\n"
        f"Автор: {author}\n"
        f"Триггер: <code>{trigger}</code>\n"
        f"Авто-режим: <b>{auto_label}</b>\n\n"
        f"<b>Исходное сообщение</b>\n"
        f"<blockquote>{source}</blockquote>\n\n"
        f"<b>Вариант Gemini</b>\n"
        f"<blockquote>{body}</blockquote>"
    )


class DraftEngine:
    """Coordinates monitoring → Gemini → operator card → optional send."""

    def __init__(
        self,
        settings: "Settings",
        redis: "Redis",
        *,
        alert_service: Optional["AlertService"] = None,
        bot: Optional["Bot"] = None,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.alerts = alert_service
        self.bot = bot
        self.dedupe = DedupeStore(redis)
        self._send_lock = asyncio.Lock()

    def bind_bot(self, bot: "Bot") -> None:
        self.bot = bot

    async def handle_incoming_message(
        self,
        *,
        account_id: int,
        chat_id: int,
        chat_title: Optional[str],
        message_id: int,
        sender_id: int,
        sender_username: Optional[str],
        text: str,
    ) -> Optional[PendingDraft]:
        """Process one inbound message; return created draft or None if skipped."""
        async with session_scope() as session:
            svc = DraftService(session)
            settings = await svc.get_settings()
            if not settings.enabled:
                return None

            account = await session.get(Account, account_id)
            if account is None or not account.assistant_enabled:
                return None
            if account.status != AccountStatus.active:
                return None

            matched = match_trigger(text, settings.trigger_regex)
            if not matched:
                return None

            if await self.dedupe.already_seen(
                chat_id=chat_id,
                user_id=sender_id or None,
                message_id=message_id,
                ttl_hours=int(settings.dedupe_ttl_hours),
            ):
                logger.debug("Dedupe skip chat=%s msg=%s", chat_id, message_id)
                return None

            sent_today = await svc.count_sent_today(chat_id)
            if sent_today >= int(settings.max_replies_per_chat_day):
                logger.info(
                    "Chat %s hit daily reply limit (%s)",
                    chat_id,
                    settings.max_replies_per_chat_day,
                )
                return None

            try:
                draft = await generate_and_store_draft(
                    session,
                    account=account,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    source_message_id=message_id,
                    source_user_id=sender_id or None,
                    source_username=sender_username,
                    source_text=text,
                    matched_trigger=matched,
                    settings=settings,
                    env_gemini_key=self.settings.gemini_api_key,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Draft generation failed: %s", exc)
                if self.alerts is not None:
                    await self.alerts.send(
                        account_id,
                        account.phone_number,
                        "warning",
                        f"Gemini draft failed: {exc}",
                    )
                return None

            draft_id = draft.id
            auto = bool(settings.auto_approve_enabled)

        if auto:
            logger.info("Auto-approve draft #%s", draft_id)
            asyncio.create_task(
                self._safe_auto_send(draft_id),
                name=f"auto-send-draft-{draft_id}",
            )
            return await self._reload_draft(draft_id)

        await self.notify_operators(draft_id)
        return await self._reload_draft(draft_id)

    async def _reload_draft(self, draft_id: int) -> Optional[PendingDraft]:
        async with session_scope() as session:
            return await DraftService(session).get_draft(draft_id)

    async def notify_operators(self, draft_id: int) -> None:
        """Send / refresh approval card to creator + ADMIN_IDS."""
        if self.bot is None:
            logger.warning("No bot bound — cannot notify operators about draft #%s", draft_id)
            return

        from tg_pool.admin.keyboards import draft_card_kb

        async with session_scope() as session:
            svc = DraftService(session)
            draft = await svc.get_draft(draft_id)
            settings = await svc.get_settings()
            if draft is None or draft.status != DraftStatus.pending:
                return
            text = draft_card_text(draft, auto_on=bool(settings.auto_approve_enabled))
            kb = draft_card_kb(
                draft.id,
                auto_on=bool(settings.auto_approve_enabled),
            )
            targets = self._operator_targets()
            if not targets:
                logger.warning("No operator targets for draft card #%s", draft_id)
                return

            # Prefer updating existing card if we already have admin_chat/message
            if draft.admin_chat_id and draft.admin_message_id:
                try:
                    await self.bot.edit_message_text(
                        text,
                        chat_id=draft.admin_chat_id,
                        message_id=draft.admin_message_id,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    return
                except Exception:  # noqa: BLE001
                    logger.debug("edit draft card failed — send new", exc_info=True)

            last_chat = None
            last_mid = None
            for tg_id in targets:
                try:
                    msg = await self.bot.send_message(
                        tg_id,
                        text,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                    last_chat = tg_id
                    last_mid = msg.message_id
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to send draft card to %s", tg_id)

            if last_chat is not None and last_mid is not None:
                await svc.set_draft_status(
                    draft_id,
                    DraftStatus.pending,
                    admin_chat_id=last_chat,
                    admin_message_id=last_mid,
                )

    def _operator_targets(self) -> Sequence[int]:
        ids: list[int] = []
        if self.settings.creator_id:
            ids.append(int(self.settings.creator_id))
        for aid in self.settings.admin_ids:
            if aid not in ids:
                ids.append(int(aid))
        return ids

    async def approve_and_send(self, draft_id: int, operator_tg_id: int) -> PendingDraft:
        """Mark approved and send via userbot with delay + typing."""
        async with self._send_lock:
            async with session_scope() as session:
                svc = DraftService(session)
                draft = await svc.get_draft(draft_id)
                if draft is None:
                    raise ValueError("Черновик не найден")
                if draft.status not in {DraftStatus.pending, DraftStatus.approved}:
                    raise ValueError(f"Черновик уже в статусе {draft.status.value}")
                settings = await svc.get_settings()
                await svc.set_draft_status(
                    draft_id,
                    DraftStatus.approved,
                    reviewed_by=operator_tg_id,
                )
                draft = await svc.get_draft(draft_id)
                assert draft is not None
                await send_draft_via_userbot(
                    session,
                    draft,
                    settings,
                    alerts=self.alerts,
                    reviewed_by=operator_tg_id,
                )
                sent = await svc.get_draft(draft_id)
                assert sent is not None
                return sent

    async def reject(self, draft_id: int, operator_tg_id: int) -> Optional[PendingDraft]:
        async with session_scope() as session:
            return await DraftService(session).set_draft_status(
                draft_id,
                DraftStatus.rejected,
                reviewed_by=operator_tg_id,
            )

    async def update_draft_text(
        self,
        draft_id: int,
        new_text: str,
        operator_tg_id: int,
    ) -> Optional[PendingDraft]:
        text = (new_text or "").strip()
        if not text:
            raise ValueError("Пустой текст")
        async with session_scope() as session:
            draft = await DraftService(session).set_draft_status(
                draft_id,
                DraftStatus.pending,
                reviewed_by=operator_tg_id,
                draft_text=text[:500],
            )
        if draft is not None:
            await self.notify_operators(draft_id)
        return draft

    async def set_auto_approve(self, enabled: bool) -> AutoReplySettings:
        async with session_scope() as session:
            return await DraftService(session).update_settings(auto_approve_enabled=enabled)

    async def _safe_auto_send(self, draft_id: int) -> None:
        try:
            await self.approve_and_send(draft_id, operator_tg_id=0)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Auto-send failed for draft #%s: %s", draft_id, exc)
            try:
                if self.bot is not None:
                    for tg_id in self._operator_targets():
                        await self.bot.send_message(
                            tg_id,
                            f"⚠️ Авто-отправка черновика #{draft_id} не удалась:\n<code>{escape(str(exc))}</code>",
                            parse_mode="HTML",
                        )
            except Exception:  # noqa: BLE001
                logger.debug("auto-send failure notify failed", exc_info=True)


async def list_assistant_accounts() -> list[Account]:
    async with session_scope() as session:
        result = await session.execute(
            select(Account).where(
                Account.assistant_enabled.is_(True),
                Account.status == AccountStatus.active,
            )
        )
        return list(result.scalars().all())
