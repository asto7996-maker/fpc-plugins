"""
Модуль конфигурации.
Загружает, валидирует и предоставляет доступ к настройкам бота из YAML-файла.
"""

import os
import copy
import logging
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
    "telegram": {
        "api_id": 0,
        "api_hash": "",
        "bot_token": "",
        "session_name": "content_bot_session",
        "phone": "",
    },
    "admin": {
        "user_ids": [],
        "chat_ids": [],
    },
    "donors": [],
    "target": {
        "channel_id": 0,
        "channel_username": "",
    },
    "posting": {
        "min_delay_seconds": 300,
        "max_delay_seconds": 900,
        "caption_template": "",
        "append_link": "",
        "remove_original_caption": False,
        "remove_original_links": False,
        "add_signature": True,
        "signature_text": "",
        "post_immediately": False,
        "max_queue_size": 500,
        "schedule_hours": {"start": 0, "end": 24},
        "shuffle_queue": False,
    },
    "monitoring": {
        "enabled": True,
        "check_interval_seconds": 30,
        "max_post_age_hours": 24,
        "skip_forwards": False,
        "skip_edits": True,
        "download_timeout_seconds": 120,
    },
    "filters": {
        "min_text_length": 0,
        "max_text_length": 0,
        "min_views": 0,
        "required_media_types": [],
        "blocked_media_types": [],
        "keyword_whitelist": [],
        "keyword_blacklist": [],
        "regex_whitelist": [],
        "regex_blacklist": [],
        "skip_ads": True,
        "ad_keywords": [
            "реклама", "партнёр", "промокод", "скидка", "promo",
            "sponsor", "ad", "#реклама", "#ad",
        ],
        "duplicate_check": True,
        "duplicate_similarity_threshold": 0.85,
    },
    "media": {
        "download_dir": "downloads",
        "max_file_size_mb": 50,
        "strip_metadata": True,
        "reencode_video": False,
        "video_codec": "libx264",
        "video_bitrate": "2M",
        "add_watermark": False,
        "watermark_text": "",
        "watermark_position": "bottom_right",
        "watermark_opacity": 0.5,
        "cleanup_after_post": True,
        "compress_images": False,
        "image_quality": 85,
    },
    "database": {
        "path": "bot_database.db",
        "cleanup_days": 30,
    },
    "logging": {
        "level": "INFO",
        "file": "bot.log",
        "max_bytes": 10_000_000,
        "backup_count": 5,
        "console_output": True,
    },
    "proxy": {
        "enabled": False,
        "type": "socks5",
        "host": "",
        "port": 0,
        "username": "",
        "password": "",
    },
}


class ConfigError(Exception):
    """Ошибка конфигурации."""
    pass


