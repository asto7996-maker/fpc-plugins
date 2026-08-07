#!/usr/bin/env python3
"""
Генерирует HTML-страницы документов для мини-приложения из legal/documents.py.

Запуск из папки vk_vpn_bot:
    python3 tools/build_legal_pages.py
Результат: miniapp/legal/*.html
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from legal.documents import ALL_DOCS, LegalDoc

CSS = """
:root{
  --bg:#070b14;
  --card:rgba(255,255,255,.04);
  --line:rgba(255,255,255,.08);
  --text:#e8eef7;
  --muted:#93a0b5;
  --accent:#5b9fff;
  --accent2:#7c5cff;
  --radius:18px;
  --font:'Segoe UI',system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font-family:var(--font);line-height:1.55;-webkit-font-smoothing:antialiased}
body{
  min-height:100vh;
  background:
    radial-gradient(1200px 600px at 10% -10%, rgba(91,159,255,.22), transparent 55%),
    radial-gradient(900px 500px at 100% 0%, rgba(124,92,255,.18), transparent 50%),
    radial-gradient(700px 400px at 50% 100%, rgba(61,214,140,.08), transparent 45%),
    var(--bg);
}
.wrap{max-width:720px;margin:0 auto;padding:28px 18px 64px}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:22px}
.logo{
  width:42px;height:42px;border-radius:14px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:grid;place-items:center;font-size:20px;
  box-shadow:0 10px 30px rgba(91,159,255,.25);
}
.brand h1{margin:0;font-size:1.15rem;letter-spacing:.02em}
.brand p{margin:2px 0 0;color:var(--muted);font-size:.85rem}
.card{
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:22px 20px;backdrop-filter:blur(10px);
  box-shadow:0 20px 50px rgba(0,0,0,.25);
}
.card h2{margin:0 0 8px;font-size:1.35rem}
.meta{color:var(--muted);font-size:.86rem;margin-bottom:18px}
.doc{white-space:pre-wrap;font-size:.98rem;color:#d7e0ee}
.back{
  display:inline-block;text-decoration:none;color:var(--text);border:1px solid var(--line);
  background:rgba(255,255,255,.03);padding:10px 14px;border-radius:999px;
  font-size:.9rem;transition:.2s ease;
}
.back:hover{border-color:rgba(91,159,255,.5);background:rgba(91,159,255,.12)}
.grid{display:grid;gap:12px}
.item{
  display:block;text-decoration:none;color:inherit;
  border:1px solid var(--line);background:rgba(255,255,255,.03);
  border-radius:16px;padding:16px 16px 14px;transition:.2s ease;
}
.item:hover{transform:translateY(-2px);border-color:rgba(91,159,255,.45);background:rgba(91,159,255,.08)}
.item .e{font-size:1.35rem}
.item h3{margin:8px 0 4px;font-size:1.05rem}
.item p{margin:0;color:var(--muted);font-size:.9rem}
.footer{margin-top:28px;color:var(--muted);font-size:.8rem;text-align:center}
"""


def doc_page(doc: LegalDoc) -> str:
    body = html.escape(doc.body.strip())
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#070b14"/>
<title>{doc.emoji} {doc.title} · Paskod</title>
<style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="brand">
      <div class="logo">✦</div>
      <div>
        <h1>Paskod VPN</h1>
        <p>Мини-приложение · документы</p>
      </div>
    </div>
    <a class="back" href="index.html">← Ко всем документам</a>
    <div class="card" style="margin-top:14px">
      <h2>{doc.emoji} {doc.title}</h2>
      <div class="meta">{html.escape(doc.summary)}<br/>Обновлено {doc.updated}</div>
      <div class="doc">{body}</div>
    </div>
    <div class="footer">© Paskod · Пользуясь сервисом, вы принимаете условия документов</div>
  </div>
</body>
</html>"""


def index_page() -> str:
    items = "\n".join(
        f'<a class="item" href="{d.slug}.html">'
        f'<div class="e">{d.emoji}</div>'
        f"<h3>{d.title}</h3><p>{html.escape(d.summary)}</p></a>"
        for d in ALL_DOCS
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#070b14"/>
<title>Документы · Paskod</title>
<style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="brand">
      <div class="logo">✦</div>
      <div>
        <h1>Paskod VPN</h1>
        <p>Пользовательские документы</p>
      </div>
    </div>
    <div class="card">
      <h2>📂 Документы</h2>
      <div class="meta">Пользовательское соглашение, политика конфиденциальности и другие материалы сервиса.</div>
      <div class="grid">{items}</div>
    </div>
    <div class="footer">Также доступно в VK-боте · раздел «ℹ️ Инфо»</div>
  </div>
</body>
</html>"""


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "miniapp" / "legal"
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(index_page(), encoding="utf-8")
    for doc in ALL_DOCS:
        (out / f"{doc.slug}.html").write_text(doc_page(doc), encoding="utf-8")
    print(f"Built {len(ALL_DOCS) + 1} pages in {out}")


if __name__ == "__main__":
    main()
