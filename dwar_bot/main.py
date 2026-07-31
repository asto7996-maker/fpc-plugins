"""
Точка входа DwarBot: инициализация MasterController и запуск FSM.

Использование:
  python main.py
  python -m dwar_bot
  python -m dwar_bot.main --mode farming --server w2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import List, Optional

from dwar_bot.config import load_config
from dwar_bot.core.master_controller import (
    MasterController,
    PrimaryMode,
    primary_mode_from_env,
)
from dwar_bot.logger import get_logger, log_exception, setup_logging
from dwar_bot.modules.auction_trader import TradeOffer

DEFAULT_HP_HEAL_PCT = 55.0
DEFAULT_HP_CRITICAL_PCT = 35.0


def _load_quest_script(path: Optional[str]) -> List[dict]:
    if not path:
        return []
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Квест-сценарий не найден: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "steps" in data:
        data = data["steps"]
    if not isinstance(data, list):
        raise ValueError("Квест-сценарий должен быть JSON-списком шагов")
    return [step for step in data if isinstance(step, dict)]


def _load_trade_watch_list(path: Optional[str]) -> List[TradeOffer]:
    if not path:
        return []
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        return []
    data = json.loads(file_path.read_text(encoding="utf-8"))
    items = data.get("watch_list", data) if isinstance(data, dict) else data
    offers: List[TradeOffer] = []
    if not isinstance(items, list):
        return offers
    for row in items:
        if not isinstance(row, dict):
            continue
        name = str(row.get("item_name") or row.get("name") or "").strip()
        if not name:
            continue
        offers.append(
            TradeOffer(
                item_name=name,
                target_price=float(row.get("target_price", row.get("price", 0)) or 0),
                max_quantity=int(row.get("max_quantity", row.get("qty", 1)) or 1),
                category_id=str(row.get("category_id", "") or ""),
            )
        )
    return offers


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DwarBot FSM — Легенда: Наследие Драконов"
    )
    parser.add_argument(
        "--mode",
        choices=("farming", "trading", "quests", "idle"),
        default="",
        help="Основной режим FSM (иначе DWAR_PRIMARY_MODE / farming)",
    )
    parser.add_argument("--quest-script", type=str, default="", help="JSON квест-шагов")
    parser.add_argument(
        "--trade-watch",
        type=str,
        default="",
        help="JSON watch-list для аукциона",
    )
    parser.add_argument(
        "--farm-targets",
        type=str,
        default="",
        help="Цели фарма через запятую (например: аметист,трава)",
    )
    parser.add_argument("--hp-heal", type=float, default=DEFAULT_HP_HEAL_PCT)
    parser.add_argument("--hp-critical", type=float, default=DEFAULT_HP_CRITICAL_PCT)
    parser.add_argument("--server", choices=("w1", "w2"), default="")
    return parser.parse_args(argv)


async def async_main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.server:
        os.environ["DWAR_SERVER"] = args.server
    if args.mode:
        os.environ["DWAR_PRIMARY_MODE"] = args.mode

    bot_config = load_config()
    setup_logging(bot_config)
    logger = get_logger("dwar_bot.main")

    try:
        quest_script = _load_quest_script(args.quest_script or None)
    except Exception as exc:
        logger.error("Квест-сценарий: %s", exc)
        return 2

    trade_watch = _load_trade_watch_list(args.trade_watch or None)
    farm_targets = [
        p.strip()
        for p in (args.farm_targets or os.getenv("DWAR_FARM_TARGETS", "")).split(",")
        if p.strip()
    ]
    mode = primary_mode_from_env(PrimaryMode.FARMING)

    controller = MasterController(
        bot_config,
        quest_script=quest_script,
        hp_heal_pct=args.hp_heal,
        hp_critical_pct=args.hp_critical,
        primary_mode=mode,
        trade_watch_list=trade_watch,
        farm_targets=farm_targets,
    )

    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        controller.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            signal.signal(sig, lambda *_: controller.request_stop())

    exit_code = 0
    try:
        # run_state_machine сам делает initialize + graceful_shutdown
        await controller.run_state_machine()
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt")
        controller.request_stop()
    except Exception as exc:
        log_exception(logger, "Фатальная ошибка FSM", exc)
        try:
            await controller._capture_crash(exc, "main.fatal")  # noqa: SLF001
        except Exception:
            pass
        exit_code = 1
        try:
            await asyncio.wait_for(
                controller.graceful_shutdown(),
                timeout=bot_config.graceful_shutdown_timeout_sec,
            )
        except Exception as stop_exc:
            log_exception(logger, "Ошибка shutdown после fatal", stop_exc)

    return exit_code


def main() -> None:
    try:
        code = asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        code = 130
    sys.exit(code)


# Обратная совместимость со старым именем
BotOrchestrator = MasterController


if __name__ == "__main__":
    main()
