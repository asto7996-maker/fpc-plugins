"""
Fight client — completes dwar.ru battles over the HTML5 fight WebSocket.

Protocol (from ``js/canvas/canvas.all.js``)
------------------------------------------
1. Connect ``wss://<wsHost>`` (usually ``w1.dwar.ru/wsproxy/``)
2. Send ``{"event":"connect","host":"<fsrv>","port":5050}``
3. Wait for ``{"event":"connected"}``
4. Exchange hex-encoded ServerParser packets via
   ``{"event":"message","data":"<hex>"}`` (null-separated frames)

Client opcodes: FS_SCCL_INIT=101, STATE=102, ATTACK=105, …
Events: FS_PE_ATTACKNOW=103 → our turn; FS_PE_FIGHTOVER=108 → done.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from dwar_bot.config import COMBAT
from dwar_bot.core.game_client import DwarGameClient
from dwar_bot.modules.battle_strategy import (
    DEFAULT_HIT_SEQ,
    FS_SCCL_CHANGE_MODE,
    MIDDLE_ATTACK_ID,
    TO_FS_PF_DEFENDED,
    TO_FS_PF_MAGIC,
    FightBrain,
    pick_hit_sequence,
)

logger = logging.getLogger(__name__)

HealCallback = Callable[[int, int], Awaitable[bool]]

# Param types
PT_INT = 1
PT_STRING = 3
PT_NINT = 5
PT_BIGINT = 9
PT_NBIGINT = 10

# Client → server
FS_SCCL_INIT = 101
FS_SCCL_STATE = 102
FS_SCCL_ATTACK = 105
FS_SCCL_SKIP_TURN = 114

# Server → client (event when cmd == FS_SC_NONE)
FS_SC_NONE = 0
FS_PE_ATTACKNOW = 103
FS_PE_ATTACKWAIT = 104
FS_PE_ATTACK = 105
FS_PE_ATTACKTIMEOUT = 106
FS_PE_FIGHTOVER = 108
FS_PE_DAMAGE = 112

# Marker: DwarBOT-adapted hit seq + block + damage telemetry
FIGHT_STRATEGY_DWARBOT_V1 = True

_SPRINTF_ARR = [
    "", "", "", "",
    "0000", "000", "00", "0", "",
    "0000000", "000000", "00000", "0000", "000", "00", "0", "",
    "000000000000000", "00000000000000", "0000000000000", "000000000000",
    "00000000000", "0000000000", "000000000", "00000000", "0000000",
    "000000", "00000", "0000", "000", "00", "0", "",
]


def _sprintf(width: int, value: int) -> str:
    hx = format(int(value), "x")
    idx = len(hx) + width
    if idx >= len(_SPRINTF_ARR):
        return hx.zfill(width)
    return _SPRINTF_ARR[idx] + hx


def pack_params(fields: list[tuple[int, int, Any]]) -> str:
    """Build a ServerParser hex packet (id, type, val)… with length prefix."""
    body = ""
    for pid, ptype, val in fields:
        typ = int(ptype)
        v = val
        if typ == PT_INT and isinstance(v, (int, float)) and int(v) < 0:
            typ = PT_NINT
            v = -int(v)
        elif typ == PT_BIGINT and isinstance(v, (int, float)) and int(v) < 0:
            typ = PT_NBIGINT
            v = -int(v)
        body += _sprintf(4, (int(pid) << 8) + (typ & 255))
        if typ in (PT_INT, PT_NINT):
            body += _sprintf(8, int(v))
        elif typ in (PT_BIGINT, PT_NBIGINT):
            body += _sprintf(16, int(v))
        elif typ == PT_STRING:
            s = str(v)
            body += _sprintf(4, len(s)) + s
        else:
            raise ValueError(f"unsupported pack type {typ}")
    return _sprintf(4, len(body)) + body


def unpack_params(packet: str) -> list[int | str]:
    """Best-effort unpack → list of values (ints / strings)."""
    if not packet or len(packet) < 4:
        return []
    try:
        total = int(packet[:4], 16)
    except ValueError:
        return []
    data = packet[4:4 + total]
    values: list[int | str] = []
    r = 0
    n = len(data)
    while r + 4 <= n:
        try:
            hdr = int(data[r:r + 4], 16)
        except ValueError:
            break
        typ = hdr & 255
        r += 4
        if typ in (PT_INT, PT_NINT):
            if r + 8 > n:
                break
            s = int(data[r:r + 8], 16)
            if typ == PT_NINT:
                s = -s
            values.append(s)
            r += 8
        elif typ in (PT_BIGINT, PT_NBIGINT):
            if r + 16 > n:
                break
            s = int(data[r:r + 16], 16)
            if typ == PT_NBIGINT:
                s = -s
            values.append(s)
            r += 16
        elif typ == PT_STRING:
            if r + 4 > n:
                break
            ln = int(data[r:r + 4], 16)
            r += 4
            values.append(data[r:r + ln])
            r += ln
        else:
            # unknown — stop rather than desync
            break
    return values


@dataclass
class FightOutcome:
    won: bool = False
    finished: bool = False
    error: str = ""
    attacks: int = 0
    url_finish: str = ""
    damage_dealt: int = 0
    damage_taken: int = 0
    blocks_toggled: int = 0
    hit_seq: str = ""


class FightClient:
    """Drive one fight to completion via wsproxy."""

    def __init__(self, client: DwarGameClient) -> None:
        self._client = client

    def _build_brain(
        self,
        conf: dict,
        *,
        pers_id: int = 0,
        hit_zone: Optional[int] = None,
        level: int = 1,
    ) -> FightBrain:
        """DwarBOT + BotMek-adapted brain: combo/hit seq + block + stance."""
        configured = getattr(COMBAT, "hit_list", None)
        botmek_fb = None
        botmek_name = ""
        want_magic = False
        suis_fb = None
        suis_label = ""
        if getattr(COMBAT, "botmek_enabled", True):
            try:
                from dwar_bot.modules.botmek_presets import build_fight_plan
                plan = build_fight_plan(
                    level=level,
                    enabled=True,
                    preset_name=str(getattr(COMBAT, "botmek_preset", "") or ""),
                )
                if plan:
                    botmek_fb = plan.fallback_hit_seq()
                    botmek_name = plan.preset.name
                    want_magic = bool(plan.enter_magic_stance)
            except Exception as exc:
                logger.debug("botmek plan: %s", exc)
        if getattr(COMBAT, "suis_enabled", True):
            try:
                from dwar_bot.modules.suis_knowledge import (
                    default_suis_sequence,
                    suis_sequence_to_hit_list,
                )
                raw_seq = str(getattr(COMBAT, "suis_sequence", "") or "").strip()
                if not raw_seq:
                    raw_seq = default_suis_sequence(level)
                suis_fb = suis_sequence_to_hit_list(raw_seq)
                suis_label = f"suis:{raw_seq}"
            except Exception as exc:
                logger.debug("suis seq: %s", exc)

        if hit_zone is not None and hit_zone in (1, 2, 3):
            seq = [int(hit_zone)]
        elif getattr(COMBAT, "prefer_fight_combo", True):
            seq = pick_hit_sequence(
                conf,
                configured=configured,
                botmek_fallback=botmek_fb,
                suis_fallback=suis_fb,
                source_label=suis_label or botmek_name,
            )
        else:
            seq = pick_hit_sequence(
                None,
                configured=configured,
                botmek_fallback=botmek_fb,
                suis_fallback=suis_fb,
                source_label=suis_label or botmek_name,
            )
        brain = FightBrain(
            hit_seq=list(seq or DEFAULT_HIT_SEQ),
            block_hp_percent=float(getattr(COMBAT, "hp_block_threshold", 45.0)),
            unblock_hp_percent=float(getattr(COMBAT, "hp_unblock_threshold", 60.0)),
            unblock_before_finisher=bool(
                getattr(COMBAT, "unblock_before_finisher", True)
            ),
            elixir_hp_percent=float(getattr(COMBAT, "hp_elixir_threshold", 55.0)),
            pers_id=int(pers_id or 0),
            want_magic_stance=want_magic,
            botmek_preset=botmek_name or suis_label,
        )
        if botmek_name or suis_label:
            logger.info(
                "Fight brain BotMek=%s SUIS=%s magic=%s",
                botmek_name or "-", suis_label or "-", want_magic,
            )
        return brain

    async def complete_current_fight(
        self,
        *,
        timeout: float = 180.0,
        hit_zone: Optional[int] = None,
        brain: Optional[FightBrain] = None,
        level: int = 1,
        heal_callback: Optional[HealCallback] = None,
    ) -> FightOutcome:
        # FIGHT_WS_SERIAL_V1 — one reconnect if WS drops mid-fight
        st = await self._client.get_state()
        if not st.fight_id:
            return FightOutcome(error="not in fight")
        conf = await self._client.get_fight_conf(st.fight_id)
        vars_ = conf.get("swf_fight_vars") or {}
        if not vars_:
            return FightOutcome(error="no fight conf vars")

        pers_id = int(vars_.get("pers_id") or 0)
        if brain is None:
            brain = self._build_brain(
                conf, pers_id=pers_id, hit_zone=hit_zone, level=level,
            )
        else:
            brain.pers_id = int(pers_id or brain.pers_id or 0)
            # Upgrade sequence from live fight combos, keep HP seed / block state
            botmek_fb = None
            botmek_name = brain.botmek_preset
            if getattr(COMBAT, "botmek_enabled", True):
                try:
                    from dwar_bot.modules.botmek_presets import build_fight_plan
                    plan = build_fight_plan(
                        level=level,
                        enabled=True,
                        preset_name=str(getattr(COMBAT, "botmek_preset", "") or ""),
                    )
                    if plan:
                        botmek_fb = plan.fallback_hit_seq()
                        botmek_name = plan.preset.name
                        brain.want_magic_stance = bool(plan.enter_magic_stance)
                        brain.botmek_preset = botmek_name
                except Exception:
                    pass
            if hit_zone is None and getattr(COMBAT, "prefer_fight_combo", True):
                try:
                    brain.hit_seq = pick_hit_sequence(
                        conf,
                        configured=getattr(COMBAT, "hit_list", None),
                        botmek_fallback=botmek_fb,
                        source_label=botmek_name,
                    )
                except Exception:
                    pass
            elif hit_zone in (1, 2, 3):
                brain.hit_seq = [int(hit_zone)]
            brain.block_hp_percent = float(
                getattr(COMBAT, "hp_block_threshold", brain.block_hp_percent)
            )
            brain.unblock_hp_percent = float(
                getattr(COMBAT, "hp_unblock_threshold", brain.unblock_hp_percent)
            )
            brain.elixir_hp_percent = float(
                getattr(COMBAT, "hp_elixir_threshold", brain.elixir_hp_percent or 55.0)
            )

        last = FightOutcome(error="fight not started")
        attempts = 2
        per_try = max(45.0, float(timeout) / attempts)
        for attempt in range(1, attempts + 1):
            last = await self.run_fight(
                vars_,
                timeout=per_try,
                hit_zone=hit_zone,
                brain=brain,
                conf=conf,
                heal_callback=heal_callback,
            )
            if last.finished:
                return last
            # Left the fight somehow (server ended it)
            try:
                st2 = await self._client.get_state()
                if not st2.fight_id:
                    last.finished = True
                    last.won = True
                    last.error = ""
                    return last
            except Exception:
                pass
            if attempt < attempts:
                logger.warning(
                    "Fight WS attempt %d/%d failed (%s) — reconnect…",
                    attempt, attempts, last.error or "no finish",
                )
                await asyncio.sleep(1.0)
                # Refresh fight vars for new akey / url_finish
                try:
                    conf = await self._client.get_fight_conf(st.fight_id)
                    vars_ = conf.get("swf_fight_vars") or vars_
                except Exception as exc:
                    logger.debug("refresh fight conf: %s", exc)
        return last

    async def run_fight(
        self,
        vars_: dict,
        *,
        timeout: float = 180.0,
        hit_zone: Optional[int] = None,
        brain: Optional[FightBrain] = None,
        conf: Optional[dict] = None,
        heal_callback: Optional[HealCallback] = None,
    ) -> FightOutcome:
        try:
            import websockets
        except ImportError as exc:
            return FightOutcome(error=f"websockets missing: {exc}")

        fight_id = int(vars_.get("fight_id") or 0)
        pers_id = int(vars_.get("pers_id") or 0)
        akey = int(vars_.get("akey") or 0)
        ws_host = str(vars_.get("wsHost") or "w1.dwar.ru/wsproxy/").rstrip("/") + "/"
        fs_host = str(vars_.get("host") or "fsrv.w1.dwar.ru")
        fs_port = int(vars_.get("port") or 5050)
        url_finish = str(vars_.get("url_finish") or "")
        if not fight_id or not pers_id or not akey:
            return FightOutcome(error="missing fight auth fields")

        if brain is None:
            brain = self._build_brain(
                conf or {"swf_fight_vars": vars_},
                pers_id=pers_id,
                hit_zone=hit_zone,
            )
        else:
            brain.pers_id = brain.pers_id or pers_id

        ws_url = f"wss://{ws_host}"
        outcome = FightOutcome(
            url_finish=url_finish,
            hit_seq=",".join(str(z) for z in brain.hit_seq),
        )
        win_team: Optional[int] = None
        cookie_hdr = "; ".join(
            f"{k}={v}" for k, v in (self._client._session or {}).items() if v
        )
        headers = {
            "Origin": self._client._world_url,
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Cookie": cookie_hdr,
        }

        logger.info(
            "Fight WS connect fight_id=%s pers_id=%s via %s → %s:%s seq=%s",
            fight_id, pers_id, ws_url, fs_host, fs_port, outcome.hit_seq,
        )

        async def _attack(ws, zone: int) -> None:
            await self._send_pak(
                ws,
                pack_params([
                    (0, PT_INT, FS_SCCL_ATTACK),
                    (0, PT_INT, int(zone)),
                    (0, PT_INT, 0),
                ]),
            )

        async def _set_block(ws, enabled: bool) -> None:
            # changeMode(flag=TO_FS_PF_DEFENDED, switcher=1|0)
            await self._send_pak(
                ws,
                pack_params([
                    (0, PT_INT, FS_SCCL_CHANGE_MODE),
                    (0, PT_INT, TO_FS_PF_DEFENDED),
                    (0, PT_INT, 1 if enabled else 0),
                ]),
            )

        async def _set_magic_stance(ws, enabled: bool = True) -> None:
            # BotMek «вход в бой в магической стойке»
            await self._send_pak(
                ws,
                pack_params([
                    (0, PT_INT, FS_SCCL_CHANGE_MODE),
                    (0, PT_INT, TO_FS_PF_MAGIC),
                    (0, PT_INT, 1 if enabled else 0),
                ]),
            )

        async def _do_turn(ws, *, now: float, reason: str) -> None:
            decision = brain.decide_turn(now=now)
            if decision.drink_elixir and heal_callback is not None:
                try:
                    drank = await heal_callback(int(brain.hp), int(brain.hp_max))
                    if drank:
                        # Optimistic local HP bump so we don't spam drinks
                        if brain.hp_max > 0:
                            heal_amt = max(1, int(brain.hp_max * 0.25))
                            brain.hp = min(brain.hp_max, brain.hp + heal_amt)
                        logger.info(
                            "Fight: mid-fight potion (hp≈%.0f%%) before %s",
                            brain.hp_percent, reason,
                        )
                except Exception as exc:
                    logger.debug("Fight mid-fight potion: %s", exc)
            if decision.set_magic_stance is not None:
                await _set_magic_stance(ws, decision.set_magic_stance)
                logger.info(
                    "Fight: BotMek magic stance %s (%s)",
                    "ON" if decision.set_magic_stance else "OFF",
                    brain.botmek_preset or "preset",
                )
            if decision.set_block is not None:
                await _set_block(ws, decision.set_block)
                brain.apply_block_result(decision.set_block, now=now)
                logger.info(
                    "Fight: block %s (hp≈%.0f%%) before %s",
                    "ON" if decision.set_block else "OFF",
                    brain.hp_percent, reason,
                )
            await _attack(ws, decision.hit_zone)
            outcome.attacks += 1
            logger.info(
                "Fight: %s → zone=%s/%s (#%d)%s",
                reason, decision.zone_name, decision.hit_zone, outcome.attacks,
                " finisher" if decision.is_finisher else "",
            )

        try:
            # websockets>=12: additional_headers; older: extra_headers
            connect_kwargs: dict[str, Any] = {
                "open_timeout": 15,
                "max_size": 2**22,
                "ping_interval": 20,
                "ping_timeout": 20,
                "close_timeout": 5,
            }
            try:
                import inspect
                sig = inspect.signature(websockets.connect)
                if "additional_headers" in sig.parameters:
                    connect_kwargs["additional_headers"] = headers
                else:
                    connect_kwargs["extra_headers"] = headers
            except Exception:
                connect_kwargs["additional_headers"] = headers

            async with websockets.connect(ws_url, **connect_kwargs) as ws:
                await ws.send(json.dumps({
                    "event": "connect",
                    "host": fs_host,
                    "port": fs_port,
                }))
                if not await self._wait_connected(ws, timeout=15):
                    return FightOutcome(error="wsproxy connect timeout")

                # INIT
                init = pack_params([
                    (0, PT_INT, FS_SCCL_INIT),
                    (0, PT_INT, pers_id),
                    (0, PT_BIGINT, fight_id),
                    (0, PT_INT, akey),
                ])
                await self._send_pak(ws, init)

                # Kick STATE once
                await self._send_pak(ws, pack_params([(0, PT_INT, FS_SCCL_STATE)]))

                deadline = asyncio.get_event_loop().time() + timeout
                idle_attacks = 0
                last_progress = asyncio.get_event_loop().time()
                inited = False

                while asyncio.get_event_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        now = asyncio.get_event_loop().time()
                        # Stall: INIT OK but no ATTACKNOW for too long → nudge hard
                        stalled = (now - last_progress) > 25.0
                        await self._send_pak(
                            ws, pack_params([(0, PT_INT, FS_SCCL_STATE)])
                        )
                        if idle_attacks < (8 if stalled else 4):
                            await _do_turn(ws, now=now, reason="stall-nudge")
                            idle_attacks += 1
                            if stalled and idle_attacks % 3 == 0:
                                await self._send_pak(
                                    ws,
                                    pack_params([(0, PT_INT, FS_SCCL_SKIP_TURN)]),
                                )
                                logger.warning(
                                    "Fight stall nudge: attacks=%d idle=%.0fs",
                                    outcome.attacks, now - last_progress,
                                )
                        continue

                    for frame in self._iter_frames(raw):
                        vals = unpack_params(frame)
                        if not vals:
                            continue
                        cmd = int(vals[0]) if isinstance(vals[0], int) else -1
                        if cmd == FS_SC_NONE and len(vals) > 1:
                            ev = int(vals[1]) if isinstance(vals[1], int) else -1
                            if ev == FS_PE_ATTACKNOW:
                                now = asyncio.get_event_loop().time()
                                await _do_turn(ws, now=now, reason="ATTACKNOW")
                                idle_attacks = 0
                                last_progress = now
                            elif ev == FS_PE_FIGHTOVER:
                                outcome.finished = True
                                if len(vals) > 2 and isinstance(vals[2], int):
                                    win_team = vals[2]
                                logger.info(
                                    "Fight: FIGHTOVER win_team=%s attacks=%d "
                                    "dmg=%d/%d blocks=%d",
                                    win_team, outcome.attacks,
                                    brain.damage_dealt, brain.damage_taken,
                                    brain.blocks_toggled,
                                )
                                break
                            elif ev == FS_PE_DAMAGE:
                                # vals: cmd, ev, persId, dmg, dmgType, crit, absorb, …
                                if len(vals) > 3 and isinstance(vals[2], int):
                                    dmg = vals[3] if isinstance(vals[3], int) else 0
                                    brain.note_damage(vals[2], dmg)
                                idle_attacks = 0
                                last_progress = asyncio.get_event_loop().time()
                            elif ev in (
                                FS_PE_ATTACKWAIT, FS_PE_ATTACK, FS_PE_ATTACKTIMEOUT,
                            ):
                                idle_attacks = 0
                                last_progress = asyncio.get_event_loop().time()
                        elif cmd == FS_SCCL_STATE:
                            # STATE reply: params[9]=hp, [10]=hp_max (canvas MCmd.state)
                            if len(vals) > 10:
                                try:
                                    hp = int(vals[9])  # type: ignore[arg-type]
                                    hp_max = int(vals[10])  # type: ignore[arg-type]
                                    if hp_max > 0:
                                        brain.note_state_hp(hp, hp_max)
                                except (TypeError, ValueError):
                                    pass
                        elif cmd == FS_SCCL_INIT:
                            # status in vals[1]; 0 == OK
                            status = vals[1] if len(vals) > 1 else None
                            inited = True
                            last_progress = asyncio.get_event_loop().time()
                            logger.info("Fight: INIT status=%s", status)
                            if status not in (0, "0", None) and status not in (0,):
                                # -5 = NO_AUTH etc.
                                if isinstance(status, int) and status < 0:
                                    outcome.error = f"INIT failed status={status}"
                                    return outcome
                    if outcome.finished:
                        break
                else:
                    outcome.error = "fight timeout"
                    if inited and outcome.attacks == 0:
                        logger.warning(
                            "Fight timeout with 0 attacks after INIT — likely hang."
                        )
        except Exception as exc:
            logger.warning("Fight WS error: %s", exc)
            return FightOutcome(
                error=str(exc),
                url_finish=url_finish,
                attacks=outcome.attacks,
                damage_dealt=brain.damage_dealt,
                damage_taken=brain.damage_taken,
                blocks_toggled=brain.blocks_toggled,
                hit_seq=outcome.hit_seq,
            )

        outcome.damage_dealt = brain.damage_dealt
        outcome.damage_taken = brain.damage_taken
        outcome.blocks_toggled = brain.blocks_toggled

        if outcome.finished and url_finish:
            try:
                path = url_finish if url_finish.startswith("/") else "/" + url_finish
                await self._client._get(path)
                logger.info("Fight finish URL hit: %s", path[:80])
            except Exception as exc:
                logger.debug("fight_finish: %s", exc)
        elif outcome.error and url_finish and outcome.attacks > 0:
            # Best-effort settle after WS drop mid-fight
            try:
                path = url_finish if url_finish.startswith("/") else "/" + url_finish
                await self._client._get(path)
                logger.info("Fight finish URL after WS error: %s", path[:80])
            except Exception:
                pass

        def _won_from_team() -> bool:
            # Canvas: win if winTeam == persTeam. Fallback: team 1 (common PvE).
            # win_team=None + 0 attacks is NOT a real win (phantom settle).
            if outcome.attacks <= 0:
                return False
            if win_team is None:
                return True
            if brain.pers_team is not None:
                return int(win_team) == int(brain.pers_team)
            return int(win_team) == 1

        # Confirm we left the fight
        try:
            st = await self._client.get_state()
            if not st.fight_id:
                outcome.finished = True
                outcome.won = _won_from_team()
                outcome.error = ""
            elif outcome.finished:
                outcome.won = _won_from_team()
        except Exception:
            pass

        if outcome.finished and not outcome.error:
            outcome.won = _won_from_team()
        return outcome

    @staticmethod
    async def _wait_connected(ws, timeout: float = 15.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except asyncio.TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("event") == "connected":
                return True
        return False

    @staticmethod
    async def _send_pak(ws, hex_pak: str) -> None:
        await ws.send(json.dumps({"event": "message", "data": hex_pak}))

    @staticmethod
    def _iter_frames(raw: Any):
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:
                return
        try:
            msg = json.loads(raw)
        except Exception:
            # bare hex?
            if isinstance(raw, str) and raw:
                for part in raw.split("\0"):
                    if part:
                        yield part
            return
        if msg.get("event") == "message":
            data = msg.get("data") or ""
            for part in str(data).split("\0"):
                if part:
                    yield part
