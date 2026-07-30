#!/usr/bin/env python3
"""v11 gold-standard Subscription / Mini App performance.

Preserves UI and functionality. Targets phone-wide jank causes:
- Twemoji SVG parsing/network on country names
- staleTime:0 + refetchOnMount always API storms
- canvas/RAF leftovers + class thrash
- GPU blur box-shadows
- unnecessary framer-motion side-effect parse on Subscription
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SRV = Path("/srv/cabinet")
DIST = Path("/root/cabinet-dist")

LOOP = r"""import{r}from"./vendor-react-B5aZ5G0Y.js";
/* v11 gold: low FPS, hard-pause on subscription / scroll, no classList */
const A=typeof window<"u"&&window.innerWidth<768;
const b=A?24:45;
const h=1e3/b;
function paused(){
  try{
    if(window.__paskodPauseAnim)return!0;
    const p=(location.pathname||"");
    if(p.indexOf("subscription")!==-1)return!0;
  }catch{}
  return!1;
}
function y(s,f){
  const a=r.useRef(s);a.current=s;
  r.useEffect(()=>{
    let e=0,n=0,d=document.hidden,t=!1,scrolling=!1,scrollTimer=0,poll=0;
    const busy=()=>d||t||scrolling||paused();
    const tick=l=>{
      if(busy()){e=0;return}
      const m=l-n;if(m>=h){n=l-m%h;try{a.current(l,m)}catch{}}
      e=requestAnimationFrame(tick);
    };
    const start=()=>{if(!e&&!busy()){n=performance.now();e=requestAnimationFrame(tick)}};
    const stop=()=>{if(e){cancelAnimationFrame(e);e=0}};
    const mark=()=>{
      if(!scrolling){scrolling=!0;stop()}
      if(scrollTimer)clearTimeout(scrollTimer);
      scrollTimer=setTimeout(()=>{scrolling=!1;start()},220);
    };
    const onVis=()=>{d=document.hidden;busy()?stop():start()};
    const o=window.Telegram&&window.Telegram.WebApp;
    document.addEventListener("visibilitychange",onVis);
    window.addEventListener("scroll",mark,{passive:!0,capture:!0});
    document.addEventListener("scroll",mark,{passive:!0,capture:!0});
    window.addEventListener("touchmove",mark,{passive:!0,capture:!0});
    window.addEventListener("wheel",mark,{passive:!0,capture:!0});
    o&&o.onEvent&&(o.onEvent("activated",()=>{t=!1;start()}),o.onEvent("deactivated",()=>{t=!0;stop()}));
    /* re-check pause flag (route changes) without class thrash */
    poll=window.setInterval(()=>{busy()?stop():start()},500);
    start();
    return()=>{
      stop();if(scrollTimer)clearTimeout(scrollTimer);if(poll)clearInterval(poll);
      document.removeEventListener("visibilitychange",onVis);
      window.removeEventListener("scroll",mark,!0);
      document.removeEventListener("scroll",mark,!0);
      window.removeEventListener("touchmove",mark,!0);
      window.removeEventListener("wheel",mark,!0);
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
  return Math.min(d,A?1:1.25);
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
      #boot.hide{opacity:0;pointer-events:none;transition:opacity .2s}
      @keyframes b{to{transform:rotate(360deg)}}
    </style>
    <script type="module" crossorigin src="/assets/v11/index-a89cf6b8.js"></script>
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-react-B5aZ5G0Y.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-query-qJWxXJpd.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-utils-rksnccus.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-telegram-BljK9Sw_.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-twemoji-D9HvikB8.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-i18n-BxdFJVG3.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-motion-yrvsEvRj.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-radix-CmBkAP5K.js">
    <link rel="modulepreload" crossorigin href="/assets/v11/vendor-cmdk-CTFjeOaw.js">
    <link rel="stylesheet" crossorigin href="/assets/v11/index-ffaf3eb8.css">
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
            setTimeout(function () { if (boot && boot.parentNode) boot.parentNode.removeChild(boot); }, 200);
            clearInterval(iv);
            return;
          }
          if (n > 48) {
            clearInterval(iv);
            try {
              var k = 'paskod_boot_retry_v11';
              if (!sessionStorage.getItem(k)) {
                sessionStorage.setItem(k, '1');
                var u = new URL(location.href);
                u.searchParams.set('_v', '11');
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
/* v11-gold */
html.route-subscription canvas,
html.route-subscription [style*="z-index: -2"],
html.route-subscription [style*="z-index:-2"]{
  display:none!important;
  visibility:hidden!important;
  pointer-events:none!important;
}
@media (max-width:1024px){
  canvas{contain:strict!important;will-change:auto!important}
  .backdrop-blur-linear,.backdrop-blur-sm,.backdrop-blur-md,.backdrop-blur-lg,.backdrop-blur-xl,
  .backdrop-blur,[class*="backdrop-blur"]{
    -webkit-backdrop-filter:none!important;backdrop-filter:none!important
  }
  nav.fixed.z-50{
    backdrop-filter:none!important;-webkit-backdrop-filter:none!important;
    background:rgba(10,15,26,.97)!important;
    transition:background-color .12s linear,border-color .12s linear!important;
  }
  html.route-subscription #root{
    content-visibility:visible;
    contain:layout style;
  }
  html.route-subscription .twemoji{
    display:none!important;
  }
}
"""


def kill_canvases_js() -> str:
    return (
        "try{window.__paskodPauseAnim=!0;"
        "document.documentElement.classList.add('route-subscription');"
        "document.querySelectorAll('canvas').forEach(function(c){"
        "try{c.width=0;c.height=0;c.remove()}catch(e){}});"
        "}catch(e){}"
    )


def main() -> None:
    src = SRV / "assets" / "v10"
    if not src.is_dir():
        src = SRV / "assets" / "v9"
    dst = SRV / "assets" / "v11"
    if not src.is_dir():
        print("missing source assets", file=sys.stderr)
        sys.exit(1)

    print("copy", src, "->", dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    for name in list(dst.glob("useAnimationLoop-*.js")):
        name.write_text(LOOP)
        print("loop", name.name)

    # --- index shell patches ---
    idx_path = dst / "index-a89cf6b8.js"
    idx = idx_path.read_text()

    # su(): do not enable background while on subscription routes
    old_su = (
        "function su(){l.useEffect(()=>(Oa.setState(e=>({count:e.count+1})),"
        "()=>Oa.setState(e=>({count:e.count-1}))),[])}"
    )
    new_su = (
        "function su(){const loc=Re();l.useEffect(()=>{"
        'if((loc.pathname||"").includes("subscription"))return;'
        "Oa.setState(e=>({count:e.count+1}));"
        "return()=>Oa.setState(e=>({count:e.count-1}))"
        "},[loc.pathname])}"
    )
    if old_su not in idx:
        # maybe already patched differently — try loose
        if "function su(){const loc=Re()" not in idx:
            raise SystemExit("su() pattern not found")
    else:
        idx = idx.replace(old_su, new_su, 1)
        print("patched su")

    # ru already may have subscription gate from v10
    if 'includes("subscription")?t.jsx(nu,{}):null}' not in idx and 'includes("subscription")' not in idx:
        old_ru = (
            "function ru(){const e=Oa(s=>s.count>0),[a,n]=l.useState(e);"
            "return l.useEffect(()=>{if(e){n(!0);return}const s=setTimeout(()=>n(!1),300);"
            "return()=>clearTimeout(s)},[e]),a?t.jsx(nu,{}):null}"
        )
        new_ru = (
            "function ru(){const e=Oa(s=>s.count>0),[a,n]=l.useState(e),loc=Re();"
            "return l.useEffect(()=>{if(e){n(!0);return}const s=setTimeout(()=>n(!1),300);"
            'return()=>clearTimeout(s)},[e]),a&&!(loc.pathname||"").includes("subscription")?t.jsx(nu,{}):null}'
        )
        if old_ru not in idx:
            raise SystemExit("ru() pattern not found")
        idx = idx.replace(old_ru, new_ru, 1)
        print("patched ru")
    else:
        print("ru gate present")

    # iu shell: sync pause flag + kill canvases on subscription routes
    needle = "Rd(),su();"
    if needle not in idx:
        raise SystemExit("Rd(),su(); not found")
    inject = (
        "Rd(),su();"
        "l.useEffect(()=>{"
        'const heavy=(n.pathname||"").includes("subscription");'
        "try{window.__paskodPauseAnim=!!heavy;"
        "document.documentElement.classList.toggle('route-subscription',!!heavy);"
        "if(heavy){document.querySelectorAll('canvas').forEach(function(c){"
        "try{c.width=0;c.height=0;c.remove()}catch(e){}})}}"
        "catch(e){}"
        "},[n.pathname]);"
    )
    if "route-subscription" not in idx:
        idx = idx.replace(needle, inject, 1)
        print("patched iu pause effect")
    else:
        print("iu pause already present")

    # Always reduce mobile BG settings if Vr still has reducedOnMobile gate
    idx = idx.replace(
        "i=e.reducedOnMobile&&r?au(e.settings):e.settings",
        "i=r?au(e.settings):e.settings",
    )
    # Remove translateZ on BG portal if present
    idx = idx.replace(
        'transform:"translateZ(0)"},children:t.jsx(l.Suspense',
        'contentVisibility:"auto"},children:t.jsx(l.Suspense',
    )
    idx = idx.replace("whileHover:{y:-4}", "whileHover:{}")

    if "v11-gold" not in idx:
        idx = idx.rstrip() + "\n\n/* v11-gold */\n"
    idx_path.write_text(idx)
    print("index", idx_path.stat().st_size)

    # --- Subscription chunk ---
    sub = dst / "Subscription-5825f3b4.js"
    su = sub.read_text()

    # Remove framer-motion side-effect import (huge parse cost on open)
    su2 = su.replace('import"./vendor-motion-yrvsEvRj.js";', "")
    su2 = su2.replace("import\"./vendor-motion-yrvsEvRj.js\";", "")

    # Twemoji SVG → native text (flags already via ge(); names stay readable)
    su2 = su2.replace(
        'e.jsx(ls,{options:{className:"twemoji",folder:"svg",ext:".svg"},children:g.name})',
        'e.jsx("span",{children:g.name})',
    )
    # Drop unused twemoji import if no longer referenced
    if "jsx(ls," not in su2 and "{T as ls}" in su2:
        su2 = su2.replace('import{T as ls}from"./vendor-twemoji-D9HvikB8.js";', "")
    elif "jsx(ls," not in su2 and 'from"./vendor-twemoji-D9HvikB8.js"' in su2:
        su2 = su2.replace('import{T as ls}from"./vendor-twemoji-D9HvikB8.js";', "")

    # Stop API storms
    su2 = su2.replace(
        'queryKey:["subscription",r],queryFn:()=>y.getSubscription(r),retry:!1,staleTime:0,refetchOnMount:"always"',
        'queryKey:["subscription",r],queryFn:()=>y.getSubscription(r),retry:!1,staleTime:3e4,refetchOnMount:!0',
    )
    su2 = su2.replace(
        'queryKey:["connection-link",r],queryFn:()=>y.getConnectionLink(r),retry:!1,staleTime:0',
        'queryKey:["connection-link",r],queryFn:()=>y.getConnectionLink(r),retry:!1,staleTime:3e4',
    )
    su2 = su2.replace(
        'queryKey:["purchase-options",r],queryFn:()=>y.getPurchaseOptions(r),staleTime:0,refetchOnMount:"always"',
        'queryKey:["purchase-options",r],queryFn:()=>y.getPurchaseOptions(r),staleTime:3e4,refetchOnMount:!0',
    )

    # Softer status glows: keep color accent, drop expensive blur
    su2 = su2.replace(
        "boxShadow:`0 0 8px ${N.mainHex}80`",
        "boxShadow:`0 0 0 1px ${N.mainHex}99`",
    )
    su2 = su2.replace(
        "boxShadow:`0 0 8px ${N.mainHex}40`",
        "boxShadow:`0 0 0 1px ${N.mainHex}55`",
    )

    # Countdown: 1s → 2s (still looks live, half the React commits)
    # Only the expiry countdown that rebuilds {days,hours,minutes,seconds}
    su2 = su2.replace(
        "f();const n=setInterval(f,1e3);return()=>clearInterval(n)},[b]);",
        "f();const n=setInterval(f,2e3);return()=>clearInterval(n)},[b]);",
    )

    # Cheaper transitions already partly done; reinforce
    su2 = su2.replace(
        "transition-[background-color,box-shadow] duration-150",
        "transition-colors duration-150",
    )
    su2 = su2.replace(
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] "',
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] [content-visibility:auto] "',
    )
    su2 = su2.replace(
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] py-12 text-center"',
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] [content-visibility:auto] py-12 text-center"',
    )
    su2 = su2.replace(
        'className:"max-h-64 space-y-2 overflow-y-auto"',
        'className:"max-h-64 space-y-2 overflow-y-auto [content-visibility:auto] [contain:content]"',
    )

    if "v11-gold" not in su2:
        su2 = su2.rstrip() + "\n/* v11-gold */\n"
    sub.write_text(su2)
    print("subscription", sub.stat().st_size)

    # --- stars: ultra-light when somehow still mounted ---
    for stars in dst.glob("shooting-stars-*.js"):
        st = stars.read_text()
        st = st.replace(
            "const t=Math.min(Math.floor(i*a*p),180);",
            'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?24:48);',
        )
        st = st.replace(
            'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?48:90);',
            'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?24:48);',
        )
        st = st.replace(
            'style:{transform:"translateZ(0)",willChange:"contents",contain:"strict"}',
            'style:{willChange:"auto",contain:"strict"}',
        )
        st = st.replace(
            'style:{transform:"translateZ(0)",willChange:"auto",contain:"strict"}',
            'style:{willChange:"auto",contain:"strict"}',
        )
        st = st.replace(
            "twinkleSpeed:Math.random()>.3?.5+Math.random()*.5:null",
            "twinkleSpeed:null",
        )
        st = st.replace(
            "twinkleSpeed:Math.random()>.85?.5+Math.random()*.5:null",
            "twinkleSpeed:null",
        )
        st = st.replace(
            "nextShootingDelay:4200+Math.random()*4500",
            "nextShootingDelay:12e3+Math.random()*12e3",
        )
        st = st.replace(
            "nextShootingDelay:7000+Math.random()*8000",
            "nextShootingDelay:12e3+Math.random()*12e3",
        )
        st = st.replace(
            "o.nextShootingDelay=4200+Math.random()*4500",
            "o.nextShootingDelay=12e3+Math.random()*12e3",
        )
        st = st.replace(
            "o.nextShootingDelay=7000+Math.random()*8000",
            "o.nextShootingDelay=12e3+Math.random()*12e3",
        )
        stars.write_text(st)
    print("stars tuned")

    # CSS
    css_path = dst / "index-ffaf3eb8.css"
    css = css_path.read_text()
    if "v11-gold" not in css:
        css_path.write_text(css.rstrip() + "\n" + CSS_EXTRA)
        print("css appended")

    (SRV / "index.html").write_text(INDEX_HTML)

    dist_assets = DIST / "assets"
    dist_assets.mkdir(parents=True, exist_ok=True)
    dist_v = dist_assets / "v11"
    if dist_v.exists():
        shutil.rmtree(dist_v)
    shutil.copytree(dst, dist_v)
    shutil.copy2(SRV / "index.html", DIST / "index.html")

    checks = [
        "/assets/v11/" in (SRV / "index.html").read_text(),
        "route-subscription" in idx_path.read_text(),
        "__paskodPauseAnim" in (dst / "useAnimationLoop-637b4af4.js").read_text()
        or "__paskodPauseAnim" in next(dst.glob("useAnimationLoop-*.js")).read_text(),
        'e.jsx("span",{children:g.name})' in sub.read_text(),
        "staleTime:3e4" in sub.read_text(),
        'import"./vendor-motion-yrvsEvRj.js"' not in sub.read_text(),
        "v11-gold" in sub.read_text(),
    ]
    print("checks", checks)
    if not all(checks):
        sys.exit(2)
    print("OK_DEPLOY_V11")


if __name__ == "__main__":
    main()
