"""Recursive spintax renderer and human-like reply randomization."""

from __future__ import annotations

import random
import re
from typing import Mapping, Optional


_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")
_EMOJI_POOL = ("🙂", "✨", "👍", "🙌", "💬", "🙂‍↔️", "👌", "🤝")
_NEAR_KEYS = {
    "а": "о",
    "о": "а",
    "е": "и",
    "и": "е",
    "с": "с",
    "н": "н",
    "т": "т",
    "a": "s",
    "e": "r",
    "i": "o",
    "o": "i",
    "s": "a",
}


def _split_top_level(body: str) -> list[str]:
    """Split spintax alternatives on top-level `|` only."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in body:
        if ch == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def expand_spintax(template: str, rng: Optional[random.Random] = None) -> str:
    """Recursively expand nested spintax: `{a|b|{c|d}}`."""
    choose = (rng or random).choice

    def _expand(text: str) -> str:
        result: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] != "{":
                result.append(text[i])
                i += 1
                continue

            depth = 0
            j = i
            while j < n:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1

            if j >= n or text[j] != "}":
                # Unbalanced brace — keep literal
                result.append(text[i])
                i += 1
                continue

            body = text[i + 1 : j]
            options = [part.strip() for part in _split_top_level(body)]
            # Single option without `|` may be a leftover placeholder — keep as-is
            if len(options) <= 1 and "|" not in body:
                result.append(text[i : j + 1])
            else:
                picked = choose([o for o in options if o != ""]) if options else ""
                result.append(_expand(picked))
            i = j + 1
        return "".join(result)

    # Iterate until stable for safety with odd nesting leftovers
    current = template
    for _ in range(20):
        nxt = _expand(current)
        if nxt == current:
            break
        current = nxt
    return current


def apply_context(template: str, context: Mapping[str, str] | None) -> str:
    text = template
    if not context:
        return text
    for key, value in sorted(context.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(f"{{{{{key}}}}}", str(value))
        # Only replace bare `{key}` when it is not a spintax group
        text = re.sub(rf"\{{{re.escape(key)}\}}", str(value), text)
    return text


def randomize_formatting(
    text: str,
    *,
    emoji_chance: float = 0.30,
    typo_enabled: bool = False,
    case_randomize: bool = False,
    zwsp_enabled: bool = True,
    rng: Optional[random.Random] = None,
) -> str:
    """Apply optional humanization / hash uniquification to reply text."""
    r = rng or random
    out = text.strip()

    if case_randomize and out:
        if r.random() < 0.5:
            out = out[0].lower() + out[1:]
        else:
            out = out[0].upper() + out[1:]

    if typo_enabled and len(out) > 8 and r.random() < 0.25:
        idx = r.randint(1, len(out) - 2)
        ch = out[idx]
        replacement = _NEAR_KEYS.get(ch.lower())
        if replacement:
            if ch.isupper():
                replacement = replacement.upper()
            out = out[:idx] + replacement + out[idx + 1 :]

    if emoji_chance > 0 and r.random() < emoji_chance:
        out = f"{out} {r.choice(_EMOJI_POOL)}"

    if zwsp_enabled:
        # Insert zero-width spaces to uniquify message hash without visible change
        positions = max(1, min(3, len(out) // 12))
        chars = list(out)
        for _ in range(positions):
            if len(chars) < 2:
                break
            pos = r.randint(1, len(chars) - 1)
            chars.insert(pos, "\u200b")
        out = "".join(chars)

    return out


def render_template(
    template: str,
    context: Mapping[str, str] | None = None,
    *,
    emoji_chance: float = 0.30,
    typo_enabled: bool = False,
    case_randomize: bool = False,
    zwsp_enabled: bool = True,
    randomize: bool = True,
    rng: Optional[random.Random] = None,
) -> str:
    """Full pipeline: context → nested spintax → optional formatting noise."""
    text = apply_context(template, context)
    text = expand_spintax(text, rng=rng)
    if randomize:
        text = randomize_formatting(
            text,
            emoji_chance=emoji_chance,
            typo_enabled=typo_enabled,
            case_randomize=case_randomize,
            zwsp_enabled=zwsp_enabled,
            rng=rng,
        )
    else:
        text = text.strip()
    return text


def pick_variant(variants: list[str], **kwargs) -> str:
    if not variants:
        raise ValueError("No response variants available")
    return render_template(random.choice(variants), **kwargs)
