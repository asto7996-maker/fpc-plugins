# Starvell Cardinal — Architecture (v2.0 ULTIMATE)

## Clean Architecture Layers

```
/workspace/
├── main.py                 # Entry → core.app.Application
├── config.py               # Settings, paths, constants
├── core/                   # Ядро
│   ├── app.py              # Application bootstrap
│   ├── bot_core.py         # BotCore + EventBus
│   ├── i18n.py             # Fluent localization
│   ├── database/           # SQLAlchemy async engine
│   ├── plugins/
│   │   ├── base.py         # BasePlugin ABC
│   │   ├── manager.py      # PluginEngine (hot-reload)
│   │   └── scheduler.py    # APScheduler wrapper
│   ├── delivery/
│   │   └── templates.py    # Плейсхолдеры автовыдачи 2.0
│   └── security/
│       └── payment_guard.py # Anti-Abuse fake payments
├── api/
│   ├── starvell_client.py  # HTTP client + 429 backoff
│   └── rate_limiter.py
├── handlers/
│   ├── builtin.py          # → handlers.py (Starvell events)
│   └── tg/                 # Telegram premium UI
├── keyboards/              # InlineKeyboard factories + pagination
├── plugins/                # Dynamic plugins (BasePlugin)
└── locales/                # Fluent .ftl files
```

## Plugin Engine

1. Наследуйте `core.plugins.base.BasePlugin`
2. Реализуйте `on_load()` / `on_unload()`
3. Опционально: `get_router()` для aiogram, `schedule_task()` для таймеров
4. Hot-reload: измените `.py` в `plugins/` — движок перезагрузит за ~5 сек

## Premium UI

- `handlers/tg/loading.py` — скелетоны `⚡️ Запрос обрабатывается…`
- `keyboards/factory.py` — пагинация `[1 / 10]`, навигация ◀️ 🏠 🔄
- `keyboards/plugins.py` — панель плагинов

## Security

- Оплаты **только** через API заказов (`fetch_orders`, status `CREATED`)
- `PaymentGuard` — алерт при поддельных «системных» сообщениях в чате

## Auto-Delivery 2.0 Placeholders

`{username}`, `{order_id}`, `{date}`, `{product_name}`, `{content}`, `{price}`, `{quantity}`

Пример: `plugins/autodelivery_pro.py`
