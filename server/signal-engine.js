// Signal Engine — OTC Optimised (v10 logic)
class SignalEngine {
  ema(a, n) {
    if (!a || a.length < n) return a?.length ? a[a.length-1] : 0;
    const k = 2/(n+1);
    let e = a.slice(0,n).reduce((x,y)=>x+y,0)/n;
    for (let i=n;i<a.length;i++) e = a[i]*k+e*(1-k);
    return e;
  }
  rsi(c, n=7) {
    if (c.length < n+2) return 50;
    let ag=0,al=0;
    for (let i=1;i<=n;i++){const d=c[i]-c[i-1];if(d>0)ag+=d;else al+=Math.abs(d);}
    ag/=n;al/=n;
    for (let j=n+1;j<c.length;j++){
      const d=c[j]-c[j-1];
      ag=(ag*(n-1)+Math.max(d,0))/n;
      al=(al*(n-1)+Math.max(-d,0))/n;
    }
    if(al===0) return ag>0?72:50;
    return Math.min(92,Math.max(8,100-100/(1+ag/al)));
  }
  adx(cs, n=14) {
    if(cs.length<n*2) return 18;
    const sl=cs.slice(-(n*2));
    let sTR=0,sPDM=0,sNDM=0;
    for(let i=1;i<=n;i++){
      const h=sl[i].h,l=sl[i].l,pc=sl[i-1].c;
      sTR+=Math.max(h-l,Math.abs(h-pc),Math.abs(l-pc));
      const up=h-sl[i-1].h,dn=sl[i-1].l-l;
      sPDM+=(up>dn&&up>0)?up:0;
      sNDM+=(dn>up&&dn>0)?dn:0;
    }
    const dx=[];
    for(let j=n+1;j<sl.length;j++){
      const h=sl[j].h,l=sl[j].l,pc=sl[j-1].c;
      const tr=Math.max(h-l,Math.abs(h-pc),Math.abs(l-pc));
      const up=h-sl[j-1].h,dn=sl[j-1].l-l;
      sTR=sTR-sTR/n+tr;
      sPDM=sPDM-sPDM/n+((up>dn&&up>0)?up:0);
      sNDM=sNDM-sNDM/n+((dn>up&&dn>0)?dn:0);
      if(sTR===0) continue;
      const diP=(sPDM/sTR)*100,diM=(sNDM/sTR)*100;
      if(diP+diM>0) dx.push(Math.abs(diP-diM)/(diP+diM)*100);
    }
    if(!dx.length) return 18;
    return Math.min(70,Math.max(5,dx.reduce((a,b)=>a+b,0)/dx.length));
  }

