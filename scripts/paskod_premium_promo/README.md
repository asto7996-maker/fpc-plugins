# Paskod Premium Promo (1000 slots / 10 GB)

Limited Telegram promo for Paskod VPN.

## Message

HTML promo matching the marketing copy, with a green inline button:

`Активировать премиум [N/1000]` (`style=success`)

## Limits

- Max **1000** activations (`claimed` counter, seeded at 124)
- Each promo activation gets **3 days** premium (squad `prem`) with **`traffic_limit_gb = 10`**
  (caps white-list / bypass traffic via Remnawave `trafficLimitBytes`)
- Users who already have an active paid subscription do not consume a slot

## Deploy (VPS)

Files live on the bot host:

- `patches/paskod_premium_promo.py` → mounted to `/app/app/handlers/paskod_premium_promo.py`
- `patches/bot.py` → registers `register_paskod_premium_promo_handlers(dp)`
- `data/paskod_premium_promo.json` → persistent counter / claim map

Recreate:

```bash
cd /root/remnawave-bedolaga-telegram-bot
docker compose up -d --force-recreate bot
```

## Send to a user

```bash
docker exec remnawave_bot python /tmp/send_promo.py
```

Target for the initial campaign DM: `@xei1y` (`telegram_id=7835556726`).
