from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
#  Review Bonus — FunPay Cardinal plugin
#  Автовыдача бонуса (текста) покупателю ТОЛЬКО после реального отзыва к заказу.
#
#  Триггер — исключительно системное событие отзыва FunPay (MessageTypes.NEW_FEEDBACK),
#  привязанное к конкретному заказу. Обычный текст в чате ("ppp оставил отзыв к
#  заказу XXX") бонус НЕ выдаёт: такое сообщение имеет другой тип и не связано
#  с order.review, поэтому отбрасывается.
#
#  Автор: @xei1y
# ──────────────────────────────────────────────────────────────────────────────

import html
import json
import logging
import os
import threading
import time
import traceback
from typing import Any, Final

from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI.types import MessageTypes, Order
from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent
from cardinal import Cardinal
from tg_bot import CBT
from telebot.types import CallbackQuery, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM, Message


NAME          = "Review Bonus"
VERSION       = "1.0.0"
DESCRIPTION   = "Автовыдача бонуса (текста) покупателю после отзыва к заказу на FunPay 🎁"
CREDITS       = "@xei1y"
UUID          = "b7f3a2e1-6c48-4d92-9a10-5e3b7c1f8d24"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

SETTINGS_FILE = f"storage/plugins/{UUID}/settings.json"
CB_PREFIX     = f"rvb_{UUID[:8]}"
MAX_ISSUED_HISTORY: Final[int] = 2000

DEFAULT_BONUS_TEXT = (
    "Спасибо за отзыв, {buyer}! 🎁\n"
    "Ваш бонус за заказ #{order_id}: <впишите сюда ваш бонус>"
)

logger = logging.getLogger("FPC.ReviewBonus")
_P = "[ReviewBonus]"

_plugin: "Plugin | None" = None


def _escape(val: Any) -> str:
    return html.escape(str(val if val is not None else ""))


# ═════════════════════════════════════════════════════════════════════════════
#  Plugin
# ═════════════════════════════════════════════════════════════════════════════

