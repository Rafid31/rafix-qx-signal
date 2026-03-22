// QX Session v4 — Pure WebSocket with correct message parsing
const WebSocket = require('ws');

const ALL_PAIRS = [
  'EURUSD_otc','GBPUSD_otc','AUDUSD_otc','USDCAD_otc','USDCHF_otc',
  'NZDUSD_otc','EURGBP_otc','EURJPY_otc','GBPJPY_otc','AUDJPY_otc',
  'USDBRL_otc','USDMXN_otc','USDINR_otc','USDARS_otc','USDBDT_otc'
];

class QXSession {
  constructor({ token, onTick, onVerifyNeeded, onDisconnect }) {
    this.token = token;
    this.onTick = onTick;
    this.onDisconnect = onDisconnect;
    this.ws = null;
    this.running = false;
    this.tickCount = 0;
    this.endpointIndex = 0;
    this.reconnectDelay = 3000;
    this.endpoints = [
      'wss://ws2.qxbroker.com/socket.io/?EIO=4&transport=websocket',
      'wss://ws.qxbroker.com/socket.io/?EIO=4&transport=websocket',
      'wss://ws2.market-qx.trade/socket.io/?EIO=4&transport=websocket',
      'wss://ws.market-qx.trade/socket.io/?EIO=4&transport=websocket',
    ];
  }

  async start() {
    this.running = true;
    this._connect();
  }

