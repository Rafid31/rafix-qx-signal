"""
Pocket Option Algo Trader v2.0.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auth method (confirmed working):
  • Cookie: ci_session + autologin sent in WebSocket Upgrade headers
  • SSID:   42["auth",{"session":"<escaped_ci_session>","isDemo":1,"uid":<uid>,"platform":1}]
  • Trades every 30 s using 6-indicator signal engine
  • Web dashboard with START / STOP + live trade log
  • DEMO mode by default — set TRADE_MODE=REAL for real money

Environment variables:
  POCKET_SSID       Full 42["auth",...] message string
  POCKET_COOKIE     Raw Cookie header: ci_session=...&autologin=...
  POCKET_UID        Pocket Option user ID (numeric)
  TRADE_AMOUNT      $ per trade (default 1.0)
  TRADE_DURATION    Seconds per trade (default 30)
  TRADE_MODE        DEMO or REAL (default DEMO)
"""

import asyncio
import json
import logging
import os
import ssl
import time
import threading
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
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
VERSION         = "2.0.0"
POCKET_SSID     = os.environ.get("POCKET_SSID", "")
POCKET_COOKIE   = os.environ.get("POCKET_COOKIE", "")
POCKET_UID      = int(os.environ.get("POCKET_UID", "0"))
TRADE_AMOUNT    = float(os.environ.get("TRADE_AMOUNT", "1.0"))
TRADE_DURATION  = int(os.environ.get("TRADE_DURATION", "30"))
MIN_CANDLES     = 50
SIGNAL_THRESH   = 60.0
LOOP_SECS       = 30
MAX_TRADES_HOUR = 20
TRADE_MODE      = os.environ.get("TRADE_MODE", "DEMO")
IS_DEMO         = (TRADE_MODE.upper() != "REAL")

WS_URL_DEMO     = "wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket"
WS_URL_REAL     = "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket"
WS_URL          = WS_URL_DEMO if IS_DEMO else WS_URL_REAL

TRADING_PAIRS = [
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc",
    "GBPJPY_otc", "EURJPY_otc", "BTCUSD_otc", "ETHUSD_otc",
]

# ══════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════
state = {
    "running":      False,
    "connected":    False,
    "balance":      0.0,
    "mode":         TRADE_MODE,
    "total":        0,
    "won":          0,
    "lost":         0,
    "pending":      0,
    "trades_hour":  0,
    "hour_ts":      time.time(),
    "last_signal":  "",
    "error":        "",
    "version":      VERSION,
}
trade_log: deque = deque(maxlen=100)

# Candle store per pair — deque of {"time", "open", "close", "high", "low"}
candles: dict = {pair: deque(maxlen=500) for pair in TRADING_PAIRS}

# Pending trade IDs waiting for result
pending_trades: dict = {}   # trade_id → {"pair", "direction", "amount", "opened_at"}

# WebSocket connection (live)
_ws_conn   = None
_ws_lock   = asyncio.Lock()
_ws_loop   = None
_trader_task = None
_stop_event  = threading.Event()

# ══════════════════════════════════════════════════════════
# SSID / COOKIE HELPERS
# ══════════════════════════════════════════════════════════

def _build_ssid_from_cookie(ci_session_raw: str, uid: int, is_demo: bool) -> str:
    """
    Build the Socket.IO auth message from a URL-encoded ci_session cookie value.
    Escape any double-quotes in the decoded PHP serialized string.
    """
    decoded = urllib.parse.unquote(ci_session_raw)
    escaped = decoded.replace("\\", "\\\\").replace('"', '\\"')
    demo_flag = 1 if is_demo else 0
    return f'42["auth",{{"session":"{escaped}","isDemo":{demo_flag},"uid":{uid},"platform":1}}]'


