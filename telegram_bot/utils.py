"""
Utility functions: logging setup, media metadata stripping,
proxy parsing, human-like delays, text obfuscation, etc.
"""

import asyncio
import io
import logging
import logging.handlers
import os
import random
import re
import string
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class ColorFormatter(logging.Formatter):
    """Console formatter with ANSI color codes."""

    COLORS = {
        logging.DEBUG:    "\033[36m",   # cyan
        logging.INFO:     "\033[32m",   # green
        logging.WARNING:  "\033[33m",   # yellow
        logging.ERROR:    "\033[31m",   # red
        logging.CRITICAL: "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, self.RESET)
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_file: str = "logs/bot.log",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure root logger with rotating file handler + colored console handler."""
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(
        ColorFormatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Suppress overly noisy third-party loggers
    for noisy in ("telethon", "httpx", "httpcore", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    logger.info("Logging initialised at level %s → %s", level, log_file)


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

PROXY_RE = re.compile(
    r"^(?P<type>socks5|socks4|http)://"
    r"(?:(?P<user>[^:@]+):(?P<password>[^@]+)@)?"
    r"(?P<host>[^:/]+):(?P<port>\d+)$",
    re.IGNORECASE,
)


def parse_proxy_line(line: str) -> Optional[Dict]:
    """
    Parse a single proxy line in URI format.
    Accepted formats:
      socks5://user:pass@host:port
      socks5://host:port
      http://host:port
    Returns dict with keys: type, host, port, username, password
    or None if the line is invalid / comment.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = PROXY_RE.match(line)
    if not m:
        logger.warning("Cannot parse proxy line: %r", line)
        return None
    return {
        "type": m.group("type").lower(),
        "host": m.group("host"),
        "port": int(m.group("port")),
        "username": m.group("user"),
        "password": m.group("password"),
    }


def load_proxies_from_file(path: str) -> List[Dict]:
    """Load and parse all proxy lines from a file."""
    result = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                proxy = parse_proxy_line(line)
                if proxy:
                    result.append(proxy)
    except FileNotFoundError:
        logger.warning("Proxy file not found: %s", path)
    return result


def proxy_to_telethon(proxy: Dict) -> Tuple:
    """
    Convert internal proxy dict to Telethon's proxy tuple format:
    (proxy_type, host, port, True, username, password)
    """
    import socks  # PySocks
    type_map = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http":   socks.HTTP,
    }
    return (
        type_map.get(proxy["type"], socks.SOCKS5),
        proxy["host"],
        proxy["port"],
        True,
        proxy.get("username"),
        proxy.get("password"),
    )


# ---------------------------------------------------------------------------
# Human-like async delays
# ---------------------------------------------------------------------------

async def human_delay(base_seconds: float, jitter_ratio: float = 0.3) -> None:
    """
    Sleep for base_seconds ± (base_seconds * jitter_ratio).
    Adds realistic randomness to avoid bot-detection patterns.
    """
    jitter = base_seconds * jitter_ratio
    delay = base_seconds + random.uniform(-jitter, jitter)
    delay = max(0.5, delay)
    await asyncio.sleep(delay)


async def random_typing_delay() -> None:
    """Simulate brief typing pause before sending a message (0.5–3s)."""
    await asyncio.sleep(random.uniform(0.5, 3.0))


# ---------------------------------------------------------------------------
# Text / caption obfuscation
# ---------------------------------------------------------------------------

# Zero-width Unicode characters that are invisible but change file hash
ZERO_WIDTH_CHARS = [
    "\u200b",  # ZERO WIDTH SPACE
    "\u200c",  # ZERO WIDTH NON-JOINER
    "\u200d",  # ZERO WIDTH JOINER
    "\u2060",  # WORD JOINER
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE
]


