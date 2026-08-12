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

# Strip previous injections (all duplicates)
t = re.sub(
    r'\n?\s*<script id="paskod-perf-boot">[\s\S]*?</script>\n?',
    "\n",
    t,
)
t = re.sub(
    r'\n?\s*<!-- pk-build:[\w.-]+ -->\s*<style id="paskod-bg-fx">[\s\S]*?</style>\n?',
    "\n",
    t,
)
t = re.sub(
    r'\n?\s*<div id="pk-fx"[\s\S]*?</div>\s*<script id="paskod-perf-runtime">[\s\S]*?</script>\n?',
    "\n",
    t,
)
# legacy anonymous script after pk-fx
t = re.sub(
    r'\n?\s*<div id="pk-fx"[\s\S]*?</div>\s*<script>try\{if\(window\.__paskodDisableBg\)[\s\S]*?</script>\n?',
    "\n",
    t,
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
      <!-- pk-build:20260812-fix -->
      <style id="paskod-bg-fx">
      /* Visual FX only — do NOT alter scroll/touch/layout semantics */
      html{background-color:#070b1c!important}
      body,.dark body,.light body{background-color:transparent!important}
      #root{background:transparent!important}
      html.dark #root [class*="min-h-viewport"],
      html.dark #root main{background-color:transparent!important}

      /* Soften opaque page canvas so FX can show — narrow, no broad bg-[#0] / rounded overrides */
      html.dark #root > div > div[class*="min-h-viewport"]{
        background-color:rgba(8,12,28,.35)!important;
      }

      #root button,
      #root a,
      #root [role="button"]{
        touch-action:manipulation;
        -webkit-tap-highlight-color:transparent;
      }

      #pk-fx{transition:opacity .28s ease}
      html.pk-theme-swap #pk-fx{opacity:0!important}

      #pk-fx{
        position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden;
        contain:strict;isolation:isolate;
        transform:translateZ(0);backface-visibility:hidden;
        background:radial-gradient(120% 90% at 50% -18%, #1a2458 0%, #0d1434 42%, #070b1c 78%, #050712 100%);
      }
      #pk-fx .orb{
        position:absolute;border-radius:50%;
        transform:translateZ(0);backface-visibility:hidden;
        contain:layout paint style;
      }
      #pk-fx .o1{width:95vw;height:95vw;left:-24vw;top:-20vh;opacity:.48;
        background:radial-gradient(circle,rgba(139,92,246,.9) 0%,rgba(139,92,246,.22) 40%,transparent 68%);
        animation:pkO1 32s ease-in-out infinite}
      #pk-fx .o2{width:80vw;height:80vw;right:-18vw;top:-6vh;opacity:.36;
        background:radial-gradient(circle,rgba(56,189,248,.88) 0%,rgba(56,189,248,.2) 42%,transparent 68%);
        animation:pkO2 38s ease-in-out infinite}
      #pk-fx .o3{width:110vw;height:110vw;left:-14vw;bottom:-38vh;opacity:.4;
        background:radial-gradient(circle,rgba(99,102,241,.9) 0%,rgba(99,102,241,.2) 42%,transparent 66%);
        animation:pkO3 44s ease-in-out infinite}
      #pk-fx .o4{width:72vw;height:72vw;right:-12vw;bottom:-14vh;opacity:.28;
        background:radial-gradient(circle,rgba(45,212,191,.85) 0%,rgba(45,212,191,.16) 44%,transparent 68%);
        animation:pkO2 36s ease-in-out infinite reverse}
      #pk-fx .o5{width:58vw;height:58vw;left:22vw;top:28vh;opacity:.16;
        background:radial-gradient(circle,rgba(236,72,153,.75) 0%,rgba(236,72,153,.12) 44%,transparent 68%)}
      #pk-fx .o6{width:50vw;height:50vw;right:10vw;top:42vh;opacity:.12;
        background:radial-gradient(circle,rgba(250,204,21,.65) 0%,rgba(250,204,21,.1) 46%,transparent 70%)}
      #pk-fx .aurora{
        position:absolute;inset:-20% -10%;opacity:.22;transform:translateZ(0);
        background:conic-gradient(from 210deg at 50% 40%,
          rgba(139,92,246,0),rgba(56,189,248,.28),rgba(167,139,250,0),
          rgba(236,72,153,.2),rgba(45,212,191,.14),rgba(139,92,246,0));
      }
      #pk-fx .beam{
        position:absolute;width:140%;height:42%;left:-20%;top:8%;opacity:.4;
        transform:translateZ(0) rotate(-8deg);
        background:linear-gradient(105deg,transparent 20%,rgba(96,165,250,.12) 45%,rgba(167,139,250,.14) 55%,transparent 78%);
      }
      #pk-fx .beam.b2{
        top:auto;bottom:5%;height:36%;opacity:.32;
        background:linear-gradient(75deg,transparent 15%,rgba(45,212,191,.1) 48%,rgba(139,92,246,.12) 62%,transparent 85%);
        transform:translateZ(0) rotate(6deg);
      }
      #pk-fx .stars{
        position:absolute;inset:0;opacity:.5;transform:translateZ(0);
        background-image:
          radial-gradient(1.4px 1.4px at 8% 14%,rgba(255,255,255,.95),transparent),
          radial-gradient(1.1px 1.1px at 22% 42%,rgba(210,225,255,.7),transparent),
          radial-gradient(1.3px 1.3px at 40% 18%,rgba(255,255,255,.9),transparent),
          radial-gradient(1px 1px at 58% 62%,rgba(200,220,255,.65),transparent),
          radial-gradient(1.5px 1.5px at 74% 28%,rgba(255,255,255,.92),transparent),
          radial-gradient(1.1px 1.1px at 88% 54%,rgba(190,210,255,.65),transparent),
          radial-gradient(1.2px 1.2px at 12% 78%,rgba(255,255,255,.8),transparent),
          radial-gradient(1px 1px at 48% 88%,rgba(220,230,255,.7),transparent),
          radial-gradient(1.3px 1.3px at 96% 36%,rgba(255,255,255,.85),transparent);
      }
      #pk-fx .veil{position:absolute;inset:0;transform:translateZ(0);background:
        radial-gradient(120% 90% at 50% 35%,transparent 55%,rgba(2,4,12,.28) 100%),
        linear-gradient(180deg,rgba(7,11,28,.15) 0%,transparent 28%,transparent 70%,rgba(5,7,18,.35) 100%)}

      /* Pause orb CSS only on real scroll class — no display:none thrash */
      html.pk-scrolling #pk-fx .orb{animation-play-state:paused!important}

      html.light{background-color:#dceaf8!important}
      html.light #pk-fx{
        display:block!important;
        background:radial-gradient(120% 90% at 50% -10%, #eef6ff 0%, #d9e9fb 45%, #c7dbf5 100%);
      }
      html.light #pk-fx .o1{opacity:.38;background:radial-gradient(circle,rgba(147,197,253,.85) 0%,rgba(147,197,253,.18) 42%,transparent 68%)}
      html.light #pk-fx .o2{opacity:.3;background:radial-gradient(circle,rgba(196,181,253,.8) 0%,rgba(196,181,253,.16) 42%,transparent 68%)}
      html.light #pk-fx .o3{opacity:.32;background:radial-gradient(circle,rgba(125,211,252,.8) 0%,rgba(125,211,252,.16) 42%,transparent 66%)}
      html.light #pk-fx .o4{opacity:.24;background:radial-gradient(circle,rgba(167,243,208,.75) 0%,rgba(167,243,208,.14) 42%,transparent 68%)}
      html.light #pk-fx .o5{opacity:.14;background:radial-gradient(circle,rgba(251,207,232,.65) 0%,transparent 68%)}
      html.light #pk-fx .o6{opacity:.1;background:radial-gradient(circle,rgba(253,230,138,.55) 0%,transparent 70%)}
      html.light #pk-fx .aurora{opacity:.14}
      html.light #pk-fx .beam{opacity:.28}
      html.light #pk-fx .stars{opacity:.18}
      html.light #pk-fx .veil{background:
        radial-gradient(120% 90% at 50% 35%,transparent 50%,rgba(180,210,240,.3) 100%),
        linear-gradient(180deg,rgba(255,255,255,.2) 0%,transparent 30%,transparent 70%,rgba(190,215,240,.26) 100%)}
      html.light #root [class*="min-h-viewport"],
      html.light #root main{background-color:transparent!important}

      @keyframes pkO1{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(5vw,3vh,0) scale(1.05)}}
      @keyframes pkO2{0%,100%{transform:translate3d(0,0,0) scale(1.02)}50%{transform:translate3d(-4vw,2vh,0) scale(.97)}}
      @keyframes pkO3{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(3vw,-4vh,0) scale(1.05)}}

      @media (prefers-reduced-motion:reduce){
        #pk-fx .orb{animation:none!important}
        #pk-fx{transition:none!important}
      }
      html.paskod-avg #pk-fx .aurora,
      html.paskod-avg #pk-fx .beam,
      html.paskod-avg #pk-fx .o5,
      html.paskod-avg #pk-fx .o6{display:none!important}
      html.paskod-lite #pk-fx .aurora,
      html.paskod-lite #pk-fx .beam,
      html.paskod-lite #pk-fx .o4,
      html.paskod-lite #pk-fx .o5,
      html.paskod-lite #pk-fx .o6,
      html.paskod-lite #pk-fx .stars{display:none!important}
      html.paskod-lite #pk-fx .orb{animation:none!important;opacity:.32}
      html.pk-hidden #pk-fx .orb{animation-play-state:paused!important}
    </style>
