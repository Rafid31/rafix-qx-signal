"""
Pocket Option Algo Trader v3.0.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture:
  • Candle data  → Yahoo Finance HTTP API (instant, no wait)
  • Trading      → Pocket Option WebSocket (auth + openOrder)
  • First trade  → within 30-60 seconds of pressing START
  • Signal engine → RSI + BB + Stoch + MACD + EMA + Reversal (6 indicators)

Auth method (confirmed working):
  • Cookie in WebSocket Upgrade headers
  • SSID: 42["auth",{"session":"<escaped>","isDemo":1,"uid":<uid>,"platform":1}]

Environment variables (Railway):
  POCKET_SSID     Full 42["auth",...] message string
  POCKET_COOKIE   Raw cookie header string
  POCKET_UID      User ID (numeric)
  TRADE_AMOUNT    $ per trade (default 1.0)
  TRADE_DURATION  Seconds (default 30)
  TRADE_MODE      DEMO or REAL (default DEMO)
  SIGNAL_THRESH   Min confidence % to trade (default 55)
  LOOP_SECS       Seconds between scans (default 30)
"""

import asyncio
import json
import logging
import os
import ssl
import time
import threading
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import requests
import websockets
from fastapi import FastAPI, WebSocket as FWebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# ══════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("po_trader")

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
VERSION        = "3.1.0"
POCKET_SSID    = os.environ.get("POCKET_SSID", "")
POCKET_COOKIE  = os.environ.get("POCKET_COOKIE", "")
POCKET_UID     = int(os.environ.get("POCKET_UID", "0"))
TRADE_AMOUNT   = float(os.environ.get("TRADE_AMOUNT", "1.0"))
TRADE_DURATION = int(os.environ.get("TRADE_DURATION", "30"))
TRADE_MODE     = os.environ.get("TRADE_MODE", "DEMO")
IS_DEMO        = (TRADE_MODE.upper() != "REAL")
SIGNAL_THRESH  = float(os.environ.get("SIGNAL_THRESH", "55.0"))
LOOP_SECS      = int(os.environ.get("LOOP_SECS", "30"))
MAX_TRADES_HR  = int(os.environ.get("MAX_TRADES_HOUR", "20"))
MIN_CANDLES    = 14   # only 14 candles needed → data loaded instantly

WS_URL = (
    "wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket"
    if IS_DEMO else
    "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket"
)

# Pocket Option asset → Yahoo Finance ticker
PAIRS = {
    "EURUSD_otc": "EURUSD=X",
    "GBPUSD_otc": "GBPUSD=X",
    "USDJPY_otc": "USDJPY=X",
    "AUDUSD_otc": "AUDUSD=X",
    "GBPJPY_otc": "GBPJPY=X",
    "EURJPY_otc": "EURJPY=X",
    "EURCAD_otc": "EURCAD=X",
    "EURGBP_otc": "EURGBP=X",
}

# ══════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════
state = {
    "running":     False,
    "connected":   False,
    "balance":     0.0,
    "mode":        TRADE_MODE,
    "total":       0,
    "won":         0,
    "lost":        0,
    "pending":     0,
    "trades_hour": 0,
    "hour_ts":     time.time(),
    "last_signal": "",
    "error":       "",
    "version":     VERSION,
    "data_source": "Yahoo Finance",
}
trade_log: deque = deque(maxlen=100)
pending_trades: dict = {}
_ws_conn = None
_ws_loop = None
_dashboard_clients: list = []

# ══════════════════════════════════════════════════════════
# YAHOO FINANCE CANDLE FETCHER
# ══════════════════════════════════════════════════════════

