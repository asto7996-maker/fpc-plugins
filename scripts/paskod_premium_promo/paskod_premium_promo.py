"""Limited Paskod premium promo: 1000 activations, 10 GB whitelist traffic cap."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import create_trial_subscription
from app.database.models import SubscriptionStatus, User
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

CALLBACK_DATA = 'paskod_promo_premium'
STATE_PATH = Path('/app/data/paskod_premium_promo.json')
MAX_CLAIMS = 1000
INITIAL_CLAIMED = 124
PROMO_DAYS = 3
PROMO_TRAFFIC_GB = 10  # white-list / bypass traffic cap per promo user
PREMIUM_TARIFF_ID = 2
PREMIUM_SQUAD_UUID = 'ca641cad-1797-43f0-8005-76d33c7f1046'

_lock = threading.Lock()

PROMO_MESSAGE_HTML = (
    '🔥 <b>Забудь про блокировки — Paskod VPN летает ВСЕГДА!</b>\n'
    '\n'
    'Пока другие сервисы падают, мы выдаем мощные 10 Гбит/с и намертво пробиваем '
    'любые «белые списки» и глушилки.\n'
    '\n'
    '<blockquote>📣 <b>Что ты получаешь:</b>\n'
    '└ 📈 Скорость до 10 Гбит/с — 4K-видео и игры без лагов\n'
    '└ 🔵 Zero-Logs & Анонимность — не храним данные и логи\n'
    '└ 🟢 Обход глушилок — связь работает даже при жестких блоках</blockquote>\n'
    '\n'
    '🎁 <b>ДАРИМ 3 ДНЯ ПРЕМИУМА БЕСПЛАТНО!</b>\n'
    'Без привязки карт, без вопросов и сложных настроек.\n'
    '\n'
    '✔️ Проверь скорость сам в один клик:'
)


def _default_state() -> dict:
    return {
        'max_claims': MAX_CLAIMS,
        'claimed': INITIAL_CLAIMED,
        'traffic_limit_gb': PROMO_TRAFFIC_GB,
        'duration_days': PROMO_DAYS,
        'claims': {},  # telegram_id(str) -> {claimed_at, subscription_id}
    }


def load_state() -> dict:
    with _lock:
        if not STATE_PATH.exists():
            state = _default_state()
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            return state
        try:
            state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        except Exception:
            state = _default_state()
        state.setdefault('max_claims', MAX_CLAIMS)
        state.setdefault('claimed', INITIAL_CLAIMED)
        state.setdefault('traffic_limit_gb', PROMO_TRAFFIC_GB)
        state.setdefault('duration_days', PROMO_DAYS)
        state.setdefault('claims', {})
        # Never allow claimed to drift below INITIAL_CLAIMED baseline from seed.
        if int(state.get('claimed') or 0) < INITIAL_CLAIMED and not state['claims']:
            state['claimed'] = INITIAL_CLAIMED
        return state


def save_state(state: dict) -> None:
    with _lock:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(STATE_PATH)


def button_text(claimed: int | None = None, max_claims: int | None = None) -> str:
    state = load_state() if claimed is None or max_claims is None else None
    c = int(claimed if claimed is not None else state['claimed'])
    m = int(max_claims if max_claims is not None else state['max_claims'])
    return f'Активировать премиум [{c}/{m}]'


def build_keyboard(claimed: int | None = None, max_claims: int | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_text(claimed, max_claims),
                    callback_data=CALLBACK_DATA,
                    style='success',
                )
            ]
        ]
    )


def _has_active_paid(user: User) -> bool:
    now = datetime.now(UTC)
    for sub in getattr(user, 'subscriptions', None) or []:
        if not sub:
            continue
        if getattr(sub, 'is_trial', False):
            continue
        status = getattr(sub, 'status', None)
        if status not in (
            SubscriptionStatus.ACTIVE.value,
            SubscriptionStatus.TRIAL.value,
            'limited',
        ):
            # also allow plain 'active'
            if status != 'active':
                continue
        end = getattr(sub, 'end_date', None)
        if end is None or end > now:
            return True
    # fallback single subscription attr
    sub = getattr(user, 'subscription', None)
    if sub and not getattr(sub, 'is_trial', False):
        end = getattr(sub, 'end_date', None)
        status = getattr(sub, 'status', None)
        if status in (SubscriptionStatus.ACTIVE.value, 'active', 'limited') and (end is None or end > now):
            return True
    return False


async def handle_promo_activate(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer('Сообщение устарело. Напишите /start.', show_alert=True)
        return

    if db_user is None:
        await callback.answer('Сначала нажмите /start в боте.', show_alert=True)
        return

    await db.refresh(db_user, ['subscriptions'])

    state = load_state()
    tg_key = str(callback.from_user.id)
    claimed = int(state.get('claimed') or 0)
    max_claims = int(state.get('max_claims') or MAX_CLAIMS)

    if tg_key in state.get('claims', {}):
        await callback.answer('Вы уже активировали этот премиум.', show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=build_keyboard(claimed, max_claims))
        except Exception:
            pass
        return

    if claimed >= max_claims:
        await callback.answer('Лимит 1000 активаций исчерпан.', show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=build_keyboard(claimed, max_claims))
        except Exception:
            pass
        return

    if _has_active_paid(db_user):
        await callback.answer('У вас уже активен премиум — слот не расходуем.', show_alert=True)
        return

    # Create limited premium trial: 3 days, 10 GB (white-list traffic cap), prem squad.
    try:
        subscription = await create_trial_subscription(
            db,
            db_user.id,
            duration_days=int(state.get('duration_days') or PROMO_DAYS),
            traffic_limit_gb=int(state.get('traffic_limit_gb') or PROMO_TRAFFIC_GB),
            device_limit=5,
            connected_squads=[PREMIUM_SQUAD_UUID],
            tariff_id=PREMIUM_TARIFF_ID,
        )
        # Force traffic cap even if create_trial returned an existing live sub.
        if (subscription.traffic_limit_gb or 0) != PROMO_TRAFFIC_GB and getattr(subscription, 'is_trial', False):
            subscription.traffic_limit_gb = PROMO_TRAFFIC_GB
            await db.commit()
            await db.refresh(subscription)

        # If create_trial returned an existing paid/live unlimited sub, do not claim slot.
        if not getattr(subscription, 'is_trial', False):
            await callback.answer('У вас уже есть активная подписка.', show_alert=True)
            return

        subscription_service = SubscriptionService()
        if subscription_service.is_configured:
            try:
                if getattr(subscription, 'remnawave_uuid', None) or getattr(db_user, 'remnawave_uuid', None):
                    await subscription_service.update_remnawave_user(db, subscription)
                else:
                    await subscription_service.create_remnawave_user(db, subscription)
                await db.refresh(subscription)
            except Exception as sync_err:
                logger.error('Promo remnawave sync failed', error=sync_err, user_id=db_user.id)

        state = load_state()
        if tg_key in state.get('claims', {}):
            await callback.answer('Вы уже активировали этот премиум.', show_alert=True)
            return
        if int(state.get('claimed') or 0) >= int(state.get('max_claims') or MAX_CLAIMS):
            await callback.answer('Лимит 1000 активаций исчерпан.', show_alert=True)
            return

        state['claimed'] = int(state.get('claimed') or 0) + 1
        state.setdefault('claims', {})[tg_key] = {
            'claimed_at': datetime.now(UTC).isoformat(),
            'subscription_id': subscription.id,
            'traffic_limit_gb': PROMO_TRAFFIC_GB,
            'username': getattr(callback.from_user, 'username', None),
        }
        save_state(state)
        claimed = int(state['claimed'])
        max_claims = int(state['max_claims'])

        link = getattr(subscription, 'subscription_url', None) or ''
        text = (
            '✅ <b>Премиум активирован!</b>\n'
            f'📅 {PROMO_DAYS} дня · 📊 лимит белых списков {PROMO_TRAFFIC_GB} ГБ\n'
            f'🎟 Слот: <b>{claimed}/{max_claims}</b>\n'
        )
        if link:
            text += f'\n🔗 Подписка: <code>{link}</code>'

        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=build_keyboard(claimed, max_claims))
        await callback.answer('Готово! Премиум на 3 дня.', show_alert=True)
        logger.info(
            'Paskod promo premium activated',
            telegram_id=callback.from_user.id,
            claimed=claimed,
            subscription_id=subscription.id,
        )
    except Exception as exc:
        logger.exception('Paskod promo activation failed', error=exc, telegram_id=callback.from_user.id)
        await callback.answer('Не удалось активировать. Попробуйте позже.', show_alert=True)


def register_handlers(dp: Dispatcher) -> None:
    # Ensure state file exists with seeded counter.
    load_state()
    dp.callback_query.register(handle_promo_activate, F.data == CALLBACK_DATA)
    logger.info('Paskod premium promo handlers registered', max_claims=MAX_CLAIMS, traffic_gb=PROMO_TRAFFIC_GB)