def _get_auth_params():
    """
    Return (ssid, cookie_header, ws_headers).
    Priority:
      1. POCKET_SSID + POCKET_COOKIE env vars (Railway / manual)
      2. Chrome browser cookies (local machine)
    """
    ssid   = POCKET_SSID
    cookie = POCKET_COOKIE
    uid    = POCKET_UID

    # Try to read from Chrome if not set via env
    if not ssid or not cookie:
        try:
            import browser_cookie3
            cookies = browser_cookie3.chrome(domain_name='.pocketoption.com')
            jar = {c.name: c.value for c in cookies}
            ci_raw = jar.get("ci_session", "")
            if ci_raw and not uid:
                # Extract uid from autologin cookie
                autologin_raw = jar.get("autologin", "")
                if autologin_raw:
                    autologin = urllib.parse.unquote(autologin_raw)
                    import re
                    m = re.search(r'"user_id";s:\d+:"(\d+)"', autologin)
                    if m:
                        uid = int(m.group(1))
            if ci_raw and uid:
                ssid   = _build_ssid_from_cookie(ci_raw, uid, IS_DEMO)
                autologin_raw = jar.get("autologin", "")
                cookie = (
                    f"ci_session={ci_raw}"
                    + (f"; autologin={autologin_raw}" if autologin_raw else "")
                    + "; loggedIn=1; lang=en"
                )
                log.info("Auth loaded from Chrome browser cookies (uid=%s)", uid)
        except Exception as e:
            log.warning("Could not load Chrome cookies: %s", e)

    if not ssid:
        raise RuntimeError(
            "No SSID available. Set POCKET_SSID + POCKET_COOKIE env vars, "
            "or run locally with Pocket Option open in Chrome."
        )

    ws_headers = {"Origin": "https://pocketoption.com"}
    if cookie:
        ws_headers["Cookie"] = cookie

    return ssid, cookie, ws_headers

# ══════════════════════════════════════════════════════════
# SIGNAL ENGINE  (same as QX server.py v2.6.2)
# ══════════════════════════════════════════════════════════

def compute_rsi(prices: np.ndarray, period: int = 7) -> float:
    if len(prices) < period + 1:
        return float("nan")
    deltas = np.diff(prices[-(period + 1):])
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def generate_signal(pair: str) -> dict:
    """Run signal engine on candle history. Returns signal dict."""
    cdls = list(candles[pair])
    if len(cdls) < MIN_CANDLES:
        return {"signal": "WAIT", "confidence": 0, "reason": "Not enough candles"}

    closes = np.array([c["close"] for c in cdls], dtype=float)
    highs  = np.array([c["high"]  for c in cdls], dtype=float)
    lows   = np.array([c["low"]   for c in cdls], dtype=float)

    score_buy = score_sell = 0.0

    # --- RSI(7) weight 2.5 ---
    rsi = compute_rsi(closes, 7)
    if not np.isnan(rsi):
        if rsi < 30:   score_buy  += 2.5
        elif rsi > 70: score_sell += 2.5
        else:
            score_buy  += 2.5 * max(0, (50 - rsi) / 20)
            score_sell += 2.5 * max(0, (rsi - 50) / 20)

    # --- Bollinger Bands(14) weight 2.5 ---
    if len(closes) >= 14:
        bb_mid = closes[-14:].mean()
        bb_std = closes[-14:].std()
        bb_up  = bb_mid + 2 * bb_std
        bb_dn  = bb_mid - 2 * bb_std
        price  = closes[-1]
        if bb_std > 0:
            pct = (price - bb_dn) / (bb_up - bb_dn)
            if pct < 0.2:  score_buy  += 2.5
            elif pct > 0.8: score_sell += 2.5

    # --- Stochastic(5,3) weight 2.0 ---
    if len(closes) >= 8:
        h5 = highs[-5:].max(); l5 = lows[-5:].min()
        if h5 != l5:
            k = (closes[-1] - l5) / (h5 - l5) * 100
            if k < 20:   score_buy  += 2.0
            elif k > 80: score_sell += 2.0

    # --- MACD(5,13,4) weight 3.0 ---
    if len(closes) >= 13:
        def ema(arr, n):
            s = arr[0]; k = 2/(n+1)
            for v in arr[1:]: s = v*k + s*(1-k)
            return s
        fast = ema(closes[-13:], 5)
        slow = ema(closes[-13:], 13)
        macd_line = fast - slow
        if macd_line > 0:   score_buy  += 3.0
        elif macd_line < 0: score_sell += 3.0

    # --- EMA(3) vs EMA(8) weight 2.5 ---
    if len(closes) >= 8:
        def ema(arr, n):
            s = arr[0]; k = 2/(n+1)
            for v in arr[1:]: s = v*k + s*(1-k)
            return s
        ema3 = ema(closes[-8:], 3)
        ema8 = ema(closes[-8:], 8)
        if ema3 > ema8:   score_buy  += 2.5
        elif ema3 < ema8: score_sell += 2.5

    # --- Reversal weight 2.0 ---
    if len(closes) >= 5:
        recent  = closes[-3:].mean()
        earlier = closes[-6:-3].mean() if len(closes) >= 6 else closes[-3:].mean()
        if recent < earlier: score_buy  += 2.0
        elif recent > earlier: score_sell += 2.0

    total_weight = 14.5
    confidence_buy  = score_buy  / total_weight * 100
    confidence_sell = score_sell / total_weight * 100

    if confidence_buy > confidence_sell and confidence_buy >= SIGNAL_THRESH:
        return {"signal": "BUY",  "confidence": round(confidence_buy, 1)}
    elif confidence_sell > confidence_buy and confidence_sell >= SIGNAL_THRESH:
        return {"signal": "SELL", "confidence": round(confidence_sell, 1)}
    return {"signal": "WAIT", "confidence": round(max(confidence_buy, confidence_sell), 1)}

