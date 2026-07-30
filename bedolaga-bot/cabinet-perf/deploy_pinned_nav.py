#!/usr/bin/env python3
"""Deploy pinned-nav + usePlatform fallback. Auth: Transport.connect() then auth_password."""
import hashlib, re, time
from pathlib import Path
import paramiko

HOST = "132.243.224.57"
PASSWORD = __import__("os").environ["CABINET_SSH_PASSWORD"]
ROOTS = ("/srv/cabinet", "/root/cabinet-dist")
SRC = Path("/workspace/cabinet-src")

def get_transport():
    for i in range(3):
        try:
            t = paramiko.Transport((HOST, 22))
            t.banner_timeout = 60
            t.connect()
            t.auth_password("root", PASSWORD)
            if t.is_authenticated():
                return t
        except Exception as e:
            print("auth fail", i + 1, e)
            time.sleep(30)
    raise RuntimeError("ssh auth failed")

def main():
    t = get_transport()
    sftp = paramiko.SFTPClient.from_transport(t)

    def run(cmd, timeout=180):
        chan = t.open_session()
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        out = b""
        err = b""
        while True:
            if chan.recv_ready():
                out += chan.recv(65536)
            if chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            time.sleep(0.05)
        print(out.decode()[-2000:])
        if err:
            print("ERR", err.decode()[-400:])
        return chan.recv_exit_status()

    def write_both(rel, data: bytes):
        for root in ROOTS:
            with sftp.file(f"{root}/{rel}", "wb") as f:
                f.write(data)
        print("wrote", rel, len(data))

    css = (SRC / "index-C_ixFJ_6.css").read_bytes()
    h_css = hashlib.sha256(css).hexdigest()[:8]
    for name in [f"assets/index-{h_css}.css", "assets/index-C_ixFJ_6.css", "assets/index-887eaf05.css"]:
        write_both(name, css)

    index = (SRC / "index-a2bed1e6.js").read_text(encoding="utf-8")
    if "platform-fallback-v1" not in index:
        index += "\n/* platform-fallback-v1 */\n"
    index_b = index.encode()
    h_index = hashlib.sha256(index_b).hexdigest()[:8]
    write_both(f"assets/index-{h_index}.js", index_b)
    write_both("assets/index-a2bed1e6.js", index_b)

    html = sftp.file("/srv/cabinet/index.html").read().decode()
    html2 = re.sub(r'src="/assets/index-[A-Za-z0-9_-]+\.js"', f'src="/assets/index-{h_index}.js"', html, count=1)
    html2 = re.sub(r'href="/assets/index-[A-Za-z0-9_-]+\.css"', f'href="/assets/index-{h_css}.css"', html2, count=1)
    for root in ROOTS:
        with sftp.file(f"{root}/index.html", "w") as f:
            f.write(html2)

    remote = f'''import os, re, shutil
base = "/srv/cabinet/assets"
h_index = "{h_index}"
changed = []
for name in os.listdir(base):
    if not name.endswith(".js") or name.startswith("index-") or name.startswith("vendor-"):
        continue
    path = os.path.join(base, name)
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        continue
    text2 = re.sub(r'(from\\"./index-)[A-Za-z0-9_-]+(\\.js\\")', r"\\1" + h_index + r"\\2", text)
    if text2 != text:
        open(path, "w", encoding="utf-8").write(text2)
        changed.append(name)
print("patched", len(changed))
print(",".join(changed[:80]))
for name in changed:
    shutil.copy2("/srv/cabinet/assets/" + name, "/root/cabinet-dist/assets/" + name)
shutil.copy2("/srv/cabinet/index.html", "/root/cabinet-dist/index.html")
print("sync ok")
'''
    with sftp.file("/tmp/patch_cabinet_imports.py", "w") as f:
        f.write(remote)
    run("python3 /tmp/patch_cabinet_imports.py")
    run(
        "python3 -c \""
        f"from pathlib import Path;"
        f"js=Path('/srv/cabinet/assets/index-{h_index}.js').read_text();"
        "assert 'return e||Yc()' in js;"
        f"css=Path('/srv/cabinet/assets/index-{h_css}.css').read_text();"
        "assert 'pinned-nav-fix-v1' in css;"
        "assert '#root{{transform:translateZ(0)}}' not in css;"
        "html=Path('/srv/cabinet/index.html').read_text();"
        f"assert 'index-{h_index}.js' in html;"
        "sup=Path('/srv/cabinet/assets/Support-BfiyGb6A.js').read_text();"
        f"assert 'index-{h_index}.js' in sup;"
        "print('VERIFY OK')"
        "\""
    )
    print("DEPLOY COMPLETE", h_index, h_css)
    sftp.close()
    t.close()

if __name__ == "__main__":
    main()
