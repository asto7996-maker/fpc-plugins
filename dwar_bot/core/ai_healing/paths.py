"""
Shared path helpers for AI healing (VPS flat layout vs monorepo).
"""

from __future__ import annotations

from pathlib import Path


def resolve_repo_root() -> Path:
    """
    Return the workspace root for Cursor CLI + pytest.

    * Monorepo (dev): ``<repo>/dwar_bot/core/ai_healing`` → ``<repo>``
      (has ``tests/`` + ``dwar_bot/``).
    * VPS layout: code lives in ``/root/dwar_bot`` with ``tests/`` inside
      the package → return ``/root/dwar_bot`` (NOT ``/root``).
    """
    here = Path(__file__).resolve()
    dwar_pkg = here.parents[2]  # …/dwar_bot
    parent = dwar_pkg.parent
    if (parent / "dwar_bot").is_dir() and (
        (parent / "tests").is_dir() or (parent / ".git").is_dir()
    ):
        return parent
    if (dwar_pkg / "modules").is_dir() and (
        (dwar_pkg / "tests").is_dir() or (dwar_pkg / "main.py").exists()
    ):
        return dwar_pkg
    return parent


def resolve_test_target(repo_root: Path) -> str:
    for cand in (
        "tests/test_bot.py",
        "tests/test_ai_healing.py",
        "test_bot.py",
    ):
        if (repo_root / cand).exists():
            return cand
    # Fallback relative path (may fail pytest — caller handles)
    return "tests/test_bot.py"
