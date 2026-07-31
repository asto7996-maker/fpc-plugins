// ==UserScript==
// @name         FunPay Gemini Link Automation
// @namespace    https://funpay.com/gemini-automation
// @version      1.0.0
// @description  Автоматизация продажи Gemini link (18 мес) через API Telegram-бота поставщика
// @author       FunPay Automation
// @match        https://funpay.com/*
// @match        https://*.funpay.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_addStyle
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  // ===========================================================================
  // БЛОК КОНФИГУРАЦИИ (публичные настройки — без секретов)
  // ===========================================================================

  const PUBLIC_CONFIG = {
    /** Интервал опроса новых заказов, мс */
    ORDER_CHECK_INTERVAL_MS: 8000,

    /** Ключевые слова в описании лота для фильтрации */
    PRODUCT_KEYWORDS: ['Gemini link'],

    /** Идентификатор продукта в API бота (18 месяцев) */
    BOT_PRODUCT_ID: 'gemini_18m',

    /** Максимум повторных попыток при сбое API */
    API_RETRY_COUNT: 3,

    /** Интервал между повторами, мс */
    API_RETRY_DELAY_MS: 5000,

    /** Таймаут HTTP-запроса к API бота, мс */
    API_TIMEOUT_MS: 20000,

    /** Относительные пути API бота (к BOT_API_URL) */
    API_PATHS: {
      balance: '/api/v1/balance',
      stock: '/api/v1/stock',
      purchase: '/api/v1/purchase',
    },

    /** Ключи в защищённом хранилище Tampermonkey */
    SECURE_KEYS: {
      botApiUrl: 'fgp_bot_api_url',
      botApiKey: 'fgp_bot_api_key',
    },

    /** Локальные ключи localStorage */
    STORAGE_KEYS: {
      processedOrders: 'fgp_processed_orders',
      preorderQueue: 'fgp_preorder_queue',
      manualAttention: 'fgp_manual_attention',
      disabledChats: 'fgp_disabled_chats',
      recentLogs: 'fgp_recent_logs',
      chatStates: 'fgp_chat_states',
    },

    /** Селекторы FunPay DOM */
    FUNPAY: {
      ordersUrl: '/orders/trade',
      orderLinkPattern: /\/orders\/([A-Z0-9]+)\//i,
      chatMessageInput: 'textarea[name="content"]',
      chatSendButton: 'button[type="submit"]',
    },
  };

  // ===========================================================================
  // ЗАЩИЩЁННАЯ КОНФИГУРАЦИЯ (секреты — только через GM_* / prompt при первом запуске)
  // ===========================================================================

  const SecureConfig = {
    get botApiUrl() {
      return storageGet(PUBLIC_CONFIG.SECURE_KEYS.botApiUrl, '');
    },
    set botApiUrl(value) {
      storageSet(PUBLIC_CONFIG.SECURE_KEYS.botApiUrl, String(value || '').trim());
    },
    get botApiKey() {
      return storageGet(PUBLIC_CONFIG.SECURE_KEYS.botApiKey, '');
    },
    set botApiKey(value) {
      storageSet(PUBLIC_CONFIG.SECURE_KEYS.botApiKey, String(value || '').trim());
    },
    isConfigured() {
      return Boolean(this.botApiUrl && this.botApiKey);
    },
    /** Маскированное значение для UI (ключ не светится) */
    maskedKey() {
      const key = this.botApiKey;
      if (!key) return '— не задан —';
      if (key.length <= 8) return '****';
      return `${key.slice(0, 4)}…${key.slice(-4)}`;
    },
  };

  // ===========================================================================
  // УТИЛИТЫ: хранилище, логирование, время
  // ===========================================================================

  const hasGM = typeof GM_getValue === 'function';

  function storageGet(key, defaultValue = null) {
    try {
      if (hasGM) {
        const val = GM_getValue(key, null);
        return val !== null && val !== undefined ? val : defaultValue;
      }
      const raw = localStorage.getItem(key);
      return raw !== null ? JSON.parse(raw) : defaultValue;
    } catch {
      return defaultValue;
    }
  }

  function storageSet(key, value) {
    try {
      if (hasGM) {
        GM_setValue(key, value);
      } else {
        localStorage.setItem(key, JSON.stringify(value));
      }
    } catch (err) {
      console.error('[FGP] storageSet error:', err);
    }
  }

  function timestamp() {
    return new Date().toISOString().replace('T', ' ').slice(0, 19);
  }

  class Logger {
    constructor(onLog) {
      this.onLog = onLog;
      this.maxEntries = 200;
      this.entries = storageGet(PUBLIC_CONFIG.STORAGE_KEYS.recentLogs, []);
    }

    _persist() {
      storageSet(PUBLIC_CONFIG.STORAGE_KEYS.recentLogs, this.entries.slice(-this.maxEntries));
    }

    log(level, message, meta = {}) {
      const entry = { ts: timestamp(), level, message, meta };
      const prefix = `[FGP ${entry.ts}]`;
      const consoleFn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log;
      consoleFn(`${prefix} [${level.toUpperCase()}] ${message}`, Object.keys(meta).length ? meta : '');
      this.entries.push(entry);
      this._persist();
      if (typeof this.onLog === 'function') {
        this.onLog(entry);
      }
    }

    info(msg, meta) { this.log('info', msg, meta); }
    warn(msg, meta) { this.log('warn', msg, meta); }
    error(msg, meta) { this.log('error', msg, meta); }
    debug(msg, meta) { this.log('debug', msg, meta); }

    getRecent(count = 5) {
      return this.entries.slice(-count);
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function getCsrfToken() {
    const input = document.querySelector('input[name="csrf_token"]');
    if (input && input.value) return input.value;
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    const match = document.cookie.match(/(?:^|;\s*)_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  // ===========================================================================
  // HTTP-слой (Fetch + GM_xmlhttpRequest fallback для CORS)
  // ===========================================================================

  async function httpRequest(url, options = {}) {
    const {
      method = 'GET',
      headers = {},
      body = null,
      timeout = PUBLIC_CONFIG.API_TIMEOUT_MS,
    } = options;

    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = controller
      ? setTimeout(() => controller.abort(), timeout)
      : null;

    try {
      const response = await fetch(url, {
        method,
        headers,
        body,
        credentials: 'include',
        signal: controller ? controller.signal : undefined,
      });
      if (timer) clearTimeout(timer);
      return response;
    } catch (fetchErr) {
      if (timer) clearTimeout(timer);

      if (hasGM && typeof GM_xmlhttpRequest === 'function') {
        return new Promise((resolve, reject) => {
          GM_xmlhttpRequest({
            method,
            url,
            headers,
            data: body,
            timeout,
            onload(res) {
              resolve({
                ok: res.status >= 200 && res.status < 300,
                status: res.status,
                statusText: res.statusText,
                json: async () => JSON.parse(res.responseText),
                text: async () => res.responseText,
              });
            },
            onerror: reject,
            ontimeout: () => reject(new Error('Request timeout')),
          });
        });
      }
      throw fetchErr;
    }
  }

  // ===========================================================================
  // Класс FunPayGeminiPlugin
  // ===========================================================================

  class FunPayGeminiPlugin {
    constructor() {
      this.logger = new Logger((entry) => this.uiPanel?.pushLog(entry));
      this.processedOrders = new Set(storageGet(PUBLIC_CONFIG.STORAGE_KEYS.processedOrders, []));
      this.preorderQueue = storageGet(PUBLIC_CONFIG.STORAGE_KEYS.preorderQueue, []);
      this.manualAttention = storageGet(PUBLIC_CONFIG.STORAGE_KEYS.manualAttention, []);
      this.disabledChats = new Map(
        Object.entries(storageGet(PUBLIC_CONFIG.STORAGE_KEYS.disabledChats, {}))
      );
      this.chatStates = storageGet(PUBLIC_CONFIG.STORAGE_KEYS.chatStates, {});
      this.orderMonitorTimer = null;
      this.messageObserver = null;
      this.running = false;
      this.status = 'offline';
      this.lastBalance = null;
      this.lastStock = null;
      this.uiPanel = null;
      this._fetchHookInstalled = false;
    }

    // -------------------------------------------------------------------------
    // init()
    // -------------------------------------------------------------------------

    async init() {
      this.logger.info('Инициализация плагина FunPay Gemini Link Automation');

      if (!SecureConfig.isConfigured()) {
        await this._promptSecureConfig();
      }

      if (!SecureConfig.isConfigured()) {
        this.logger.warn('API бота не настроен — плагин работает в режиме ожидания конфигурации');
        this.status = 'config_required';
      } else {
        this.logger.info('Конфигурация API загружена', {
          url: SecureConfig.botApiUrl,
          key: SecureConfig.maskedKey(),
        });
        this.status = 'online';
      }

      this._persistProcessedOrders();
      this._installFetchHook();
      this._installMessageInterceptor();
      this.uiPanel = new SellerUIPanel(this);
      this.uiPanel.render();

      if (SecureConfig.isConfigured()) {
        await this.refreshDashboardStats();
      }

      this.startOrderMonitor();
      this.running = true;
      this.logger.info('Плагин запущен', { processedOrders: this.processedOrders.size });
    }

    async _promptSecureConfig() {
      const url = window.prompt(
        '🔐 FunPay Gemini Plugin\n\nВведите BOT_API_URL (URL API Telegram-бота поставщика):',
        SecureConfig.botApiUrl || 'https://your-bot-api.example.com'
      );
      if (url === null) return;
      SecureConfig.botApiUrl = url;

      const key = window.prompt(
        '🔐 FunPay Gemini Plugin\n\nВведите BOT_API_KEY (токен авторизации):\n⚠️ Ключ хранится локально и не попадает в логи.',
        ''
      );
      if (key === null) return;
      SecureConfig.botApiKey = key;
    }

    _persistProcessedOrders() {
      storageSet(PUBLIC_CONFIG.STORAGE_KEYS.processedOrders, [...this.processedOrders]);
    }

    _persistPreorderQueue() {
      storageSet(PUBLIC_CONFIG.STORAGE_KEYS.preorderQueue, this.preorderQueue);
    }

    _persistManualAttention() {
      storageSet(PUBLIC_CONFIG.STORAGE_KEYS.manualAttention, this.manualAttention);
    }

    _persistDisabledChats() {
      storageSet(PUBLIC_CONFIG.STORAGE_KEYS.disabledChats, Object.fromEntries(this.disabledChats));
    }

    // -------------------------------------------------------------------------
    // startOrderMonitor() — ТОЛЬКО новые оплаченные заказы
    // -------------------------------------------------------------------------

    startOrderMonitor() {
      if (this.orderMonitorTimer) {
        clearInterval(this.orderMonitorTimer);
      }

      this.logger.info('Запуск мониторинга новых заказов (Order Event)', {
        intervalMs: PUBLIC_CONFIG.ORDER_CHECK_INTERVAL_MS,
      });

      const tick = async () => {
        try {
          await this._pollNewOrders();
        } catch (err) {
          this.logger.error('Ошибка цикла мониторинга заказов', { error: String(err) });
        }
      };

      tick();
      this.orderMonitorTimer = setInterval(tick, PUBLIC_CONFIG.ORDER_CHECK_INTERVAL_MS);
    }

    _installFetchHook() {
      if (this._fetchHookInstalled) return;
      this._fetchHookInstalled = true;

      const originalFetch = window.fetch.bind(window);
      const self = this;

      window.fetch = async function (...args) {
        const response = await originalFetch(...args);
        try {
          const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
          if (url.includes('orders') || url.includes('runner')) {
            const clone = response.clone();
            clone.text().then((text) => self._inspectNetworkPayload(url, text)).catch(() => {});
          }
        } catch {
          /* ignore hook errors */
        }
        return response;
      };

      this.logger.debug('Fetch-hook для перехвата Order Event установлен');
    }

    _inspectNetworkPayload(url, text) {
      if (!text || text.length > 500000) return;

      const orderIdMatches = text.match(/\/orders\/([A-Z0-9]{6,})\//gi) || [];
      for (const match of orderIdMatches) {
        const idMatch = match.match(PUBLIC_CONFIG.FUNPAY.orderLinkPattern);
        if (idMatch) {
          this._enqueueOrderCheck(idMatch[1], 'network_hook');
        }
      }

      try {
        const json = JSON.parse(text);
        this._extractOrdersFromJson(json);
      } catch {
        /* not JSON */
      }
    }

    _extractOrdersFromJson(data) {
      const stack = [data];
      while (stack.length) {
        const node = stack.pop();
        if (!node || typeof node !== 'object') continue;

        if (node.id && (node.description || node.full_description || node.lot_description)) {
          const desc = node.description || node.full_description || node.lot_description || '';
          if (this._matchesProductKeywords(desc)) {
            this._enqueueOrderCheck(String(node.id), 'json_event');
          }
        }

        if (node.order_id && node.status) {
          this._enqueueOrderCheck(String(node.order_id), 'json_order');
        }

        for (const val of Object.values(node)) {
          if (val && typeof val === 'object') stack.push(val);
        }
      }
    }

    _enqueueOrderCheck(orderId, source) {
      if (!orderId || this.processedOrders.has(orderId)) return;
      this.logger.debug('Обнаружен кандидат заказа', { orderId, source });
      this._handleNewOrder(orderId, source).catch((err) => {
        this.logger.error('Ошибка обработки заказа из hook', { orderId, error: String(err) });
      });
    }

    async _pollNewOrders() {
      const csrf = getCsrfToken();
      const headers = { Accept: 'text/html,application/json' };
      if (csrf) headers['X-Requested-With'] = 'XMLHttpRequest';

      const response = await httpRequest(`https://funpay.com${PUBLIC_CONFIG.FUNPAY.ordersUrl}`, {
        method: 'GET',
        headers,
      });

      if (!response.ok) {
        this.logger.warn('Не удалось получить список заказов', { status: response.status });
        return;
      }

      const html = await response.text();
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');

      const orderLinks = doc.querySelectorAll('a[href*="/orders/"]');
      const candidates = [];

      orderLinks.forEach((link) => {
        const href = link.getAttribute('href') || '';
        const match = href.match(PUBLIC_CONFIG.FUNPAY.orderLinkPattern);
        if (!match) return;

        const orderId = match[1];
        const row = link.closest('.tc-item, tr, .order-item, .offer');
        const rowText = row ? row.textContent || '' : '';
        const isPaid =
          /оплачен/i.test(rowText) ||
          /paid/i.test(rowText) ||
          /в работе/i.test(rowText) ||
          !/ожида/i.test(rowText);

        if (isPaid && !this.processedOrders.has(orderId)) {
          candidates.push(orderId);
        }
      });

      for (const orderId of [...new Set(candidates)]) {
        await this._handleNewOrder(orderId, 'poll');
      }
    }

    async _handleNewOrder(orderId, source) {
      if (this.processedOrders.has(orderId)) return;

      this.logger.info('Обнаружен новый заказ → начало обработки', { orderId, source });

      const description = await this.parseOrderDescription(orderId);
      if (!this._matchesProductKeywords(description)) {
        this.logger.debug('Заказ не соответствует PRODUCT_KEYWORDS — пропуск', { orderId });
        return;
      }

      this.logger.info('Заказ соответствует Gemini link → проверка баланса', { orderId });

      const chatInfo = await this._resolveOrderChat(orderId);
      const chatId = chatInfo?.chatId;

      try {
        const balance = await this.checkBalance();
        this.logger.info('Баланс проверен', { orderId, balance: balance.balance, currency: balance.currency });

        const stock = await this.checkStock();
        this.logger.info('Наличие проверено', { orderId, available: stock.available });

        if (stock.status === 'out_of_stock' || stock.available <= 0) {
          await this._handleOutOfStock(orderId, chatId);
          return;
        }

        if (balance.balance !== null && stock.price !== null && balance.balance < stock.price) {
          this.logger.warn('Недостаточно баланса в боте', { orderId, balance: balance.balance, price: stock.price });
          await this._markManualAttention(orderId, chatId, 'insufficient_balance');
          return;
        }

        this.logger.info('Запрос к боту → покупка (выкуп)', { orderId });
        const purchase = await this.purchaseLink(orderId);

        if (purchase.status === 'out_of_stock') {
          await this._handleOutOfStock(orderId, chatId);
          return;
        }

        if (!purchase.activationLink) {
          throw new Error('API вернул успех без activation_link');
        }

        this.logger.info('Ссылка получена от бота', {
          orderId,
          linkPreview: `${purchase.activationLink.slice(0, 30)}…`,
        });

        const deliveryText = this._formatDeliveryMessage(orderId, purchase.activationLink);

        if (chatId) {
          await this.sendFunPayMessage(chatId, deliveryText);
          this.logger.info('Ссылка отправлена покупателю', { orderId, chatId });
        } else {
          this.logger.warn('chatId не найден — ссылка только в логе', { orderId });
          this._markManualAttention(orderId, null, 'no_chat_id');
        }

        this.processedOrders.add(orderId);
        this._persistProcessedOrders();
        await this.refreshDashboardStats();
      } catch (err) {
        this.logger.error('Сбой автоматической выдачи', { orderId, error: String(err) });
        await this._markManualAttention(orderId, chatId, 'api_failure');
      }
    }

    // -------------------------------------------------------------------------
    // parseOrderDescription(orderId)
    // -------------------------------------------------------------------------

    async parseOrderDescription(orderId) {
      try {
        const response = await httpRequest(`https://funpay.com/orders/${orderId}/`, {
          method: 'GET',
          headers: { Accept: 'text/html' },
        });

        if (!response.ok) {
          this.logger.warn('Не удалось загрузить страницу заказа', { orderId, status: response.status });
          return '';
        }

        const html = await response.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');

        const selectors = [
          '.order-desc',
          '.order-description',
          '.param-item',
          '.text-bold',
          '.page-content',
          'h1',
        ];

        let parts = [];
        selectors.forEach((sel) => {
          doc.querySelectorAll(sel).forEach((el) => {
            const text = (el.textContent || '').trim();
            if (text) parts.push(text);
          });
        });

        if (!parts.length) {
          parts = [doc.body?.textContent || ''];
        }

        const description = parts.join('\n');
        this.logger.debug('Описание заказа извлечено', { orderId, length: description.length });
        return description;
      } catch (err) {
        this.logger.error('parseOrderDescription error', { orderId, error: String(err) });
        return '';
      }
    }

    _matchesProductKeywords(text) {
      const lower = (text || '').toLowerCase();
      return PUBLIC_CONFIG.PRODUCT_KEYWORDS.some((kw) => lower.includes(kw.toLowerCase()));
    }

    async _resolveOrderChat(orderId) {
      try {
        const response = await httpRequest(`https://funpay.com/orders/${orderId}/`, {
          method: 'GET',
          headers: { Accept: 'text/html' },
        });
        if (!response.ok) return null;

        const html = await response.text();
        const chatLinkMatch = html.match(/\/chat\/\?node=(\d+)/i) || html.match(/data-node="(\d+)"/i);
        const buyerMatch = html.match(/buyer-username[^>]*>([^<]+)</i) || html.match(/"buyer"\s*:\s*"([^"]+)"/i);

        return {
          chatId: chatLinkMatch ? chatLinkMatch[1] : null,
          buyer: buyerMatch ? buyerMatch[1].trim() : null,
        };
      } catch {
        return null;
      }
    }

    // -------------------------------------------------------------------------
    // handleBotAPI(action, payload) — единая точка входа с retry
    // -------------------------------------------------------------------------

    async handleBotAPI(action, payload = {}) {
      const paths = PUBLIC_CONFIG.API_PATHS;
      let url;
      let method = 'GET';
      let body = null;

      switch (action) {
        case 'balance':
          url = `${SecureConfig.botApiUrl}${paths.balance}`;
          break;
        case 'stock':
          url = `${SecureConfig.botApiUrl}${paths.stock}?product=${encodeURIComponent(PUBLIC_CONFIG.BOT_PRODUCT_ID)}`;
          break;
        case 'purchase':
          url = `${SecureConfig.botApiUrl}${paths.purchase}`;
          method = 'POST';
          body = JSON.stringify({
            product: PUBLIC_CONFIG.BOT_PRODUCT_ID,
            order_id: payload.orderId,
            ...payload.extra,
          });
          break;
        default:
          throw new Error(`Unknown bot API action: ${action}`);
      }

      const headers = {
        Accept: 'application/json',
        Authorization: `Bearer ${SecureConfig.botApiKey}`,
      };
      if (body) headers['Content-Type'] = 'application/json';

      let lastError = null;

      for (let attempt = 1; attempt <= PUBLIC_CONFIG.API_RETRY_COUNT; attempt++) {
        try {
          this.logger.debug(`API запрос: ${action}`, { attempt, url: url.replace(/\/\/.*@/, '//***@') });

          const response = await httpRequest(url, { method, headers, body });

          if (response.status >= 500) {
            throw new Error(`Server error ${response.status}`);
          }

          const data = await response.json();

          if (!response.ok) {
            const msg = data?.message || data?.error || `HTTP ${response.status}`;
            throw new Error(msg);
          }

          return this._normalizeBotResponse(action, data);
        } catch (err) {
          lastError = err;
          this.logger.warn(`API ${action} — попытка ${attempt}/${PUBLIC_CONFIG.API_RETRY_COUNT} неудачна`, {
            error: String(err),
          });

          if (attempt < PUBLIC_CONFIG.API_RETRY_COUNT) {
            await sleep(PUBLIC_CONFIG.API_RETRY_DELAY_MS);
          }
        }
      }

      throw lastError || new Error(`API ${action} failed after retries`);
    }

    _normalizeBotResponse(action, data) {
      switch (action) {
        case 'balance':
          return {
            balance: Number(data.balance ?? data.amount ?? 0),
            currency: data.currency || 'RUB',
            raw: data,
          };
        case 'stock':
          return {
            available: Number(data.available ?? data.quantity ?? data.stock ?? 0),
            price: data.price != null ? Number(data.price) : null,
            status: data.status === 'out_of_stock' || data.available === 0 ? 'out_of_stock' : 'ok',
            raw: data,
          };
        case 'purchase': {
          const link =
            data.activation_link ||
            data.link ||
            data.url ||
            data.activationLink ||
            null;
          const status =
            data.status === 'out_of_stock' || data.error === 'out_of_stock'
              ? 'out_of_stock'
              : link
                ? 'ok'
                : 'error';
          return { activationLink: link, status, raw: data };
        }
        default:
          return data;
      }
    }

    async checkBalance() {
      const result = await this.handleBotAPI('balance');
      this.lastBalance = result;
      this.uiPanel?.updateStats();
      return result;
    }

    async checkStock() {
      const result = await this.handleBotAPI('stock');
      this.lastStock = result;
      this.uiPanel?.updateStats();
      return result;
    }

    async purchaseLink(orderId) {
      return this.handleBotAPI('purchase', { orderId });
    }

    async refreshDashboardStats() {
      try {
        await this.checkBalance();
        await this.checkStock();
        this.status = 'online';
      } catch (err) {
        this.status = 'degraded';
        this.logger.warn('Не удалось обновить статистику панели', { error: String(err) });
      }
      this.uiPanel?.updateStats();
    }

    // -------------------------------------------------------------------------
    // sendFunPayMessage(chatId, text)
    // -------------------------------------------------------------------------

    async sendFunPayMessage(chatId, text) {
      const csrf = getCsrfToken();
      if (!csrf) {
        throw new Error('CSRF token not found — авторизуйтесь на FunPay');
      }

      const form = new URLSearchParams();
      form.append('node', String(chatId));
      form.append('content', text);
      form.append('csrf_token', csrf);

      const response = await httpRequest('https://funpay.com/runner/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: form.toString(),
      });

      if (!response.ok) {
        const errText = await response.text().catch(() => '');
        throw new Error(`FunPay send failed: ${response.status} ${errText.slice(0, 200)}`);
      }

      this.logger.debug('Сообщение отправлено в FunPay', { chatId, length: text.length });
    }

    // -------------------------------------------------------------------------
    // Исключительные ситуации
    // -------------------------------------------------------------------------

    async _handleOutOfStock(orderId, chatId) {
      this.logger.warn('Товар закончился в боте → предзаказ', { orderId });

      if (!this.preorderQueue.find((p) => p.orderId === orderId)) {
        this.preorderQueue.push({
          orderId,
          chatId,
          queuedAt: timestamp(),
          status: 'waiting_restock',
        });
        this._persistPreorderQueue();
      }

      const msg = [
        '⏳ **Gemini Link — временно нет в наличии**',
        '',
        'К сожалению, активационные ссылки на 18 месяцев сейчас закончились у поставщика.',
        '',
        '✅ Ваш заказ **автоматически поставлен в очередь предзаказа**.',
        `📋 ID заказа: \`${orderId}\``,
        '',
        'Мы выдадим ссылку сразу после пополнения склада — обычно это занимает от нескольких минут до нескольких часов.',
        '',
        '💬 Если нужна срочная помощь — напишите `/Gemini help`',
      ].join('\n');

      if (chatId) {
        await this.sendFunPayMessage(chatId, msg);
      }

      this._markManualAttention(orderId, chatId, 'out_of_stock', false);
    }

    async _markManualAttention(orderId, chatId, reason, notifyBuyer = true) {
      this.logger.error('⚠️ ТРЕБУЕТ РУЧНОГО ВНИМАНИЯ', { orderId, reason });

      if (!this.manualAttention.find((m) => m.orderId === orderId)) {
        this.manualAttention.push({ orderId, chatId, reason, at: timestamp() });
        this._persistManualAttention();
      }

      this.status = 'attention_required';
      this.uiPanel?.updateStats();

      if (notifyBuyer && chatId) {
        const msg = [
          '🛠 **Техническая задержка**',
          '',
          'При автоматической выдаче вашей подписки Gemini произошла временная ошибка.',
          'Продавец уже получил уведомление и обработает заказ вручную в ближайшее время.',
          '',
          `📋 Заказ: \`${orderId}\``,
          '',
          'Приносим извинения за неудобства! 🙏',
        ].join('\n');

        try {
          await this.sendFunPayMessage(chatId, msg);
        } catch (err) {
          this.logger.error('Не удалось уведомить покупателя', { orderId, error: String(err) });
        }
      }
    }

    _formatDeliveryMessage(orderId, link) {
      return [
        '🎉 **Ваша подписка Gemini готова!**',
        '',
        '🔗 **Ссылка для активации (18 месяцев):**',
        link,
        '',
        '📌 **Инструкция:**',
        '1. Перейдите по ссылке выше',
        '2. Следуйте шагам активации на сайте Google',
        '3. Если возникнут вопросы — `/Gemini help`',
        '',
        `📋 Заказ: \`${orderId}\``,
        '',
        'Спасибо за покупку! ⭐',
      ].join('\n');
    }

    // -------------------------------------------------------------------------
    // Модуль командного меню (до покупки / в чате)
    // -------------------------------------------------------------------------

    _installMessageInterceptor() {
      this._observeChatMessages();
      this._pollActiveChatInput();
      this.logger.debug('Перехватчик команд /Gemini установлен');
    }

    _observeChatMessages() {
      const processNode = (node) => {
        if (!node || !node.querySelectorAll) return;
        node.querySelectorAll('.chat-msg-text, .message-text, [class*="message"]').forEach((el) => {
          const text = (el.textContent || '').trim();
          if (text.startsWith('/Gemini') || text.startsWith('/gemini')) {
            this._handleGeminiCommand(text, el);
          }
        });
      };

      this.messageObserver = new MutationObserver((mutations) => {
        for (const m of mutations) {
          m.addedNodes.forEach((n) => {
            if (n.nodeType === 1) processNode(n);
          });
        }
      });

      this.messageObserver.observe(document.body, { childList: true, subtree: true });
    }

    _pollActiveChatInput() {
      document.addEventListener(
        'keydown',
        (e) => {
          if (e.key !== 'Enter' || e.shiftKey) return;
          const input = document.querySelector(PUBLIC_CONFIG.FUNPAY.chatMessageInput);
          if (!input || document.activeElement !== input) return;

          const text = (input.value || '').trim();
          if (!/^\/gemini/i.test(text)) return;

          e.preventDefault();
          e.stopPropagation();

          const chatId = this._getCurrentChatId();
          this._handleGeminiCommand(text, null, chatId);
          input.value = '';
        },
        true
      );
    }

    _getCurrentChatId() {
      const urlMatch = window.location.search.match(/node=(\d+)/);
      if (urlMatch) return urlMatch[1];

      const active = document.querySelector('[data-node-id]');
      if (active) return active.getAttribute('data-node-id');

      const link = document.querySelector('a[href*="node="]');
      if (link) {
        const m = link.href.match(/node=(\d+)/);
        if (m) return m[1];
      }
      return null;
    }

    async _handleGeminiCommand(rawText, messageEl, forcedChatId = null) {
      const chatId = forcedChatId || this._getCurrentChatId();
      if (!chatId) {
        this.logger.warn('Команда /Gemini без chatId');
        return;
      }

      if (this.disabledChats.has(chatId)) {
        const until = this.disabledChats.get(chatId);
        if (Date.now() < until) {
          this.logger.info('Автоответ отключён для чата (help)', { chatId });
          return;
        }
        this.disabledChats.delete(chatId);
        this._persistDisabledChats();
      }

      const parts = rawText.trim().split(/\s+/);
      const sub = (parts[1] || '').toLowerCase();

      this.logger.info('Команда /Gemini получена', { chatId, sub: sub || 'menu' });

      if (!sub) {
        await this.sendFunPayMessage(chatId, this._geminiMainMenu());
        return;
      }

      switch (sub) {
        case 'check':
        case 'наличие':
        case 'stock':
          await this._cmdCheck(chatId);
          break;
        case 'preorder':
        case 'предзаказ':
          await this._cmdPreorder(chatId);
          break;
        case 'help':
        case 'помощь':
          await this._cmdHelp(chatId);
          break;
        default:
          await this.sendFunPayMessage(chatId, this._geminiMainMenu());
      }
    }

    _geminiMainMenu() {
      return [
        '🌟 **Gemini Link — Меню**',
        '',
        'Добро пожаловать! Выберите действие:',
        '',
        '📦 `/Gemini check` — Проверить наличие',
        '📝 `/Gemini preorder` — Сделать предзаказ',
        '🆘 `/Gemini help` — Позвать продавца',
        '',
        '💡 После оплаты ссылка выдаётся **автоматически** в течение нескольких минут.',
      ].join('\n');
    }

    async _cmdCheck(chatId) {
      try {
        const stock = await this.checkStock();
        const msg = [
          '📦 **Проверка наличия**',
          '',
          `✅ В наличии: **${stock.available} шт.**`,
          '',
          stock.available > 0
            ? '🛒 Можете оформить заказ — ссылка придёт автоматически после оплаты!'
            : '⏳ Сейчас нет в наличии. Используйте `/Gemini preorder` для бронирования.',
        ].join('\n');
        await this.sendFunPayMessage(chatId, msg);
      } catch (err) {
        await this.sendFunPayMessage(
          chatId,
          '⚠️ Не удалось проверить наличие. Попробуйте позже или `/Gemini help`.'
        );
        this.logger.error('cmd check failed', { error: String(err) });
      }
    }

    async _cmdPreorder(chatId) {
      const entry = {
        chatId,
        type: 'preorder_intent',
        at: timestamp(),
      };

      this.preorderQueue.push(entry);
      this._persistPreorderQueue();

      await this.sendFunPayMessage(
        chatId,
        [
          '📝 **Предзаказ Gemini Link (18 мес.)**',
          '',
          'Вы зарегистрированы в списке предзаказа! ✅',
          '',
          '**Как забронировать:**',
          '1. Оформите заказ на лот «Gemini link» на FunPay',
          '2. После оплаты ссылка будет выдана автоматически',
          '3. Если товар временно закончится — вы в приоритетной очереди',
          '',
          '💬 Вопросы? `/Gemini help`',
        ].join('\n')
      );
    }

    async _cmdHelp(chatId) {
      const disableUntil = Date.now() + 30 * 60 * 1000;
      this.disabledChats.set(chatId, disableUntil);
      this._persistDisabledChats();

      this.logger.warn('🆘 ПРОДАВЕЦ: требуется ручное вмешательство', { chatId });

      await this.sendFunPayMessage(
        chatId,
        [
          '🆘 **Продавец уведомлён!**',
          '',
          'Автоответчик временно отключён для этого чата.',
          'Продавец ответит вам лично в ближайшее время.',
          '',
          '⏳ Обычно ответ — в течение 15–30 минут.',
        ].join('\n')
      );
    }
  }

  // ===========================================================================
  // UI-панель продавца
  // ===========================================================================

  class SellerUIPanel {
    constructor(plugin) {
      this.plugin = plugin;
      this.root = null;
      this.collapsed = false;
    }

    render() {
      if (document.getElementById('fgp-panel-root')) return;

      GM_addStyle(`
        #fgp-panel-root {
          position: fixed;
          bottom: 16px;
          right: 16px;
          z-index: 999999;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          font-size: 12px;
          color: #e8eaed;
          user-select: none;
        }
        #fgp-panel {
          width: 280px;
          background: linear-gradient(145deg, #1a1d23 0%, #12151a 100%);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 12px;
          box-shadow: 0 8px 32px rgba(0,0,0,0.45);
          overflow: hidden;
        }
        #fgp-panel-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 10px 12px;
          background: rgba(255,255,255,0.04);
          cursor: pointer;
          border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        #fgp-panel-header h3 {
          margin: 0;
          font-size: 13px;
          font-weight: 600;
          display: flex;
          align-items: center;
          gap: 6px;
        }
        .fgp-status-dot {
          width: 8px;
          height: 8px;
          border-radius: 50%;
          display: inline-block;
        }
        .fgp-status-online { background: #34d399; box-shadow: 0 0 6px #34d399; }
        .fgp-status-degraded { background: #fbbf24; box-shadow: 0 0 6px #fbbf24; }
        .fgp-status-attention { background: #f87171; box-shadow: 0 0 6px #f87171; box-shadow: 0 0 6px #f87171; animation: fgp-pulse 1.5s infinite; }
        .fgp-status-config { background: #94a3b8; }
        @keyframes fgp-pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
        #fgp-panel-body { padding: 10px 12px; }
        .fgp-stat-row {
          display: flex;
          justify-content: space-between;
          padding: 4px 0;
          border-bottom: 1px solid rgba(255,255,255,0.04);
        }
        .fgp-stat-label { color: #9ca3af; }
        .fgp-stat-value { font-weight: 600; color: #f3f4f6; }
        #fgp-log-section { margin-top: 8px; }
        #fgp-log-section h4 {
          margin: 0 0 6px;
          font-size: 11px;
          color: #9ca3af;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }
        #fgp-log-list {
          max-height: 100px;
          overflow-y: auto;
          font-size: 10px;
          line-height: 1.4;
          color: #d1d5db;
        }
        #fgp-log-list .fgp-log-entry { padding: 2px 0; border-bottom: 1px solid rgba(255,255,255,0.03); }
        #fgp-log-list .fgp-log-ts { color: #6b7280; }
        #fgp-panel-actions {
          display: flex;
          gap: 6px;
          margin-top: 8px;
        }
        #fgp-panel-actions button {
          flex: 1;
          padding: 5px 8px;
          border: none;
          border-radius: 6px;
          background: rgba(99,102,241,0.25);
          color: #a5b4fc;
          font-size: 10px;
          cursor: pointer;
        }
        #fgp-panel-actions button:hover { background: rgba(99,102,241,0.4); }
        #fgp-panel.collapsed #fgp-panel-body { display: none; }
      `);

      this.root = document.createElement('div');
      this.root.id = 'fgp-panel-root';
      this.root.innerHTML = `
        <div id="fgp-panel">
          <div id="fgp-panel-header">
            <h3><span class="fgp-status-dot fgp-status-config" id="fgp-status-dot"></span> Gemini Auto</h3>
            <span id="fgp-toggle-icon">▼</span>
          </div>
          <div id="fgp-panel-body">
            <div class="fgp-stat-row">
              <span class="fgp-stat-label">Статус</span>
              <span class="fgp-stat-value" id="fgp-stat-status">—</span>
            </div>
            <div class="fgp-stat-row">
              <span class="fgp-stat-label">Баланс бота</span>
              <span class="fgp-stat-value" id="fgp-stat-balance">—</span>
            </div>
            <div class="fgp-stat-row">
              <span class="fgp-stat-label">Gemini 18 мес.</span>
              <span class="fgp-stat-value" id="fgp-stat-stock">—</span>
            </div>
            <div class="fgp-stat-row">
              <span class="fgp-stat-label">Обработано</span>
              <span class="fgp-stat-value" id="fgp-stat-processed">0</span>
            </div>
            <div id="fgp-log-section">
              <h4>Последние действия</h4>
              <div id="fgp-log-list"></div>
            </div>
            <div id="fgp-panel-actions">
              <button id="fgp-btn-refresh">↻ Обновить</button>
              <button id="fgp-btn-config">⚙ API</button>
            </div>
          </div>
        </div>
      `;

      document.body.appendChild(this.root);

      document.getElementById('fgp-panel-header').addEventListener('click', () => {
        this.collapsed = !this.collapsed;
        document.getElementById('fgp-panel').classList.toggle('collapsed', this.collapsed);
        document.getElementById('fgp-toggle-icon').textContent = this.collapsed ? '▲' : '▼';
      });

      document.getElementById('fgp-btn-refresh').addEventListener('click', (e) => {
        e.stopPropagation();
        this.plugin.refreshDashboardStats();
      });

      document.getElementById('fgp-btn-config').addEventListener('click', async (e) => {
        e.stopPropagation();
        await this.plugin._promptSecureConfig();
        if (SecureConfig.isConfigured()) {
          this.plugin.status = 'online';
          await this.plugin.refreshDashboardStats();
        }
        this.updateStats();
      });

      this.updateStats();
      this.renderLogs();
    }

    _statusLabel(status) {
      const map = {
        online: '🟢 В сети',
        degraded: '🟡 Частично',
        attention_required: '🔴 Внимание!',
        config_required: '⚙ Настройка',
        offline: '⚫ Оффлайн',
      };
      return map[status] || status;
    }

    _statusDotClass(status) {
      const map = {
        online: 'fgp-status-online',
        degraded: 'fgp-status-degraded',
        attention_required: 'fgp-status-attention',
        config_required: 'fgp-status-config',
        offline: 'fgp-status-config',
      };
      return map[status] || 'fgp-status-config';
    }

    updateStats() {
      const p = this.plugin;
      const dot = document.getElementById('fgp-status-dot');
      const statusEl = document.getElementById('fgp-stat-status');
      const balanceEl = document.getElementById('fgp-stat-balance');
      const stockEl = document.getElementById('fgp-stat-stock');
      const processedEl = document.getElementById('fgp-stat-processed');

      if (!dot) return;

      dot.className = `fgp-status-dot ${this._statusDotClass(p.status)}`;
      if (statusEl) statusEl.textContent = this._statusLabel(p.status);

      if (balanceEl) {
        balanceEl.textContent =
          p.lastBalance != null
            ? `${p.lastBalance.balance} ${p.lastBalance.currency}`
            : '—';
      }

      if (stockEl) {
        stockEl.textContent =
          p.lastStock != null ? `${p.lastStock.available} шт.` : '—';
      }

      if (processedEl) {
        processedEl.textContent = String(p.processedOrders.size);
      }
    }

    pushLog(entry) {
      this.renderLogs();
    }

    renderLogs() {
      const list = document.getElementById('fgp-log-list');
      if (!list) return;

      const logs = this.plugin.logger.getRecent(5);
      list.innerHTML = logs
        .map(
          (e) =>
            `<div class="fgp-log-entry"><span class="fgp-log-ts">${e.ts}</span> ${this._escapeHtml(e.message)}</div>`
        )
        .join('');
    }

    _escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    }
  }

  // ===========================================================================
  // Fallback для GM_addStyle вне Tampermonkey
  // ===========================================================================

  if (typeof GM_addStyle !== 'function') {
    window.GM_addStyle = function (css) {
      const style = document.createElement('style');
      style.textContent = css;
      document.head.appendChild(style);
    };
  }

  // ===========================================================================
  // Точка входа
  // ===========================================================================

  const plugin = new FunPayGeminiPlugin();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => plugin.init());
  } else {
    plugin.init();
  }

  window.FunPayGeminiPlugin = plugin;
})();
