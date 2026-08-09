#!/usr/bin/env python3
"""Inject rich ambient background FX into stock cabinet index.html — layout untouched."""
from pathlib import Path
import time
import re

p = Path("/srv/cabinet/index.html")
t = p.read_text(encoding="utf-8")

# If already injected, strip previous pk FX block and body node
if "paskod-bg-fx" in t:
    t = re.sub(r'\n?\s*<!-- pk-build:[\w.-]+ -->\s*<style id="paskod-bg-fx">[\s\S]*?</style>\n?', '\n', t, count=1)
    t = re.sub(r'\n?\s*<div id="pk-fx"[\s\S]*?</div>\s*<script>try\{if\(window\.__paskodDisableBg\)[\s\S]*?</script>\n?', '\n', t, count=1)
    Path(str(p) + f".bak.{int(time.time())}").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
else:
    Path(str(p) + f".bak.{int(time.time())}").write_text(t, encoding="utf-8")

assert "</head>" in t and "<body>" in t

FX_CSS = r"""
      <!-- pk-build:20260809-fx-rich2 -->
      <style id="paskod-bg-fx">
      html{background-color:#070b1c!important}
      body,.dark body,.light body{background-color:transparent!important}
      #root{background:transparent!important}
      html.dark #root [class*="min-h-viewport"],
      html.dark #root main{background-color:transparent!important}
      html.dark #root div[class*="bg-dark-9"],
      html.dark #root div[class*="bg-dark-950"],
      html.dark #root div[class*="bg-[#0"]{background-color:rgba(8,12,28,.42)!important}
      html.dark #root div[class*="rounded-3xl"],
      html.dark #root div[class*="rounded-2xl"],
      html.dark #root section[class*="rounded-3xl"],
      html.dark #root article[class*="rounded-3xl"]{
        backdrop-filter:blur(14px) saturate(140%)!important;
        -webkit-backdrop-filter:blur(14px) saturate(140%)!important;
      }

      #pk-fx{
        position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden;contain:strict;
        background:radial-gradient(120% 90% at 50% -18%, #1a2458 0%, #0d1434 42%, #070b1c 78%, #050712 100%);
      }
      #pk-fx .orb{position:absolute;border-radius:50%;mix-blend-mode:screen;will-change:transform,opacity;filter:blur(2px)}
      #pk-fx .o1{width:95vw;height:95vw;left:-24vw;top:-20vh;opacity:.55;
        background:radial-gradient(circle,rgba(139,92,246,.95) 0%,rgba(139,92,246,0) 62%);animation:pkO1 26s ease-in-out infinite}
      #pk-fx .o2{width:80vw;height:80vw;right:-18vw;top:-6vh;opacity:.42;
        background:radial-gradient(circle,rgba(56,189,248,.92) 0%,rgba(56,189,248,0) 62%);animation:pkO2 32s ease-in-out infinite}
      #pk-fx .o3{width:110vw;height:110vw;left:-14vw;bottom:-38vh;opacity:.48;
        background:radial-gradient(circle,rgba(99,102,241,.95) 0%,rgba(99,102,241,0) 58%);animation:pkO3 38s ease-in-out infinite}
      #pk-fx .o4{width:72vw;height:72vw;right:-12vw;bottom:-14vh;opacity:.34;
        background:radial-gradient(circle,rgba(45,212,191,.9) 0%,rgba(45,212,191,0) 60%);animation:pkO2 28s ease-in-out infinite reverse}
      #pk-fx .o5{width:58vw;height:58vw;left:22vw;top:28vh;opacity:.22;
        background:radial-gradient(circle,rgba(236,72,153,.85) 0%,rgba(236,72,153,0) 62%);animation:pkO1 42s ease-in-out infinite reverse}
      #pk-fx .o6{width:50vw;height:50vw;right:10vw;top:42vh;opacity:.18;
        background:radial-gradient(circle,rgba(250,204,21,.75) 0%,rgba(250,204,21,0) 65%);animation:pkO3 46s ease-in-out infinite}

      #pk-fx .aurora{position:absolute;inset:-20% -10%;opacity:.35;mix-blend-mode:screen;filter:blur(28px);
        background:conic-gradient(from 180deg at 50% 40%,rgba(139,92,246,.0),rgba(56,189,248,.35),rgba(167,139,250,.0),rgba(236,72,153,.28),rgba(45,212,191,.22),rgba(139,92,246,.0));
        animation:pkAurora 48s linear infinite}
      #pk-fx .beam{position:absolute;width:140%;height:42%;left:-20%;top:8%;
        background:linear-gradient(105deg,transparent 20%,rgba(96,165,250,.12) 45%,rgba(167,139,250,.16) 55%,transparent 78%);
        transform:rotate(-8deg);opacity:.55;mix-blend-mode:screen;animation:pkBeam 18s ease-in-out infinite alternate}
      #pk-fx .beam.b2{top:auto;bottom:5%;height:36%;
        background:linear-gradient(75deg,transparent 15%,rgba(45,212,191,.1) 48%,rgba(139,92,246,.14) 62%,transparent 85%);
        transform:rotate(6deg);animation-duration:22s;animation-direction:alternate-reverse}

      #pk-fx .grid{position:absolute;inset:0;opacity:.14;
        background-image:linear-gradient(rgba(148,163,255,.09) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,255,.09) 1px,transparent 1px);
        background-size:48px 48px;
        mask-image:radial-gradient(ellipse 80% 70% at 50% 30%,#000 20%,transparent 75%);
        -webkit-mask-image:radial-gradient(ellipse 80% 70% at 50% 30%,#000 20%,transparent 75%);
        animation:pkGrid 40s linear infinite}

      #pk-fx .stars{position:absolute;inset:0;opacity:.62;background-image:
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
        animation:pkTw 7.5s ease-in-out infinite}
      #pk-fx .stars.s2{opacity:.35;transform:scale(1.15) rotate(8deg);animation:pkTw 11s ease-in-out infinite reverse}

      #pk-fx .spark{position:absolute;width:3px;height:3px;border-radius:50%;background:#fff;box-shadow:0 0 10px 2px rgba(165,180,255,.85);opacity:0;animation:pkSpark 5.5s ease-in-out infinite}
      #pk-fx .spark.s1{left:12%;top:22%;animation-delay:.2s}
      #pk-fx .spark.s2{left:72%;top:18%;animation-delay:1.1s;animation-duration:6.2s}
      #pk-fx .spark.s3{left:38%;top:58%;animation-delay:2.4s;animation-duration:7s}
      #pk-fx .spark.s4{left:84%;top:64%;animation-delay:3.3s}
      #pk-fx .spark.s5{left:54%;top:34%;animation-delay:4.1s;animation-duration:5.8s}
      #pk-fx .spark.s6{left:22%;top:72%;animation-delay:.8s;animation-duration:6.6s}

      #pk-fx .ring{position:absolute;left:50%;top:18%;width:min(120vw,48rem);height:min(120vw,48rem);
        margin-left:calc(min(120vw,48rem)/-2);margin-top:calc(min(120vw,48rem)/-2.4);
        border-radius:50%;border:1px solid rgba(148,163,255,.08);box-shadow:inset 0 0 60px rgba(99,102,241,.06);animation:pkRing 24s ease-in-out infinite}
      #pk-fx .ring.r2{width:min(90vw,36rem);height:min(90vw,36rem);
        margin-left:calc(min(90vw,36rem)/-2);margin-top:calc(min(90vw,36rem)/-2.2);
        border-color:rgba(56,189,248,.07);animation-duration:30s;animation-direction:reverse}

      #pk-fx .veil{position:absolute;inset:0;background:
        radial-gradient(120% 90% at 50% 35%,transparent 55%,rgba(2,4,12,.28) 100%),
        linear-gradient(180deg,rgba(7,11,28,.15) 0%,transparent 28%,transparent 70%,rgba(5,7,18,.35) 100%)}

      /* Light theme: softer sky FX still visible behind login */
      html.light{background-color:#dceaf8!important}
      html.light #pk-fx{
        display:block!important;
        background:radial-gradient(120% 90% at 50% -10%, #eef6ff 0%, #d9e9fb 45%, #c7dbf5 100%);
      }
      html.light #pk-fx .orb{mix-blend-mode:multiply;filter:blur(6px)}
      html.light #pk-fx .o1{opacity:.45;background:radial-gradient(circle,rgba(147,197,253,.9) 0%,transparent 62%)}
      html.light #pk-fx .o2{opacity:.38;background:radial-gradient(circle,rgba(196,181,253,.85) 0%,transparent 62%)}
      html.light #pk-fx .o3{opacity:.4;background:radial-gradient(circle,rgba(125,211,252,.85) 0%,transparent 58%)}
      html.light #pk-fx .o4{opacity:.3;background:radial-gradient(circle,rgba(167,243,208,.8) 0%,transparent 60%)}
      html.light #pk-fx .o5{opacity:.22;background:radial-gradient(circle,rgba(251,207,232,.75) 0%,transparent 62%)}
      html.light #pk-fx .o6{opacity:.16;background:radial-gradient(circle,rgba(253,230,138,.7) 0%,transparent 65%)}
      html.light #pk-fx .aurora{opacity:.22;mix-blend-mode:multiply;filter:blur(36px);
        background:conic-gradient(from 180deg at 50% 40%,rgba(147,197,253,0),rgba(125,211,252,.4),rgba(196,181,253,0),rgba(251,207,232,.35),rgba(167,243,208,.3),rgba(147,197,253,0))}
      html.light #pk-fx .beam{opacity:.4;mix-blend-mode:multiply;
        background:linear-gradient(105deg,transparent 20%,rgba(147,197,253,.25) 45%,rgba(196,181,253,.22) 55%,transparent 78%)}
      html.light #pk-fx .beam.b2{background:linear-gradient(75deg,transparent 15%,rgba(167,243,208,.2) 48%,rgba(147,197,253,.18) 62%,transparent 85%)}
      html.light #pk-fx .grid{opacity:.08;background-image:linear-gradient(rgba(100,130,180,.12) 1px,transparent 1px),linear-gradient(90deg,rgba(100,130,180,.12) 1px,transparent 1px)}
      html.light #pk-fx .stars{opacity:.25;filter:none;background-image:
        radial-gradient(1.4px 1.4px at 12% 20%,rgba(99,102,241,.55),transparent),
        radial-gradient(1.1px 1.1px at 40% 35%,rgba(59,130,246,.4),transparent),
        radial-gradient(1.3px 1.3px at 70% 25%,rgba(139,92,246,.45),transparent),
        radial-gradient(1px 1px at 85% 60%,rgba(14,165,233,.35),transparent),
        radial-gradient(1.2px 1.2px at 28% 78%,rgba(99,102,241,.4),transparent)}
      html.light #pk-fx .stars.s2{opacity:.15}
      html.light #pk-fx .spark{background:#6366f1;box-shadow:0 0 10px 2px rgba(99,102,241,.45)}
      html.light #pk-fx .ring{border-color:rgba(99,102,241,.12);box-shadow:inset 0 0 50px rgba(147,197,253,.12)}
      html.light #pk-fx .ring.r2{border-color:rgba(14,165,233,.12)}
      html.light #pk-fx .veil{background:
        radial-gradient(120% 90% at 50% 35%,transparent 50%,rgba(180,210,240,.35) 100%),
        linear-gradient(180deg,rgba(255,255,255,.25) 0%,transparent 30%,transparent 70%,rgba(190,215,240,.3) 100%)}
      html.light #root [class*="min-h-viewport"],
      html.light #root main{background-color:transparent!important}

      @keyframes pkO1{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(8vw,5vh,0) scale(1.08)}}
      @keyframes pkO2{0%,100%{transform:translate3d(0,0,0) scale(1.03)}50%{transform:translate3d(-7vw,4vh,0) scale(.94)}}
      @keyframes pkO3{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(6vw,-7vh,0) scale(1.1)}}
      @keyframes pkAurora{0%{transform:rotate(0deg) scale(1.05)}100%{transform:rotate(360deg) scale(1.05)}}
      @keyframes pkBeam{0%{transform:rotate(-8deg) translateX(-4%);opacity:.35}100%{transform:rotate(-6deg) translateX(4%);opacity:.65}}
      @keyframes pkGrid{0%{background-position:0 0}100%{background-position:48px 48px}}
      @keyframes pkTw{0%,100%{opacity:.38}50%{opacity:.78}}
      @keyframes pkSpark{0%,100%{opacity:0;transform:scale(.6)}40%,60%{opacity:.95;transform:scale(1.35)}80%{opacity:.2}}
      @keyframes pkRing{0%,100%{transform:scale(1);opacity:.55}50%{transform:scale(1.06);opacity:.9}}

      @media (prefers-reduced-motion:reduce){
        #pk-fx .orb,#pk-fx .aurora,#pk-fx .beam,#pk-fx .grid,#pk-fx .stars,#pk-fx .spark,#pk-fx .ring{animation:none!important}
      }
      html.paskod-lite #pk-fx .stars,
      html.paskod-lite #pk-fx .spark,
      html.paskod-lite #pk-fx .grid,
      html.paskod-lite #pk-fx .o5,
      html.paskod-lite #pk-fx .o6,
      html.paskod-lite #pk-fx .aurora,
      html.paskod-lite #pk-fx .beam,
      html.paskod-lite #pk-fx .ring{display:none!important}
      html.paskod-lite #pk-fx .orb{animation:none!important;opacity:.36;filter:none}
      html.route-subscription #pk-fx .spark,
      html.route-subscription #pk-fx .aurora{display:none!important}
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
      <span class="spark s1"></span><span class="spark s2"></span><span class="spark s3"></span>
      <span class="spark s4"></span><span class="spark s5"></span><span class="spark s6"></span>
      <span class="veil"></span>
    </div>
    <script>try{if(window.__paskodDisableBg){document.documentElement.classList.add('paskod-lite');}}catch(e){}</script>
"""

t = t.replace("</head>", FX_CSS + "  </head>", 1)
if "<body>\n" in t:
    t = t.replace("<body>\n", "<body>\n" + FX_BODY, 1)
else:
    t = t.replace("<body>", "<body>\n" + FX_BODY, 1)

p.write_text(t, encoding="utf-8")
assert "pk-build:20260809-fx-rich2" in t
assert 'id="pk-fx"' in t
assert "pk-header-total" not in t
print("OK fx-rich2", len(t))
