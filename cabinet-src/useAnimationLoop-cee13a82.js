import{r}from"./vendor-react-B5aZ5G0Y.js";
/* Subscription-smooth: pause RAF on any scroll/touch; mark body.is-scrolling */
const A=typeof window<"u"&&window.innerWidth<768;
const _rr=typeof window<"u"&&window.screen&&Number(window.screen.refreshRate)||0;
const b=A?72:Math.min(Math.max(_rr||90,72),90);
const h=1e3/b;
function y(s,f){
  const a=r.useRef(s);
  a.current=s;
  r.useEffect(()=>{
    let e=0,n=0,d=document.hidden,t=!1,scrolling=!1,scrollTimer=0;
    const busy=()=>d||t||scrolling;
    const setScrollClass=on=>{
      try{document.documentElement.classList.toggle("is-scrolling",!!on);document.body.classList.toggle("is-scrolling",!!on)}catch{}
    };
    const tick=l=>{
      if(busy()){e=0;return}
      const m=l-n;
      if(m>=h){n=l-m%h;a.current(l,m)}
      e=requestAnimationFrame(tick);
    };
    const start=()=>{if(!e&&!busy()){n=performance.now();e=requestAnimationFrame(tick)}};
    const stop=()=>{if(e){cancelAnimationFrame(e);e=0}};
    const markScroll=()=>{
      if(!scrolling){scrolling=!0;setScrollClass(!0);stop()}
      if(scrollTimer)clearTimeout(scrollTimer);
      scrollTimer=setTimeout(()=>{scrolling=!1;setScrollClass(!1);start()},180);
    };
    const onVis=()=>{d=document.hidden;busy()?stop():start()};
    const o=window.Telegram&&window.Telegram.WebApp;
    const onAct=()=>{t=!1;start()};
    const onDeact=()=>{t=!0;stop()};
    document.addEventListener("visibilitychange",onVis);
    // scroll does not bubble — capture on document + window covers nested scrollers
    window.addEventListener("scroll",markScroll,{passive:!0,capture:!0});
    document.addEventListener("scroll",markScroll,{passive:!0,capture:!0});
    window.addEventListener("touchstart",markScroll,{passive:!0,capture:!0});
    window.addEventListener("touchmove",markScroll,{passive:!0,capture:!0});
    window.addEventListener("wheel",markScroll,{passive:!0,capture:!0});
    o&&o.onEvent&&(o.onEvent("activated",onAct),o.onEvent("deactivated",onDeact));
    start();
    return()=>{
      stop();
      if(scrollTimer)clearTimeout(scrollTimer);
      setScrollClass(!1);
      document.removeEventListener("visibilitychange",onVis);
      window.removeEventListener("scroll",markScroll,!0);
      document.removeEventListener("scroll",markScroll,!0);
      window.removeEventListener("touchstart",markScroll,!0);
      window.removeEventListener("touchmove",markScroll,!0);
      window.removeEventListener("wheel",markScroll,!0);
      o&&o.offEvent&&(o.offEvent("activated",onAct),o.offEvent("deactivated",onDeact));
    };
  },f);
}
function T(){
  const[s,f]=r.useState(()=>typeof document<"u"?document.hidden:!1);
  return r.useEffect(()=>{
    let a=document.hidden,e=!1;
    const n=()=>f(a||e);
    const d=()=>{a=document.hidden;n()};
    const tg=window.Telegram&&window.Telegram.WebApp;
    const i=()=>{e=!1;n()};
    const c=()=>{e=!0;n()};
    document.addEventListener("visibilitychange",d);
    tg&&tg.onEvent&&(tg.onEvent("activated",i),tg.onEvent("deactivated",c));
    return()=>{
      document.removeEventListener("visibilitychange",d);
      tg&&tg.offEvent&&(tg.offEvent("activated",i),tg.offEvent("deactivated",c));
    };
  },[]),s;
}
function R(){
  const d=(typeof devicePixelRatio<"u"&&devicePixelRatio)||1;
  return Math.min(d,A?1.1:1.25);
}
export{y as a,R as g,T as u};
