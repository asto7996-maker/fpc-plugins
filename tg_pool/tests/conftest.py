"""Pytest configuration for tg_pool async tests."""

from __future__ import annotations

import pytest

# pytest-asyncio: auto mode for async tests
pytest_plugins: list[str] = []


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: mark test as asyncio")
