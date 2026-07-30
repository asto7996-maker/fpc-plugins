"""Pytest: SpintaxEngine expansion & variety self-test."""

from __future__ import annotations

import random

import pytest

from tg_pool.core.spintax import SpintaxEngine, SpintaxError


@pytest.fixture
def engine() -> SpintaxEngine:
    return SpintaxEngine()


def test_basic_spin(engine: SpintaxEngine) -> None:
    out = engine.spin("{a|b}", rng=random.Random(0))
    assert out in {"a", "b"}


def test_nested_spin(engine: SpintaxEngine) -> None:
    tpl = "{Hi|Hello}, {friend|{pal|mate}}!"
    outs = {engine.spin(tpl, rng=random.Random(i)) for i in range(50)}
    assert any(x.startswith("Hi") or x.startswith("Hello") for x in outs)
    assert not any("{" in x or "}" in x for x in outs)


def test_emoji_and_specials(engine: SpintaxEngine) -> None:
    tpl = "VPN {😊|👍|🚀} — {@PaskodVPN_bot|бот}!"
    out = engine.spin(tpl, rng=random.Random(1))
    assert "{" not in out and "}" not in out
    assert "VPN" in out


def test_unbalanced_raises(engine: SpintaxEngine) -> None:
    with pytest.raises(SpintaxError):
        engine.spin("{a|b")


def test_template_variety(engine: SpintaxEngine) -> None:
    report = engine.test_template_variety(
        "{a|b|c} {1|2|{x|y}}",
        samples=100,
    )
    assert report.ok
    assert report.unique_count >= 2
    assert not report.has_unexpanded_braces
