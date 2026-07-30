"""Unit tests for TData ZIP helpers (no live Telegram / real tdata required)."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tg_pool.clients.tdata_converter import (
    TDataConversionError,
    cleanup_tree,
    find_tdata_dir,
    safe_extract_zip,
    _proxy_dict_to_telethon,
)


class FindTDataTests(unittest.TestCase):
    def test_direct_tdata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tdata").mkdir()
            (root / "tdata" / "key_datas").write_text("x")
            self.assertEqual(find_tdata_dir(root), root / "tdata")

    def test_nested_tdata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "Telegram Desktop" / "tdata"
            nested.mkdir(parents=True)
            self.assertEqual(find_tdata_dir(root), nested)

    def test_top_level_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "key_datas").write_text("x")
            self.assertEqual(find_tdata_dir(root), root)

    def test_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TDataConversionError):
                find_tdata_dir(Path(tmp))


class ZipSafetyTests(unittest.TestCase):
    def test_extract_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_path = root / "a.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("tdata/key_datas", "hello")
            dest = root / "out"
            dest.mkdir()
            safe_extract_zip(zip_path, dest, max_bytes=1024 * 1024)
            self.assertTrue((dest / "tdata" / "key_datas").exists())

    def test_zip_slip_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zip_path = root / "evil.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../outside.txt", "pwn")
            dest = root / "out"
            dest.mkdir()
            with self.assertRaises(TDataConversionError):
                safe_extract_zip(zip_path, dest, max_bytes=1024 * 1024)

    def test_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "nested"
            root.mkdir()
            (root / "f").write_text("1")
            cleanup_tree(root)
            self.assertFalse(root.exists())


class ProxyDictTests(unittest.TestCase):
    def test_socks5(self) -> None:
        t = _proxy_dict_to_telethon(
            {
                "protocol": "socks5",
                "ip": "1.2.3.4",
                "port": 1080,
                "username": "u",
                "password": "p",
            }
        )
        assert t is not None
        self.assertEqual(t[1], "1.2.3.4")
        self.assertEqual(t[2], 1080)

    def test_none(self) -> None:
        self.assertIsNone(_proxy_dict_to_telethon(None))


if __name__ == "__main__":
    unittest.main()
