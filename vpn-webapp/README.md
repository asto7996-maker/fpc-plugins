# Paskod Mini App — TMA gold-standard shell

Flexbox column architecture with Telegram safe areas, `100dvh` viewport, and a fixed bottom dock that never clips content.

## Architecture

```
.app (flex column, height: 100dvh, overflow: hidden)
├── .app-header   flex: 0 0 auto   ← below Telegram Close via safe-area + chrome
├── .app-main     flex: 1 1 auto   ← ONLY scroll container (overflow-y: auto)
│   ├── .app-main__inner          ← 16px under header, card gap 12px
│   └── .dock-spacer              ← dock height + 20px
└── .app-dock     position: fixed; z-index: 100
```

## Rules

- **Never** `100vh` — use `100dvh` / `min-height: 100dvh`
- Call `Telegram.WebApp.expand()` on boot
- Safe areas: `env(safe-area-inset-*)` + `safeAreaInset` / `contentSafeAreaInset`
- Fullscreen overlay: add `--tg-chrome-top` (~48–52px) when Close sits on the WebView
- Spacing tokens: `8 / 14 / 16 / 20` px · card gap `12px` · radius `16px`
- Header → welcome title: **exactly 16px**
- Countdown / money: `tabular-nums` + `nowrap`

## Ambient FX + perf

Background-only GPU stack (layout untouched): orbs / aurora / beams / grid / rings / stars / sparks.
Live build `pk-build:20260809-perf120`: no `mix-blend` / `filter:blur` on FX, glass blur only when idle, FX paused while scrolling, `touch-action: manipulation`, duplicate React portal backgrounds disabled via `__paskodDisableBg`.

## Run

```bash
python3 -m http.server 5173 --directory .
```
