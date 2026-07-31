"""
Аналитика сессии, KPI и периодические отчёты DwarBot.

Пишет события в SQLite (`data/analytics.db`) с JSON-фоллбеком,
считает профит/winrate/расход эликсиров и шлёт сводки в Telegram
(и опционально Discord Webhook) по расписанию.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

from dwar_bot.config import DATA_DIR, BotConfig, config

logger = logging.getLogger(__name__)

ANALYTICS_DB_NAME = "analytics.db"
ANALYTICS_JSONL_NAME = "analytics_events.jsonl"
DEFAULT_REPORT_INTERVAL_HOURS = 12.0
DEFAULT_TIMEFRAME_HOURS = 24

# Известные типы событий
EVENT_BATTLE_WON = "battle_won"
EVENT_BATTLE_LOST = "battle_lost"
EVENT_RESOURCE_FARMED = "resource_farmed"
EVENT_POTION_USED = "potion_used"
EVENT_AUCTION_BUY = "auction_buy"
EVENT_AUCTION_SELL = "auction_sell"
EVENT_CAPTCHA = "captcha"
EVENT_GOLD_EARNED = "gold_earned"
EVENT_GOLD_SPENT = "gold_spent"
EVENT_EXP_GAINED = "exp_gained"
EVENT_VALOR_GAINED = "valor_gained"
EVENT_DOWNTIME = "downtime"
EVENT_SESSION_START = "session_start"
EVENT_SESSION_STOP = "session_stop"
EVENT_CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _empty_int_dict() -> Dict[str, int]:
    return {}


@dataclass(slots=True)
class SessionMetrics:
    """Метрики текущей / агрегированной сессии бота."""

    start_time: datetime = field(default_factory=_utcnow)
    gold_earned: float = 0.0  # чистый профит в золоте
    battles_won: int = 0
    battles_lost: int = 0
    exp_gained: int = 0
    valor_gained: int = 0  # доблесть
    resources_farmed: Dict[str, int] = field(default_factory=_empty_int_dict)
    potions_used: Dict[str, int] = field(default_factory=_empty_int_dict)
    captchas_encountered: int = 0
    total_downtime_seconds: float = 0.0
    gold_spent: float = 0.0
    auctions_bought: int = 0
    auctions_sold: int = 0
    end_time: Optional[datetime] = None

    @property
    def battles_total(self) -> int:
        return self.battles_won + self.battles_lost

    @property
    def winrate_pct(self) -> float:
        total = self.battles_total
        if total <= 0:
            return 0.0
        return 100.0 * self.battles_won / float(total)

    @property
    def duration_hours(self) -> float:
        end = self.end_time or _utcnow()
        start = self.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        seconds = max(0.0, (end - start).total_seconds() - self.total_downtime_seconds)
        return max(seconds / 3600.0, 1e-6)

    @property
    def gold_per_hour(self) -> float:
        return self.gold_earned / self.duration_hours

    @property
    def potions_total(self) -> int:
        return sum(self.potions_used.values())

    @property
    def resources_total(self) -> int:
        return sum(self.resources_farmed.values())

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["start_time"] = self.start_time.isoformat()
        data["end_time"] = self.end_time.isoformat() if self.end_time else None
        data["winrate_pct"] = round(self.winrate_pct, 2)
        data["gold_per_hour"] = round(self.gold_per_hour, 4)
        data["duration_hours"] = round(self.duration_hours, 4)
        return data


@dataclass(slots=True)
class DailyReport:
    """Агрегированная статистика за последние N часов (по умолчанию 24)."""

    timeframe_hours: int = 24
    generated_at: datetime = field(default_factory=_utcnow)
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    events_count: int = 0
    top_resources: List[Tuple[str, int]] = field(default_factory=list)
    top_potions: List[Tuple[str, int]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "timeframe_hours": self.timeframe_hours,
            "generated_at": self.generated_at.isoformat(),
            "metrics": self.metrics.as_dict(),
            "events_count": self.events_count,
            "top_resources": list(self.top_resources),
            "top_potions": list(self.top_potions),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# AnalyticsReporter
# ---------------------------------------------------------------------------


class AnalyticsReporter:
    """
    Трекер KPI + планировщик отчётов.

    Хранилище: SQLite ``data/analytics.db`` (основное) и зеркало JSONL
    ``data/analytics_events.jsonl`` на случай повреждения БД.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        db_path: Optional[Path] = None,
        jsonl_path: Optional[Path] = None,
        report_interval_hours: float = DEFAULT_REPORT_INTERVAL_HOURS,
        discord_webhook_url: Optional[str] = None,
        enable_jsonl_mirror: bool = True,
    ) -> None:
        self._config = bot_config or config
        data_dir = DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = Path(db_path or (data_dir / ANALYTICS_DB_NAME))
        self.jsonl_path = Path(jsonl_path or (data_dir / ANALYTICS_JSONL_NAME))
        self.report_interval_hours = max(0.25, float(report_interval_hours))
        self.discord_webhook_url = (
            discord_webhook_url
            if discord_webhook_url is not None
            else os.getenv("DWAR_DISCORD_WEBHOOK_URL", "").strip()
        )
        self.enable_jsonl_mirror = enable_jsonl_mirror

        self.session = SessionMetrics(start_time=_utcnow())
        self._lock = threading.RLock()
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._db_ok = False

        self._init_storage()
        self.track_event(
            EVENT_SESSION_START,
            {"server": self._config.server.server},
        )

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _init_storage(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        ts_iso TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)"
                )
                conn.commit()
            self._db_ok = True
            logger.info("Analytics SQLite: %s", self.db_path)
        except Exception as exc:
            self._db_ok = False
            logger.error(
                "Не удалось инициализировать SQLite (%s) — используем JSONL",
                exc,
            )
            self.enable_jsonl_mirror = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def _append_jsonl(self, record: Dict[str, Any]) -> None:
        if not self.enable_jsonl_mirror:
            return
        try:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("jsonl mirror write failed: %s", exc)

    # ------------------------------------------------------------------
    # track_event
    # ------------------------------------------------------------------

    def track_event(self, event_type: str, payload: Optional[dict] = None) -> None:
        """
        Зарегистрировать событие и обновить SessionMetrics.

        Примеры ``event_type``:
          battle_won, battle_lost, resource_farmed, potion_used,
          auction_buy, auction_sell, captcha, gold_earned, gold_spent,
          exp_gained, valor_gained, downtime
        """
        etype = (event_type or EVENT_CUSTOM).strip().lower() or EVENT_CUSTOM
        data = dict(payload or {})
        now = time.time()
        ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        record = {
            "ts": now,
            "ts_iso": ts_iso,
            "event_type": etype,
            "payload": data,
        }

        with self._lock:
            self._apply_to_session(etype, data)
            self._persist_event(record)

        logger.debug("analytics event=%s payload=%s", etype, data)

    def _persist_event(self, record: Dict[str, Any]) -> None:
        payload_json = json.dumps(record["payload"], ensure_ascii=False)
        if self._db_ok:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO events (ts, ts_iso, event_type, payload) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            float(record["ts"]),
                            str(record["ts_iso"]),
                            str(record["event_type"]),
                            payload_json,
                        ),
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning("SQLite insert failed (%s) — JSONL fallback", exc)
                self._db_ok = False
                self.enable_jsonl_mirror = True

        self._append_jsonl(
            {
                "ts": record["ts"],
                "ts_iso": record["ts_iso"],
                "event_type": record["event_type"],
                "payload": record["payload"],
            }
        )

    def _apply_to_session(self, event_type: str, payload: Dict[str, Any]) -> None:
        s = self.session
        if event_type == EVENT_BATTLE_WON:
            s.battles_won += 1
            s.exp_gained += int(payload.get("exp", 0) or 0)
            s.valor_gained += int(payload.get("valor", 0) or 0)
            s.gold_earned += float(payload.get("gold", 0) or 0)
        elif event_type == EVENT_BATTLE_LOST:
            s.battles_lost += 1
        elif event_type == EVENT_RESOURCE_FARMED:
            name = str(payload.get("name") or payload.get("resource") or "unknown")
            qty = max(1, int(payload.get("count", payload.get("qty", 1)) or 1))
            s.resources_farmed[name] = s.resources_farmed.get(name, 0) + qty
        elif event_type == EVENT_POTION_USED:
            name = str(payload.get("name") or payload.get("potion") or "эликсир")
            qty = max(1, int(payload.get("count", 1) or 1))
            s.potions_used[name] = s.potions_used.get(name, 0) + qty
        elif event_type == EVENT_AUCTION_BUY:
            s.auctions_bought += 1
            spent = float(payload.get("gold", payload.get("spent_gold", 0)) or 0)
            s.gold_spent += spent
            s.gold_earned -= spent
        elif event_type == EVENT_AUCTION_SELL:
            s.auctions_sold += 1
            earned = float(payload.get("gold", payload.get("earned_gold", 0)) or 0)
            s.gold_earned += earned
        elif event_type == EVENT_CAPTCHA:
            s.captchas_encountered += int(payload.get("count", 1) or 1)
            downtime = float(payload.get("downtime_seconds", 0) or 0)
            s.total_downtime_seconds += max(0.0, downtime)
        elif event_type == EVENT_GOLD_EARNED:
            s.gold_earned += float(payload.get("gold", payload.get("amount", 0)) or 0)
        elif event_type == EVENT_GOLD_SPENT:
            amount = float(payload.get("gold", payload.get("amount", 0)) or 0)
            s.gold_spent += amount
            s.gold_earned -= amount
        elif event_type == EVENT_EXP_GAINED:
            s.exp_gained += int(payload.get("exp", payload.get("amount", 0)) or 0)
        elif event_type == EVENT_VALOR_GAINED:
            s.valor_gained += int(payload.get("valor", payload.get("amount", 0)) or 0)
        elif event_type == EVENT_DOWNTIME:
            s.total_downtime_seconds += max(
                0.0,
                float(payload.get("seconds", payload.get("downtime_seconds", 0)) or 0),
            )
        elif event_type == EVENT_SESSION_STOP:
            s.end_time = _utcnow()

    # ------------------------------------------------------------------
    # Aggregation / reports
    # ------------------------------------------------------------------

    def fetch_events(
        self,
        *,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        event_types: Optional[Sequence[str]] = None,
        limit: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """Прочитать события из SQLite (или JSONL-фоллбек)."""
        since = since_ts if since_ts is not None else 0.0
        until = until_ts if until_ts is not None else time.time() + 1.0
        types = {t.lower() for t in event_types} if event_types else None

        if self._db_ok:
            try:
                rows: List[Dict[str, Any]] = []
                with self._connect() as conn:
                    cur = conn.execute(
                        "SELECT ts, ts_iso, event_type, payload FROM events "
                        "WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT ?",
                        (since, until, int(limit)),
                    )
                    for row in cur.fetchall():
                        etype = str(row["event_type"])
                        if types is not None and etype.lower() not in types:
                            continue
                        try:
                            payload = json.loads(row["payload"] or "{}")
                        except json.JSONDecodeError:
                            payload = {}
                        rows.append(
                            {
                                "ts": float(row["ts"]),
                                "ts_iso": str(row["ts_iso"]),
                                "event_type": etype,
                                "payload": payload,
                            }
                        )
                return rows
            except Exception as exc:
                logger.warning("SQLite fetch failed: %s", exc)

        return self._fetch_events_jsonl(
            since=since, until=until, types=types, limit=limit
        )

    def _fetch_events_jsonl(
        self,
        *,
        since: float,
        until: float,
        types: Optional[set],
        limit: int,
    ) -> List[Dict[str, Any]]:
        if not self.jsonl_path.is_file():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with self.jsonl_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = float(rec.get("ts") or 0)
                    if ts < since or ts > until:
                        continue
                    etype = str(rec.get("event_type") or "")
                    if types is not None and etype.lower() not in types:
                        continue
                    rows.append(
                        {
                            "ts": ts,
                            "ts_iso": str(rec.get("ts_iso") or ""),
                            "event_type": etype,
                            "payload": dict(rec.get("payload") or {}),
                        }
                    )
                    if len(rows) >= limit:
                        break
        except Exception as exc:
            logger.error("JSONL fetch failed: %s", exc)
        return rows

    def build_metrics_from_events(
        self,
        events: Sequence[Dict[str, Any]],
        *,
        start_time: Optional[datetime] = None,
    ) -> SessionMetrics:
        """Построить SessionMetrics из списка событий (не трогая self.session)."""
        metrics = SessionMetrics(
            start_time=start_time or _utcnow(),
            resources_farmed={},
            potions_used={},
        )
        if events:
            first_ts = float(events[0]["ts"])
            metrics.start_time = datetime.fromtimestamp(first_ts, tz=timezone.utc)
            last_ts = float(events[-1]["ts"])
            metrics.end_time = datetime.fromtimestamp(last_ts, tz=timezone.utc)

        saved = self.session
        self.session = metrics
        try:
            for ev in events:
                self._apply_to_session(
                    str(ev["event_type"]), dict(ev.get("payload") or {})
                )
        finally:
            self.session = saved
        return metrics

    def build_daily_report(
        self, timeframe_hours: int = DEFAULT_TIMEFRAME_HOURS
    ) -> DailyReport:
        hours = max(1, int(timeframe_hours))
        since = time.time() - hours * 3600.0
        events = self.fetch_events(since_ts=since)
        start = datetime.fromtimestamp(since, tz=timezone.utc)
        metrics = self.build_metrics_from_events(events, start_time=start)
        metrics.end_time = _utcnow()

        top_resources = sorted(
            metrics.resources_farmed.items(), key=lambda x: x[1], reverse=True
        )[:8]
        top_potions = sorted(
            metrics.potions_used.items(), key=lambda x: x[1], reverse=True
        )[:8]

        notes: List[str] = []
        if metrics.captchas_encountered:
            notes.append(f"Капч за период: {metrics.captchas_encountered}")
        if metrics.total_downtime_seconds >= 60:
            notes.append(
                f"Простой: {metrics.total_downtime_seconds / 60.0:.1f} мин"
            )
        if metrics.battles_total == 0:
            notes.append("Боёв не зафиксировано")
        if metrics.gold_per_hour < 0:
            notes.append("Отрицательный профит (траты > доходов)")

        return DailyReport(
            timeframe_hours=hours,
            generated_at=_utcnow(),
            metrics=metrics,
            events_count=len(events),
            top_resources=top_resources,
            top_potions=top_potions,
            notes=notes,
        )

    def generate_summary_report(
        self, timeframe_hours: int = DEFAULT_TIMEFRAME_HOURS
    ) -> str:
        """Красиво отформатированное сообщение для Telegram / Discord."""
        report = self.build_daily_report(timeframe_hours=timeframe_hours)
        m = report.metrics
        hours = report.timeframe_hours

        def fmt_res(items: Sequence[Tuple[str, int]]) -> str:
            if not items:
                return "—"
            return ", ".join(f"{name}×{cnt}" for name, cnt in items)

        lines = [
            f"📊 DwarBot отчёт за {hours}ч",
            f"🕒 {report.generated_at.astimezone().strftime('%d.%m.%Y %H:%M')}",
            "",
            f"💰 Чистый профит: {m.gold_earned:+.2f}з",
            f"📈 Gold/hr: {m.gold_per_hour:+.2f}з/ч",
            (
                f"🛒 Траты: {m.gold_spent:.2f}з | "
                f"аукцион ⬇{m.auctions_bought} ⬆{m.auctions_sold}"
            ),
            "",
            f"⚔️ Бои: {m.battles_won}W / {m.battles_lost}L",
            f"🎯 Winrate: {m.winrate_pct:.1f}% ({m.battles_total} всего)",
            f"✨ Опыт: +{m.exp_gained} | 🏅 Доблесть: +{m.valor_gained}",
            "",
            f"🌾 Ресурсы ({m.resources_total}): {fmt_res(report.top_resources)}",
            f"🧪 Эликсиры ({m.potions_total}): {fmt_res(report.top_potions)}",
            f"🤖 Капчи: {m.captchas_encountered}",
            f"⏸ Простой: {m.total_downtime_seconds / 60.0:.1f} мин",
            f"📦 Событий в БД: {report.events_count}",
        ]
        if report.notes:
            lines.append("")
            lines.append("📝 " + " · ".join(report.notes))

        sess = self.session
        lines.extend(
            [
                "",
                "———",
                (
                    f"🟢 Сессия с {sess.start_time.astimezone().strftime('%H:%M')}: "
                    f"{sess.battles_won}W/{sess.battles_lost}L · "
                    f"{sess.gold_earned:+.2f}з · {sess.gold_per_hour:+.2f}з/ч"
                ),
            ]
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Scheduled sending
    # ------------------------------------------------------------------

    async def send_scheduled_report(self, bot_control: Any) -> None:
        """
        Фоновый asyncio-таск: каждые ``report_interval_hours`` часов
        формирует сводку и отправляет через TelegramRemoteControl /
        Discord Webhook.

        ``bot_control`` — TelegramRemoteControl / объект с ``send_alert``,
        либо оркестратор с атрибутом ``telegram``.
        """
        self._stop_event = asyncio.Event()
        interval = self.report_interval_hours * 3600.0
        logger.info(
            "AnalyticsReporter scheduler started (every %.2fh)",
            self.report_interval_hours,
        )

        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    pass

                if self._stop_event.is_set():
                    break

                tf = max(1, int(round(self.report_interval_hours)))
                if tf > 24:
                    tf = DEFAULT_TIMEFRAME_HOURS
                text = self.generate_summary_report(timeframe_hours=tf)
                await self._dispatch_report(bot_control, text)
        except asyncio.CancelledError:
            logger.info("AnalyticsReporter scheduler cancelled")
            raise
        finally:
            logger.info("AnalyticsReporter scheduler stopped")

    async def start_scheduler(self, bot_control: Any) -> asyncio.Task[None]:
        """Запустить ``send_scheduled_report`` как фоновую задачу."""
        await self.stop_scheduler()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self.send_scheduled_report(bot_control),
            name="analytics-reporter",
        )
        return self._task

    async def stop_scheduler(self) -> None:
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def send_report_now(
        self,
        bot_control: Any,
        *,
        timeframe_hours: int = DEFAULT_TIMEFRAME_HOURS,
    ) -> str:
        """Сформировать и сразу отправить отчёт (ручной вызов)."""
        text = self.generate_summary_report(timeframe_hours=timeframe_hours)
        await self._dispatch_report(bot_control, text)
        return text

    async def _dispatch_report(self, bot_control: Any, text: str) -> None:
        telegram = self._resolve_telegram(bot_control)
        sent = False
        if telegram is not None:
            try:
                result = await telegram.send_alert(text)
                sent = bool(result)
                logger.info("Analytics report → Telegram (ok=%s)", sent)
            except Exception as exc:
                logger.error("Telegram report failed: %s", exc, exc_info=True)

        if self.discord_webhook_url:
            try:
                await self._send_discord(text)
                sent = True
                logger.info("Analytics report → Discord webhook")
            except Exception as exc:
                logger.error("Discord report failed: %s", exc, exc_info=True)

        if not sent:
            logger.warning(
                "Отчёт не доставлен (нет Telegram/Discord). Текст:\n%s",
                text[:500],
            )

    @staticmethod
    def _resolve_telegram(bot_control: Any) -> Any:
        if bot_control is None:
            return None
        if hasattr(bot_control, "send_alert") and callable(bot_control.send_alert):
            return bot_control
        if hasattr(bot_control, "telegram"):
            return bot_control.telegram
        return None

    async def _send_discord(self, content: str) -> None:
        url = self.discord_webhook_url
        if not url:
            return
        chunk = content if len(content) <= 1900 else content[:1900] + "…"
        payload = {"content": chunk, "username": "DwarBot Analytics"}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Discord webhook HTTP {resp.status}: {body[:200]}"
                    )

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def export_to_csv(
        self,
        filepath: str,
        *,
        timeframe_hours: Optional[int] = None,
    ) -> Path:
        """Экспорт истории событий в CSV для Excel / Google Sheets."""
        path = Path(filepath).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)

        since = None
        if timeframe_hours is not None:
            since = time.time() - max(1, int(timeframe_hours)) * 3600.0
        events = self.fetch_events(since_ts=since, limit=200_000)

        fieldnames = [
            "ts",
            "ts_iso",
            "event_type",
            "gold",
            "exp",
            "valor",
            "name",
            "count",
            "downtime_seconds",
            "payload_json",
        ]
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for ev in events:
                payload = dict(ev.get("payload") or {})
                writer.writerow(
                    {
                        "ts": ev.get("ts"),
                        "ts_iso": ev.get("ts_iso"),
                        "event_type": ev.get("event_type"),
                        "gold": payload.get(
                            "gold",
                            payload.get(
                                "spent_gold", payload.get("earned_gold", "")
                            ),
                        ),
                        "exp": payload.get("exp", ""),
                        "valor": payload.get("valor", ""),
                        "name": payload.get(
                            "name",
                            payload.get("resource", payload.get("potion", "")),
                        ),
                        "count": payload.get("count", payload.get("qty", "")),
                        "downtime_seconds": payload.get(
                            "downtime_seconds", payload.get("seconds", "")
                        ),
                        "payload_json": json.dumps(payload, ensure_ascii=False),
                    }
                )

        logger.info("Analytics CSV exported: %s (%s rows)", path, len(events))
        return path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def snapshot_session(self) -> SessionMetrics:
        s = self.session
        return SessionMetrics(
            start_time=s.start_time,
            gold_earned=s.gold_earned,
            battles_won=s.battles_won,
            battles_lost=s.battles_lost,
            exp_gained=s.exp_gained,
            valor_gained=s.valor_gained,
            resources_farmed=dict(s.resources_farmed),
            potions_used=dict(s.potions_used),
            captchas_encountered=s.captchas_encountered,
            total_downtime_seconds=s.total_downtime_seconds,
            gold_spent=s.gold_spent,
            auctions_bought=s.auctions_bought,
            auctions_sold=s.auctions_sold,
            end_time=s.end_time,
        )

    def close(self) -> None:
        self.track_event(EVENT_SESSION_STOP, {})
        logger.info(
            "Analytics session closed: %+0.2fз, %sW/%sL, resources=%s",
            self.session.gold_earned,
            self.session.battles_won,
            self.session.battles_lost,
            self.session.resources_total,
        )


__all__ = [
    "SessionMetrics",
    "DailyReport",
    "AnalyticsReporter",
    "EVENT_BATTLE_WON",
    "EVENT_BATTLE_LOST",
    "EVENT_RESOURCE_FARMED",
    "EVENT_POTION_USED",
    "EVENT_AUCTION_BUY",
    "EVENT_AUCTION_SELL",
    "EVENT_CAPTCHA",
    "EVENT_GOLD_EARNED",
    "EVENT_GOLD_SPENT",
    "EVENT_EXP_GAINED",
    "EVENT_VALOR_GAINED",
    "EVENT_DOWNTIME",
]
