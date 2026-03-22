// QX Session v4 — Pure WebSocket, NO Puppeteer
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
    this.ws.send('42["instruments/list",{}]');
    ALL_PAIRS.forEach(sym => {
      this.ws.send(`42["chart/subscribe",{"asset":"${sym}","period":60}]`);
    });
    this.ws.send(`42["tick/subscribe",{}]`);
    console.log(`[QX-WS] Subscribed to ${ALL_PAIRS.length} pairs`);
    // Re-request instruments/list every 90s to keep prices fresh
    clearInterval(this._listInterval);
    this._listInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send('42["instruments/list",{}]');
      }
    }, 90000);
  }

  _handle(msg) {
    try {
      if (msg === '2') { this.ws?.send('3'); return; }

      // Handle 451- multi-packet (instruments/list etc)
      if (msg.startsWith('451-')) {
        const payload = JSON.parse(msg.slice(4));
        if (!Array.isArray(payload)) return;
        const event = payload[0];
        const data = payload[1];

        if (event === 'instruments/list' && Array.isArray(data)) {
          let extracted = 0;
          data.forEach(inst => {
            if (!Array.isArray(inst)) return;
            const sym = inst[1];
            if (!sym || !sym.includes('_otc')) return;
            // From logs: [id,sym,name,type,?,payout,period,?,?,?,?,?,[],timestamp,bool,[candles],?,PRICE,...]
            // Index 17 confirmed as price from log: "4,38,60,30,3,1,0,0,[],ts,true,[],10,3.95"
            const price = parseFloat(inst[17]) || parseFloat(inst[16]) || parseFloat(inst[18]);
            if (price > 0 && price < 1000000) {
              if (this.onTick) this.onTick(sym, price, Date.now());
              this.tickCount++;
              extracted++;
            }
          });
          if (extracted > 0) console.log(`[QX-WS] instruments/list — ${extracted} prices ✅`);
        }

        if (['tick','price','quote'].includes(event) && data) {
          const sym = data.asset || data.symbol;
          const price = parseFloat(data.price || data.value);
          if (sym && price > 0 && this.onTick) { this.onTick(sym, price, Date.now()); this.tickCount++; }
        }
        return;
      }

      if (!msg.startsWith('42')) return;
      const payload = JSON.parse(msg.slice(2));
      if (!Array.isArray(payload) || payload.length < 2) return;
      const event = payload[0];
      const data = payload[1];

      if (['tick','price','quote/price','instruments/update','chart/update'].includes(event)) {
        const sym = data.asset || data.symbol || data.id;
        const price = parseFloat(data.price || data.value || data.close);
        const ts = data.time ? (data.time > 1e10 ? data.time : data.time * 1000) : Date.now();
        if (sym && price > 0 && this.onTick) {
          this.onTick(sym, price, ts);
          this.tickCount++;
          if (this.tickCount % 500 === 1) console.log(`[QX-WS] ${this.tickCount} ticks — ${sym}: ${price}`);
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
