# Paskod Mini App — Gold-standard TMA layout

Adaptive Telegram Mini App shell with production-safe area handling.

## What’s included

- **Safe areas**: `env(safe-area-inset-*)` + Telegram `safeAreaInset` / `contentSafeAreaInset`
- **TMA detection**: expands WebApp, clears Close/••• when they overlay (fullscreen)
- **Layout**: Flex + Grid, `clamp()` type, no absolute main layout
- **Screens**: Home, Subscription, Balance, Referrals, Support
- **Dock**: fixed bottom nav with `safe-area-inset-bottom`

## Run

Open `index.html` in a browser, or serve statically:

```bash
python3 -m http.server 5173 --directory .
```

Then open in Telegram via a Mini App URL, or use [Telegram Web App](https://core.telegram.org/bots/webapps) tooling.

## Safe-area rules (summary)

| Mode | Top inset |
|------|-----------|
| Expanded (Close outside WebView) | ~12px breath |
| Fullscreen / overlay | `max(system, content)` + TG chrome (~48–50px) |
| Bottom dock | `env(safe-area-inset-bottom)` always |

Background paints edge-to-edge; padding lives on the sticky header and dock so notches stay filled with the theme color.
