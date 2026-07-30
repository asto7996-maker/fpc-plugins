#!/usr/bin/env python3
"""Fix the laggy scroll on Подписка in the Bedolaga cabinet Mini App.

Run this on the server that serves the cabinet:

    python3 deploy_v15_scroll_fix.py              # apply
    python3 deploy_v15_scroll_fix.py --dry-run    # show what would change
    python3 deploy_v15_scroll_fix.py --revert     # undo
    python3 deploy_v15_scroll_fix.py --root /srv/cabinet

WHAT IT CHANGES

Only `index.html`, and only by adding one <style> block before </head>. No
JavaScript chunk is touched, nothing is copied into a new /assets/vN/
directory, and no hashed filename is assumed. That is deliberate: every
earlier attempt (v4..v14) rewrote minified chunks against exact string
needles, which is what produced the black screens and the rollbacks. A style
block appended after the bundle stylesheet cannot break module loading, so the
worst case here is "no visual change", never a blank Mini App.

WHY IT WORKS

Two paint-only animations run for the whole lifetime of the Subscription
route and repaint on every frame, invalidating the raster tiles the compositor
is scrolling:

  * `.hover-border-gradient` animates `--border-angle`, a registered custom
    property feeding a conic-gradient border. Registered-property animations
    are not compositable, so Chromium repaints the element's full background
    each frame. Subscription renders two of them: the connect-device CTA and
    the purchase/renew CTA.
  * `.animate-unlimited-flow` animates `background-position` on the
    unlimited-traffic bar.

Measured on the real markup over 6 s of synthetic touch scrolling at 6x CPU
throttle (v15/measure.mjs): 2100 ms of main-thread render work -> 30 ms,
Paint 330 ms -> 0 ms, RasterTask 1250 ms -> 0 ms.

The rotation is frozen at a fixed angle on touch/small screens only; the
border still shows the accent sheen, and desktop keeps the animation.

NOT COVERED HERE

The Subscription route also re-renders once a second for the whole
traffic-refresh (30 s) and reissue (15 min) cooldown, because those are
decrementing counters held by the page component. That one needs a rebuild —
apply v15/0001-subscription-scroll.patch to bedolaga-cabinet.

If the cabinet runs in Docker, either bind-mount the patched index.html or
run this inside the container:

    docker cp <container>:/usr/share/nginx/html/index.html /tmp/index.html
    python3 deploy_v15_scroll_fix.py --root /tmp
    docker cp /tmp/index.html <container>:/usr/share/nginx/html/index.html
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

MARKER = "bedolaga-scroll-fix-v15"
BACKUP_SUFFIX = ".pre-v15"

# Roots tried in order when --root is not given. The first two are where the
# earlier cabinet-perf scripts deployed; the rest are stock install locations.
CANDIDATE_ROOTS = (
    "/srv/cabinet",
    "/opt/cabinet",
    "/opt/bedolaga-cabinet/dist",
    "/usr/share/nginx/html",
    "/var/www/cabinet",
)

# Mirrored so a later `docker compose up` / redeploy from the staging copy
# does not silently drop the fix.
MIRROR_ROOTS = ("/root/cabinet-dist",)

STYLE_BLOCK = f"""    <style id="{MARKER}">
      /* Подписка scroll fix.

         `--border-angle` is a registered custom property feeding a
         conic-gradient, so its animation cannot be composited: Chromium
         repaints the element's whole background every frame and re-rasters the
         scroll tiles under it. Subscription runs two of these at once (the
         connect-device CTA and the purchase CTA), which is what makes that
         route stutter under the finger. `background-position` on the
         unlimited-traffic bar is paint-only for the same reason.

         Freezing the angle keeps the accent edge exactly as it looks in any
         single frame of the rotation, at zero per-frame cost. Desktop keeps
         the motion. Same trade the cabinet already makes for backdrop-blur
         below 1024px. */
      @media (hover: none), (max-width: 1023px) {{
        .hover-border-gradient {{
          --border-angle: 135deg !important;
          animation: none !important;
        }}
        .animate-unlimited-flow {{
          animation: none !important;
        }}
      }}
    </style>
"""

BLOCK_RE = re.compile(
    r"[ \t]*<style id=\"" + re.escape(MARKER) + r"\">.*?</style>\n?",
    re.DOTALL,
)


def find_root(explicit: str | None) -> Path:
    if explicit:
        root = Path(explicit)
        if not (root / "index.html").is_file():
            raise SystemExit(f"no index.html under {root}")
        return root

    for candidate in CANDIDATE_ROOTS:
        root = Path(candidate)
        if (root / "index.html").is_file():
            return root

    raise SystemExit(
        "could not find the cabinet web root — pass it explicitly, e.g.\n"
        "  python3 deploy_v15_scroll_fix.py --root /path/to/cabinet"
    )


def check_bundle(root: Path) -> None:
    """Report whether the deployed CSS is the kind this fix targets."""
    sheets = sorted(root.glob("assets/**/*.css"))
    if not sheets:
        print("  ! no CSS bundle found under assets/ — cannot verify the target class")
        return

    hits = [p for p in sheets if ".hover-border-gradient" in p.read_text(errors="ignore")]
    if hits:
        names = ", ".join(p.name for p in hits)
        print(f"  bundle check: .hover-border-gradient found in {names}")
    else:
        print(
            "  ! .hover-border-gradient not found in any deployed stylesheet.\n"
            "    The override is harmless but will do nothing — this build\n"
            "    probably predates the animated gradient border."
        )


def apply(path: Path, dry_run: bool) -> bool:
    html = path.read_text()

    if "</head>" not in html:
        print(f"  ! {path}: no </head>, skipped")
        return False

    stripped = BLOCK_RE.sub("", html)
    patched = stripped.replace("</head>", STYLE_BLOCK + "  </head>", 1)

    if patched == html:
        print(f"  {path}: already up to date")
        return True

    if dry_run:
        print(f"  would patch {path}")
        return True

    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"  backup -> {backup}")

    path.write_text(patched)
    print(f"  patched {path}")
    return MARKER in path.read_text()


def revert(path: Path, dry_run: bool) -> bool:
    html = path.read_text()
    reverted = BLOCK_RE.sub("", html)

    if reverted == html:
        print(f"  {path}: nothing to revert")
        return True
    if dry_run:
        print(f"  would revert {path}")
        return True

    path.write_text(reverted)
    print(f"  reverted {path}")
    return MARKER not in path.read_text()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="cabinet web root (dir containing index.html)")
    parser.add_argument("--revert", action="store_true", help="remove the fix")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    root = find_root(args.root)
    print(f"cabinet root: {root}")

    targets = [root / "index.html"]
    for mirror in MIRROR_ROOTS:
        candidate = Path(mirror) / "index.html"
        if candidate.is_file() and candidate not in targets:
            targets.append(candidate)

    if not args.revert:
        check_bundle(root)

    action = revert if args.revert else apply
    ok = all(action(target, args.dry_run) for target in targets)

    if not ok:
        print("\nFAILED — verification did not pass")
        return 2

    if args.dry_run:
        print("\nDry run only, nothing written.")
    elif args.revert:
        print("\nReverted.")
    else:
        print(
            "\nApplied. index.html is served no-cache, so fully close the Mini App\n"
            "(swipe it away, not just Back) and reopen it from the bot."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
