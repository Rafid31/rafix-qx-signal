// QX Session — Puppeteer login + tick interception
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
  }

  async start() {
    console.log('[QX] Launching browser...');
    this.browser = await puppeteer.launch({
      headless: 'new',
      args: ['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
             '--disable-gpu','--no-first-run','--single-process','--no-zygote']
    });
    this.page = await this.browser.newPage();
    await this.page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36');

    // Intercept WebSocket ticks BEFORE page loads
    await this.page.evaluateOnNewDocument(() => {
      const OrigWS = window.WebSocket;
      window.WebSocket = function(...args) {
        const ws = new OrigWS(...args);
        ws.addEventListener('message', (e) => {
          try { window.__qxTick && window.__qxTick(e.data); } catch(_) {}
        });
        ws.addEventListener('close', () => {
          try { window.__qxDC && window.__qxDC(); } catch(_) {}
        });
        return ws;
      };
      Object.setPrototypeOf(window.WebSocket, OrigWS);
      window.WebSocket.prototype = OrigWS.prototype;
    });

    await this.page.exposeFunction('__qxTick', (raw) => this._parseTick(raw));
    await this.page.exposeFunction('__qxDC', () => {
      console.log('[QX] WebSocket closed');
      if (this.onDisconnect) this.onDisconnect();
    });

    await this._login();
    this.running = true;
    console.log('[QX] Session active — collecting all OTC pairs');

    // Keep-alive: reload every 6 hours to prevent session expiry
    setInterval(() => {
      if (this.running) {
        console.log('[QX] Refreshing page to keep session alive...');
        this.page.reload({ waitUntil: 'networkidle2' }).catch(() => {});
      }
    }, 6 * 60 * 60 * 1000);
  }

  async _login() {
    console.log('[QX] Navigating to sign-in...');
    await this.page.goto('https://qxbroker.com/en/sign-in', { waitUntil: 'domcontentloaded', timeout: 60000 });
    // Extra wait for JS to render the form
    await new Promise(r => setTimeout(r, 5000));

    // Try multiple selectors for email field
    const emailSelectors = ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="mail" i]', 'input[class*="email" i]', 'form input:first-of-type'];
    let emailFilled = false;
    for (const sel of emailSelectors) {
      try {
        await this.page.waitForSelector(sel, { timeout: 8000 });
        await this.page.click(sel);
        await this.page.type(sel, this.email, { delay: 60 });
        emailFilled = true;
        console.log('[QX] Email filled using selector:', sel);
        break;
      } catch(e) { continue; }
    }
    if (!emailFilled) {
      // Last resort: use JS to fill
      await this.page.evaluate((email) => {
        const inputs = document.querySelectorAll('input');
        for (const inp of inputs) {
          if (inp.type === 'email' || inp.name === 'email' || inp.placeholder?.toLowerCase().includes('mail')) {
            inp.value = email;
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            break;
          }
        }
      }, this.email);
    }

    // Fill password
    const passSels = ['input[type="password"]', 'input[name="password"]'];
    for (const sel of passSels) {
      try {
        await this.page.waitForSelector(sel, { timeout: 5000 });
        await this.page.click(sel);
        await this.page.type(sel, this.pass, { delay: 60 });
        break;
      } catch(e) { continue; }
    }
    await this.page.keyboard.press('Enter');

    await this.page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await new Promise(r => setTimeout(r, 3000));

    // Check if verification code page appeared
    const url = this.page.url();
    const bodyText = await this.page.evaluate(() => document.body.innerText).catch(() => '');

    const needsVerify = url.includes('verify') || url.includes('confirm') ||
      bodyText.toLowerCase().includes('verification code') ||
      bodyText.toLowerCase().includes('confirm your email') ||
      await this.page.$('input[name="code"], input[placeholder*="code" i], input[placeholder*="verify" i]').then(el => !!el).catch(() => false);

    if (needsVerify) {
      console.log('[QX] Verification code required');
      // Get email hint from page
      const emailHint = this.email.replace(/(.{2}).*(@.*)/, '$1***$2');
      const code = await this.onVerifyNeeded(emailHint);
      if (!code) throw new Error('Verification code timeout — no code received');

      // Enter the code
      const codeInput = await this.page.$('input[name="code"], input[type="text"], input[placeholder*="code" i]');
      if (codeInput) {
        await codeInput.click();
        await codeInput.type(code, { delay: 50 });
        await this.page.keyboard.press('Enter');
        await this.page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 20000 }).catch(() => {});
        console.log('[QX] Verification code entered successfully');
      }
    }

    // Navigate to demo trade page
    await this.page.goto('https://qxbroker.com/en/demo-trade', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await new Promise(r => setTimeout(r, 5000));
    console.log('[QX] Logged in. URL:', this.page.url());
  }

  _parseTick(raw) {
    try {
      const d = JSON.parse(raw);
      // Format 1: { asset, value/price, time }
      if (d.asset && (d.value || d.price) && d.time) {
        const sym = d.asset;
        const price = d.value || d.price;
        const ts = d.time > 1e10 ? d.time : d.time * 1000;
        if (this.onTick) this.onTick(sym, parseFloat(price), ts);
        return;
      }
      // Format 2: [[ sym, ts, price ]]
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
