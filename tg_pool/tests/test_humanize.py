"""Stylistics + behavioral timing self-tests for humanize module."""

from __future__ import annotations

import random

import pytest

from tg_pool.core.humanize import (
    BehavioralEmulationEngine,
    humanize_text,
    humanize_text_sync,
    typing_duration_sec,
)


@pytest.mark.asyncio
async def test_humanize_lowercases_start() -> None:
    out = await humanize_text("Лучший вариант для скорости — @PaskodVPN_bot")
    assert out
    # first alphabetic char is lowercase
    for ch in out:
        if ch.isalpha():
            assert ch == ch.lower()
            assert ch != ch.upper() or not ch.isupper()
            break
    assert out[0].islower() or not out[0].isalpha()


def test_humanize_strips_all_emoji() -> None:
    raw = "смотри 😀 вот 🔥 этот 👍 сервис @PaskodVPN_bot 🚀✨"
    out = humanize_text_sync(raw)
    assert "😀" not in out
    assert "🔥" not in out
    assert "👍" not in out
    assert "🚀" not in out
    assert "✨" not in out
    assert "@PaskodVPN_bot" in out
    assert out == out  # sanity


def test_humanize_collapses_spaces_and_newlines() -> None:
    out = humanize_text_sync("Привет,   мир.\n\n\nкак дела?")
    assert "  " not in out
    assert "\n\n\n" not in out
    assert out.startswith("п")  # lowercased


def test_humanize_softens_bangs_and_caps() -> None:
    out = humanize_text_sync("СМОТРИ СЮДА!!! @PaskodVPN_bot")
    assert "!!!" not in out
    assert "смотри" in out.lower()


def test_typing_scales_with_length() -> None:
    rng = random.Random(0)
    short = typing_duration_sec("hi", cps_min=0.15, cps_max=0.35, rng=rng)
    rng = random.Random(0)
    long = typing_duration_sec("hi" * 50, cps_min=0.15, cps_max=0.35, rng=rng)
    assert long > short
    # Bounds for short text (2 chars): 2*0.15 .. 2*0.35
    rng = random.Random(1)
    t = typing_duration_sec("ab", cps_min=0.15, cps_max=0.35, rng=rng)
    assert 2 * 0.15 <= t <= 2 * 0.35


def test_behavior_plan_ranges() -> None:
    eng = BehavioralEmulationEngine(
        read_min=15,
        read_max=45,
        pause_min=1.5,
        pause_max=3.5,
        rng=random.Random(7),
    )
    plan = eng.plan("Нужен VPN? Попробуй @PaskodVPN_bot")
    assert 15.0 <= plan.read_sec <= 45.0
    assert 1.5 <= plan.pause_sec <= 3.5
    assert plan.typing_sec > 0
    assert plan.text[0].islower()
    assert "😀" not in plan.text


@pytest.mark.asyncio
async def test_typing_refresh_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typing longer than 5s should be split into refresh chunks ≤4.5s."""
    sleeps: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    class DummyAction:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class DummyClient:
        def action(self, chat_id, what):
            return DummyAction()

    eng = BehavioralEmulationEngine(typing_refresh_sec=4.5, rng=random.Random(0))
    monkeypatch.setattr("tg_pool.core.humanize.asyncio.sleep", fake_sleep)
    await eng.emulate_typing(DummyClient(), 123, 12.0)
    assert sleeps
    assert all(s <= 4.5 + 1e-9 for s in sleeps)
    assert abs(sum(sleeps) - 12.0) < 1e-6
