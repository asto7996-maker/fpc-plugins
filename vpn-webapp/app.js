/**
 * Paskod Mini App — gold-standard TMA safe areas + navigation
 */
(function () {
  "use strict";

  const root = document.documentElement;
  const tg = window.Telegram && window.Telegram.WebApp;

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }

  function readCssEnv(name) {
    try {
      const el = document.createElement("div");
      el.style.cssText = `position:fixed;visibility:hidden;padding-top:env(${name},0px)`;
      document.body.appendChild(el);
      const px = parseFloat(getComputedStyle(el).paddingTop) || 0;
      el.remove();
      return px;
    } catch (_) {
      return 0;
    }
  }

  /**
   * Official TMA model:
   * - safeAreaInset = system (notch / status bar)
   * - contentSafeAreaInset = Telegram UI within the safe area
   * - When fullscreen, Close/••• overlay the WebView → add TG chrome height
   * - When expanded normally, Close is outside the WebView → light breath only
   */
  function applySafeArea() {
    let top = 0;
    let bottom = 0;
    let left = 0;
    let right = 0;
    let tgBar = 0;

    const envTop = readCssEnv("safe-area-inset-top");
    const envBottom = readCssEnv("safe-area-inset-bottom");
    const envLeft = readCssEnv("safe-area-inset-left");
    const envRight = readCssEnv("safe-area-inset-right");

    if (tg) {
      try { tg.ready(); } catch (_) {}
      try { if (tg.expand) tg.expand(); } catch (_) {}

      const sa = tg.safeAreaInset || {};
      const csa = tg.contentSafeAreaInset || {};
      const platform = String(tg.platform || "").toLowerCase();
      const fs = !!tg.isFullscreen;
      const insetTop = Math.max(num(sa.top), num(csa.top), envTop);

      if (fs) {
        // Content draws under system UI + Telegram Close bar
        top = insetTop;
        tgBar = platform === "ios" ? 46 : 50;
      } else if (insetTop > 0) {
        top = insetTop;
      } else {
        // Close is outside the WebView
        top = 0;
        tgBar = 0;
      }

      bottom = Math.max(num(sa.bottom), num(csa.bottom), envBottom);
      left = Math.max(num(sa.left), num(csa.left), envLeft);
      right = Math.max(num(sa.right), num(csa.right), envRight);

      root.classList.add("is-tma");
      root.classList.toggle("is-tma-fs", fs);
      try {
        tg.setHeaderColor("#0b1736");
        tg.setBackgroundColor("#070b1c");
      } catch (_) {}
    } else {
      top = envTop;
      bottom = envBottom;
      left = envLeft;
      right = envRight;
      root.classList.remove("is-tma", "is-tma-fs");
    }

    // Soft ceilings — avoid pathological empty bands
    top = Math.min(top, 80);
    tgBar = Math.min(tgBar, 56);
    bottom = Math.min(bottom, 40);

    // Non-overlay TMA: tiny breath under Close
    if (tg && !tg.isFullscreen && top === 0 && tgBar === 0) {
      top = 12;
    }

    root.style.setProperty("--tg-safe-top", `${top}px`);
    root.style.setProperty("--tg-bar", `${tgBar}px`);
    root.style.setProperty("--tg-safe-bottom", `${bottom}px`);
    root.style.setProperty("--tg-safe-left", `${left}px`);
    root.style.setProperty("--tg-safe-right", `${right}px`);
  }

  /* ---------- Navigation ---------- */
  const pages = [...document.querySelectorAll(".page")];
  const dockItems = [...document.querySelectorAll(".dock__item")];

  function go(name) {
    pages.forEach((p) => {
      const on = p.dataset.page === name;
      p.hidden = !on;
      p.classList.toggle("is-active", on);
    });
    dockItems.forEach((b) => b.classList.toggle("is-active", b.dataset.nav === name));
    history.replaceState(null, "", `#${name}`);
    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  }

  document.addEventListener("click", (e) => {
    const nav = e.target.closest("[data-nav]");
    if (!nav) return;
    e.preventDefault();
    go(nav.dataset.nav);
  });

  const initial = (location.hash || "#home").slice(1) || "home";
  go(document.getElementById(`page-${initial}`) ? initial : "home");

  /* ---------- Light extras ---------- */
  const nameEl = document.getElementById("userName");
  if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
    const u = tg.initDataUnsafe.user;
    nameEl.textContent = u.first_name || u.username || "друг";
  }

  let sec = 18;
  const refreshSec = document.getElementById("refreshSec");
  setInterval(() => {
    sec = sec <= 0 ? 20 : sec - 1;
    if (refreshSec) refreshSec.textContent = `${sec}s`;
  }, 1000);

  const end = Date.UTC(2027, 0, 13, 12, 0, 0);
  const cd = document.getElementById("countdown");
  function tickCd() {
    if (!cd) return;
    const diff = Math.max(0, end - Date.now());
    const d = Math.floor(diff / 86400000);
    const h = Math.floor((diff % 86400000) / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    const s = Math.floor((diff % 60000) / 1000);
    const p = (n) => String(n).padStart(2, "0");
    cd.textContent = `${d} дн. ${p(h)} : ${p(m)} : ${p(s)}`;
  }
  tickCd();
  setInterval(tickCd, 1000);

  document.getElementById("btnTheme")?.addEventListener("click", () => {
    document.body.classList.toggle("theme-flip");
  });

  /* ---------- Boot safe areas ---------- */
  applySafeArea();
  [50, 200, 600, 1200, 2500].forEach((ms) => setTimeout(applySafeArea, ms));
  window.addEventListener("resize", applySafeArea);
  if (tg && tg.onEvent) {
    ["safeAreaChanged", "contentSafeAreaChanged", "viewportChanged", "fullscreenChanged"].forEach((ev) => {
      try { tg.onEvent(ev, applySafeArea); } catch (_) {}
    });
  }
})();
