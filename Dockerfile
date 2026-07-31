# Dwar Bot — Playwright + Chromium (Linux VPS)
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DWAR_COOKIES_FILE=/app/auth/cookies.json \
    DWAR_LOG_FILE=/app/bot.log \
    DWAR_HEADLESS=true \
    DWAR_LOG_CONSOLE=true

WORKDIR /app

# Системные библиотеки для Chromium / Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxshmfence1 \
    libxss1 \
    libxtst6 \
    wget \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости
COPY dwar_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && playwright install --with-deps chromium

# Исходники бота
COPY dwar_bot /app/dwar_bot
COPY main.py /app/main.py

# Тома: куки, лог, капчи (пути снаружи задаются в docker-compose)
RUN mkdir -p /app/auth /app/captchas /app/dwar_bot/data/cookies /app/dwar_bot/data/logs /app/dwar_bot/data/screenshots \
    && rm -rf /app/dwar_bot/data/captchas \
    && ln -s /app/captchas /app/dwar_bot/data/captchas \
    && touch /app/bot.log

# Непривилегированный пользователь (браузеру нужен home для кэша)
RUN useradd --create-home --shell /bin/bash dwar \
    && chown -R dwar:dwar /app /ms-playwright
USER dwar

WORKDIR /app
EXPOSE 0

# Запуск оркестратора
CMD ["python", "main.py"]
