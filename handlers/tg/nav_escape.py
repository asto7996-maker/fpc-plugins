"""Приоритетная навигация — сброс FSM и выход из «зависших» состояний."""

from __future__ import annotations

from typing import Any

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message


def create_nav_router(ctx: Any) -> Router:
    router = Router(name="nav_escape")

    @router.message(CommandStart())
    @router.message(Command("menu", "cancel"))
    async def nav_home(message: Message, state: FSMContext) -> None:
        uid = message.from_user.id if message.from_user else 0
        if not await ctx._has_access(uid):
            await ctx._deny(message)
            return
        await state.clear()
        await ctx.cmd_start(message, state)

    return router
