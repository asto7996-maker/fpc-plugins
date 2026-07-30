#!/usr/bin/env python3
"""v14: stop phone-wide 30fps on Subscription.

Evidence (Telegram docs + Android Mini App reports):
- Continuous canvas RAF in Telegram WebView saturates shared GPU
- System UI (notification shade) drops to ~30fps with the Mini App
- Official guidance: minimize animations by Android performanceClass

v13 restored beauty AND left particle background running on Subscription.
That combination is what tanks the whole phone.

v14 keeps Subscription page beauty (cards/glows) but HARD-STOPS background
canvas on subscription routes, and scales home BG by performanceClass.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SRV = Path("/srv/cabinet")
DIST = Path("/root/cabinet-dist")

LOOP = r"""import{r}from"./vendor-react-B5aZ5G0Y.js";
/* v14: stop RAF completely when paused (subscription); skip paints while scrolling */
const A=typeof window<"u"&&window.innerWidth<768;
const _rr=typeof window<"u"&&window.screen&&Number(window.screen.refreshRate)||0;
const b=A?60:Math.min(Math.max(_rr||90,60),120);
const h=1e3/b;
function hardPaused(){
  try{
    if(window.__paskodPauseAnim)return!0;
    if(window.__paskodDisableBg)return!0;
    const p=location.pathname||"";
    if(p.indexOf("subscription")!==-1)return!0;
  }catch{}
  return!1;
}
function y(s,f){
  const a=r.useRef(s);a.current=s;
  r.useEffect(()=>{
    let e=0,n=0,d=document.hidden,t=!1,scrolling=!1,scrollTimer=0,poll=0;
    const tick=l=>{
      if(hardPaused()||d||t){e=0;return}
      e=requestAnimationFrame(tick);
      if(scrolling)return;
      const m=l-n;
      if(m>=h){n=l-m%h;try{a.current(l,m)}catch{}}
    };
    const start=()=>{if(!e&&!hardPaused()&&!d&&!t){n=performance.now();e=requestAnimationFrame(tick)}};
    const stop=()=>{if(e){cancelAnimationFrame(e);e=0}};
    const markScroll=()=>{
      scrolling=!0;
      if(scrollTimer)clearTimeout(scrollTimer);
      scrollTimer=setTimeout(()=>{scrolling=!1},140);
    };
    const onVis=()=>{d=document.hidden;d||hardPaused()?stop():start()};
    const o=window.Telegram&&window.Telegram.WebApp;
    const onAct=()=>{t=!1;start()};
    const onDeact=()=>{t=!0;stop()};
    document.addEventListener("visibilitychange",onVis);
    window.addEventListener("scroll",markScroll,{passive:!0,capture:!0});
    document.addEventListener("scroll",markScroll,{passive:!0,capture:!0});
    window.addEventListener("wheel",markScroll,{passive:!0,capture:!0});
    o&&o.onEvent&&(o.onEvent("activated",onAct),o.onEvent("deactivated",onDeact));
    /* react to route pause without touchmove thrash */
    poll=window.setInterval(()=>{hardPaused()||d||t?stop():start()},500);
    start();
    return()=>{
      stop();
      if(scrollTimer)clearTimeout(scrollTimer);
      if(poll)clearInterval(poll);
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
  return Math.min(d,A?1.15:1.35);
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
        try {
          var ua = navigator.userAgent || '';
          var m = ua.match(/Telegram-Android\/[\d.]+ \([^)]*;\s*(LOW|AVERAGE|HIGH)\)/i);
          var cls = m ? m[1].toUpperCase() : '';
          window.__paskodPerfClass = cls || '';
          /* Official Telegram guidance: minimize effects on weaker Android classes.
             LOW → no particle BG. AVERAGE → reduced. HIGH → full. */
          if (cls === 'LOW') window.__paskodDisableBg = true;
        } catch (e) {}
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
      /* While on subscription: never composite particle canvases (GPU shared with system UI) */
      html.route-subscription canvas{display:none!important;width:0!important;height:0!important}
    </style>
    <script type="module" crossorigin src="/assets/v14/index-a89cf6b8.js"></script>
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-react-B5aZ5G0Y.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-query-qJWxXJpd.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-utils-rksnccus.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-telegram-BljK9Sw_.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-twemoji-D9HvikB8.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-i18n-BxdFJVG3.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-motion-yrvsEvRj.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-radix-CmBkAP5K.js">
    <link rel="modulepreload" crossorigin href="/assets/v14/vendor-cmdk-CTFjeOaw.js">
    <link rel="stylesheet" crossorigin href="/assets/v14/index-ffaf3eb8.css">
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
              var k = 'paskod_boot_retry_v14';
              if (!sessionStorage.getItem(k)) {
                sessionStorage.setItem(k, '1');
                var u = new URL(location.href);
                u.searchParams.set('_v', '14');
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

CSS_EXTRA = """
/* v14-gpu-guard */
#root{transform:none!important;-webkit-transform:none!important}
#cabinet-bottom-nav,nav.fixed.z-50{
  position:fixed!important;left:16px!important;right:16px!important;
  bottom:calc(16px + env(safe-area-inset-bottom,0px))!important;
  top:auto!important;z-index:2147483000!important;
  transform:none!important;-webkit-transform:none!important
}
html.route-subscription canvas,
html.route-subscription [style*="z-index: -2"],
html.route-subscription [style*="z-index:-2"]{
  display:none!important;visibility:hidden!important;pointer-events:none!important;
  width:0!important;height:0!important
}
"""


def main() -> None:
    src = SRV / "assets" / "v13"
    dst = SRV / "assets" / "v14"
    if not src.is_dir():
        raise SystemExit("v13 missing")

    print("copy v13 -> v14")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    for p in dst.glob("useAnimationLoop-*.js"):
        p.write_text(LOOP)
        print("loop", p.name)

    idx_path = dst / "index-a89cf6b8.js"
    idx = idx_path.read_text()

    # su: don't enable BG counter on subscription routes
    old_su = (
        "function su(){l.useEffect(()=>(Oa.setState(e=>({count:e.count+1})),"
        "()=>Oa.setState(e=>({count:e.count-1}))),[])}"
    )
    new_su = (
        "function su(){const loc=Re();l.useEffect(()=>{"
        'const heavy=(loc.pathname||"").includes("subscription")||window.__paskodDisableBg;'
        "if(heavy)return;"
        "Oa.setState(e=>({count:e.count+1}));"
        "return()=>Oa.setState(e=>({count:e.count-1}))"
        "},[loc.pathname])}"
    )
    if old_su not in idx:
        raise SystemExit("su pattern missing")
    idx = idx.replace(old_su, new_su, 1)
    print("patched su")

    old_ru = (
        "function ru(){const e=Oa(s=>s.count>0),[a,n]=l.useState(e);"
        "return l.useEffect(()=>{if(e){n(!0);return}const s=setTimeout(()=>n(!1),300);"
        "return()=>clearTimeout(s)},[e]),a?t.jsx(nu,{}):null}"
    )
    new_ru = (
        "function ru(){const e=Oa(s=>s.count>0),[a,n]=l.useState(e),loc=Re();"
        "return l.useEffect(()=>{if(e){n(!0);return}const s=setTimeout(()=>n(!1),300);"
        "return()=>clearTimeout(s)},[e]),"
        'a&&!(loc.pathname||"").includes("subscription")&&!window.__paskodDisableBg'
        "?t.jsx(nu,{}):null}"
    )
    if old_ru not in idx:
        raise SystemExit("ru pattern missing")
    idx = idx.replace(old_ru, new_ru, 1)
    print("patched ru")

    # iu: hard pause + destroy canvases on subscription
    needle = "Rd(),su();"
    if needle not in idx:
        raise SystemExit("Rd(),su(); missing")
    if "route-subscription" not in idx:
        inject = (
            "Rd(),su();"
            "l.useEffect(()=>{"
            'const heavy=(n.pathname||"").includes("subscription");'
            "try{"
            "window.__paskodPauseAnim=!!heavy;"
            "document.documentElement.classList.toggle('route-subscription',!!heavy);"
            "if(heavy){"
            "document.querySelectorAll('canvas').forEach(function(c){"
            "try{const x=c.getContext('2d');x&&x.clearRect(0,0,c.width,c.height);"
            "c.width=0;c.height=0;c.remove()}catch(e){}});"
            "}"
            "}catch(e){}"
            "},[n.pathname]);"
        )
        idx = idx.replace(needle, inject, 1)
        print("patched iu pause/destroy")
    else:
        print("iu pause already present")

    # Vr: respect disable flag + force reduce on AVERAGE Android
    old_vr_head = (
        "function Vr({config:e}){const a=l.useMemo(()=>window.matchMedia(\"(prefers-reduced-motion: reduce)\").matches,[]);"
        "if(!e.enabled||e.type===\"none\"||a)return null;"
    )
    new_vr_head = (
        "function Vr({config:e}){const a=l.useMemo(()=>window.matchMedia(\"(prefers-reduced-motion: reduce)\").matches,[]);"
        "if(!e.enabled||e.type===\"none\"||a||window.__paskodDisableBg||window.__paskodPauseAnim)return null;"
    )
    if old_vr_head not in idx:
        raise SystemExit("Vr head missing")
    idx = idx.replace(old_vr_head, new_vr_head, 1)

    # Force reduced settings on mobile OR AVERAGE perf class (keep look, fewer particles)
    idx = idx.replace(
        "const r=window.innerWidth<768,i=e.reducedOnMobile&&r?au(e.settings):e.settings,u=0;",
        'const r=window.innerWidth<768||window.__paskodPerfClass==="AVERAGE"||window.__paskodPerfClass==="LOW",'
        "i=r?au(e.settings):e.settings,u=0;",
    )
    print("patched Vr")

    if "v14-gpu-guard" not in idx:
        idx = idx.rstrip() + "\n\n/* v14-gpu-guard */\n"
    idx_path.write_text(idx)

    # Subscription: keep beauty (glows). Only ensure no content-visibility.
    sub = dst / "Subscription-5825f3b4.js"
    su = sub.read_text()
    su = su.replace("[content-visibility:auto]", "")
    # Soften ONLY the continuous GPU-ish blur glows slightly but keep colored accents
    # Keep 0 0 8px — user wants beauty. Static shadows don't cause 30fps system-wide.
    if "v14-gpu-guard" not in su:
        su = su.rstrip() + "\n/* v14-gpu-guard */\n"
    sub.write_text(su)
    print("sub glows", su.count("0 0 8px"))

    css_path = dst / "index-ffaf3eb8.css"
    css = css_path.read_text()
    # Keep v13 pin if present; append v14 guard
    if "v14-gpu-guard" not in css:
        css_path.write_text(css.rstrip() + "\n" + CSS_EXTRA)

    (SRV / "index.html").write_text(INDEX_HTML)

    dist_v = DIST / "assets" / "v14"
    if dist_v.exists():
        shutil.rmtree(dist_v)
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copytree(dst, dist_v)
    shutil.copy2(SRV / "index.html", DIST / "index.html")

    loop = (dst / "useAnimationLoop-637b4af4.js").read_text()
    checks = [
        "/assets/v14/" in (SRV / "index.html").read_text(),
        'includes("subscription")' in idx_path.read_text(),
        "__paskodPauseAnim" in idx_path.read_text(),
        "__paskodDisableBg" in idx_path.read_text(),
        "hardPaused" in loop,
        'addEventListener("touchmove"' not in loop,
        "0 0 8px" in sub.read_text(),
        "route-subscription" in (SRV / "index.html").read_text(),
        "__paskodPerfClass" in (SRV / "index.html").read_text(),
    ]
    print("checks", checks)
    if not all(checks):
        sys.exit(2)
    print("OK_DEPLOY_V14")


if __name__ == "__main__":
    main()
