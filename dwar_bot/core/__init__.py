"""Ядро браузерного движка, anti-bot, Telegram, recovery и диагностика."""

from dwar_bot.core.anti_bot import AntiBot, CaptchaHandler, HumanBehavior
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError, NavigationError
from dwar_bot.core.recovery import CrashRecoveryManager
from dwar_bot.core.self_diagnostics import CrashDump, SelfDiagnostics
from dwar_bot.core.telegram_bot import RemoteControlState, TelegramRemoteControl

__all__ = [
    "AntiBot",
    "BrowserEngine",
    "BrowserEngineError",
    "CaptchaHandler",
    "CrashDump",
    "CrashRecoveryManager",
    "HumanBehavior",
    "NavigationError",
    "RemoteControlState",
    "SelfDiagnostics",
    "TelegramRemoteControl",
]
