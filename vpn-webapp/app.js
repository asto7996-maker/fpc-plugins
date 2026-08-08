/* ======================================================================
   AuroraVPN — Telegram Mini App · логика интерфейса
   Автономный демо-фронтенд: подключение, серверы, тарифы, профиль.
   ====================================================================== */
(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  /* -------------------------------------------------------------- utils */
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function haptic(type = "light") {
    try {
      if (!tg || !tg.HapticFeedback) return;
      if (type === "success" || type === "error" || type === "warning") {
        tg.HapticFeedback.notificationOccurred(type);
      } else {
        tg.HapticFeedback.impactOccurred(type);
      }
    } catch (_) {}
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  /* -------------------------------------------------------- Telegram init */
  function initTelegram() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) tg.setHeaderColor("#060814");
      if (tg.setBackgroundColor) tg.setBackgroundColor("#060814");
      if (tg.enableClosingConfirmation) tg.enableClosingConfirmation();

      const u = tg.initDataUnsafe && tg.initDataUnsafe.user;
      if (u) {
        const name = [u.first_name, u.last_name].filter(Boolean).join(" ") || u.username || "Пользователь";
        $("#profName").textContent = name;
        const initials = (u.first_name || u.username || "U").trim().charAt(0).toUpperCase();
        $("#profAvatar").textContent = initials;
        $("#avatarFallback").textContent = initials;
        if (u.photo_url) {
          const img = $("#avatar");
          img.src = u.photo_url;
          img.onload = () => { img.classList.add("show"); $("#avatarFallback").style.display = "none"; };
        }
      }
    } catch (_) {}
  }

  /* --------------------------------------------------- фон: звёздное поле */
  function initStars() {
    const canvas = $("#stars");
    const ctx = canvas.getContext("2d");
    let w, h, dpr, stars = [];
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = canvas.clientWidth; h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.min(120, Math.floor((w * h) / 9000));
      stars = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        r: Math.random() * 1.4 + 0.3,
        a: Math.random() * 0.6 + 0.2,
        tw: Math.random() * 0.02 + 0.004,
        dy: Math.random() * 0.12 + 0.02,
        p: Math.random() * Math.PI * 2,
      }));
    }

    function frame() {
      ctx.clearRect(0, 0, w, h);
      for (const s of stars) {
        s.p += s.tw;
        s.y += s.dy;
        if (s.y > h + 2) { s.y = -2; s.x = Math.random() * w; }
        const alpha = s.a * (0.6 + 0.4 * Math.sin(s.p));
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(200, 215, 255, ${alpha})`;
        ctx.shadowBlur = 6; ctx.shadowColor = "rgba(124,92,255,.6)";
        ctx.fill();
      }
      ctx.shadowBlur = 0;
      requestAnimationFrame(frame);
    }

    resize();
    window.addEventListener("resize", resize);
    if (reduced) {
      // статичный кадр без анимации
      for (const s of stars) {
        ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(200,215,255,${s.a})`; ctx.fill();
      }
    } else {
      requestAnimationFrame(frame);
    }
  }

  /* ---------------------------------------------------------- данные demo */
  const SERVERS = [
    { id: "nl", flag: "🇳🇱", name: "Нидерланды", city: "Амстердам", ping: 24, load: 18 },
    { id: "de", flag: "🇩🇪", name: "Германия", city: "Франкфурт", ping: 31, load: 42 },
    { id: "us", flag: "🇺🇸", name: "США", city: "Нью-Йорк", ping: 96, load: 61 },
    { id: "fi", flag: "🇫🇮", name: "Финляндия", city: "Хельсинки", ping: 19, load: 12, premium: false },
    { id: "jp", flag: "🇯🇵", name: "Япония", city: "Токио", ping: 128, load: 33, premium: true },
    { id: "sg", flag: "🇸🇬", name: "Сингапур", city: "Сингапур", ping: 142, load: 27, premium: true },
    { id: "fr", flag: "🇫🇷", name: "Франция", city: "Париж", ping: 38, load: 51 },
    { id: "tr", flag: "🇹🇷", name: "Турция", city: "Стамбул", ping: 58, load: 44 },
    { id: "ae", flag: "🇦🇪", name: "ОАЭ", city: "Дубай", ping: 74, load: 29, premium: true },
    { id: "gb", flag: "🇬🇧", name: "Великобритания", city: "Лондон", ping: 41, load: 37 },
  ];

  const PLANS = [
    { id: "m1",  name: "1 месяц",   price: "199 ₽",  old: "",        per: "/мес",  sub: "Попробовать без обязательств" },
    { id: "m12", name: "12 месяцев",price: "99 ₽",   old: "199 ₽",   per: "/мес",  sub: "Экономия 50% · списание раз в год", ribbon: "Выгодно" },
    { id: "m6",  name: "6 месяцев", price: "139 ₽",  old: "199 ₽",   per: "/мес",  sub: "Баланс цены и гибкости" },
  ];

  const state = {
    connected: false,
    connecting: false,
    currentServer: SERVERS[0],
    selectedPlan: "m12",
    timerId: null,
    metricsId: null,
    seconds: 0,
  };

  /* --------------------------------------------------------- подключение */
  const ring = $("#connectRing");
  const connectWrap = $(".connect");
  const statusText = $("#statusText");
  const mainAction = $("#mainAction");
  const mainActionText = $("#mainActionText");

  function fmtTime(s) {
    const h = String(Math.floor(s / 3600)).padStart(2, "0");
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const sec = String(s % 60).padStart(2, "0");
    return `${h}:${m}:${sec}`;
  }

  function startMetrics() {
    let dl = 0, ul = 0;
    state.metricsId = setInterval(() => {
      dl = Math.max(0, dl + (Math.random() - 0.45) * 40);
      ul = Math.max(0, ul + (Math.random() - 0.45) * 18);
      $("#dlValue").textContent = (60 + dl).toFixed(1);
      $("#ulValue").textContent = (18 + ul).toFixed(1);
      $("#pingValue").textContent = String(state.currentServer.ping + Math.floor(Math.random() * 6));
    }, 1200);
  }
  function stopMetrics() {
    clearInterval(state.metricsId);
    $("#dlValue").textContent = "0.0";
    $("#ulValue").textContent = "0.0";
    $("#pingValue").textContent = "—";
  }

  function setConnected(on) {
    state.connected = on;
    state.connecting = false;
    connectWrap.classList.remove("is-connecting");
    connectWrap.classList.toggle("is-on", on);
    mainAction.classList.toggle("is-on", on);

    if (on) {
      statusText.textContent = "Защищено";
      mainActionText.textContent = "Отключиться";
      state.seconds = 0;
      $("#timer").textContent = "00:00:00";
      state.timerId = setInterval(() => { state.seconds++; $("#timer").textContent = fmtTime(state.seconds); }, 1000);
      startMetrics();
      haptic("success");
      toast("Соединение установлено · " + state.currentServer.name);
    } else {
      statusText.textContent = "Отключено";
      mainActionText.textContent = "Подключиться";
      clearInterval(state.timerId);
      $("#timer").textContent = "00:00:00";
      stopMetrics();
      haptic("warning");
      toast("VPN отключён");
    }
    updateMainButton();
  }

  function toggleConnection() {
    if (state.connecting) return;
    if (state.connected) { setConnected(false); return; }
    // процесс подключения
    state.connecting = true;
    connectWrap.classList.add("is-connecting");
    statusText.textContent = "Подключение…";
    mainActionText.textContent = "Подключение…";
    haptic("light");
    setTimeout(() => setConnected(true), 1400);
  }

  ring.addEventListener("click", toggleConnection);
  ring.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleConnection(); } });
  mainAction.addEventListener("click", () => {
    const active = $(".tab--active").dataset.tab;
    if (active === "plans") { subscribe(); return; }
    toggleConnection();
  });

  /* --------------------------------------- Telegram MainButton (по вкладке) */
  function updateMainButton() {
    if (!tg || !tg.MainButton) return;
    const active = $(".tab--active").dataset.tab;
    const mb = tg.MainButton;
    if (active === "plans") {
      mb.setParams({ text: "Оформить подписку", color: "#7c5cff", text_color: "#ffffff", is_active: true, is_visible: true });
    } else {
      mb.setParams({
        text: state.connected ? "Отключиться" : "Подключиться",
        color: state.connected ? "#e1435f" : "#7c5cff",
        text_color: "#ffffff", is_active: true, is_visible: true,
      });
    }
  }

  /* --------------------------------------------------------- рендер серверов */
  function pingClass(p) { return p < 50 ? "good" : p < 100 ? "mid" : "bad"; }

  function renderServers(filter = "") {
    const list = $("#serverList");
    const q = filter.trim().toLowerCase();
    const items = SERVERS.filter((s) => !q || s.name.toLowerCase().includes(q) || s.city.toLowerCase().includes(q));
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = `<div class="glass" style="padding:20px;text-align:center;color:var(--muted)">Ничего не найдено</div>`;
      return;
    }
    for (const s of items) {
      const el = document.createElement("button");
      el.className = "srv glass" + (s.id === state.currentServer.id ? " is-active" : "");
      el.innerHTML = `
        <span class="srv__flag">${s.flag}</span>
        <span class="srv__info">
          <span class="srv__name">${s.name} ${s.premium ? '<span class="srv__badge">PRO</span>' : ""}</span>
          <span class="srv__meta">${s.city} · загрузка ${s.load}%</span>
        </span>
        <span class="srv__ping ${pingClass(s.ping)}">${s.ping} мс</span>
        <span class="srv__check"></span>`;
      el.addEventListener("click", () => selectServer(s));
      list.appendChild(el);
    }
  }

  function selectServer(s) {
    state.currentServer = s;
    $("#curFlag").textContent = s.flag;
    $("#curName").textContent = `${s.name} · ${s.city}`;
    renderServers($("#serverSearch").value);
    haptic("light");
    toast("Локация: " + s.name);
    if (state.connected) toast("Переключение на " + s.name + "…");
    // после выбора возвращаемся на главную
    setTimeout(() => switchTab("home"), 250);
  }

  $("#serverSearch").addEventListener("input", (e) => renderServers(e.target.value));
  $("#serverCard").addEventListener("click", () => switchTab("servers"));

  /* --------------------------------------------------------- рендер тарифов */
  function renderPlans() {
    const wrap = $("#planList");
    wrap.innerHTML = "";
    for (const p of PLANS) {
      const el = document.createElement("button");
      el.className = "plan glass" + (p.id === state.selectedPlan ? " is-selected" : "");
      el.innerHTML = `
        <span class="plan__glow"></span>
        <span class="plan__radio"></span>
        <div class="plan__head">
          <span class="plan__name">${p.name}</span>
          ${p.ribbon ? `<span class="plan__ribbon">${p.ribbon}</span>` : ""}
        </div>
        <div class="plan__price">
          <span class="plan__cur">${p.price}</span>
          <span class="plan__per">${p.per}</span>
          ${p.old ? `<span class="plan__old">${p.old}</span>` : ""}
        </div>
        <div class="plan__sub">${p.sub}</div>`;
      el.addEventListener("click", () => {
        state.selectedPlan = p.id;
        renderPlans();
        haptic("light");
      });
      wrap.appendChild(el);
    }
  }

  function subscribe() {
    const p = PLANS.find((x) => x.id === state.selectedPlan);
    haptic("success");
    toast(`Оформляем «${p.name}» за ${p.price}…`);
    if (tg && tg.sendData) {
      try { tg.sendData(JSON.stringify({ action: "subscribe", plan: p.id })); } catch (_) {}
    }
  }

  /* --------------------------------------------------------- переключение вкладок */
  function switchTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("tab--active", t.dataset.tab === name));
    $$(".page").forEach((pg) => pg.classList.toggle("page--active", pg.id === "page-" + name));
    // прячем нижнюю кнопку на профиле и серверах
    const showCta = name === "home" || name === "plans";
    $(".cta").style.display = showCta ? "block" : "none";
    if (name === "plans") mainActionText.textContent = "Оформить подписку";
    else if (name === "home") mainActionText.textContent = state.connected ? "Отключиться" : "Подключиться";
    updateMainButton();
    haptic("light");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  /* --------------------------------------------------------- профиль/настройки */
  $$(".setting[data-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sw = $(".switch", btn);
      sw.classList.toggle("on");
      haptic("light");
    });
  });
  $("#supportBtn").addEventListener("click", () => {
    haptic("light");
    if (tg && tg.openTelegramLink) tg.openTelegramLink("https://t.me/");
    else toast("Открываем чат поддержки…");
  });
  $("#copyRef").addEventListener("click", async () => {
    const code = $("#refCode").textContent.trim();
    try {
      await navigator.clipboard.writeText(code);
      toast("Промокод скопирован: " + code);
    } catch (_) {
      toast("Ваш промокод: " + code);
    }
    haptic("success");
  });
  $("#profileBtn").addEventListener("click", () => switchTab("profile"));

  /* --------------------------------------------------------- Telegram события */
  if (tg && tg.MainButton) {
    tg.MainButton.onClick(() => {
      const active = $(".tab--active").dataset.tab;
      if (active === "plans") subscribe();
      else toggleConnection();
    });
  }
  if (tg && tg.onEvent) {
    tg.onEvent("themeChanged", () => {});
  }

  /* --------------------------------------------------------- запуск */
  initTelegram();
  initStars();
  renderServers();
  renderPlans();
  switchTab("home");

  // приятная деталь: подсветить квоту при загрузке
  requestAnimationFrame(() => { $("#quotaFill").style.width = "34%"; });
})();
