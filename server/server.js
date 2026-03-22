// RafiX Signal Server v6 — Pocket Option WebSocket (24/7, no PC, no IP blocks)
const express = require('express');
const { WebSocketServer } = require('ws');
const http = require('http');
const { QXSession } = require('./qx-session');
const { SignalEngine } = require('./signal-engine');
const { PairScorer } = require('./pair-scorer');

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server });

// QX session token — set via Railway env var QX_TOKEN
const QX_TOKEN = process.env.QX_TOKEN || '';

// Telegram bot for verification code alerts
const TG_TOKEN  = process.env.TG_TOKEN  || '8701910654:AAE5Xcl3tFRGhtArlOXjFQ9EFlnL3IOBl1U';
const TG_CHATID = process.env.TG_CHATID || '6063240252';

const ALL_PAIRS = [
  'EURUSD_otc','GBPUSD_otc','AUDUSD_otc','USDCAD_otc','USDCHF_otc',
  'NZDUSD_otc','EURGBP_otc','EURJPY_otc','GBPJPY_otc','AUDJPY_otc',
  'USDBRL_otc','USDMXN_otc','USDINR_otc','USDARS_otc','USDBDT_otc'
];

// State
const candles   = {};  // pairSym -> [candle,...]
const liveTicks = {};  // pairSym -> [tick,...]
const pairScore = {};  // pairSym -> 0-100
const lastSig   = {};  // pairSym -> signal obj
ALL_PAIRS.forEach(p => { candles[p] = []; liveTicks[p] = []; });

const engine = new SignalEngine();
const scorer = new PairScorer();
let session  = null;
let verifyCode = null; // holds pending verification code from Telegram

// Pending verification resolver
let verifyResolver = null;

// Telegram helper — send message
async function tgSend(text) {
  try {
    const fetch = require('node-fetch');
    await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: TG_CHATID, text, parse_mode: 'HTML' })
    });
  } catch(e) { console.log('[TG] Send failed:', e.message); }
}

// Poll Telegram for /code reply from Rafid
async function waitForTelegramCode(timeoutMs = 300000) {
  return new Promise((resolve) => {
    verifyResolver = resolve;
    setTimeout(() => {
      verifyResolver = null;
      resolve(null);
    }, timeoutMs);
  });
}

// Tick processor — builds candles from raw ticks
function processTick(sym, price, timestamp) {
  if (!ALL_PAIRS.includes(sym)) return;
  const minuteTs = Math.floor(timestamp / 60000) * 60000;
  if (!liveTicks[sym]) liveTicks[sym] = [];

  const prev = liveTicks[sym][liveTicks[sym].length - 1];
  const prevMin = prev ? Math.floor(prev.t / 60000) * 60000 : null;

  if (prevMin && prevMin !== minuteTs) {
    // Candle closed — finalise it
    const tks = liveTicks[sym];
    const candle = {
      t: prevMin,
      o: tks[0].p,
      h: Math.max(...tks.map(t => t.p)),
      l: Math.min(...tks.map(t => t.p)),
      c: tks[tks.length - 1].p,
      n: tks.length
    };
    if (!candles[sym]) candles[sym] = [];
    candles[sym].push(candle);
    if (candles[sym].length > 300) candles[sym].shift();
    liveTicks[sym] = [];
  }
  liveTicks[sym].push({ p: price, t: timestamp });
}

// Signal loop — every 500ms
function runLoop() {
  const now = Date.now();
  const sec  = Math.floor((now % 60000) / 1000);
  const rem  = 60 - sec;
  const rescore = sec % 30 === 0;
  const updates = {};

  ALL_PAIRS.forEach(sym => {
    const hist = candles[sym] || [];
    if (hist.length < 5) return;

    const tks = liveTicks[sym] || [];
    const live = tks.length > 0 ? {
      t: Math.floor(now / 60000) * 60000,
      o: tks[0].p,
      h: Math.max(...tks.map(t => t.p)),
      l: Math.min(...tks.map(t => t.p)),
      c: tks[tks.length - 1].p,
      n: tks.length
    } : null;

    const all = live ? [...hist, live] : hist;

    if (rescore || !pairScore[sym]) pairScore[sym] = scorer.score(all);

    const sig = engine.calculate(all, rem, pairScore[sym]);
    lastSig[sym] = sig;

    updates[sym] = {
      sym,
      label: sym.replace('_otc', ' (OTC)'),
      price: live ? live.c : (hist[hist.length-1]?.c || 0),
      signal: sig.sig,
      earlyDir: sig.earlyDir,
      score: sig.score,
      maxScore: sig.maxScore,
      quality: pairScore[sym],
      secondsLeft: rem,
      isSignalWindow: rem <= 15 && rem > 0,
      isUrgent: rem <= 5 && rem > 0,
      candleCount: hist.length,
      indicators: sig.indicators,
      blocked: sig.blocked,
      blockReason: sig.blockReason,
      warnMsg: sig.warnMsg,
      streak: sig.streak,
      consecUp: sig.consecUp,
      consecDn: sig.consecDn,
      adx: sig.adx,
      rsi: sig.rsi
    };
  });

  // Show pairs scoring 20+ (was 40 — lowered so Asian session pairs show)
  const active = Object.values(updates)
    .filter(p => p.quality >= 5)
    .sort((a, b) => b.quality - a.quality)
    .slice(0, 4);

  const msg = JSON.stringify({
    type: 'UPDATE', ts: now, secondsLeft: rem,
    isSignalWindow: rem <= 15 && rem > 0,
    activePairs: active,
    totalMonitored: ALL_PAIRS.length,
    serverStatus: session ? 'LIVE' : 'WAITING'
  });

  wss.clients.forEach(c => { if (c.readyState === 1) c.send(msg); });
}

