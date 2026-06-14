# Telegram AI Userbot

Production-oriented Telegram userbot with Gemini AI, proxy rotation, and SQLite persistence.

## Modules

| File | Responsibility |
|------|----------------|
| `database.py` | SQLite schema, config, blacklists, error logs, chat context |
| `ai_engine.py` | Gemini HTTP client, proxy validation, parsing, rotation |
| `handlers.py` | Telethon events, setup flow, admin commands, AI replies |
| `main.py` | Initialization, login, reconnect loop |

## Quick start

```bash
cd userbot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_API_ID=123456
export TELEGRAM_API_HASH=your_api_hash
export TELEGRAM_PHONE=+70000000000

python main.py
```

On first login the bot sends a setup prompt to **Saved Messages**. Send credentials as:

```
AIzaSy...|ip:port:user:pass
```

Or run `/parse_proxy` to fetch public SOCKS5 proxies.

## Admin commands (owner only)

- `/logs` — last 50 errors
- `/status` — uptime, proxy, Gemini health
- `/set_prompt [text]` — change persona prompt
- `/blacklist_add <chat_id>` / `/blacklist_rm <chat_id>`
- `/restore_profile` — revert Telegram profile
- `/parse_proxy` — fetch and validate public proxies

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_API_ID` | yes | https://my.telegram.org |
| `TELEGRAM_API_HASH` | yes | API hash |
| `TELEGRAM_PHONE` | first login | Phone number |
| `TELEGRAM_SESSION_STRING` | optional | Skip file session |

Data is stored under `userbot/data/` and errors in `userbot/userbot_errors.log`.
