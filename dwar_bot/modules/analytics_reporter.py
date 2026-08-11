"""
AnalyticsReporter — exhaustive console / Telegram analytics from live state.

Pulls TelemetryEngine, GameKnowledgeBase, LevelingEngine, combat/quest
sessions and MasterController — every field is a real runtime value.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from dwar_bot.core.bot_state import get_bot_state
from dwar_bot.core.rich_notifications import RichNotifications, _esc, _fmt_duration, _fmt_num
from dwar_bot.core.telemetry_engine import TelemetryEngine
from dwar_bot.modules.money_format import format_money_from_state

logger = logging.getLogger(__name__)


class AnalyticsReporter:
    """
    Builds full analytics reports for heartbeat / ``/stats`` / console.

    Construct with a live ``DwarBot`` (duck-typed) that exposes:
    ``telemetry``, ``rich``, ``leveling``, ``knowledge``, ``controller``,
    ``combat``, ``quests``, ``brain``, ``settings``, ``_char``, ``_state``,
    ``_area_title``, ``_profile``, ``_iteration``, ``_started_at``,
    ``get_status``.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @property
    def telemetry(self) -> TelemetryEngine:
        return self.bot.telemetry

    @property
    def rich(self) -> RichNotifications:
        return self.bot.rich

    def build_full_report(self) -> str:
        bot = self.bot
        st = bot.get_status()
        r = bot.settings.report
        rates = self.telemetry.rates(window_sec=3600.0)
        bsum = self.telemetry.battle_stats_summary()
        lv = bot.leveling.progress
        directive = bot.controller.directive_summary()
        kb_best = bot.knowledge.best_farm_target(
            char_level=int(st.get("level") or 0),
            area_id=str(st.get("area_id") or ""),
        )
        matrix = bot.knowledge.efficiency_matrix(
            char_level=int(st.get("level") or 0),
            area_id=str(st.get("area_id") or ""),
        )[:5]

        parts: list[str] = [
            f"<b>📊 Analytics Report</b> · {time.strftime('%H:%M:%S')}",
            f"🧙 <b>{_esc(st.get('nick', '?'))}</b> "
            f"Lv{st.get('level', '?')} · "
            f"❤️ {st.get('hp', '?')}/{st.get('hp_max', '?')} "
            f"({getattr(bot._char, 'hp_percent', 0):.0f}%) · "
            f"💙 {st.get('mp', '?')}/{st.get('mp_max', '?')}",
            f"💰 <b>{_esc(format_money_from_state(bot._state) if hasattr(bot, '_state') else _fmt_num(float(st.get('money') or 0), 2))}</b> · "
            f"📍 {_esc(st.get('area_title') or st.get('area_id') or '?')} "
            f"(id=<code>{_esc(st.get('area_id'))}</code>)",
            f"⏱ Аптайм <b>{_esc(st.get('uptime', '?'))}</b> · "
            f"тик {st.get('iteration', 0)} · "
            f"SM <code>{_esc(get_bot_state().name)}</code>",
        ]

        # Level-Up
        parts.append("")
        parts.append("<b>📈 Level-Up</b>")
        parts.append(
            f"• Прогресс уровня: <b>{lv.level}</b> ({lv.exp_pct:.1f}%) · "
            f"режим <code>{_esc(lv.mode)}</code>"
        )
        parts.append(
            f"• Exp/час (LevelingEngine): <b>{_fmt_num(lv.exp_per_hour)}</b> · "
            f"телеметрия: <b>{_fmt_num(rates['exp_per_hour'])}</b>"
        )
        parts.append(
            f"• ETA: ~{_esc(_fmt_duration(lv.eta_seconds))} · "
            f"приоритет: {_esc(lv.priority_title or '—')}"
        )
        parts.append(
            f"• Директива: <code>{_esc(directive.get('state', '—'))}</code> "
            f"«{_esc(directive.get('title', ''))}» · "
            f"mob=<code>{_esc(directive.get('mob_id') or directive.get('mob_name') or '—')}</code>"
        )

        # Combat telemetry
        if r.include_combat:
            cs = bot.combat.session
            parts.append("")
            parts.append("<b>⚔️ Боевая сессия (live + SQLite)</b>")
            parts.append(
                f"• Live: бои {cs.battles_joined} · "
                f"🏆{cs.wins}/💀{cs.losses} · WR {cs.win_rate:.0f}% · "
                f"🧪{cs.potions_used} · 👊{cs.attacks_made}"
            )
            parts.append(
                f"• SQLite выборка: {bsum['count']} боёв · "
                f"avg {_esc(_fmt_duration(bsum['avg_duration_sec']))} · "
                f"DPS {bsum['avg_dps']:.2f} · "
                f"Eff {bsum['avg_efficiency']:.0f}/100 · "
                f"промахи {bsum['miss_rate']:.1f}% · "
                f"эл./бой {bsum['avg_potions']:.2f}"
            )
            if self.telemetry.active_battle:
                ab = self.telemetry.active_battle
                parts.append(
                    f"• Активный бой: «{_esc(ab.mob_name or ab.source)}» "
                    f"с {_esc(time.strftime('%H:%M:%S', time.localtime(ab.started_at)))}"
                )

        # Quests
        if r.include_quests:
            qs = bot.quests.session
            parts.append("")
            parts.append("<b>📜 Квесты</b>")
            parts.append(
                f"• ✅{qs.quests_completed} · 📝{qs.quests_accepted} · "
                f"💬{qs.dialogues_handled} · NPC {qs.npcs_visited}"
            )
            pending = bot.brain.pending_hunt_mob or bot.quests.pending_hunt_mob
            if pending:
                parts.append(f"• Kill-gate: <b>{_esc(pending)}</b>")
            if self.telemetry.active_quest:
                aq = self.telemetry.active_quest
                elapsed = time.time() - aq.started_at
                parts.append(
                    f"• Активный трек: «{_esc(aq.title)}» · "
                    f"идёт {_esc(_fmt_duration(elapsed))} · "
                    f"расходников {len(aq.consumables)}"
                )
            last_q = self.telemetry.latest_completed_quest()
            if last_q:
                parts.append(
                    f"• Последний завершённый: «{_esc(last_q.title)}» · "
                    f"{_esc(last_q.duration_human())} · "
                    f"+{_fmt_num(last_q.exp_gained)} Exp · "
                    f"{_fmt_num(last_q.exp_per_hour)} Exp/ч"
                )

        # Economy
        parts.append("")
        parts.append("<b>💰 Экономика (1ч окно)</b>")
        parts.append(
            f"• Gold/час <b>{_fmt_num(rates['gold_per_hour'], 2)}</b> · "
            f"Net <b>{_fmt_num(rates['net_gold_per_hour'], 2)}</b> зл./ч"
        )
        parts.append(
            f"• Расходники: <b>{_fmt_num(rates['consumable_cost_silver'], 0)}</b> сер. · "
            f"боёв в окне {int(rates['battles_in_window'])}"
        )

        # Knowledge matrix
        parts.append("")
        parts.append("<b>📚 Knowledge Matrix (Exp/Min)</b>")
        if kb_best:
            parts.append(
                f"• Best: <b>{_esc(kb_best.name)}</b> · "
                f"{kb_best.exp_per_min:.1f} Exp/мин · "
                f"Exp/Gold {kb_best.exp_per_gold:.1f} · "
                f"n={kb_best.sample_n}"
            )
        if matrix:
            for row in matrix:
                parts.append(
                    f"  • [{_esc(row.kind)}] {_esc(row.name)}: "
                    f"{row.exp_per_min:.1f}/мин · {row.exp_per_gold:.1f} Exp/Gold"
                )
        else:
            parts.append("• Пока нет замеров — появятся после боёв/ingest hunt_bots")

        # Brain plan
        if r.include_plan and bot.brain.last:
            parts.append("")
            parts.append(bot.brain.last.report_html())

        # Inventory / timers
        if r.include_inventory:
            inv = st.get("inventory") or []
            parts.append("")
            parts.append(
                f"🎒 Предметов: <b>{len(inv)}</b> · "
                f"🧪 {st.get('potions_count', 0)} · "
                f"🎁 лут-тиков: {getattr(bot, '_loot_claimed', 0)}"
            )
            # Top durability wear
            worn = []
            for a in getattr(bot._profile, "equipment", None) or []:
                pct = getattr(a, "durability_percent", 100)
                if pct < 100:
                    worn.append((a.title, pct))
            worn.sort(key=lambda x: x[1])
            for title, pct in worn[:4]:
                parts.append(f"  • {_esc(title)}: {pct:.0f}%")

        if r.include_timers:
            timers = st.get("timers") or []
            if timers:
                parts.append("")
                parts.append("<b>⏱ Таймеры</b>")
                for t in timers[:6]:
                    parts.append(
                        f"• {_esc(t.get('description', '?'))}: "
                        f"<b>{_esc(t.get('remaining', '?'))}</b>"
                    )

        # Effects
        effects = st.get("effects") or []
        if effects:
            parts.append("")
            parts.append(
                "✨ Эффекты: " + ", ".join(_esc(e.get("title", "?")) for e in effects[:6])
            )

        f = bot.settings.farm
        parts.append("")
        parts.append(
            f"🤖 Макс-фарм {bot.settings.on_off(f.max_farm)} · "
            f"Квесты {bot.settings.on_off(f.auto_quests)} · "
            f"Бои {bot.settings.on_off(f.auto_combat)} · "
            f"Лут {bot.settings.on_off(f.auto_loot)}"
        )
        return "\n".join(parts)

    def build_console_summary(self) -> str:
        """Plain-text one-liner block for logger.info."""
        rates = self.telemetry.rates()
        bsum = self.telemetry.battle_stats_summary()
        lv = self.bot.leveling.progress
        return (
            f"Analytics: Lv{lv.level}({lv.exp_pct:.1f}%) "
            f"Exp/h={rates['exp_per_hour']:.0f}/{lv.exp_per_hour:.0f} "
            f"Gold/h={rates['gold_per_hour']:.2f} "
            f"battles={bsum['count']} avg={bsum['avg_duration_sec']:.0f}s "
            f"eff={bsum['avg_efficiency']:.0f} "
            f"SM={get_bot_state().name} "
            f"focus={lv.priority_title or '—'}"
        )

    async def maybe_send_heartbeat(self) -> None:
        """Respect report interval; send full analytics via notify."""
        bot = self.bot
        r = bot.settings.report
        if not r.enabled:
            return
        interval = max(5, int(r.interval_min)) * 60
        if time.time() - bot.settings.last_report_at < interval:
            return
        text = self.build_full_report()
        await bot.notify(text, "heartbeat")
        bot.settings.last_report_at = time.time()
        bot.settings.save()
        logger.info("%s", self.build_console_summary())
