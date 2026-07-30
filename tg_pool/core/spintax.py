"""
Spintax engine — nested `{a|b|{c|d}}` expansion for reply templates.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional


_LEAF_RE = re.compile(r"\{([^{}]+)\}")


class SpintaxError(ValueError):
    """Invalid spintax template."""


@dataclass(frozen=True)
class VarietyReport:
    unique_count: int
    samples: tuple[str, ...]
    has_unexpanded_braces: bool
    ok: bool
    detail: str


class SpintaxEngine:
    """Expand nested spintax and self-test template variety."""

    def spin(self, template: str, *, rng: Optional[random.Random] = None) -> str:
        if template is None:
            raise SpintaxError("template is None")
        rng = rng or random.Random()
        text = str(template)
        for _ in range(64):
            match = _LEAF_RE.search(text)
            if not match:
                if "{" in text or "}" in text:
                    raise SpintaxError(f"Unexpanded braces left in: {text!r}")
                return text
            options = match.group(1).split("|")
            if not options or any(opt == "" and len(options) == 1 for opt in options):
                raise SpintaxError("Empty spintax group")
            choice = rng.choice(options)
            text = text[: match.start()] + choice + text[match.end() :]
        raise SpintaxError("Spintax nesting too deep or cyclic")

    def count_variants(self, template: str, *, cap: int = 10_000) -> int:
        """Approximate combinatorial size by expanding leaf groups."""
        text = str(template)
        total = 1
        for _ in range(64):
            match = _LEAF_RE.search(text)
            if not match:
                if "{" in text or "}" in text:
                    raise SpintaxError(f"Unexpanded braces left in: {text!r}")
                return min(total, cap)
            options = match.group(1).split("|")
            total *= max(1, len(options))
            if total >= cap:
                return cap
            # replace group with a neutral token so outer groups can be counted
            text = text[: match.start()] + "x" + text[match.end() :]
        raise SpintaxError("Spintax nesting too deep or cyclic")

    def test_template_variety(
        self,
        template_string: str,
        *,
        samples: int = 100,
    ) -> VarietyReport:
        """
        Generate `samples` expansions; report unique count and brace leftovers.
        """
        try:
            outs: list[str] = []
            seed_rng = random.Random(42)
            for _ in range(samples):
                local = random.Random(seed_rng.randint(0, 1_000_000_000))
                outs.append(self.spin(template_string, rng=local))
            unique = len(set(outs))
            leftover = any("{" in o or "}" in o for o in outs)
            combinatorial = self.count_variants(template_string)
            ok = (not leftover) and unique >= 1
            detail = (
                f"unique={unique}/{samples}, combinatorial≈{combinatorial}, "
                f"leftover_braces={leftover}"
            )
            return VarietyReport(
                unique_count=unique,
                samples=tuple(outs[:5]),
                has_unexpanded_braces=leftover,
                ok=ok,
                detail=detail,
            )
        except SpintaxError as exc:
            return VarietyReport(
                unique_count=0,
                samples=(),
                has_unexpanded_braces=True,
                ok=False,
                detail=str(exc),
            )