class Config:
    """Менеджер конфигурации бота."""

    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = Path(config_path)
        self._data: Dict[str, Any] = {}
        self._loaded = False

    @property
    def data(self) -> Dict[str, Any]:
        if not self._loaded:
            raise ConfigError("Конфигурация не загружена. Вызовите load() сначала.")
        return self._data

    def load(self) -> None:
        """Загружает конфигурацию из YAML-файла."""
        if not self._config_path.exists():
            logger.warning(
                "Файл конфигурации %s не найден, создаю шаблон...",
                self._config_path,
            )
            self._create_default_config()
            raise ConfigError(
                f"Создан шаблонный файл {self._config_path}. "
                "Заполните его и перезапустите бота."
            )

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Ошибка парсинга YAML: {e}") from e

        self._data = self._deep_merge(copy.deepcopy(DEFAULT_CONFIG), raw)
        self._apply_env_overrides()
        self._validate()
        self._loaded = True
        logger.info("Конфигурация успешно загружена из %s", self._config_path)

    def reload(self) -> None:
        """Перезагружает конфигурацию."""
        self._loaded = False
        self.load()

    def save(self) -> None:
        """Сохраняет текущую конфигурацию в файл."""
        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.dump(
                self._data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        logger.info("Конфигурация сохранена в %s", self._config_path)

    def get(self, path: str, default: Any = None) -> Any:
        """
        Получает значение по точечному пути.
        Пример: config.get("posting.min_delay_seconds")
        """
        keys = path.split(".")
        value = self._data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, path: str, value: Any) -> None:
        """Устанавливает значение по точечному пути."""
        keys = path.split(".")
        target = self._data
        for key in keys[:-1]:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
            target = target[key]
        target[keys[-1]] = value

    @property
    def api_id(self) -> int:
        return self.get("telegram.api_id", 0)

    @property
    def api_hash(self) -> str:
        return self.get("telegram.api_hash", "")

    @property
    def bot_token(self) -> str:
        return self.get("telegram.bot_token", "")

    @property
    def session_name(self) -> str:
        return self.get("telegram.session_name", "content_bot_session")

    @property
    def phone(self) -> str:
        return self.get("telegram.phone", "")

    @property
    def admin_ids(self) -> List[int]:
        return self.get("admin.user_ids", [])

    @property
    def admin_chat_ids(self) -> List[int]:
        return self.get("admin.chat_ids", [])

    @property
    def donors(self) -> List[Union[str, int]]:
        return self.get("donors", [])

    @donors.setter
    def donors(self, value: List[Union[str, int]]) -> None:
        self._data["donors"] = value

    @property
    def target_channel(self) -> Union[str, int]:
        cid = self.get("target.channel_id", 0)
        if cid:
            return cid
        return self.get("target.channel_username", "")

    @property
    def posting(self) -> Dict[str, Any]:
        return self.get("posting", {})

    @property
    def monitoring(self) -> Dict[str, Any]:
        return self.get("monitoring", {})

    @property
    def filters_config(self) -> Dict[str, Any]:
        return self.get("filters", {})

    @property
    def media_config(self) -> Dict[str, Any]:
        return self.get("media", {})

    @property
    def database_path(self) -> str:
        return self.get("database.path", "bot_database.db")

    @property
    def proxy_settings(self) -> Optional[Dict[str, Any]]:
        if self.get("proxy.enabled", False):
            return {
                "proxy_type": self.get("proxy.type", "socks5"),
                "addr": self.get("proxy.host", ""),
                "port": self.get("proxy.port", 0),
                "username": self.get("proxy.username") or None,
                "password": self.get("proxy.password") or None,
            }
        return None

    def _create_default_config(self) -> None:
        """Создаёт файл конфигурации с дефолтными значениями."""
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["telegram"]["api_id"] = 12345
        config["telegram"]["api_hash"] = "your_api_hash_here"
        config["telegram"]["bot_token"] = "your_bot_token_here"
        config["telegram"]["phone"] = "+79001234567"
        config["admin"]["user_ids"] = [123456789]
        config["donors"] = ["@donor_channel_1", "@donor_channel_2"]
        config["target"]["channel_username"] = "@your_channel"

        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.dump(
                config,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """Рекурсивно объединяет два словаря."""
        result = base.copy()
        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _apply_env_overrides(self) -> None:
        """Применяет переменные окружения поверх конфигурации."""
        env_map = {
            "TG_API_ID": ("telegram.api_id", int),
            "TG_API_HASH": ("telegram.api_hash", str),
            "TG_BOT_TOKEN": ("telegram.bot_token", str),
            "TG_PHONE": ("telegram.phone", str),
            "TG_ADMIN_IDS": ("admin.user_ids", lambda x: [int(i) for i in x.split(",")]),
            "TG_TARGET_CHANNEL": ("target.channel_username", str),
            "TG_PROXY_HOST": ("proxy.host", str),
            "TG_PROXY_PORT": ("proxy.port", int),
        }
        for env_key, (config_path, converter) in env_map.items():
            env_val = os.environ.get(env_key)
            if env_val:
                try:
                    self.set(config_path, converter(env_val))
                    logger.debug("Применена переменная окружения %s", env_key)
                except (ValueError, TypeError) as e:
                    logger.warning(
                        "Не удалось применить %s=%s: %s", env_key, env_val, e
                    )

    def _validate(self) -> None:
        """Валидирует конфигурацию."""
        errors: List[str] = []

        if not self.api_id or self.api_id == 12345:
            errors.append("telegram.api_id не указан или является примером (12345)")
        if not self.api_hash or self.api_hash == "your_api_hash_here":
            errors.append("telegram.api_hash не указан")
        if not self.bot_token or self.bot_token == "your_bot_token_here":
            errors.append("telegram.bot_token не указан")
        if not self.admin_ids:
            errors.append("admin.user_ids пуст — нужен хотя бы один администратор")
        if not self.target_channel:
            errors.append("target канал не указан (channel_id или channel_username)")

        min_delay = self.get("posting.min_delay_seconds", 0)
        max_delay = self.get("posting.max_delay_seconds", 0)
        if min_delay < 0 or max_delay < 0:
            errors.append("Задержки публикации не могут быть отрицательными")
        if max_delay < min_delay:
            errors.append("max_delay_seconds не может быть меньше min_delay_seconds")

        schedule = self.get("posting.schedule_hours", {})
        start_h = schedule.get("start", 0)
        end_h = schedule.get("end", 24)
        if not (0 <= start_h <= 24) or not (0 <= end_h <= 24):
            errors.append("schedule_hours должны быть в диапазоне 0-24")

        if errors:
            msg = "Ошибки конфигурации:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ConfigError(msg)

    def __repr__(self) -> str:
        return f"Config(path={self._config_path}, loaded={self._loaded})"
