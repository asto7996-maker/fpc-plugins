# FunPay Gemini Link Automation

Userscript (Tampermonkey / Violentmonkey) для автоматизации продажи **Gemini link (18 месяцев)** на FunPay с интеграцией через API Telegram-бота поставщика.

## Установка

1. Установите [Tampermonkey](https://www.tampermonkey.net/) или Violentmonkey в браузер.
2. Создайте новый скрипт и вставьте содержимое файла `funpay-gemini-link.user.js`.
3. Сохраните скрипт и откройте [funpay.com](https://funpay.com) под аккаунтом продавца.
4. При первом запуске введите `BOT_API_URL` и `BOT_API_KEY` в диалоге настройки (или через кнопку **⚙ API** на панели).

## Конфигурация

Публичные настройки — в блоке `PUBLIC_CONFIG` в начале файла:

| Параметр | Описание |
|----------|----------|
| `ORDER_CHECK_INTERVAL_MS` | Интервал опроса новых заказов (мс) |
| `PRODUCT_KEYWORDS` | Ключевые слова в описании лота |
| `BOT_PRODUCT_ID` | ID продукта в API бота |
| `API_PATHS` | Пути эндпоинтов API |

Секреты (`BOT_API_URL`, `BOT_API_KEY`) хранятся в защищённом хранилище Tampermonkey (`GM_setValue`) и **не попадают в логи**.

## API Telegram-бота

Базовый URL: `BOT_API_URL`. Авторизация: `Authorization: Bearer <BOT_API_KEY>`.

### GET `/api/v1/balance`

```json
{ "balance": 1500.00, "currency": "RUB" }
```

### GET `/api/v1/stock?product=gemini_18m`

```json
{ "available": 12, "price": 890, "status": "ok" }
```

При отсутствии товара:

```json
{ "available": 0, "status": "out_of_stock" }
```

### POST `/api/v1/purchase`

```json
{ "product": "gemini_18m", "order_id": "ABC123" }
```

Ответ:

```json
{ "activation_link": "https://...", "status": "ok" }
```

Пути настраиваются в `PUBLIC_CONFIG.API_PATHS`.

## Логика работы

### Новые заказы (Order Event)

- Мониторинг **только оплаченных заказов** через polling `/orders/trade` + перехват fetch/runner.
- Фильтрация по ключевым словам `PRODUCT_KEYWORDS`.
- Цепочка: баланс → наличие → покупка → отправка ссылки в чат FunPay.
- Retry до 3 раз с интервалом 5 сек при сетевых/5xx ошибках.

### Команды в чате (до покупки)

| Команда | Действие |
|---------|----------|
| `/Gemini` | Главное меню |
| `/Gemini check` | Проверить наличие |
| `/Gemini preorder` | Зарегистрировать предзаказ |
| `/Gemini help` | Отключить автоответ, уведомить продавца |

### UI-панель продавца

Микро-панель в правом нижнем углу FunPay:

- Статус работы
- Баланс в TG-боте
- Количество Gemini 18 мес.
- Лог последних 5 действий

## Отладка

Откройте DevTools → Console. Все события помечены префиксом `[FGP YYYY-MM-DD HH:MM:SS]`.

Глобальный объект для отладки: `window.FunPayGeminiPlugin`.

## Примечание

Этот плагин написан как **JavaScript userscript** для браузера. Для серверной автоматизации через FunPay Cardinal используйте Python-плагины в папке `plugins/`.
