"""
Rich Telegram notifications — exhaustive HTML reports from TelemetryEngine.

Every number comes from a real ``QuestTelemetry`` / ``BattleTelemetry`` /
``rates()`` sample. No placeholders.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from dwar_bot.core.telemetry_engine import (
    BattleTelemetry,
    QuestTelemetry,
    TelemetryEngine,
    _fmt_duration,
)

logger = logging.getLogger(__name__)

NotifyFn = Callable[[str, str], Awaitable[None]]  # text, category


def _esc(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_num(n: float, digits: int = 0) -> str:
    if digits <= 0:
        return f"{n:,.0f}".replace(",", " ")
    return f"{n:,.{digits}f}".replace(",", " ")


def _fmt_silver(silver: float) -> str:
    """Render silver; if |x|>=100 show as gold too."""
    if abs(silver) >= 100:
        gold = silver / 100.0
        return f"{_fmt_num(gold, 2)} зл. ({_fmt_num(silver, 0)} сер.)"
    return f"{_fmt_num(silver, 0)} сер."


class RichNotifications:
    """Formats and optionally sends rich Telegram reports."""

    def __init__(
        self,
        telemetry: TelemetryEngine,
        *,
        notify_fn: Optional[NotifyFn] = None,
    ) -> None:
        self.telemetry = telemetry
        self._notify = notify_fn

    def bind_notify(self, notify_fn: NotifyFn) -> None:
        self._notify = notify_fn

    async def send(self, text: str, category: str) -> None:
        if not self._notify:
            logger.info("rich_notify[%s]: %s", category, text[:200])
            return
        await self._notify(text, category)

    # ------------------------------------------------------------------
    # Quest
    # ------------------------------------------------------------------

    def format_quest_completed(self, qt: QuestTelemetry) -> str:
        lines = [
            f"📜 <b>[КВЕСТ ВЫПОЛНЕН]</b> «{_esc(qt.title)}»",
            f"⏱ Длительность: <b>{_esc(qt.duration_human())}</b>",
            f"🕐 {time_range(qt.started_at, qt.finished_at)}",
            "",
            "📊 <b>Затраты и Ресурсы:</b>",
        ]
        if qt.consumables:
            for c in qt.consumables:
                cost = f"cost: {_fmt_num(c.total_cost_silver, 0)} сер."
                if c.unit_cost_silver <= 0:
                    cost = "cost: н/д (нет цены аукциона)"
                lines.append(
                    f"• {_esc(c.title)}: <b>{c.qty}</b> шт. ({cost})"
                )
        else:
            lines.append("• Расходники: не использовались")

        dur = qt.durability_delta_pct
        sign = "+" if dur > 0 else ""
        lines.append(f"• Износ снаряжения: <b>{sign}{dur:.1f}%</b>")
        if qt.gold_spent > 0:
            lines.append(f"• Потрачено золота: <b>{_fmt_num(qt.gold_spent, 2)}</b> зл.")

        lines.append("")
        lines.append("🎁 <b>Полученная Награда:</b>")
        if qt.exp_gained > 0:
            pct = f" ({qt.exp_pct_of_level:.1f}% от уровня)" if qt.exp_pct_of_level else ""
            lines.append(f"• Опыт: <b>+{_fmt_num(qt.exp_gained)}</b> Exp{pct}")
        else:
            lines.append("• Опыт: н/д (сервер не отдал число — учтён прокси LevelingEngine при наличии)")
        if qt.valor_gained > 0:
            lines.append(f"• Доблесть: <b>+{_fmt_num(qt.valor_gained)}</b> Val")
        if qt.reputation_gained > 0:
            lines.append(f"• Репутация: <b>+{_fmt_num(qt.reputation_gained)}</b>")
        if qt.gold_gained > 0:
            lines.append(f"• Золото: <b>+{_fmt_num(qt.gold_gained, 2)}</b> зл.")
        if qt.loot:
            loot_s = ", ".join(
                f"[{_esc(x.title)}] x{x.qty}" for x in qt.loot[:12]
            )
            lines.append(f"• Лут: {loot_s}")
        else:
            lines.append("• Лут: —")

        lines.append("")
        eph = qt.exp_per_hour
        lines.append(
            f"📈 <b>Эффективность:</b> {_fmt_num(eph)} Exp/час | "
            f"Чистая прибыль: <b>{_fmt_silver(qt.net_profit_silver)}</b>"
        )
        if qt.area_id:
            lines.append(f"📍 Локация id=<code>{_esc(qt.area_id)}</code>")
        return "\n".join(lines)

    async def notify_quest_completed(self, qt: QuestTelemetry) -> str:
        text = self.format_quest_completed(qt)
        await self.send(text, "quests")
        return text

    # ------------------------------------------------------------------
    # Battle
    # ------------------------------------------------------------------

    def format_battle_finished(self, bt: BattleTelemetry) -> str:
        icon = "⚔️" if bt.result == "WIN" else ("💀" if bt.result == "LOSE" else "⚠️")
        title = bt.mob_name or bt.source or "бой"
        lines = [
            f"{icon} <b>[БОЙ {_esc(bt.result)}]</b> «{_esc(title)}»",
            f"⏱ Длительность: <b>{_esc(bt.duration_human())}</b>",
            f"🕐 {time_range(bt.started_at, bt.finished_at)}",
            f"📎 Источник: <code>{_esc(bt.source or '—')}</code>",
            "",
            "📊 <b>Боевая статистика:</b>",
            f"• Урон: <b>{_fmt_num(bt.damage_dealt)}</b> "
            f"(получено {_fmt_num(bt.damage_taken)})",
            f"• DPS: <b>{bt.dps:.2f}</b>",
            f"• Удары / промахи: <b>{bt.hits}</b> / <b>{bt.misses}</b>"
            + (f" · криты {bt.crits}" if bt.crits else ""),
            f"• Атак (сессия): <b>{bt.attacks}</b>",
            f"• Эликсиры: <b>{bt.potions_used}</b>"
            + (
                f" ({_esc(', '.join(bt.elixir_titles[:4]))})"
                if bt.elixir_titles else ""
            ),
            f"• Efficiency Score: <b>{bt.efficiency_score:.0f}/100</b>",
        ]
        if bt.area_id:
            lines.append(f"📍 area=<code>{_esc(bt.area_id)}</code>")
        return "\n".join(lines)

    async def notify_battle_finished(self, bt: BattleTelemetry) -> str:
        text = self.format_battle_finished(bt)
        await self.send(text, "battles")
        return text

    # ------------------------------------------------------------------
    # Farm / economy
    # ------------------------------------------------------------------

    def format_farm_economy(self, *, window_sec: float = 3600.0) -> str:
        r = self.telemetry.rates(window_sec=window_sec)
        bs = self.telemetry.battle_stats_summary()
        lines = [
            "💰 <b>[ЭКОНОМИКА / ФАРМ]</b>",
            f"⏱ Окно: {_esc(_fmt_duration(r['window_sec']))} · "
            f"Аптайм сессии: {_esc(_fmt_duration(r['session_uptime_sec']))}",
            "",
            "📈 <b>Скорости:</b>",
            f"• Exp/час: <b>{_fmt_num(r['exp_per_hour'])}</b>",
            f"• Gold/час: <b>{_fmt_num(r['gold_per_hour'], 2)}</b> зл.",
            f"• Net Gold/час (минус расходники): "
            f"<b>{_fmt_num(r['net_gold_per_hour'], 2)}</b> зл.",
            f"• Затраты на расходники: "
            f"<b>{_fmt_num(r['consumable_cost_silver'], 0)}</b> сер.",
            "",
            "⚔️ <b>Бои (окно / выборка):</b>",
            f"• Боёв: <b>{int(r['battles_in_window'])}</b> "
            f"(побед {int(r['wins_in_window'])})",
            f"• Средняя длительность: <b>{_esc(_fmt_duration(r['avg_battle_sec']))}</b>",
            f"• Avg DPS: <b>{r['avg_dps']:.2f}</b> · "
            f"Eff <b>{r['avg_efficiency']:.0f}/100</b>",
            f"• Промахи (выборка {bs['count']}): <b>{bs['miss_rate']:.1f}%</b> · "
            f"эл./бой <b>{bs['avg_potions']:.2f}</b>",
        ]
        return "\n".join(lines)

    async def notify_farm_economy(self, *, window_sec: float = 3600.0) -> str:
        text = self.format_farm_economy(window_sec=window_sec)
        await self.send(text, "heartbeat")
        return text

    def format_level_up_rich(
        self,
        *,
        level: int,
        exp_pct: float,
        exp_per_hour: float,
        eta_seconds: float,
        priority: str,
        directive_state: str = "",
    ) -> str:
        rates = self.telemetry.rates()
        # Proxy Exp% is not from the server — never claim "~5 сек" near the ceiling.
        pct_note = f"{exp_pct:.1f}%"
        if exp_pct >= 90.0 and (eta_seconds <= 0 or eta_seconds < 60.0):
            pct_note = f"{exp_pct:.0f}%·оценка"
            eta_note = "н/д (сервер не отдаёт Exp%)"
        elif eta_seconds <= 0:
            eta_note = "н/д"
        else:
            eta_note = _fmt_duration(eta_seconds)
        return "\n".join([
            "📈 <b>Level-Up Update</b>",
            f"• Уровень: <b>{level}</b> ({pct_note})",
            f"• Скорость кача: <b>+{_fmt_num(exp_per_hour)}</b> Exp/час",
            f"• Телеметрия Exp/час: <b>{_fmt_num(rates['exp_per_hour'])}</b>",
            f"• Gold/час: <b>{_fmt_num(rates['gold_per_hour'], 2)}</b> зл.",
            f"• До следующего уровня: ~{_esc(eta_note)}",
            f"• Текущий приоритет: {_esc(priority or '—')}",
            f"• State Machine: <code>{_esc(directive_state or '—')}</code>",
        ])


def time_range(start: float, end: float) -> str:
    import time as _t
    if start <= 0:
        return "—"
    a = _t.strftime("%H:%M:%S", _t.localtime(start))
    if end <= 0:
        return f"{a} → …"
    b = _t.strftime("%H:%M:%S", _t.localtime(end))
    return f"{a} → {b}"
