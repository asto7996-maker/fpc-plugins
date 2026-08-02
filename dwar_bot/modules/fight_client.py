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
from typing import Any, Optional

from dwar_bot.core.game_client import DwarGameClient

logger = logging.getLogger(__name__)

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

# Hit zones (TOP/MIDDLE/BOTTOM)
MIDDLE_ATTACK_ID = 2

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


class FightClient:
    """Drive one fight to completion via wsproxy."""

    def __init__(self, client: DwarGameClient) -> None:
        self._client = client

    async def complete_current_fight(
        self,
        *,
        timeout: float = 180.0,
        hit_zone: int = MIDDLE_ATTACK_ID,
    ) -> FightOutcome:
        st = await self._client.get_state()
        if not st.fight_id:
            return FightOutcome(error="not in fight")
        conf = await self._client.get_fight_conf(st.fight_id)
        vars_ = conf.get("swf_fight_vars") or {}
        if not vars_:
            return FightOutcome(error="no fight conf vars")
        return await self.run_fight(vars_, timeout=timeout, hit_zone=hit_zone)

    async def run_fight(
        self,
        vars_: dict,
        *,
        timeout: float = 180.0,
        hit_zone: int = MIDDLE_ATTACK_ID,
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

        ws_url = f"wss://{ws_host}"
        outcome = FightOutcome(url_finish=url_finish)
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
            "Fight WS connect fight_id=%s pers_id=%s via %s → %s:%s",
            fight_id, pers_id, ws_url, fs_host, fs_port,
        )

        try:
            # websockets>=12: additional_headers; older: extra_headers
            connect_kwargs: dict[str, Any] = {
                "open_timeout": 15,
                "max_size": 2**22,
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
                our_team_win: Optional[int] = None
                idle_attacks = 0

                while asyncio.get_event_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
                    except asyncio.TimeoutError:
                        # Nudge state / opportunistic attack if turn stalled
                        await self._send_pak(
                            ws, pack_params([(0, PT_INT, FS_SCCL_STATE)])
                        )
                        if idle_attacks < 3:
                            await self._send_pak(
                                ws,
                                pack_params([
                                    (0, PT_INT, FS_SCCL_ATTACK),
                                    (0, PT_INT, hit_zone),
                                    (0, PT_INT, 0),
                                ]),
                            )
                            outcome.attacks += 1
                            idle_attacks += 1
                        continue

                    for frame in self._iter_frames(raw):
                        vals = unpack_params(frame)
                        if not vals:
                            continue
                        cmd = int(vals[0]) if isinstance(vals[0], int) else -1
                        if cmd == FS_SC_NONE and len(vals) > 1:
                            ev = int(vals[1]) if isinstance(vals[1], int) else -1
                            if ev == FS_PE_ATTACKNOW:
                                await self._send_pak(
                                    ws,
                                    pack_params([
                                        (0, PT_INT, FS_SCCL_ATTACK),
                                        (0, PT_INT, hit_zone),
                                        (0, PT_INT, 0),
                                    ]),
                                )
                                outcome.attacks += 1
                                idle_attacks = 0
                                logger.info(
                                    "Fight: ATTACKNOW → hit zone=%s (#%d)",
                                    hit_zone, outcome.attacks,
                                )
                            elif ev == FS_PE_FIGHTOVER:
                                outcome.finished = True
                                if len(vals) > 2 and isinstance(vals[2], int):
                                    our_team_win = vals[2]
                                logger.info(
                                    "Fight: FIGHTOVER win_team=%s attacks=%d",
                                    our_team_win, outcome.attacks,
                                )
                                break
                            elif ev in (FS_PE_ATTACKWAIT, FS_PE_ATTACK, FS_PE_ATTACKTIMEOUT):
                                idle_attacks = 0
                        elif cmd == FS_SCCL_INIT:
                            # status in vals[1]; 0 == OK
                            status = vals[1] if len(vals) > 1 else None
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
        except Exception as exc:
            logger.warning("Fight WS error: %s", exc)
            return FightOutcome(error=str(exc), url_finish=url_finish)

        if outcome.finished and url_finish:
            try:
                path = url_finish if url_finish.startswith("/") else "/" + url_finish
                await self._client._get(path)
                logger.info("Fight finish URL hit: %s", path[:80])
            except Exception as exc:
                logger.debug("fight_finish: %s", exc)

        # Confirm we left the fight
        try:
            st = await self._client.get_state()
            if not st.fight_id:
                outcome.finished = True
                outcome.won = True if our_team_win in (None, 1) else bool(outcome.won)
            elif outcome.finished:
                outcome.won = our_team_win == 1
        except Exception:
            pass

        if outcome.finished and not outcome.error:
            outcome.won = True if our_team_win in (None, 1) else (our_team_win == 1)
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