class Plugin:
    def __init__(self, cardinal: Cardinal) -> None:
        self.cardinal = cardinal
        self._cfg: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._processing: set[str] = set()
        self.reload_settings()

    def log(self, msg: str, *args) -> None:
        logger.info("%s " + msg, _P, *args)

    # ── Settings persistence ──────────────────────────────────────────────────

    def reload_settings(self) -> None:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        defaults = self._default_cfg()
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in defaults.items():
                    loaded.setdefault(k, v)
                self._cfg = loaded
            else:
                self._cfg = defaults
                self._save_settings()
        except Exception as exc:
            logger.error("%s settings load: %s", _P, exc)
            self._cfg = defaults

    def _save_settings(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            tmp = f"{SETTINGS_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, indent=4, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)

    def get_cfg(self, key: str, default: Any = None) -> Any:
        field = self.get_schema_field(key)
        if default is None and field:
            default = field.get("default")
        return self._cfg.get(key, default)

    def set_cfg(self, key: str, value: Any) -> None:
        self._cfg[key] = value
        self._save_settings()

    @staticmethod
    def _default_cfg() -> dict[str, Any]:
        return {
            "enabled": True,
            "bonus_text": DEFAULT_BONUS_TEXT,
            "react_on_changed": False,
            "once_per_order": True,
            "min_stars": 1,
            "delay_seconds": 0,
            "issued_orders": [],
        }

    # ── Settings schema (Telegram UI) ─────────────────────────────────────────

    @staticmethod
    def settings_page_size() -> int:
        return 8

    def get_settings_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "enabled", "label": "Автовыдача бонуса за отзыв", "type": "bool", "default": True},
            {
                "key": "bonus_text",
                "label": "Текст бонуса",
                "type": "multiline",
                "default": DEFAULT_BONUS_TEXT,
                "description": "Плейсхолдеры: {order_id}, {buyer}, {stars}",
            },
            {"key": "min_stars", "label": "Мин. оценка для бонуса (1–5)", "type": "int", "default": 1, "min": 1, "max": 5},
            {"key": "once_per_order", "label": "Один бонус на заказ", "type": "bool", "default": True},
            {"key": "react_on_changed", "label": "Выдавать при изменении отзыва", "type": "bool", "default": False},
            {"key": "delay_seconds", "label": "Задержка перед выдачей (сек)", "type": "int", "default": 0, "min": 0, "max": 600},
            {"key": "preview_bonus", "label": "👁 Предпросмотр текста", "type": "action"},
            {"key": "clear_history", "label": "🗑 Очистить историю выдач", "type": "action"},
        ]

    def get_schema_field(self, key: str) -> dict[str, Any] | None:
        for field in self.get_settings_schema():
            if field.get("key") == key:
                return field
        return None

    def _schema_field_by_index(self, idx: int) -> dict[str, Any] | None:
        schema = self.get_settings_schema()
        if 0 <= idx < len(schema):
            return schema[idx]
        return None

    def _format_setting_line(self, field: dict[str, Any], val: Any) -> str:
        label = _escape(field.get("label", ""))
        ftype = field.get("type", "str")
        if ftype == "bool":
            return f"{'🟢' if val else '🔴'} {label}"
        if ftype == "multiline":
            return f"• <b>{label}</b>: <i>{len(str(val or ''))} симв.</i>"
        if ftype == "int":
            return f"• <b>{label}</b>: <code>{_escape(val)}</code>"
        if ftype == "action":
            return f"▶️ <b>{label}</b>"
        preview = _escape(str(val or "")[:55])
        return f"• <b>{label}</b>: <code>{preview or '—'}</code>"

    def render_settings_text(self, page: int = 0) -> str:
        schema = self.get_settings_schema()
        ps = self.settings_page_size()
        pages = max(1, (len(schema) + ps - 1) // ps)
        page = max(0, min(page, pages - 1))
        chunk = schema[page * ps:(page + 1) * ps]
        lines = [
            f"🎁 <b>{_escape(NAME)}</b> v{VERSION}",
            "━━━━━━━━━━━━━━━━━━",
            f"<i>{_escape(DESCRIPTION)}</i>",
            f"<i>Автор: {_escape(CREDITS)}</i>",
            "",
            "ℹ️ Бонус выдаётся только по реальному отзыву к заказу.",
            "",
        ]
        if pages > 1:
            lines.append(f"📄 Страница <b>{page + 1}</b> / {pages}\n")
        for field in chunk:
            key = field["key"]
            val = "" if field.get("type") == "action" else self.get_cfg(key)
            lines.append(self._format_setting_line(field, val))
        issued = self.get_cfg("issued_orders", [])
        lines.append(f"\n📊 Выдано бонусов: <b>{len(issued)}</b>")
        return "\n".join(lines)

    def build_settings_keyboard(self, page: int = 0) -> IKM:
        schema = self.get_settings_schema()
        ps = self.settings_page_size()
        pages = max(1, (len(schema) + ps - 1) // ps)
        page = max(0, min(page, pages - 1))
        chunk = schema[page * ps:(page + 1) * ps]
        kb = IKM()
        for local_i, field in enumerate(chunk):
            key = field["key"]
            idx = page * ps + local_i
            label = field.get("label", key)
            ftype = field.get("type", "str")
            if ftype == "bool":
                on = bool(self.get_cfg(key))
                kb.add(IKB(f"{'🟢' if on else '🔴'} {label[:42]}", callback_data=f"{CB_PREFIX}:tog:{idx}"))
            elif ftype == "action":
                kb.add(IKB(label[:48], callback_data=f"{CB_PREFIX}:act:{key}"))
            else:
                val = str(self.get_cfg(key, "")).replace("\n", " ")[:16]
                if len(str(self.get_cfg(key, ""))) > 16:
                    val += "…"
                kb.add(IKB(f"✏️ {label[:22]}: {val or '—'}", callback_data=f"{CB_PREFIX}:edit:{idx}"))
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(IKB("◀️", callback_data=f"{CB_PREFIX}:page:{page - 1}"))
            nav.append(IKB(f"{page + 1}/{pages}", callback_data=f"{CB_PREFIX}:noop"))
            if page < pages - 1:
                nav.append(IKB("▶️", callback_data=f"{CB_PREFIX}:page:{page + 1}"))
            kb.row(*nav)
        kb.add(IKB("◀️ К плагину", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
        return kb

    # ── Bonus logic ────────────────────────────────────────────────────────────

    def _fill(self, template: str, order: Order) -> str:
        review = getattr(order, "review", None)
        stars = getattr(review, "stars", None) if review else None
        subs = {
            "{order_id}": str(getattr(order, "id", "") or ""),
            "{buyer}": str(getattr(order, "buyer_username", "") or ""),
            "{stars}": str(stars if stars is not None else ""),
        }
        for k, v in subs.items():
            template = template.replace(k, v)
        return template

    def _already_issued(self, order_id: str) -> bool:
        return order_id in set(self.get_cfg("issued_orders", []))

    def _remember_issued(self, order_id: str) -> None:
        issued = list(self.get_cfg("issued_orders", []))
        if order_id in issued:
            return
        issued.append(order_id)
        self.set_cfg("issued_orders", issued[-MAX_ISSUED_HISTORY:])

    def process_order(self, order: Order, chat_id: Any = None) -> bool:
        """Выдаёт бонус, если к заказу действительно есть отзыв."""
        review = getattr(order, "review", None)
        stars = getattr(review, "stars", None) if review else None
        # Ключевая проверка: отзыв реально существует у заказа.
        if not review or not stars:
            return False

        oid = str(getattr(order, "id", "") or "")
        if not oid:
            return False

        try:
            min_stars = int(self.get_cfg("min_stars", 1))
        except (TypeError, ValueError):
            min_stars = 1
        if int(stars) < min_stars:
            self.log("Отзыв #%s: %s★ < мин. %s★ — бонус пропущен", oid, stars, min_stars)
            return False

        once = bool(self.get_cfg("once_per_order", True))

        with self._lock:
            if oid in self._processing:
                return False
            if once and self._already_issued(oid):
                return False
            self._processing.add(oid)

        try:
            if chat_id is None:
                chat_id = getattr(order, "chat_id", None)
            if not chat_id:
                self.log("Заказ #%s: не удалось определить chat_id", oid)
                return False

            text = str(self.get_cfg("bonus_text", DEFAULT_BONUS_TEXT))
            if not text.strip():
                self.log("Заказ #%s: текст бонуса пуст — выдача пропущена", oid)
                return False
            text = self._fill(text, order)

            try:
                delay = int(self.get_cfg("delay_seconds", 0))
            except (TypeError, ValueError):
                delay = 0
            if delay > 0:
                time.sleep(min(delay, 600))

            buyer = str(getattr(order, "buyer_username", "") or "")
            self.cardinal.send_message(chat_id, text, buyer)
            if once:
                self._remember_issued(oid)
            self.log("Бонус за отзыв выдан по заказу #%s (%s★, покупатель %s)", oid, stars, buyer)
            return True
        except Exception as exc:
            logger.error("%s process #%s: %s", _P, oid, exc)
            logger.debug(traceback.format_exc())
            return False
        finally:
            with self._lock:
                self._processing.discard(oid)

    def _resolve_order(self, message_obj: Any) -> Order | None:
        try:
            order = self.cardinal.get_order_from_object(message_obj)
        except Exception as exc:
            logger.debug("%s get_order_from_object: %s", _P, exc)
            order = None
        if order is not None:
            return order
        try:
            matches = RegularExpressions().ORDER_ID.findall(str(message_obj))
            if not matches:
                return None
            oid = matches[0][1:] if matches[0].startswith("#") else matches[0]
            return self.cardinal.account.get_order(oid)
        except Exception as exc:
            logger.debug("%s resolve order fallback: %s", _P, exc)
            return None

    # ── Event hooks ────────────────────────────────────────────────────────────

    def on_new_message(self, event: NewMessageEvent) -> None:
        if not self.get_cfg("enabled"):
            return
        msg_type = event.message.type
        # Триггер только на системные события отзыва — не на обычный текст.
        if msg_type == MessageTypes.NEW_FEEDBACK:
            pass
        elif msg_type == MessageTypes.FEEDBACK_CHANGED and self.get_cfg("react_on_changed"):
            pass
        else:
            return
        # Отзыв на НАШУ покупку (мы — покупатель) бонусом не поощряем.
        if getattr(event.message, "i_am_buyer", False):
            return

        order = self._resolve_order(event.message)
        if order is None:
            return
        review = getattr(order, "review", None)
        if not review or not getattr(review, "stars", None):
            return
        threading.Thread(
            target=self.process_order,
            args=(order, getattr(event.message, "chat_id", None)),
            daemon=True,
        ).start()

    def on_last_chat(self, event: LastChatMessageChangedEvent) -> None:
        if not getattr(self.cardinal, "old_mode_enabled", False) or not self.get_cfg("enabled"):
            return
        chat = event.chat
        if chat.last_message_type != MessageTypes.NEW_FEEDBACK:
            return
        try:
            if f" {self.cardinal.account.username} " in str(chat):
                return
        except Exception:
            pass
        order = self._resolve_order(chat)
        if order is None:
            return
        review = getattr(order, "review", None)
        if not review or not getattr(review, "stars", None):
            return
        threading.Thread(
            target=self.process_order,
            args=(order, getattr(chat, "id", None)),
            daemon=True,
        ).start()

    # ── Telegram settings actions ──────────────────────────────────────────────

    def on_settings_action(self, call: CallbackQuery, action: str) -> bool:
        bot = self.cardinal.telegram.bot
        chat_id = call.message.chat.id
        if action == "preview_bonus":
            text = str(self.get_cfg("bonus_text", DEFAULT_BONUS_TEXT))
            preview = (
                text.replace("{order_id}", "A1B2C3D4")
                    .replace("{buyer}", "example_buyer")
                    .replace("{stars}", "5")
            )
            bot.answer_callback_query(call.id, "Предпросмотр отправлен")
            bot.send_message(
                chat_id,
                f"👁 <b>Предпросмотр бонуса:</b>\n\n{_escape(preview)}",
                parse_mode="HTML",
            )
            return True
        if action == "clear_history":
            self.set_cfg("issued_orders", [])
            bot.answer_callback_query(call.id, "История выдач очищена", show_alert=True)
            return True
        return False

    # ── Telegram UI (schema-driven) ────────────────────────────────────────────

    def setup_telegram(self) -> None:
        if not self.cardinal.telegram:
            return
        tg = self.cardinal.telegram
        bot = tg.bot
        plugin = self

        def show_settings(chat_id: int, msg_id: int, page: int = 0) -> None:
            text = plugin.render_settings_text(page)
            kb = plugin.build_settings_keyboard(page)
            try:
                bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

        def on_callback(call: CallbackQuery) -> None:
            data = call.data or ""
            if not data.startswith(f"{CB_PREFIX}:"):
                return
            parts = data.split(":")
            action = parts[1]
            chat_id, msg_id = call.message.chat.id, call.message.message_id

            if action == "noop":
                bot.answer_callback_query(call.id)
                return
            if action == "page":
                show_settings(chat_id, msg_id, int(parts[2]))
                bot.answer_callback_query(call.id)
                return
            if action == "tog":
                field = plugin._schema_field_by_index(int(parts[2]))
                if field and field.get("type") == "bool":
                    key = field["key"]
                    plugin.set_cfg(key, not bool(plugin.get_cfg(key)))
                    page = int(parts[2]) // plugin.settings_page_size()
                    show_settings(chat_id, msg_id, page)
                bot.answer_callback_query(call.id)
                return
            if action == "act":
                key = parts[2]
                if plugin.on_settings_action(call, key):
                    return
                bot.answer_callback_query(call.id)
                return
            if action == "edit":
                field = plugin._schema_field_by_index(int(parts[2]))
                if not field:
                    bot.answer_callback_query(call.id)
                    return
                key = field["key"]
                cur = plugin.get_cfg(key, "")
                label = field.get("label", key)
                desc = field.get("description", "")
                hint = f"\n\n<i>{_escape(desc)}</i>" if desc else ""
                prompt = (
                    f"✏️ <b>{_escape(label)}</b>{hint}\n\n"
                    f"Текущее:\n<code>{_escape(str(cur)[:800])}</code>\n\n"
                    f"Введите новое значение.\n/cancel — отмена"
                )
                result = bot.send_message(chat_id, prompt, parse_mode="HTML")
                tg.set_state(chat_id, result.id, call.from_user.id, state=f"{CB_PREFIX}:edit:{key}")
                bot.answer_callback_query(call.id)

        def on_text(message: Message) -> None:
            state_data = tg.get_state(message.chat.id, message.from_user.id)
            if not state_data or "state" not in state_data:
                return
            state = state_data["state"]
            if not str(state).startswith(f"{CB_PREFIX}:edit:"):
                return
            key = str(state).split(":", 2)[-1]
            field = plugin.get_schema_field(key)
            if not field:
                tg.clear_state(message.chat.id, message.from_user.id)
                return
            text = message.text or ""
            if text.strip().lower() in ("/cancel", "отмена"):
                tg.clear_state(message.chat.id, message.from_user.id)
                bot.reply_to(message, "❌ Отменено")
                return
            if field.get("type") == "int":
                try:
                    val = int(text.strip())
                except ValueError:
                    bot.reply_to(message, "⚠️ Введите целое число")
                    return
                min_v = field.get("min", 0)
                max_v = field.get("max", 1000)
                if val < min_v or val > max_v:
                    bot.reply_to(message, f"⚠️ Допустимо: {min_v}–{max_v}")
                    return
                plugin.set_cfg(key, val)
            else:
                plugin.set_cfg(key, text if field.get("type") == "multiline" else text.strip())
            tg.clear_state(message.chat.id, message.from_user.id)
            bot.reply_to(message, f"✅ Сохранено: <b>{_escape(field.get('label', key))}</b>", parse_mode="HTML")

        def on_plugin_settings(call: CallbackQuery) -> None:
            if f"{CBT.PLUGIN_SETTINGS}:{UUID}" not in (call.data or ""):
                if not (call.data or "").startswith(f"{CBT.EDIT_PLUGIN}:{UUID}"):
                    return
            show_settings(call.message.chat.id, call.message.message_id, 0)
            bot.answer_callback_query(call.id)

        def _is_editing(m: Message) -> bool:
            state_data = tg.get_state(m.chat.id, m.from_user.id)
            if not state_data or "state" not in state_data:
                return False
            return str(state_data["state"]).startswith(f"{CB_PREFIX}:edit:")

        tg.cbq_handler(on_callback, lambda c: (c.data or "").startswith(f"{CB_PREFIX}:"))
        tg.cbq_handler(on_plugin_settings, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
        tg.msg_handler(on_text, func=_is_editing)
        self.log("Telegram UI зарегистрирован ✅")


# ═════════════════════════════════════════════════════════════════════════════
#  FunPay Cardinal bindings
# ═════════════════════════════════════════════════════════════════════════════

def init_plugin(cardinal: Cardinal) -> None:
    global _plugin
    _plugin = Plugin(cardinal)
    _plugin.setup_telegram()
    logger.info("%s v%s загружен (автор %s)", _P, VERSION, CREDITS)


def message_handler(cardinal: Cardinal, event: NewMessageEvent) -> None:
    if _plugin:
        _plugin.on_new_message(event)


def last_chat_handler(cardinal: Cardinal, event: LastChatMessageChangedEvent) -> None:
    if _plugin:
        _plugin.on_last_chat(event)


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [last_chat_handler]
