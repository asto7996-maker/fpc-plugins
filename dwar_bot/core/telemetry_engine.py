"""
TelemetryEngine — exhaustive event tracking for quests, combat and economy.

All metrics are derived from real snapshots (gold, inventory, durability,
battle timers, SQLite knowledge prices) — nothing is fabricated.
Persists to SQLite so reports survive restarts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PKG = Path(__file__).resolve().parents[1]
_REPO = _PKG.parent


def default_telemetry_db(account_id: str = "") -> Path:
    root = _REPO / "data"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = _PKG / "data"
        root.mkdir(parents=True, exist_ok=True)
    if account_id and account_id != "default":
        return root / f"telemetry_{account_id}.db"
    return root / "telemetry.db"


def _fmt_duration(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин {s} сек"
    if m:
        return f"{m} мин {s} сек"
    return f"{s} сек"


@dataclass
class ConsumableUse:
    title: str
    qty: int = 1
    unit_cost_silver: float = 0.0  # 1 gold = 100 silver (game convention)
    kind: str = "potion"  # potion | scroll | other

    @property
    def total_cost_silver(self) -> float:
        return self.unit_cost_silver * self.qty


@dataclass
class LootItem:
    title: str
    qty: int = 1


@dataclass
class InventorySnapshot:
    """Point-in-time bag + wallet + gear wear."""

    ts: float
    gold: float
    potions: dict[str, int] = field(default_factory=dict)  # title → count
    scrolls: dict[str, int] = field(default_factory=dict)
    items: dict[str, int] = field(default_factory=dict)
    equip_durability_pct: float = 100.0
    equip_count: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "InventorySnapshot":
        d = json.loads(raw or "{}")
        return cls(
            ts=float(d.get("ts") or 0),
            gold=float(d.get("gold") or 0),
            potions=dict(d.get("potions") or {}),
            scrolls=dict(d.get("scrolls") or {}),
            items=dict(d.get("items") or {}),
            equip_durability_pct=float(d.get("equip_durability_pct") or 100),
            equip_count=int(d.get("equip_count") or 0),
        )


@dataclass
class QuestTelemetry:
    event_id: str
    title: str
    quest_id: str = ""
    npc_id: str = ""
    area_id: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_sec: float = 0.0
    consumables: list[ConsumableUse] = field(default_factory=list)
    durability_delta_pct: float = 0.0
    gold_spent: float = 0.0
    gold_gained: float = 0.0
    exp_gained: float = 0.0
    exp_pct_of_level: float = 0.0
    valor_gained: float = 0.0
    reputation_gained: float = 0.0
    loot: list[LootItem] = field(default_factory=list)
    status: str = "active"  # active | completed | abandoned
    start_snap: Optional[InventorySnapshot] = None
    end_snap: Optional[InventorySnapshot] = None

    @property
    def net_profit_silver(self) -> float:
        # gold delta in silver (×100) minus consumable costs
        net_gold = self.gold_gained - self.gold_spent
        return net_gold * 100.0 - sum(c.total_cost_silver for c in self.consumables)

    @property
    def exp_per_hour(self) -> float:
        if self.duration_sec <= 0 or self.exp_gained <= 0:
            return 0.0
        return self.exp_gained / self.duration_sec * 3600.0

    def duration_human(self) -> str:
        return _fmt_duration(self.duration_sec)


@dataclass
class BattleTelemetry:
    event_id: str
    source: str = ""  # hunt | area | arena | front | recover
    mob_id: str = ""
    mob_name: str = ""
    area_id: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_sec: float = 0.0
    result: str = ""  # WIN | LOSE | FLED | ERROR
    damage_dealt: int = 0
    damage_taken: int = 0
    hits: int = 0
    misses: int = 0
    crits: int = 0
    attacks: int = 0
    potions_used: int = 0
    elixir_titles: list[str] = field(default_factory=list)
    efficiency_score: float = 0.0  # 0..100
    dps: float = 0.0

    def duration_human(self) -> str:
        return _fmt_duration(self.duration_sec)

    def compute_efficiency(self) -> None:
        if self.duration_sec > 0 and self.damage_dealt > 0:
            self.dps = self.damage_dealt / self.duration_sec
        total_swings = self.hits + self.misses
        hit_rate = (self.hits / total_swings) if total_swings else 1.0
        # Score: DPS weight + hit rate + win bonus − potion penalty
        base = min(70.0, self.dps * 2.5) if self.dps else (40.0 if self.result == "WIN" else 10.0)
        base += hit_rate * 20.0
        if self.result == "WIN":
            base += 10.0
        base -= min(15.0, self.potions_used * 3.0)
        self.efficiency_score = max(0.0, min(100.0, base))


@dataclass
class EconomySnapshot:
    ts: float
    gold: float
    exp_proxy: float = 0.0
    battles: int = 0
    wins: int = 0
    potions_used: int = 0
    quests_completed: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS quest_events (
    event_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    quest_id TEXT DEFAULT '',
    npc_id TEXT DEFAULT '',
    area_id TEXT DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    consumables_json TEXT DEFAULT '[]',
    durability_delta_pct REAL DEFAULT 0,
    gold_spent REAL DEFAULT 0,
    gold_gained REAL DEFAULT 0,
    exp_gained REAL DEFAULT 0,
    exp_pct_of_level REAL DEFAULT 0,
    valor_gained REAL DEFAULT 0,
    reputation_gained REAL DEFAULT 0,
    loot_json TEXT DEFAULT '[]',
    status TEXT DEFAULT 'active',
    start_snap_json TEXT DEFAULT '{}',
    end_snap_json TEXT DEFAULT '{}',
    net_profit_silver REAL DEFAULT 0,
    exp_per_hour REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS battle_events (
    event_id TEXT PRIMARY KEY,
    source TEXT DEFAULT '',
    mob_id TEXT DEFAULT '',
    mob_name TEXT DEFAULT '',
    area_id TEXT DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    result TEXT DEFAULT '',
    damage_dealt INTEGER DEFAULT 0,
    damage_taken INTEGER DEFAULT 0,
    hits INTEGER DEFAULT 0,
    misses INTEGER DEFAULT 0,
    crits INTEGER DEFAULT 0,
    attacks INTEGER DEFAULT 0,
    potions_used INTEGER DEFAULT 0,
    elixir_titles_json TEXT DEFAULT '[]',
    efficiency_score REAL DEFAULT 0,
    dps REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consumable_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    title TEXT NOT NULL,
    kind TEXT DEFAULT 'potion',
    unit_cost_silver REAL DEFAULT 0,
    context TEXT DEFAULT '',
    quest_event_id TEXT DEFAULT '',
    battle_event_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS economy_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    gold REAL NOT NULL,
    exp_proxy REAL DEFAULT 0,
    battles INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    potions_used INTEGER DEFAULT 0,
    quests_completed INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_quest_finished ON quest_events(finished_at);
CREATE INDEX IF NOT EXISTS idx_battle_finished ON battle_events(finished_at);
CREATE INDEX IF NOT EXISTS idx_economy_ts ON economy_snapshots(ts);
"""


