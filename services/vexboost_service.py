"""VexBoost API: lookup min/max quantity по ID услуги (action=services)."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config import DB_PATH

logger = logging.getLogger("starvell.vexboost_service")

VEXBOOST_PLUGIN_UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"
DEFAULT_API_URL = "https://vexboost.ru/api/v2"
_SERVICES_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = 3600


class VexBoostServiceError(Exception):
    """Ошибка загрузки услуги VexBoost."""


@dataclass
class VexBoostServiceInfo:
    service_id: int
    name: str
    min_qty: int
    max_qty: int
    rate: str = ""
    category: str = ""


def _cache_key(api_url: str, api_key: str) -> str:
    return f"{api_url.rstrip('/')}:{api_key[:8]}"


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", ".").strip()))
    except (TypeError, ValueError):
        return default


async def load_vexboost_api_config(db: Any | None = None) -> dict[str, str]:
    """API key: env → settings.json → плагин VexBoost в SQLite."""
    cfg = {
        "api_url": os.getenv("VEXBOOST_API_URL", DEFAULT_API_URL).rstrip("/"),
        "api_key": os.getenv("VEXBOOST_API_KEY", "").strip(),
    }
    try:
        from config import load_settings

        s = load_settings()
        if s.parser_vexboost_api_key.strip():
            cfg["api_key"] = s.parser_vexboost_api_key.strip()
        if s.parser_vexboost_api_url.strip():
            cfg["api_url"] = s.parser_vexboost_api_url.strip().rstrip("/")
    except Exception as exc:
        logger.debug("load_vexboost_api_config from settings: %s", exc)
    if db is not None:
        try:
            from core.plugins.settings_store import PluginSettingsStore

            plugin_cfg = await PluginSettingsStore(db).get_all(VEXBOOST_PLUGIN_UUID)
            if plugin_cfg.get("api_key"):
                cfg["api_key"] = str(plugin_cfg["api_key"]).strip()
            if plugin_cfg.get("api_url"):
                cfg["api_url"] = str(plugin_cfg["api_url"]).rstrip("/")
        except Exception as exc:
            logger.debug("load_vexboost_api_config from db: %s", exc)
    elif DB_PATH.exists():
        try:
            import aiosqlite

            async with aiosqlite.connect(DB_PATH) as conn:
                cur = await conn.execute(
                    "SELECT config_json FROM plugin_settings WHERE plugin_uuid = ?",
                    (VEXBOOST_PLUGIN_UUID,),
                )
                row = await cur.fetchone()
            if row and row[0]:
                import json

                plugin_cfg = json.loads(row[0]) or {}
                if plugin_cfg.get("api_key"):
                    cfg["api_key"] = str(plugin_cfg["api_key"]).strip()
                if plugin_cfg.get("api_url"):
                    cfg["api_url"] = str(plugin_cfg["api_url"]).rstrip("/")
        except Exception as exc:
            logger.debug("load_vexboost_api_config from sqlite: %s", exc)
    return cfg


async def _fetch_services_list(api_url: str, api_key: str) -> list[dict[str, Any]]:
    key = _cache_key(api_url, api_key)
    now = time.time()
    cached = _SERVICES_CACHE.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    data: Any
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                api_url,
                data={"key": api_key, "action": "services"},
            )
            data = resp.json()
    except Exception as exc:
        raise VexBoostServiceError(f"Не удалось связаться с VexBoost: {exc}") from exc

    if isinstance(data, dict) and data.get("error"):
        err = str(data["error"])
        if "api key" in err.lower() or "incorrect" in err.lower():
            raise VexBoostServiceError("Неверный API Key VexBoost — проверьте настройки плагина")
        if "inactive" in err.lower():
            raise VexBoostServiceError("API Key VexBoost неактивен")
        raise VexBoostServiceError(err)

    if not isinstance(data, list):
        raise VexBoostServiceError("VexBoost вернул неожиданный ответ (ожидался список услуг)")

    _SERVICES_CACHE[key] = (now, data)
    return data


def _service_from_row(row: dict[str, Any], service_id: int) -> VexBoostServiceInfo | None:
    sid = _parse_int(row.get("service") or row.get("id"), 0)
    if sid != int(service_id):
        return None
    min_qty = _parse_int(row.get("min"), 1)
    max_qty = _parse_int(row.get("max"), 0)
    if min_qty < 1:
        min_qty = 1
    if max_qty < min_qty:
        max_qty = min_qty
    return VexBoostServiceInfo(
        service_id=sid,
        name=str(row.get("name") or f"Service {sid}").strip(),
        min_qty=min_qty,
        max_qty=max_qty,
        rate=str(row.get("rate") or "").strip(),
        category=str(row.get("category") or "").strip(),
    )


async def fetch_vexboost_service(
    service_id: int,
    *,
    db: Any | None = None,
    config: dict[str, str] | None = None,
) -> VexBoostServiceInfo:
    """Возвращает min/max и название услуги VexBoost по ID."""
    sid = int(service_id)
    if sid < 1:
        raise VexBoostServiceError("ID услуги VexBoost должен быть положительным числом")

    cfg = config or await load_vexboost_api_config(db)
    api_key = str(cfg.get("api_key") or "").strip()
    api_url = str(cfg.get("api_url") or DEFAULT_API_URL).rstrip("/")
    if not api_key:
        raise VexBoostServiceError(
            "API Key VexBoost не настроен. Укажите ключ в "
            "Плагины → VexBoost AutoSMM → Настройки "
            "или добавьте parser_vexboost_api_key в config/settings.json"
        )

    services = await _fetch_services_list(api_url, api_key)
    for row in services:
        if not isinstance(row, dict):
            continue
        info = _service_from_row(row, sid)
        if info:
            return info

    raise VexBoostServiceError(f"Услуга VexBoost с ID {sid} не найдена в каталоге")
