# Плагины Starvell Cardinal

Нативные плагины пишутся **только для Starvell** и загружаются в папку `plugins/` или через Telegram (📤 Загрузить плагин).

## Быстрый старт

1. Скопируйте `_starvell_template.py` → `my_plugin.py`
2. Задайте уникальный `UUID`
3. Перезагрузите бота или нажмите 🔄 в панели плагинов

## Минимальный плагин

```python
from starvell_sdk import StarvellPlugin, on_message, MessageContext

NAME = "Hello"
UUID = "hello-plugin-001"
VERSION = "1.0.0"

class Plugin(StarvellPlugin):
    @on_message
    async def hello(self, ctx: MessageContext):
        if "привет" in ctx.text.lower():
            await ctx.reply("Здравствуйте!")
```

## Обязательные поля

| Поле | Описание |
|------|----------|
| `NAME` | Название в панели |
| `UUID` | Уникальный ID (не меняйте после публикации) |
| `VERSION` | Версия |
| `class Plugin` | Класс **обязательно** с именем `Plugin` |
| `StarvellPlugin` | Наследование от `starvell_sdk.StarvellPlugin` |

## События (@on_*)

| Декоратор | Когда срабатывает |
|-----------|-------------------|
| `@on_message` | Новое сообщение покупателя |
| `@on_order_paid` | Оплаченный заказ (CREATED) |
| `@on_order_completed` | Заказ завершён |
| `@on_order_status` | Смена статуса заказа |
| `@on_pre_delivery` | Перед автовыдачей (`ctx.cancel()` — отменить) |
| `@on_post_delivery` | После автовыдачи |
| `@on_bump` | После поднятия лотов |

## Контексты

- `MessageContext` — `ctx.text`, `ctx.chat_id`, `ctx.reply()`, `ctx.reply_watermarked()`
- `OrderContext` — `ctx.order_id`, `ctx.buyer_username`, `ctx.send_to_buyer()`
- `DeliveryContext` — `ctx.codes`, `ctx.delivery_text`, `ctx.cancel()`

## Настройки в Telegram

```python
SETTINGS_PAGE = True

def get_settings_schema(self) -> list[dict]:
    return [
        {"key": "enabled", "label": "Вкл", "type": "bool", "default": True},
        {"key": "trigger", "label": "Триггер", "type": "text", "default": "тест"},
        {"key": "mode", "label": "Режим", "type": "select", "default": "auto",
         "options": ["auto", "manual"]},
        {"key": "template", "label": "Шаблон", "type": "multiline", "default": "..."},
        {"key": "test", "label": "Проверить", "type": "action"},
    ]
```

Типы: `bool` (переключатель), `text`/`multiline`, `int`, `select`, `action`.

Доступ: `await self.get_cfg("enabled")` / `await self.set_cfg("enabled", True)`

## Жизненный цикл

| Метод | Когда |
|-------|-------|
| `on_load()` | Синхронно при загрузке файла |
| `on_startup()` | После старта бота и API |
| `on_shutdown()` | Перед выгрузкой |
| `on_unload()` | При hot-reload / остановке |

## Импорт SDK

```python
from starvell_sdk import StarvellPlugin, on_message, MessageContext
```

Файл `starvell_sdk.py` лежит в корне проекта — путь добавляется автоматически.

## FPC / FunPay плагины

Плагины с `FunPayAPI`, `telebot`, `from cardinal import Cardinal` **не запускаются** в Starvell Cardinal (отмечены 🎮 в панели).

## Примеры

- `starvell_example.py` — ответ на триггер + настройки
- `_starvell_template.py` — пустой шаблон (не загружается автоматически)
- `autodelivery_pro.py` — расширенная автовыдача
