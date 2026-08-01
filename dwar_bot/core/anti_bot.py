"""
Anti-bot / human-like behaviour helpers.

All public functions accept a Playwright ``Page`` and simulate realistic
interaction patterns: smooth mouse movement, natural typing with per-key
delays, random scroll jitter, and probabilistic idle pauses.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Optional, Tuple

from playwright.async_api import ElementHandle, Locator, Page

from dwar_bot.config import (
    DELAY_ACTION,
    DELAY_IDLE,
    DELAY_TYPING,
    IDLE_PAUSE_PROBABILITY,
    MOUSE_STEPS_RANGE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core delay helpers
# ---------------------------------------------------------------------------

async def sleep_random(min_s: float, max_s: float) -> None:
    """Sleep for a random duration in [min_s, max_s] seconds."""
    duration = random.uniform(min_s, max_s)
    logger.debug("Sleeping %.2fs …", duration)
    await asyncio.sleep(duration)


async def action_delay() -> None:
    """Standard between-action pause."""
    await sleep_random(DELAY_ACTION.min, DELAY_ACTION.max)


async def maybe_idle(probability: float = IDLE_PAUSE_PROBABILITY) -> None:
    """
    With *probability* chance, trigger a long idle pause to simulate the
    human stepping away from the keyboard.
    """
    if random.random() < probability:
        idle_s = random.uniform(DELAY_IDLE.min, DELAY_IDLE.max)
        logger.info("Idle pause triggered — sleeping %.0fs.", idle_s)
        await asyncio.sleep(idle_s)


# ---------------------------------------------------------------------------
# Mouse movement
# ---------------------------------------------------------------------------

def _bezier_curve(
    start: Tuple[float, float],
    end: Tuple[float, float],
    steps: int,
) -> list[Tuple[float, float]]:
    """
    Generate points along a cubic Bézier curve between *start* and *end*,
    with two randomly-offset control points for a natural-looking arc.
    """
    x0, y0 = start
    x3, y3 = end

    # Random control points with a slight perpendicular offset
    mid_x = (x0 + x3) / 2
    mid_y = (y0 + y3) / 2
    jitter = random.uniform(0.1, 0.3)
    dx, dy = x3 - x0, y3 - y0
    norm = math.hypot(dx, dy) or 1.0

    perp_x = -dy / norm * norm * jitter
    perp_y =  dx / norm * norm * jitter

    x1 = mid_x + perp_x + random.uniform(-10, 10)
    y1 = mid_y + perp_y + random.uniform(-10, 10)
    x2 = mid_x - perp_x + random.uniform(-10, 10)
    y2 = mid_y - perp_y + random.uniform(-10, 10)

    points: list[Tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        t2 = t * t
        t3 = t2 * t
        mt = 1 - t
        mt2 = mt * mt
        mt3 = mt2 * mt
        x = mt3 * x0 + 3 * mt2 * t * x1 + 3 * mt * t2 * x2 + t3 * x3
        y = mt3 * y0 + 3 * mt2 * t * y1 + 3 * mt * t2 * y2 + t3 * y3
        points.append((x, y))
    return points


async def move_mouse_to(
    page: Page,
    x: float,
    y: float,
    steps: Optional[int] = None,
) -> None:
    """Move the mouse from its current position to (x, y) along a Bézier path."""
    if steps is None:
        steps = random.randint(*MOUSE_STEPS_RANGE)

    # Get current mouse position from JS (defaults to 0,0 on fresh page)
    try:
        pos = await page.evaluate(
            "() => ({ x: window._mouseX || 0, y: window._mouseY || 0 })"
        )
        start = (float(pos.get("x", 0)), float(pos.get("y", 0)))
    except Exception:
        start = (0.0, 0.0)

    curve = _bezier_curve(start, (x, y), steps)
    for px, py in curve:
        await page.mouse.move(px, py)
        # Micro-sleep between steps for realism
        await asyncio.sleep(random.uniform(0.003, 0.012))

    # Track position in page JS for next call
    try:
        await page.evaluate(
            f"() => {{ window._mouseX = {x}; window._mouseY = {y}; }}"
        )
    except Exception:
        pass


async def human_click(
    page: Page,
    selector: str,
    timeout_ms: int = 15_000,
    double: bool = False,
) -> None:
    """
    Locate *selector*, move the mouse to it naturally, then click.

    Parameters
    ----------
    page:        Playwright page.
    selector:    CSS or XPath selector.
    timeout_ms:  How long to wait for the element to be visible.
    double:      If True, perform a double-click.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        box = await locator.bounding_box()
        if box is None:
            raise RuntimeError(f"Element '{selector}' has no bounding box (not visible?).")

        # Aim for a slightly random point within the element
        target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
        target_y = box["y"] + box["height"] * random.uniform(0.25, 0.75)

        await move_mouse_to(page, target_x, target_y)
        await sleep_random(0.08, 0.25)   # hover pause

        if double:
            await page.mouse.dblclick(target_x, target_y)
        else:
            await page.mouse.click(target_x, target_y)

        logger.debug(
            "human_click('%s') at (%.0f, %.0f)", selector, target_x, target_y
        )
    except Exception as exc:
        logger.warning("human_click failed for '%s': %s", selector, exc)
        raise


