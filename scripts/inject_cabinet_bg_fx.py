#!/usr/bin/env python3
"""Inject GPU-optimized ambient FX + 120fps scroll/tap runtime into stock cabinet.

Replaces any previous paskod-bg-fx / perf blocks. No layout spacer overrides.
"""
from pathlib import Path
import time
import re

p = Path("/srv/cabinet/index.html")
raw = p.read_text(encoding="utf-8")
Path(str(p) + f".bak.{int(time.time())}").write_text(raw, encoding="utf-8")
t = raw

# Strip previous injections
t = re.sub(
    r'\n?\s*<script id="paskod-perf-boot">[\s\S]*?</script>\n?',
    "\n",
    t,
    count=1,
)
t = re.sub(
    r'\n?\s*<!-- pk-build:[\w.-]+ -->\s*<style id="paskod-bg-fx">[\s\S]*?</style>\n?',
    "\n",
    t,
    count=1,
)
t = re.sub(
    r'\n?\s*<div id="pk-fx"[\s\S]*?</div>\s*<script id="paskod-perf-runtime">[\s\S]*?</script>\n?',
    "\n",
    t,
    count=1,
)
# legacy anonymous script after pk-fx
t = re.sub(
    r'\n?\s*<div id="pk-fx"[\s\S]*?</div>\s*<script>try\{if\(window\.__paskodDisableBg\)[\s\S]*?</script>\n?',
    "\n",
    t,
    count=1,
)

assert "</head>" in t and "<body>" in t

# Patch stock Telegram Android perf probe so it doesn't wipe our class to ""
STOCK_OLD = """          window.__paskodPerfClass = cls || '';
          /* Official Telegram guidance: minimize effects on weaker Android classes.
             LOW → no particle BG. AVERAGE → reduced. HIGH → full. */
          if (cls === 'LOW') window.__paskodDisableBg = true;"""

STOCK_NEW = """          /* Preserve HIGH default from paskod-perf-boot; only Telegram UA overrides */
          if (cls) window.__paskodPerfClass = cls;
          else if (!window.__paskodPerfClass) window.__paskodPerfClass = 'HIGH';
          /* Official Telegram guidance: minimize effects on weaker Android classes. */
          if (cls === 'LOW') {
            window.__paskodDisableBg = true;
            try { document.documentElement.classList.add('paskod-lite'); } catch (e1) {}
          } else if (cls === 'AVERAGE') {
            try { document.documentElement.classList.add('paskod-avg'); } catch (e2) {}
          }
          /* Always own ambient via #pk-fx (skip duplicate React portal BG) */
          window.__paskodDisableBg = true;"""

if STOCK_OLD in t:
    t = t.replace(STOCK_OLD, STOCK_NEW, 1)
else:
    # softer fallback patch
    t = t.replace(
        "window.__paskodPerfClass = cls || '';",
        "if (cls) window.__paskodPerfClass = cls; else if (!window.__paskodPerfClass) window.__paskodPerfClass = 'HIGH';",
        1,
    )

BOOT = r"""
    <script id="paskod-perf-boot">
    (function(){
      try{
        /* Sole ambient owner — React portal BG stays off */
        window.__paskodDisableBg = true;
        window.__paskodPerfClass = window.__paskodPerfClass || 'HIGH';
        var weak = false;
        try{
          if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) weak = true;
          var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
          if (c && (c.saveData || /2g|slow-2g/i.test(String(c.effectiveType||'')))) weak = true;
          /* Only very low RAM — do NOT lite on 4-core phones (common mid-range) */
          if (typeof navigator.deviceMemory === 'number' && navigator.deviceMemory <= 1) weak = true;
        }catch(e){}
        if (weak) {
          window.__paskodPerfClass = 'LOW';
          document.documentElement.classList.add('paskod-lite');
        }
      }catch(e){}
    })();
    </script>
"""

