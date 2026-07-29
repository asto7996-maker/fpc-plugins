import{r}from"./vendor-react-B5aZ5G0Y.js";
/* High-refresh animation loop: 90–144 FPS, pause when hidden */
const A=typeof window<"u"&&window.innerWidth<768;
const _rr=typeof window<"u"&&window.screen&&Number(window.screen.refreshRate)||0;
const b=A?90:Math.min(Math.max(_rr||120,90),144);
const h=1e3/b;
function y(s,f){
  const a=r.useRef(s);
  a.current=s;
  r.useEffect(()=>{
    let e=0,n=0,d=document.hidden,t=!1;
    const i=()=>d||t;
    const c=l=>{
      const m=l-n;
      if(m>=h){
        n=l-m%h;
        a.current(l,m);
      }
      e=requestAnimationFrame(c);
    };
    const u=()=>{e||(n=performance.now(),e=requestAnimationFrame(c))};
    const v=()=>{e&&(cancelAnimationFrame(e),e=0)};
    const E=()=>{d=document.hidden,i()?v():u()};
    const o=window.Telegram?.WebApp;
    const p=()=>{t=!1,i()||u()};
    const g=()=>{t=!0,v()};
    document.addEventListener("visibilitychange",E);
    o&&(o.onEvent?.("activated",p),o.onEvent?.("deactivated",g));
    i()||u();
    return()=>{
      v();
      document.removeEventListener("visibilitychange",E);
      o&&(o.offEvent?.("activated",p),o.offEvent?.("deactivated",g));
    };
  },f);
}
function T(){
  const[s,f]=r.useState(()=>typeof document<"u"?document.hidden:!1);
  return r.useEffect(()=>{
    let a=document.hidden,e=!1;
    const n=()=>f(a||e);
    const d=()=>{a=document.hidden,n()};
    const t=window.Telegram?.WebApp;
    const i=()=>{e=!1,n()};
    const c=()=>{e=!0,n()};
    document.addEventListener("visibilitychange",d);
    t&&(t.onEvent?.("activated",i),t.onEvent?.("deactivated",c));
    return()=>{
      document.removeEventListener("visibilitychange",d);
      t&&(t.offEvent?.("activated",i),t.offEvent?.("deactivated",c));
    };
  },[]),s;
}
/* Cap DPR to keep canvas fill-rate high at 90–144 FPS */
function R(){
  const d=(typeof devicePixelRatio<"u"&&devicePixelRatio)||1;
  return Math.min(d,A?1.25:1.5);
}
export{y as a,R as g,T as u};
