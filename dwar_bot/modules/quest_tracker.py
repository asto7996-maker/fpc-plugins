"""Трекер сюжета: диалоги NPC, выбор веток, принятие/сдача квестов.

Логика намеренно консервативна: бот читает текст диалога, применяет
пользовательские предпочтения по ключевым словам (``preferred_keywords``),
а при их отсутствии выбирает первый доступный вариант ответа. Это позволяет
проходить линейные диалоги без риска «застрять».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..config import CONFIG
from ..core.anti_bot import HumanBehavior
from ..core.browser import BrowserManager
from ..logger import get_logger, log_exception

logger = get_logger(__name__)


@dataclass
class DialogState:
    text: str = ""
    options: List[str] = field(default_factory=list)
    has_accept: bool = False
    has_complete: bool = False

    @property
    def is_active(self) -> bool:
        return bool(self.text or self.options or self.has_accept or self.has_complete)


class QuestTracker:
    """Проходит диалоги NPC и управляет квестами."""

    def __init__(
        self,
        browser: BrowserManager,
        human: HumanBehavior,
        preferred_keywords: Optional[Sequence[str]] = None,
        avoid_keywords: Optional[Sequence[str]] = None,
    ) -> None:
        self._browser = browser
        self._human = human
        self._sel = CONFIG.selectors
        # Ключевые слова, которые предпочитаем в ответах (напр. «да», «продолжить»).
        self._preferred = [k.lower() for k in (preferred_keywords or (
            "продолж", "далее", "да", "принять", "взять", "согласен", "хорошо"
        ))]
        # Ключевые слова, которых избегаем (напр. «отказаться», «атаковать»).
        self._avoid = [k.lower() for k in (avoid_keywords or (
            "отказ", "выйти", "уйти", "напасть", "атаков",
        ))]

    async def dialog_active(self) -> bool:
        """Есть ли на странице активный диалог NPC."""
        return await self._browser.exists(self._sel.dialog_container, timeout_ms=2000)

    async def read_dialog(self) -> DialogState:
        """Считывает текущее состояние диалога."""
        state = DialogState()
        try:
            container = await self._browser.page.query_selector(
                self._sel.dialog_container
            )
            if container is None:
                return state

            text_el = await container.query_selector(self._sel.dialog_text)
            if text_el is not None:
                state.text = (await text_el.inner_text()).strip()

            option_elements = await container.query_selector_all(
                self._sel.dialog_option
            )
            for element in option_elements:
                try:
                    text = (await element.inner_text()).strip()
                    if text:
                        state.options.append(text)
                except Exception:  # noqa: BLE001
                    continue

            state.has_accept = await self._browser.exists(
                self._sel.quest_accept, timeout_ms=800
            )
            state.has_complete = await self._browser.exists(
                self._sel.quest_complete, timeout_ms=800
            )
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "Ошибка чтения диалога", exc)
        return state

    def _choose_option_index(self, options: Sequence[str]) -> Optional[int]:
        """Выбирает индекс наиболее подходящего варианта ответа."""
        if not options:
            return None

        lowered = [opt.lower() for opt in options]

        # 1. Предпочтительные ключевые слова.
        for keyword in self._preferred:
            for idx, opt in enumerate(lowered):
                if keyword in opt and not self._is_avoided(opt):
                    return idx

        # 2. Первый вариант, не входящий в «избегаемые».
        for idx, opt in enumerate(lowered):
            if not self._is_avoided(opt):
                return idx

        # 3. Совсем крайний случай — первый.
        return 0

    def _is_avoided(self, text_low: str) -> bool:
        return any(keyword in text_low for keyword in self._avoid)

    async def advance_dialog(self) -> bool:
        """Делает один шаг в диалоге. Возвращает ``True``, если что-то сделали."""
        state = await self.read_dialog()
        if not state.is_active:
            return False

        # Приоритет: завершить квест -> принять квест -> выбрать реплику.
        if state.has_complete:
            logger.info("Сдаю квест")
            if await self._human.click(self._sel.quest_complete, timeout_ms=3000):
                await self._human.read_pause()
                return True

        if state.has_accept:
            logger.info("Принимаю квест")
            if await self._human.click(self._sel.quest_accept, timeout_ms=3000):
                await self._human.read_pause()
                return True

        if state.options:
            index = self._choose_option_index(state.options)
            if index is None:
                return False
            chosen = state.options[index]
            logger.info("Выбираю реплику [%d]: %s", index, chosen[:60])
            # Кликаем по конкретному варианту по его порядковому номеру.
            selector = f"{self._sel.dialog_option} >> nth={index}"
            if await self._human.click(selector, timeout_ms=3000):
                await self._human.read_pause()
                return True

        return False

    async def run_dialog(self, max_steps: int = 25) -> int:
        """Проходит диалог до конца (или до лимита шагов).

        Возвращает число выполненных шагов.
        """
        steps = 0
        try:
            while steps < max_steps:
                if not await self.dialog_active():
                    break
                advanced = await self.advance_dialog()
                if not advanced:
                    break
                steps += 1
                await self._human.action_pause()
            if steps:
                logger.info("Диалог пройден за %d шаг(ов)", steps)
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "Ошибка прохождения диалога", exc)
            if CONFIG.runtime.screenshot_on_error:
                await self._browser.screenshot("quest_error")
        return steps


__all__ = ["QuestTracker", "DialogState"]
