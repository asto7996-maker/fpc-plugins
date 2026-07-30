"""
Draft engine: settings CRUD, pending drafts, chat rate limits, send pipeline.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tg_pool.clients.gemini_client import DEFAULT_MODEL, GeminiDraftClient
from tg_pool.clients.session_wrapper import SessionWrapper
from tg_pool.db.models import (
    Account,
    AccountStatus,
    AutoReplySettings,
    DraftStatus,
    PendingDraft,
)
from tg_pool.services.account_service import AccountService
from tg_pool.services.alerts import AlertService

logger = logging.getLogger(__name__)


class DraftService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ settings
    async def get_settings(self) -> AutoReplySettings:
        row = await self.session.get(AutoReplySettings, 1)
        if row is None:
            row = AutoReplySettings(id=1)
            self.session.add(row)
            await self.session.flush()
        return row

    async def update_settings(self, **fields) -> AutoReplySettings:
        settings = await self.get_settings()
        for key, value in fields.items():
            if hasattr(settings, key) and value is not None:
                setattr(settings, key, value)
        await self.session.flush()
        return settings

    # ------------------------------------------------------------------ drafts
    async def create_draft(
        self,
        *,
        account_id: int,
        chat_id: int,
        chat_title: Optional[str],
        source_message_id: int,
        source_user_id: Optional[int],
        source_username: Optional[str],
        source_text: str,
        matched_trigger: Optional[str],
        draft_text: str,
    ) -> PendingDraft:
        draft = PendingDraft(
            account_id=account_id,
            chat_id=chat_id,
            chat_title=chat_title,
            source_message_id=source_message_id,
            source_user_id=source_user_id,
            source_username=source_username,
            source_text=source_text[:2000],
            matched_trigger=matched_trigger,
            draft_text=draft_text,
            status=DraftStatus.pending,
        )
        self.session.add(draft)
        await self.session.flush()
        return draft

    async def get_draft(self, draft_id: int) -> Optional[PendingDraft]:
        return await self.session.get(PendingDraft, draft_id)

    async def list_pending(self, limit: int = 20) -> Sequence[PendingDraft]:
        result = await self.session.execute(
            select(PendingDraft)
            .where(PendingDraft.status == DraftStatus.pending)
            .order_by(PendingDraft.id.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def set_draft_status(
        self,
        draft_id: int,
        status: DraftStatus,
        *,
        reviewed_by: Optional[int] = None,
        error: Optional[str] = None,
        draft_text: Optional[str] = None,
        admin_message_id: Optional[int] = None,
        admin_chat_id: Optional[int] = None,
    ) -> Optional[PendingDraft]:
        draft = await self.get_draft(draft_id)
        if draft is None:
            return None
        draft.status = status
        if reviewed_by is not None:
            draft.reviewed_by = reviewed_by
        if error is not None:
            draft.error = error
        if draft_text is not None:
            draft.draft_text = draft_text
        if admin_message_id is not None:
            draft.admin_message_id = admin_message_id
        if admin_chat_id is not None:
            draft.admin_chat_id = admin_chat_id
        if status == DraftStatus.sent:
            draft.sent_at = datetime.now(timezone.utc)
        await self.session.flush()
        return draft

    async def count_sent_today(self, chat_id: int) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = await self.session.execute(
            select(func.count())
            .select_from(PendingDraft)
            .where(PendingDraft.chat_id == chat_id)
            .where(PendingDraft.status == DraftStatus.sent)
            .where(PendingDraft.sent_at >= datetime.fromisoformat(f"{day}T00:00:00+00:00"))
        )
        return int(result.scalar_one() or 0)

    async def set_account_assistant(self, account_id: int, enabled: bool) -> None:
        await self.session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(assistant_enabled=enabled)
        )


class DedupeStore:
    """Redis-backed TTL dedupe for author/thread windows."""

    def __init__(self, redis) -> None:
        self.redis = redis

    async def already_seen(
        self,
        *,
        chat_id: int,
        user_id: Optional[int],
        message_id: int,
        ttl_hours: int,
    ) -> bool:
        ttl = max(60, int(ttl_hours * 3600))
        keys = [
            f"tg_pool:draft:msg:{chat_id}:{message_id}",
        ]
        if user_id is not None:
            keys.append(f"tg_pool:draft:user:{chat_id}:{user_id}")

        # If any key exists — skip
        for key in keys:
            if await self.redis.exists(key):
                return True

        pipe = self.redis.pipeline()
        for key in keys:
            pipe.set(key, b"1", ex=ttl)
        await pipe.execute()
        return False


_TRIGGER_CACHE: dict[str, re.Pattern[str]] = {}


def match_trigger(text: str, pattern: str) -> Optional[str]:
    if not text or not pattern:
        return None
    compiled = _TRIGGER_CACHE.get(pattern)
    if compiled is None:
        try:
            compiled = re.compile(pattern, re.IGNORECASE | re.UNICODE)
        except re.error:
            compiled = re.compile(
                r"(?i)\b(vpn|впн|прокси|proxy|доступ)\b",
                re.IGNORECASE,
            )
        _TRIGGER_CACHE[pattern] = compiled
    m = compiled.search(text)
    if not m:
        return None
    return m.group(0)


async def generate_and_store_draft(
    session: AsyncSession,
    *,
    account: Account,
    chat_id: int,
    chat_title: Optional[str],
    source_message_id: int,
    source_user_id: Optional[int],
    source_username: Optional[str],
    source_text: str,
    matched_trigger: str,
    settings: AutoReplySettings,
    env_gemini_key: str = "",
) -> PendingDraft:
    api_key = (settings.gemini_api_key or env_gemini_key or "").strip()
    if not api_key:
        raise RuntimeError("Gemini API key is not configured")

    client = GeminiDraftClient(
        api_key,
        model=settings.gemini_model or DEFAULT_MODEL,
        promote_username=settings.promote_username,
    )
    from tg_pool.core.humanize import humanize_text

    draft_text = await client.generate_draft(
        source_text=source_text,
        chat_title=chat_title,
        matched_trigger=matched_trigger,
    )
    draft_text = await humanize_text(draft_text)
    svc = DraftService(session)
    return await svc.create_draft(
        account_id=account.id,
        chat_id=chat_id,
        chat_title=chat_title,
        source_message_id=source_message_id,
        source_user_id=source_user_id,
        source_username=source_username,
        source_text=source_text,
        matched_trigger=matched_trigger,
        draft_text=draft_text,
    )


async def send_draft_via_userbot(
    session: AsyncSession,
    draft: PendingDraft,
    settings: AutoReplySettings,
    *,
    alerts: Optional[AlertService] = None,
    reviewed_by: Optional[int] = None,
) -> None:
    """
    Human-emulation send: read latency → typing (refreshed) → pause → message.

    Text is passed through ``humanize_text`` (lowercase start, zero emoji).
    """
    from tg_pool.core.humanize import BehavioralEmulationEngine, humanize_text

    account = await session.get(Account, draft.account_id)
    if account is None or not account.session_string:
        raise RuntimeError("Account missing for draft send")
    if account.status not in {AccountStatus.active, AccountStatus.paused}:
        # allow paused for explicit operator send
        if account.status in {AccountStatus.banned, AccountStatus.flood_wait}:
            raise RuntimeError(f"Account status blocks send: {account.status.value}")

    # Refresh relationship
    svc = AccountService(session)
    account = await svc.get_account(account.id)
    assert account is not None

    # Spec: T_read ∈ [15, 45], T_pause ∈ [1.5, 3.5], typing ∝ len(text)
    engine = BehavioralEmulationEngine(
        read_min=15.0,
        read_max=45.0,
        pause_min=1.5,
        pause_max=3.5,
    )
    final_text = await humanize_text(draft.draft_text)
    if not final_text:
        raise RuntimeError("Draft text empty after humanize")

    wrapper = SessionWrapper(account, proxy=account.proxy)
    try:
        client = await wrapper.connect()
        final_text = await engine.run_before_send(
            client,
            draft.chat_id,
            final_text,
        )
        draft.draft_text = final_text

        await client.send_message(
            draft.chat_id,
            final_text,
            reply_to=draft.source_message_id,
        )
        draft_svc = DraftService(session)
        await draft_svc.set_draft_status(
            draft.id,
            DraftStatus.sent,
            reviewed_by=reviewed_by,
            draft_text=final_text,
        )
        await svc.increment_actions(account.id)

        if alerts is not None:
            await alerts.send(
                account.id,
                account.phone_number,
                "info",
                (
                    f"[Аккаунт #{account.id}] Ответил в чате "
                    f"«{draft.chat_title or draft.chat_id}»:\n"
                    f"«{final_text}»"
                ),
            )
    except Exception as exc:  # noqa: BLE001
        await DraftService(session).set_draft_status(
            draft.id,
            DraftStatus.failed,
            reviewed_by=reviewed_by,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        await wrapper.disconnect()
