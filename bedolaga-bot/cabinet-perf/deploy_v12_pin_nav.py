#!/usr/bin/env python3
"""v12: pin bottom nav via body portal + deeper Subscription/home perf."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SRV = Path("/srv/cabinet")
DIST = Path("/root/cabinet-dist")

LOOP = r"""import{r}from"./vendor-react-B5aZ5G0Y.js";
/* v12: pause on subscription/scroll; low FPS; no classList */
const A=typeof window<"u"&&window.innerWidth<768;
const b=A?20:40;
const h=1e3/b;
function paused(){
  try{
    if(window.__paskodPauseAnim)return!0;
    if((location.pathname||"").indexOf("subscription")!==-1)return!0;
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
      scrollTimer=setTimeout(()=>{scrolling=!1;start()},250);
    };
    const onVis=()=>{d=document.hidden;busy()?stop():start()};
    const o=window.Telegram&&window.Telegram.WebApp;
    document.addEventListener("visibilitychange",onVis);
    window.addEventListener("scroll",mark,{passive:!0,capture:!0});
    document.addEventListener("scroll",mark,{passive:!0,capture:!0});
    window.addEventListener("touchmove",mark,{passive:!0,capture:!0});
    window.addEventListener("wheel",mark,{passive:!0,capture:!0});
    o&&o.onEvent&&(o.onEvent("activated",()=>{t=!1;start()}),o.onEvent("deactivated",()=>{t=!0;stop()}));
    poll=window.setInterval(()=>{busy()?stop():start()},400);
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
  return Math.min(d,A?1:1.2);
}
export{y as a,R as g,T as u};
"""

STARFIELD = r"""import{r as w,j as b}from"./vendor-react-B5aZ5G0Y.js";
import{d3 as A,d6 as E}from"./index-a89cf6b8.js";
import{a as H,g as C}from"./useAnimationLoop-637b4af4.js";
import"./vendor-query-qJWxXJpd.js";
import"./vendor-utils-rksnccus.js";
import"./vendor-telegram-BljK9Sw_.js";
import"./vendor-twemoji-D9HvikB8.js";
import"./vendor-i18n-BxdFJVG3.js";
import"./vendor-radix-CmBkAP5K.js";
import"./vendor-cmdk-CTFjeOaw.js";
/* v12 starfield: few dots, fillRect, no arc */
const g=1e3;
function x(c,l,a){return{x:(Math.random()-.5)*c*2,y:(Math.random()-.5)*l*2,z:a?Math.random()*g:g}}
function I({settings:c}){
  const l=w.useRef(null),a=w.useRef(null),y=A(c.color,"#ffffff");
  const mobile=typeof window<"u"&&window.innerWidth<768;
  const u=Math.min(E(c.starCount,50,800,200),mobile?36:64);
  const z=Math.min(E(c.speed,.1,5,1),mobile?0.7:1);
  return w.useEffect(()=>{
    const e=l.current;if(!e)return;
    const t=C(),s=e.parentElement,n=s?.offsetWidth??window.innerWidth,o=s?.offsetHeight??window.innerHeight;
    e.width=n*t;e.height=o*t;e.style.width=`${n}px`;e.style.height=`${o}px`;e.style.willChange="auto";
    const f=e.getContext("2d",{alpha:!0,desynchronized:!0});if(!f)return;
    f.setTransform(t,0,0,t,0,0);
    const d=Array.from({length:Math.floor(u)},()=>x(n,o,!0));
    a.current={ctx:f,stars:d,w:n,h:o,dpr:t};
    let rq=!1;
    const h=()=>{
      if(rq)return;rq=!0;
      requestAnimationFrame(()=>{
        rq=!1;
        const i=s?.offsetWidth??window.innerWidth,r=s?.offsetHeight??window.innerHeight;
        e.width=i*t;e.height=r*t;e.style.width=`${i}px`;e.style.height=`${r}px`;
        a.current&&(a.current.ctx.setTransform(t,0,0,t,0,0),a.current.w=i,a.current.h=r);
      });
    };
    window.addEventListener("resize",h,{passive:!0});
    return()=>window.removeEventListener("resize",h);
  },[u]),
  H(()=>{
    const e=a.current;if(!e)return;
    const{ctx:t,stars:s,w:n,h:o}=e,f=n/2,d=o/2,h=Math.min(n,o);
    t.clearRect(0,0,n,o);t.fillStyle=y;
    for(let i=0;i<s.length;i++){
      const r=s[i];
      if(r.z-=z*4,r.z<=1){s[i]=x(n,o,!1);continue}
      const M=h/r.z,p=f+r.x*M,m=d+r.y*M;
      if(p<0||p>n||m<0||m>o){s[i]=x(n,o,!1);continue}
      const v=1-r.z/g,R=Math.max(.4,v*1.8);
      t.globalAlpha=.25+v*.65;
      t.fillRect(p-R,m-R,R*2,R*2);
    }
    t.globalAlpha=1;
  },[y,u,z]),
  b.jsx("canvas",{ref:l,className:"absolute inset-0 h-full w-full",style:{contain:"strict",willChange:"auto"}});
}
export{I as default};
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
    <script type="module" crossorigin src="/assets/v12/index-a89cf6b8.js"></script>
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-react-B5aZ5G0Y.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-query-qJWxXJpd.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-utils-rksnccus.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-telegram-BljK9Sw_.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-twemoji-D9HvikB8.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-i18n-BxdFJVG3.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-motion-yrvsEvRj.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-radix-CmBkAP5K.js">
    <link rel="modulepreload" crossorigin href="/assets/v12/vendor-cmdk-CTFjeOaw.js">
    <link rel="stylesheet" crossorigin href="/assets/v12/index-ffaf3eb8.css">
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
              var k = 'paskod_boot_retry_v12';
              if (!sessionStorage.getItem(k)) {
                sessionStorage.setItem(k, '1');
                var u = new URL(location.href);
                u.searchParams.set('_v', '12');
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
/* v12-pin-nav-sub */
#root {
  transform: none !important;
  -webkit-transform: none !important;
  perspective: none !important;
  filter: none !important;
}
#cabinet-bottom-nav,
nav.fixed.z-50 {
  position: fixed !important;
  left: 16px !important;
  right: 16px !important;
  bottom: calc(16px + env(safe-area-inset-bottom, 0px)) !important;
  top: auto !important;
  z-index: 2147483000 !important;
  transform: none !important;
  -webkit-transform: none !important;
  translate: none !important;
  filter: none !important;
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  will-change: auto !important;
  margin: 0 !important;
  background: rgba(10, 15, 26, 0.97) !important;
  box-shadow: 0 2px 12px rgba(0,0,0,.35), inset 0 0 0 1px rgba(255,255,255,.05) !important;
}
html.route-subscription canvas,
html.route-subscription [style*="z-index: -2"],
html.route-subscription [style*="z-index:-2"] {
  display: none !important;
}
@media (max-width: 1024px) {
  canvas { contain: strict !important; will-change: auto !important; }
  [class*="backdrop-blur"] {
    -webkit-backdrop-filter: none !important;
    backdrop-filter: none !important;
  }
}
"""


def main() -> None:
    src = SRV / "assets" / "v11"
    if not src.is_dir():
        src = SRV / "assets" / "v10"
    dst = SRV / "assets" / "v12"
    if not src.is_dir():
        raise SystemExit("missing source")

    print("copy", src.name, "-> v12")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    for p in dst.glob("useAnimationLoop-*.js"):
        p.write_text(LOOP)
        print("loop", p.name)

    sf = dst / "starfield-hLRdE50W.js"
    if sf.exists():
        sf.write_text(STARFIELD)
        print("starfield rewritten", sf.stat().st_size)

    # lighter au() star floor
    idx_path = dst / "index-a89cf6b8.js"
    idx = idx_path.read_text()
    idx = idx.replace(
        'typeof a.starCount=="number"&&(a.starCount=Math.max(50,Math.floor(a.starCount/4)))',
        'typeof a.starCount=="number"&&(a.starCount=Math.max(24,Math.floor(a.starCount/6)))',
    )
    idx = idx.replace(
        'typeof a.particleCount=="number"&&(a.particleCount=Math.max(20,Math.floor(a.particleCount/4)))',
        'typeof a.particleCount=="number"&&(a.particleCount=Math.max(12,Math.floor(a.particleCount/6)))',
    )

    # Portal bottom nav to document.body (escapes any containing-block quirks)
    old_nav = 'return t.jsx("nav",{className:g("fixed z-50 transition-colors duration-200 lg:hidden","bg-dark-900/95 backdrop-blur-linear","border border-dark-700/30",e?"pointer-events-none opacity-0":"opacity-100"),style:{bottom:"calc(16px + env(safe-area-inset-bottom, 0px))",left:"16px",right:"16px",borderRadius:"var(--bento-radius, 24px)",padding:"8px 4px",boxShadow:"0 4px 30px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(255, 255, 255, 0.05) inset"},children:t.jsx("div",{className:"flex justify-around",children:h.map(c=>t.jsxs(ae,{to:c.path,onClick:o,className:g("relative flex min-w-[56px] flex-1 shrink-0 flex-col items-center justify-center rounded-2xl px-3 py-2.5 transition-all duration-200",u(c.path)?"text-accent-400":"text-dark-400 hover:text-dark-200"),children:[u(c.path)&&t.jsx("div",{className:"absolute inset-0 rounded-2xl bg-accent-500/15"}),t.jsx(c.icon,{className:"relative z-10 h-5 w-5"}),t.jsx("span",{className:"relative z-10 mt-1 whitespace-nowrap text-2xs",children:c.label})]},c.path))})})}'
    new_nav = 'return ea.createPortal(t.jsx("nav",{id:"cabinet-bottom-nav",className:g("fixed z-50 transition-colors duration-150 lg:hidden","bg-dark-900/95 border border-dark-700/30",e?"pointer-events-none opacity-0":"opacity-100"),style:{position:"fixed",bottom:"calc(16px + env(safe-area-inset-bottom, 0px))",left:"16px",right:"16px",borderRadius:"var(--bento-radius, 24px)",padding:"8px 4px",zIndex:2147483000,transform:"none",WebkitTransform:"none",boxShadow:"0 2px 12px rgba(0, 0, 0, 0.35), 0 0 0 1px rgba(255, 255, 255, 0.05) inset"},children:t.jsx("div",{className:"flex justify-around",children:h.map(c=>t.jsxs(ae,{to:c.path,onClick:o,className:g("relative flex min-w-[56px] flex-1 shrink-0 flex-col items-center justify-center rounded-2xl px-3 py-2.5 transition-colors duration-150",u(c.path)?"text-accent-400":"text-dark-400"),children:[u(c.path)&&t.jsx("div",{className:"absolute inset-0 rounded-2xl bg-accent-500/15"}),t.jsx(c.icon,{className:"relative z-10 h-5 w-5"}),t.jsx("span",{className:"relative z-10 mt-1 whitespace-nowrap text-2xs",children:c.label})]},c.path))})}),document.body)}'

    if old_nav not in idx:
        # try detect if already portaled
        if 'id:"cabinet-bottom-nav"' in idx:
            print("nav already portaled")
        else:
            # show nearby for debug
            i = idx.find('return t.jsx("nav",{className:g("fixed z-50')
            print("NAV MISSING, snippet:", repr(idx[i : i + 200]) if i >= 0 else "no nav")
            raise SystemExit("nav pattern not found")
    else:
        idx = idx.replace(old_nav, new_nav, 1)
        print("nav portaled to body")

    # Keep pause / su / ru from v11; ensure markers
    if "route-subscription" not in idx:
        raise SystemExit("missing route-subscription from v11 base")

    if "v12-pin" not in idx:
        idx = idx.rstrip() + "\n\n/* v12-pin */\n"
    idx_path.write_text(idx)
    print("index", idx_path.stat().st_size)

    # Subscription deeper
    sub = dst / "Subscription-5825f3b4.js"
    su = sub.read_text()
    su = su.replace(
        'queryKey:["subscription",r],queryFn:()=>y.getSubscription(r),retry:!1,staleTime:3e4,refetchOnMount:!0',
        'queryKey:["subscription",r],queryFn:()=>y.getSubscription(r),retry:!1,staleTime:6e4,refetchOnMount:!1',
    )
    su = su.replace(
        'queryKey:["connection-link",r],queryFn:()=>y.getConnectionLink(r),retry:!1,staleTime:3e4',
        'queryKey:["connection-link",r],queryFn:()=>y.getConnectionLink(r),retry:!1,staleTime:6e4',
    )
    su = su.replace(
        'queryKey:["purchase-options",r],queryFn:()=>y.getPurchaseOptions(r),staleTime:3e4,refetchOnMount:!0',
        'queryKey:["purchase-options",r],queryFn:()=>y.getPurchaseOptions(r),staleTime:6e4,refetchOnMount:!1',
    )
    su = su.replace(
        'queryKey:["devices",r],queryFn:()=>y.getDevices(r),enabled:!!t',
        'queryKey:["devices",r],queryFn:()=>y.getDevices(r),enabled:!!t,staleTime:6e4',
    )
    # remaining glows
    su = su.replace(
        "boxShadow:m?i.shadow:`0 2px 16px ${N.mainHex}12`",
        "boxShadow:m?i.shadow:`0 1px 0 ${N.mainHex}22`",
    )
    su = su.replace(
        'boxShadow:G<C?`0 0 6px ${N.mainHex}50`:"none"',
        'boxShadow:G<C?`0 0 0 1px ${N.mainHex}66`:"none"',
    )
    # countdown slower
    su = su.replace("setInterval(f,2e3)", "setInterval(f,5e3)")
    su = su.replace("setInterval(f,1e3)", "setInterval(f,5e3)")
    # revoke timers can stay 1s for accuracy of short countdowns - or slow them
    # Keep revoke at 1s — short UX. Only expiry card was the heavy one.

    su = su.replace("transition-transform duration-150 group-hover:translate-x-1", "transition-colors duration-150")
    su = su.replace("transition-transform duration-150", "transition-colors duration-150")

    if "v12-pin" not in su:
        su = su.rstrip() + "\n/* v12-pin */\n"
    sub.write_text(su)
    print("subscription", sub.stat().st_size)

    # stars even lighter
    for stars in dst.glob("shooting-stars-*.js"):
        st = stars.read_text()
        st = st.replace(
            'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?24:48);',
            'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?16:32);',
        )
        st = st.replace(
            'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?48:90);',
            'const t=Math.min(Math.floor(i*a*p),(typeof window<"u"&&window.innerWidth<768)?16:32);',
        )
        stars.write_text(st)

    css_path = dst / "index-ffaf3eb8.css"
    css = css_path.read_text()
    if "v12-pin-nav-sub" not in css:
        css_path.write_text(css.rstrip() + "\n" + CSS_EXTRA)
        print("css ok")

    (SRV / "index.html").write_text(INDEX_HTML)

    dist_v = DIST / "assets" / "v12"
    if dist_v.exists():
        shutil.rmtree(dist_v)
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copytree(dst, dist_v)
    shutil.copy2(SRV / "index.html", DIST / "index.html")

    checks = [
        "/assets/v12/" in (SRV / "index.html").read_text(),
        'id:"cabinet-bottom-nav"' in idx_path.read_text(),
        "createPortal(t.jsx(\"nav\"" in idx_path.read_text() or "ea.createPortal(t.jsx(\"nav\"" in idx_path.read_text(),
        "staleTime:6e4,refetchOnMount:!1" in sub.read_text(),
        "fillRect" in (dst / "starfield-hLRdE50W.js").read_text(),
        "cabinet-bottom-nav" in css_path.read_text(),
    ]
    print("checks", checks)
    if not all(checks):
        sys.exit(2)
    print("OK_DEPLOY_V12")


if __name__ == "__main__":
    main()