FX_CSS = r"""
      <!-- pk-build:20260809-smooth -->
      <style id="paskod-bg-fx">
      html{background-color:#070b1c!important}
      body,.dark body,.light body{background-color:transparent!important}
      #root{background:transparent!important}
      html.dark #root [class*="min-h-viewport"],
      html.dark #root main{background-color:transparent!important}
      html.dark #root div[class*="bg-dark-9"],
      html.dark #root div[class*="bg-dark-950"],
      html.dark #root div[class*="bg-[#0"]{background-color:rgba(8,12,28,.48)!important}

      /* Surfaces: stable fill so glass on/off doesn’t pop */
      html.dark #root div[class*="rounded-3xl"],
      html.dark #root div[class*="rounded-2xl"],
      html.dark #root section[class*="rounded-3xl"],
      html.dark #root article[class*="rounded-3xl"]{
        background-color:rgba(10,14,32,.58)!important;
        transition:background-color .32s ease,box-shadow .32s ease,border-color .32s ease,opacity .28s ease,transform .28s ease!important;
      }
      html.dark:not(.pk-scrolling) #root div[class*="rounded-3xl"],
      html.dark:not(.pk-scrolling) #root div[class*="rounded-2xl"],
      html.dark:not(.pk-scrolling) #root section[class*="rounded-3xl"],
      html.dark:not(.pk-scrolling) #root article[class*="rounded-3xl"]{
        backdrop-filter:blur(10px) saturate(130%)!important;
        -webkit-backdrop-filter:blur(10px) saturate(130%)!important;
      }
      html.pk-scrolling #root *,
      html.pk-scrolling #root *::before,
      html.pk-scrolling #root *::after{
        backdrop-filter:none!important;
        -webkit-backdrop-filter:none!important;
      }

      /* Interaction surface + chrome easing */
      html,body{overscroll-behavior:none;-webkit-text-size-adjust:100%}
      #root{
        overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
        transition:opacity .36s cubic-bezier(.22,.61,.36,1);
      }
      #root main,
      #root [class*="overflow-y-auto"],
      #root [class*="overflow-auto"],
      #root [class*="overflow-y-scroll"]{
        overscroll-behavior:contain;
        -webkit-overflow-scrolling:touch;
      }
      #root button,
      #root a,
      #root [role="button"],
      #root [class*="cursor-pointer"],
      #root input,
      #root select,
      #root textarea,
      #root label{
        touch-action:manipulation;
        -webkit-tap-highlight-color:transparent;
        transition:background-color .2s ease,color .2s ease,border-color .2s ease,opacity .2s ease,box-shadow .2s ease,transform .12s ease!important;
      }
      @media (hover:none){
        #root button:active,
        #root a:active,
        #root [role="button"]:active{
          transform:translateZ(0) scale(.985);
        }
      }

      /* Theme / boot crossfade */
      html.pk-theme-swap #root{opacity:.88}
      html.pk-theme-swap #pk-fx{opacity:0}
      #boot{transition:opacity .35s ease!important}
      @keyframes pkFxIn{from{opacity:0}to{opacity:1}}
      @keyframes pkSoftIn{from{opacity:0;transform:translate3d(0,8px,0)}to{opacity:1;transform:translate3d(0,0,0)}}
      html.pk-booted #root > *:not(#pk-fx){animation:pkSoftIn .38s cubic-bezier(.22,.61,.36,1) both}

      /* Ambient FX — compositor-friendly (transform/opacity only) */
      #pk-fx{
        position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden;
        contain:strict;isolation:isolate;
        transform:translateZ(0);backface-visibility:hidden;
        background:radial-gradient(120% 90% at 50% -18%, #1a2458 0%, #0d1434 42%, #070b1c 78%, #050712 100%);
        animation:pkFxIn .55s cubic-bezier(.22,.61,.36,1) both;
        transition:opacity .38s cubic-bezier(.22,.61,.36,1);
      }
      #pk-fx .orb,
      #pk-fx .aurora,
      #pk-fx .beam,
      #pk-fx .grid,
      #pk-fx .ring,
      #pk-fx .stars,
      #pk-fx .stars.s2,
      #pk-fx .veil{
        transition:opacity .34s cubic-bezier(.22,.61,.36,1);
      }
      #pk-fx .orb{
        position:absolute;border-radius:50%;
        will-change:transform;transform:translateZ(0);backface-visibility:hidden;
        contain:layout paint style;
      }
      #pk-fx .o1{width:95vw;height:95vw;left:-24vw;top:-20vh;opacity:.5;
        background:radial-gradient(circle,rgba(139,92,246,.9) 0%,rgba(139,92,246,.25) 38%,rgba(139,92,246,0) 68%);
        animation:pkO1 28s ease-in-out infinite}
      #pk-fx .o2{width:80vw;height:80vw;right:-18vw;top:-6vh;opacity:.38;
        background:radial-gradient(circle,rgba(56,189,248,.88) 0%,rgba(56,189,248,.22) 40%,rgba(56,189,248,0) 68%);
        animation:pkO2 34s ease-in-out infinite}
      #pk-fx .o3{width:110vw;height:110vw;left:-14vw;bottom:-38vh;opacity:.42;
        background:radial-gradient(circle,rgba(99,102,241,.9) 0%,rgba(99,102,241,.22) 40%,rgba(99,102,241,0) 66%);
        animation:pkO3 40s ease-in-out infinite}
      #pk-fx .o4{width:72vw;height:72vw;right:-12vw;bottom:-14vh;opacity:.3;
        background:radial-gradient(circle,rgba(45,212,191,.85) 0%,rgba(45,212,191,.18) 42%,rgba(45,212,191,0) 68%);
        animation:pkO2 30s ease-in-out infinite reverse}
      #pk-fx .o5{width:58vw;height:58vw;left:22vw;top:28vh;opacity:.2;
        background:radial-gradient(circle,rgba(236,72,153,.8) 0%,rgba(236,72,153,.15) 42%,rgba(236,72,153,0) 68%);
        animation:pkO1 44s ease-in-out infinite reverse}
      #pk-fx .o6{width:50vw;height:50vw;right:10vw;top:42vh;opacity:.16;
        background:radial-gradient(circle,rgba(250,204,21,.7) 0%,rgba(250,204,21,.12) 45%,rgba(250,204,21,0) 70%);
        animation:pkO3 48s ease-in-out infinite}

      #pk-fx .aurora{
        position:absolute;inset:-25% -15%;opacity:.28;
        transform:translateZ(0);will-change:transform;backface-visibility:hidden;
        background:conic-gradient(from 180deg at 50% 40%,
          rgba(139,92,246,0),rgba(56,189,248,.32),rgba(167,139,250,0),
          rgba(236,72,153,.24),rgba(45,212,191,.18),rgba(139,92,246,0));
        animation:pkAurora 64s linear infinite;
      }
      #pk-fx .beam{
        position:absolute;width:140%;height:42%;left:-20%;top:8%;opacity:.45;
        transform:translateZ(0) rotate(-8deg);will-change:transform,opacity;backface-visibility:hidden;
        background:linear-gradient(105deg,transparent 20%,rgba(96,165,250,.14) 45%,rgba(167,139,250,.18) 55%,transparent 78%);
        animation:pkBeam 20s ease-in-out infinite alternate;
      }
      #pk-fx .beam.b2{
        top:auto;bottom:5%;height:36%;
        background:linear-gradient(75deg,transparent 15%,rgba(45,212,191,.12) 48%,rgba(139,92,246,.14) 62%,transparent 85%);
        transform:translateZ(0) rotate(6deg);animation-duration:24s;animation-direction:alternate-reverse;
      }
      #pk-fx .grid{
        position:absolute;inset:0;opacity:.12;transform:translateZ(0);
        background-image:
          linear-gradient(rgba(148,163,255,.09) 1px,transparent 1px),
          linear-gradient(90deg,rgba(148,163,255,.09) 1px,transparent 1px);
        background-size:48px 48px;
        mask-image:radial-gradient(ellipse 80% 70% at 50% 30%,#000 20%,transparent 75%);
        -webkit-mask-image:radial-gradient(ellipse 80% 70% at 50% 30%,#000 20%,transparent 75%);
      }
      #pk-fx .stars{
        position:absolute;inset:0;opacity:.55;transform:translateZ(0);
        background-image:
          radial-gradient(1.5px 1.5px at 8% 14%,rgba(255,255,255,.95),transparent),
          radial-gradient(1.1px 1.1px at 18% 48%,rgba(210,225,255,.75),transparent),
          radial-gradient(1.4px 1.4px at 28% 22%,rgba(255,255,255,.9),transparent),
          radial-gradient(1px 1px at 36% 68%,rgba(200,220,255,.7),transparent),
          radial-gradient(1.6px 1.6px at 44% 12%,rgba(255,255,255,.95),transparent),
          radial-gradient(1.2px 1.2px at 52% 56%,rgba(190,210,255,.65),transparent),
          radial-gradient(1.3px 1.3px at 62% 30%,rgba(255,255,255,.88),transparent),
          radial-gradient(1px 1px at 70% 74%,rgba(200,220,255,.6),transparent),
          radial-gradient(1.5px 1.5px at 78% 18%,rgba(255,255,255,.92),transparent),
          radial-gradient(1.1px 1.1px at 86% 52%,rgba(190,210,255,.7),transparent),
          radial-gradient(1.2px 1.2px at 92% 82%,rgba(255,255,255,.8),transparent),
          radial-gradient(1px 1px at 12% 86%,rgba(220,230,255,.7),transparent),
          radial-gradient(1.3px 1.3px at 58% 90%,rgba(255,255,255,.85),transparent),
          radial-gradient(1px 1px at 4% 62%,rgba(200,220,255,.55),transparent),
          radial-gradient(1.4px 1.4px at 96% 38%,rgba(255,255,255,.88),transparent);
      }
      #pk-fx .stars.s2{opacity:.28;transform:translateZ(0) scale(1.12) rotate(8deg)}
      #pk-fx .ring{
        position:absolute;left:50%;top:18%;width:min(120vw,48rem);height:min(120vw,48rem);
        margin-left:calc(min(120vw,48rem)/-2);margin-top:calc(min(120vw,48rem)/-2.4);
        border-radius:50%;border:1px solid rgba(148,163,255,.08);
        box-shadow:inset 0 0 60px rgba(99,102,241,.05);opacity:.5;transform:translateZ(0);
      }
      #pk-fx .ring.r2{
        width:min(90vw,36rem);height:min(90vw,36rem);
        margin-left:calc(min(90vw,36rem)/-2);margin-top:calc(min(90vw,36rem)/-2.2);
        border-color:rgba(56,189,248,.07);opacity:.4;
      }
      #pk-fx .spark{
        position:absolute;width:3px;height:3px;border-radius:50%;background:#fff;
        box-shadow:0 0 8px 1px rgba(165,180,255,.75);
        opacity:0;transform:translateZ(0) scale(.6);will-change:transform,opacity;
        animation:pkSpark 6s ease-in-out infinite;
      }
      #pk-fx .spark.s1{left:12%;top:22%;animation-delay:.2s}
      #pk-fx .spark.s2{left:72%;top:18%;animation-delay:1.4s;animation-duration:6.8s}
      #pk-fx .spark.s3{left:38%;top:58%;animation-delay:2.8s;animation-duration:7.2s}
      #pk-fx .spark.s4{left:84%;top:64%;animation-delay:3.6s}
      #pk-fx .veil{position:absolute;inset:0;transform:translateZ(0);background:
        radial-gradient(120% 90% at 50% 35%,transparent 55%,rgba(2,4,12,.28) 100%),
        linear-gradient(180deg,rgba(7,11,28,.15) 0%,transparent 28%,transparent 70%,rgba(5,7,18,.35) 100%)}

      /* Scroll: pause motion + soft opacity fade (no visibility snap) */
      html.pk-scrolling #pk-fx .orb,
      html.pk-scrolling #pk-fx .aurora,
      html.pk-scrolling #pk-fx .beam,
      html.pk-scrolling #pk-fx .spark{animation-play-state:paused!important;will-change:auto!important}
      html.pk-scrolling #pk-fx .o1{opacity:.28!important}
      html.pk-scrolling #pk-fx .o2{opacity:.2!important}
      html.pk-scrolling #pk-fx .o3{opacity:.22!important}
      html.pk-scrolling #pk-fx .o4{opacity:.16!important}
      html.pk-scrolling #pk-fx .o5{opacity:.1!important}
      html.pk-scrolling #pk-fx .o6{opacity:.08!important}
      html.pk-scrolling #pk-fx .aurora,
      html.pk-scrolling #pk-fx .beam,
      html.pk-scrolling #pk-fx .grid,
      html.pk-scrolling #pk-fx .ring,
      html.pk-scrolling #pk-fx .stars.s2{opacity:0!important}
      html.pk-scrolling #pk-fx .spark{opacity:0!important;animation:none!important}
      html.pk-scroll-settle #pk-fx .orb,
      html.pk-scroll-settle #pk-fx .aurora,
      html.pk-scroll-settle #pk-fx .beam,
      html.pk-scroll-settle #pk-fx .grid,
      html.pk-scroll-settle #pk-fx .ring,
      html.pk-scroll-settle #pk-fx .stars,
      html.pk-scroll-settle #pk-fx .stars.s2{
        transition-duration:.45s;
      }

      html.light{background-color:#dceaf8!important}
      html.light #pk-fx{
        display:block!important;
        background:radial-gradient(120% 90% at 50% -10%, #eef6ff 0%, #d9e9fb 45%, #c7dbf5 100%);
      }
      html.light #pk-fx .o1{opacity:.4;background:radial-gradient(circle,rgba(147,197,253,.85) 0%,rgba(147,197,253,.2) 42%,transparent 68%)}
      html.light #pk-fx .o2{opacity:.34;background:radial-gradient(circle,rgba(196,181,253,.8) 0%,rgba(196,181,253,.18) 42%,transparent 68%)}
      html.light #pk-fx .o3{opacity:.36;background:radial-gradient(circle,rgba(125,211,252,.8) 0%,rgba(125,211,252,.18) 42%,transparent 66%)}
      html.light #pk-fx .o4{opacity:.26;background:radial-gradient(circle,rgba(167,243,208,.75) 0%,rgba(167,243,208,.15) 42%,transparent 68%)}
      html.light #pk-fx .o5{opacity:.18;background:radial-gradient(circle,rgba(251,207,232,.7) 0%,rgba(251,207,232,.12) 42%,transparent 68%)}
      html.light #pk-fx .o6{opacity:.14;background:radial-gradient(circle,rgba(253,230,138,.65) 0%,rgba(253,230,138,.1) 45%,transparent 70%)}
      html.light #pk-fx .aurora{opacity:.18;background:conic-gradient(from 180deg at 50% 40%,rgba(147,197,253,0),rgba(125,211,252,.35),rgba(196,181,253,0),rgba(251,207,232,.3),rgba(167,243,208,.25),rgba(147,197,253,0))}
      html.light #pk-fx .beam{opacity:.35;background:linear-gradient(105deg,transparent 20%,rgba(147,197,253,.22) 45%,rgba(196,181,253,.2) 55%,transparent 78%)}
      html.light #pk-fx .beam.b2{background:linear-gradient(75deg,transparent 15%,rgba(167,243,208,.18) 48%,rgba(147,197,253,.16) 62%,transparent 85%)}
      html.light #pk-fx .grid{opacity:.07}
      html.light #pk-fx .stars{opacity:.22;background-image:
        radial-gradient(1.4px 1.4px at 12% 20%,rgba(99,102,241,.5),transparent),
        radial-gradient(1.1px 1.1px at 40% 35%,rgba(59,130,246,.35),transparent),
        radial-gradient(1.3px 1.3px at 70% 25%,rgba(139,92,246,.4),transparent),
        radial-gradient(1px 1px at 85% 60%,rgba(14,165,233,.3),transparent),
        radial-gradient(1.2px 1.2px at 28% 78%,rgba(99,102,241,.35),transparent)}
      html.light #pk-fx .stars.s2{opacity:.12}
      html.light #pk-fx .spark{background:#6366f1;box-shadow:0 0 8px 1px rgba(99,102,241,.4)}
      html.light #pk-fx .ring{border-color:rgba(99,102,241,.12);box-shadow:inset 0 0 40px rgba(147,197,253,.1)}
      html.light #pk-fx .veil{background:
        radial-gradient(120% 90% at 50% 35%,transparent 50%,rgba(180,210,240,.32) 100%),
        linear-gradient(180deg,rgba(255,255,255,.22) 0%,transparent 30%,transparent 70%,rgba(190,215,240,.28) 100%)}
      html.light #root [class*="min-h-viewport"],
      html.light #root main{background-color:transparent!important}

      @keyframes pkO1{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(6vw,4vh,0) scale(1.06)}}
      @keyframes pkO2{0%,100%{transform:translate3d(0,0,0) scale(1.02)}50%{transform:translate3d(-5vw,3vh,0) scale(.96)}}
      @keyframes pkO3{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(4vw,-5vh,0) scale(1.07)}}
      @keyframes pkAurora{to{transform:translateZ(0) rotate(360deg)}}
      @keyframes pkBeam{
        0%{transform:translateZ(0) rotate(-8deg) translate3d(-3%,0,0);opacity:.3}
        100%{transform:translateZ(0) rotate(-6deg) translate3d(3%,0,0);opacity:.55}
      }
      @keyframes pkSpark{
        0%,100%{opacity:0;transform:translateZ(0) scale(.55)}
        45%,55%{opacity:.9;transform:translateZ(0) scale(1.25)}
        75%{opacity:.15;transform:translateZ(0) scale(.7)}
      }

      @media (prefers-reduced-motion:reduce){
        #pk-fx .orb,#pk-fx .aurora,#pk-fx .beam,#pk-fx .spark{animation:none!important}
        #pk-fx,#pk-fx *,#root,#root *{transition:none!important;animation:none!important}
      }
      /* AVERAGE Android class — keep orbs, drop secondary motion */
      html.paskod-avg #pk-fx .spark,
      html.paskod-avg #pk-fx .aurora,
      html.paskod-avg #pk-fx .beam,
      html.paskod-avg #pk-fx .stars.s2,
      html.paskod-avg #pk-fx .grid{display:none!important}
      html.paskod-lite #pk-fx .stars.s2,
      html.paskod-lite #pk-fx .spark,
      html.paskod-lite #pk-fx .grid,
      html.paskod-lite #pk-fx .o5,
      html.paskod-lite #pk-fx .o6,
      html.paskod-lite #pk-fx .aurora,
      html.paskod-lite #pk-fx .beam,
      html.paskod-lite #pk-fx .ring{display:none!important}
      html.paskod-lite #pk-fx .orb{animation:none!important;opacity:.34;will-change:auto}
      html.pk-hidden #pk-fx .orb,
      html.pk-hidden #pk-fx .aurora,
      html.pk-hidden #pk-fx .beam,
      html.pk-hidden #pk-fx .spark{animation-play-state:paused!important}

      /* Hide any leftover React BG portals if they mount */
      #root > div.pointer-events-none.fixed.inset-0{display:none!important}
    </style>
"""

