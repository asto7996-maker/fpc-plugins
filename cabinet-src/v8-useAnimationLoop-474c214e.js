import{r}from"./vendor-react-B5aZ5G0Y.js";
/* v8: lock ~120 FPS, pause RAF on scroll without DOM class thrash */
const A=typeof window<"u"&&window.innerWidth<768;
const _rr=typeof window<"u"&&window.screen&&Number(window.screen.refreshRate)||0;
const b=Math.min(Math.max(_rr||120,120),144);
const h=1e3/b;
function y(s,f){
  const a=r.useRef(s);a.current=s;
  r.useEffect(()=>{
    let e=0,n=0,d=document.hidden,t=!1,scrolling=!1,scrollTimer=0;
    const busy=()=>d||t||scrolling;
    const tick=l=>{
      if(busy()){e=0;return}
      const m=l-n;
      if(m>=h){n=l-m%h;a.current(l,m)}
      e=requestAnimationFrame(tick);
    };
    const start=()=>{if(!e&&!busy()){n=performance.now();e=requestAnimationFrame(tick)}};
    const stop=()=>{if(e){cancelAnimationFrame(e);e=0}};
    const mark=()=>{
      if(!scrolling){scrolling=!0;stop()}
      if(scrollTimer)clearTimeout(scrollTimer);
      scrollTimer=setTimeout(()=>{scrolling=!1;start()},100);
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
    let a=document.hidden,e=!1;const n=()=>f(a||e);
    const d=()=>{a=document.hidden;n()};
    const tg=window.Telegram&&window.Telegram.WebApp;
    document.addEventListener("visibilitychange",d);
    tg&&tg.onEvent&&(tg.onEvent("activated",()=>{e=!1;n()}),tg.onEvent("deactivated",()=>{e=!0;n()}));
    return()=>{document.removeEventListener("visibilitychange",d)};
  },[]),s;
}
function R(){
  const d=(typeof devicePixelRatio<"u"&&devicePixelRatio)||1;
  return Math.min(d,A?1.15:1.35);
}
export{y as a,R as g,T as u};