"""

FX_BODY = r"""
    <div id="pk-fx" aria-hidden="true">
      <span class="aurora"></span>
      <span class="orb o1"></span><span class="orb o2"></span><span class="orb o3"></span>
      <span class="orb o4"></span><span class="orb o5"></span><span class="orb o6"></span>
      <span class="beam"></span><span class="beam b2"></span>
      <span class="stars"></span>
      <span class="veil"></span>
    </div>
    <script id="paskod-perf-runtime">
    (function(){
      var root = document.documentElement;
      var scrolling = false;
      var timer = 0;
      var raf = 0;
      var themeTimer = 0;
      var prevLight = root.classList.contains('light');

      function armScroll(){
        if (!scrolling) {
          scrolling = true;
          root.classList.add('pk-scrolling');
        }
        clearTimeout(timer);
        timer = setTimeout(function(){
          scrolling = false;
          root.classList.remove('pk-scrolling');
        }, 180);
      }

      function onScrollHint(){
        if (raf) return;
        raf = requestAnimationFrame(function(){
          raf = 0;
          armScroll();
        });
      }

      /* ONLY real scroll events — never touchmove (breaks gestures / felt like freeze) */
      var opts = {passive:true, capture:true};
      window.addEventListener('scroll', onScrollHint, opts);
      document.addEventListener('scroll', onScrollHint, opts);

      document.addEventListener('visibilitychange', function(){
        root.classList.toggle('pk-hidden', document.hidden);
      }, false);

      try {
        new MutationObserver(function(){
          var light = root.classList.contains('light');
          if (light === prevLight) return;
          prevLight = light;
          root.classList.add('pk-theme-swap');
          clearTimeout(themeTimer);
          themeTimer = setTimeout(function(){ root.classList.remove('pk-theme-swap'); }, 300);
        }).observe(root, {attributes:true, attributeFilter:['class']});
      } catch (e3) {}
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
assert "pk-build:20260812-fix" in t
assert 'id="pk-fx"' in t
assert "paskod-perf-runtime" in t
assert "pk-header-total" not in t
assert t.count('id="paskod-perf-boot"') == 1
assert t.count('id="paskod-bg-fx"') == 1
print("OK fix", len(t))
print("stock_patched", STOCK_NEW[:40] in t or "if (cls) window.__paskodPerfClass" in t)
