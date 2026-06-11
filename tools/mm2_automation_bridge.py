#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple


VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mm2_bridge_config.json")


DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8765,
    "api_key": "",
    "log_file": "mm2_bridge.log",
    "join": {
        "open_mode": "web",
        "wait_after_open_seconds": 35,
        "focus_roblox_window": True,
        "roblox_window_title_keywords": ["Roblox"],
    },
    "trade": {
        "enabled": False,
        "timeout_seconds": 180,
        "step_delay_seconds": 0.35,
        "macro": [
            {
                "action": "comment",
                "text": "Настройте macro под ваш интерфейс MM2. Без этого bridge сможет открыть сервер, но не сможет гарантированно кинуть трейд."
            }
        ],
        "success_images": [],
        "privacy_error_images": [],
        "declined_images": [],
        "item_missing_images": []
    }
}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def deep_merge(defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        save_json(path, DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(path, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    if not isinstance(loaded, dict):
        raise ValueError("Config root must be a JSON object")
    merged = deep_merge(DEFAULT_CONFIG, loaded)
    if merged != loaded:
        save_json(path, merged)
    return merged


def save_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


class Logger:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.path = str(config.get("log_file") or "mm2_bridge.log")
        self.lock = threading.RLock()

    def log(self, level: str, message: str) -> None:
        line = f"{now()} [{level}] {message}"
        with self.lock:
            print(line, flush=True)
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass


class OptionalPyAutoGUI:
    def __init__(self) -> None:
        self.module = None
        self.error = ""
        try:
            import pyautogui  # type: ignore

            self.module = pyautogui
            self.module.FAILSAFE = True
        except Exception as exc:
            self.error = str(exc)

    @property
    def available(self) -> bool:
        return self.module is not None


class BridgeState:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.logger = Logger(self.config)
        self.pyauto = OptionalPyAutoGUI()
        self.lock = threading.RLock()

    def reload(self) -> None:
        with self.lock:
            self.config = load_config(self.config_path)
            self.logger = Logger(self.config)
            self.pyauto = OptionalPyAutoGUI()


STATE: Optional[BridgeState] = None


def response(status: str, ok: bool, message: str = "", **extra: Any) -> Dict[str, Any]:
    payload = {"ok": ok, "status": status, "message": message}
    payload.update(extra)
    return payload


def build_join_url(server: Dict[str, Any]) -> str:
    join_url = str(server.get("join_url") or "").strip()
    if join_url:
        return join_url
    place_id = str(server.get("place_id") or "").strip()
    game_id = str(server.get("game_id") or "").strip()
    if place_id and game_id:
        params = urllib.parse.urlencode({"placeId": place_id, "gameInstanceId": game_id})
        return f"https://www.roblox.com/games/start?{params}"
    vip = str(server.get("vip_server_url") or "").strip()
    if vip:
        return vip
    return ""


def roblox_protocol_url(server: Dict[str, Any]) -> str:
    place_id = str(server.get("place_id") or "").strip()
    game_id = str(server.get("game_id") or "").strip()
    if place_id and game_id:
        return f"roblox://experiences/start?placeId={urllib.parse.quote(place_id)}&gameInstanceId={urllib.parse.quote(game_id)}"
    if place_id:
        return f"roblox://experiences/start?placeId={urllib.parse.quote(place_id)}"
    return build_join_url(server)


def open_join_url(payload: Dict[str, Any], state: BridgeState) -> Dict[str, Any]:
    join_cfg = dict(state.config.get("join") or {})
    server = dict(payload.get("server") or {})
    mode = str(join_cfg.get("open_mode") or "web").lower()
    url = roblox_protocol_url(server) if mode == "protocol" else build_join_url(server)
    if not url:
        return response("JOIN_FAILED", False, "Нет join_url/place_id/game_id/vip_server_url в payload.")

    state.logger.log("INFO", f"Opening Roblox URL mode={mode}: {url}")
    try:
        webbrowser.open(url, new=0, autoraise=True)
    except Exception as exc:
        return response("JOIN_FAILED", False, f"Не удалось открыть Roblox URL: {exc}")

    wait_seconds = max(0, int(join_cfg.get("wait_after_open_seconds") or 0))
    if wait_seconds:
        time.sleep(wait_seconds)

    if bool(join_cfg.get("focus_roblox_window", True)):
        focus_roblox_window(state)

    return response("SUCCESS", True, "Join command sent to Roblox client.", join_url=url)


def focus_roblox_window(state: BridgeState) -> bool:
    keywords = [str(x).lower() for x in (state.config.get("join") or {}).get("roblox_window_title_keywords", ["Roblox"])]
    system = platform.system().lower()
    try:
        if system == "windows":
            script = (
                "$ws = New-Object -ComObject WScript.Shell; "
                "$p = Get-Process | Where-Object { $_.MainWindowTitle -match 'Roblox' } | Select-Object -First 1; "
                "if ($p) { $ws.AppActivate($p.Id) | Out-Null; exit 0 } else { exit 1 }"
            )
            completed = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=5)
            if completed.returncode == 0:
                return True
        elif system == "darwin":
            completed = subprocess.run(["osascript", "-e", 'tell application "Roblox" to activate'], capture_output=True, timeout=5)
            return completed.returncode == 0
        else:
            # Linux desktop users may have wmctrl installed. Most Roblox bot setups are Windows.
            completed = subprocess.run(["wmctrl", "-a", "Roblox"], capture_output=True, timeout=5)
            return completed.returncode == 0
    except Exception as exc:
        state.logger.log("WARN", f"Cannot focus Roblox window: {exc}")
    state.logger.log("WARN", f"Roblox window was not focused; expected title keywords={keywords}")
    return False


def substitute(value: Any, payload: Dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    buyer = dict(payload.get("buyer") or {})
    item = dict(payload.get("item") or {})
    server = dict(payload.get("server") or {})
    replacements = {
        "{roblox_username}": str(buyer.get("roblox_username") or ""),
        "{roblox_user_id}": str(buyer.get("roblox_user_id") or ""),
        "{item_name}": str(item.get("name") or ""),
        "{lot_id}": str(item.get("lot_id") or ""),
        "{place_id}": str(server.get("place_id") or ""),
        "{game_id}": str(server.get("game_id") or ""),
    }
    for needle, repl in replacements.items():
        value = value.replace(needle, repl)
    return value


def run_trade_macro(payload: Dict[str, Any], state: BridgeState) -> Dict[str, Any]:
    trade_cfg = dict(state.config.get("trade") or {})
    if not bool(trade_cfg.get("enabled", False)):
        return response(
            "BRIDGE_NOT_CONFIGURED",
            False,
            "trade.enabled=false. Bridge открыл сервер, но макрос трейда не настроен.",
            hint="Настройте trade.macro в mm2_bridge_config.json под ваш интерфейс MM2."
        )
    if not state.pyauto.available:
        return response(
            "BRIDGE_NOT_CONFIGURED",
            False,
            f"pyautogui недоступен: {state.pyauto.error}",
            hint="Установите pyautogui: py -m pip install pyautogui pillow"
        )

    pyautogui = state.pyauto.module
    assert pyautogui is not None
    focus_roblox_window(state)

    macro = list(trade_cfg.get("macro") or [])
    if not macro:
        return response("BRIDGE_NOT_CONFIGURED", False, "trade.macro пустой.")

    step_delay = float(trade_cfg.get("step_delay_seconds") or 0.35)
    state.logger.log("INFO", f"Running trade macro steps={len(macro)} buyer={payload.get('buyer')} item={payload.get('item')}")
    try:
        for idx, raw_step in enumerate(macro, start=1):
            if not isinstance(raw_step, dict):
                continue
            action = str(raw_step.get("action") or "").lower()
            if action == "comment":
                continue
            execute_macro_step(pyautogui, action, raw_step, payload, state, idx)
            if step_delay:
                time.sleep(step_delay)
    except Exception as exc:
        state.logger.log("ERROR", f"Trade macro failed: {exc}\n{traceback.format_exc()}")
        return response("UNKNOWN_ERROR", False, f"Ошибка macro step: {exc}")

    detected = detect_trade_result(pyautogui, trade_cfg, state)
    if detected:
        return detected
    return response("SUCCESS", True, "Trade macro completed. Если предмет не передался, настройте success_images/error_images для проверки результата.")


def execute_macro_step(pyautogui: Any, action: str, step: Dict[str, Any], payload: Dict[str, Any], state: BridgeState, idx: int) -> None:
    if action == "sleep":
        time.sleep(float(step.get("seconds") or step.get("value") or 1))
    elif action == "press":
        pyautogui.press(str(substitute(step.get("key") or step.get("value") or "", payload)))
    elif action == "hotkey":
        keys = step.get("keys") or []
        if isinstance(keys, str):
            keys = [part.strip() for part in keys.split("+") if part.strip()]
        pyautogui.hotkey(*[str(substitute(key, payload)) for key in keys])
    elif action == "typewrite":
        pyautogui.write(str(substitute(step.get("text") or "", payload)), interval=float(step.get("interval") or 0.02))
    elif action == "paste":
        paste_text(str(substitute(step.get("text") or "", payload)), pyautogui)
    elif action == "click":
        pyautogui.click(int(step.get("x")), int(step.get("y")), clicks=int(step.get("clicks") or 1))
    elif action == "double_click":
        pyautogui.doubleClick(int(step.get("x")), int(step.get("y")))
    elif action == "move":
        pyautogui.moveTo(int(step.get("x")), int(step.get("y")), duration=float(step.get("duration") or 0.1))
    elif action == "image_click":
        image_click(pyautogui, str(step.get("image") or ""), float(step.get("confidence") or 0.85), int(step.get("timeout") or 10))
    else:
        raise ValueError(f"Unknown macro action at step {idx}: {action}")


def paste_text(text: str, pyautogui: Any) -> None:
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
    except Exception:
        pyautogui.write(text, interval=0.02)


def image_click(pyautogui: Any, image: str, confidence: float, timeout: int) -> None:
    if not image or not os.path.exists(image):
        raise FileNotFoundError(f"Image not found: {image}")
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            location = pyautogui.locateCenterOnScreen(image, confidence=confidence)
            if location:
                pyautogui.click(location.x, location.y)
                return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.3)
    raise TimeoutError(f"Image not found on screen: {image}. {last_error}")


def detect_trade_result(pyautogui: Any, trade_cfg: Dict[str, Any], state: BridgeState) -> Optional[Dict[str, Any]]:
    checks = [
        ("success_images", "SUCCESS", True),
        ("privacy_error_images", "TRADE_PRIVACY_DISABLED", False),
        ("declined_images", "TRADE_DECLINED", False),
        ("item_missing_images", "ITEM_NOT_FOUND", False),
    ]
    for key, status, ok in checks:
        for image in trade_cfg.get(key) or []:
            if not image:
                continue
            try:
                location = pyautogui.locateCenterOnScreen(str(image), confidence=0.85)
                if location:
                    return response(status, ok, f"Detected {key}: {image}")
            except Exception as exc:
                state.logger.log("WARN", f"Image detection failed {image}: {exc}")
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = f"MM2AutomationBridge/{VERSION}"

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        if STATE:
            STATE.logger.log("HTTP", fmt % args)

    def route(self, method: str) -> None:
        assert STATE is not None
        try:
            if not self.authorized(STATE):
                self.write_json(401, response("AUTH_ERROR", False, "Invalid bridge API key."))
                return
            path = urllib.parse.urlparse(self.path).path
            payload = self.read_json() if method == "POST" else {}
            if method == "GET" and path == "/health":
                self.write_json(200, self.health_payload(STATE))
            elif method == "POST" and path == "/reload":
                STATE.reload()
                self.write_json(200, response("SUCCESS", True, "Config reloaded."))
            elif method == "POST" and path == "/join":
                self.write_json(200, open_join_url(payload, STATE))
            elif method == "POST" and path == "/trade":
                self.write_json(200, run_trade_macro(payload, STATE))
            elif method == "POST" and path == "/deliver":
                join_result = open_join_url(payload, STATE)
                if not join_result.get("ok"):
                    self.write_json(200, join_result)
                    return
                trade_result = run_trade_macro(payload, STATE)
                self.write_json(200, trade_result)
            else:
                self.write_json(404, response("NOT_FOUND", False, f"Unknown endpoint {method} {path}"))
        except Exception as exc:
            if STATE:
                STATE.logger.log("ERROR", traceback.format_exc())
            self.write_json(500, response("UNKNOWN_ERROR", False, str(exc)))

    def authorized(self, state: BridgeState) -> bool:
        api_key = str(state.config.get("api_key") or "")
        if not api_key:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {api_key}" or self.headers.get("X-API-Key", "") == api_key

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def write_json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def health_payload(self, state: BridgeState) -> Dict[str, Any]:
        trade_cfg = dict(state.config.get("trade") or {})
        return response(
            "SUCCESS",
            True,
            "Bridge is running.",
            version=VERSION,
            platform=platform.platform(),
            python=sys.version.split()[0],
            pyautogui_available=state.pyauto.available,
            pyautogui_error=state.pyauto.error,
            trade_enabled=bool(trade_cfg.get("enabled", False)),
            macro_steps=len(trade_cfg.get("macro") or []),
            config_path=state.config_path,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="MM2 Roblox automation bridge for FunPay Cardinal")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to bridge config JSON")
    parser.add_argument("--host", default="", help="Override host")
    parser.add_argument("--port", type=int, default=0, help="Override port")
    args = parser.parse_args()

    global STATE
    STATE = BridgeState(args.config)
    host = args.host or str(STATE.config.get("host") or "0.0.0.0")
    port = args.port or int(STATE.config.get("port") or 8765)
    STATE.logger.log("INFO", f"Starting MM2 bridge v{VERSION} on {host}:{port}")
    STATE.logger.log("INFO", f"Config: {args.config}")
    STATE.logger.log("INFO", f"pyautogui available={STATE.pyauto.available} error={STATE.pyauto.error}")

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STATE.logger.log("INFO", "Stopping bridge.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