# ══════════════════════════════════════════════════════════
# POCKET OPTION WebSocket CLIENT
# ══════════════════════════════════════════════════════════

def _ssl_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _ws_listener(ws, ssid: str):
    """
    Handle all incoming messages from Pocket Option WebSocket.
    Protocol:
      Server: 0{sid}   → Client: 40
      Server: 40{sid}  → Client: SSID (auth message)
      Server: 451-["updateAssets"] → auth confirmed
      Binary data      → balance / order results / candle data
    """
    global _ws_conn
    sent_auth = False
    auth_confirmed = False

    async for msg in ws:
        try:
            if isinstance(msg, bytes):
                await _handle_binary(msg)
            else:
                await _handle_text(ws, msg, ssid, sent_auth, auth_confirmed)
                # Update flags based on response
                if msg.startswith("40{") and "sid" in msg and not sent_auth:
                    sent_auth = True
                elif "updateAssets" in msg and not auth_confirmed:
                    auth_confirmed = True
                    state["connected"] = True
                    log.info("✅ Authenticated with Pocket Option!")
                    _broadcast_state()
                    # Request balance
                    await ws.send('42["sendMessage",{"name":"get-balances","version":"1.0"}]')
        except Exception as e:
            log.error("WS message error: %s", e)


async def _handle_text(ws, msg: str, ssid: str, sent_auth: bool, auth_confirmed: bool):
    """Process text Socket.IO frames."""
    if msg.startswith("0{") and "sid" in msg:
        await ws.send("40")
        log.debug("SND: 40 (namespace connect)")

    elif msg.startswith("40{") and "sid" in msg and not sent_auth:
        await ws.send(ssid)
        log.info("SND: SSID auth message")

    elif msg == "2":
        await ws.send("3")  # pong

    elif "updateAssets" in msg:
        log.debug("updateAssets received → auth confirmed")

    elif "successopenOrder" in msg:
        log.info("Trade opened successfully")

    elif "updateBalance" in msg:
        log.debug("Balance update event: %s", msg[:200])

    elif "NotAuthorized" in msg:
        log.error("❌ Not authorized — SSID/session expired. Update POCKET_SSID.")
        state["error"]     = "Session expired. Please update POCKET_SSID."
        state["connected"] = False

    elif "updateClosedDeals" in msg:
        log.debug("Closed deals update")


async def _handle_binary(data: bytes):
    """Process binary msgpack/JSON frames (balance, orders, candles)."""
    try:
        raw = data.decode("utf-8", errors="replace")
        obj = json.loads(raw)

        if isinstance(obj, dict):
            # Balance update
            if "balance" in obj and "uid" in obj:
                balance = float(obj["balance"])
                is_demo  = bool(obj.get("isDemo", 1))
                state["balance"] = balance
                log.info("💰 Balance: $%.2f (isDemo=%s)", balance, is_demo)
                _broadcast_state()

            # Trade open confirmation
            if obj.get("requestId") == "buy" or "requestId" in obj:
                trade_id  = obj.get("id") or obj.get("requestId")
                asset     = obj.get("asset", "?")
                direction = obj.get("action", obj.get("direction", "?"))
                amount    = obj.get("amount", 0)
                log.info("📋 Trade opened: %s %s %s $%.2f id=%s",
                         asset, direction, TRADE_DURATION, amount, trade_id)
                if trade_id in pending_trades:
                    pending_trades[trade_id]["opened"] = True

        elif isinstance(obj, list):
            # Closed deals / trade results
            for deal in obj:
                if isinstance(deal, dict) and "profit" in deal:
                    _process_closed_deal(deal)

    except (json.JSONDecodeError, UnicodeDecodeError):
        pass  # Binary format not parseable as JSON (msgpack, etc.)


