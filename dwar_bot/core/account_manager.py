"""
Multi-account registry — each Telegram user owns an isolated game session.

Layout
------
dwar_bot/accounts/
  registry.json
  tg-<user_id>/
    account.json
    cookies.json
    state.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from dwar_bot.auth.oauth_login import extract_access_token
from dwar_bot.config import BASE_DIR, GAME_WORLD_URL
from dwar_bot.core.game_client import (
    DwarGameClient,
    TokenExpiredError,
    load_cookie_dict,
    persist_session_cookies,
)
from dwar_bot.modules.bot_settings import BotSettings

logger = logging.getLogger(__name__)

ACCOUNTS_DIR: Path = BASE_DIR / "accounts"
REGISTRY_FILE: Path = ACCOUNTS_DIR / "registry.json"

_SAFE_ID = re.compile(r"^[0-9-]{3,32}$")


def slot_id_for_user(telegram_user_id: str) -> str:
    uid = str(telegram_user_id).strip()
    if not _SAFE_ID.match(uid):
        raise ValueError(f"unsafe telegram user id: {telegram_user_id!r}")
    return f"tg-{uid.lstrip('-')}" if not uid.startswith("-") else f"tg{uid}"


@dataclass
class AccountSpec:
    telegram_user_id: str
    slot_id: str
    notify_chat_id: str = ""
    world: str = "w1"
    world_url: str = ""
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    label: str = ""

    def __post_init__(self) -> None:
        if not self.notify_chat_id:
            self.notify_chat_id = self.telegram_user_id
        if not self.world_url:
            self.world_url = f"https://{self.world}.dwar.ru"
        if not self.slot_id:
            self.slot_id = slot_id_for_user(self.telegram_user_id)

    @property
    def root(self) -> Path:
        return ACCOUNTS_DIR / self.slot_id

    @property
    def cookie_path(self) -> Path:
        return self.root / "cookies.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def meta_path(self) -> Path:
        return self.root / "account.json"

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        mode = 0o700
        try:
            self.root.chmod(mode)
        except OSError:
            pass

    def save_meta(self) -> None:
        self.ensure_dirs()
        self.meta_path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load_meta(cls, slot_dir: Path) -> Optional["AccountSpec"]:
        meta = slot_dir / "account.json"
        if not meta.exists():
            return None
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            return cls(**{
                k: v for k, v in data.items()
                if k in cls.__dataclass_fields__
            })
        except Exception as exc:
            logger.warning("Bad account meta %s: %s", meta, exc)
            return None


@dataclass
class AccountRuntime:
    """Live bot/client for one Telegram user."""

    spec: AccountSpec
    client: DwarGameClient
    bot: Any  # DwarBot — avoided circular import at type time
    settings: BotSettings
    task: Optional[asyncio.Task] = None
    waiting_for_cookies: bool = False

    @property
    def user_id(self) -> str:
        return self.spec.telegram_user_id

    @property
    def nick(self) -> str:
        try:
            return str(getattr(self.bot, "_char", None) and self.bot._char.nick or "")
        except Exception:
            return ""

    def status_brief(self) -> str:
        st = {}
        try:
            st = self.bot.get_status()
        except Exception:
            pass
        nick = st.get("nick") or "—"
        if self.waiting_for_cookies or not st.get("token_ok", True):
            return f"⏳ ждёт куки ({self.user_id})"
        return (
            f"{nick} Lv{st.get('level', '?')} · "
            f"HP {st.get('hp', '?')}/{st.get('hp_max', '?')} · "
            f"area={st.get('area_id', '?')}"
        )


class AccountManager:
    """
    Registry + factory for per-Telegram-user game accounts.

    TELEGRAM_ADMIN_IDS / allowed users each get an isolated slot. Cookie paste
    and commands always route to that user's own DwarBot — never another account.
    """

    def __init__(
        self,
        *,
        allowed_user_ids: list[str],
        default_world: str = "w1",
        default_world_url: str = "",
        accounts_dir: Optional[Path] = None,
    ) -> None:
        global ACCOUNTS_DIR, REGISTRY_FILE
        if accounts_dir is not None:
            ACCOUNTS_DIR = Path(accounts_dir)
            REGISTRY_FILE = ACCOUNTS_DIR / "registry.json"
        self.allowed = {str(u).strip() for u in allowed_user_ids if str(u).strip()}
        self.default_world = default_world or "w1"
        self.default_world_url = (
            default_world_url or f"https://{self.default_world}.dwar.ru"
        )
        self._runtimes: dict[str, AccountRuntime] = {}
        self._lock = asyncio.Lock()
        self._bot_cls: Optional[type] = None
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Registry persistence
    # ------------------------------------------------------------------

    def _load_registry(self) -> dict[str, str]:
        if not REGISTRY_FILE.exists():
            return {}
        try:
            data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            users = data.get("users") if isinstance(data, dict) else {}
            return {str(k): str(v) for k, v in (users or {}).items()}
        except Exception as exc:
            logger.warning("registry load failed: %s", exc)
            return {}

    def _save_registry(self, users: dict[str, str]) -> None:
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        REGISTRY_FILE.write_text(
            json.dumps({"users": users, "updated_at": time.time()}, indent=2),
            encoding="utf-8",
        )

    def is_allowed(self, user_id: str) -> bool:
        return str(user_id) in self.allowed

    def list_user_ids(self) -> list[str]:
        return sorted(self.allowed)

    # ------------------------------------------------------------------
    def ensure_spec(self, user_id: str) -> AccountSpec:
        uid = str(user_id).strip()
        if not self.is_allowed(uid):
            raise PermissionError(f"user {uid} not in TELEGRAM_ADMIN_IDS")
        reg = self._load_registry()
        slot = reg.get(uid) or slot_id_for_user(uid)
        root = ACCOUNTS_DIR / slot
        spec = AccountSpec.load_meta(root)
        if spec is None:
            spec = AccountSpec(
                telegram_user_id=uid,
                slot_id=slot,
                notify_chat_id=uid,
                world=self.default_world,
                world_url=self.default_world_url,
            )
            spec.ensure_dirs()
            spec.save_meta()
        else:
            spec.ensure_dirs()
        reg[uid] = spec.slot_id
        self._save_registry(reg)
        return spec

    def get_runtime(self, user_id: str) -> Optional[AccountRuntime]:
        return self._runtimes.get(str(user_id))

    def require_runtime(self, user_id: str) -> AccountRuntime:
        rt = self.get_runtime(user_id)
        if rt is None:
            raise KeyError(f"no runtime for user {user_id}")
        return rt

    def all_runtimes(self) -> list[AccountRuntime]:
        return list(self._runtimes.values())

    # ------------------------------------------------------------------
    def _build_client(self, spec: AccountSpec) -> DwarGameClient:
        cookies: dict[str, str] = {}
        token = ""
        mycom = ""
        if spec.cookie_path.exists():
            cookies = load_cookie_dict(spec.cookie_path)
            mycom = cookies.get("mycom", "")
            token = extract_access_token(mycom) if mycom else ""
        return DwarGameClient(
            world_url=spec.world_url or self.default_world_url,
            access_token=token,
            mycom_cookie_value=mycom,
            cookie_file=spec.cookie_path,
            initial_cookies=cookies or None,
        )

    def create_runtime(self, user_id: str, dwar_bot_cls: type | None = None) -> AccountRuntime:
        """Create (or return existing) runtime. Does not start the game loop."""
        uid = str(user_id)
        if uid in self._runtimes:
            return self._runtimes[uid]
        cls = dwar_bot_cls or self._bot_cls
        if cls is None:
            raise RuntimeError("DwarBot class not registered on AccountManager")
        spec = self.ensure_spec(uid)
        settings = BotSettings.load(spec.state_path)
        client = self._build_client(spec)
        bot = cls(
            client,
            settings=settings,
            account_id=spec.slot_id,
            owner_user_id=uid,
        )
        waiting = not (
            client._session.get("sess_sid")
            or client._access_token
            or client._session.get("mycom")
        )
        rt = AccountRuntime(
            spec=spec,
            client=client,
            bot=bot,
            settings=settings,
            waiting_for_cookies=waiting,
        )
        self._runtimes[uid] = rt
        logger.info(
            "Account ready user=%s slot=%s cookies=%s waiting=%s",
            uid, spec.slot_id, spec.cookie_path.exists(), waiting,
        )
        return rt

    def bootstrap_all(self, dwar_bot_cls: type) -> list[AccountRuntime]:
        self._bot_cls = dwar_bot_cls
        out: list[AccountRuntime] = []
        for uid in sorted(self.allowed):
            try:
                out.append(self.create_runtime(uid, dwar_bot_cls))
            except Exception as exc:
                logger.error("Failed to bootstrap user %s: %s", uid, exc)
        return out

    async def start_runtime(self, rt: AccountRuntime) -> None:
        """Start (or restart) the game loop task for one account."""
        if rt.task and not rt.task.done():
            return
        if rt.waiting_for_cookies and not rt.spec.cookie_path.exists():
            logger.info(
                "User %s: no cookies yet — loop idle until paste.",
                rt.user_id,
            )
            rt.task = asyncio.create_task(
                self._wait_cookies_then_run(rt),
                name=f"account-{rt.spec.slot_id}",
            )
            return
        rt.task = asyncio.create_task(
            self._run_account(rt),
            name=f"account-{rt.spec.slot_id}",
        )

    async def start_all(self) -> None:
        for rt in self.all_runtimes():
            await self.start_runtime(rt)

    async def _wait_cookies_then_run(self, rt: AccountRuntime) -> None:
        while not rt.spec.cookie_path.exists():
            await asyncio.sleep(5)
            if not rt.waiting_for_cookies and rt.spec.cookie_path.exists():
                break
        # Cookie paste sets waiting_for_cookies=False and may start run directly
        if rt.task and rt.task.get_name().endswith("-running"):
            return
        await self._run_account(rt)

    async def _run_account(self, rt: AccountRuntime) -> None:
        bot = rt.bot
        client = rt.client
        try:
            # Soft connect — finish fight if locked, never wipe other users
            try:
                await client.ensure_session()
                state = await client.get_state()
                char = await client.get_char_stats()
                if not char.nick and getattr(state, "fight_id", 0):
                    from dwar_bot.modules.stats_parser import is_fight_lock_html
                    html = ""
                    try:
                        html = (await client._get("/user.php")).text
                    except Exception:
                        html = ""
                    if state.fight_id or is_fight_lock_html(html):
                        logger.warning(
                            "[%s] startup fight_id=%s — finishing…",
                            rt.spec.slot_id, state.fight_id,
                        )
                        await bot.combat.finish_fight(timeout=180.0)
                        char = await client.get_char_stats()
                if char.nick:
                    logger.info(
                        "[%s] Connected %s Lv%s area=%s",
                        rt.spec.slot_id, char.nick, char.level,
                        (await client.get_state()).area_id,
                    )
                    rt.waiting_for_cookies = False
                    bot._char = char
                else:
                    soft = await client.soft_recheck_session()
                    if not soft:
                        rt.waiting_for_cookies = True
                        logger.warning(
                            "[%s] session dead — waiting for own cookies",
                            rt.spec.slot_id,
                        )
            except TokenExpiredError:
                rt.waiting_for_cookies = True
                logger.warning("[%s] token expired — waiting for cookies", rt.spec.slot_id)
                await bot.notify(
                    "⚠️ Нужны ваши куки (Cookie Editor JSON). "
                    "Чужой аккаунт бот использовать не будет.",
                    "token",
                )

            bot.timers.start_background_tasks()
            try:
                await bot.timers.sync_server_time()
            except Exception:
                pass
            await bot.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[%s] account loop crashed: %s", rt.spec.slot_id, exc)
        finally:
            try:
                await bot.timers.stop_background_tasks()
            except Exception:
                pass

    async def apply_cookies_for_user(self, user_id: str, raw_json: str) -> str:
        """Route Cookie Editor JSON to THIS user's account only."""
        rt = self.require_runtime(user_id)
        result = await rt.bot.apply_cookie_json(raw_json)
        if result.startswith("✅"):
            rt.waiting_for_cookies = False
            # Wake loop if it was idle
            if rt.task is None or rt.task.done():
                await self.start_runtime(rt)
        return result

    def migrate_legacy(
        self,
        owner_user_id: str,
        legacy_cookie: Path,
        legacy_state: Optional[Path] = None,
    ) -> None:
        """Move old single-account cookies into the owner's slot (once)."""
        if not owner_user_id or not legacy_cookie.exists():
            return
        spec = self.ensure_spec(owner_user_id)
        if spec.cookie_path.exists():
            return
        import shutil
        spec.ensure_dirs()
        shutil.copy2(legacy_cookie, spec.cookie_path)
        logger.info(
            "Migrated legacy cookies → %s (user %s)",
            spec.cookie_path, owner_user_id,
        )
        if legacy_state and legacy_state.exists() and not spec.state_path.exists():
            shutil.copy2(legacy_state, spec.state_path)
