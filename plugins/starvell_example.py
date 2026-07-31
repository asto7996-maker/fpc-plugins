NAME = "Starvell Example Plugin"
VERSION = "1.0.0"
DESCRIPTION = "Пример плагина для Starvell Cardinal"
CREDITS = "Starvell Cardinal"
UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

import logging


class Plugin:
    """Пример плагина — отвечает на слово «тест» в чате Starvell."""

    def __init__(self, cardinal, config):
        self.cardinal = cardinal
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.logger.info("ExamplePlugin загружен")

    def setup(self):
        self.cardinal.event_manager.register_handler("on_message", self.on_message)
        self.logger.info("Обработчик on_message зарегистрирован")

    async def on_message(self, data):
        message = data.get("message", "")
        chat_id = data.get("chat_id")
        ctx = data.get("ctx")
        if "тест" in message.lower() and chat_id and ctx:
            await self.cardinal.send_message(chat_id, "Плагин Starvell Cardinal работает! ✅", ctx.account_name)
            self.logger.info("Ответил на тестовое сообщение в чате %s", chat_id)

    def unload(self):
        self.cardinal.event_manager.unregister_handler("on_message", self.on_message)
        self.logger.info("ExamplePlugin выгружен")
