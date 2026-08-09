/**
 * Paskod TMA — gold-standard viewport + safe areas + shell sync
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
   * Telegram WebApp viewport + safe areas
   * - expand() → use full available height (pair with 100dvh, never 100vh)
   * - safeAreaInset → system notch / home indicator
   * - contentSafeAreaInset → Telegram UI (Close / •••) inside the WebView
   * - fullscreen → Close overlays content → reserve --tg-chrome-top
   */
  function applySafeArea() {
    let top = 0;
    let bottom = 0;
    let left = 0;
    let right = 0;
    let chromeTop = 0;

    const envTop = readCssEnv("safe-area-inset-top");
    const envBottom = readCssEnv("safe-area-inset-bottom");
    const envLeft = readCssEnv("safe-area-inset-left");
    const envRight = readCssEnv("safe-area-inset-right");

    if (tg) {
      try { tg.ready(); } catch (_) {}
      try { if (tg.expand) tg.expand(); } catch (_) {}
      try {
        if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
      } catch (_) {}

      const sa = tg.safeAreaInset || {};
      const csa = tg.contentSafeAreaInset || {};
      const platform = String(tg.platform || "").toLowerCase();
      const fs = !!tg.isFullscreen;

      // Prefer content-safe inset (Telegram chrome already accounted for)
      const contentTop = Math.max(num(csa.top), 0);
      const systemTop = Math.max(num(sa.top), envTop);

      if (fs) {
        // Fullscreen: Close / ••• sit ON TOP of the WebView
        top = Math.max(systemTop, contentTop);
        // If content inset is tiny, add explicit TG chrome bar height
        if (contentTop < 40) {
          chromeTop = platform === "ios" ? 48 : 52;
        }
      } else if (contentTop > 0 || systemTop > 0) {
        // Partial overlay / reported insets
        top = Math.max(contentTop, systemTop);
        if (contentTop === 0 && systemTop >= 20 && fs === false) {
          // System notch only — Close usually outside WebView
          chromeTop = 0;
        }
      } else {
        // Classic expanded: Close lives outside the WebView — no chrome band.
        top = 0;
        chromeTop = 0;
      }

      // Prefer Telegram-injected CSS variables when the client provides them
      try {
        const cs = getComputedStyle(root);
        const tgCssTop = parseFloat(cs.getPropertyValue("--tg-content-safe-area-inset-top")) || 0;
        const tgCssSafe = parseFloat(cs.getPropertyValue("--tg-safe-area-inset-top")) || 0;
        if (tgCssTop > top) {
          top = tgCssTop;
          if (tgCssTop >= 40) chromeTop = 0;
        } else if (tgCssSafe > top) {
          top = tgCssSafe;
        }
      } catch (_) {}

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
    top = Math.min(top, 72);
    chromeTop = Math.min(chromeTop, 56);
    bottom = Math.min(bottom, 40);

    root.style.setProperty("--tg-content-safe-area-top", `${top}px`);
    root.style.setProperty("--tg-chrome-top", `${chromeTop}px`);
    root.style.setProperty("--tg-content-safe-area-bottom", `${bottom}px`);
    root.style.setProperty("--tg-content-safe-area-left", `${left}px`);
    root.style.setProperty("--tg-content-safe-area-right", `${right}px`);

    syncDockClearance();
  }

  /** Measure painted dock → keep spacer = dock height + 20px */
  function syncDockClearance() {
    const dock = document.getElementById("appDock");
    if (!dock) return;
    const h = Math.ceil(dock.getBoundingClientRect().height);
    if (h > 40) {
      root.style.setProperty("--dock-h", `${h}px`);
      root.style.setProperty("--dock-clearance", `${h + 20}px`);
    }
  }

  /* ---------- Navigation (scroll only .app-main) ---------- */
  const pages = [...document.querySelectorAll(".page")];
  const dockItems = [...document.querySelectorAll(".app-dock__item")];
  const main = document.getElementById("appMain");

  function go(name) {
    pages.forEach((p) => {
      const on = p.dataset.page === name;
      p.hidden = !on;
      p.classList.toggle("is-active", on);
    });
    dockItems.forEach((b) => b.classList.toggle("is-active", b.dataset.nav === name));
    history.replaceState(null, "", `#${name}`);
    if (main) main.scrollTop = 0;
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
    if (nameEl) nameEl.textContent = u.first_name || u.username || "друг";
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
    root.classList.add("pk-theme-swap");
    document.body.classList.toggle("theme-flip");
    setTimeout(() => root.classList.remove("pk-theme-swap"), 380);
  });

  /* ---------- 120fps: pause FX while scrolling (soft settle) ---------- */
  let scrollTimer = 0;
  let settleTimer = 0;
  function markScrolling() {
    root.classList.add("pk-scrolling");
    root.classList.remove("pk-scroll-settle");
    clearTimeout(scrollTimer);
    clearTimeout(settleTimer);
    scrollTimer = setTimeout(() => {
      root.classList.remove("pk-scrolling");
      root.classList.add("pk-scroll-settle");
      settleTimer = setTimeout(() => root.classList.remove("pk-scroll-settle"), 420);
    }, 140);
  }
  const scrollOpts = { passive: true, capture: true };
  if (main) main.addEventListener("scroll", markScrolling, scrollOpts);
  window.addEventListener("touchmove", markScrolling, scrollOpts);
  window.addEventListener("wheel", markScrolling, scrollOpts);
  document.addEventListener("visibilitychange", () => {
    root.classList.toggle("pk-hidden", document.hidden);
  });

  /* ---------- Boot ---------- */
  applySafeArea();
  [50, 200, 600, 1200, 2500].forEach((ms) => setTimeout(applySafeArea, ms));
  window.addEventListener("resize", applySafeArea);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", applySafeArea);
  }
  if (tg && tg.onEvent) {
    ["safeAreaChanged", "contentSafeAreaChanged", "viewportChanged", "fullscreenChanged"].forEach((ev) => {
      try { tg.onEvent(ev, applySafeArea); } catch (_) {}
    });
  }
})();
