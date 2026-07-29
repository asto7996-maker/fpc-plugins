#!/usr/bin/env python3
"""Deploy v10: fix Subscription / mini-app lag from background canvas + heavy paint."""
from pathlib import Path
import shutil
import subprocess
import sys

SRV = Path("/srv/cabinet")
DIST = Path("/root/cabinet-dist")

LOOP = r"""import{r}from"./vendor-react-B5aZ5G0Y.js";
/* v10: bg anim capped; pause on scroll/touch; no html class thrash */
const A=typeof window<"u"&&window.innerWidth<768;
const b=A?36:60;
const h=1e3/b;
function y(s,f){
  const a=r.useRef(s);a.current=s;
  r.useEffect(()=>{
    let e=0,n=0,d=document.hidden,t=!1,scrolling=!1,scrollTimer=0;
    const busy=()=>d||t||scrolling;
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
      scrollTimer=setTimeout(()=>{scrolling=!1;start()},180);
    };
    const onVis=()=>{d=document.hidden;busy()?stop():start()};
    const o=window.Telegram&&window.Telegram.WebApp;
    document.addEventListener("visibilitychange",onVis);
    window.addEventListener("scroll",mark,{passive:!0,capture:!0});
    document.addEventListener("scroll",mark,{passive:!0,capture:!0});
    window.addEventListener("touchmove",mark,{passive:!0,capture:!0});
    window.addEventListener("wheel",mark,{passive:!0,capture:!0});
    o&&o.onEvent&&(o.onEvent("activated",()=>{t=!1;start()}),o.onEvent("deactivated",()=>{t=!0;stop()}));
    start();
    return()=>{
      stop();if(scrollTimer)clearTimeout(scrollTimer);
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
    <script type="module" crossorigin src="/assets/v10/index-a89cf6b8.js"></script>
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-react-B5aZ5G0Y.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-query-qJWxXJpd.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-utils-rksnccus.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-telegram-BljK9Sw_.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-twemoji-D9HvikB8.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-i18n-BxdFJVG3.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-motion-yrvsEvRj.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-radix-CmBkAP5K.js">
    <link rel="modulepreload" crossorigin href="/assets/v10/vendor-cmdk-CTFjeOaw.js">
    <link rel="stylesheet" crossorigin href="/assets/v10/index-ffaf3eb8.css">
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
              var k = 'paskod_boot_retry_v10';
              if (!sessionStorage.getItem(k)) {
                sessionStorage.setItem(k, '1');
                var u = new URL(location.href);
                u.searchParams.set('_v', '10');
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
/* v10-sub-smooth */
@media (max-width:1024px){
  canvas{contain:strict!important;will-change:auto!important}
  html.is-scrolling canvas,body.is-scrolling canvas{visibility:hidden!important}
  .backdrop-blur-linear,.backdrop-blur-sm,.backdrop-blur-md,.backdrop-blur-lg,.backdrop-blur-xl{
    -webkit-backdrop-filter:none!important;backdrop-filter:none!important
  }
  nav.fixed.z-50{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;transition:background-color .15s ease,border-color .15s ease!important}
}
"""


