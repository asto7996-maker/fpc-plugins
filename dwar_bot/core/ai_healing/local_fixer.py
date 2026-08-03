"""
Deterministic local patches for known stuck patterns (before Cursor CLI).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from dwar_bot.core.ai_healing.paths import resolve_repo_root

logger = logging.getLogger(__name__)


def try_local_fix(verdict: dict, log_slice: str = "") -> Optional[str]:
    """
    Apply a known safe fix without Cursor. Returns changed file path or None.
    """
    blob = (log_slice or "").lower()
    issue = str(verdict.get("issue_type") or "")
    target = str(verdict.get("target_file") or "")

    if (
        "heal_wounded" in blob
        or "не задано действие" in blob
        or ("излечен" in blob and "use" in blob)
    ):
        return _patch_heal_wounded_guard()

    if issue == "STUCK_NO_PROGRESS" and "quest_tracker" in target:
        if "heal_wounded" in blob or "ранен" in blob:
            return _patch_heal_wounded_guard()

    return None


def _quest_tracker_path() -> Path:
    root = resolve_repo_root()
    candidates = [
        root / "modules" / "quest_tracker.py",
        root / "dwar_bot" / "modules" / "quest_tracker.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _patch_heal_wounded_guard() -> Optional[str]:
    """
    Harden _try_heal_wounded: never USE when flash_only / http_impossible.
    """
    path = _quest_tracker_path()
    if not path.exists():
        return None
    src = path.read_text(encoding="utf-8")
    marker = "HEAL_WOUNDED_SAFE_GUARD_V3"
    if marker in src:
        logger.info("local_fixer: heal_wounded V3 guard already present")
        return None

    new_method = '''
    async def _try_heal_wounded(self, obj: dict[str, Any]) -> bool:
        """Best-effort medicine use — fail fast, never storm the API.

        ''' + marker + '''
        """
        if obj.get("http_impossible") or obj.get("flash_only"):
            fail_until = float(obj.get("protocol_fail_until") or 0)
            if not fail_until or time.time() >= fail_until:
                obj = dict(obj)
                obj["flash_only"] = True
                obj["http_impossible"] = True
                obj["protocol_fail_until"] = time.time() + 3600.0
                self.pending_world_objective = obj
                self._persist_world_objective()
            return False

        fail_until = float(obj.get("protocol_fail_until") or 0)
        if fail_until and time.time() < fail_until:
            return False

        artikul = str(obj.get("artikul_id") or "18209")
        art_id = ""
        try:
            bag = await self._client.get_bag()
        except Exception as exc:
            logger.debug("heal_wounded get_bag: %s", exc)
            return False
        for a in bag.get("artifact_list") or []:
            if str(a.get("artikul_id") or "") == artikul:
                art_id = str(a.get("id") or a.get("artifact_id") or "")
                if not obj.get("artifact_title"):
                    obj["artifact_title"] = str(a.get("title") or a.get("name") or "")
                break
        if not art_id:
            logger.info(
                "heal_wounded: medicine artikul=%s not in bag — farm / wait.",
                artikul,
            )
            return False

        try:
            resp = await self._client.common_action("USE", {"artifact_id": art_id})
        except Exception as exc:
            logger.info("heal_wounded USE exception: %s", exc)
            obj = dict(obj)
            obj["protocol_fail_until"] = time.time() + 600.0
            obj["flash_only"] = True
            obj["http_impossible"] = True
            self.pending_world_objective = obj
            self._persist_world_objective()
            return False

        err = str(resp.error or getattr(resp, "redirect_error", "") or "")
        logger.info(
            "heal_wounded USE artifact=%s → status=%s err=%s",
            art_id, resp.status, (err or "—")[:100],
        )
        err_l = err.lower()
        if resp.status == STATUS_OK and err_l in ("", "false", "none"):
            logger.info("heal_wounded: USE accepted — clearing objective.")
            self.clear_world_objective("medicine_used")
            return True

        cooldown = 86400.0
        obj = dict(obj)
        obj["protocol_fail_until"] = time.time() + cooldown
        obj["flash_only"] = True
        obj["http_impossible"] = True
        obj["flash_notified"] = True
        self.pending_world_objective = obj
        self._persist_world_objective()
        logger.warning(
            "heal_wounded: FLASH-ONLY (HTTP '%s') — locked, no more USE.",
            (err or "empty")[:60],
        )
        return False
'''

    pattern = re.compile(
        r"    async def _try_heal_wounded\(self, obj: dict\[str, Any\]\) -> bool:.*?"
        r"(?=\n    def has_pending_type2|\n    async def retry_pending_type2|\n    def )",
        re.S,
    )
    if not pattern.search(src):
        logger.warning("local_fixer: could not locate _try_heal_wounded")
        return None
    updated = pattern.sub(new_method.rstrip() + "\n\n", src, count=1)
    if updated == src:
        return None
    import ast

    try:
        ast.parse(updated)
    except SyntaxError as exc:
        logger.error("local_fixer: syntax error after patch: %s", exc)
        return None
    path.write_text(updated, encoding="utf-8")
    logger.warning("local_fixer: applied heal_wounded V3 guard → %s", path)
    return str(path)
