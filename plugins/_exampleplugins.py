import logging

class Plugin:
    def __init__(self, cardinal, config):
        # cardinal — это экземпляр основного бота
        # config — это словарь с настройками из config.json
        self.cardinal = cardinal
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.logger.info("ExamplePlugin загружен")

    def setup(self):
        # Здесь регистрируются все обработчики событий
        self.cardinal.event_manager.register_handler('on_message', self.on_message)
        self.logger.info("Пример обработчика сообщений зарегистрирован")

    def on_message(self, data):
        # Эта функция будет вызываться при каждом новом сообщении
        message = data.get('message', '')
        chat_id = data.get('chat_id')

        # Проверяем, есть ли в сообщении слово "тест"
        if 'тест' in message.lower():
            self.cardinal.send_message(chat_id, "Плагин работает!")
            self.logger.info(f"Ответил на тестовое сообщение в чате {chat_id}")

    def unload(self):
        # Здесь происходит отписка от событий, чтобы избежать ошибок
        self.cardinal.event_manager.unregister_handler('on_message', self.on_message)
        self.logger.info("ExamplePlugin выгружен")