async def human_click_element(
    page: Page,
    element: ElementHandle,
    double: bool = False,
) -> None:
    """Click a concrete ElementHandle with human-like mouse movement."""
    box = await element.bounding_box()
    if box is None:
        raise RuntimeError("Element has no bounding box.")

    target_x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
    target_y = box["y"] + box["height"] * random.uniform(0.25, 0.75)

    await move_mouse_to(page, target_x, target_y)
    await sleep_random(0.08, 0.25)

    if double:
        await page.mouse.dblclick(target_x, target_y)
    else:
        await page.mouse.click(target_x, target_y)


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------

async def human_type(
    page: Page,
    selector: str,
    text: str,
    clear_first: bool = True,
    timeout_ms: int = 10_000,
) -> None:
    """
    Focus *selector* and type *text* character by character with random delays,
    simulating human typing speed and occasional micro-pauses.
    """
    locator = page.locator(selector).first
    await locator.wait_for(state="visible", timeout=timeout_ms)

    await human_click(page, selector, timeout_ms=timeout_ms)
    await sleep_random(0.1, 0.3)

    if clear_first:
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.05)
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.1)

    for char in text:
        await page.keyboard.type(char)
        # Occasionally pause longer (like thinking between words)
        if char == " " and random.random() < 0.15:
            await sleep_random(0.15, 0.45)
        else:
            await sleep_random(DELAY_TYPING.min, DELAY_TYPING.max)

    logger.debug("human_type('%s') → %d chars typed.", selector, len(text))


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------

async def human_scroll(
    page: Page,
    direction: str = "down",
    amount: Optional[int] = None,
    steps: int = 3,
) -> None:
    """
    Scroll the page in *direction* (``"up"`` or ``"down"``) in several
    randomised increments.
    """
    if amount is None:
        amount = random.randint(200, 600)

    delta = amount if direction == "down" else -amount
    per_step = delta // steps

    for _ in range(steps):
        jitter = random.randint(-30, 30)
        await page.mouse.wheel(0, per_step + jitter)
        await sleep_random(0.1, 0.35)

    logger.debug("human_scroll(%s, %d px).", direction, amount)


# ---------------------------------------------------------------------------
# Wait helpers with human-like jitter
# ---------------------------------------------------------------------------

async def wait_for_selector_safe(
    page: Page,
    selector: str,
    timeout_ms: int = 15_000,
    state: str = "visible",
) -> Optional[ElementHandle]:
    """
    Wait for *selector* and return the ElementHandle, or ``None`` on timeout.

    Does NOT raise — lets the caller decide whether the absence is fatal.
    """
    try:
        handle = await page.wait_for_selector(
            selector, timeout=timeout_ms, state=state
        )
        return handle
    except Exception:
        logger.debug("wait_for_selector_safe: '%s' not found within %dms.", selector, timeout_ms)
        return None


async def wait_and_click(
    page: Page,
    selector: str,
    timeout_ms: int = 15_000,
) -> bool:
    """
    Wait for *selector* to be visible then click it humanly.

    Returns True on success, False if the element never appeared.
    """
    handle = await wait_for_selector_safe(page, selector, timeout_ms=timeout_ms)
    if handle is None:
        return False
    await human_click(page, selector)
    await action_delay()
    return True


async def random_mouse_wander(page: Page, moves: int = 3) -> None:
    """
    Move the mouse to a few random screen positions to simulate idle activity.
    """
    from dwar_bot.config import VIEWPORT
    for _ in range(moves):
        x = random.uniform(50, VIEWPORT["width"] - 50)
        y = random.uniform(50, VIEWPORT["height"] - 50)
        await move_mouse_to(page, x, y)
        await sleep_random(0.3, 1.0)
