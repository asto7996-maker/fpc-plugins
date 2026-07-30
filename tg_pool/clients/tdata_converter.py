"""
TData (Telegram Desktop) → Telethon StringSession converter.

Uses `opentele` to load an official Desktop session from a `tdata/` folder,
preserve organic device fingerprints, and validate the account through a
live connect via a sticky SOCKS5/HTTP proxy.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiofiles

logger = logging.getLogger(__name__)

# Soft limit — Telegram Desktop tdata ZIPs are usually a few MB.
DEFAULT_MAX_ZIP_BYTES = 50 * 1024 * 1024


class TDataConversionError(Exception):
    """User-facing conversion failure (invalid archive, banned session, …)."""


@dataclass(frozen=True)
class ConvertedSession:
    """Result of a successful TData → StringSession conversion."""

    session_string: str
    api_id: int
    api_hash: str
    device_model: str
    system_version: str
    app_version: str
    lang_code: str
    phone_number: str
    username: Optional[str]
    user_id: int
    display_name: str


def _proxy_dict_to_telethon(proxy: Optional[dict[str, Any]]) -> Optional[tuple]:
    """
    Convert a plain proxy dict into a Telethon proxy tuple.

    Expected keys: protocol (socks5|http), ip, port, username?, password?
    Critical: the SAME proxy must later be persisted and reused for this account.
    """
    if not proxy:
        return None
    protocol = str(proxy.get("protocol") or proxy.get("type") or "socks5").lower()
    host = proxy.get("ip") or proxy.get("host")
    port = proxy.get("port")
    if not host or not port:
        raise TDataConversionError("Proxy dict must contain ip and port")

    import socks

    proxy_type = socks.SOCKS5 if protocol == "socks5" else socks.HTTP
    return (
        proxy_type,
        str(host),
        int(port),
        True,  # rdns through proxy
        proxy.get("username"),
        proxy.get("password"),
    )


def find_tdata_dir(root: Path) -> Path:
    """
    Locate a `tdata` directory inside an extracted archive.

    Accepts either:
      archive/tdata/...
      archive/.../tdata/...
      archive/  (already the tdata root — contains key_datas / map*)
    """
    direct = root / "tdata"
    if direct.is_dir():
        return direct

    for candidate in root.rglob("tdata"):
        if candidate.is_dir():
            return candidate

    # Some ZIPs contain the contents of tdata at the top level
    markers = ("key_datas", "map", "settings", "usertag")
    if any((root / name).exists() for name in markers):
        return root

    raise TDataConversionError(
        "В архиве не найдена папка tdata (ожидается ZIP с каталогом tdata)."
    )


def safe_extract_zip(zip_path: Path, dest_dir: Path, *, max_bytes: int) -> None:
    """
    Extract ZIP with Zip-Slip protection and total uncompressed size cap.
    """
    if not zipfile.is_zipfile(zip_path):
        raise TDataConversionError("Файл не является корректным ZIP-архивом.")

    dest_dir = dest_dir.resolve()
    total = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            # Skip absolute / traversal paths
            member_path = Path(info.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise TDataConversionError(
                    f"Небезопасный путь в архиве: {info.filename}"
                )
            total += int(info.file_size)
            if total > max_bytes:
                raise TDataConversionError(
                    f"Распакованный размер превышает лимит {max_bytes} байт."
                )
            target = (dest_dir / info.filename).resolve()
            if not str(target).startswith(str(dest_dir)):
                raise TDataConversionError(
                    f"Zip-Slip detected: {info.filename}"
                )
        zf.extractall(dest_dir)


async def convert_tdata_to_session(
    tdata_dir: str,
    proxy: Optional[dict[str, Any]] = None,
    *,
    passcode: Optional[str] = None,
) -> ConvertedSession:
    """
    Load TData via opentele, convert to Telethon StringSession, probe connectivity.

    Parameters
    ----------
    tdata_dir:
        Path to the `tdata` directory (not the ZIP).
    proxy:
        Optional dict `{protocol, ip, port, username?, password?}` used for the
        live connectivity check. Sticky — store the same proxy on the Account.
    passcode:
        Local passcode if the TData is encrypted.

    Raises
    ------
    TDataConversionError
        On invalid TData, auth failures, bans, FloodWait, checksum errors, etc.
    """
    # opentele / Qt internals are sync-heavy — run conversion off the event loop
    # where possible, but ToTelethon itself is async.
    try:
        from opentele.td import TDesktop
        from opentele.api import API, UseCurrentSession
        from opentele.exception import (
            OpenTeleException,
            TDataBadDecryptKey,
            TDataInvalidCheckSum,
            TDataInvalidMagic,
            TDesktopHasNoAccount,
            TDesktopNotLoaded,
            TDesktopUnauthorized,
            TFileNotFound,
        )
    except ImportError as exc:  # pragma: no cover
        raise TDataConversionError(
            "Пакет opentele не установлен. pip install opentele"
        ) from exc

    from telethon.errors import (
        AuthKeyDuplicatedError,
        FloodWaitError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    )
    from telethon.sessions import StringSession

    path = Path(tdata_dir)
    if not path.is_dir():
        raise TDataConversionError(f"tdata_dir не существует: {tdata_dir}")

    # --- 1) Load official Telegram Desktop session ---------------------------
    # UseCurrentSession + TelegramDesktop API keeps the original auth key and
    # organic Desktop fingerprint (do NOT swap to Android API here).
    api = API.TelegramDesktop
    try:
        tdesk = await asyncio.to_thread(
            TDesktop,
            str(path),
            api,
            passcode,
        )
    except TDataBadDecryptKey as exc:
        raise TDataConversionError(
            "TData защищена локальным паролем. Передайте passcode."
        ) from exc
    except TDataInvalidCheckSum as exc:
        # InvalidChecksum / corrupted map files
        raise TDataConversionError(
            "TData повреждена (InvalidChecksum). Проверьте архив."
        ) from exc
    except TDataInvalidMagic as exc:
        raise TDataConversionError(
            "TData имеет неверную сигнатуру (InvalidMagic)."
        ) from exc
    except TFileNotFound as exc:
        raise TDataConversionError(f"Файлы TData не найдены: {exc}") from exc
    except OpenTeleException as exc:
        raise TDataConversionError(f"Ошибка чтения TData: {exc}") from exc

    if not tdesk.isLoaded():
        raise TDataConversionError(
            "TData не загрузилась (TDesktopNotLoaded) — пустой или битый архив."
        )

    # Prefer device params from the API object attached to TDesktop
    api_data = getattr(tdesk, "api", None) or api
    device_model = str(getattr(api_data, "device_model", None) or "PC 64bit")
    system_version = str(getattr(api_data, "system_version", None) or "Windows 10")
    app_version = str(getattr(api_data, "app_version", None) or "4.14.0 x64")
    lang_code = str(
        getattr(api_data, "lang_code", None)
        or getattr(api_data, "system_lang_code", None)
        or "en"
    )[:8]
    api_id = int(getattr(api_data, "api_id", 2040))
    api_hash = str(getattr(api_data, "api_hash", ""))

    # AppVersion int from tdata (e.g. 4005000) — keep as fallback label
    tdesk_app_ver = getattr(tdesk, "AppVersion", None)
    if tdesk_app_ver and (not app_version or app_version == "4.14.0 x64"):
        app_version = str(tdesk_app_ver)

    proxy_tuple = _proxy_dict_to_telethon(proxy)
    client = None

    try:
        # --- 2) Convert to Telethon using the CURRENT auth key ----------------
        try:
            client = await tdesk.ToTelethon(
                session=StringSession(),
                flag=UseCurrentSession,
                api=api_data,
            )
        except TDesktopHasNoAccount as exc:
            raise TDataConversionError("В TData нет ни одного аккаунта.") from exc
        except TDesktopNotLoaded as exc:
            raise TDataConversionError("TDesktop не загружен.") from exc
        except TDesktopUnauthorized as exc:
            raise TDataConversionError(
                "TData не авторизована (сессия протухла)."
            ) from exc
        except OpenTeleException as exc:
            raise TDataConversionError(f"Ошибка конвертации opentele: {exc}") from exc

        # Inject sticky proxy BEFORE connect — never rotate later for this session
        if proxy_tuple is not None:
            if hasattr(client, "set_proxy"):
                client.set_proxy(proxy_tuple)
            else:
                # Fallback for Telethon builds without set_proxy helper
                client._proxy = proxy_tuple  # type: ignore[attr-defined]

        # --- 3) Live connectivity / viability probe --------------------------
        try:
            await client.connect()
        except FloodWaitError as exc:
            raise TDataConversionError(
                f"FloodWait при проверке сессии: подождите {exc.seconds}s."
            ) from exc
        except AuthKeyDuplicatedError as exc:
            raise TDataConversionError(
                "AuthKeyDuplicated: эта сессия уже используется с другого IP/устройства. "
                "Привяжите постоянный прокси и не запускайте TData параллельно."
            ) from exc
        except (UserDeactivatedError, UserDeactivatedBanError) as exc:
            raise TDataConversionError(
                f"Аккаунт забанен / деактивирован: {type(exc).__name__}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — network / proxy failures
            raise TDataConversionError(
                f"Не удалось подключиться через прокси: {type(exc).__name__}: {exc}"
            ) from exc

        if not await client.is_user_authorized():
            raise TDataConversionError(
                "Сессия не авторизована после конвертации TData."
            )

        try:
            me = await client.get_me()
        except FloodWaitError as exc:
            raise TDataConversionError(
                f"FloodWait на get_me: {exc.seconds}s"
            ) from exc
        except (UserDeactivatedError, UserDeactivatedBanError) as exc:
            raise TDataConversionError(
                f"Аккаунт забанен при get_me: {type(exc).__name__}"
            ) from exc

        if me is None:
            raise TDataConversionError("get_me() вернул пустой профиль.")

        phone = getattr(me, "phone", None) or ""
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"
        if not phone:
            phone = f"id:{me.id}"

        username = getattr(me, "username", None)
        first = getattr(me, "first_name", None) or ""
        last = getattr(me, "last_name", None) or ""
        display = username or f"{first} {last}".strip() or str(me.id)

        session_string = StringSession.save(client.session)
        if not session_string:
            raise TDataConversionError("Не удалось сериализовать StringSession.")

        logger.info(
            "TData converted user_id=%s phone=%s device=%s/%s/%s proxy=%s",
            me.id,
            phone,
            device_model,
            system_version,
            app_version,
            bool(proxy_tuple),
        )

        return ConvertedSession(
            session_string=session_string,
            api_id=api_id,
            api_hash=api_hash,
            device_model=device_model[:64],
            system_version=system_version[:64],
            app_version=app_version[:32],
            lang_code=lang_code,
            phone_number=phone[:32],
            username=username,
            user_id=int(me.id),
            display_name=str(display)[:128],
        )
    finally:
        if client is not None:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("Error disconnecting conversion client", exc_info=True)


async def convert_tdata_zip(
    zip_path: Path,
    work_dir: Path,
    proxy: Optional[dict[str, Any]] = None,
    *,
    passcode: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_ZIP_BYTES,
) -> ConvertedSession:
    """
    High-level helper: validate ZIP → extract → convert → (caller cleans work_dir).

    `work_dir` must be a temporary directory owned by the caller; this function
    does NOT delete it (so the caller can wrap cleanup in `finally`).
    """
    zip_path = Path(zip_path)
    if zip_path.suffix.lower() != ".zip":
        raise TDataConversionError("Ожидается файл с расширением .zip")
    size = zip_path.stat().st_size
    if size <= 0:
        raise TDataConversionError("Пустой ZIP-файл.")
    if size > max_bytes:
        raise TDataConversionError(
            f"ZIP слишком большой ({size} bytes > {max_bytes})."
        )

    extract_dir = work_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(safe_extract_zip, zip_path, extract_dir, max_bytes=max_bytes)
    tdata_dir = find_tdata_dir(extract_dir)
    return await convert_tdata_to_session(
        str(tdata_dir),
        proxy=proxy,
        passcode=passcode,
    )


async def save_document_to_path(file_path: str | Path, destination: Path) -> Path:
    """Copy / stream a downloaded bot document into destination using aiofiles."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    # aiogram downloads to a local path already; just move/copy safely
    src = Path(file_path)
    async with aiofiles.open(src, "rb") as src_f:
        data = await src_f.read()
    async with aiofiles.open(destination, "wb") as dst_f:
        await dst_f.write(data)
    return destination


def cleanup_tree(path: Path) -> None:
    """Best-effort recursive delete for temp TData artifacts."""
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to cleanup temp path %s", path, exc_info=True)
