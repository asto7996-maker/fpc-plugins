# Sweetshop premium emoji (BOT-T)

Равномерно проставляет premium `<tg-emoji emoji-id="…">` на **все названия** свободных сообщений `@sweetshopxxx_bot` (`bot_id=348122`).

## Паки (тематика 18+ / OnlyFans)

Из ранее присланных стикерпаков, приоритет:

1. `NeonEmojis`
2. `FireEmojiPack`
3. `TranslucentPack`
4. `AdaptivePixelEmoji`
5. `tgmacicons`

Карта: `emoji_map.json` (🔥❤️💋💎✨🔞💕💦🍑👑👀 …).

## Запуск

```bash
export BOTT_SECRET_KEY='…'   # ЛК BOT-T → обновление токена
# или: export TELEGRAM_BOT_TOKEN='…'

# превью
python3 apply_premium_titles.py --dry-run

# применить ко всем названиям
python3 apply_premium_titles.py

# названия + тексты
python3 apply_premium_titles.py --also-text
```

В ЛК BOT-T у сообщений с премиум-эмодзи: **Форматирование текста = HTML**.
