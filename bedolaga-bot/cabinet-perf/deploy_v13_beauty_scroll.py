#!/usr/bin/env python3
"""v13: restore beautiful visuals; fix REAL scroll jank.

Root cause of laggy scroll: animation loops cancelled RAF on every touchmove
(and toggled html classList / polled pause). That runs JS on every finger move
and fights the compositor.

Fix: keep RAF chain alive; only skip canvas paints while scrolling; listen to
scroll only (not touchmove). Restore v7 visuals + keep pinned nav portal.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SRV = Path("/srv/cabinet")
DIST = Path("/root/cabinet-dist")

# Gold loop: beautiful high FPS when idle; zero main-thread thrash while scrolling
LOOP = r"""import{r}from"./vendor-react-B5aZ5G0Y.js";
/* v13 gold scroll: keep RAF alive, skip paints while scrolling, no touchmove, no classList */
const A=typeof window<"u"&&window.innerWidth<768;
const _rr=typeof window<"u"&&window.screen&&Number(window.screen.refreshRate)||0;
const b=A?60:Math.min(Math.max(_rr||90,60),120);
const h=1e3/b;
function y(s,f){
  const a=r.useRef(s);a.current=s;
  r.useEffect(()=>{
    let e=0,n=0,d=document.hidden,t=!1,scrolling=!1,scrollTimer=0;
    const tick=l=>{
      e=requestAnimationFrame(tick);
      if(d||t||scrolling)return;
      const m=l-n;
      if(m>=h){n=l-m%h;try{a.current(l,m)}catch{}}
    };
    const start=()=>{if(!e){n=performance.now();e=requestAnimationFrame(tick)}};
    const stop=()=>{if(e){cancelAnimationFrame(e);e=0}};
    /* ONLY set a flag — never cancel RAF on every move (that was the scroll jank) */
    const markScroll=()=>{
      scrolling=!0;
      if(scrollTimer)clearTimeout(scrollTimer);
      scrollTimer=setTimeout(()=>{scrolling=!1},140);
    };
    const onVis=()=>{d=document.hidden;d?stop():start()};
    const o=window.Telegram&&window.Telegram.WebApp;
    const onAct=()=>{t=!1;start()};
    const onDeact=()=>{t=!0;stop()};
    document.addEventListener("visibilitychange",onVis);
    /* scroll only — do NOT listen to touchmove (fires every finger pixel) */
    window.addEventListener("scroll",markScroll,{passive:!0,capture:!0});
    document.addEventListener("scroll",markScroll,{passive:!0,capture:!0});
    window.addEventListener("wheel",markScroll,{passive:!0,capture:!0});
    o&&o.onEvent&&(o.onEvent("activated",onAct),o.onEvent("deactivated",onDeact));
    start();
    return()=>{
      stop();
      if(scrollTimer)clearTimeout(scrollTimer);
      document.removeEventListener("visibilitychange",onVis);
      window.removeEventListener("scroll",markScroll,!0);
      document.removeEventListener("scroll",markScroll,!0);
      window.removeEventListener("wheel",markScroll,!0);
      o&&o.offEvent&&(o.offEvent("activated",onAct),o.offEvent("deactivated",onDeact));
    };
  },f);
}
function T(){
  const[s,f]=r.useState(()=>typeof document<"u"?document.hidden:!1);
  return r.useEffect(()=>{
    const d=()=>f(document.hidden);
    document.addEventListener("visibilitychange",d);
    return()=>document.removeEventListener("visibilitychange",d);
  },[]),s;
}
function R(){
  const d=(typeof devicePixelRatio<"u"&&devicePixelRatio)||1;
  return Math.min(d,A?1.25:1.5);
}
export{y as a,R as g,T as u};
"""

INDEX_HTML = r"""<!doctype html>
<html lang="ru" class="dark">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover" />
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate" />
    <meta http-equiv="Pragma" content="no-cache" />
    <meta http-equiv="Expires" content="0" />
    <meta name="theme-color" content="#0a0f1a" />
    <meta name="mobile-web-app-capable" content="yes" />
    <title>VPN</title>
    <script>
      (function () {
        var isTelegram = window.TelegramWebviewProxy || location.hash.indexOf('tgWebApp') !== -1 || location.search.indexOf('tgWebApp') !== -1;
        if (isTelegram) {
          var s = document.createElement('script');
          s.src = 'https://telegram.org/js/telegram-web-app.js';
          s.async = true;
          document.head.appendChild(s);
        }
      })();
    </script>
    <style>
      html,body{background:#0a0f1a;margin:0;color:#e8eef7}
      #root{min-height:100vh}
      #boot{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;background:#0a0f1a;z-index:99999;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
      #boot .spin{width:28px;height:28px;border:3px solid rgba(255,255,255,.15);border-top-color:#5b9fff;border-radius:50%;animation:b .8s linear infinite}
      #boot p{margin:0;font-size:14px;opacity:.85}
      #boot.hide{opacity:0;pointer-events:none;transition:opacity .25s}
      @keyframes b{to{transform:rotate(360deg)}}
    </style>
    <script type="module" crossorigin src="/assets/v13/index-a89cf6b8.js"></script>
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-react-B5aZ5G0Y.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-query-qJWxXJpd.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-utils-rksnccus.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-telegram-BljK9Sw_.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-twemoji-D9HvikB8.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-i18n-BxdFJVG3.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-motion-yrvsEvRj.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-radix-CmBkAP5K.js">
    <link rel="modulepreload" crossorigin href="/assets/v13/vendor-cmdk-CTFjeOaw.js">
    <link rel="stylesheet" crossorigin href="/assets/v13/index-ffaf3eb8.css">
  </head>
  <body>
    <div id="boot" aria-live="polite"><div class="spin"></div><p>Загрузка…</p></div>
    <div id="root"></div>
    <script>
      (function () {
        var boot = document.getElementById('boot');
        var root = document.getElementById('root');
        var n = 0;
        var iv = setInterval(function () {
          n++;
          if (root && root.childElementCount > 0) {
            boot.classList.add('hide');
            setTimeout(function () { if (boot && boot.parentNode) boot.parentNode.removeChild(boot); }, 250);
            clearInterval(iv);
            return;
          }
          if (n > 48) {
            clearInterval(iv);
            try {
              var k = 'paskod_boot_retry_v13';
              if (!sessionStorage.getItem(k)) {
                sessionStorage.setItem(k, '1');
                var u = new URL(location.href);
                u.searchParams.set('_v', '13');
                location.replace(u.toString());
              } else if (boot) {
                boot.innerHTML = '<p style="max-width:260px;text-align:center;line-height:1.4">Не удалось загрузить. Полностью закройте мини-приложение и откройте снова.</p>';
              }
            } catch (e) {
              if (boot) boot.innerHTML = '<p>Не удалось загрузить</p>';
            }
          }
        }, 250);
      })();
    </script>
  </body>
</html>
"""

# Minimal CSS: pin nav only. Do NOT strip blurs/glows/filters from the design.
CSS_PIN = """
/* v13-beauty-scroll: pin nav without killing visuals */
#root{
  transform:none!important;
  -webkit-transform:none!important;
}
#cabinet-bottom-nav,
nav.fixed.z-50{
  position:fixed!important;
  left:16px!important;
  right:16px!important;
  bottom:calc(16px + env(safe-area-inset-bottom, 0px))!important;
  top:auto!important;
  z-index:2147483000!important;
  transform:none!important;
  -webkit-transform:none!important;
}
"""


def main() -> None:
    src = SRV / "assets" / "v7"
    dst = SRV / "assets" / "v13"
    if not src.is_dir():
        raise SystemExit("v7 missing — needed as beauty baseline")

    print("copy v7 (beauty baseline) -> v13")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    # 1) Gold animation loops everywhere
    for p in dst.glob("useAnimationLoop-*.js"):
        p.write_text(LOOP)
        print("loop", p.name)

    # 2) Index: pin nav via portal; do NOT kill backgrounds on subscription
    idx_path = dst / "index-a89cf6b8.js"
    idx = idx_path.read_text()

    old_nav = (
        'return t.jsx("nav",{className:g("fixed z-50 transition-colors duration-200 lg:hidden",'
        '"bg-dark-900/95 backdrop-blur-linear","border border-dark-700/30",'
        'e?"pointer-events-none opacity-0":"opacity-100"),'
        'style:{bottom:"calc(16px + env(safe-area-inset-bottom, 0px))",left:"16px",right:"16px",'
        'borderRadius:"var(--bento-radius, 24px)",padding:"8px 4px",'
        'boxShadow:"0 4px 30px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(255, 255, 255, 0.05) inset"},'
        'children:t.jsx("div",{className:"flex justify-around",children:h.map(c=>t.jsxs(ae,{'
        "to:c.path,onClick:o,className:g(\"relative flex min-w-[56px] flex-1 shrink-0 flex-col "
        'items-center justify-center rounded-2xl px-3 py-2.5 transition-all duration-200",'
        'u(c.path)?"text-accent-400":"text-dark-400 hover:text-dark-200"),'
        'children:[u(c.path)&&t.jsx("div",{className:"absolute inset-0 rounded-2xl bg-accent-500/15"}),'
        't.jsx(c.icon,{className:"relative z-10 h-5 w-5"}),'
        't.jsx("span",{className:"relative z-10 mt-1 whitespace-nowrap text-2xs",children:c.label})'
        "]},c.path))})})}"
    )
    new_nav = (
        'return ea.createPortal(t.jsx("nav",{id:"cabinet-bottom-nav",'
        'className:g("fixed z-50 transition-colors duration-200 lg:hidden",'
        '"bg-dark-900/95 backdrop-blur-linear","border border-dark-700/30",'
        'e?"pointer-events-none opacity-0":"opacity-100"),'
        'style:{position:"fixed",bottom:"calc(16px + env(safe-area-inset-bottom, 0px))",'
        'left:"16px",right:"16px",borderRadius:"var(--bento-radius, 24px)",padding:"8px 4px",'
        "zIndex:2147483000,transform:\"none\","
        'boxShadow:"0 4px 30px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(255, 255, 255, 0.05) inset"},'
        'children:t.jsx("div",{className:"flex justify-around",children:h.map(c=>t.jsxs(ae,{'
        "to:c.path,onClick:o,className:g(\"relative flex min-w-[56px] flex-1 shrink-0 flex-col "
        'items-center justify-center rounded-2xl px-3 py-2.5 transition-colors duration-200",'
        'u(c.path)?"text-accent-400":"text-dark-400 hover:text-dark-200"),'
        'children:[u(c.path)&&t.jsx("div",{className:"absolute inset-0 rounded-2xl bg-accent-500/15"}),'
        't.jsx(c.icon,{className:"relative z-10 h-5 w-5"}),'
        't.jsx("span",{className:"relative z-10 mt-1 whitespace-nowrap text-2xs",children:c.label})'
        "]},c.path))})}),document.body)}"
    )
    if old_nav not in idx:
        i = idx.find('return t.jsx("nav",{className:g("fixed z-50')
        print("NAV MISSING", repr(idx[i : i + 180]) if i >= 0 else None)
        raise SystemExit("nav pattern not found on v7 baseline")
    idx = idx.replace(old_nav, new_nav, 1)
    print("nav portaled (beauty shadow kept)")

    # Ensure #root transform none is not fighting — already in CSS
    if "v13-beauty-scroll" not in idx:
        idx = idx.rstrip() + "\n\n/* v13-beauty-scroll */\n"
    idx_path.write_text(idx)

    # 3) Subscription: keep v7 beauty; only avoid Twemoji SVG network storm (flags already native via ge())
    sub = dst / "Subscription-5825f3b4.js"
    su = sub.read_text()
    # Twemoji SVG on country names = dozens of network+decode ops → scroll/open freezes.
    # Keep beautiful native text; flags still via ge(country_code).
    if 'folder:"svg"' in su:
        su = su.replace(
            'e.jsx(ls,{options:{className:"twemoji",folder:"svg",ext:".svg"},children:g.name})',
            'e.jsx("span",{children:g.name})',
        )
        print("sub: twemoji SVG -> native text (flags unchanged)")
    # Mild cache only (not visual): avoid refetch storm without changing UI
    su = su.replace(
        'staleTime:0,refetchOnMount:"always"',
        "staleTime:3e4,refetchOnMount:!0",
    )
    su = su.replace(
        'queryKey:["connection-link",r],queryFn:()=>y.getConnectionLink(r),retry:!1,staleTime:0',
        'queryKey:["connection-link",r],queryFn:()=>y.getConnectionLink(r),retry:!1,staleTime:3e4',
    )
    if "v13-beauty-scroll" not in su:
        su = su.rstrip() + "\n/* v13-beauty-scroll */\n"
    sub.write_text(su)
    print("sub glows", su.count("0 0 8px"), "motion import", "vendor-motion" in su)

    # 4) CSS = pure v7 + pin only (strip any previous kill-beauty appendages by using v7 file)
    css_path = dst / "index-ffaf3eb8.css"
    css = css_path.read_text()
    # Remove leftover markers if any copied (v7 shouldn't have them)
    if "v13-beauty-scroll" not in css:
        css_path.write_text(css.rstrip() + "\n" + CSS_PIN)
    print("css beauty+pin", css_path.stat().st_size)

    # 5) Ensure shooting-stars / starfield use loops (already import useAnimationLoop-*)
    # Restore original starfield from v7 already copied — good.

    (SRV / "index.html").write_text(INDEX_HTML)

    dist_v = DIST / "assets" / "v13"
    if dist_v.exists():
        shutil.rmtree(dist_v)
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copytree(dst, dist_v)
    shutil.copy2(SRV / "index.html", DIST / "index.html")

    loop_txt = (dst / "useAnimationLoop-637b4af4.js").read_text()
    checks = [
        "/assets/v13/" in (SRV / "index.html").read_text(),
        'id:"cabinet-bottom-nav"' in idx_path.read_text(),
        "touchmove" not in loop_txt,
        "classList" not in loop_txt,
        "requestAnimationFrame(tick)" in loop_txt,
        "0 0 8px" in sub.read_text(),
        "content-visibility" not in sub.read_text(),
        "v13-beauty-scroll" in css_path.read_text(),
        "#cabinet-bottom-nav" in css_path.read_text(),
        "backdrop-filter:none!important" not in css_path.read_text().split("v13-beauty-scroll")[-1],
    ]
    print("checks", checks)
    if not all(checks):
        sys.exit(2)
    print("OK_DEPLOY_V13")


if __name__ == "__main__":
    main()