def _process_closed_deal(deal: dict):
    """Handle a closed trade deal."""
    trade_id  = deal.get("id")
    profit    = float(deal.get("profit", 0))
    asset     = deal.get("asset", "?")
    direction = deal.get("action", "?")

    won = profit > 0
    state["total"]   += 1
    state["won"]     += 1 if won else 0
    state["lost"]    += 0 if won else 1
    state["pending"] = max(0, state["pending"] - 1)

    result = "WIN" if won else "LOSS"
    entry = {
        "time":      datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "pair":      asset,
        "direction": direction.upper(),
        "amount":    deal.get("amount", TRADE_AMOUNT),
        "profit":    profit,
        "result":    result,
    }
    trade_log.appendleft(entry)
    log.info("🏁 Trade %s: %s %s → %s profit=%.2f",
             trade_id, asset, direction.upper(), result, profit)
    _broadcast_state()
    if trade_id in pending_trades:
        del pending_trades[trade_id]


async def _place_trade(ws, pair: str, direction: str) -> Optional[str]:
    """Place a real trade via WebSocket."""
    is_demo   = 1 if IS_DEMO else 0
    req_id    = int(time.time() * 1000) % 2147483647
    msg = (
        f'42["openOrder",{{'
        f'"asset":"{pair}",'
        f'"amount":{TRADE_AMOUNT},'
        f'"action":"{direction.lower()}",'
        f'"isDemo":{is_demo},'
        f'"requestId":{req_id},'
        f'"optionType":100,'
        f'"time":{TRADE_DURATION}'
        f'}}]'
    )
    await ws.send(msg)
    pending_trades[str(req_id)] = {
        "pair":       pair,
        "direction":  direction,
        "amount":     TRADE_AMOUNT,
        "opened_at":  time.time(),
    }
    state["pending"] += 1
    state["total"]   += 1  # tentative (corrected on close)
    log.info("🚀 Trade: %s %s $%.2f %ds (reqId=%d)",
             pair, direction.upper(), TRADE_AMOUNT, TRADE_DURATION, req_id)
    return str(req_id)

# ══════════════════════════════════════════════════════════
# TRADING LOOP
# ══════════════════════════════════════════════════════════

async def _subscribe_candles(ws, pair: str):
    """Subscribe to real-time candle stream for a pair."""
    msg = json.dumps(["changeSymbol", {"asset": pair, "period": TRADE_DURATION}])
    await ws.send(f"42{msg}")


async def _trading_loop(ws):
    """Main trading logic — runs every LOOP_SECS seconds."""
    # Subscribe to all pairs
    for pair in TRADING_PAIRS:
        await _subscribe_candles(ws, pair)
        await asyncio.sleep(0.1)

    log.info("Subscribed to %d pairs. Trading loop started.", len(TRADING_PAIRS))

    while state["running"] and state["connected"]:
        # Hour rate-limit reset
        if time.time() - state["hour_ts"] > 3600:
            state["trades_hour"] = 0
            state["hour_ts"]     = time.time()

        if state["trades_hour"] >= MAX_TRADES_HOUR:
            log.warning("Max trades/hour reached (%d). Pausing.", MAX_TRADES_HOUR)
            await asyncio.sleep(LOOP_SECS)
            continue

        # Scan all pairs for a signal
        best_pair       = None
        best_direction  = None
        best_confidence = 0.0

        for pair in TRADING_PAIRS:
            sig = generate_signal(pair)
            if sig["signal"] in ("BUY", "SELL") and sig["confidence"] > best_confidence:
                best_pair       = pair
                best_direction  = "call" if sig["signal"] == "BUY" else "put"
                best_confidence = sig["confidence"]

        if best_pair:
            state["last_signal"] = (
                f"{best_pair} {best_direction.upper()} "
                f"{best_confidence:.0f}%"
            )
            log.info("Signal: %s", state["last_signal"])
            await _place_trade(ws, best_pair, best_direction)
            state["trades_hour"] += 1
        else:
            state["last_signal"] = "WAIT — no strong signal"

        _broadcast_state()
        await asyncio.sleep(LOOP_SECS)


