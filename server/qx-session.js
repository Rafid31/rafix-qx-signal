// PO Session — Pocket Option WebSocket
// NO IP BLOCKS, NO LOGIN, NO VERIFICATION CODES — works 24/7 from any server
const WebSocket = require('ws');
const https = require('https');

const ENDPOINTS = [
  'wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket',
  'wss://demo-api-msk.po.market/socket.io/?EIO=4&transport=websocket',
  'wss://demo-api-us-south.po.market/socket.io/?EIO=4&transport=websocket',
];

// PO OTC pair names (uppercase, PO format)
const PO_OTC_PAIRS = [
  'EURUSD_OTC','GBPUSD_OTC','AUDUSD_OTC','USDCAD_OTC','USDCHF_OTC',
  'NZDUSD_OTC','EURGBP_OTC','EURJPY_OTC','GBPJPY_OTC','AUDJPY_OTC',
  'USDJPY_OTC','EURNZD_OTC','EURCAD_OTC','GBPCAD_OTC','CADJPY_OTC'
];

// Convert PO format to server format: EURUSD_OTC -> EURUSD_otc
const toOtc = s => s.replace('_OTC','_otc');

class QXSession {
  constructor({ token, onTick, onVerifyNeeded, onDisconnect }) {
    this.onTick = onTick;
    this.onDisconnect = onDisconnect;
    this.ws = null;
    this.running = false;
    this.tickCount = 0;
    this.endpointIdx = 0;
    this.reconnectDelay = 5000;
    this.msgCount = 0;
    this.assetIds = {}; // symbol -> id for history requests
  }

  async start() {
    this.running = true;
    this._connect();
  }

  _connect() {
    if (!this.running) return;
    const url = ENDPOINTS[this.endpointIdx % ENDPOINTS.length];
    console.log(`[PO-WS] Connecting to ${url}`);
    this.msgCount = 0;

    this.ws = new WebSocket(url, {
      headers: {
        'Origin': 'https://pocketoption.com',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
      },
      handshakeTimeout: 20000,
    });

    this.ws.on('open', () => {
      console.log('[PO-WS] Connected ✅');
      this.reconnectDelay = 5000;
      this.ws.send('40');
      // Ping every 20s
      this._pingInterval = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) this.ws.send('3');
      }, 20000);
    });

    this.ws.on('message', (data) => this._handle(data.toString()));

    this.ws.on('close', () => {
      console.log(`[PO-WS] Closed — retry in ${this.reconnectDelay}ms`);
      clearInterval(this._pingInterval);
      clearInterval(this._historyInterval);
      this.endpointIdx++;
      if (this.running) setTimeout(() => this._connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, 30000);
    });

    this.ws.on('error', (err) => console.log('[PO-WS] Error:', err.message));
  }

  _handle(msg) {
    try {
      this.msgCount++;
      if (msg === '2') { this.ws?.send('3'); return; }
      if (msg.startsWith('0') || msg.startsWith('40')) return;

      // Message 4 is the big assets array (comes after 451- header)
      // Format: [[id, symbol, name, type, category, payout, ...], ...]
      if (msg.startsWith('[[') || (msg.startsWith('[') && msg.includes('_OTC'))) {
        this._parseAssetList(msg);
        return;
      }

      // 451- multi-packet header — data follows in next message
      if (msg.startsWith('451-')) return;

      // Regular Socket.IO events
      if (!msg.startsWith('42')) return;
      const payload = JSON.parse(msg.slice(2));
      if (!Array.isArray(payload)) return;
      const event = payload[0], data = payload[1];
      if (!event) return;

      // History candles response
      if (event === 'loadHistoryPeriod' || event === 'history' || event === 'candles') {
        this._parseHistory(data);
      }

      // Live stream tick
      if (event === 'updateStream' || event === 'tick' || event === 'quote') {
        const sym = data?.asset || data?.symbol;
        const price = parseFloat(data?.price || data?.value || data?.c);
        if (sym && price > 0 && this.onTick) {
          this.onTick(toOtc(sym), price, Date.now());
          this.tickCount++;
        }
      }

      // Asset data update
      if (event === 'updateAsset' || event === 'updateAssets') {
        if (Array.isArray(data)) this._parseAssetList(JSON.stringify(data));
      }

    } catch(e) {}
  }

  _parseAssetList(msg) {
    try {
      const arr = JSON.parse(msg);
      if (!Array.isArray(arr)) return;
      let prices = 0;
      arr.forEach(inst => {
        if (!Array.isArray(inst) || inst.length < 5) return;
        const sym = String(inst[1] || '');
        if (!sym.includes('_OTC') && !sym.includes('_otc')) return;
        const id = inst[0];
        const payout = inst[5];
        // Store asset ID for history requests
        this.assetIds[sym] = id;
        // Current price is at index 8 or nearby — try to find it
        // Based on log: [id, sym, name, type, cat, payout, period, ?, payout2, isOtc, ...]
        // Price comes from history, not asset list on PO
        prices++;
      });
      if (prices > 0) {
        console.log(`[PO-WS] Assets loaded: ${prices} OTC pairs`);
        // Now request history for all OTC pairs
        this._requestAllHistory();
        // Re-request every 60s for fresh candles
        clearInterval(this._historyInterval);
        this._historyInterval = setInterval(() => this._requestAllHistory(), 60000);
      }
    } catch(e) {}
  }

  _requestAllHistory() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const t = Math.floor(Date.now() / 1000);
    PO_OTC_PAIRS.forEach((sym, i) => {
      setTimeout(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(`42["loadHistoryPeriod",{"asset":"${sym}","period":60,"time":${t},"index":1,"count":100}]`);
        }
      }, i * 200); // stagger to avoid flooding
    });
    console.log(`[PO-WS] Requested history for ${PO_OTC_PAIRS.length} pairs`);
  }

  _parseHistory(data) {
    try {
      if (!data) return;
      const sym = data.asset || data.symbol;
      const candles = data.candles || data.data || data.history || [];
      if (!sym || !Array.isArray(candles) || candles.length === 0) return;
      const otcSym = toOtc(sym);
      let count = 0;
      candles.forEach(c => {
        // PO candle format: {time, open, close, high, low} or {time, value}
        const price = parseFloat(c.close || c.value || c.c || c[4]);
        const ts = (c.time || c.t || c[0] || 0);
        const timestamp = ts > 1e10 ? ts : ts * 1000;
        if (price > 0 && this.onTick) {
          this.onTick(otcSym, price, timestamp || Date.now());
          count++;
        }
      });
      if (count > 0) {
        this.tickCount += count;
        if (this.tickCount % 200 === 0 || count > 50)
          console.log(`[PO-WS] ${count} candles for ${sym} | total ticks: ${this.tickCount}`);
      }
    } catch(e) {}
  }

  stop() {
    this.running = false;
    clearInterval(this._pingInterval);
    clearInterval(this._historyInterval);
    this.ws?.terminate();
  }
}

module.exports = { QXSession };
