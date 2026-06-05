"""
Configuration module for Telegram Content Bot.
Loads settings from environment variables / .env file with validation and defaults.
"""

import os
import sys
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


def _parse_int_list(value: str, default: List[int] = None) -> List[int]:
    """Parse comma-separated string into list of ints."""
    if not value or not value.strip():
        return default or []
    result = []
    for item in value.split(","):
        item = item.strip()
        if item:
            try:
                result.append(int(item))
            except ValueError:
                logger.warning(f"Cannot parse int value: {item!r}")
    return result


def _parse_str_list(value: str, default: List[str] = None) -> List[str]:
    """Parse comma-separated string into list of strings."""
    if not value or not value.strip():
        return default or []
    return [x.strip() for x in value.split(",") if x.strip()]


def _get_bool(key: str, default: bool = False) -> bool:
    """Get boolean env var."""
    val = os.getenv(key, str(default)).lower().strip()
    return val in ("true", "1", "yes", "on")


def _get_int(key: str, default: int = 0) -> int:
    """Get integer env var with fallback."""
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        logger.warning(f"Invalid int for {key}, using default {default}")
        return default


def _get_float(key: str, default: float = 0.0) -> float:
    """Get float env var with fallback."""
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        logger.warning(f"Invalid float for {key}, using default {default}")
        return default


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TelegramConfig:
    """Core Telegram credentials."""
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    api_id: int = field(default_factory=lambda: _get_int("API_ID", 0))
    api_hash: str = field(default_factory=lambda: os.getenv("API_HASH", ""))
    admin_ids: List[int] = field(
        default_factory=lambda: _parse_int_list(os.getenv("ADMIN_IDS", ""))
    )

    def validate(self) -> List[str]:
        errors = []
        if not self.bot_token:
            errors.append("BOT_TOKEN is required")
        if not self.api_id:
            errors.append("API_ID is required for user-level operations")
        if not self.api_hash:
            errors.append("API_HASH is required for user-level operations")
        if not self.admin_ids:
            errors.append("ADMIN_IDS must contain at least one admin user ID")
        return errors


@dataclass
class ContentMachineConfig:
    """Content reposting machine settings."""
    target_channel: str = field(
        default_factory=lambda: os.getenv("TARGET_CHANNEL", "")
    )
    donor_channels: List[str] = field(
        default_factory=lambda: _parse_str_list(os.getenv("DONOR_CHANNELS", ""))
    )
    ad_text: str = field(
        default_factory=lambda: os.getenv("AD_TEXT", "")
    )
    ad_button_text: str = field(
        default_factory=lambda: os.getenv("AD_BUTTON_TEXT", "Подписаться")
    )
    ad_button_url: str = field(
        default_factory=lambda: os.getenv("AD_BUTTON_URL", "")
    )
    # How often to check donors for new posts (seconds)
    check_interval: int = field(
        default_factory=lambda: _get_int("CM_CHECK_INTERVAL", 300)
    )
    # Minimum delay between reposts (seconds)
    repost_min_delay: int = field(
        default_factory=lambda: _get_int("CM_REPOST_MIN_DELAY", 1800)
    )
    # Maximum reposts per day across all donors
    max_posts_per_day: int = field(
        default_factory=lambda: _get_int("CM_MAX_POSTS_PER_DAY", 48)
    )
    # Strip EXIF / media metadata before reposting
    strip_metadata: bool = field(
        default_factory=lambda: _get_bool("CM_STRIP_METADATA", True)
    )
    # Append random unicode zero-width chars to caption to avoid duplicate detection
    obfuscate_caption: bool = field(
        default_factory=lambda: _get_bool("CM_OBFUSCATE_CAPTION", True)
    )
    # Only repost posts newer than this many hours
    max_post_age_hours: int = field(
        default_factory=lambda: _get_int("CM_MAX_POST_AGE_HOURS", 48)
    )
    # Skip posts that already contain certain keywords
    skip_keywords: List[str] = field(
        default_factory=lambda: _parse_str_list(os.getenv("CM_SKIP_KEYWORDS", ""))
    )
    # Temp directory for downloaded media
    media_dir: str = field(
        default_factory=lambda: os.getenv("MEDIA_DIR", "media")
    )