  calculate(candles, secondsLeft, quality=50) {
    const EMPTY = {sig:'WAIT',earlyDir:'',score:0,maxScore:6,blocked:false,blockReason:'',warnMsg:'',streak:'neutral',consecUp:0,consecDn:0,adx:0,rsi:50,indicators:{rsi:'n',ema:'n',macd:'n',candle:'n',trend:'n',momentum:'n'}};
    if(candles.length<20) return {...EMPTY,blockReason:'Need 20+ candles'};

    const c=candles.map(x=>x.c),h=candles.map(x=>x.h),lo=candles.map(x=>x.l);
    const last=c[c.length-1],prev=c[c.length-2]||last,prev2=c[c.length-3]||prev;
    const adxV=this.adx(candles,14);

    const lC=candles[candles.length-2],pC=candles[candles.length-3]||lC;
    const rng=lC.h-lC.l||0.0001,bdy=Math.abs(lC.c-lC.o);
    const uW=lC.h-Math.max(lC.c,lC.o),lW=Math.min(lC.c,lC.o)-lC.l;
    const bPct=bdy/rng,pBdy=Math.abs(pC.c-pC.o);

    const isDoji=bPct<0.08;
    const bullDir=lC.c>lC.o,bearDir=lC.c<lC.o;
    const sBull=bullDir&&bPct>0.30,sBear=bearDir&&bPct>0.30;
    const pinTop=uW>rng*0.50&&bPct<0.25,pinBot=lW>rng*0.50&&bPct<0.25;
    const inside=lC.h<pC.h&&lC.l>pC.l&&bPct<0.12;
    const engU=bullDir&&lC.c>pC.o&&lC.o<pC.c&&bdy>pBdy*0.7;
    const engD=bearDir&&lC.c<pC.o&&lC.o>pC.c&&bdy>pBdy*0.7;

    let cUp=0,cDn=0;
    for(let i=candles.length-2;i>=Math.max(0,candles.length-10);i--){if(candles[i].c>candles[i].o)cUp++;else break;}
    for(let i=candles.length-2;i>=Math.max(0,candles.length-10);i--){if(candles[i].c<candles[i].o)cDn++;else break;}

    // Momentum acceleration: bodies getting bigger?
    const b3=candles.slice(-4,-1).map(x=>Math.abs(x.c-x.o));
    const accelU=bullDir&&cUp>=2&&b3[2]>b3[1]&&b3[1]>b3[0]*0.8;
    const accelD=bearDir&&cDn>=2&&b3[2]>b3[1]&&b3[1]>b3[0]*0.8;
    const trendU=cUp>=2&&cUp<7,trendD=cDn>=2&&cDn<7;

    const rsiV=this.rsi(c,7),rsiP=this.rsi(c.slice(0,-1),7);
    let rsiU=rsiV>rsiP&&rsiV>42&&rsiV<75,rsiD=rsiV<rsiP&&rsiV<58&&rsiV>25;
    if(rsiV<25&&rsiV>rsiP){rsiU=true;rsiD=false;}
    if(rsiV>75&&rsiV<rsiP){rsiD=true;rsiU=false;}

    const e5=this.ema(c,5),e13=this.ema(c,13);
    const slpU=last>prev2,slpD=last<prev2;
    const emaU=e5>e13&&slpU,emaD=e5<e13&&slpD;
    const mhN=e5-e13,mhP=this.ema(c.slice(0,-1),5)-this.ema(c.slice(0,-1),13);
    const macdU=mhN>mhP,macdD=mhN<mhP;

    const vUp=(rsiU?1:0)+(emaU?1:0)+(macdU?1:0)+((sBull||bullDir)?1:0)+(trendU?1:0)+(accelU?1:0);
    const vDn=(rsiD?1:0)+(emaD?1:0)+(macdD?1:0)+((sBear||bearDir)?1:0)+(trendD?1:0)+(accelD?1:0);

    const exh=cUp>=7||cDn>=7;
    let blocked=false,blockReason='';
    if(isDoji){blocked=true;blockReason='⏸ FLAT CANDLE';}
    else if(inside){blocked=true;blockReason='📦 INSIDE BAR';}
    else if(exh){blocked=true;blockReason=`⚠️ EXHAUSTED x${Math.max(cUp,cDn)}`;}
    else if(pinTop&&cUp>=3){blocked=true;blockReason='📌 REJECTION TOP';}
    else if(pinBot&&cDn>=3){blocked=true;blockReason='📌 REJECTION BOT';}

    let warnMsg='';
    if(blocked) warnMsg=blockReason;
    else if(engD&&vUp>vDn) warnMsg='🕯 BEARISH ENGULF';
    else if(engU&&vDn>vUp) warnMsg='🕯 BULLISH ENGULF';
    else if(cUp>=5) warnMsg=`⚠️ ${cUp} greens — watch ↓`;
    else if(cDn>=5) warnMsg=`⚠️ ${cDn} reds — watch ↑`;
    else if(pinTop) warnMsg='📌 REJECTION at top';
    else if(pinBot) warnMsg='📌 REJECTION at bottom';

    let sig='WAIT',earlyDir='';
    if(!blocked){
      if(vUp>=3&&vUp>vDn&&last>prev2){sig='BUY';earlyDir='BUY';}
      else if(vDn>=3&&vDn>vUp&&last<prev2){sig='SELL';earlyDir='SELL';}
      else if(vUp>=2&&vUp>vDn){sig='LEAN';earlyDir='BUY';}
      else if(vDn>=2&&vDn>vUp){sig='LEAN';earlyDir='SELL';}
    }
    if(secondsLeft<=15&&secondsLeft>0&&sig==='LEAN') sig=earlyDir;

    const streak=cUp>=2?`🟢 x${cUp}${cUp>=5?' ⚠️':''}`:cDn>=2?`🔴 x${cDn}${cDn>=5?' ⚠️':''}`: 'neutral';
    return {sig,earlyDir,score:Math.max(vUp,vDn),maxScore:6,blocked,blockReason,warnMsg,streak,consecUp:cUp,consecDn:cDn,adx:Math.round(adxV),rsi:Math.round(rsiV),indicators:{rsi:rsiU?'up':rsiD?'dn':'n',ema:emaU?'up':emaD?'dn':'n',macd:macdU?'up':macdD?'dn':'n',candle:(sBull||bullDir)?'up':(sBear||bearDir)?'dn':'n',trend:trendU?'up':trendD?'dn':'n',momentum:accelU?'up':accelD?'dn':'n'}};
  }
}
module.exports = { SignalEngine };
