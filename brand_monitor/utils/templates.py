"""Knowledge-base response template renderer.

Supports alternative phrasing via curly-brace groups:

    Hello! {How can I help?|What can I do for you?}

Each `{a|b|c}` group is replaced by a randomly chosen alternative.
"""

from __future__ import annotations

import random
import re
from typing import Mapping


_VARIANT_RE = re.compile(r"\{([^{}]+)\}")


def render_template(template: str, context: Mapping[str, str] | None = None) -> str:
    """Render a knowledge-base template with random variants and optional context.

    Context placeholders (`{agent}`, `{{agent}}`) are applied first.
    Curly groups that contain `|` are treated as random alternatives.
    """
    text = template
    if context:
        # Longer / double-brace keys first to avoid partial collisions
        for key, value in sorted(context.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(f"{{{{{key}}}}}", str(value))
            text = text.replace(f"{{{key}}}", str(value))

    def _pick(match: re.Match[str]) -> str:
        body = match.group(1)
        if "|" not in body:
            # Not a variant group (e.g. leftover `{unknown}`) — keep as-is
            return match.group(0)
        options = [part.strip() for part in body.split("|") if part.strip()]
        return random.choice(options) if options else ""

    text = _VARIANT_RE.sub(_pick, text)
    return text.strip()


def pick_variant(variants: list[str]) -> str:
    """Pick and render a random template from a list."""
    if not variants:
        raise ValueError("No response variants available")
    return render_template(random.choice(variants))
