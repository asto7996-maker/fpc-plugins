# Paskod Premium Promo (1000 slots / 10 GB)

Limited Telegram promo for Paskod VPN with **premium custom emoji** from:

| Pack | Short name |
|------|------------|
| Adaptive Pixel Emoji | `AdaptivePixelEmoji` |
| @umarovman | `DasgirPubgm_by_fStikBot` |
| Translucent Pack by @devaiden | `TranslucentPack` |
| TG iOS & macOS Icons | `tgmacicons` |

## Message

HTML promo with `<tg-emoji emoji-id="...">` entities + green button:

`Активировать премиум [N/1000]` (`style=success`, Adaptive Pixel gift icon)

## Limits

- Max **1000** activations (`claimed` counter, seeded at 124)
- Each promo activation gets **3 days** premium with **`traffic_limit_gb = 10`**
- Users who already have an active paid subscription do not consume a slot

## Deploy (VPS)

- `patches/paskod_premium_promo.py` → `/app/app/handlers/paskod_premium_promo.py`
- `patches/bot.py` → registers handlers
- `data/paskod_premium_promo.json` → persistent counter

```bash
cd /root/remnawave-bedolaga-telegram-bot
docker compose up -d --force-recreate bot
```
