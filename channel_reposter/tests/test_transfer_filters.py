"""
Тесты отдельной функции чистого перелива (без сети).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transfer_filters import (  # noqa: E402
    REASON_FILE,
    REASON_GIF,
    REASON_LINK,
    REASON_VOICE,
    filter_album_for_transfer,
    should_skip_transfer,
    transfer_skip_reason,
)


def _msg(**kwargs):
    defaults = dict(
        photo=None,
        video=None,
        document=None,
        animation=None,
        voice=None,
        audio=None,
        sticker=None,
        video_note=None,
        text=None,
        caption=None,
        entities=None,
        caption_entities=None,
        web_page=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TransferSkipReasonTests(unittest.TestCase):
    def test_plain_text_kept(self) -> None:
        self.assertIsNone(transfer_skip_reason(_msg(text="обычный пост")))
        self.assertFalse(should_skip_transfer(_msg(text="hello")))

    def test_photo_and_video_kept(self) -> None:
        self.assertIsNone(transfer_skip_reason(_msg(photo=object())))
        self.assertIsNone(transfer_skip_reason(_msg(video=object(), caption="без ссылок")))

    def test_http_link_skipped(self) -> None:
        self.assertEqual(
            transfer_skip_reason(_msg(text="смотри https://example.com/x")),
            REASON_LINK,
        )

    def test_telegram_link_skipped(self) -> None:
        self.assertEqual(transfer_skip_reason(_msg(text="t.me/channel/1")), REASON_LINK)
        self.assertEqual(
            transfer_skip_reason(_msg(caption="https://t.me/c/123/4")),
            REASON_LINK,
        )

    def test_text_link_entity_skipped(self) -> None:
        entity = SimpleNamespace(type="text_link", url="https://example.com")
        self.assertEqual(
            transfer_skip_reason(_msg(text="жми сюда", entities=[entity])),
            REASON_LINK,
        )

    def test_url_entity_enum_name_skipped(self) -> None:
        entity = SimpleNamespace(type=SimpleNamespace(name="URL"), url=None)
        self.assertEqual(
            transfer_skip_reason(_msg(text="link", entities=[entity])),
            REASON_LINK,
        )

    def test_web_preview_skipped(self) -> None:
        self.assertEqual(
            transfer_skip_reason(_msg(text="заголовок", web_page=object())),
            REASON_LINK,
        )

    def test_document_skipped(self) -> None:
        doc = SimpleNamespace(mime_type="application/pdf", file_name="a.pdf", attributes=[])
        self.assertEqual(transfer_skip_reason(_msg(document=doc)), REASON_FILE)

    def test_gif_animation_skipped(self) -> None:
        self.assertEqual(transfer_skip_reason(_msg(animation=object())), REASON_GIF)

    def test_gif_document_skipped(self) -> None:
        doc = SimpleNamespace(mime_type="image/gif", file_name="loop.gif", attributes=[])
        self.assertEqual(transfer_skip_reason(_msg(document=doc)), REASON_GIF)

    def test_voice_skipped(self) -> None:
        self.assertEqual(transfer_skip_reason(_msg(voice=object())), REASON_VOICE)

    def test_mention_without_url_kept(self) -> None:
        entity = SimpleNamespace(type="mention", url=None)
        self.assertIsNone(
            transfer_skip_reason(_msg(text="@channel", entities=[entity]))
        )


class AlbumFilterTests(unittest.TestCase):
    def test_album_with_link_dropped_entirely(self) -> None:
        photo = _msg(photo=object(), caption="https://evil.example")
        kept, reason = filter_album_for_transfer([photo])
        self.assertEqual(kept, [])
        self.assertEqual(reason, REASON_LINK)

    def test_album_drops_file_keeps_photo(self) -> None:
        photo = _msg(id=1, photo=object())
        doc = _msg(
            id=2,
            document=SimpleNamespace(
                mime_type="application/zip", file_name="a.zip", attributes=[]
            ),
        )
        kept, reason = filter_album_for_transfer([photo, doc])
        self.assertIsNone(reason)
        self.assertEqual(kept, [photo])

    def test_album_of_gifs_dropped(self) -> None:
        gifs = [_msg(animation=object()), _msg(animation=object())]
        kept, reason = filter_album_for_transfer(gifs)
        self.assertEqual(kept, [])
        self.assertEqual(reason, REASON_GIF)


if __name__ == "__main__":
    unittest.main()