def fetch_candles_yf(yf_ticker: str, count: int = 50) -> list:
    """
    Fetch 1-minute OHLC candles from Yahoo Finance.
    Returns list of {"time","open","close","high","low"} dicts.
    Falls back to 5-minute data on market close.
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    # Try 1-minute interval first (last 1 day)
    for interval, period in [("1m", "1d"), ("5m", "5d"), ("15m", "5d")]:
        try:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_ticker}"
                f"?interval={interval}&range={period}"
            )
            resp = requests.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                continue
            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                continue
            r = result[0]
            timestamps = r.get("timestamp", [])
            quote = r.get("indicators", {}).get("quote", [{}])[0]
            opens  = quote.get("open",  [])
            closes = quote.get("close", [])
            highs  = quote.get("high",  [])
            lows   = quote.get("low",   [])

            candles = []
            for i in range(len(timestamps)):
                try:
                    if (opens[i] is not None and closes[i] is not None and
                            highs[i] is not None and lows[i] is not None):
                        candles.append({
                            "time":  timestamps[i],
                            "open":  float(opens[i]),
                            "close": float(closes[i]),
                            "high":  float(highs[i]),
                            "low":   float(lows[i]),
                        })
                except (IndexError, TypeError):
                    pass

            if len(candles) >= MIN_CANDLES:
                log.debug("YF %s: %d candles (%s)", yf_ticker, len(candles), interval)
                return candles[-count:]  # return last N candles
        except Exception as e:
            log.debug("YF fetch error %s %s: %s", yf_ticker, interval, e)
            continue

    return []


# Cache candles to avoid hammering Yahoo Finance
_candle_cache: dict = {}   # yf_ticker → (timestamp, candles)
_CACHE_TTL = 25  # seconds — refresh just before each trading loop


def get_candles(po_pair: str) -> list:
    """Get candles for a PO pair (with caching)."""
    yf_ticker = PAIRS.get(po_pair)
    if not yf_ticker:
        return []
    now = time.time()
    cached = _candle_cache.get(yf_ticker)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    candles = fetch_candles_yf(yf_ticker)
    _candle_cache[yf_ticker] = (now, candles)
    return candles

# ══════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ══════════════════════════════════════════════════════════

def _ema(arr: np.ndarray, n: int) -> float:
    s = arr[0]; k = 2 / (n + 1)
    for v in arr[1:]:
        s = v * k + s * (1 - k)
    return s


def compute_rsi(prices: np.ndarray, period: int = 7) -> float:
    if len(prices) < period + 1:
        return float("nan")
    deltas = np.diff(prices[-(period + 1):])
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    ag, al = gains.mean(), losses.mean()
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def generate_signal(po_pair: str) -> dict:
    cdls = get_candles(po_pair)
    if len(cdls) < MIN_CANDLES:
        return {"signal": "WAIT", "confidence": 0,
                "reason": f"Only {len(cdls)} candles (need {MIN_CANDLES})"}

    closes = np.array([c["close"] for c in cdls], dtype=float)
    highs  = np.array([c["high"]  for c in cdls], dtype=float)
    lows   = np.array([c["low"]   for c in cdls], dtype=float)
    buy = sell = 0.0

    # RSI(7) — weight 2.5
    rsi = compute_rsi(closes, 7)
    if not np.isnan(rsi):
        if rsi < 30:   buy  += 2.5
        elif rsi > 70: sell += 2.5
        else:
            buy  += 2.5 * max(0, (50 - rsi) / 20)
            sell += 2.5 * max(0, (rsi - 50) / 20)

    # Bollinger Bands(14) — weight 2.5
    if len(closes) >= 14:
        bb_m = closes[-14:].mean()
        bb_s = closes[-14:].std()
        if bb_s > 0:
            pct = (closes[-1] - (bb_m - 2*bb_s)) / (4*bb_s)
            if pct < 0.2:   buy  += 2.5
            elif pct > 0.8: sell += 2.5

    # Stochastic(5) — weight 2.0
    if len(closes) >= 5:
        h5 = highs[-5:].max(); l5 = lows[-5:].min()
        if h5 != l5:
            k = (closes[-1] - l5) / (h5 - l5) * 100
            if k < 20:   buy  += 2.0
            elif k > 80: sell += 2.0

    # MACD(5,13) — weight 3.0
    if len(closes) >= 13:
        macd = _ema(closes[-13:], 5) - _ema(closes[-13:], 13)
        if macd > 0:   buy  += 3.0
        elif macd < 0: sell += 3.0

    # EMA(3) vs EMA(8) — weight 2.5
    if len(closes) >= 8:
        if _ema(closes[-8:], 3) > _ema(closes[-8:], 8): buy  += 2.5
        else:                                             sell += 2.5

    # Reversal — weight 2.0
    if len(closes) >= 6:
        rec = closes[-3:].mean(); ear = closes[-6:-3].mean()
        if rec < ear:   buy  += 2.0
        elif rec > ear: sell += 2.0

    W = 14.5
    cb = round(buy  / W * 100, 1)
    cs = round(sell / W * 100, 1)

    if cb >= cs and cb >= SIGNAL_THRESH:
        return {"signal": "BUY",  "confidence": cb, "candles": len(cdls)}
    if cs > cb and cs >= SIGNAL_THRESH:
        return {"signal": "SELL", "confidence": cs, "candles": len(cdls)}
    return {"signal": "WAIT", "confidence": max(cb, cs), "candles": len(cdls)}

# ══════════════════════════════════════════════════════════
# AUTH HELPERS
# ══════════════════════════════════════════════════════════

def _get_auth_params():
    ssid   = POCKET_SSID
    cookie = POCKET_COOKIE
    uid    = POCKET_UID

    if not ssid or not cookie:
        try:
            import browser_cookie3, re
            jar = {c.name: c.value for c in
                   browser_cookie3.chrome(domain_name='.pocketoption.com')}
            ci_raw = jar.get("ci_session", "")
            if ci_raw:
                if not uid:
                    al = urllib.parse.unquote(jar.get("autologin", ""))
                    m  = re.search(r'"user_id";s:\d+:"(\d+)"', al)
                    uid = int(m.group(1)) if m else 0
                if uid:
                    ci  = urllib.parse.unquote(ci_raw)
                    esc = ci.replace("\\", "\\\\").replace('"', '\\"')
                    d   = 1 if IS_DEMO else 0
                    ssid   = f'42["auth",{{"session":"{esc}","isDemo":{d},"uid":{uid},"platform":1}}]'
                    autologin_raw = jar.get("autologin", "")
                    cookie = (f"ci_session={ci_raw}"
                              + (f"; autologin={autologin_raw}" if autologin_raw else "")
                              + "; loggedIn=1; lang=en")
                    log.info("Auth loaded from Chrome (uid=%s)", uid)
        except Exception as e:
            log.warning("Chrome cookie load failed: %s", e)

    if not ssid:
        raise RuntimeError(
            "No auth. Set POCKET_SSID + POCKET_COOKIE env vars, "
            "or run locally with Pocket Option open in Chrome."
        )
    ws_headers = {"Origin": "https://pocketoption.com"}
    if cookie:
        ws_headers["Cookie"] = cookie
    return ssid, cookie, ws_headers


def _ssl_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx

# ══════════════════════════════════════════════════════════
# POCKET OPTION WebSocket HANDLER
# ══════════════════════════════════════════════════════════

async def _ws_handler(ws, ssid: str):
    """Handle all incoming WS messages from PocketOption."""
    sent_auth = False

    async for msg in ws:
        try:
            if isinstance(msg, bytes):
                _parse_binary(msg)
                continue

            log.info("WS← %s", msg[:200])

            if msg.startswith("0{") and "sid" in msg:
                await ws.send("40")
            elif msg == "2":
                await ws.send("3")
            elif msg.startswith("40") and not sent_auth:
                sent_auth = True
                await ws.send(ssid)
                log.info("SSID auth sent")
            elif msg.startswith("451-") or msg.startswith("42["):
                try:
                    json_str = msg[4:] if msg.startswith("451-") else msg[2:]
                    arr = json.loads(json_str)
                    if isinstance(arr, list) and arr:
                        await _handle_sio_event(ws, str(arr[0]),
                                                arr[1] if len(arr) > 1 else {})
                except Exception as ep:
                    log.debug("Event parse err: %s | raw=%s", ep, msg[:120])

        except Exception as e:
            log.error("WS msg error: %s", e)


async def _handle_sio_event(ws, event: str, data):
    """Dispatch a parsed Socket.IO event."""
    log.info("EVT %-25s data=%s", event,
             str(data)[:150] if not isinstance(data, dict)
             else list(data.keys()))
    ev = event.lower()

    if ev in ("successauth", "success-auth"):
        state["connected"] = True
        if isinstance(data, dict):
            _update_balance(data)
        log.info("✅ PO auth success!")
        await ws.send('42["sendMessage",{"name":"get-balances","version":"1.0"}]')
        _broadcast()

    elif ev == "updateassets" and not state["connected"]:
        state["connected"] = True
        log.info("✅ PO connected (updateAssets)!")
        await ws.send('42["sendMessage",{"name":"get-balances","version":"1.0"}]')
        _broadcast()

    elif ev in ("notauthorized",) or (isinstance(data, str) and "notauthorized" in data.lower()):
        log.error("❌ Not authorized — session expired")
        state["error"]     = "Session expired. Update POCKET_SSID + POCKET_COOKIE."
        state["connected"] = False
        _broadcast()

    elif ev in ("updatebalance", "updatebalances", "balances",
                "successbalances", "balance"):
        if isinstance(data, dict):
            _update_balance(data)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    _update_balance(item)

    elif ev in ("openorder", "openorderv2", "successopenorder"):
        if isinstance(data, dict):
            tid = str(data.get("id", ""))
            req = str(data.get("requestId", ""))
            log.info("📋 Trade opened id=%s req=%s asset=%s",
                     tid, req, data.get("asset", "?"))
            if req in pending_trades and tid:
                pending_trades[tid] = pending_trades.pop(req)
                pending_trades[tid]["trade_id"] = tid

    elif ev in ("closeorder", "updateorder", "successcloseorder",
                "deals", "deal", "traderesult"):
        if isinstance(data, list):
            for deal in data:
                if isinstance(deal, dict):
                    _close_deal(deal)
        elif isinstance(data, dict):
            _close_deal(data)


def _update_balance(d: dict):
    for key in ("balance", "demoBalance", "demo_balance", "demo",
                "liveBalance", "live_balance"):
        if key in d and d[key] is not None:
            state["balance"] = float(d[key])
            log.info("💰 Balance: $%.2f", state["balance"])
            _broadcast()
            return


def _parse_binary(data: bytes):
    for skip in (0, 1, 2, 4):
        try:
            obj = json.loads(data[skip:].decode("utf-8", errors="replace"))
            if isinstance(obj, dict):
                if "balance" in obj:
                    _update_balance(obj)
                if ("profit" in obj or "win" in obj) and "asset" in obj:
                    _close_deal(obj)
                if isinstance(obj.get("requestId"), int) and "id" in obj:
                    tid = str(obj["id"]); req = str(obj["requestId"])
                    log.info("📋 Binary trade open id=%s", tid)
                    if req in pending_trades:
                        pending_trades[tid] = pending_trades.pop(req)
            elif isinstance(obj, list):
                for deal in obj:
                    if isinstance(deal, dict) and "profit" in deal:
                        _close_deal(deal)
            return
        except Exception:
            continue


def _close_deal(deal: dict):
    profit    = float(deal.get("profit", 0))
    asset     = deal.get("asset", "?")
    direction = str(deal.get("action", deal.get("dir", "?"))).upper()
    tid       = str(deal.get("id", ""))
    if "win" in deal:
        won = bool(deal["win"]) and profit >= 0
    else:
        won = profit > 0

    state["total"] += 1
    state["won"]   += 1 if won else 0
    state["lost"]  += 0 if won else 1
    state["pending"] = max(0, state["pending"] - 1)

    entry = {
        "time":      datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "pair":      asset,
        "direction": direction,
        "amount":    deal.get("amount", TRADE_AMOUNT),
        "profit":    profit,
        "result":    "WIN" if won else "LOSS",
    }
    trade_log.appendleft(entry)
    log.info("🏁 %s %s %s → %s  profit=%.4f",
             asset, direction, tid, entry["result"], profit)
    if tid in pending_trades:
        del pending_trades[tid]
    _broadcast()


async def _place_trade(ws, pair: str, direction: str) -> str:
    is_demo = 1 if IS_DEMO else 0
    req_id  = int(time.time() * 1000) % 2_147_483_647
    msg = (
        f'42["openOrder",{{"asset":"{pair}",'
        f'"amount":{TRADE_AMOUNT},'
        f'"action":"{direction.lower()}",'
        f'"isDemo":{is_demo},'
        f'"requestId":{req_id},'
        f'"optionType":100,'
        f'"time":{TRADE_DURATION}}}]'
    )
    await ws.send(msg)
    pending_trades[str(req_id)] = {
        "pair": pair, "direction": direction,
        "amount": TRADE_AMOUNT, "opened_at": time.time(),
    }
    state["pending"]     += 1
    state["trades_hour"] += 1
    log.info("🚀 %s %s $%.2f %ds (reqId=%d)",
             pair, direction.upper(), TRADE_AMOUNT, TRADE_DURATION, req_id)
    return str(req_id)

# ══════════════════════════════════════════════════════════
# TRADING LOOP
# ══════════════════════════════════════════════════════════

async def _trading_loop(ws):
    """Scan pairs every LOOP_SECS seconds and fire trades on strong signals."""
    log.info("Trading loop started — scanning %d pairs every %ds",
             len(PAIRS), LOOP_SECS)

    # Pre-warm candle cache in background
    def warm():
        for pair in PAIRS:
            get_candles(pair)
    threading.Thread(target=warm, daemon=True).start()

    # First scan after 10 seconds (give cache time to warm)
    await asyncio.sleep(10)

    while state["running"] and state["connected"]:
        # Hour rate-limit reset
        if time.time() - state["hour_ts"] > 3600:
            state["trades_hour"] = 0
            state["hour_ts"]     = time.time()

        if state["trades_hour"] >= MAX_TRADES_HR:
            log.warning("Max trades/hour (%d) reached — pausing", MAX_TRADES_HR)
            await asyncio.sleep(LOOP_SECS)
            continue

        best_pair = best_dir = None
        best_conf = 0.0

        for pair in PAIRS:
            sig = generate_signal(pair)
            log.debug("Signal %s: %s %.1f%% (%d cdls)",
                      pair, sig["signal"], sig["confidence"], sig.get("candles", 0))
            if sig["signal"] in ("BUY", "SELL") and sig["confidence"] > best_conf:
                best_pair = pair
                best_dir  = "call" if sig["signal"] == "BUY" else "put"
                best_conf = sig["confidence"]

        if best_pair:
            state["last_signal"] = (
                f"{best_pair} {best_dir.upper()} {best_conf:.0f}%"
            )
            await _place_trade(ws, best_pair, best_dir)
        else:
            state["last_signal"] = "WAIT — no signal above threshold"

        _broadcast()
        await asyncio.sleep(LOOP_SECS)

# ══════════════════════════════════════════════════════════
# KEEPALIVE
# ══════════════════════════════════════════════════════════

async def _keepalive(ws):
    """Send Socket.IO ping every 20s to prevent 1005 disconnects."""
    while state["running"] and state["connected"]:
        await asyncio.sleep(20)
        try:
            await ws.send("2")
            log.debug("↔ keepalive ping")
        except Exception:
            break


async def _clean_stale_trades():
    """Remove pending trades with no result after 3x trade duration."""
    while state["running"]:
        await asyncio.sleep(60)
        cutoff = time.time() - (TRADE_DURATION * 3 + 30)
        stale = [k for k, v in list(pending_trades.items())
                 if v.get("opened_at", 0) < cutoff]
        for k in stale:
            log.warning("⏰ Stale trade %s — marking expired", k)
            state["pending"] = max(0, state["pending"] - 1)
            del pending_trades[k]
        if stale:
            _broadcast()


# ══════════════════════════════════════════════════════════
# BOT RUNNER
# ══════════════════════════════════════════════════════════

async def _run_bot():
    state["error"] = ""
    try:
        ssid, cookie, ws_headers = _get_auth_params()
    except RuntimeError as e:
        state["running"] = False
        state["error"] = str(e)
        log.error(str(e))
        _broadcast()
        return

    log.info("PO Trader v%s | Mode=%s | Amount=$%.2f | Duration=%ds",
             VERSION, TRADE_MODE, TRADE_AMOUNT, TRADE_DURATION)

    while state["running"]:
        try:
            async with websockets.connect(
                WS_URL,
                ssl=_ssl_ctx(),
                extra_headers=ws_headers,
                ping_interval=None,
                ping_timeout=None,
            ) as ws:
                global _ws_conn
                _ws_conn = ws
                state["connected"] = False

                handler = asyncio.create_task(_ws_handler(ws, ssid))

                for _ in range(40):
                    if state["connected"]:
                        break
                    await asyncio.sleep(0.5)

                if state["connected"]:
                    trader  = asyncio.create_task(_trading_loop(ws))
                    alive   = asyncio.create_task(_keepalive(ws))
                    cleaner = asyncio.create_task(_clean_stale_trades())
                    await asyncio.gather(handler, trader, alive, cleaner)
                else:
                    handler.cancel()
                    log.error("Auth timeout — session may be expired")
                    state["error"]   = "Auth timeout. Update POCKET_SSID."
                    state["running"] = False
                    break

        except Exception as e:
            log.error("WS error: %s", e)
            state["connected"] = False
            if state["running"]:
                log.info("Reconnecting in 5s…")
                await asyncio.sleep(5)

    state["connected"] = state["running"] = False
    log.info("Bot stopped.")
    _broadcast()


def _start_bot_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run_bot())
    loop.close()

# ══════════════════════════════════════════════════════════
# BROADCAST
# ══════════════════════════════════════════════════════════

def _broadcast():
    payload = json.dumps({
        **state,
        "trades":  list(trade_log)[:20],
        "winrate": round(state["won"] / state["total"] * 100, 1)
                   if state["total"] else 0,
    })
    for q in list(_dashboard_clients):
        try:
            q.put_nowait(payload)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════
# FASTAPI
# ══════════════════════════════════════════════════════════
app = FastAPI(title="PO Trader", version=VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.post("/api/start")
async def api_start():
    if state["running"]:
        return {"ok": False, "msg": "Already running"}
    state.update(running=True, total=0, won=0, lost=0,
                 pending=0, trades_hour=0, hour_ts=time.time(),
                 last_signal="", error="")
    trade_log.clear()
    threading.Thread(target=_start_bot_thread, daemon=True).start()
    return {"ok": True, "msg": "Bot started"}


@app.post("/api/stop")
async def api_stop():
    if not state["running"]:
        return {"ok": False, "msg": "Not running"}
    state["running"] = state["connected"] = False
    return {"ok": True, "msg": "Stopped"}


@app.get("/api/status")
async def api_status():
    return {**state, "trades": list(trade_log)[:20],
            "winrate": round(state["won"]/state["total"]*100,1) if state["total"] else 0}


@app.websocket("/ws")
async def ws_endpoint(ws: FWebSocket):
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    _dashboard_clients.append(q)
    try:
        await ws.send_text(json.dumps({
            **state, "trades": list(trade_log)[:20],
            "winrate": round(state["won"]/state["total"]*100,1) if state["total"] else 0}))
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
                await ws.send_text(msg)
            except asyncio.TimeoutError:
                await ws.send_text('{"ping":1}')
    except WebSocketDisconnect:
        pass
    finally:
        if q in _dashboard_clients:
            _dashboard_clients.remove(q)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PO Algo Trader v""" + VERSION + """</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#060c1a;color:#dde4f0;min-height:100vh}
.hdr{background:linear-gradient(135deg,#0d1b4b,#0a3a7a);padding:16px 22px;display:flex;align-items:center;
  justify-content:space-between;box-shadow:0 2px 14px #0006}
.logo{font-size:1.35em;font-weight:800;letter-spacing:1px}.logo span{color:#38bdf8}
.badge{padding:3px 11px;border-radius:20px;font-size:.72em;font-weight:700;text-transform:uppercase}
.badge.demo{background:#f97316;color:#000}.badge.real{background:#ef4444;color:#fff}
.main{max-width:1060px;margin:0 auto;padding:20px 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.card{background:#0f1829;border:1px solid #1c2d4a;border-radius:10px;padding:14px 16px}
.card .lbl{font-size:.7em;color:#6b7fa8;text-transform:uppercase;letter-spacing:.5px}
.card .val{font-size:1.55em;font-weight:700;margin-top:3px}
.val.g{color:#22c55e}.val.r{color:#ef4444}.val.b{color:#38bdf8}.val.y{color:#fbbf24}
.bar{padding:9px 14px;border-radius:8px;margin-bottom:16px;display:flex;align-items:center;gap:9px;font-size:.86em}
.bar.ok{background:#0d2710;border:1px solid #166534;color:#86efac}
.bar.ng{background:#2a0c0c;border:1px solid #7f1d1d;color:#fca5a5}
.bar.cn{background:#0c1f35;border:1px solid #075985;color:#7dd3fc}
.dot{width:8px;height:8px;border-radius:50%}
.dot.g{background:#22c55e;box-shadow:0 0 5px #22c55e}
.dot.r{background:#ef4444;box-shadow:0 0 5px #ef4444}
.dot.b{background:#38bdf8;animation:pulse 1.1s ease-in-out infinite;box-shadow:0 0 5px #38bdf8}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.sig{padding:9px 14px;background:#0b1526;border-radius:7px;border:1px solid #1c2d4a;
  color:#7dd3fc;font-size:.88em;margin-bottom:16px}
.ctrls{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}
.btn{padding:11px 26px;border:none;border-radius:7px;font-size:.95em;font-weight:700;cursor:pointer;transition:all .18s}
.btn-s{background:linear-gradient(135deg,#16a34a,#14532d);color:#fff}
.btn-s:hover{transform:translateY(-1px);box-shadow:0 4px 15px #22c55e40}
.btn-x{background:linear-gradient(135deg,#dc2626,#7f1d1d);color:#fff}
.btn-x:hover{transform:translateY(-1px);box-shadow:0 4px 15px #ef444440}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none!important}
.err{padding:9px 14px;background:#2a0c0c;border:1px solid #7f1d1d;border-radius:7px;
  color:#fca5a5;font-size:.84em;margin-bottom:16px;display:none}
.tbl-wrap{background:#0f1829;border:1px solid #1c2d4a;border-radius:10px;overflow:hidden}
.tbl-wrap h3{padding:12px 16px;font-size:.88em;color:#6b7fa8;border-bottom:1px solid #1c2d4a}
table{width:100%;border-collapse:collapse;font-size:.83em}
th{padding:9px 13px;text-align:left;color:#475569;font-weight:600;text-transform:uppercase;font-size:.72em;background:#0b1220}
td{padding:9px 13px;border-bottom:1px solid #141f32}
tr:last-child td{border-bottom:none}
.win{color:#22c55e;font-weight:700}.loss{color:#ef4444;font-weight:700}
.ds{font-size:.75em;color:#475569;margin-left:8px}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo">🤖 PO Algo <span>Trader</span> <span class="ds">v""" + VERSION + """ · Yahoo Finance data</span></div>
  <span id="mbadge" class="badge demo">""" + TRADE_MODE + """</span>
</div>
<div class="main">
  <div id="err" class="err"></div>
  <div class="grid">
    <div class="card"><div class="lbl">Balance</div><div class="val b" id="bal">$0.00</div></div>
    <div class="card"><div class="lbl">Total</div><div class="val" id="tot">0</div></div>
    <div class="card"><div class="lbl">Won</div><div class="val g" id="won">0</div></div>
    <div class="card"><div class="lbl">Lost</div><div class="val r" id="lst">0</div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val y" id="wr">0%</div></div>
    <div class="card"><div class="lbl">Pending</div><div class="val" id="pnd">0</div></div>
  </div>
  <div id="sbar" class="bar ng"><span class="dot r" id="dot"></span><span id="stxt">Disconnected</span></div>
  <div class="sig">📡 Signal: <b id="sig">—</b></div>
  <div class="ctrls">
    <button class="btn btn-s" id="bstart" onclick="startBot()">▶ START</button>
    <button class="btn btn-x" id="bstop"  onclick="stopBot()" disabled>■ STOP</button>
  </div>
  <div class="tbl-wrap">
    <h3>Trade History</h3>
    <table>
      <thead><tr><th>Time</th><th>Pair</th><th>Dir</th><th>Amount</th><th>Profit</th><th>Result</th></tr></thead>
      <tbody id="tbody"><tr><td colspan="6" style="color:#475569;text-align:center;padding:18px">
        No trades yet — press START to begin
      </td></tr></tbody>
    </table>
  </div>
</div>
<script>
let ws=null;
function conn(){
  const p=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(p+'://'+location.host+'/ws');
  ws.onmessage=e=>{const d=JSON.parse(e.data);if(d.ping)return;upd(d);};
  ws.onclose=()=>setTimeout(conn,2000);
}
function upd(d){
  document.getElementById('bal').textContent='$'+(d.balance||0).toFixed(2);
  document.getElementById('tot').textContent=d.total||0;
  document.getElementById('won').textContent=d.won||0;
  document.getElementById('lst').textContent=d.lost||0;
  document.getElementById('wr').textContent=(d.winrate||0)+'%';
  document.getElementById('pnd').textContent=d.pending||0;
  document.getElementById('sig').textContent=d.last_signal||'—';
  const bar=document.getElementById('sbar'),dot=document.getElementById('dot'),
        txt=document.getElementById('stxt'),bs=document.getElementById('bstart'),
        bx=document.getElementById('bstop'),er=document.getElementById('err');
  er.style.display=d.error?'block':'none';
  if(d.error)er.textContent='⚠ '+d.error;
  if(d.connected){bar.className='bar ok';dot.className='dot g';
    txt.textContent='Connected · '+(d.mode||'DEMO')+' · Yahoo Finance ✓';}
  else if(d.running){bar.className='bar cn';dot.className='dot b';txt.textContent='Connecting…';}
  else{bar.className='bar ng';dot.className='dot r';txt.textContent='Disconnected';}
  bs.disabled=d.running;bx.disabled=!d.running;
  if(d.trades&&d.trades.length){
    document.getElementById('tbody').innerHTML=d.trades.map(t=>{
      const c=t.result==='WIN'?'win':'loss';
      const p=t.profit>=0?'+$'+t.profit.toFixed(2):'-$'+Math.abs(t.profit).toFixed(2);
      return`<tr><td>${t.time}</td><td>${t.pair}</td><td>${t.direction}</td>
        <td>$${t.amount.toFixed(2)}</td><td class="${c}">${p}</td>
        <td class="${c}">${t.result}</td></tr>`;
    }).join('');
  }
}
async function startBot(){
  document.getElementById('bstart').disabled=true;
  const r=await fetch('/api/start',{method:'POST'});
  const d=await r.json();
  if(!d.ok){alert(d.msg);document.getElementById('bstart').disabled=false;}
}
async function stopBot(){
  document.getElementById('bstop').disabled=true;
  await fetch('/api/stop',{method:'POST'});
}
conn();
</script>
</body>
</html>""")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    log.info("PO Trader v%s on port %d", VERSION, port)
    uvicorn.run(app, host="0.0.0.0", port=port)
