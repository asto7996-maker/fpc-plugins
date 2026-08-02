"""
fake_client.py — минимальный «Telegram» для тестов цикла публикации.

Повторяет семантику Pyrogram/Telegram, которая важна поster'у:
  • get_chat_history отдаёт сообщения от новых к старым;
  • offset_id — строго СТАРШЕ указанного ID (exclusive);
  • offset (add_offset) сдвигает окно, отрицательный — к более новым;
  • несуществующих ID в истории просто нет (дыры не отдаются).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from pyrogram.enums import ChatType
from pyrogram.types import Chat, Message, Photo, Sticker

SOURCE_ID = -1001000000001
TARGET_ID = -1001000000002


def make_chat(chat_id: int = SOURCE_ID) -> Chat:
    return Chat(id=chat_id, type=ChatType.CHANNEL)


def text_message(message_id: int, text: str = "post", chat_id: int = SOURCE_ID) -> Message:
    return Message(id=message_id, chat=make_chat(chat_id), text=text)


def photo_message(
    message_id: int,
    *,
    caption: Optional[str] = None,
    group: Optional[str] = None,
    chat_id: int = SOURCE_ID,
) -> Message:
    photo = Photo(
        file_id=f"file_{message_id}",
        file_unique_id=f"u_{message_id}",
        width=100,
        height=100,
        file_size=1000,
        date=None,
    )
    return Message(
        id=message_id,
        chat=make_chat(chat_id),
        photo=photo,
        caption=caption,
        media_group_id=group,
    )


def sticker_message(message_id: int, chat_id: int = SOURCE_ID) -> Message:
    sticker = Sticker(
        file_id=f"sticker_{message_id}",
        file_unique_id=f"su_{message_id}",
        width=512,
        height=512,
        is_animated=False,
        is_video=False,
    )
    return Message(id=message_id, chat=make_chat(chat_id), sticker=sticker)


class FakeClient:
    """Клиент-заглушка: хранит «канал-источник» и собирает публикации."""

    def __init__(
        self,
        messages: Iterable[Message],
        *,
        target_id: int = TARGET_ID,
    ) -> None:
        self.messages: dict[int, Message] = {m.id: m for m in messages}
        self.target_id = target_id
        self.published: list[dict[str, Any]] = []
        self.history_calls = 0
        self.get_message_calls = 0
        self.window_broken = False
        self.window_skips = 0
        self.fail_next: Optional[BaseException] = None
        self.fail_always: Optional[BaseException] = None
        self.resolve_fail: Optional[BaseException] = None
        self.publish_fail: Optional[BaseException] = None
        self.dialogs: list[Any] = []
        self._next_target_id = 1000

    # ---------------------------------------------------------------- helpers

    def _ids_desc(self) -> list[int]:
        return sorted(self.messages, reverse=True)

    def _raise_if_needed(self) -> None:
        if self.fail_always is not None:
            raise self.fail_always
        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            raise error

    def _raise_publish(self) -> None:
        self._raise_if_needed()
        if self.publish_fail is not None:
            raise self.publish_fail

    def _new_target_message(self, **kwargs: Any) -> Message:
        self._next_target_id += 1
        return Message(
            id=self._next_target_id,
            chat=make_chat(self.target_id),
            **kwargs,
        )

    # ---------------------------------------------------------------- API

    async def get_chat_history(
        self,
        chat_id: int | str,
        limit: int = 0,
        offset: int = 0,
        offset_id: int = 0,
        offset_date: int = 0,
    ):
        self.history_calls += 1
        if self.window_broken and offset < 0:
            raise RuntimeError("history window unsupported")

        ids = self._ids_desc()
        start = 0
        if offset_id:
            start = next(
                (i for i, mid in enumerate(ids) if mid < offset_id), len(ids)
            )
        start += offset
        if self.window_skips and offset < 0:
            # Эмулируем «плохой» ответ: окно пропускает часть постов
            start += self.window_skips
        start = max(0, start)
        window = ids[start:]
        if limit:
            window = window[:limit]
        for mid in window:
            yield self.messages[mid]

    async def get_messages(self, chat_id: int | str, message_id: int):
        self.get_message_calls += 1
        self._raise_if_needed()
        return self.messages.get(message_id)

    async def get_media_group(self, chat_id: int | str, message_id: int):
        anchor = self.messages[message_id]
        gid = anchor.media_group_id
        return [m for m in self.messages.values() if m.media_group_id == gid]

    async def copy_message(
        self,
        chat_id: int | str,
        from_chat_id: int | str,
        message_id: int,
        caption: Optional[str] = None,
        parse_mode: Any = None,
    ):
        self._raise_publish()
        source = self.messages[message_id]
        self.published.append(
            {
                "kind": "sticker" if source.sticker else "copy",
                "source_id": message_id,
                "caption": caption,
            }
        )
        return self._new_target_message(
            photo=source.photo, sticker=source.sticker, caption=caption
        )

    async def copy_media_group(
        self,
        chat_id: int | str,
        from_chat_id: int | str,
        message_id: int,
    ):
        self._raise_publish()
        group = await self.get_media_group(from_chat_id, message_id)
        self.published.append(
            {"kind": "album", "source_id": message_id, "size": len(group)}
        )
        return [self._new_target_message(photo=m.photo) for m in group]

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: Any = None,
    ):
        self._raise_publish()
        self.published.append({"kind": "text", "text": text})
        return self._new_target_message(text=text)

    async def send_media_group(self, chat_id: int | str, media: list):
        self._raise_publish()
        caption = next((getattr(m, "caption", None) for m in media if getattr(m, "caption", None)), None)
        self.published.append(
            {"kind": "album", "size": len(media), "caption": caption}
        )
        first = self._new_target_message(caption=caption)
        rest = [self._new_target_message() for _ in media[1:]]
        return [first, *rest]

    async def send_photo(self, chat_id, media, caption=None, parse_mode=None):
        self._raise_if_needed()
        self.published.append({"kind": "photo", "caption": caption})
        return self._new_target_message(caption=caption)

    async def send_video(self, chat_id, media, caption=None, parse_mode=None):
        return await self.send_photo(chat_id, media, caption, parse_mode)

    async def send_document(self, chat_id, media, caption=None, parse_mode=None):
        return await self.send_photo(chat_id, media, caption, parse_mode)

    async def delete_messages(self, chat_id, message_ids):
        return True

    async def get_me(self):
        class _User:
            id = 424242
            username = "test_user"
            first_name = "Test"

        return _User()

    async def get_chat(self, chat_id):
        if self.resolve_fail is not None:
            raise self.resolve_fail
        if chat_id in ("me", "self"):
            return make_chat(424242)
        return make_chat(int(chat_id) if str(chat_id).lstrip("-").isdigit() else SOURCE_ID)

    async def resolve_peer(self, peer_id):
        """Прогрев peer для закрытых каналов (числовой id)."""
        if self.resolve_fail is not None:
            raise self.resolve_fail
        return int(peer_id) if str(peer_id).lstrip("-").isdigit() else peer_id

    async def get_dialogs(self):
        for d in self.dialogs:
            yield d

    async def invoke(self, *args, **kwargs):
        raise RuntimeError("resolve not supported in tests")
