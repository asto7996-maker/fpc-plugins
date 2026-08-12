"""
Shared path helpers for AI healing (VPS flat layout vs monorepo).
"""

from __future__ import annotations

from pathlib import Path


def resolve_repo_root() -> Path:
    """
    Return the workspace root for Cursor CLI + pytest.

    * VPS layout (checked first): ``/root/dwar_bot`` with ``modules/`` + ``tests/``
      inside the package — must NOT pick ``/root`` even if ``/root/tests`` exists.
    * Monorepo (dev): ``<repo>/`` with ``dwar_bot/`` + top-level ``tests/``.
    """
    here = Path(__file__).resolve()
    dwar_pkg = here.parents[2]  # …/dwar_bot
    parent = dwar_pkg.parent

    # VPS / flat package install: code+tests under dwar_bot/
    if (dwar_pkg / "modules").is_dir() and (dwar_pkg / "tests").is_dir():
        return dwar_pkg
    if (dwar_pkg / "modules").is_dir() and (dwar_pkg / "main.py").exists():
        # tests may be missing on some deploys — still prefer package root
        if not (parent / "dwar_bot" / "core").exists() or parent == dwar_pkg.parent:
            # If parent only wraps this one package, use the package
            if dwar_pkg.name == "dwar_bot":
                return dwar_pkg

    # Monorepo: <repo>/dwar_bot + <repo>/tests
    if (parent / "dwar_bot" / "modules").is_dir() and (parent / "tests").is_dir():
        return parent
    if (parent / ".git").is_dir() and (parent / "dwar_bot").is_dir():
        return parent

    return dwar_pkg


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
