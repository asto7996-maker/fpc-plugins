#!/usr/bin/env python3
"""Load every FunPay Cardinal plugin from this repo through the real host framework.

This mirrors ``Cardinal.load_plugin`` / ``add_handlers_from_plugin`` from
sidor0912/FunPayCardinal: each plugin module is imported by file path (executing
all of its module-level code and imports), the mandatory metadata fields are
verified, and the ``BIND_TO_*`` event handler lists are collected. It is the
same code path the bot uses at startup, so a green run proves the plugins are
loadable into a live Cardinal instance without needing FunPay credentials.

Usage (run from the FunPay Cardinal host directory, with its venv active):

    python /path/to/repo/.cursor/validate_plugins.py

The host directory defaults to ``$FPC_HOME`` or the current working directory.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback

# Mandatory metadata fields every FunPay Cardinal plugin must expose.
REQUIRED_FIELDS = [
    "NAME", "VERSION", "DESCRIPTION", "CREDITS",
    "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE",
]

# Event handler list variables collected by the host (BIND_TO_DELETE is a single
# callback field, handled separately, not an event handler list).
HANDLER_LIST_VARS = [
    "BIND_TO_PRE_INIT", "BIND_TO_POST_INIT",
    "BIND_TO_PRE_START", "BIND_TO_POST_START",
    "BIND_TO_PRE_STOP", "BIND_TO_POST_STOP",
    "BIND_TO_NEW_MESSAGE", "BIND_TO_INIT_MESSAGE",
    "BIND_TO_NEW_ORDER", "BIND_TO_INIT_ORDER",
    "BIND_TO_ORDER_STATUS_CHANGED",
    "BIND_TO_PRE_DELIVERY", "BIND_TO_POST_DELIVERY",
    "BIND_TO_PRE_LOTS_RAISE", "BIND_TO_POST_LOTS_RAISE",
    "BIND_TO_LAST_CHAT_MESSAGE_CHANGED",
    "BIND_TO_MESSAGES_LIST_CHANGED", "BIND_TO_ORDERS_LIST_CHANGED",
]

# The example/template file follows the legacy ``class Plugin`` style and is not
# a real Cardinal plugin, so its metadata fields are not required.
EXEMPT_FILES = {"exampleplugins.py"}


def load_plugin(host_dir: str, file: str):
    """Import a plugin by file path, exactly like Cardinal.load_plugin."""
    path = os.path.join(host_dir, "plugins", file)
    mod_name = f"plugins.{file[:-3]}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    host_dir = os.environ.get("FPC_HOME", os.getcwd())
    plugins_dir = os.path.join(host_dir, "plugins")

    # Make the host packages (cardinal, tg_bot, FunPayAPI, Utils) importable.
    sys.path.insert(0, host_dir)
    sys.path.append(plugins_dir)

    if not os.path.isdir(plugins_dir):
        print(f"[ERROR] plugins directory not found: {plugins_dir}")
        return 2

    files = sorted(f for f in os.listdir(plugins_dir) if f.endswith(".py"))
    if not files:
        print(f"[ERROR] no plugin .py files found in {plugins_dir}")
        return 2

    print(f"FunPay Cardinal host : {host_dir}")
    print(f"Plugins directory    : {plugins_dir}")
    print(f"Python               : {sys.version.split()[0]}")
    print(f"Found {len(files)} plugin file(s)\n")

    failures = 0
    for file in files:
        try:
            module = load_plugin(host_dir, file)
        except Exception:
            print(f"[FAIL] {file}: exception while importing")
            traceback.print_exc()
            failures += 1
            continue

        if file not in EXEMPT_FILES:
            missing = [f for f in REQUIRED_FIELDS if not hasattr(module, f)]
            if missing:
                print(f"[FAIL] {file}: missing required field(s): {missing}")
                failures += 1
                continue

        handlers = {}
        for name in HANDLER_LIST_VARS:
            value = getattr(module, name, None)
            if value:
                try:
                    handlers[name] = len(value)
                except TypeError:
                    handlers[name] = 1

        name = getattr(module, "NAME", getattr(module, "__name__", file))
        version = getattr(module, "VERSION", "-")
        note = " (template, not a Cardinal plugin)" if file in EXEMPT_FILES else ""
        print(f"[OK]   {file}: {name!r} v{version}{note}")
        if handlers:
            print(f"        handlers: {handlers}")

    print()
    total = len(files)
    ok = total - failures
    print(f"Result: {ok}/{total} plugin file(s) loaded successfully")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