// WebSocket handler
wss.on('connection', (ws, req) => {
  console.log('[WS] Client connected:', req.socket.remoteAddress);
  ws.send(JSON.stringify({ type: 'INIT', pairs: ALL_PAIRS.length, status: session ? 'LIVE' : 'STARTING' }));
  ws.on('close', () => console.log('[WS] Client disconnected'));
});

// HTTP — health + admin (verify code input page)
// Accept tick data pushed from Chrome extension
app.use(express.json());
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.sendStatus(200);
  next();
});
app.post('/push', (req, res) => {
  try {
    const { ticks } = req.body;
    if (Array.isArray(ticks)) {
      ticks.forEach(({ sym, price, ts }) => {
        if (sym && price) processTick(sym, parseFloat(price), ts || Date.now());
      });
    }
    res.json({ ok: true, received: ticks?.length || 0 });
  } catch(e) { res.json({ ok: false }); }
});

app.get('/', (req, res) => res.json({
  ok: true, pairs: ALL_PAIRS.length,
  clients: wss.clients.size,
  uptime: Math.floor(process.uptime()) + 's',
  session: session ? 'active' : 'none'
}));

// Admin page — enter verification code from browser if needed
app.get('/admin', (req, res) => {
  res.send(`<!DOCTYPE html><html><body style="background:#111;color:#fff;font-family:monospace;padding:30px">
  <h2>RafiX Server Admin</h2>
  <p>Enter QX verification code if server is waiting:</p>
  <input id="code" placeholder="e.g. 123456" style="padding:8px;font-size:16px;width:200px">
  <button onclick="submitCode()" style="padding:8px 16px;margin-left:8px;cursor:pointer">Submit</button>
  <p id="msg" style="color:#0f0;margin-top:10px"></p>
  <script>
  function submitCode(){
    const c=document.getElementById('code').value.trim();
    fetch('/code?c='+encodeURIComponent(c))
      .then(r=>r.json()).then(d=>document.getElementById('msg').textContent=d.ok?'✅ Code submitted!':'❌ Error');
  }
  </script></body></html>`);
});

// Code endpoint — receives code from browser OR Telegram bot
app.get('/code', (req, res) => {
  const code = req.query.c || '';
  if (code && verifyResolver) {
    verifyResolver(code);
    verifyResolver = null;
    res.json({ ok: true });
  } else {
    res.json({ ok: false, msg: 'No code needed right now' });
  }
});

// Telegram long-poll for /code replies
async function pollTelegram() {
  let offset = 0;
  const fetch = require('node-fetch');
  while (true) {
    try {
      const r = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/getUpdates?offset=${offset}&timeout=30`);
      const data = await r.json();
      if (data.result) {
        for (const update of data.result) {
          offset = update.update_id + 1;
          const text = update.message?.text || '';
          if (text.startsWith('/code ') && verifyResolver) {
            const code = text.replace('/code ', '').trim();
            verifyResolver(code);
            verifyResolver = null;
            await tgSend('✅ Code received! Server continuing login...');
          }
        }
      }
    } catch(e) { await new Promise(r => setTimeout(r, 5000)); }
  }
}

// Start session — Pocket Option WebSocket, no IP blocks, 24/7
async function startSession() {
  try {
    session = new QXSession({ onTick: processTick });
    await session.start();
    await tgSend('✅ RafiX Signal Server LIVE!\n📡 Pocket Option WebSocket connected\n15 OTC pairs — works 24/7 without PC!');
  } catch(e) {
    console.error('[Session] Error:', e.message);
    session = null;
    setTimeout(startSession, 30000);
  }
}

// Boot
const PORT = process.env.PORT || 8080;
server.listen(PORT, async () => {
  console.log(`[Server] Running on port ${PORT}`);
  pollTelegram();
  setInterval(runLoop, 500);
  await startSession();
});

process.on('unhandledRejection', err => console.error('[Error]', err?.message));
