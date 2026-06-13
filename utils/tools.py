"""Утилиты: бэкап, система, обновления."""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

from config import BACKUP_DIR, BASE_DIR, CONFIG_DIR, GITHUB_BRANCH, GITHUB_REPO, LOGS_DIR, STORAGE_DIR, VERSION


def system_stats() -> str:
  try:
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    boot = datetime.fromtimestamp(psutil.boot_time()).strftime("%d.%m.%Y %H:%M")
    return (
      f"🖥 <b>Система</b>\n"
      f"CPU: {cpu}%\n"
      f"RAM: {mem.percent}% ({mem.used // (1024**2)} / {mem.total // (1024**2)} MB)\n"
      f"Uptime с: {boot}"
    )
  except ImportError:
    return "🖥 Установите psutil для статистики: pip install psutil"


def create_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"backup_{ts}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in (CONFIG_DIR, STORAGE_DIR):
            if folder.exists():
                for root, _, files in os.walk(folder):
                    for f in files:
                        fp = Path(root) / f
                        zf.write(fp, fp.relative_to(BASE_DIR))
    return path


def restore_backup(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(BASE_DIR)


def logs_zip() -> Path | None:
    if not LOGS_DIR.exists():
        return None
    path = BACKUP_DIR / f"logs_{int(time.time())}.zip"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.make_archive(str(path.with_suffix("")), "zip", LOGS_DIR)
    return path.with_suffix(".zip") if path.with_suffix(".zip").exists() else Path(str(path) + ".zip")


async def check_github_update() -> tuple[bool, str]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code != 200:
                return False, f"GitHub HTTP {resp.status_code}"
            data = resp.json()
            sha = (data.get("commit") or {}).get("message", "")[:80]
            return True, f"Ветка {GITHUB_BRANCH}\nПоследний коммит:\n{sha}\nТекущая версия: {VERSION}"
    except Exception as exc:
        return False, str(exc)
