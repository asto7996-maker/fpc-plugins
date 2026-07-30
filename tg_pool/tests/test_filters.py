"""Pytest: inbound message filters (triggers, stop-words, bots, length)."""

from __future__ import annotations

from tg_pool.core.filters import MessageFilter


def test_trigger_match() -> None:
    f = MessageFilter(trigger_words=("vpn", "впн"))
    assert f.matched_trigger("Need a VPN today") == "vpn"
    assert f.matched_trigger("просто текст") is None


def test_stop_words() -> None:
    f = MessageFilter(trigger_words=("vpn",), stop_words=("casino", "xxx"))
    ok, reason = f.accept("vpn casino offer")
    assert not ok and reason == "stop_word"


def test_length_bounds() -> None:
    f = MessageFilter(trigger_words=(), min_length=3, max_length=10)
    assert not f.length_ok("ab")
    assert f.length_ok("abcd")
    assert not f.length_ok("abcdefghijk")


def test_ignore_bots_and_self() -> None:
    f = MessageFilter(trigger_words=("vpn",), ignore_bots=True)
    assert f.should_skip_sender(is_bot=True) is True
    assert f.should_skip_sender(is_self=True) is True
    ok, reason = f.accept("vpn please", is_bot=True)
    assert not ok and reason == "sender_skipped"


def test_accept_happy_path() -> None:
    f = MessageFilter(trigger_words=("vpn", "proxy"))
    ok, reason = f.accept("looking for proxy options")
    assert ok and reason == "ok"