async def _run_bot():
    """Connect to Pocket Option and run bot until stopped."""
    global _ws_conn
    state["error"] = ""

    try:
        ssid, cookie, ws_headers = _get_auth_params()
    except RuntimeError as e:
        state["error"]   = str(e)
        state["running"] = False
        log.error(str(e))
        _broadcast_state()
        return

    log.info("Connecting to %s", WS_URL)
    log.info("Mode: %s | Amount: $%.2f | Duration: %ds",
             TRADE_MODE, TRADE_AMOUNT, TRADE_DURATION)

    while state["running"]:
        try:
            async with websockets.connect(
                WS_URL,
                ssl=_ssl_ctx(),
                extra_headers=ws_headers,
                ping_interval=20,
                ping_timeout=30,
            ) as ws:
                _ws_conn = ws
                state["connected"] = False

                # Listener + trading tasks run concurrently
                listener = asyncio.create_task(_ws_listener(ws, ssid))

                # Wait for auth confirmation before starting trading
                for _ in range(60):  # up to 30s
                    if state["connected"]:
                        break
                    await asyncio.sleep(0.5)

                if state["connected"]:
                    trader = asyncio.create_task(_trading_loop(ws))
                    await asyncio.gather(listener, trader)
                else:
                    listener.cancel()
                    log.error("Auth timed out — session may be expired")
                    state["error"] = "Auth timeout — update POCKET_SSID"
                    state["running"] = False
                    break

        except Exception as e:
            log.error("WebSocket error: %s", e)
            state["connected"] = False
            if state["running"]:
                log.info("Reconnecting in 5s…")
                await asyncio.sleep(5)

    state["connected"] = False
    state["running"]   = False
    _ws_conn = None
    log.info("Bot stopped.")
    _broadcast_state()

# ══════════════════════════════════════════════════════════
# WEBSOCKET BROADCAST (dashboard ↔ server)
# ══════════════════════════════════════════════════════════
_dashboard_clients: list = []


def _broadcast_state():
    payload = json.dumps({
        **state,
        "trades":  list(trade_log)[:20],
        "winrate": (
            round(state["won"] / state["total"] * 100, 1)
            if state["total"] else 0
        ),
    })
    for q in list(_dashboard_clients):
        try:
            q.put_nowait(payload)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════════════════
app = FastAPI(title="PO Trader", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _start_bot_thread():
    """Start async bot in a new event loop in a background thread."""
    global _ws_loop
    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)
    _ws_loop.run_until_complete(_run_bot())
    _ws_loop.close()


@app.post("/api/start")
async def api_start():
    if state["running"]:
        return {"ok": False, "msg": "Already running"}
    state["running"] = True
    t = threading.Thread(target=_start_bot_thread, daemon=True)
    t.start()
    return {"ok": True, "msg": "Bot started"}


@app.post("/api/stop")
async def api_stop():
    if not state["running"]:
        return {"ok": False, "msg": "Not running"}
    state["running"]   = False
    state["connected"] = False
    return {"ok": True, "msg": "Stop signal sent"}


