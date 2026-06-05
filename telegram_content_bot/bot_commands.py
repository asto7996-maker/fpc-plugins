"""
Модуль команд Telegram-бота.
Реализует админ-панель для управления ботом через команды.
"""

import asyncio
import html
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from database import PostStatus

if TYPE_CHECKING:
    from main import ContentBot

logger = logging.getLogger(__name__)

HELP_TEXT = """
<b>📋 Команды бота</b>

<b>Управление:</b>
/start — Запуск / приветствие
/help — Список команд
/status — Статус бота
/stats — Статистика
/pause — Приостановить публикацию
/resume — Возобновить публикацию
/reload — Перезагрузить конфигурацию

<b>Доноры:</b>
/donors — Список каналов-доноров
/add_donor &lt;канал&gt; — Добавить канал-донор
/remove_donor &lt;ID&gt; — Удалить канал-донор
/toggle_donor &lt;ID&gt; — Включить/выключить донора

<b>Очередь:</b>
/queue — Показать очередь
/queue_size — Размер очереди
/clear_queue — Очистить очередь
/skip — Пропустить текущий пост

<b>Настройки:</b>
/set_delay &lt;мин&gt; &lt;макс&gt; — Задержка (секунды)
/set_caption &lt;шаблон&gt; — Шаблон подписи
/set_link &lt;ссылка&gt; — Рекламная ссылка
/set_schedule &lt;старт&gt; &lt;конец&gt; — Расписание (часы)

<b>Фильтры:</b>
/filters — Текущие фильтры
/add_blackword &lt;слово&gt; — Чёрный список
/remove_blackword &lt;слово&gt; — Убрать из ЧС
/add_whiteword &lt;слово&gt; — Белый список
/remove_whiteword &lt;слово&gt; — Убрать из БС
/set_min_views &lt;число&gt; — Мин. просмотры

<b>Обслуживание:</b>
/errors — Последние ошибки
/cleanup — Очистка старых данных
/cleanup_media — Очистка загрузок
/ping — Проверка связи
"""


