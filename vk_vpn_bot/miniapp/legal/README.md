# Документы мини-приложения Paskod

Статические страницы для кабинета / Telegram Mini App:

| Файл | Документ |
|------|----------|
| `index.html` | Оглавление |
| `privacy.html` | Политика конфиденциальности |
| `terms.html` | Пользовательское соглашение |
| `offer.html` | Публичная оферта |
| `rules.html` | Правила сервиса |
| `faq.html` | Частые вопросы |

## Деплой на cabinet.paskod.ru

Скопируйте папку `legal/` в корень статики кабинета так, чтобы были доступны URL:

```
https://cabinet.paskod.ru/legal/index.html
https://cabinet.paskod.ru/legal/privacy.html
…
```

Если кабинет отдаёт SPA со всех путей, добавьте в Caddy/nginx приоритет для `/legal/*`
как `file_server` / `try_files` на эти HTML (чтобы не перехватывал `index.html` SPA).

Пример Caddy:

```
handle_path /legal/* {
    root * /var/www/cabinet/legal
    file_server
}
```

Тексты синхронизированы с модулем `vk_vpn_bot/legal/documents.py` и разделом «ℹ️ Инфо» в VK-боте.
