"""dwar_bot — модульный асинхронный бот для игры «Легенда: Наследие Драконов».

Пакет разбит на слои:
    * :mod:`dwar_bot.config`  — конфигурация и селекторы;
    * :mod:`dwar_bot.logger`  — логирование (файл + Telegram);
    * :mod:`dwar_bot.auth`    — управление куки-сессиями;
    * :mod:`dwar_bot.core`    — браузер и имитация поведения;
    * :mod:`dwar_bot.modules` — прикладные модули (статы, бой, квесты, таймеры);
    * :mod:`dwar_bot.main`    — оркестратор.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
