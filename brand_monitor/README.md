# Brand Monitor

Production-grade async system for brand-mention monitoring and first-line support replies in Telegram groups.

## Stack

- Python 3.10+ / asyncio (+ uvloop on Linux)
- Telethon — support agent userbots
- aiogram 3.x — admin panel
- aiosqlite — SQLite with explicit transactions
- PySocks — sticky SOCKS5 / HTTP proxies

## Structure

```
brand_monitor/
├── main.py                 # Entry + uvloop + graceful shutdown
├── config.py
├── core/
│   ├── userbot_manager.py  # Agent pool, filters, flood cooldown
│   ├── rate_limiter.py     # Sliding window limits
│   └── reply_coordinator.py# Cross-account pending reply sync
├── database/
├── admin/bot.py            # Live panel, stats, CSV, kill switch
└── utils/
    ├── fingerprint.py      # Stable device fingerprints
    ├── templates.py        # Nested spintax + humanization
    └── backoff.py
```

## Highlights

1. **Stable fingerprints** — `device_model` / `system_version` / `app_version` / `lang_code` generated once and stored; sticky proxy per agent
2. **Smart rate limits** — N/hour, N/day, random 10–25 min pause; `FloodWait` → `cooldown` + admin alert
3. **Filters** — bots/system/self ignored; length 10–500; stop-words table; cross-account claim + delayed-reply cancel
4. **Nested spintax** — `{A|{B|C}}` + optional emoji / typos / ZWSP
5. **Admin** — `/live` `/stats` `/export` `/kill` `/resume` + inline panel
6. **Stability** — uvloop, DB transactions, graceful `disconnect()` on shutdown

## Run

```bash
pip install -r brand_monitor/requirements.txt
cp brand_monitor/.env.example brand_monitor/.env
PYTHONPATH=. python -m brand_monitor
```

## Tests

```bash
PYTHONPATH=. python -m unittest brand_monitor.tests.test_core_logic brand_monitor.tests.test_userbot_manager -v
```
