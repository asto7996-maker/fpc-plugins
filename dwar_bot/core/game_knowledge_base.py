"""
Game Knowledge Base — 24/7 SQLite store for mobs, quests, auction & Exp efficiency.

Persists every observed mob / quest / loot sample and continuously recalculates
``Exp/Min`` and ``Exp/Gold`` per area and mob type for the LevelingEngine.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Prefer repo-root data/; fall back beside the package on flat VPS layouts.
_PKG = Path(__file__).resolve().parents[1]  # dwar_bot/
_REPO = _PKG.parent
_DEFAULT_DB_CANDIDATES = (
    _REPO / "data" / "game_knowledge.db",
    _PKG / "data" / "game_knowledge.db",
    Path("/root/data/game_knowledge.db"),
)


def default_db_path() -> Path:
    for p in _DEFAULT_DB_CANDIDATES:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            continue
    return _REPO / "data" / "game_knowledge.db"


@dataclass
class MobRecord:
    mob_id: str
    name: str
    level: int = 0
    hp: int = 0
    area_id: str = ""
    artikul_id: str = ""
    weakness: str = ""
    loot_json: str = "[]"
    exp_reward: float = 0.0
    kills: int = 0
    total_fight_sec: float = 0.0
    total_gold_spent: float = 0.0
    last_seen: float = 0.0

    @property
    def exp_per_min(self) -> float:
        if self.total_fight_sec <= 0 or self.exp_reward <= 0 or self.kills <= 0:
            return 0.0
        # Average fight duration → Exp/Min
        avg_sec = self.total_fight_sec / max(1, self.kills)
        if avg_sec <= 0:
            return 0.0
        return (self.exp_reward / avg_sec) * 60.0

    @property
    def exp_per_gold(self) -> float:
        if self.total_gold_spent <= 0:
            return float("inf") if self.exp_reward > 0 else 0.0
        total_exp = self.exp_reward * max(1, self.kills)
        return total_exp / self.total_gold_spent


@dataclass
class QuestRecord:
    quest_key: str
    title: str
    npc_id: str = ""
    area_id: str = ""
    level_req: int = 0
    exp_reward: float = 0.0
    valor_reward: float = 0.0
    gold_reward: float = 0.0
    required_items_json: str = "[]"
    status: str = "seen"  # seen | available | active | ready | done
    last_seen: float = 0.0
    raw_json: str = "{}"


@dataclass
class AuctionSample:
    item_key: str
    title: str
    price_gold: float
    qty: int = 1
    source: str = "auction"
    sampled_at: float = 0.0


@dataclass
class EfficiencyRow:
    key: str
    kind: str  # mob | area
    name: str
    area_id: str
    exp_per_min: float
    exp_per_gold: float
    sample_n: int
    level: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobs (
    mob_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    level INTEGER DEFAULT 0,
    hp INTEGER DEFAULT 0,
    area_id TEXT DEFAULT '',
    artikul_id TEXT DEFAULT '',
    weakness TEXT DEFAULT '',
    loot_json TEXT DEFAULT '[]',
    exp_reward REAL DEFAULT 0,
    kills INTEGER DEFAULT 0,
    total_fight_sec REAL DEFAULT 0,
    total_gold_spent REAL DEFAULT 0,
    last_seen REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quests (
    quest_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    npc_id TEXT DEFAULT '',
    area_id TEXT DEFAULT '',
    level_req INTEGER DEFAULT 0,
    exp_reward REAL DEFAULT 0,
    valor_reward REAL DEFAULT 0,
    gold_reward REAL DEFAULT 0,
    required_items_json TEXT DEFAULT '[]',
    status TEXT DEFAULT 'seen',
    last_seen REAL DEFAULT 0,
    raw_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS auction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL,
    title TEXT NOT NULL,
    price_gold REAL NOT NULL,
    qty INTEGER DEFAULT 1,
    source TEXT DEFAULT 'auction',
    sampled_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS area_stats (
    area_id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    kills INTEGER DEFAULT 0,
    total_exp REAL DEFAULT 0,
    total_fight_sec REAL DEFAULT 0,
    total_gold_spent REAL DEFAULT 0,
    last_seen REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT DEFAULT 'daily',
    starts_hour INTEGER DEFAULT -1,
    ends_hour INTEGER DEFAULT -1,
    bonus_mult REAL DEFAULT 1.0,
    last_seen REAL DEFAULT 0,
    meta_json TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_mobs_area ON mobs(area_id);
CREATE INDEX IF NOT EXISTS idx_quests_area ON quests(area_id);
CREATE INDEX IF NOT EXISTS idx_auction_item ON auction(item_key, sampled_at);
"""


