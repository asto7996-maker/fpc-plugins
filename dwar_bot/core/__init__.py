"""Ядро браузерного движка, anti-bot, Telegram и recovery."""

from dwar_bot.core.anti_bot import AntiBot, CaptchaHandler, HumanBehavior
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError, NavigationError
from dwar_bot.core.recovery import CrashRecoveryManager
from dwar_bot.core.telegram_bot import RemoteControlState, TelegramRemoteControl

__all__ = [
    "AntiBot",
    "BrowserEngine",
    "BrowserEngineError",
    "CaptchaHandler",
    "CrashRecoveryManager",
    "HumanBehavior",
    "NavigationError",
    "RemoteControlState",
    "TelegramRemoteControl",
]
