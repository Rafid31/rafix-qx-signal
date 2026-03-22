// QX Session v5 — Twelve Data API (no login, no IP blocks, 24/7)
// Real forex pairs: OTC follows real prices exactly
const https = require('https');

const API_KEY = process.env.TWELVE_API_KEY || 'e7648cf5327e43d482cc42bfe6260eec';

// Map OTC pairs to Twelve Data symbols
const PAIR_MAP = {
  'EURUSD_otc': 'EUR/USD', 'GBPUSD_otc': 'GBP/USD', 'AUDUSD_otc': 'AUD/USD',
  'USDCAD_otc': 'USD/CAD', 'USDCHF_otc': 'USD/CHF', 'NZDUSD_otc': 'NZD/USD',
  'EURGBP_otc': 'EUR/GBP', 'EURJPY_otc': 'EUR/JPY', 'GBPJPY_otc': 'GBP/JPY',
  'AUDJPY_otc': 'AUD/JPY', 'USDJPY_otc': 'USD/JPY', 'USDMXN_otc': 'USD/MXN',
  'USDINR_otc': 'USD/INR', 'USDCNH_otc': 'USD/CNH', 'EURCAD_otc': 'EUR/CAD'
};

class QXSession {
  constructor({ token, onTick, onVerifyNeeded, onDisconnect }) {
    this.onTick = onTick;
    this.running = false;
    this.tickCount = 0;
  }

  async start() {
    this.running = true;
    console.log('[TwelveData] Starting — polling all pairs every 60s');
    await this._fetchAll();
    // Poll every 60 seconds
    this._interval = setInterval(() => this._fetchAll(), 60000);
  }

  async _fetchAll() {
    const symbols = Object.values(PAIR_MAP).join(',');
    const url = `https://api.twelvedata.com/time_series?symbol=${encodeURIComponent(symbols)}&interval=1min&outputsize=30&apikey=${API_KEY}`;
    try {
      const data = await this._get(url);
      let got = 0;
      for (const [otcSym, tdSym] of Object.entries(PAIR_MAP)) {
        const series = data[tdSym];
        if (!series || series.status === 'error' || !series.values) continue;
        series.values.reverse().forEach(candle => {
          const price = parseFloat(candle.close);
          const ts = new Date(candle.datetime + 'Z').getTime() || Date.now();
          if (price > 0 && this.onTick) {
            this.onTick(otcSym, price, ts);
            got++;
          }
        });
      }
      this.tickCount += got;
      console.log(`[TwelveData] Fetched ${got} candles across ${Object.keys(PAIR_MAP).length} pairs`);
    } catch(e) {
      console.log('[TwelveData] Error:', e.message);
    }
  }

  _get(url) {
    return new Promise((resolve, reject) => {
      https.get(url, res => {
        let d = '';
        res.on('data', c => d += c);
        res.on('end', () => { try { resolve(JSON.parse(d)); } catch(e) { reject(e); } });
      }).on('error', reject);
    });
  }

  stop() {
    this.running = false;
    clearInterval(this._interval);
  }
}

module.exports = { QXSession };
