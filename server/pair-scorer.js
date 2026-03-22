// Pair Scorer — rates 0-100 how tradeable a pair is right now
class PairScorer {
  score(candles) {
    if (candles.length < 30) return 0;
    let score = 0;
    // 1. Session (Dubai UTC+4) — all sessions shown, just quality scored
    const h = new Date(Date.now() + 4*3600000).getUTCHours();
    if (h>=17&&h<20) score+=25;       // London+NY overlap — best
    else if (h>=12&&h<22) score+=20;  // London or NY — good
    else if (h>=4&&h<12) score+=12;   // Asian — lower but still shown
    else score+=10;                    // Late NY / early morning
    // 2. Trend bias (last 10 candles)
    const l10=candles.slice(-10);
    const bias=Math.abs(l10.filter(c=>c.c>c.o).length-5);
    if(bias>=4)score+=25;else if(bias>=3)score+=18;else if(bias>=2)score+=10;
    // 3. Volatility
    const l30=candles.slice(-30);
    const avgRng=l30.reduce((s,c)=>s+(c.h-c.l),0)/l30.length;
    const avgC=l30.reduce((s,c)=>s+c.c,0)/l30.length;
    const rr=avgC>0?(avgRng/avgC)*10000:0;
    if(rr>=1.5&&rr<=8)score+=25;else if(rr>=0.8&&rr<=12)score+=15;else if(rr<0.5)score+=0;else score+=5;
    // 4. Continuation rate
    let cont=0;
    for(let i=candles.length-20;i<candles.length-1;i++){
      if((candles[i].c>candles[i].o)===(candles[i+1].c>candles[i+1].o)) cont++;
    }
    const cr=cont/19;
    if(cr>=0.65)score+=25;else if(cr>=0.55)score+=15;else if(cr>=0.50)score+=8;
    return Math.min(100,score);
  }
}
module.exports = { PairScorer };