  _connect() {
    if (!this.running) return;
    const url = this.endpoints[this.endpointIndex % this.endpoints.length];
    console.log(`[QX-WS] Connecting to ${url}`);
    this.ws = new WebSocket(url, {
      headers: {
        'Cookie': `session=${this.token}`,
        'Origin': 'https://qxbroker.com',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
      },
      handshakeTimeout: 20000,
    });
    this.ws.on('open', () => {
      console.log('[QX-WS] Connected ✅');
      this.reconnectDelay = 3000;
      this.ws.send('40');
      setTimeout(() => this._subscribe(), 2000);
      this._pingInterval = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) this.ws.send('3');
      }, 20000);
    });
    this.ws.on('message', (data) => this._handle(data.toString()));
    this.ws.on('close', () => {
      console.log(`[QX-WS] Closed — retry in ${this.reconnectDelay}ms`);
      clearInterval(this._pingInterval);
      this.endpointIndex++;
      if (this.running) setTimeout(() => this._connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, 30000);
    });
    this.ws.on('error', (err) => console.log('[QX-WS] Error:', err.message));
  }

  _subscribe() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    // Request instruments list — QX sends all pair data in response
    this.ws.send('42["instruments/list",{}]');
    // Subscribe to real-time candles for each pair
    ALL_PAIRS.forEach(sym => {
      this.ws.send(`42["chart/subscribe",{"asset":"${sym}","period":60}]`);
    });
    // Subscribe to real-time tick stream
    this.ws.send(`42["tick/subscribe",{}]`);
    console.log(`[QX-WS] Subscribed to ${ALL_PAIRS.length} pairs`);
  }

  _handle(msg) {
    try {
      if (msg === '2') { this.ws?.send('3'); return; }

      // Log first 3 unique event types to understand format
      if (this.tickCount === 0 && msg.length > 2) {
        console.log('[QX-WS] RAW: ' + msg.slice(0, 300));
      }
      // Handle 451- multi-packet (instruments/list, ticks)
      if (msg.startsWith('451-')) {
        try {
          const payload = JSON.parse(msg.slice(4));
          if (!Array.isArray(payload)) return;
          const event = payload[0], data = payload[1];
          if (event === 'instruments/list' && Array.isArray(data)) {
            data.forEach(inst => {
              if (!Array.isArray(inst)) return;
              const sym = inst[1];
              if (!sym || !sym.includes('_otc')) return;
              for (let i = 3; i < Math.min(inst.length, 20); i++) {
                const p = parseFloat(inst[i]);
                if (p > 0.0001 && p < 1000000 && !isNaN(p)) {
                  if (this.onTick) this.onTick(sym, p, Date.now());
                  this.tickCount++; break;
                }
              }
            });
            console.log('[QX-WS] instruments/list — ' + this.tickCount + ' prices');
          }
          if (['tick','price','quote'].includes(event) && data) {
            const sym = data.asset || data.symbol;
            const price = parseFloat(data.price || data.value);
            if (sym && price > 0 && this.onTick) { this.onTick(sym, price, Date.now()); this.tickCount++; }
          }
        } catch(e) {}
        return;
      }
      if (!msg.startsWith('42')) return;

      // QX uses two formats:
      // Format A: 42["event", {object}]
      // Format B: 451-["event", [array of data]]  (multi-packet)
      let payload;
      if (msg.startsWith('451-')) {
        payload = JSON.parse(msg.slice(4));
      } else {
        payload = JSON.parse(msg.slice(2));
      }

      if (!Array.isArray(payload) || payload.length < 2) return;
      const event = payload[0];
      const data = payload[1];

      // ── TICK / PRICE events ──────────────────────────────────
      if (event === 'tick' || event === 'price' || event === 'tick/price') {
        // Format: { asset: "EURUSD_otc", price: 1.1050, time: 1234567 }
        const sym = data.asset || data.symbol;
        const price = parseFloat(data.price || data.value);
        const ts = data.time ? (data.time > 1e10 ? data.time : data.time * 1000) : Date.now();
        if (sym && price > 0 && this.onTick) {
          this.onTick(sym, price, ts);
          this.tickCount++;
          if (this.tickCount % 500 === 1) console.log(`[QX-WS] ${this.tickCount} ticks — ${sym}: ${price}`);
        }
      }

      // ── INSTRUMENTS LIST (contains current prices) ───────────
      // Format B: 451-["instruments/list", [array of instruments]]
      // Each instrument: [id, symbol, name, type, ...]
      if (event === 'instruments/list' && Array.isArray(data)) {
        data.forEach(inst => {
          if (!Array.isArray(inst)) return;
          // inst[1] = symbol, inst[14] might be last price
          const sym = inst[1];
          if (!sym || !sym.includes('_otc')) return;
          // Look for price in the array
          const price = parseFloat(inst[14] || inst[5] || inst[4] || 0);
          if (price > 0 && this.onTick) {
            this.onTick(sym, price, Date.now());
            this.tickCount++;
          }
        });
        if (this.tickCount > 0) console.log(`[QX-WS] instruments/list: got ${this.tickCount} prices`);
      }

      // ── CHART / CANDLE DATA ──────────────────────────────────
      if (event === 'chart/update' || event === 'candle' || event === 'chart/candle') {
        const sym = data.asset || data.symbol;
        const price = parseFloat(data.close || data.price || data.value);
        const ts = data.time ? (data.time > 1e10 ? data.time : data.time * 1000) : Date.now();
        if (sym && price > 0 && this.onTick) {
          this.onTick(sym, price, ts);
          this.tickCount++;
        }
      }

      // ── HISTORY CANDLES (batch) ──────────────────────────────
      if (event === 'history/candles' || event === 'candle/history') {
        const sym = data.asset || data.symbol;
        const candles = data.candles || data.data || [];
        if (sym && candles.length > 0) {
          candles.forEach(c => {
            const price = parseFloat(c.close || c.c);
            const ts = (c.time || c.t || 0);
            const timestamp = ts > 1e10 ? ts : ts * 1000;
            if (price > 0 && this.onTick) this.onTick(sym, price, timestamp || Date.now());
          });
          console.log(`[QX-WS] ${candles.length} history candles for ${sym}`);
        }
      }

    } catch(e) {}
  }

  stop() {
    this.running = false;
    clearInterval(this._pingInterval);
    this.ws?.terminate();
  }
}

module.exports = { QXSession };