def obfuscate_text(text: str, num_chars: int = 3) -> str:
    """Insert invisible zero-width characters to avoid duplicate detection."""
    if not text:
        return text
    chars = random.choices(ZERO_WIDTH_CHARS, k=num_chars)
    positions = sorted(random.sample(range(len(text) + 1), min(num_chars, len(text) + 1)))
    result = list(text)
    for offset, (pos, ch) in enumerate(zip(positions, chars)):
        result.insert(pos + offset, ch)
    return "".join(result)


def clean_text(text: str) -> str:
    """Remove zero-width characters from text."""
    for ch in ZERO_WIDTH_CHARS:
        text = text.replace(ch, "")
    return text.strip()


def vary_caption(caption: str, ad_text: str, obfuscate: bool = True) -> str:
    """
    Build final caption: original text + advertising text.
    Optionally inserts invisible chars to prevent duplicate detection.
    """
    parts = []
    if caption:
        parts.append(caption.strip())
    if ad_text:
        parts.append(ad_text.strip())
    result = "\n\n".join(parts)
    if obfuscate and result:
        result = obfuscate_text(result)
    return result


# ---------------------------------------------------------------------------
# Media metadata stripping
# ---------------------------------------------------------------------------

def strip_jpeg_metadata(data: bytes) -> bytes:
    """
    Remove EXIF and other metadata from JPEG bytes.
    Preserves image data while removing APP1-APP15 markers (EXIF, IPTC, XMP, etc.)
    """
    if not data.startswith(b"\xff\xd8"):
        return data  # Not JPEG

    result = bytearray(b"\xff\xd8")
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            # Not a valid marker, copy rest as-is
            result.extend(data[i:])
            break
        marker = data[i + 1]
        # APP1–APP15 (0xE1–0xEF) and COM (0xFE) contain metadata
        if 0xE1 <= marker <= 0xEF or marker == 0xFE:
            if i + 4 > len(data):
                break
            length = struct.unpack(">H", data[i + 2: i + 4])[0]
            i += 2 + length  # skip this segment
        else:
            # Copy this segment verbatim
            if marker in (0xD8, 0xD9, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7):
                result.extend(data[i: i + 2])
                i += 2
            else:
                if i + 4 > len(data):
                    result.extend(data[i:])
                    break
                length = struct.unpack(">H", data[i + 2: i + 4])[0]
                result.extend(data[i: i + 2 + length])
                i += 2 + length
    return bytes(result)


def strip_png_metadata(data: bytes) -> bytes:
    """Remove tEXt, iTXt, zTXt, pHYs and other non-critical PNG chunks."""
    PNG_HEADER = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(PNG_HEADER):
        return data

    # Chunks to keep: IHDR, IDAT, IEND, PLTE, tRNS, gAMA, cHRM, sRGB, sBIT, bKGD, hIST, pHYs
    KEEP_CHUNKS = {b"IHDR", b"IDAT", b"IEND", b"PLTE", b"tRNS"}

    result = bytearray(PNG_HEADER)
    i = 8  # skip PNG header
    while i < len(data):
        if i + 8 > len(data):
            break
        length = struct.unpack(">I", data[i: i + 4])[0]
        chunk_type = data[i + 4: i + 8]
        chunk_end = i + 12 + length
        if chunk_type in KEEP_CHUNKS:
            result.extend(data[i:chunk_end])
        i = chunk_end
    return bytes(result)