FX_BODY = r"""
    <div id="pk-fx" aria-hidden="true">
      <span class="aurora"></span>
      <span class="orb o1"></span><span class="orb o2"></span><span class="orb o3"></span>
      <span class="orb o4"></span><span class="orb o5"></span><span class="orb o6"></span>
      <span class="beam"></span><span class="beam b2"></span>
      <span class="grid"></span>
      <span class="ring"></span><span class="ring r2"></span>
      <span class="stars"></span><span class="stars s2"></span>
      <span class="spark s1"></span><span class="spark s2"></span>
      <span class="spark s3"></span><span class="spark s4"></span>
      <span class="veil"></span>
    </div>
    <script id="paskod-perf-runtime">
    (function(){
      var root = document.documentElement;
      var scrolling = false;
      var timer = 0;
      var settleTimer = 0;
      var themeTimer = 0;
      var prevLight = root.classList.contains('light');

      function setScrolling(on){
        if (!on) return;
        if (!scrolling) {
          scrolling = true;
          root.classList.add('pk-scrolling');
          root.classList.remove('pk-scroll-settle');
          try { window.__paskodPauseAnim = true; } catch (e) {}
        }
        clearTimeout(timer);
        clearTimeout(settleTimer);
        timer = setTimeout(function(){
          scrolling = false;
          root.classList.remove('pk-scrolling');
          root.classList.add('pk-scroll-settle');
          try {
            if (!(location.pathname || '').includes('subscription')) {
              window.__paskodPauseAnim = false;
            }
          } catch (e2) {}
          settleTimer = setTimeout(function(){
            root.classList.remove('pk-scroll-settle');
          }, 420);
        }, 140);
      }

      var opts = {passive:true, capture:true};
      window.addEventListener('scroll', function(){ setScrolling(true); }, opts);
      window.addEventListener('wheel', function(){ setScrolling(true); }, opts);
      window.addEventListener('touchmove', function(){ setScrolling(true); }, opts);
      document.addEventListener('scroll', function(){ setScrolling(true); }, opts);

      document.addEventListener('visibilitychange', function(){
        if (document.hidden) root.classList.add('pk-hidden');
        else root.classList.remove('pk-hidden');
      }, false);

      /* Smooth theme (light/dark) crossfade */
      try {
        new MutationObserver(function(){
          var light = root.classList.contains('light');
          if (light === prevLight) return;
          prevLight = light;
          root.classList.add('pk-theme-swap');
          clearTimeout(themeTimer);
          themeTimer = setTimeout(function(){
            root.classList.remove('pk-theme-swap');
          }, 380);
        }).observe(root, {attributes:true, attributeFilter:['class']});
      } catch (e3) {}

      /* Soft enter after boot node leaves */
      try {
        var boot = document.getElementById('boot');
        if (!boot) root.classList.add('pk-booted');
        else {
          new MutationObserver(function(){
            if (!document.getElementById('boot') || (boot.classList && boot.classList.contains('hide'))) {
              root.classList.add('pk-booted');
            }
          }).observe(boot, {attributes:true, attributeFilter:['class']});
          setTimeout(function(){ root.classList.add('pk-booted'); }, 1200);
        }
      } catch (e4) {
        root.classList.add('pk-booted');
      }
    })();
    </script>
"""

# Boot as early as possible in <head>
if "<head>" in t:
    t = t.replace("<head>", "<head>\n" + BOOT, 1)
else:
    t = BOOT + t

t = t.replace("</head>", FX_CSS + "  </head>", 1)
if "<body>\n" in t:
    t = t.replace("<body>\n", "<body>\n" + FX_BODY, 1)
else:
    t = t.replace("<body>", "<body>\n" + FX_BODY, 1)

p.write_text(t, encoding="utf-8")
assert "pk-build:20260809-smooth" in t
assert 'id="pk-fx"' in t
assert "paskod-perf-runtime" in t
assert "pk-header-total" not in t
assert t.count('id="paskod-perf-boot"') == 1
assert t.count('id="paskod-bg-fx"') == 1
print("OK smooth", len(t))
print("stock_patched", STOCK_NEW[:40] in t or "if (cls) window.__paskodPerfClass" in t)
