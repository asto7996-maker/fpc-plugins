"""FSM states for the control panel."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class InviteStates(StatesGroup):
    waiting_code = State()


class AddAccountStates(StatesGroup):
    phone = State()
    session = State()
    api = State()
    proxy = State()


class AddProxyStates(StatesGroup):
    raw = State()


class ImportTDataStates(StatesGroup):
    proxy = State()
    passcode = State()
    archive = State()


class DraftEditStates(StatesGroup):
    waiting_text = State()


class GeminiSettingsStates(StatesGroup):
    waiting_api_key = State()
    waiting_promote = State()
