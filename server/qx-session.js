// QX Session v3 — Pure WebSocket, NO Puppeteer, NO login, NO verification codes
// Connects directly to QX WebSocket using browser session token
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
    // Send Socket.IO connect to namespace
    this.ws.send('40');
    setTimeout(() => {
      ALL_PAIRS.forEach(sym => {
        // Request candle history
        this.ws.send(`42["history/generate",{"asset":"${sym}","period":60,"count":200}]`);
      });
      // Subscribe to live quotes for all pairs
      this.ws.send(`42["quote/subscribe",{"assets":${JSON.stringify(ALL_PAIRS)}}]`);
      // Also try instruments subscription
      ALL_PAIRS.forEach(sym => {
        this.ws.send(`42["instruments/follow",{"asset":"${sym}"}]`);
      });
      console.log(`[QX-WS] Subscribed to ${ALL_PAIRS.length} pairs`);
    }, 1000);
  }

  _handle(msg) {
    try {
      if (msg === '2') { this.ws?.send('3'); return; }
      // Log ALL messages until we get first tick — to understand QX format
      if (this.tickCount === 0) {
        console.log(`[QX-WS] RAW: ${msg.slice(0, 200)}`);
      }
      if (!msg.startsWith('42')) return;
      const payload = JSON.parse(msg.slice(2));
      const event = payload[0], data = payload[1];
      if (!event || !data) return;

      // Real-time tick events
      if (['tick','price','quote/price','tick/price','instruments/update'].includes(event)) {
        const sym = data.asset || data.symbol || data.id;
        const price = parseFloat(data.price || data.value || data.ask);
        const ts = data.time ? (data.time > 1e10 ? data.time : data.time * 1000) : Date.now();
        if (sym && price > 0 && this.onTick) {
          this.onTick(sym, price, ts);
          this.tickCount++;
          if (this.tickCount % 500 === 1) console.log(`[QX-WS] ${this.tickCount} ticks — ${sym}: ${price}`);
        }
      }

      // Historical candles (batch load on connect)
      if (['history/candles','history/generate','candles'].includes(event)) {
        const sym = data.asset || data.symbol;
        const candles = data.candles || data.data || [];
        if (sym && Array.isArray(candles) && candles.length > 0) {
          candles.forEach(c => {
            const price = parseFloat(c.close || c.c || c[4]);
            const ts = (c.time || c.t || c[0] || 0);
            const timestamp = ts > 1e10 ? ts : ts * 1000;
            if (price > 0 && this.onTick) this.onTick(sym, price, timestamp || Date.now());
          });
          console.log(`[QX-WS] Loaded ${candles.length} candles for ${sym}`);
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
