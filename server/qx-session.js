// QX Session v2 — Dual mode: WebSocket intercept + React store polling
const puppeteer = require('puppeteer');

class QXSession {
  constructor({ email, pass, onTick, onVerifyNeeded, onDisconnect }) {
    this.email = email;
    this.pass = pass;
    this.onTick = onTick;
    this.onVerifyNeeded = onVerifyNeeded;
    this.onDisconnect = onDisconnect;
    this.browser = null;
    this.page = null;
    this.running = false;
    this.tickCount = 0;
  }

  async start() {
    console.log('[QX] Launching browser...');
    this.browser = await puppeteer.launch({
      headless: 'new',
      args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
             '--disable-gpu','--no-first-run','--single-process','--no-zygote']
    });
    this.page = await this.browser.newPage();
    await this.page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36');

    // WebSocket intercept
    await this.page.evaluateOnNewDocument(() => {
      const OrigWS = window.WebSocket;
      window.__rfxTicks = 0;
      window.WebSocket = function(...args) {
        const ws = new OrigWS(...args);
        ws.addEventListener('message', (e) => {
          try {
            if (window.__rfxOnTick) window.__rfxOnTick(e.data);
            window.__rfxTicks++;
          } catch(_) {}
        });
        ws.addEventListener('close', () => {
          try { if (window.__rfxOnDC) window.__rfxOnDC(); } catch(_) {}
        });
        return ws;
      };
      Object.setPrototypeOf(window.WebSocket, OrigWS);
      window.WebSocket.prototype = OrigWS.prototype;
    });

    await this.page.exposeFunction('__rfxOnTick', (raw) => this._parseTick(raw));
    await this.page.exposeFunction('__rfxOnDC', () => {
      console.log('[QX] WS closed');
      if (this.onDisconnect) this.onDisconnect();
    });

    await this._login();
    this.running = true;

    // START REACT STORE POLLING — runs every 2 seconds as backup
    this._startStorePoll();

    // Keep-alive refresh every 4 hours
    setInterval(() => {
      if (!this.running) return;
      console.log('[QX] Keep-alive refresh...');
      this.page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
    }, 4 * 60 * 60 * 1000);

    console.log('[QX] Session active — collecting all OTC pairs');
  }

  // Poll React store every 2s for live prices
  _startStorePoll() {
    setInterval(async () => {
      if (!this.running || !this.page) return;
      try {
        const prices = await this.page.evaluate(() => {
          try {
            // Find Redux store via React fiber
            const app = document.querySelector('.app');
            if (!app) return null;
            const fk = Object.keys(app).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternals'));
            if (!fk) return null;
            let fib = app[fk], depth = 0;
            while (fib && depth < 300) {
              try {
                const v = fib.memoizedProps && fib.memoizedProps.value;
                if (v && v.store && typeof v.store.getState === 'function') {
                  const st = v.store.getState();
                  if (st && st.quotes && st.quotes.quoteBySymbol) {
                    const result = {};
                    const qbs = st.quotes.quoteBySymbol;
                    Object.keys(qbs).forEach(sym => {
                      if (sym.includes('_otc')) {
                        const q = qbs[sym];
                        if (q && (q.price || q.value)) {
                          result[sym] = q.price || q.value;
                        }
                      }
                    });
                    return Object.keys(result).length > 0 ? result : null;
                  }
                }
              } catch(e) {}
              fib = fib.return;
              depth++;
            }
            return null;
          } catch(e) { return null; }
        });

        if (prices) {
          const now = Date.now();
          Object.keys(prices).forEach(sym => {
            const price = parseFloat(prices[sym]);
            if (price && price > 0) {
              if (this.onTick) this.onTick(sym, price, now);
              this.tickCount++;
            }
          });
          if (this.tickCount % 50 === 0) {
            console.log(`[QX] Store poll active — ${this.tickCount} ticks, ${Object.keys(prices).length} pairs`);
          }
        }
      } catch(e) {}
    }, 2000);
  }

  async _login() {
    console.log('[QX] Navigating to sign-in...');
    await this.page.goto('https://qxbroker.com/en/sign-in', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await new Promise(r => setTimeout(r, 5000));

    const emailSels = ['input[type="email"]','input[name="email"]','input[placeholder*="mail" i]','form input:first-of-type'];
    let filled = false;
    for (const sel of emailSels) {
      try {
        await this.page.waitForSelector(sel, { timeout: 6000 });
        await this.page.click(sel);
        await this.page.type(sel, this.email, { delay: 60 });
        filled = true;
        break;
      } catch(e) {}
    }
    if (!filled) {
      await this.page.evaluate((em) => {
        const inp = [...document.querySelectorAll('input')].find(i => i.type==='email'||i.name==='email'||i.placeholder?.toLowerCase().includes('mail'));
        if (inp) { inp.value=em; inp.dispatchEvent(new Event('input',{bubbles:true})); inp.dispatchEvent(new Event('change',{bubbles:true})); }
      }, this.email);
    }

    for (const sel of ['input[type="password"]','input[name="password"]']) {
      try {
        await this.page.waitForSelector(sel, { timeout: 5000 });
        await this.page.click(sel);
        await this.page.type(sel, this.pass, { delay: 60 });
        break;
      } catch(e) {}
    }
    await this.page.keyboard.press('Enter');
    await this.page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await new Promise(r => setTimeout(r, 4000));

    // Check for verification code
    const bodyText = await this.page.evaluate(() => document.body.innerText).catch(() => '');
    const url = this.page.url();
    const needsVerify = url.includes('verify') || url.includes('confirm') ||
      bodyText.toLowerCase().includes('verification') || bodyText.toLowerCase().includes('confirm your email') ||
      await this.page.$('input[name="code"]').then(el=>!!el).catch(()=>false);

    if (needsVerify) {
      console.log('[QX] Verification code required');
      const hint = this.email.replace(/(.{2}).*(@.*)/, '$1***$2');
      const code = await this.onVerifyNeeded(hint);
      if (!code) throw new Error('No verification code received');
      const codeInput = await this.page.$('input[name="code"], input[type="text"]').catch(() => null);
      if (codeInput) {
        await codeInput.type(code, { delay: 50 });
        await this.page.keyboard.press('Enter');
        await this.page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 20000 }).catch(() => {});
        await new Promise(r => setTimeout(r, 3000));
      }
    }

    await this.page.goto('https://qxbroker.com/en/demo-trade', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await new Promise(r => setTimeout(r, 8000)); // Wait for React store to load
    console.log('[QX] Logged in. URL:', this.page.url());
  }

  _parseTick(raw) {
    try {
      const d = JSON.parse(raw);
      if (d.asset && (d.value || d.price) && d.time) {
        const ts = d.time > 1e10 ? d.time : d.time * 1000;
        if (this.onTick) this.onTick(d.asset, parseFloat(d.value || d.price), ts);
        return;
      }
      if (Array.isArray(d) && Array.isArray(d[0]) && d[0].length >= 3) {
        const [sym, ts, price] = d[0];
        if (typeof sym === 'string' && sym.includes('_otc')) {
          if (this.onTick) this.onTick(sym, parseFloat(price), ts > 1e10 ? ts : ts * 1000);
        }
      }
    } catch(_) {}
  }

  async stop() {
    this.running = false;
    if (this.browser) await this.browser.close().catch(() => {});
  }
}

module.exports = { QXSession };
