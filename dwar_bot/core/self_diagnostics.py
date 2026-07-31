"""
Глубокая самодиагностика и генерация баг-репортов DwarBot.

При сбое сохраняет скриншот, HTML всех фреймов, console-логи браузера
и манифест в ``data/crash_dumps/``. Умеет гипотезировать смену CSS-селекторов
и формировать сжатый отчёт для Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Error as PlaywrightError, Frame, Page

from dwar_bot.config import DATA_DIR, BotConfig, config

logger = logging.getLogger(__name__)

CRASH_DUMPS_DIR = DATA_DIR / "crash_dumps"
MANIFEST_NAME = "dump_manifest.json"
DEFAULT_CONSOLE_LIMIT = 20
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_FOLDER_MB = 500

# Приоритетные фреймы для HTML-снимка
PRIORITY_FRAME_NAMES: Tuple[str, ...] = (
    "main",
    "fight",
    "combat",
    "user",
    "pers",
    "chat",
    "menu",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CrashDump:
    """Полный дамп сбоя для последующего разбора."""

    dump_id: str
    timestamp: datetime
    module_name: str
    exception_type: str
    exception_traceback: str
    screenshot_path: str = ""
    html_snapshot_path: str = ""
    console_logs: List[str] = field(default_factory=list)
    page_url: str = ""
    hypothesis: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


# ---------------------------------------------------------------------------
# SelfDiagnostics
# ---------------------------------------------------------------------------


class SelfDiagnostics:
    """
    Перехватчик аварий: скриншот + HTML фреймов + console + манифест.

    Подключите ``attach_page(page)`` после старта браузера, чтобы копить
    console-сообщения. Вызывайте ``capture_crash`` из recovery / main.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        dumps_dir: Optional[Path] = None,
        console_limit: int = DEFAULT_CONSOLE_LIMIT,
        telegram: Any = None,
    ) -> None:
        self._config = bot_config or config
        self.dumps_dir = Path(dumps_dir or CRASH_DUMPS_DIR)
        self.dumps_dir.mkdir(parents=True, exist_ok=True)
        self.console_limit = max(5, int(console_limit))
        self._telegram = telegram

        self._console_logs: List[str] = []
        self._page_ref: Optional[Page] = None
        self._listener_attached = False
        self._manifest_path = self.dumps_dir / MANIFEST_NAME
        self._ensure_manifest()

    # ------------------------------------------------------------------
    # Console capture
    # ------------------------------------------------------------------

    def attach_page(self, page: Page) -> None:
        """Подписаться на ``page.on('console')`` (идемпотентно)."""
        if page is None:
            return
        # При рестарте сессии — новая page
        if self._page_ref is page and self._listener_attached:
            return
        self._page_ref = page
        self._listener_attached = False

        def _on_console(msg: Any) -> None:
            try:
                mtype = getattr(msg, "type", "log")
                text = getattr(msg, "text", None)
                if callable(text):
                    text = text()
                text = str(text or "")
                # Берём error/warning и часть log
                if mtype in {"error", "warning"} or (
                    mtype == "log" and any(
                        k in text.lower()
                        for k in ("error", "fail", "exception", "uncaught")
                    )
                ):
                    line = f"[{mtype}] {text[:500]}"
                    self._console_logs.append(line)
                    if len(self._console_logs) > self.console_limit * 3:
                        self._console_logs = self._console_logs[
                            -(self.console_limit * 2) :
                        ]
            except Exception:
                logger.debug("console listener error", exc_info=True)

        def _on_page_error(exc: Any) -> None:
            try:
                self._console_logs.append(f"[pageerror] {exc}")
                if len(self._console_logs) > self.console_limit * 3:
                    self._console_logs = self._console_logs[-(self.console_limit * 2) :]
            except Exception:
                pass

        try:
            page.on("console", _on_console)
            page.on("pageerror", _on_page_error)
            self._listener_attached = True
            logger.info("SelfDiagnostics: console listener attached")
        except Exception as exc:
            logger.warning("Не удалось подписаться на console: %s", exc)

    def recent_console_logs(self, limit: Optional[int] = None) -> List[str]:
        n = limit or self.console_limit
        return list(self._console_logs[-n:])

    def bind_telegram(self, telegram: Any) -> None:
        self._telegram = telegram

    # ------------------------------------------------------------------
    # capture_crash
    # ------------------------------------------------------------------

    async def capture_crash(
        self,
        page: Optional[Page],
        exception: BaseException,
        module_context: str,
        *,
        expected_selector: str = "",
        send_telegram: bool = True,
    ) -> CrashDump:
        """
        Снять скриншот, HTML фреймов, console-логи и записать манифест.
        """
        dump_id = str(uuid.uuid4())
        ts = _utcnow()
        stamp = ts.strftime("%Y%m%d_%H%M%S")
        dump_dir = self.dumps_dir / f"{stamp}_{dump_id[:8]}"
        dump_dir.mkdir(parents=True, exist_ok=True)

        exc_type = type(exception).__name__
        exc_tb = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        module_name = (module_context or "unknown").strip() or "unknown"

        screenshot_path = ""
        html_snapshot_path = ""
        page_url = ""
        hypothesis = ""

        if page is not None:
            try:
                if not page.is_closed():
                    page_url = page.url or ""
            except Exception:
                page_url = ""

            screenshot_path = await self._save_screenshot(page, dump_dir, dump_id)
            html_snapshot_path = await self._save_html_frames(page, dump_dir, dump_id)

            if expected_selector:
                try:
                    hypothesis = await self.analyze_dom_desync(page, expected_selector)
                except Exception as exc:
                    logger.debug("analyze_dom_desync failed: %s", exc)

        console_logs = self.recent_console_logs(self.console_limit)

        dump = CrashDump(
            dump_id=dump_id,
            timestamp=ts,
            module_name=module_name,
            exception_type=exc_type,
            exception_traceback=exc_tb,
            screenshot_path=screenshot_path,
            html_snapshot_path=html_snapshot_path,
            console_logs=console_logs,
            page_url=page_url,
            hypothesis=hypothesis,
            extra={"dump_dir": str(dump_dir)},
        )

        # Метаданные рядом с артефактами
        meta_path = dump_dir / "crash_meta.json"
        try:
            meta_path.write_text(
                json.dumps(dump.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Не удалось записать crash_meta.json: %s", exc)

        self._append_manifest(dump)
        logger.critical(
            "CrashDump %s module=%s type=%s screenshot=%s",
            dump_id,
            module_name,
            exc_type,
            screenshot_path or "-",
        )

        if send_telegram:
            await self._send_crash_telegram(dump)

        return dump

    async def _save_screenshot(
        self, page: Page, dump_dir: Path, dump_id: str
    ) -> str:
        path = dump_dir / f"screenshot_{dump_id[:8]}.png"
        try:
            if page.is_closed():
                return ""
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:
            logger.warning("screenshot failed: %s", exc)
            # Фоллбек без full_page
            try:
                await page.screenshot(path=str(path), full_page=False)
                return str(path)
            except Exception:
                return ""

    async def _save_html_frames(
        self, page: Page, dump_dir: Path, dump_id: str
    ) -> str:
        """
        Сохранить HTML всех активных фреймов. Возвращает путь к
        главному index-файлу (или каталогу снимков).
        """
        frames_dir = dump_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        index_path = dump_dir / f"dom_{dump_id[:8]}.html"
        parts: List[str] = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            f"<title>Crash dump {dump_id}</title></head><body>",
            f"<h1>Crash dump {dump_id}</h1>",
            f"<p>URL: {getattr(page, 'url', '')}</p><hr/>",
        ]

        frames: List[Frame] = []
        try:
            frames = list(page.frames)
        except PlaywrightError:
            frames = []

        # Сортировка: приоритетные имена выше
        def _rank(fr: Frame) -> Tuple[int, str]:
            name = (fr.name or "").lower()
            try:
                idx = PRIORITY_FRAME_NAMES.index(name)
            except ValueError:
                idx = 99
            return (idx, name or fr.url[-40:])

        try:
            frames_sorted = sorted(frames, key=_rank)
        except Exception:
            frames_sorted = frames

        saved = 0
        for i, fr in enumerate(frames_sorted):
            name = (fr.name or f"frame_{i}").strip() or f"frame_{i}"
            safe = re.sub(r"[^\w.\-]+", "_", name)[:60]
            file_name = f"{i:02d}_{safe}.html"
            file_path = frames_dir / file_name
            try:
                html = await asyncio.wait_for(fr.content(), timeout=8.0)
            except Exception as exc:
                html = f"<!-- failed to read frame {name}: {exc} -->"
            try:
                file_path.write_text(html, encoding="utf-8", errors="replace")
                saved += 1
            except OSError as exc:
                logger.debug("write frame html %s: %s", file_path, exc)
            parts.append(
                f"<h2>Frame: {name}</h2>"
                f"<p>url={fr.url}</p>"
                f"<p><a href='frames/{file_name}'>{file_name}</a></p>"
                f"<details><summary>preview</summary>"
                f"<pre>{_escape_preview(html, 4000)}</pre></details>"
            )

        # Также top-level content дублем
        try:
            top_html = await asyncio.wait_for(page.content(), timeout=8.0)
            top_path = frames_dir / "00_page.html"
            top_path.write_text(top_html, encoding="utf-8", errors="replace")
        except Exception:
            pass

        parts.append(f"<p>Saved frames: {saved}</p></body></html>")
        try:
            index_path.write_text("\n".join(parts), encoding="utf-8")
            return str(index_path)
        except OSError as exc:
            logger.warning("index html write failed: %s", exc)
            return str(frames_dir) if saved else ""

    # ------------------------------------------------------------------
    # DOM desync analysis
    # ------------------------------------------------------------------

    async def analyze_dom_desync(self, page: Page, expected_selector: str) -> str:
        """
        Если ``expected_selector`` не найден — ищет похожие теги/кнопки
        и формулирует гипотезу о смене селектора.
        """
        selector = (expected_selector or "").strip()
        if not selector:
            return "Селектор не задан — анализ невозможен."

        # Уже есть?
        try:
            handle = await page.query_selector(selector)
            if handle is not None:
                return (
                    f"Селектор `{selector}` найден на странице — "
                    "десинхрон DOM маловероятен (элемент мог быть скрыт/вне viewport)."
                )
        except PlaywrightError:
            pass

        # Собираем кандидатов из page + frames
        candidates: List[Dict[str, str]] = []
        owners: List[Any] = [page]
        try:
            owners.extend(page.frames)
        except PlaywrightError:
            pass

        for owner in owners:
            try:
                html = await asyncio.wait_for(owner.content(), timeout=5.0)
            except Exception:
                continue
            soup = BeautifulSoup(html, "html.parser")
            candidates.extend(self._extract_candidate_elements(soup))

        if not candidates:
            return (
                f"Селектор `{selector}` не найден, похожих элементов в DOM нет "
                "(страница пустая / фрейм не загружен)."
            )

        scored = self._score_candidates(selector, candidates)
        if not scored:
            return f"Селектор `{selector}` не найден; близких совпадений нет."

        best = scored[0]
        alt = scored[1] if len(scored) > 1 else None
        best_sel = best["suggested"]
        ratio = best["score"]

        hypothesis = (
            f"Селектор кнопки/элемента изменился с `{selector}` на `{best_sel}` "
            f"(сходство {ratio:.0%}, tag={best.get('tag')}, "
            f"text≈«{best.get('text', '')[:40]}»)."
        )
        if alt:
            hypothesis += (
                f" Альтернатива: `{alt['suggested']}` ({alt['score']:.0%})."
            )
        return hypothesis

    def _extract_candidate_elements(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for tag in soup.find_all(
            ["a", "button", "input", "div", "span", "img", "td", "tr", "form"]
        ):
            if not isinstance(tag, Tag):
                continue
            el_id = str(tag.get("id") or "").strip()
            classes = tag.get("class") or []
            if isinstance(classes, str):
                classes = classes.split()
            class_str = " ".join(str(c) for c in classes)
            name = str(tag.get("name") or "")
            data_attrs = {
                k: str(v)
                for k, v in tag.attrs.items()
                if str(k).startswith("data-") and v
            }
            text = tag.get_text(" ", strip=True)[:80]
            title = str(tag.get("title") or tag.get("alt") or tag.get("value") or "")
            out.append(
                {
                    "tag": tag.name or "?",
                    "id": el_id,
                    "class": class_str,
                    "name": name,
                    "text": text or title,
                    "data": " ".join(f"{k}={v}" for k, v in list(data_attrs.items())[:4]),
                }
            )
            if len(out) >= 400:
                break
        return out

    def _score_candidates(
        self, expected: str, candidates: Sequence[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        needle = expected.lower().strip()
        # Токены из селектора: id, classes, attr values
        tokens = set(re.findall(r"[a-zA-Zа-яА-Я_][\w\-]*", needle))
        tokens -= {"has", "text", "nth", "child", "type", "href", "data"}

        scored: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for cand in candidates:
            suggested = self._suggest_selector(cand)
            if not suggested or suggested in seen:
                continue
            seen.add(suggested)
            blob = " ".join(
                [
                    cand.get("id", ""),
                    cand.get("class", ""),
                    cand.get("name", ""),
                    cand.get("text", ""),
                    cand.get("data", ""),
                    cand.get("tag", ""),
                ]
            ).lower()
            ratio = SequenceMatcher(None, needle, suggested.lower()).ratio()
            token_hits = sum(1 for t in tokens if t.lower() in blob)
            token_score = token_hits / max(1, len(tokens))
            text_bonus = 0.15 if any(
                t.lower() in (cand.get("text") or "").lower() for t in tokens if len(t) > 2
            ) else 0.0
            score = 0.55 * ratio + 0.35 * token_score + text_bonus
            if score < 0.25:
                continue
            scored.append(
                {
                    "suggested": suggested,
                    "score": min(1.0, score),
                    "tag": cand.get("tag"),
                    "text": cand.get("text", ""),
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:5]

    @staticmethod
    def _suggest_selector(cand: Dict[str, str]) -> str:
        el_id = cand.get("id") or ""
        if el_id:
            return f"#{el_id}"
        classes = [c for c in (cand.get("class") or "").split() if c]
        tag = cand.get("tag") or "div"
        if classes:
            return f"{tag}." + ".".join(classes[:3])
        name = cand.get("name") or ""
        if name:
            return f"{tag}[name='{name}']"
        text = (cand.get("text") or "").strip()
        if text and len(text) <= 40:
            safe = text.replace("'", "\\'")
            return f"{tag}:has-text('{safe}')"
        return ""

    # ------------------------------------------------------------------
    # Telegram report
    # ------------------------------------------------------------------

    def format_telegram_crash_report(self, dump: CrashDump) -> str:
        """Сжатое понятное сообщение для Telegram (текст + путь к скрину)."""
        tb_tail = dump.exception_traceback.strip().splitlines()
        short_tb = "\n".join(tb_tail[-8:]) if tb_tail else dump.exception_type
        if len(short_tb) > 900:
            short_tb = short_tb[-900:]

        console_tail = dump.console_logs[-5:] if dump.console_logs else []
        console_block = "\n".join(console_tail) if console_tail else "—"

        lines = [
            "Crash report DwarBot",
            f"id: {dump.dump_id[:8]}…",
            f"time: {dump.timestamp.astimezone().strftime('%d.%m %H:%M:%S')}",
            f"module: {dump.module_name}",
            f"error: {dump.exception_type}",
            f"url: {(dump.page_url or '—')[:120]}",
            "",
            "traceback (tail):",
            short_tb,
            "",
            "console:",
            console_block,
        ]
        if dump.hypothesis:
            lines.extend(["", "DOM hypothesis:", dump.hypothesis[:400]])
        if dump.screenshot_path:
            lines.extend(["", f"screenshot: {dump.screenshot_path}"])
        if dump.html_snapshot_path:
            lines.append(f"html: {dump.html_snapshot_path}")
        return "\n".join(lines)

    async def _send_crash_telegram(self, dump: CrashDump) -> None:
        tg = self._telegram
        if tg is None or not hasattr(tg, "send_alert"):
            return
        text = self.format_telegram_crash_report(dump)
        photo = dump.screenshot_path if dump.screenshot_path else None
        try:
            await tg.send_alert(text[:3500], photo_path=photo)
        except Exception as exc:
            logger.warning("Telegram crash report failed: %s", exc)

    # ------------------------------------------------------------------
    # Manifest / cleanup
    # ------------------------------------------------------------------

    def _ensure_manifest(self) -> None:
        if self._manifest_path.is_file():
            return
        try:
            self._manifest_path.write_text(
                json.dumps({"dumps": []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("manifest create failed: %s", exc)

    def _append_manifest(self, dump: CrashDump) -> None:
        """Дописать метаданные в ``crash_dumps/dump_manifest.json``."""
        try:
            if self._manifest_path.is_file():
                raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            else:
                raw = {"dumps": []}
            dumps = list(raw.get("dumps") or [])
            dumps.append(dump.as_dict())
            # Храним последние 200 записей в манифесте
            raw["dumps"] = dumps[-200:]
            raw["updated_at"] = _utcnow().isoformat()
            self._manifest_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("manifest append failed: %s", exc)

    def cleanup_old_dumps(
        self,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        max_folder_size_mb: int = DEFAULT_MAX_FOLDER_MB,
    ) -> Dict[str, int]:
        """
        Удалить устаревшие дампы и ужать каталог до лимита размера.
        """
        max_age_days = max(1, int(max_age_days))
        max_bytes = max(10, int(max_folder_size_mb)) * 1024 * 1024
        cutoff = _utcnow() - timedelta(days=max_age_days)
        deleted_dirs = 0
        deleted_files = 0
        freed = 0

        if not self.dumps_dir.exists():
            return {"deleted_dirs": 0, "deleted_files": 0, "freed_bytes": 0}

        # 1) По возрасту — каталоги dump_*
        for path in sorted(self.dumps_dir.iterdir()):
            if path.name == MANIFEST_NAME:
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                if path.is_dir():
                    size = _dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)
                    deleted_dirs += 1
                    freed += size
                elif path.is_file():
                    size = path.stat().st_size
                    path.unlink(missing_ok=True)
                    deleted_files += 1
                    freed += size
            except OSError as exc:
                logger.debug("cleanup dump %s: %s", path, exc)

        # 2) По размеру каталога — удаляем самые старые
        while _dir_size(self.dumps_dir) > max_bytes:
            oldest = self._oldest_dump_entry()
            if oldest is None:
                break
            try:
                size = _dir_size(oldest) if oldest.is_dir() else oldest.stat().st_size
                if oldest.is_dir():
                    shutil.rmtree(oldest, ignore_errors=True)
                    deleted_dirs += 1
                else:
                    if oldest.name == MANIFEST_NAME:
                        break
                    oldest.unlink(missing_ok=True)
                    deleted_files += 1
                freed += size
            except OSError:
                break

        # Подчистить манифест от удалённых путей
        self._prune_manifest()

        result = {
            "deleted_dirs": deleted_dirs,
            "deleted_files": deleted_files,
            "freed_bytes": int(freed),
        }
        logger.info("cleanup_old_dumps: %s", result)
        return result

    def _oldest_dump_entry(self) -> Optional[Path]:
        entries = [
            p
            for p in self.dumps_dir.iterdir()
            if p.name != MANIFEST_NAME
        ]
        if not entries:
            return None
        try:
            return min(entries, key=lambda p: p.stat().st_mtime)
        except OSError:
            return None

    def _prune_manifest(self) -> None:
        try:
            if not self._manifest_path.is_file():
                return
            raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            dumps = []
            for item in raw.get("dumps") or []:
                shot = str(item.get("screenshot_path") or "")
                html = str(item.get("html_snapshot_path") or "")
                dump_dir = str((item.get("extra") or {}).get("dump_dir") or "")
                alive = False
                for p in (shot, html, dump_dir):
                    if p and Path(p).exists():
                        alive = True
                        break
                if alive:
                    dumps.append(item)
            raw["dumps"] = dumps[-200:]
            self._manifest_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("prune manifest: %s", exc)


def _dir_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _escape_preview(html: str, limit: int = 4000) -> str:
    text = (html or "")[:limit]
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = [
    "CrashDump",
    "SelfDiagnostics",
    "CRASH_DUMPS_DIR",
]