async def strip_file_metadata(file_path: str) -> str:
    """
    Strip metadata from media file (JPEG / PNG / MP4 / others).
    Returns path to processed file (modifies in place for images,
    returns original path for video/audio where stripping is skipped).
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    try:
        if ext in (".jpg", ".jpeg"):
            data = path.read_bytes()
            stripped = strip_jpeg_metadata(data)
            path.write_bytes(stripped)
            logger.debug("Stripped JPEG metadata: %s (%d → %d bytes)", file_path, len(data), len(stripped))

        elif ext == ".png":
            data = path.read_bytes()
            stripped = strip_png_metadata(data)
            path.write_bytes(stripped)
            logger.debug("Stripped PNG metadata: %s (%d → %d bytes)", file_path, len(data), len(stripped))

        elif ext in (".mp4", ".mkv", ".mov", ".avi"):
            # For video, we do a lightweight re-encode of metadata only using ffmpeg if available
            await _strip_video_metadata_ffmpeg(file_path)

        elif ext in (".mp3", ".ogg", ".flac", ".m4a"):
            await _strip_audio_metadata(file_path)

    except Exception as exc:
        logger.warning("Failed to strip metadata from %s: %s", file_path, exc)

    return file_path


async def _strip_video_metadata_ffmpeg(file_path: str) -> None:
    """Use ffmpeg to copy video stream and strip all metadata."""
    out_path = file_path + ".tmp.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-i", file_path,
        "-map_metadata", "-1",
        "-c", "copy",
        out_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode == 0 and Path(out_path).exists():
        os.replace(out_path, file_path)
        logger.debug("Stripped video metadata: %s", file_path)
    else:
        if Path(out_path).exists():
            Path(out_path).unlink(missing_ok=True)


async def _strip_audio_metadata(file_path: str) -> None:
    """Remove ID3 / Vorbis tags from audio using mutagen."""
    try:
        import mutagen
        audio = mutagen.File(file_path)
        if audio is not None:
            audio.delete()
            audio.save()
            logger.debug("Stripped audio metadata: %s", file_path)
    except ImportError:
        logger.debug("mutagen not installed, skipping audio metadata strip")
    except Exception as exc:
        logger.warning("Audio metadata strip failed for %s: %s", file_path, exc)


# ---------------------------------------------------------------------------
# Channel / group link helpers
# ---------------------------------------------------------------------------

CHANNEL_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\\+)?([\w-]+)",
    re.IGNORECASE,
)


def normalise_channel_ref(ref: str) -> str:
    """
    Normalise a channel/group reference:
    - Strip https://t.me/ prefix
    - Strip leading @
    Returns bare username or +HASH for private invite links.
    """
    ref = ref.strip()
    m = CHANNEL_LINK_RE.match(ref)
    if m:
        return m.group(1)
    ref = ref.lstrip("@")
    return ref


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_size(num_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def format_uptime(start_time: datetime) -> str:
    """Format timedelta from start_time to now as 'Xd Xh Xm'."""
    delta = datetime.now() - start_time
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def format_dt(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format datetime or return '—' if None."""
    if dt is None:
        return "—"
    return dt.strftime(fmt)


def now_str() -> str:
    """ISO datetime string for the current moment (UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Retry decorator for async functions
# ---------------------------------------------------------------------------

def async_retry(
    max_attempts: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
):
    """
    Decorator that retries an async function on specified exceptions.
    Uses exponential back-off between attempts.
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__name__, max_attempts, exc,
                        )
                        raise
                    logger.warning(
                        "%s attempt %d/%d failed: %s. Retrying in %.1fs...",
                        func.__name__, attempt, max_attempts, exc, current_delay,
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Token-bucket rate limiter.
    Allows `rate` operations per `period` seconds.
    """

    def __init__(self, rate: int, period: float = 1.0):
        self.rate = rate
        self.period = period
        self._tokens = rate
        self._last_check = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_check
            self._last_check = now
            self._tokens += elapsed * (self.rate / self.period)
            self._tokens = min(self._tokens, self.rate)
            if self._tokens < 1:
                wait = (1 - self._tokens) * (self.period / self.rate)
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def generate_random_string(length: int = 8) -> str:
    """Generate a random alphanumeric string."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def truncate(text: str, max_len: int = 200) -> str:
    """Truncate a string and append '…' if needed."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def is_valid_telegram_username(username: str) -> bool:
    """Check if a string looks like a valid Telegram username."""
    username = username.lstrip("@")
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]{3,31}$", username))