class GameKnowledgeBase:
    """
    Interactive knowledge parser backed by SQLite.

    Thread-safe for sync ingest from the asyncio loop via ``to_thread`` if needed;
    methods themselves take a short lock around DB access.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(_SCHEMA)
        logger.info("GameKnowledgeBase ready: %s", self.db_path)

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
    # Mobs
    # ------------------------------------------------------------------

    def upsert_mob(
        self,
        *,
        mob_id: str,
        name: str,
        level: int = 0,
        hp: int = 0,
        area_id: str = "",
        artikul_id: str = "",
        weakness: str = "",
        loot: Optional[Iterable[str]] = None,
        exp_reward: float = 0.0,
    ) -> None:
        mid = str(mob_id or "").strip() or str(name or "").strip()
        if not mid:
            return
        now = time.time()
        loot_list = list(loot or [])
        with self._connect() as con:
            row = con.execute("SELECT * FROM mobs WHERE mob_id=?", (mid,)).fetchone()
            if row:
                merged_loot = list(json.loads(row["loot_json"] or "[]"))
                for x in loot_list:
                    if x and x not in merged_loot:
                        merged_loot.append(x)
                con.execute(
                    """
                    UPDATE mobs SET
                        name=?, level=CASE WHEN ? > 0 THEN ? ELSE level END,
                        hp=CASE WHEN ? > 0 THEN ? ELSE hp END,
                        area_id=CASE WHEN ? != '' THEN ? ELSE area_id END,
                        artikul_id=CASE WHEN ? != '' THEN ? ELSE artikul_id END,
                        weakness=CASE WHEN ? != '' THEN ? ELSE weakness END,
                        loot_json=?,
                        exp_reward=CASE WHEN ? > 0 THEN ? ELSE exp_reward END,
                        last_seen=?
                    WHERE mob_id=?
                    """,
                    (
                        name or row["name"],
                        level, level,
                        hp, hp,
                        area_id, area_id,
                        artikul_id, artikul_id,
                        weakness, weakness,
                        json.dumps(merged_loot, ensure_ascii=False),
                        exp_reward, exp_reward,
                        now, mid,
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT INTO mobs (
                        mob_id, name, level, hp, area_id, artikul_id, weakness,
                        loot_json, exp_reward, last_seen
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        mid, name or mid, int(level or 0), int(hp or 0),
                        str(area_id or ""), str(artikul_id or ""), weakness or "",
                        json.dumps(loot_list, ensure_ascii=False),
                        float(exp_reward or 0), now,
                    ),
                )

    def record_kill(
        self,
        *,
        mob_id: str,
        name: str = "",
        area_id: str = "",
        fight_sec: float = 30.0,
        exp_gained: float = 0.0,
        gold_spent: float = 0.0,
        level: int = 0,
        loot: Optional[Iterable[str]] = None,
    ) -> None:
        """Update fight duration / exp samples after a successful kill."""
        mid = str(mob_id or name or "").strip()
        if not mid:
            return
        self.upsert_mob(
            mob_id=mid,
            name=name or mid,
            level=level,
            area_id=area_id,
            loot=loot,
            exp_reward=exp_gained,
        )
        now = time.time()
        with self._connect() as con:
            con.execute(
                """
                UPDATE mobs SET
                    kills = kills + 1,
                    total_fight_sec = total_fight_sec + ?,
                    total_gold_spent = total_gold_spent + ?,
                    exp_reward = CASE WHEN ? > 0 THEN ? ELSE exp_reward END,
                    last_seen = ?
                WHERE mob_id = ?
                """,
                (max(0.0, fight_sec), max(0.0, gold_spent),
                 exp_gained, exp_gained, now, mid),
            )
            aid = str(area_id or "")
            if aid:
                row = con.execute(
                    "SELECT area_id FROM area_stats WHERE area_id=?", (aid,)
                ).fetchone()
                if row:
                    con.execute(
                        """
                        UPDATE area_stats SET
                            kills = kills + 1,
                            total_exp = total_exp + ?,
                            total_fight_sec = total_fight_sec + ?,
                            total_gold_spent = total_gold_spent + ?,
                            last_seen = ?
                        WHERE area_id = ?
                        """,
                        (max(0.0, exp_gained), max(0.0, fight_sec),
                         max(0.0, gold_spent), now, aid),
                    )
                else:
                    con.execute(
                        """
                        INSERT INTO area_stats (
                            area_id, title, kills, total_exp,
                            total_fight_sec, total_gold_spent, last_seen
                        ) VALUES (?,?,1,?,?,?,?)
                        """,
                        (aid, "", max(0.0, exp_gained), max(0.0, fight_sec),
                         max(0.0, gold_spent), now),
                    )

    def ingest_hunt_bots(self, bots: list[dict], *, area_id: str = "") -> int:
        """Parse live hunt_farm bots into the mobs table."""
        n = 0
        for b in bots or []:
            mid = str(b.get("id") or b.get("bot_id") or "").strip()
            name = str(b.get("name") or b.get("title") or "").strip()
            if not mid and not name:
                continue
            try:
                level = int(float(b.get("level") or b.get("lvl") or 0))
            except (TypeError, ValueError):
                level = 0
            try:
                hp = int(float(b.get("hp") or b.get("hp_max") or 0))
            except (TypeError, ValueError):
                hp = 0
            self.upsert_mob(
                mob_id=mid or name,
                name=name or mid,
                level=level,
                hp=hp,
                area_id=str(area_id or b.get("area_id") or ""),
                artikul_id=str(b.get("artikul_id") or ""),
                weakness=str(b.get("weakness") or b.get("w") or ""),
            )
            n += 1
        return n

    def get_mob(self, mob_id: str) -> Optional[MobRecord]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM mobs WHERE mob_id=?", (str(mob_id),)
            ).fetchone()
        return self._row_mob(row) if row else None

    def list_mobs(self, *, area_id: str = "", min_kills: int = 0) -> list[MobRecord]:
        q = "SELECT * FROM mobs WHERE kills >= ?"
        args: list[Any] = [min_kills]
        if area_id:
            q += " AND area_id = ?"
            args.append(str(area_id))
        q += " ORDER BY last_seen DESC"
        with self._connect() as con:
            rows = con.execute(q, args).fetchall()
        return [self._row_mob(r) for r in rows]

    @staticmethod
    def _row_mob(row: sqlite3.Row) -> MobRecord:
        return MobRecord(
            mob_id=row["mob_id"],
            name=row["name"],
            level=int(row["level"] or 0),
            hp=int(row["hp"] or 0),
            area_id=row["area_id"] or "",
            artikul_id=row["artikul_id"] or "",
            weakness=row["weakness"] or "",
            loot_json=row["loot_json"] or "[]",
            exp_reward=float(row["exp_reward"] or 0),
            kills=int(row["kills"] or 0),
            total_fight_sec=float(row["total_fight_sec"] or 0),
            total_gold_spent=float(row["total_gold_spent"] or 0),
            last_seen=float(row["last_seen"] or 0),
        )

    # ------------------------------------------------------------------
    # Quests
    # ------------------------------------------------------------------

    def upsert_quest(
        self,
        *,
        quest_key: str,
        title: str,
        npc_id: str = "",
        area_id: str = "",
        level_req: int = 0,
        exp_reward: float = 0.0,
        valor_reward: float = 0.0,
        gold_reward: float = 0.0,
        required_items: Optional[Iterable[str]] = None,
        status: str = "seen",
        raw: Optional[dict] = None,
    ) -> None:
        qk = str(quest_key or title or "").strip()
        if not qk:
            return
        now = time.time()
        items = list(required_items or [])
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO quests (
                    quest_key, title, npc_id, area_id, level_req,
                    exp_reward, valor_reward, gold_reward,
                    required_items_json, status, last_seen, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(quest_key) DO UPDATE SET
                    title=excluded.title,
                    npc_id=CASE WHEN excluded.npc_id!='' THEN excluded.npc_id ELSE quests.npc_id END,
                    area_id=CASE WHEN excluded.area_id!='' THEN excluded.area_id ELSE quests.area_id END,
                    level_req=CASE WHEN excluded.level_req>0 THEN excluded.level_req ELSE quests.level_req END,
                    exp_reward=CASE WHEN excluded.exp_reward>0 THEN excluded.exp_reward ELSE quests.exp_reward END,
                    valor_reward=CASE WHEN excluded.valor_reward>0 THEN excluded.valor_reward ELSE quests.valor_reward END,
                    gold_reward=CASE WHEN excluded.gold_reward>0 THEN excluded.gold_reward ELSE quests.gold_reward END,
                    required_items_json=CASE
                        WHEN excluded.required_items_json!='[]' THEN excluded.required_items_json
                        ELSE quests.required_items_json END,
                    status=excluded.status,
                    last_seen=excluded.last_seen,
                    raw_json=excluded.raw_json
                """,
                (
                    qk, title or qk, str(npc_id or ""), str(area_id or ""),
                    int(level_req or 0), float(exp_reward or 0),
                    float(valor_reward or 0), float(gold_reward or 0),
                    json.dumps(items, ensure_ascii=False), status, now,
                    json.dumps(raw or {}, ensure_ascii=False),
                ),
            )

    def ingest_npc_quests(
        self,
        quests_payload: Any,
        *,
        npc_id: str = "",
        area_id: str = "",
        char_level: int = 0,
    ) -> int:
        """
        Parse NPC quest list (dict/list from ``npc|quests`` / HTML walker).

        Accepts flexible shapes used by the live client.
        """
        items: list[dict] = []
        if isinstance(quests_payload, dict):
            for key in ("quests", "list", "items", "data"):
                if isinstance(quests_payload.get(key), list):
                    items = quests_payload[key]
                    break
            if not items and quests_payload.get("title"):
                items = [quests_payload]
            elif not items:
                # dict of id → quest
                for k, v in quests_payload.items():
                    if isinstance(v, dict):
                        vv = dict(v)
                        vv.setdefault("id", k)
                        items.append(vv)
        elif isinstance(quests_payload, list):
            items = [x for x in quests_payload if isinstance(x, dict)]

        n = 0
        for q in items:
            title = str(
                q.get("title") or q.get("name") or q.get("quest") or ""
            ).strip()
            qid = str(q.get("id") or q.get("quest_id") or q.get("qid") or title).strip()
            if not title and not qid:
                continue
            try:
                lvl = int(float(q.get("level") or q.get("lvl") or q.get("level_req") or 0))
            except (TypeError, ValueError):
                lvl = 0
            try:
                exp = float(q.get("exp") or q.get("experience") or q.get("exp_reward") or 0)
            except (TypeError, ValueError):
                exp = 0.0
            try:
                valor = float(q.get("valor") or q.get("doблесть") or q.get("glory") or 0)
            except (TypeError, ValueError):
                valor = 0.0
            try:
                gold = float(q.get("gold") or q.get("money") or q.get("reward_gold") or 0)
            except (TypeError, ValueError):
                gold = 0.0
            req_items = q.get("required_items") or q.get("items") or q.get("need") or []
            if isinstance(req_items, str):
                req_items = [req_items]
            if not isinstance(req_items, list):
                req_items = []
            status = str(q.get("status") or "available").lower()
            if char_level and lvl and char_level < lvl:
                status = "locked"
            self.upsert_quest(
                quest_key=f"{npc_id}:{qid}" if npc_id else qid,
                title=title or qid,
                npc_id=npc_id,
                area_id=area_id,
                level_req=lvl,
                exp_reward=exp,
                valor_reward=valor,
                gold_reward=gold,
                required_items=[str(x) for x in req_items],
                status=status,
                raw=q,
            )
            n += 1
        return n

    def list_quests(
        self,
        *,
        area_id: str = "",
        max_level: Optional[int] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> list[QuestRecord]:
        q = "SELECT * FROM quests WHERE 1=1"
        args: list[Any] = []
        if area_id:
            q += " AND area_id = ?"
            args.append(str(area_id))
        if max_level is not None:
            q += " AND (level_req <= ? OR level_req = 0)"
            args.append(int(max_level))
        if statuses:
            marks = ",".join("?" for _ in statuses)
            q += f" AND status IN ({marks})"
            args.extend(list(statuses))
        q += " ORDER BY exp_reward DESC, last_seen DESC"
        with self._connect() as con:
            rows = con.execute(q, args).fetchall()
        out: list[QuestRecord] = []
        for r in rows:
            out.append(QuestRecord(
                quest_key=r["quest_key"],
                title=r["title"],
                npc_id=r["npc_id"] or "",
                area_id=r["area_id"] or "",
                level_req=int(r["level_req"] or 0),
                exp_reward=float(r["exp_reward"] or 0),
                valor_reward=float(r["valor_reward"] or 0),
                gold_reward=float(r["gold_reward"] or 0),
                required_items_json=r["required_items_json"] or "[]",
                status=r["status"] or "seen",
                last_seen=float(r["last_seen"] or 0),
                raw_json=r["raw_json"] or "{}",
            ))
        return out

    # ------------------------------------------------------------------
    # Auction
    # ------------------------------------------------------------------

    def sample_auction(
        self,
        *,
        item_key: str,
        title: str,
        price_gold: float,
        qty: int = 1,
        source: str = "auction",
    ) -> None:
        if not item_key or price_gold < 0:
            return
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO auction (item_key, title, price_gold, qty, source, sampled_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    str(item_key), title or item_key, float(price_gold),
                    int(qty or 1), source, time.time(),
                ),
            )

    def latest_auction_price(self, item_key: str) -> Optional[float]:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT price_gold FROM auction
                WHERE item_key=? ORDER BY sampled_at DESC LIMIT 1
                """,
                (str(item_key),),
            ).fetchone()
        return float(row["price_gold"]) if row else None

    def quest_item_prices(self, quest: QuestRecord) -> dict[str, float]:
        try:
            items = json.loads(quest.required_items_json or "[]")
        except json.JSONDecodeError:
            items = []
        out: dict[str, float] = {}
        for it in items:
            key = str(it)
            price = self.latest_auction_price(key)
            if price is not None:
                out[key] = price
        return out

    # ------------------------------------------------------------------
    # Events / daily schedule
    # ------------------------------------------------------------------

    def upsert_event(
        self,
        *,
        event_key: str,
        title: str,
        kind: str = "daily",
        starts_hour: int = -1,
        ends_hour: int = -1,
        bonus_mult: float = 1.0,
        meta: Optional[dict] = None,
    ) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO events (
                    event_key, title, kind, starts_hour, ends_hour,
                    bonus_mult, last_seen, meta_json
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET
                    title=excluded.title,
                    kind=excluded.kind,
                    starts_hour=excluded.starts_hour,
                    ends_hour=excluded.ends_hour,
                    bonus_mult=excluded.bonus_mult,
                    last_seen=excluded.last_seen,
                    meta_json=excluded.meta_json
                """,
                (
                    event_key, title, kind, int(starts_hour), int(ends_hour),
                    float(bonus_mult), time.time(),
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )

    def active_exp_multiplier(self, hour: Optional[int] = None) -> float:
        h = int(hour if hour is not None else time.localtime().tm_hour)
        mult = 1.0
        with self._connect() as con:
            rows = con.execute("SELECT * FROM events").fetchall()
        for r in rows:
            sh, eh = int(r["starts_hour"]), int(r["ends_hour"])
            if sh < 0 or eh < 0:
                continue
            if sh <= eh:
                active = sh <= h < eh
            else:
                active = h >= sh or h < eh
            if active:
                mult = max(mult, float(r["bonus_mult"] or 1.0))
        return mult

    # ------------------------------------------------------------------
    # Efficiency matrix
    # ------------------------------------------------------------------

    def efficiency_matrix(
        self,
        *,
        char_level: int = 0,
        area_id: str = "",
    ) -> list[EfficiencyRow]:
        """
        Compute Exp/Min and Exp/Gold for mobs (and aggregated areas).

        Mobs far above/below char level are soft-penalised by callers;
        this method returns raw measured rates.
        """
        rows: list[EfficiencyRow] = []
        for m in self.list_mobs(area_id=area_id, min_kills=0):
            epm = m.exp_per_min
            # Heuristic prior when no fight samples yet: level-scaled guess
            if epm <= 0 and m.level > 0:
                # Assume ~45s fight, exp ≈ 40 * mob_level
                guess_exp = float(m.exp_reward or (40.0 * m.level))
                epm = (guess_exp / 45.0) * 60.0
            if char_level and m.level:
                # Prefer ±3 levels of character
                delta = abs(m.level - char_level)
                if delta > 5:
                    epm *= 0.35
                elif delta > 3:
                    epm *= 0.65
            epg = m.exp_per_gold
            if epg == float("inf"):
                epg = epm  # treat free fights as excellent gold efficiency
            rows.append(EfficiencyRow(
                key=m.mob_id,
                kind="mob",
                name=m.name,
                area_id=m.area_id,
                exp_per_min=epm,
                exp_per_gold=epg if epg != float("inf") else epm,
                sample_n=m.kills,
                level=m.level,
            ))

        with self._connect() as con:
            area_rows = con.execute("SELECT * FROM area_stats").fetchall()
        for a in area_rows:
            if area_id and a["area_id"] != area_id:
                continue
            sec = float(a["total_fight_sec"] or 0)
            exp = float(a["total_exp"] or 0)
            gold = float(a["total_gold_spent"] or 0)
            epm = (exp / sec * 60.0) if sec > 0 else 0.0
            epg = (exp / gold) if gold > 0 else epm
            rows.append(EfficiencyRow(
                key=a["area_id"],
                kind="area",
                name=a["title"] or a["area_id"],
                area_id=a["area_id"],
                exp_per_min=epm,
                exp_per_gold=epg,
                sample_n=int(a["kills"] or 0),
            ))

        rows.sort(key=lambda r: (-r.exp_per_min, -r.exp_per_gold))
        return rows

    def best_farm_target(
        self,
        *,
        char_level: int,
        area_id: str = "",
    ) -> Optional[EfficiencyRow]:
        matrix = [
            r for r in self.efficiency_matrix(char_level=char_level, area_id=area_id)
            if r.kind == "mob"
        ]
        return matrix[0] if matrix else None

    def touch_area_title(self, area_id: str, title: str) -> None:
        if not area_id:
            return
        with self._connect() as con:
            row = con.execute(
                "SELECT area_id FROM area_stats WHERE area_id=?", (area_id,)
            ).fetchone()
            if row:
                con.execute(
                    "UPDATE area_stats SET title=?, last_seen=? WHERE area_id=?",
                    (title or row["area_id"], time.time(), area_id),
                )
            else:
                con.execute(
                    """
                    INSERT INTO area_stats (
                        area_id, title, kills, total_exp,
                        total_fight_sec, total_gold_spent, last_seen
                    ) VALUES (?, ?, 0, 0, 0, 0, ?)
                    """,
                    (area_id, title or area_id, time.time()),
                )


# Per-process shared KB (safe: SQLite + RLock). Per-account DBs can pass db_path.
_KB: Optional[GameKnowledgeBase] = None


def get_knowledge_base(db_path: Optional[Path] = None) -> GameKnowledgeBase:
    global _KB
    if db_path is not None:
        return GameKnowledgeBase(db_path)
    if _KB is None:
        _KB = GameKnowledgeBase()
    return _KB
