"""
Модуль обработки медиафайлов.
Скачивание, очистка метаданных, перекодирование видео, наложение водяных знаков,
сжатие изображений.
"""

import asyncio
import hashlib
import io
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class MediaError(Exception):
    """Ошибка при обработке медиафайла."""
    pass


class MediaProcessor:
    """
    Обработчик медиафайлов: скачивание, очистка метаданных,
    перекодирование и наложение водяного знака.
    """

    SUPPORTED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
    SUPPORTED_VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".flv", ".3gp"}
    SUPPORTED_AUDIO_EXT = {".mp3", ".ogg", ".flac", ".wav", ".m4a", ".opus"}

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._download_dir = Path(config.get("download_dir", "downloads"))
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg_available = self._check_ffmpeg()
        self._ffprobe_available = self._check_ffprobe()

    def update_config(self, config: Dict[str, Any]) -> None:
        """Обновляет конфигурацию."""
        self._config = config
        self._download_dir = Path(config.get("download_dir", "downloads"))
        self._download_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _check_ffmpeg() -> bool:
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("ffmpeg не найден — перекодирование видео недоступно")
            return False

    @staticmethod
    def _check_ffprobe() -> bool:
        try:
            subprocess.run(
                ["ffprobe", "-version"],
                capture_output=True,
                timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_unique_path(self, extension: str) -> Path:
        """Генерирует уникальный путь для файла."""
        name = f"{uuid.uuid4().hex}{extension}"
        return self._download_dir / name

    async def process_media(
        self,
        file_path: Path,
        media_type: str,
    ) -> Tuple[Path, str]:
        """
        Выполняет полную обработку медиафайла.
        Возвращает (путь_к_обработанному_файлу, media_hash).
        """
        if not file_path.exists():
            raise MediaError(f"Файл не найден: {file_path}")

        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        max_size = self._config.get("max_file_size_mb", 50)
        if file_size_mb > max_size:
            raise MediaError(
                f"Файл слишком большой: {file_size_mb:.1f}MB > {max_size}MB"
            )

        media_hash = await self._compute_file_hash(file_path)

        result_path = file_path

        if self._config.get("strip_metadata", True):
            result_path = await self._strip_metadata(result_path, media_type)

        if media_type == "video" and self._config.get("reencode_video", False):
            result_path = await self._reencode_video(result_path)

        if media_type == "photo" and self._config.get("compress_images", False):
            result_path = await self._compress_image(result_path)

        if self._config.get("add_watermark", False) and media_type in ("photo", "video"):
            result_path = await self._add_watermark(result_path, media_type)

        return result_path, media_hash

    async def _compute_file_hash(self, file_path: Path) -> str:
        """Вычисляет SHA-256 хеш файла (первые 32 символа)."""
        def _hash():
            h = hashlib.sha256()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()[:32]

        return await asyncio.get_event_loop().run_in_executor(None, _hash)

    async def _strip_metadata(self, file_path: Path, media_type: str) -> Path:
        """
        Очищает метаданные файла.
        Для видео/аудио использует ffmpeg, для изображений — Pillow или побайтовую обработку.
        """
        if media_type in ("video", "audio", "animation", "video_note", "voice"):
            return await self._strip_video_metadata(file_path)
        elif media_type in ("photo", "document"):
            ext = file_path.suffix.lower()
            if ext in self.SUPPORTED_IMAGE_EXT:
                return await self._strip_image_metadata(file_path)
        return file_path

    async def _strip_video_metadata(self, file_path: Path) -> Path:
        """Очищает метаданные видео/аудио с помощью ffmpeg."""
        if not self._ffmpeg_available:
            return file_path

        output_path = self.get_unique_path(file_path.suffix)
        cmd = [
            "ffmpeg", "-y", "-i", str(file_path),
            "-map_metadata", "-1",
            "-fflags", "+bitexact",
            "-flags:v", "+bitexact",
            "-flags:a", "+bitexact",
            "-c", "copy",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            if proc.returncode != 0:
                logger.warning(
                    "ffmpeg strip metadata вернул код %d: %s",
                    proc.returncode,
                    stderr.decode(errors="replace")[:500],
                )
                if output_path.exists():
                    output_path.unlink()
                return file_path

            if file_path != output_path and file_path.exists():
                file_path.unlink()

            logger.debug("Метаданные очищены: %s", output_path.name)
            return output_path

        except asyncio.TimeoutError:
            logger.warning("Таймаут при очистке метаданных: %s", file_path)
            if output_path.exists():
                output_path.unlink()
            return file_path
        except Exception as e:
            logger.error("Ошибка при очистке метаданных: %s", e)
            if output_path.exists():
                output_path.unlink()
            return file_path

    async def _strip_image_metadata(self, file_path: Path) -> Path:
        """Очищает EXIF и другие метаданные из изображений."""
        try:
            from PIL import Image

            def _process():
                output_path = self.get_unique_path(file_path.suffix)
                with Image.open(file_path) as img:
                    data = list(img.getdata())
                    clean_img = Image.new(img.mode, img.size)
                    clean_img.putdata(data)

                    ext = file_path.suffix.lower()
                    if ext in (".jpg", ".jpeg"):
                        clean_img.save(output_path, "JPEG", quality=95)
                    elif ext == ".png":
                        clean_img.save(output_path, "PNG")
                    elif ext == ".webp":
                        clean_img.save(output_path, "WEBP", quality=95)
                    else:
                        clean_img.save(output_path)

                if file_path != output_path and file_path.exists():
                    file_path.unlink()
                return output_path

            result = await asyncio.get_event_loop().run_in_executor(None, _process)
            logger.debug("EXIF очищен: %s", result.name)
            return result

        except ImportError:
            logger.debug("Pillow не установлен, пропускаю очистку EXIF")
            return file_path
        except Exception as e:
            logger.warning("Ошибка при очистке EXIF: %s", e)
            return file_path

    async def _reencode_video(self, file_path: Path) -> Path:
        """Перекодирует видео с заданными параметрами."""
        if not self._ffmpeg_available:
            logger.warning("ffmpeg не найден, перекодирование пропущено")
            return file_path

        codec = self._config.get("video_codec", "libx264")
        bitrate = self._config.get("video_bitrate", "2M")
        output_path = self.get_unique_path(".mp4")

        cmd = [
            "ffmpeg", "-y", "-i", str(file_path),
            "-c:v", codec,
            "-b:v", bitrate,
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-map_metadata", "-1",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            timeout = self._config.get("reencode_timeout_seconds", 300)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            if proc.returncode != 0:
                logger.error(
                    "ffmpeg reencode вернул код %d: %s",
                    proc.returncode,
                    stderr.decode(errors="replace")[:500],
                )
                if output_path.exists():
                    output_path.unlink()
                return file_path

            if file_path != output_path and file_path.exists():
                file_path.unlink()

            logger.info("Видео перекодировано: %s", output_path.name)
            return output_path

        except asyncio.TimeoutError:
            logger.error("Таймаут при перекодировании видео: %s", file_path)
            if output_path.exists():
                output_path.unlink()
            return file_path
        except Exception as e:
            logger.error("Ошибка при перекодировании видео: %s", e)
            if output_path.exists():
                output_path.unlink()
            return file_path

    async def _compress_image(self, file_path: Path) -> Path:
        """Сжимает изображение."""
        try:
            from PIL import Image

            quality = self._config.get("image_quality", 85)

            def _compress():
                output_path = self.get_unique_path(file_path.suffix)
                with Image.open(file_path) as img:
                    if img.mode == "RGBA":
                        img = img.convert("RGB")
                    img.save(output_path, "JPEG", quality=quality, optimize=True)
                if file_path != output_path and file_path.exists():
                    file_path.unlink()
                return output_path

            result = await asyncio.get_event_loop().run_in_executor(None, _compress)
            logger.debug("Изображение сжато (quality=%d): %s", quality, result.name)
            return result

        except ImportError:
            logger.debug("Pillow не установлен, сжатие пропущено")
            return file_path
        except Exception as e:
            logger.warning("Ошибка при сжатии: %s", e)
            return file_path

    async def _add_watermark(self, file_path: Path, media_type: str) -> Path:
        """Добавляет водяной знак на фото или видео."""
        watermark_text = self._config.get("watermark_text", "")
        if not watermark_text:
            return file_path

        if media_type == "photo":
            return await self._add_image_watermark(file_path, watermark_text)
        elif media_type == "video":
            return await self._add_video_watermark(file_path, watermark_text)
        return file_path

    async def _add_image_watermark(self, file_path: Path, text: str) -> Path:
        """Наложение текстового водяного знака на изображение."""
        try:
            from PIL import Image, ImageDraw, ImageFont

            position = self._config.get("watermark_position", "bottom_right")
            opacity = self._config.get("watermark_opacity", 0.5)

            def _watermark():
                output_path = self.get_unique_path(file_path.suffix)
                with Image.open(file_path) as img:
                    if img.mode != "RGBA":
                        img = img.convert("RGBA")

                    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                    draw = ImageDraw.Draw(overlay)

                    font_size = max(img.size[0] // 30, 16)
                    try:
                        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
                    except (OSError, IOError):
                        font = ImageFont.load_default()

                    bbox = draw.textbbox((0, 0), text, font=font)
                    text_w = bbox[2] - bbox[0]
                    text_h = bbox[3] - bbox[1]

                    padding = 10
                    pos = self._calculate_position(
                        position, img.size, (text_w, text_h), padding
                    )

                    alpha_val = int(255 * opacity)
                    draw.text(pos, text, fill=(255, 255, 255, alpha_val), font=font)

                    result = Image.alpha_composite(img, overlay)
                    result = result.convert("RGB")
                    result.save(output_path, "JPEG", quality=95)

                if file_path != output_path and file_path.exists():
                    file_path.unlink()
                return output_path

            result = await asyncio.get_event_loop().run_in_executor(None, _watermark)
            logger.debug("Водяной знак добавлен: %s", result.name)
            return result

        except ImportError:
            logger.debug("Pillow не установлен, водяной знак пропущен")
            return file_path
        except Exception as e:
            logger.warning("Ошибка при добавлении водяного знака: %s", e)
            return file_path

    async def _add_video_watermark(self, file_path: Path, text: str) -> Path:
        """Наложение текстового водяного знака на видео с ffmpeg."""
        if not self._ffmpeg_available:
            return file_path

        position = self._config.get("watermark_position", "bottom_right")
        opacity = self._config.get("watermark_opacity", 0.5)
        output_path = self.get_unique_path(".mp4")

        pos_map = {
            "top_left": "x=10:y=10",
            "top_right": "x=w-tw-10:y=10",
            "bottom_left": "x=10:y=h-th-10",
            "bottom_right": "x=w-tw-10:y=h-th-10",
            "center": "x=(w-tw)/2:y=(h-th)/2",
        }
        xy = pos_map.get(position, pos_map["bottom_right"])

        escaped_text = text.replace("'", "\\'").replace(":", "\\:")
        alpha_hex = format(int(255 * opacity), '02x')

        drawtext_filter = (
            f"drawtext=text='{escaped_text}':"
            f"fontsize=24:fontcolor=white@{opacity}:{xy}"
        )

        cmd = [
            "ffmpeg", "-y", "-i", str(file_path),
            "-vf", drawtext_filter,
            "-c:a", "copy",
            "-map_metadata", "-1",
            str(output_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

            if proc.returncode != 0:
                logger.warning(
                    "ffmpeg watermark вернул код %d",
                    proc.returncode,
                )
                if output_path.exists():
                    output_path.unlink()
                return file_path

            if file_path != output_path and file_path.exists():
                file_path.unlink()

            logger.debug("Видео-водяной знак добавлен: %s", output_path.name)
            return output_path

        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("Ошибка при добавлении водяного знака на видео: %s", e)
            if output_path.exists():
                output_path.unlink()
            return file_path

    @staticmethod
    def _calculate_position(
        position: str,
        img_size: Tuple[int, int],
        text_size: Tuple[int, int],
        padding: int = 10,
    ) -> Tuple[int, int]:
        """Рассчитывает координаты для позиции водяного знака."""
        w, h = img_size
        tw, th = text_size
        positions = {
            "top_left": (padding, padding),
            "top_right": (w - tw - padding, padding),
            "bottom_left": (padding, h - th - padding),
            "bottom_right": (w - tw - padding, h - th - padding),
            "center": ((w - tw) // 2, (h - th) // 2),
        }
        return positions.get(position, positions["bottom_right"])

    async def get_video_duration(self, file_path: Path) -> Optional[float]:
        """Получает длительность видео в секундах."""
        if not self._ffprobe_available:
            return None

        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                return float(stdout.decode().strip())
        except (asyncio.TimeoutError, ValueError):
            pass
        return None

    async def get_video_dimensions(self, file_path: Path) -> Optional[Tuple[int, int]]:
        """Получает разрешение видео (width, height)."""
        if not self._ffprobe_available:
            return None

        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(file_path),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                parts = stdout.decode().strip().split("x")
                if len(parts) == 2:
                    return int(parts[0]), int(parts[1])
        except (asyncio.TimeoutError, ValueError):
            pass
        return None

    def cleanup_file(self, file_path: Path) -> None:
        """Безопасно удаляет файл."""
        try:
            if file_path.exists():
                file_path.unlink()
                logger.debug("Удалён файл: %s", file_path)
        except OSError as e:
            logger.warning("Не удалось удалить %s: %s", file_path, e)

    def cleanup_all_downloads(self) -> int:
        """Удаляет все файлы из папки загрузок. Возвращает количество удалённых файлов."""
        count = 0
        if self._download_dir.exists():
            for f in self._download_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        count += 1
                    except OSError:
                        pass
        logger.info("Очистка загрузок: удалено %d файлов", count)
        return count

    @property
    def download_dir(self) -> Path:
        return self._download_dir

    @property
    def ffmpeg_available(self) -> bool:
        return self._ffmpeg_available