class BotCommands:
    """Обработчик команд бота через Telethon."""

    def __init__(self, bot: "ContentBot"):
        self._bot = bot
        self._client: TelegramClient = bot.client
        self._admin_ids: List[int] = bot.config.admin_ids

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._admin_ids

    def register_handlers(self) -> None:
        """Регистрирует все обработчики команд."""
        handlers = {
            "start": self._cmd_start,
            "help": self._cmd_help,
            "status": self._cmd_status,
            "stats": self._cmd_stats,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "reload": self._cmd_reload,
            "donors": self._cmd_donors,
            "add_donor": self._cmd_add_donor,
            "remove_donor": self._cmd_remove_donor,
            "toggle_donor": self._cmd_toggle_donor,
            "queue": self._cmd_queue,
            "queue_size": self._cmd_queue_size,
            "clear_queue": self._cmd_clear_queue,
            "skip": self._cmd_skip,
            "set_delay": self._cmd_set_delay,
            "set_caption": self._cmd_set_caption,
            "set_link": self._cmd_set_link,
            "set_schedule": self._cmd_set_schedule,
            "filters": self._cmd_filters,
            "add_blackword": self._cmd_add_blackword,
            "remove_blackword": self._cmd_remove_blackword,
            "add_whiteword": self._cmd_add_whiteword,
            "remove_whiteword": self._cmd_remove_whiteword,
            "set_min_views": self._cmd_set_min_views,
            "errors": self._cmd_errors,
            "cleanup": self._cmd_cleanup,
            "cleanup_media": self._cmd_cleanup_media,
            "ping": self._cmd_ping,
        }

        for command, handler in handlers.items():
            pattern = f"(?i)^/{command}(?:\\s|$|@)"

            @self._client.on(events.NewMessage(pattern=f"(?i)^/{command}(?:\\s|$|@)"))
            async def _wrapper(event, _handler=handler, _cmd=command):
                if not self._is_admin(event.sender_id):
                    await event.reply("⛔ Доступ запрещён.")
                    return
                try:
                    await _handler(event)
                except Exception as e:
                    logger.error("Ошибка в команде /%s: %s", _cmd, e, exc_info=True)
                    await event.reply(f"❌ Ошибка: {html.escape(str(e)[:300])}")

        logger.info("Зарегистрировано %d команд", len(handlers))

    async def _cmd_start(self, event: events.NewMessage.Event) -> None:
        text = (
            "<b>🤖 Content Bot</b>\n\n"
            "Бот для автоматического мониторинга каналов-доноров "
            "и публикации контента в целевой канал.\n\n"
            "Используйте /help для списка команд."
        )
        await event.reply(text, parse_mode="html")

    async def _cmd_help(self, event: events.NewMessage.Event) -> None:
        await event.reply(HELP_TEXT, parse_mode="html")

    async def _cmd_status(self, event: events.NewMessage.Event) -> None:
        monitor = self._bot.monitor
        publisher = self._bot.publisher

        pub_status = await publisher.get_status() if publisher else {}

        uptime = time.time() - self._bot.start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)

        monitor_status = "🟢 Работает" if (monitor and monitor.is_running) else "🔴 Остановлен"
        pub_state = pub_status.get("state", "stopped")
        pub_state_emoji = {"running": "🟢", "paused": "🟡", "stopped": "🔴"}.get(pub_state, "⚪")

        text = (
            "<b>📊 Статус бота</b>\n\n"
            f"⏱ Аптайм: <b>{hours}ч {minutes}м</b>\n"
            f"📡 Мониторинг: {monitor_status}\n"
            f"{pub_state_emoji} Публикатор: <b>{pub_state}</b>\n"
            f"📥 Очередь: <b>{pub_status.get('queue_size', 0)}</b>\n"
            f"✅ Опубликовано: <b>{pub_status.get('published_total', 0)}</b>\n"
            f"❌ Ошибок: <b>{pub_status.get('failed_total', 0)}</b>\n"
            f"🔄 Ошибок подряд: <b>{pub_status.get('consecutive_errors', 0)}</b>\n"
        )

        last_post = pub_status.get("last_post_time")
        if last_post:
            text += f"🕐 Последний пост: <b>{last_post}</b>\n"

        text += (
            f"\n⏳ Задержка: <b>{pub_status.get('min_delay', 0)}-"
            f"{pub_status.get('max_delay', 0)}с</b>\n"
            f"🎯 Канал: <b>{pub_status.get('target_channel', '—')}</b>"
        )

        await event.reply(text, parse_mode="html")

    async def _cmd_stats(self, event: events.NewMessage.Event) -> None:
        stats = await self._bot.db.get_stats()

        text = (
            "<b>📈 Статистика</b>\n\n"
            f"📥 Всего захвачено: <b>{stats.total_captured}</b>\n"
            f"✅ Опубликовано: <b>{stats.total_posted}</b>\n"
            f"⏭ Пропущено: <b>{stats.total_skipped}</b>\n"
            f"❌ Ошибок: <b>{stats.total_failed}</b>\n"
            f"🔁 Дубликатов: <b>{stats.total_duplicates}</b>\n"
            f"📥 В очереди: <b>{stats.queue_size}</b>\n\n"
            f"📅 Сегодня: <b>{stats.posts_today}</b>\n"
            f"📅 За неделю: <b>{stats.posts_this_week}</b>\n"
            f"📡 Активных доноров: <b>{stats.donors_active}</b>\n"
            f"⏱ Аптайм: <b>{stats.uptime_hours}ч</b>\n"
        )

        if stats.last_post_time:
            text += f"🕐 Последний пост: <b>{stats.last_post_time}</b>\n"

        if stats.top_donors:
            text += "\n<b>🏆 Топ доноров:</b>\n"
            for i, d in enumerate(stats.top_donors, 1):
                name = d.get("channel_title") or d.get("channel_username") or "—"
                text += (
                    f"  {i}. {html.escape(name)} — "
                    f"📥{d.get('total_captured', 0)} "
                    f"✅{d.get('total_posted', 0)}\n"
                )

        await event.reply(text, parse_mode="html")

    async def _cmd_pause(self, event: events.NewMessage.Event) -> None:
        if self._bot.publisher:
            self._bot.publisher.pause()
            await event.reply("⏸ Публикация приостановлена.")
        else:
            await event.reply("❌ Публикатор не запущен.")

    async def _cmd_resume(self, event: events.NewMessage.Event) -> None:
        if self._bot.publisher:
            self._bot.publisher.resume()
            await event.reply("▶️ Публикация возобновлена.")
        else:
            await event.reply("❌ Публикатор не запущен.")

    async def _cmd_reload(self, event: events.NewMessage.Event) -> None:
        try:
            self._bot.config.reload()
            self._bot._apply_config_updates()
            await event.reply("✅ Конфигурация перезагружена.")
        except Exception as e:
            await event.reply(f"❌ Ошибка при перезагрузке: {html.escape(str(e)[:300])}")

    async def _cmd_donors(self, event: events.NewMessage.Event) -> None:
        donors = await self._bot.db.get_all_donors()

        if not donors:
            await event.reply("📋 Список доноров пуст.")
            return

        text = "<b>📡 Каналы-доноры:</b>\n\n"
        for d in donors:
            status = "🟢" if d.enabled else "🔴"
            name = html.escape(d.channel_title or d.channel_username or str(d.channel_id))
            username = f"@{d.channel_username}" if d.channel_username else ""
            text += (
                f"{status} <b>{name}</b>"
                f"{' (' + username + ')' if username else ''}\n"
                f"   ID: <code>{d.channel_id}</code>\n"
                f"   📥 {d.total_captured} | ✅ {d.total_posted} | ⏭ {d.total_skipped}\n"
                f"   Последний пост: #{d.last_post_id}\n\n"
            )

        await event.reply(text, parse_mode="html")

    async def _cmd_add_donor(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: /add_donor @channel_username или ID")
            return

        channel = args[1].strip()

        msg = await event.reply(f"⏳ Добавляю канал {html.escape(channel)}...")

        donor = await self._bot.monitor.add_donor(channel)
        if donor:
            self._bot.config.donors.append(channel)
            self._bot.config.save()
            await msg.edit(
                f"✅ Донор добавлен: <b>{html.escape(donor.channel_title)}</b>\n"
                f"ID: <code>{donor.channel_id}</code>",
                parse_mode="html",
            )
        else:
            await msg.edit(f"❌ Не удалось добавить канал {html.escape(channel)}")

    async def _cmd_remove_donor(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: /remove_donor <channel_id>")
            return

        try:
            channel_id = int(args[1].strip())
        except ValueError:
            await event.reply("❌ ID канала должен быть числом")
            return

        result = await self._bot.monitor.remove_donor(channel_id)
        if result:
            await event.reply(f"✅ Донор {channel_id} удалён")
        else:
            await event.reply(f"❌ Донор {channel_id} не найден")

    async def _cmd_toggle_donor(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: /toggle_donor <channel_id>")
            return

        try:
            channel_id = int(args[1].strip())
        except ValueError:
            await event.reply("❌ ID канала должен быть числом")
            return

        donor = await self._bot.db.get_donor(channel_id)
        if not donor:
            await event.reply(f"❌ Донор {channel_id} не найден")
            return

        new_state = not donor.enabled
        await self._bot.db.toggle_donor(channel_id, new_state)
        emoji = "🟢" if new_state else "🔴"
        state_text = "включён" if new_state else "выключен"
        await event.reply(
            f"{emoji} Донор <b>{html.escape(donor.channel_title)}</b> {state_text}",
            parse_mode="html",
        )

    async def _cmd_queue(self, event: events.NewMessage.Event) -> None:
        queue = await self._bot.db.get_queue(limit=10)

        if not queue:
            await event.reply("📥 Очередь пуста.")
            return

        total = await self._bot.db.get_queue_size()
        text = f"<b>📥 Очередь ({total} всего):</b>\n\n"

        for i, post in enumerate(queue, 1):
            caption_preview = (post.caption[:60] + "...") if len(post.caption) > 60 else post.caption
            caption_preview = html.escape(caption_preview) if caption_preview else "<i>без текста</i>"
            text += (
                f"{i}. [{post.media_type or 'text'}] из {html.escape(post.donor_channel_name)}\n"
                f"   {caption_preview}\n"
                f"   <code>ID: {post.id} | msg: {post.original_message_id}</code>\n\n"
            )

        if total > 10:
            text += f"\n<i>...и ещё {total - 10}</i>"

        await event.reply(text, parse_mode="html")

    async def _cmd_queue_size(self, event: events.NewMessage.Event) -> None:
        size = await self._bot.db.get_queue_size()
        await event.reply(f"📥 В очереди: <b>{size}</b> постов", parse_mode="html")

    async def _cmd_clear_queue(self, event: events.NewMessage.Event) -> None:
        count = await self._bot.db.clear_queue()
        await event.reply(f"🗑 Очередь очищена: удалено <b>{count}</b> постов", parse_mode="html")

    async def _cmd_skip(self, event: events.NewMessage.Event) -> None:
        post = await self._bot.db.get_next_queued_post()
        if not post:
            await event.reply("📥 Очередь пуста, нечего пропускать.")
            return

        await self._bot.db.update_post_status(
            post.id, PostStatus.SKIPPED, error_message="Пропущен вручную"
        )
        await event.reply(
            f"⏭ Пропущен пост #{post.id} из {html.escape(post.donor_channel_name)}",
            parse_mode="html",
        )

    async def _cmd_set_delay(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split()
        if len(args) < 3:
            current_min = self._bot.config.get("posting.min_delay_seconds", 300)
            current_max = self._bot.config.get("posting.max_delay_seconds", 900)
            await event.reply(
                f"Текущая задержка: <b>{current_min}-{current_max}с</b>\n"
                f"Использование: /set_delay &lt;мин_сек&gt; &lt;макс_сек&gt;",
                parse_mode="html",
            )
            return

        try:
            min_delay = int(args[1])
            max_delay = int(args[2])
        except ValueError:
            await event.reply("❌ Задержки должны быть числами (секунды)")
            return

        if min_delay < 10:
            await event.reply("❌ Минимальная задержка не может быть меньше 10 секунд")
            return
        if max_delay < min_delay:
            await event.reply("❌ max_delay не может быть меньше min_delay")
            return

        self._bot.config.set("posting.min_delay_seconds", min_delay)
        self._bot.config.set("posting.max_delay_seconds", max_delay)
        self._bot.config.save()

        if self._bot.publisher:
            self._bot.publisher.update_config(self._bot.config.posting)

        await event.reply(
            f"✅ Задержка установлена: <b>{min_delay}-{max_delay}с</b>",
            parse_mode="html",
        )

    async def _cmd_set_caption(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            current = self._bot.config.get("posting.caption_template", "")
            example = (
                "Переменные: {caption}, {donor}, {link}, {newline}\n"
                "Пример: /set_caption {caption}{newline}{newline}👉 {link}"
            )
            template_display = html.escape(current) if current else "<i>не задан</i>"
            await event.reply(
                f"Текущий шаблон:\n<code>{template_display}</code>\n\n{example}",
                parse_mode="html",
            )
            return

        template = args[1].strip()
        self._bot.config.set("posting.caption_template", template)
        self._bot.config.save()
        await event.reply(
            f"✅ Шаблон подписи обновлён:\n<code>{html.escape(template)}</code>",
            parse_mode="html",
        )

    async def _cmd_set_link(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            current = self._bot.config.get("posting.append_link", "")
            await event.reply(
                f"Текущая ссылка: <code>{html.escape(current or 'не задана')}</code>\n"
                f"Использование: /set_link https://t.me/yourchannel",
                parse_mode="html",
            )
            return

        link = args[1].strip()
        self._bot.config.set("posting.append_link", link)
        self._bot.config.save()
        await event.reply(
            f"✅ Ссылка установлена: <code>{html.escape(link)}</code>",
            parse_mode="html",
        )

    async def _cmd_set_schedule(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split()
        if len(args) < 3:
            schedule = self._bot.config.get("posting.schedule_hours", {"start": 0, "end": 24})
            await event.reply(
                f"Текущее расписание: <b>{schedule.get('start', 0)}-{schedule.get('end', 24)}</b> ч.\n"
                f"Использование: /set_schedule &lt;начало&gt; &lt;конец&gt; (0-24)",
                parse_mode="html",
            )
            return

        try:
            start = int(args[1])
            end = int(args[2])
        except ValueError:
            await event.reply("❌ Часы должны быть числами (0-24)")
            return

        if not (0 <= start <= 24) or not (0 <= end <= 24):
            await event.reply("❌ Часы должны быть в диапазоне 0-24")
            return

        self._bot.config.set("posting.schedule_hours", {"start": start, "end": end})
        self._bot.config.save()
        await event.reply(
            f"✅ Расписание установлено: <b>{start}:00 — {end}:00</b>",
            parse_mode="html",
        )

    async def _cmd_filters(self, event: events.NewMessage.Event) -> None:
        fc = self._bot.config.filters_config

        def _format_list(lst):
            return ", ".join(str(x) for x in lst) if lst else "—"

        text = (
            "<b>🔧 Текущие фильтры:</b>\n\n"
            f"📏 Длина текста: {fc.get('min_text_length', 0)} — {fc.get('max_text_length', 0) or '∞'}\n"
            f"👁 Мин. просмотров: {fc.get('min_views', 0)}\n"
            f"📎 Требуемые медиа: {_format_list(fc.get('required_media_types', []))}\n"
            f"🚫 Блок медиа: {_format_list(fc.get('blocked_media_types', []))}\n"
            f"✅ Whitelist: {_format_list(fc.get('keyword_whitelist', []))}\n"
            f"❌ Blacklist: {_format_list(fc.get('keyword_blacklist', []))}\n"
            f"🔍 Regex WL: {len(fc.get('regex_whitelist', []))} паттернов\n"
            f"🔍 Regex BL: {len(fc.get('regex_blacklist', []))} паттернов\n"
            f"📢 Скип рекламы: {'Да' if fc.get('skip_ads', True) else 'Нет'}\n"
            f"🔁 Дедупликация: {'Да' if fc.get('duplicate_check', True) else 'Нет'}\n"
        )
        await event.reply(text, parse_mode="html")

    async def _cmd_add_blackword(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: /add_blackword <слово>")
            return

        word = args[1].strip().lower()
        blacklist = self._bot.config.get("filters.keyword_blacklist", [])
        if word in blacklist:
            await event.reply(f"Слово '{html.escape(word)}' уже в чёрном списке")
            return

        blacklist.append(word)
        self._bot.config.set("filters.keyword_blacklist", blacklist)
        self._bot.config.save()
        self._bot._apply_config_updates()
        await event.reply(
            f"✅ Добавлено в ЧС: <b>{html.escape(word)}</b>\n"
            f"Всего в ЧС: {len(blacklist)}",
            parse_mode="html",
        )

    async def _cmd_remove_blackword(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: /remove_blackword <слово>")
            return

        word = args[1].strip().lower()
        blacklist = self._bot.config.get("filters.keyword_blacklist", [])
        if word not in blacklist:
            await event.reply(f"Слово '{html.escape(word)}' не найдено в ЧС")
            return

        blacklist.remove(word)
        self._bot.config.set("filters.keyword_blacklist", blacklist)
        self._bot.config.save()
        self._bot._apply_config_updates()
        await event.reply(
            f"✅ Удалено из ЧС: <b>{html.escape(word)}</b>",
            parse_mode="html",
        )

    async def _cmd_add_whiteword(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: /add_whiteword <слово>")
            return

        word = args[1].strip().lower()
        whitelist = self._bot.config.get("filters.keyword_whitelist", [])
        if word in whitelist:
            await event.reply(f"Слово '{html.escape(word)}' уже в белом списке")
            return

        whitelist.append(word)
        self._bot.config.set("filters.keyword_whitelist", whitelist)
        self._bot.config.save()
        self._bot._apply_config_updates()
        await event.reply(
            f"✅ Добавлено в БС: <b>{html.escape(word)}</b>\n"
            f"Всего в БС: {len(whitelist)}",
            parse_mode="html",
        )

    async def _cmd_remove_whiteword(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split(maxsplit=1)
        if len(args) < 2:
            await event.reply("Использование: /remove_whiteword <слово>")
            return

        word = args[1].strip().lower()
        whitelist = self._bot.config.get("filters.keyword_whitelist", [])
        if word not in whitelist:
            await event.reply(f"Слово '{html.escape(word)}' не найдено в БС")
            return

        whitelist.remove(word)
        self._bot.config.set("filters.keyword_whitelist", whitelist)
        self._bot.config.save()
        self._bot._apply_config_updates()
        await event.reply(
            f"✅ Удалено из БС: <b>{html.escape(word)}</b>",
            parse_mode="html",
        )

    async def _cmd_set_min_views(self, event: events.NewMessage.Event) -> None:
        args = event.message.text.split()
        if len(args) < 2:
            current = self._bot.config.get("filters.min_views", 0)
            await event.reply(
                f"Текущее значение: <b>{current}</b>\n"
                f"Использование: /set_min_views &lt;число&gt;",
                parse_mode="html",
            )
            return

        try:
            min_views = int(args[1])
        except ValueError:
            await event.reply("❌ Значение должно быть числом")
            return

        if min_views < 0:
            await event.reply("❌ Значение не может быть отрицательным")
            return

        self._bot.config.set("filters.min_views", min_views)
        self._bot.config.save()
        self._bot._apply_config_updates()
        await event.reply(
            f"✅ Мин. просмотры: <b>{min_views}</b>",
            parse_mode="html",
        )

    async def _cmd_errors(self, event: events.NewMessage.Event) -> None:
        errors_list = await self._bot.db.get_recent_errors(limit=10)

        if not errors_list:
            await event.reply("✅ Ошибок нет.")
            return

        text = "<b>⚠️ Последние ошибки:</b>\n\n"
        for err in errors_list:
            ts = err.get("created_at", "")[:19]
            err_type = html.escape(err.get("error_type", ""))
            err_msg = html.escape(err.get("error_message", "")[:100])
            text += f"<code>{ts}</code> [{err_type}]\n{err_msg}\n\n"

        await event.reply(text, parse_mode="html")

    async def _cmd_cleanup(self, event: events.NewMessage.Event) -> None:
        days = self._bot.config.get("database.cleanup_days", 30)
        count = await self._bot.db.cleanup_old_records(days)
        await event.reply(
            f"🗑 Очистка завершена: удалено <b>{count}</b> записей старше {days} дней",
            parse_mode="html",
        )

    async def _cmd_cleanup_media(self, event: events.NewMessage.Event) -> None:
        count = self._bot.media_processor.cleanup_all_downloads()
        await event.reply(
            f"🗑 Очистка медиа: удалено <b>{count}</b> файлов",
            parse_mode="html",
        )

    async def _cmd_ping(self, event: events.NewMessage.Event) -> None:
        start = time.time()
        msg = await event.reply("🏓 Pong!")
        elapsed = (time.time() - start) * 1000
        await msg.edit(f"🏓 Pong! ({elapsed:.0f}ms)")
