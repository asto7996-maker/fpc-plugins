#!/usr/bin/env python3
"""Deploy VexBoost AutoSMM to a remote FunPay Cardinal host and restart.

Usage:
  SERVER_SSH_HOST=... SERVER_SSH_USER=root SERVER_SSH_PASSWORD=... \\
    python3 scripts/deploy_vexboost_catchup.py

Optional: SERVER_SSH_PORT (default 22), FPC_DIR (auto-detect if empty)
"""
from __future__ import annotations

import os
import sys
import time

try:
    import paramiko
except ImportError:
    print("pip install paramiko", file=sys.stderr)
    sys.exit(1)

HOST = os.environ.get("SERVER_SSH_HOST", "").strip()
USER = os.environ.get("SERVER_SSH_USER", "root").strip()
PASSWORD = os.environ.get("SERVER_SSH_PASSWORD", "")
PORT = int(os.environ.get("SERVER_SSH_PORT", "22") or 22)
LOCAL_PLUGIN = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "plugins", "vexboost_autosmm.py")
)
FPC_DIR_HINT = os.environ.get("FPC_DIR", "").strip()
PLUGIN_UUID = "a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e"


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    print(f"$ {cmd[:140]}{'…' if len(cmd) > 140 else ''}")
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print("STDERR:", err[:1500])
    return code, out, err


def detect_fpc(client: paramiko.SSHClient) -> str | None:
    """Prefer live FunPayCardinal over Starvell copies."""
    preferred = [
        "/home/fpc/FunPayCardinal",
        "/opt/FunPayCardinal",
        "/root/FunPayCardinal",
    ]
    for path in preferred:
        code, out, _ = run(client, f"test -f '{path}/plugins/vexboost_autosmm.py' && echo OK")
        if "OK" in out:
            return path

    code, out, _ = run(
        client,
        "find /home /opt /root /var /srv -maxdepth 5 -type f -name vexboost_autosmm.py 2>/dev/null | head -20",
    )
    ranked: list[tuple[int, str]] = []
    for path in (line.strip() for line in out.splitlines() if line.strip()):
        if not path.endswith("/plugins/vexboost_autosmm.py"):
            continue
        root = path[: -len("/plugins/vexboost_autosmm.py")]
        # FunPay Cardinal first, Starvell last
        score = 0
        low = root.lower()
        if "funpay" in low:
            score += 10
        if "starvell" in low:
            score -= 5
        if "/home/fpc/" in root:
            score += 20
        ranked.append((score, root))
    ranked.sort(reverse=True)
    if ranked:
        return ranked[0][1]
    return None


def main() -> int:
    if not HOST or not PASSWORD:
        print("Set SERVER_SSH_HOST and SERVER_SSH_PASSWORD", file=sys.stderr)
        return 2
    if not os.path.isfile(LOCAL_PLUGIN):
        print(f"Plugin not found: {LOCAL_PLUGIN}", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting {USER}@{HOST}:{PORT} …")
    client.connect(
        HOST, port=PORT, username=USER, password=PASSWORD,
        timeout=30, allow_agent=False, look_for_keys=False,
    )

    fpc = FPC_DIR_HINT or detect_fpc(client)
    if not fpc:
        print("Could not auto-detect FunPay Cardinal. Set FPC_DIR=", file=sys.stderr)
        client.close()
        return 3

    plugins = f"{fpc}/plugins"
    remote_plugin = f"{plugins}/vexboost_autosmm.py"
    storage = f"{fpc}/storage/plugins/{PLUGIN_UUID}"
    print(f"FunPay Cardinal: {fpc}")

    code, out, err = run(client, f"test -d '{plugins}' && echo OK")
    if "OK" not in out:
        print(f"plugins dir missing: {plugins}", file=sys.stderr)
        client.close()
        return 3

    ts = time.strftime("%Y%m%d_%H%M%S")
    run(client, f"cp -a '{remote_plugin}' '{remote_plugin}.bak.{ts}' 2>/dev/null || true")

    sftp = client.open_sftp()
    sftp.put(LOCAL_PLUGIN, remote_plugin)
    clear_py = (
        "import json,os\n"
        f"p={storage!r}+'/payorders.json'\n"
        "if os.path.isfile(p):\n"
        "  data=json.load(open(p,encoding='utf-8'))\n"
        "  if isinstance(data,list):\n"
        "    for o in data:\n"
        "      o.pop('catchup_sent', None)\n"
        "    json.dump(data, open(p,'w',encoding='utf-8'), ensure_ascii=False, indent=4)\n"
        "    print('payorders catchup flags cleared', len(data))\n"
        "else:\n"
        "  print('no payorders yet')\n"
    )
    remote_clear = f"/tmp/vb_clear_catchup_{ts}.py"
    with sftp.file(remote_clear, "w") as fh:
        fh.write(clear_py)
    sftp.close()

    run(client, f"rm -rf '{plugins}/__pycache__'; find '{plugins}' -name 'vexboost_autosmm*.pyc' -delete")
    run(client, f"grep -E '^(VERSION|NAME) ' '{remote_plugin}' | head -5")
    run(
        client,
        f"mkdir -p '{storage}'; rm -f '{storage}/catchup_v2412.done' '{storage}/catchup_v2413.done'; "
        f"python3 '{remote_clear}'; rm -f '{remote_clear}'",
    )

    restarted = False
    for cmd in (
        "systemctl restart 'FunPayCardinal@fpc'",
        "systemctl restart funpay-cardinal",
        "systemctl restart cardinal",
        "systemctl restart fpc",
        "supervisorctl restart cardinal",
        "supervisorctl restart funpay",
    ):
        code, out, err = run(client, f"{cmd} 2>/dev/null")
        if code == 0:
            print("Restart via:", cmd)
            restarted = True
            break

    if not restarted:
        run(client, "ps aux | grep -iE '[Pp]ython.*(main.py|cardinal)' | grep -v grep | head -15")
        print("Авто-рестарт не найден. В Telegram Cardinal выполните: /restart")
        print("Затем при необходимости: /vb_catchup")
    else:
        print("Catchup покупателей стартует автоматически после загрузки плагина (~8с).")
        print("Ручной повтор: /vb_catchup")

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