class TelemetryEngine:
    """
    Process / account-scoped telemetry store.

    Call ``snapshot_inventory`` with a live ``FullProfile`` whenever starting
    or finishing a tracked activity so diffs are exact.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        account_id: str = "",
        price_lookup: Optional[Any] = None,
    ) -> None:
        self.account_id = account_id
        self.db_path = Path(db_path) if db_path else default_telemetry_db(account_id)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Optional GameKnowledgeBase — used for auction unit costs
        self.price_lookup = price_lookup
        self._lock = threading.RLock()
        self._init_db()

        self.active_quest: Optional[QuestTelemetry] = None
        self.active_battle: Optional[BattleTelemetry] = None
        self._session_started = time.time()
        self._potions_session = 0
        self._gold_at_start: Optional[float] = None
        self._exp_proxy = 0.0
        self._last_economy: Optional[EconomySnapshot] = None

        logger.info("TelemetryEngine ready: %s", self.db_path)

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        with self._lock:
            con = sqlite3.connect(str(self.db_path), timeout=30)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    @staticmethod
    def snapshot_inventory(profile: Any, gold: Optional[float] = None) -> InventorySnapshot:
        """Build an exact bag/wallet/gear snapshot from ``FullProfile``."""
        state = getattr(profile, "state", None)
        g = float(gold if gold is not None else (getattr(state, "money", 0.0) or 0.0))
        potions: dict[str, int] = {}
        scrolls: dict[str, int] = {}
        items: dict[str, int] = {}
        dur_sum = 0.0
        dur_n = 0
        for art in getattr(profile, "inventory", None) or []:
            title = (getattr(art, "title", "") or "").strip() or "?"
            kind = (getattr(art, "kind", "") or "").lower()
            if getattr(art, "is_potion", False) or "отвар" in kind or "эликсир" in kind or "зелье" in kind:
                potions[title] = potions.get(title, 0) + 1
            elif "свиток" in kind or "свиток" in title.lower():
                scrolls[title] = scrolls.get(title, 0) + 1
            else:
                items[title] = items.get(title, 0) + 1
            if getattr(art, "is_equipment", False) or getattr(art, "durability_max", 0):
                dmax = int(getattr(art, "durability_max", 0) or 0)
                if dmax > 0:
                    dur_sum += float(getattr(art, "durability", 0) or 0) / dmax * 100.0
                    dur_n += 1
        return InventorySnapshot(
            ts=time.time(),
            gold=g,
            potions=potions,
            scrolls=scrolls,
            items=items,
            equip_durability_pct=(dur_sum / dur_n) if dur_n else 100.0,
            equip_count=dur_n,
        )

    def _unit_cost_silver(self, title: str) -> float:
        """Resolve unit cost from knowledge-base auction samples (gold→silver)."""
        if not self.price_lookup:
            return 0.0
        try:
            price_gold = self.price_lookup.latest_auction_price(title)
            if price_gold is None:
                # try loose key
                price_gold = self.price_lookup.latest_auction_price(title.split()[0])
            if price_gold is None:
                return 0.0
            return float(price_gold) * 100.0
        except Exception:
            return 0.0

    @staticmethod
    def _diff_counts(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
        """Items consumed (count decreased)."""
        out: dict[str, int] = {}
        for k, v in before.items():
            delta = v - after.get(k, 0)
            if delta > 0:
                out[k] = delta
        return out

    @staticmethod
    def _diff_gained(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
        out: dict[str, int] = {}
        for k, v in after.items():
            delta = v - before.get(k, 0)
            if delta > 0:
                out[k] = delta
        return out

    # ------------------------------------------------------------------
    # Consumables
    # ------------------------------------------------------------------

    def note_consumable(
        self,
        title: str,
        *,
        kind: str = "potion",
        qty: int = 1,
        context: str = "",
    ) -> ConsumableUse:
        title = (title or "расходник").strip()
        use = ConsumableUse(
            title=title,
            qty=qty,
            unit_cost_silver=self._unit_cost_silver(title),
            kind=kind,
        )
        self._potions_session += qty if kind == "potion" else 0
        qid = self.active_quest.event_id if self.active_quest else ""
        bid = self.active_battle.event_id if self.active_battle else ""
        if self.active_quest and self.active_quest.status == "active":
            # merge into quest consumables
            for existing in self.active_quest.consumables:
                if existing.title == title and existing.kind == kind:
                    existing.qty += qty
                    break
            else:
                self.active_quest.consumables.append(use)
        if self.active_battle:
            self.active_battle.potions_used += qty if kind == "potion" else 0
            if title not in self.active_battle.elixir_titles:
                self.active_battle.elixir_titles.append(title)

        with self._connect() as con:
            con.execute(
                """
                INSERT INTO consumable_log
                    (ts, title, kind, unit_cost_silver, context, quest_event_id, battle_event_id)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    time.time(), title, kind, use.unit_cost_silver,
                    context, qid, bid,
                ),
            )
        logger.debug(
            "telemetry consumable: %s x%d (%.1f сер.) ctx=%s",
            title, qty, use.total_cost_silver, context,
        )
        return use

    # ------------------------------------------------------------------
    # Quests
    # ------------------------------------------------------------------

    def start_quest(
        self,
        title: str,
        *,
        quest_id: str = "",
        npc_id: str = "",
        area_id: str = "",
        profile: Any = None,
        gold: Optional[float] = None,
    ) -> QuestTelemetry:
        # Finish previous unfinished quest as abandoned (keep data)
        if self.active_quest and self.active_quest.status == "active":
            self.active_quest.status = "abandoned"
            self._persist_quest(self.active_quest)

        snap = self.snapshot_inventory(profile, gold) if profile is not None else None
        now = time.time()
        qt = QuestTelemetry(
            event_id=uuid.uuid4().hex[:16],
            title=title or "Квест",
            quest_id=str(quest_id or ""),
            npc_id=str(npc_id or ""),
            area_id=str(area_id or ""),
            started_at=now,
            status="active",
            start_snap=snap,
        )
        self.active_quest = qt
        self._persist_quest(qt)
        logger.info(
            "telemetry quest START «%s» at %s (gold=%.2f dur=%.1f%%)",
            qt.title,
            time.strftime("%H:%M:%S", time.localtime(now)),
            snap.gold if snap else -1,
            snap.equip_durability_pct if snap else -1,
        )
        return qt

    def ensure_quest(
        self,
        title: str,
        *,
        quest_id: str = "",
        npc_id: str = "",
        area_id: str = "",
        profile: Any = None,
        gold: Optional[float] = None,
    ) -> QuestTelemetry:
        if self.active_quest and self.active_quest.status == "active":
            # Update title if we learned a better name
            if title and title not in (self.active_quest.title, "Квест"):
                if self.active_quest.title in ("", "Квест", "Сюжетный квест"):
                    self.active_quest.title = title
            return self.active_quest
        return self.start_quest(
            title, quest_id=quest_id, npc_id=npc_id, area_id=area_id,
            profile=profile, gold=gold,
        )

    def complete_quest(
        self,
        *,
        profile: Any = None,
        gold: Optional[float] = None,
        exp_gained: float = 0.0,
        exp_pct_of_level: float = 0.0,
        valor_gained: float = 0.0,
        reputation_gained: float = 0.0,
        loot: Optional[list[LootItem]] = None,
        title: Optional[str] = None,
    ) -> Optional[QuestTelemetry]:
        qt = self.active_quest
        if not qt or qt.status != "active":
            # Synthesize a minimal completed event from last start if missing
            if profile is None:
                return None
            qt = self.start_quest(title or "Квест", profile=profile, gold=gold)

        end = self.snapshot_inventory(profile, gold) if profile is not None else None
        now = time.time()
        qt.finished_at = now
        qt.duration_sec = max(0.0, now - qt.started_at)
        qt.status = "completed"
        if title:
            qt.title = title
        qt.end_snap = end

        if qt.start_snap and end:
            # Consumables from inventory diff (authoritative) — merge with live notes
            used_p = self._diff_counts(qt.start_snap.potions, end.potions)
            used_s = self._diff_counts(qt.start_snap.scrolls, end.scrolls)
            for t, qty in used_p.items():
                self._merge_consumable(qt, t, qty, "potion")
            for t, qty in used_s.items():
                self._merge_consumable(qt, t, qty, "scroll")

            gold_delta = end.gold - qt.start_snap.gold
            if gold_delta >= 0:
                qt.gold_gained = gold_delta
                qt.gold_spent = 0.0
            else:
                qt.gold_spent = -gold_delta
                qt.gold_gained = 0.0

            qt.durability_delta_pct = end.equip_durability_pct - qt.start_snap.equip_durability_pct

            gained_items = self._diff_gained(qt.start_snap.items, end.items)
            # Also potions gained count as loot
            for t, qty in self._diff_gained(qt.start_snap.potions, end.potions).items():
                gained_items[t] = gained_items.get(t, 0) + qty
            qt.loot = [LootItem(title=t, qty=q) for t, q in gained_items.items()]

        if loot:
            # explicit loot overrides/extends
            known = {x.title: x for x in qt.loot}
            for li in loot:
                if li.title in known:
                    known[li.title].qty = max(known[li.title].qty, li.qty)
                else:
                    qt.loot.append(li)

        qt.exp_gained = float(exp_gained or qt.exp_gained)
        qt.exp_pct_of_level = float(exp_pct_of_level or qt.exp_pct_of_level)
        qt.valor_gained = float(valor_gained or qt.valor_gained)
        qt.reputation_gained = float(reputation_gained or qt.reputation_gained)

        self._exp_proxy += qt.exp_gained
        self._persist_quest(qt)
        logger.info(
            "telemetry quest DONE «%s» duration=%s exp=%.0f net=%.0f сер.",
            qt.title, qt.duration_human(), qt.exp_gained, qt.net_profit_silver,
        )
        self.active_quest = None
        return qt

    def _merge_consumable(self, qt: QuestTelemetry, title: str, qty: int, kind: str) -> None:
        for c in qt.consumables:
            if c.title == title and c.kind == kind:
                c.qty = max(c.qty, qty)
                if c.unit_cost_silver <= 0:
                    c.unit_cost_silver = self._unit_cost_silver(title)
                return
        qt.consumables.append(ConsumableUse(
            title=title, qty=qty, kind=kind,
            unit_cost_silver=self._unit_cost_silver(title),
        ))

    def _persist_quest(self, qt: QuestTelemetry) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO quest_events (
                    event_id, title, quest_id, npc_id, area_id,
                    started_at, finished_at, duration_sec, consumables_json,
                    durability_delta_pct, gold_spent, gold_gained,
                    exp_gained, exp_pct_of_level, valor_gained, reputation_gained,
                    loot_json, status, start_snap_json, end_snap_json,
                    net_profit_silver, exp_per_hour
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    title=excluded.title,
                    finished_at=excluded.finished_at,
                    duration_sec=excluded.duration_sec,
                    consumables_json=excluded.consumables_json,
                    durability_delta_pct=excluded.durability_delta_pct,
                    gold_spent=excluded.gold_spent,
                    gold_gained=excluded.gold_gained,
                    exp_gained=excluded.exp_gained,
                    exp_pct_of_level=excluded.exp_pct_of_level,
                    valor_gained=excluded.valor_gained,
                    reputation_gained=excluded.reputation_gained,
                    loot_json=excluded.loot_json,
                    status=excluded.status,
                    end_snap_json=excluded.end_snap_json,
                    net_profit_silver=excluded.net_profit_silver,
                    exp_per_hour=excluded.exp_per_hour
                """,
                (
                    qt.event_id, qt.title, qt.quest_id, qt.npc_id, qt.area_id,
                    qt.started_at, qt.finished_at, qt.duration_sec,
                    json.dumps([asdict(c) for c in qt.consumables], ensure_ascii=False),
                    qt.durability_delta_pct, qt.gold_spent, qt.gold_gained,
                    qt.exp_gained, qt.exp_pct_of_level, qt.valor_gained, qt.reputation_gained,
                    json.dumps([asdict(x) for x in qt.loot], ensure_ascii=False),
                    qt.status,
                    qt.start_snap.to_json() if qt.start_snap else "{}",
                    qt.end_snap.to_json() if qt.end_snap else "{}",
                    qt.net_profit_silver, qt.exp_per_hour,
                ),
            )

    def latest_completed_quest(self) -> Optional[QuestTelemetry]:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT * FROM quest_events WHERE status='completed'
                ORDER BY finished_at DESC LIMIT 1
                """
            ).fetchone()
        return self._row_quest(row) if row else None

    def _row_quest(self, row: sqlite3.Row) -> QuestTelemetry:
        cons = [ConsumableUse(**c) for c in json.loads(row["consumables_json"] or "[]")]
        loot = [LootItem(**x) for x in json.loads(row["loot_json"] or "[]")]
        return QuestTelemetry(
            event_id=row["event_id"],
            title=row["title"],
            quest_id=row["quest_id"] or "",
            npc_id=row["npc_id"] or "",
            area_id=row["area_id"] or "",
            started_at=float(row["started_at"] or 0),
            finished_at=float(row["finished_at"] or 0),
            duration_sec=float(row["duration_sec"] or 0),
            consumables=cons,
            durability_delta_pct=float(row["durability_delta_pct"] or 0),
            gold_spent=float(row["gold_spent"] or 0),
            gold_gained=float(row["gold_gained"] or 0),
            exp_gained=float(row["exp_gained"] or 0),
            exp_pct_of_level=float(row["exp_pct_of_level"] or 0),
            valor_gained=float(row["valor_gained"] or 0),
            reputation_gained=float(row["reputation_gained"] or 0),
            loot=loot,
            status=row["status"] or "",
            start_snap=InventorySnapshot.from_json(row["start_snap_json"] or "{}"),
            end_snap=InventorySnapshot.from_json(row["end_snap_json"] or "{}"),
        )

    # ------------------------------------------------------------------
    # Battles
    # ------------------------------------------------------------------

    def start_battle(
        self,
        *,
        source: str = "",
        mob_id: str = "",
        mob_name: str = "",
        area_id: str = "",
        potions_baseline: int = 0,
        attacks_baseline: int = 0,
    ) -> BattleTelemetry:
        if self.active_battle and not self.active_battle.finished_at:
            # close as ERROR if overlapping
            self.end_battle(result="ERROR", potions_total=potions_baseline, attacks_total=attacks_baseline)

        bt = BattleTelemetry(
            event_id=uuid.uuid4().hex[:16],
            source=source,
            mob_id=str(mob_id or ""),
            mob_name=str(mob_name or ""),
            area_id=str(area_id or ""),
            started_at=time.time(),
        )
        # stash baselines on object via private attrs
        bt._potions_baseline = potions_baseline  # type: ignore[attr-defined]
        bt._attacks_baseline = attacks_baseline  # type: ignore[attr-defined]
        self.active_battle = bt
        return bt

    def note_battle_hit(
        self,
        *,
        damage: int = 0,
        missed: bool = False,
        critical: bool = False,
        taken: int = 0,
    ) -> None:
        bt = self.active_battle
        if not bt:
            return
        if missed:
            bt.misses += 1
        else:
            bt.hits += 1
            bt.damage_dealt += max(0, int(damage))
            if critical:
                bt.crits += 1
        bt.damage_taken += max(0, int(taken))

    def end_battle(
        self,
        *,
        result: str,
        potions_total: Optional[int] = None,
        attacks_total: Optional[int] = None,
        damage_dealt: Optional[int] = None,
        damage_taken: Optional[int] = None,
        hits: Optional[int] = None,
        misses: Optional[int] = None,
    ) -> Optional[BattleTelemetry]:
        bt = self.active_battle
        if not bt:
            return None
        now = time.time()
        bt.finished_at = now
        bt.duration_sec = max(0.0, now - bt.started_at)
        bt.result = result
        base_p = int(getattr(bt, "_potions_baseline", 0) or 0)
        base_a = int(getattr(bt, "_attacks_baseline", 0) or 0)
        if potions_total is not None:
            bt.potions_used = max(bt.potions_used, int(potions_total) - base_p)
        if attacks_total is not None:
            bt.attacks = max(0, int(attacks_total) - base_a)
        if damage_dealt is not None:
            bt.damage_dealt = int(damage_dealt)
        if damage_taken is not None:
            bt.damage_taken = int(damage_taken)
        if hits is not None:
            bt.hits = int(hits)
        if misses is not None:
            bt.misses = int(misses)
        # If no hit telemetry, approximate from attacks
        if bt.hits == 0 and bt.misses == 0 and bt.attacks:
            bt.hits = bt.attacks
        bt.compute_efficiency()
        self._persist_battle(bt)
        logger.info(
            "telemetry battle %s «%s» %s dur=%s dps=%.1f eff=%.0f pots=%d",
            result, bt.mob_name or bt.source, bt.source,
            bt.duration_human(), bt.dps, bt.efficiency_score, bt.potions_used,
        )
        self.active_battle = None
        return bt

    def _persist_battle(self, bt: BattleTelemetry) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO battle_events (
                    event_id, source, mob_id, mob_name, area_id,
                    started_at, finished_at, duration_sec, result,
                    damage_dealt, damage_taken, hits, misses, crits, attacks,
                    potions_used, elixir_titles_json, efficiency_score, dps
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    finished_at=excluded.finished_at,
                    duration_sec=excluded.duration_sec,
                    result=excluded.result,
                    damage_dealt=excluded.damage_dealt,
                    damage_taken=excluded.damage_taken,
                    hits=excluded.hits,
                    misses=excluded.misses,
                    crits=excluded.crits,
                    attacks=excluded.attacks,
                    potions_used=excluded.potions_used,
                    elixir_titles_json=excluded.elixir_titles_json,
                    efficiency_score=excluded.efficiency_score,
                    dps=excluded.dps
                """,
                (
                    bt.event_id, bt.source, bt.mob_id, bt.mob_name, bt.area_id,
                    bt.started_at, bt.finished_at, bt.duration_sec, bt.result,
                    bt.damage_dealt, bt.damage_taken, bt.hits, bt.misses, bt.crits, bt.attacks,
                    bt.potions_used,
                    json.dumps(bt.elixir_titles, ensure_ascii=False),
                    bt.efficiency_score, bt.dps,
                ),
            )

    # ------------------------------------------------------------------
    # Economy / rates
    # ------------------------------------------------------------------

    def note_economy(
        self,
        *,
        gold: float,
        exp_proxy: float = 0.0,
        battles: int = 0,
        wins: int = 0,
        potions_used: int = 0,
        quests_completed: int = 0,
    ) -> EconomySnapshot:
        if self._gold_at_start is None:
            self._gold_at_start = float(gold)
        snap = EconomySnapshot(
            ts=time.time(),
            gold=float(gold),
            exp_proxy=float(exp_proxy or self._exp_proxy),
            battles=int(battles),
            wins=int(wins),
            potions_used=int(potions_used),
            quests_completed=int(quests_completed),
        )
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO economy_snapshots
                    (ts, gold, exp_proxy, battles, wins, potions_used, quests_completed)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    snap.ts, snap.gold, snap.exp_proxy, snap.battles,
                    snap.wins, snap.potions_used, snap.quests_completed,
                ),
            )
        self._last_economy = snap
        return snap

    def rates(self, window_sec: float = 3600.0) -> dict[str, float]:
        """
        Compute Gold/hr, Exp/hr, consumable burn and avg battle duration
        from real SQLite samples inside ``window_sec``.
        """
        now = time.time()
        since = now - window_sec
        with self._connect() as con:
            ecos = con.execute(
                """
                SELECT ts, gold, exp_proxy, potions_used FROM economy_snapshots
                WHERE ts >= ? ORDER BY ts ASC
                """,
                (since,),
            ).fetchall()
            battles = con.execute(
                """
                SELECT duration_sec, potions_used, efficiency_score, dps, result
                FROM battle_events
                WHERE finished_at >= ? AND finished_at > 0
                """,
                (since,),
            ).fetchall()
            cons = con.execute(
                """
                SELECT SUM(unit_cost_silver) AS cost FROM consumable_log WHERE ts >= ?
                """,
                (since,),
            ).fetchone()

        gold_hr = 0.0
        exp_hr = 0.0
        if len(ecos) >= 2:
            t0, g0, e0 = ecos[0]["ts"], ecos[0]["gold"], ecos[0]["exp_proxy"]
            t1, g1, e1 = ecos[-1]["ts"], ecos[-1]["gold"], ecos[-1]["exp_proxy"]
            dt = max(1.0, t1 - t0)
            gold_hr = (g1 - g0) / dt * 3600.0
            exp_hr = (e1 - e0) / dt * 3600.0
        elif self._gold_at_start is not None and self._last_economy:
            dt = max(1.0, now - self._session_started)
            gold_hr = (self._last_economy.gold - self._gold_at_start) / dt * 3600.0
            exp_hr = self._exp_proxy / dt * 3600.0

        durations = [float(b["duration_sec"]) for b in battles if float(b["duration_sec"] or 0) > 0]
        avg_battle = sum(durations) / len(durations) if durations else 0.0
        wins = sum(1 for b in battles if b["result"] == "WIN")
        effs = [float(b["efficiency_score"]) for b in battles if b["efficiency_score"]]
        dps_vals = [float(b["dps"]) for b in battles if float(b["dps"] or 0) > 0]
        potion_cost = float((cons["cost"] if cons and cons["cost"] is not None else 0) or 0)

        return {
            "gold_per_hour": gold_hr,
            "exp_per_hour": exp_hr,
            "avg_battle_sec": avg_battle,
            "battles_in_window": float(len(battles)),
            "wins_in_window": float(wins),
            "avg_efficiency": (sum(effs) / len(effs)) if effs else 0.0,
            "avg_dps": (sum(dps_vals) / len(dps_vals)) if dps_vals else 0.0,
            "consumable_cost_silver": potion_cost,
            "net_gold_per_hour": gold_hr - (potion_cost / 100.0) / max(window_sec / 3600.0, 1e-6),
            "window_sec": window_sec,
            "session_uptime_sec": now - self._session_started,
        }

    def battle_stats_summary(self, limit: int = 50) -> dict[str, Any]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT * FROM battle_events
                WHERE finished_at > 0
                ORDER BY finished_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        if not rows:
            return {
                "count": 0, "wins": 0, "losses": 0,
                "avg_duration_sec": 0.0, "avg_dps": 0.0,
                "avg_efficiency": 0.0, "avg_potions": 0.0,
                "miss_rate": 0.0,
            }
        wins = sum(1 for r in rows if r["result"] == "WIN")
        losses = sum(1 for r in rows if r["result"] == "LOSE")
        durs = [float(r["duration_sec"]) for r in rows]
        dps = [float(r["dps"]) for r in rows]
        eff = [float(r["efficiency_score"]) for r in rows]
        pots = [int(r["potions_used"]) for r in rows]
        hits = sum(int(r["hits"] or 0) for r in rows)
        misses = sum(int(r["misses"] or 0) for r in rows)
        swings = hits + misses
        return {
            "count": len(rows),
            "wins": wins,
            "losses": losses,
            "avg_duration_sec": sum(durs) / len(durs),
            "avg_dps": sum(dps) / len(dps) if dps else 0.0,
            "avg_efficiency": sum(eff) / len(eff) if eff else 0.0,
            "avg_potions": sum(pots) / len(pots) if pots else 0.0,
            "miss_rate": (misses / swings * 100.0) if swings else 0.0,
        }

    def auction_delta(self, item_key: str, lookback_sec: float = 86400.0) -> Optional[dict[str, float]]:
        """Price dynamics from knowledge-base auction table if available."""
        kb = self.price_lookup
        if not kb:
            return None
        try:
            with kb._connect() as con:  # noqa: SLF001 — intentional shared DB access
                rows = con.execute(
                    """
                    SELECT price_gold, sampled_at FROM auction
                    WHERE item_key=? AND sampled_at >= ?
                    ORDER BY sampled_at ASC
                    """,
                    (item_key, time.time() - lookback_sec),
                ).fetchall()
        except Exception as exc:
            logger.debug("auction_delta: %s", exc)
            return None
        if len(rows) < 2:
            latest = kb.latest_auction_price(item_key)
            if latest is None:
                return None
            return {"first": latest, "last": latest, "delta": 0.0, "samples": 1.0}
        first = float(rows[0]["price_gold"])
        last = float(rows[-1]["price_gold"])
        return {
            "first": first,
            "last": last,
            "delta": last - first,
            "samples": float(len(rows)),
        }