@dataclass
class ParserConfig:
    """User parser settings."""
    # Source groups to parse (username or invite link)
    source_groups: List[str] = field(
        default_factory=lambda: _parse_str_list(os.getenv("PARSER_GROUPS", ""))
    )
    # Only consider users active within this many hours
    activity_hours: int = field(
        default_factory=lambda: _get_int("PARSER_ACTIVITY_HOURS", 24)
    )
    # Maximum users to collect per run
    max_per_run: int = field(
        default_factory=lambda: _get_int("PARSER_MAX_PER_RUN", 500)
    )
    # Delay between fetching member batches (seconds)
    batch_delay: float = field(
        default_factory=lambda: _get_float("PARSER_BATCH_DELAY", 2.0)
    )
    # How often to run the parser (seconds)
    run_interval: int = field(
        default_factory=lambda: _get_int("PARSER_RUN_INTERVAL", 7200)
    )
    # Skip bots from results
    skip_bots: bool = field(
        default_factory=lambda: _get_bool("PARSER_SKIP_BOTS", True)
    )
    # Skip already-invited users
    skip_already_invited: bool = field(
        default_factory=lambda: _get_bool("PARSER_SKIP_ALREADY_INVITED", True)
    )


@dataclass
class InviterConfig:
    """Inviter / DM-sender settings."""
    # Channel or group to invite users to
    invite_target: str = field(
        default_factory=lambda: os.getenv("INVITE_TARGET", "")
    )
    # DM message template (use {username} placeholder)
    dm_message: str = field(
        default_factory=lambda: os.getenv(
            "DM_MESSAGE",
            "Привет! У нас есть закрытый канал с эксклюзивным контентом. "
            "Присоединяйся: {link}"
        )
    )
    dm_button_text: str = field(
        default_factory=lambda: os.getenv("DM_BUTTON_TEXT", "Перейти в канал")
    )
    dm_button_url: str = field(
        default_factory=lambda: os.getenv("DM_BUTTON_URL", "")
    )
    # Seconds between invite actions (base, actual value will be randomised ±30%)
    invite_delay: float = field(
        default_factory=lambda: _get_float("INVITER_DELAY", 45.0)
    )
    # Max invites per account per day
    max_invites_per_account: int = field(
        default_factory=lambda: _get_int("INVITER_MAX_PER_ACCOUNT", 30)
    )
    # Max DMs per account per day
    max_dm_per_account: int = field(
        default_factory=lambda: _get_int("INVITER_MAX_DM_PER_ACCOUNT", 15)
    )
    # Pause inviter after consecutive errors
    error_pause_seconds: int = field(
        default_factory=lambda: _get_int("INVITER_ERROR_PAUSE", 300)
    )
    # Stop account after this many consecutive errors
    max_consecutive_errors: int = field(
        default_factory=lambda: _get_int("INVITER_MAX_ERRORS", 5)
    )


@dataclass
class AccountsConfig:
    """Multi-account management settings."""
    sessions_dir: str = field(
        default_factory=lambda: os.getenv("SESSIONS_DIR", "sessions")
    )
    proxy_file: str = field(
        default_factory=lambda: os.getenv("PROXY_FILE", "proxies.txt")
    )
    # Rotate accounts every N actions
    rotate_every: int = field(
        default_factory=lambda: _get_int("ACCOUNTS_ROTATE_EVERY", 10)
    )


@dataclass
class DatabaseConfig:
    """Database settings."""
    path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "data/bot.db")
    )


@dataclass
class LoggingConfig:
    """Logging settings."""
    level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )
    file: str = field(
        default_factory=lambda: os.getenv("LOG_FILE", "logs/bot.log")
    )
    max_bytes: int = field(
        default_factory=lambda: _get_int("LOG_MAX_BYTES", 10 * 1024 * 1024)
    )
    backup_count: int = field(
        default_factory=lambda: _get_int("LOG_BACKUP_COUNT", 5)
    )


@dataclass
class AppConfig:
    """Root application configuration container."""
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    content_machine: ContentMachineConfig = field(default_factory=ContentMachineConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)
    inviter: InviterConfig = field(default_factory=InviterConfig)
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> bool:
        """Validate mandatory settings. Returns True if valid."""
        errors = self.telegram.validate()
        if errors:
            for err in errors:
                logger.error(f"Config error: {err}")
            return False
        return True

    def ensure_dirs(self):
        """Create all required directories if they don't exist."""
        dirs = [
            self.accounts.sessions_dir,
            self.content_machine.media_dir,
            Path(self.database.path).parent,
            Path(self.logging.file).parent,
        ]
        for d in dirs:
            Path(d).mkdir(parents=True, exist_ok=True)


# Singleton config instance
config = AppConfig()
