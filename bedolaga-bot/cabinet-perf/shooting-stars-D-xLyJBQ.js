import{r as g,j as C}from"./vendor-react-B5aZ5G0Y.js";
import{d3 as u,d6 as x}from"./index-CzmCxK_8.js";
import{a as E,g as v}from"./useAnimationLoop-D5qBbcNF.js";
import"./vendor-query-qJWxXJpd.js";
import"./vendor-utils-rksnccus.js";
import"./vendor-telegram-BljK9Sw_.js";
import"./vendor-twemoji-D9HvikB8.js";
import"./vendor-i18n-BxdFJVG3.js";
import"./vendor-motion-yrvsEvRj.js";
import"./vendor-radix-CmBkAP5K.js";
import"./vendor-cmdk-CTFjeOaw.js";

/* Optimized shooting-stars: same look, cheaper per-frame work for 90–144 FPS */
function j({settings:l}){
  const y=g.useRef(null),s=g.useRef(null);
  const M=u(l.starColor,"#9E00FF"),S=u(l.trailColor,"#2EB9DF"),w=u(l.bgStarColor,"#ffffff");
  const p=x(l.starDensity,1e-5,.001,15e-5),m=x(l.minSpeed,1,50,10),b=x(l.maxSpeed,5,100,30);

  return g.useEffect(()=>{
    const n=y.current;if(!n)return;
    const o=v(),e=n.parentElement;
    const i=e?.offsetWidth??window.innerWidth,a=e?.offsetHeight??window.innerHeight;
    n.width=i*o;n.height=a*o;n.style.width=`${i}px`;n.style.height=`${a}px`;
    n.style.transform="translateZ(0)";n.style.willChange="contents";
    const h=n.getContext("2d",{alpha:!0,desynchronized:!0,willReadFrequently:!1});
    if(!h)return;
    /* Cap star count so large screens stay smooth at high refresh */
    const t=Math.min(Math.floor(i*a*p),180);
    const r=Array.from({length:t},()=>({
      x:Math.random()*i,
      y:Math.random()*a,
      radius:Math.random()*1.2+.3,
      opacity:Math.random(),
      twinkleSpeed:Math.random()>.3?.5+Math.random()*.5:null
    }));
    h.setTransform(o,0,0,o,0,0);
    s.current={ctx:h,shootingStars:[],bgStars:r,lastShootingTime:0,nextShootingDelay:4200+Math.random()*4500,w:i,h:a,dpr:o};

    let resizeQueued=!1;
    const c=()=>{
      if(resizeQueued)return;
      resizeQueued=!0;
      requestAnimationFrame(()=>{
        resizeQueued=!1;
        const d=e?.offsetWidth??window.innerWidth,f=e?.offsetHeight??window.innerHeight;
        n.width=d*o;n.height=f*o;n.style.width=`${d}px`;n.style.height=`${f}px`;
        if(s.current){s.current.ctx.setTransform(o,0,0,o,0,0);s.current.w=d;s.current.h=f;}
      });
    };
    window.addEventListener("resize",c,{passive:!0});
    return()=>window.removeEventListener("resize",c);
  },[p]),

  E(n=>{
    const o=s.current;if(!o)return;
    const{ctx:e,bgStars:i,w:a,h}=o;
    e.clearRect(0,0,a,h);
    /* Batch bg stars: one fillStyle, fillRect for tiny dots (faster than arc) */
    e.fillStyle=w;
    for(const t of i){
      let r=t.opacity;
      if(t.twinkleSpeed)r=.5+.5*Math.sin(n*.001*t.twinkleSpeed*Math.PI*2);
      e.globalAlpha=r;
      const sz=t.radius*2;
      e.fillRect(t.x-t.radius,t.y-t.radius,sz,sz);
    }
    e.globalAlpha=1;

    if(n-o.lastShootingTime>o.nextShootingDelay){
      o.shootingStars.push({
        x:Math.random()*a,
        y:Math.random()*h*.5,
        angle:Math.PI/4+(Math.random()-.5)*.3,
        scale:.5+Math.random()*.5,
        speed:m+Math.random()*(b-m),
        distance:0,
        opacity:1
      });
      o.lastShootingTime=n;
      o.nextShootingDelay=4200+Math.random()*4500;
    }

    const next=[];
    for(const t of o.shootingStars){
      t.distance+=t.speed;
      t.opacity=Math.max(0,1-t.distance/500);
      if(t.opacity<=0)continue;
      const r=t.x+Math.cos(t.angle)*t.distance;
      const c=t.y+Math.sin(t.angle)*t.distance;
      const d=t.x+Math.cos(t.angle)*Math.max(0,t.distance-80);
      const f=t.y+Math.sin(t.angle)*Math.max(0,t.distance-80);
      e.lineWidth=t.scale*2;
      e.globalAlpha=t.opacity*.4;
      e.strokeStyle=S;
      e.beginPath();e.moveTo(d,f);e.lineTo(r,c);e.stroke();
      e.globalAlpha=t.opacity;
      e.fillStyle=M;
      const rad=t.scale*1.5;
      e.fillRect(r-rad,c-rad,rad*2,rad*2);
      e.globalAlpha=1;
      next.push(t);
    }
    o.shootingStars=next;
  },[M,S,w,p,m,b]),

  C.jsx("canvas",{ref:y,className:"absolute inset-0 h-full w-full",style:{transform:"translateZ(0)",willChange:"contents",contain:"strict"}});
}
export{j as default};