@app.get("/api/status")
async def api_status():
    return {
        **state,
        "trades":  list(trade_log)[:20],
        "winrate": (
            round(state["won"] / state["total"] * 100, 1)
            if state["total"] else 0
        ),
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: FWebSocket):
    await websocket.accept()
    import asyncio
    q: asyncio.Queue = asyncio.Queue()
    _dashboard_clients.append(q)
    try:
        # Send current state immediately
        await websocket.send_text(json.dumps({
            **state,
            "trades":  list(trade_log)[:20],
            "winrate": (
                round(state["won"] / state["total"] * 100, 1)
                if state["total"] else 0
            ),
        }))
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_text(msg)
            except asyncio.TimeoutError:
                await websocket.send_text('{"ping":1}')
    except WebSocketDisconnect:
        pass
    finally:
        _dashboard_clients.remove(q)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PO Algo Trader v""" + VERSION + """</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',Arial,sans-serif;background:#0a0e1a;color:#e0e6f0;min-height:100vh}
  .header{background:linear-gradient(135deg,#1a237e,#0d47a1);padding:18px 24px;display:flex;
    align-items:center;justify-content:space-between;box-shadow:0 2px 12px #0005}
  .logo{font-size:1.4em;font-weight:700;letter-spacing:1px}
  .logo span{color:#4fc3f7}
  .badge{padding:4px 12px;border-radius:20px;font-size:.75em;font-weight:600;text-transform:uppercase}
  .badge.demo{background:#ff9800;color:#000}
  .badge.real{background:#f44336;color:#fff}
  .main{max-width:1100px;margin:0 auto;padding:24px 16px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}
  .card{background:#151c2e;border-radius:12px;padding:16px 18px;border:1px solid #1e2a45}
  .card .label{font-size:.72em;color:#7986a8;text-transform:uppercase;letter-spacing:.6px}
  .card .value{font-size:1.6em;font-weight:700;margin-top:4px}
  .card .value.green{color:#4caf50}
  .card .value.red{color:#ef5350}
  .card .value.blue{color:#4fc3f7}
  .card .value.yellow{color:#ffb300}
  .controls{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
  .btn{padding:12px 28px;border:none;border-radius:8px;font-size:1em;font-weight:600;cursor:pointer;
    transition:all .2s;letter-spacing:.4px}
  .btn-start{background:linear-gradient(135deg,#43a047,#2e7d32);color:#fff}
  .btn-start:hover{transform:translateY(-1px);box-shadow:0 4px 16px #4caf5040}
  .btn-stop{background:linear-gradient(135deg,#e53935,#b71c1c);color:#fff}
  .btn-stop:hover{transform:translateY(-1px);box-shadow:0 4px 16px #ef535040}
  .btn:disabled{opacity:.5;cursor:not-allowed;transform:none !important}
  .status-bar{padding:10px 16px;border-radius:8px;margin-bottom:20px;font-size:.88em;
    display:flex;align-items:center;gap:10px}
  .status-bar.connected{background:#1b3a1b;border:1px solid #2e7d32;color:#81c784}
  .status-bar.disconnected{background:#2a1515;border:1px solid #7f0000;color:#ef9a9a}
  .status-bar.running{background:#1a2a3a;border:1px solid #0288d1;color:#81d4fa}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
  .dot.green{background:#4caf50;box-shadow:0 0 6px #4caf50}
  .dot.red{background:#ef5350;box-shadow:0 0 6px #ef5350}
  .dot.blue{background:#4fc3f7;animation:pulse 1.2s ease-in-out infinite;box-shadow:0 0 6px #4fc3f7}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .table-wrap{background:#151c2e;border-radius:12px;border:1px solid #1e2a45;overflow:hidden}
  .table-wrap h3{padding:14px 18px;font-size:.95em;color:#7986a8;border-bottom:1px solid #1e2a45}
  table{width:100%;border-collapse:collapse;font-size:.85em}
  th{padding:10px 14px;text-align:left;color:#546e7a;font-weight:600;text-transform:uppercase;
    font-size:.75em;letter-spacing:.5px;background:#111827}
  td{padding:10px 14px;border-bottom:1px solid #1a2236}
  tr:last-child td{border-bottom:none}
  .win{color:#4caf50;font-weight:700}
  .loss{color:#ef5350;font-weight:700}
  .signal{padding:10px 14px;background:#0d1526;border-radius:8px;font-size:.9em;color:#90caf9;
    margin-bottom:20px;border:1px solid #1e2a45}
  .error-msg{padding:10px 14px;background:#2a1515;border-radius:8px;color:#ef9a9a;
    margin-bottom:20px;border:1px solid #7f0000;font-size:.88em;display:none}
</style>
</head>
<body>
<div class="header">
  <div class="logo">🤖 PO Algo <span>Trader</span></div>
  <span id="mode-badge" class="badge demo">""" + TRADE_MODE + """</span>
</div>
<div class="main">
  <div id="error-msg" class="error-msg"></div>

  <div class="cards">
    <div class="card">
      <div class="label">Balance</div>
      <div class="value blue" id="balance">$0.00</div>
    </div>
    <div class="card">
      <div class="label">Total Trades</div>
      <div class="value" id="total">0</div>
    </div>
    <div class="card">
      <div class="label">Won</div>
      <div class="value green" id="won">0</div>
    </div>
    <div class="card">
      <div class="label">Lost</div>
      <div class="value red" id="lost">0</div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value yellow" id="winrate">0%</div>
    </div>
    <div class="card">
      <div class="label">Pending</div>
      <div class="value" id="pending">0</div>
    </div>
  </div>

  <div id="status-bar" class="status-bar disconnected">
    <span class="dot red" id="dot"></span>
    <span id="status-text">Disconnected</span>
  </div>

  <div class="signal">📡 Last Signal: <b id="last-signal">—</b></div>

  <div class="controls">
    <button class="btn btn-start" id="btn-start" onclick="startBot()">▶ START</button>
    <button class="btn btn-stop" id="btn-stop" onclick="stopBot()" disabled>■ STOP</button>
  </div>

  <div class="table-wrap">
    <h3>Trade History</h3>
    <table>
      <thead>
        <tr>
          <th>Time</th><th>Pair</th><th>Dir</th><th>Amount</th><th>Profit</th><th>Result</th>
        </tr>
      </thead>
      <tbody id="trade-body">
        <tr><td colspan="6" style="color:#546e7a;text-align:center;padding:20px">
          No trades yet — press START to begin
        </td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
let ws = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if (d.ping) return;
    update(d);
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

function update(d) {
  document.getElementById('balance').textContent = '$' + (d.balance||0).toFixed(2);
  document.getElementById('total').textContent   = d.total || 0;
  document.getElementById('won').textContent     = d.won || 0;
  document.getElementById('lost').textContent    = d.lost || 0;
  document.getElementById('winrate').textContent = (d.winrate||0) + '%';
  document.getElementById('pending').textContent = d.pending || 0;
  document.getElementById('last-signal').textContent = d.last_signal || '—';

  const bar  = document.getElementById('status-bar');
  const dot  = document.getElementById('dot');
  const txt  = document.getElementById('status-text');
  const btnS = document.getElementById('btn-start');
  const btnX = document.getElementById('btn-stop');
  const errDiv = document.getElementById('error-msg');

  if (d.error) {
    errDiv.style.display = 'block';
    errDiv.textContent = '⚠ ' + d.error;
  } else {
    errDiv.style.display = 'none';
  }

  if (d.connected) {
    bar.className = 'status-bar connected';
    dot.className = 'dot green';
    txt.textContent = 'Connected to Pocket Option (' + (d.mode||'DEMO') + ')';
  } else if (d.running) {
    bar.className = 'status-bar running';
    dot.className = 'dot blue';
    txt.textContent = 'Connecting…';
  } else {
    bar.className = 'status-bar disconnected';
    dot.className = 'dot red';
    txt.textContent = 'Disconnected';
  }

  btnS.disabled = d.running;
  btnX.disabled = !d.running;

  if (d.trades && d.trades.length) {
    const tbody = document.getElementById('trade-body');
    tbody.innerHTML = d.trades.map(t => {
      const cls = t.result === 'WIN' ? 'win' : 'loss';
      const profit = t.profit >= 0 ? '+$' + t.profit.toFixed(2) : '-$' + Math.abs(t.profit).toFixed(2);
      return '<tr>' +
        '<td>' + t.time + '</td>' +
        '<td>' + t.pair + '</td>' +
        '<td>' + t.direction + '</td>' +
        '<td>$' + t.amount.toFixed(2) + '</td>' +
        '<td class="' + cls + '">' + profit + '</td>' +
        '<td class="' + cls + '">' + t.result + '</td>' +
        '</tr>';
    }).join('');
  }
}

async function startBot() {
  document.getElementById('btn-start').disabled = true;
  const r = await fetch('/api/start', {method:'POST'});
  const d = await r.json();
  if (!d.ok) { alert(d.msg); document.getElementById('btn-start').disabled = false; }
}

async function stopBot() {
  document.getElementById('btn-stop').disabled = true;
  await fetch('/api/stop', {method:'POST'});
}

connect();
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    log.info("PO Trader v%s starting on port %d", VERSION, port)
    uvicorn.run(app, host="0.0.0.0", port=port)