def main() -> None:
    src = SRV / "assets" / "v9"
    dst = SRV / "assets" / "v10"
    if not src.is_dir():
        print("missing v9", file=sys.stderr)
        sys.exit(1)

    print("copy v9 -> v10")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    for name in [
        "useAnimationLoop-637b4af4.js",
        "useAnimationLoop-54062fb8.js",
        "useAnimationLoop-d49c1e0c.js",
        "useAnimationLoop-cee13a82.js",
        "useAnimationLoop-d989b507.js",
        "useAnimationLoop-D5qBbcNF.js",
        "useAnimationLoop-5634d1db.js",
    ]:
        p = dst / name
        if p.exists():
            p.write_text(LOOP)
            print("loop", name)

    idx_path = dst / "index-a89cf6b8.js"
    idx = idx_path.read_text()

    old_ru = (
        'function ru(){const e=Oa(s=>s.count>0),[a,n]=l.useState(e);'
        "return l.useEffect(()=>{if(e){n(!0);return}const s=setTimeout(()=>n(!1),300);"
        "return()=>clearTimeout(s)},[e]),a?t.jsx(nu,{}):null}"
    )
    new_ru = (
        'function ru(){const e=Oa(s=>s.count>0),[a,n]=l.useState(e),loc=Re();'
        "return l.useEffect(()=>{if(e){n(!0);return}const s=setTimeout(()=>n(!1),300);"
        'return()=>clearTimeout(s)},[e]),a&&!(loc.pathname||"").includes("subscription")?t.jsx(nu,{}):null}'
    )
    if old_ru not in idx:
        raise SystemExit("ru() pattern not found")
    idx = idx.replace(old_ru, new_ru, 1)
    print("patched ru")

    old_vr = (
        "const r=window.innerWidth<768,i=e.reducedOnMobile&&r?au(e.settings):e.settings,u=0;"
        'return ea.createPortal(t.jsx("div",{className:"pointer-events-none fixed inset-0",'
        "style:{zIndex:-2,opacity:e.opacity,contain:\"strict\",backfaceVisibility:\"hidden\","
        'transform:"translateZ(0)"},children:t.jsx(l.Suspense,{fallback:null,children:t.jsx(s,{settings:i})})}),document.body)}'
    )
    new_vr = (
        "const r=window.innerWidth<768,i=r?au(e.settings):e.settings,u=0;"
        'return ea.createPortal(t.jsx("div",{className:"pointer-events-none fixed inset-0",'
        "style:{zIndex:-2,opacity:r?Math.min(e.opacity||1,.55):e.opacity,contain:\"strict\","
        'backfaceVisibility:"hidden",contentVisibility:"auto"},'
        "children:t.jsx(l.Suspense,{fallback:null,children:t.jsx(s,{settings:i})})}),document.body)}"
    )
    if old_vr not in idx:
        raise SystemExit("Vr pattern not found")
    idx = idx.replace(old_vr, new_vr, 1)
    print("patched Vr")

    idx = idx.replace("whileHover:{y:-4}", "whileHover:{}")
    if "v10-sub-smooth" not in idx:
        idx = idx.rstrip() + "\n\n/* v10-sub-smooth */\n"
    idx_path.write_text(idx)
    print("index", idx_path.stat().st_size)

    stars = dst / "shooting-stars-aa86c8bb.js"
    st = stars.read_text()
    st = st.replace(
        "const t=Math.min(Math.floor(i*a*p),180);",
        'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?48:90);',
    )
    st = st.replace(
        'style:{transform:"translateZ(0)",willChange:"contents",contain:"strict"}',
        'style:{transform:"translateZ(0)",willChange:"auto",contain:"strict"}',
    )
    st = st.replace(
        "twinkleSpeed:Math.random()>.3?.5+Math.random()*.5:null",
        "twinkleSpeed:Math.random()>.85?.5+Math.random()*.5:null",
    )
    st = st.replace(
        "nextShootingDelay:4200+Math.random()*4500",
        "nextShootingDelay:7000+Math.random()*8000",
    )
    st = st.replace(
        "o.nextShootingDelay=4200+Math.random()*4500",
        "o.nextShootingDelay=7000+Math.random()*8000",
    )
    stars.write_text(st)
    for alt in dst.glob("shooting-stars-*.js"):
        if alt.name == stars.name:
            continue
        t = alt.read_text()
        if "Math.min(Math.floor(i*a*p),180)" in t or "willChange:\"contents\"" in t:
            alt.write_text(st)
            print("stars", alt.name)
    print("stars main ok")

    sub = dst / "Subscription-5825f3b4.js"
    su = sub.read_text()
    su = su.replace(
        "transition-[background-color,box-shadow] duration-150",
        "transition-colors duration-150",
    )
    su = su.replace(
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] "',
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] [content-visibility:auto] "',
    )
    su = su.replace(
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] py-12 text-center"',
        'className:"relative overflow-hidden rounded-3xl [contain:layout_paint] [content-visibility:auto] py-12 text-center"',
    )
    if "v10-sub-smooth" not in su:
        su = su.rstrip() + "\n/* v10-sub-smooth */\n"
    sub.write_text(su)
    print("subscription", sub.stat().st_size)

    css_path = dst / "index-ffaf3eb8.css"
    css = css_path.read_text()
    if "v10-sub-smooth" not in css:
        css_path.write_text(css.rstrip() + "\n" + CSS_EXTRA)
        print("css appended")

    (SRV / "index.html").write_text(INDEX_HTML)
    print("index.html written")

    dist_assets = DIST / "assets"
    dist_assets.mkdir(parents=True, exist_ok=True)
    dist_v10 = dist_assets / "v10"
    if dist_v10.exists():
        shutil.rmtree(dist_v10)
    shutil.copytree(dst, dist_v10)
    shutil.copy2(SRV / "index.html", DIST / "index.html")

    checks = [
        ('/assets/v10/index-a89cf6b8.js' in (SRV / "index.html").read_text()),
        ("includes(\"subscription\")" in idx_path.read_text()),
        ("A?36:60" in (dst / "useAnimationLoop-637b4af4.js").read_text()),
        ("A?36:60" in (dst / "useAnimationLoop-54062fb8.js").read_text()),
        ("?48:90" in stars.read_text()),
        ("v10-sub-smooth" in sub.read_text()),
    ]
    if not all(checks):
        print("VERIFY FAIL", checks, file=sys.stderr)
        sys.exit(2)
    print("OK_DEPLOY_V10")


if __name__ == "__main__":
    main()
