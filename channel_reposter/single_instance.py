"""
single_instance.py — защита от второго запущенного экземпляра бота.

Два процесса с одним BOT_TOKEN отбирают друг у друга апдейты: Telegram
отдаёт каждый апдейт только одному из них, и панель выглядит «зависшей» —
половина нажатий просто исчезает. Поэтому второй процесс должен честно
отказаться стартовать и объяснить, что произошло.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]


@dataclass
class LockResult:
    """Итог попытки захватить блокировку процесса."""

    acquired: bool
    owner_pid: Optional[int] = None
    handle: Optional[object] = None

    @property
    def busy(self) -> bool:
        return not self.acquired


def _read_pid(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def acquire(path: Path) -> LockResult:
    """
    Захватить lock-файл. Блокировка снимается автоматически вместе с
    процессом (flock), поэтому «мёртвый» файл после kill -9 не мешает.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:  # pragma: no cover — окружение без fcntl
        logger.warning("fcntl недоступен — защита от второго экземпляра выключена")
        return LockResult(acquired=True)

    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        owner = _read_pid(path)
        handle.close()
        return LockResult(acquired=False, owner_pid=owner)

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return LockResult(acquired=True, owner_pid=os.getpid(), handle=handle)


def release(result: LockResult) -> None:
    handle = result.handle
    if handle is None or fcntl is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
    except OSError:
        logger.debug("не удалось снять блокировку", exc_info=True)
    try:
        handle.close()  # type: ignore[union-attr]
    except OSError:
        pass
