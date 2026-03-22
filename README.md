# RafiX QX Signal — Deploy Guide

## Files
- `/server` → Railway (Node.js + Puppeteer, runs 24/7)
- `/client/index.html` → Cloudflare Pages (your signal dashboard)

---

## Step 1 — Deploy Server to Railway

1. Go to railway.app → New Project → Deploy from GitHub
2. Select repo: `Rafid31/rafix-qx-signal`, folder: `server`
3. Add these Environment Variables in Railway dashboard:

| Key | Value |
|-----|-------|
| QX_EMAIL | arsal.ebubechukwu@forliion.com |
| QX_PASS | (your QX demo password) |
| TG_TOKEN | 8701910654:AAE5Xcl3tFRGhtArlOXjFQ9EFlnL3IOBl1U |
| TG_CHATID | 6063240252 |

4. Railway gives you a URL like: `https://rafix-qx-signal-xxxx.up.railway.app`
5. Test it: open that URL — should show JSON with status

---

## Step 2 — First Login (Verification Code)

When server starts for the first time on Railway:
- QX will ask for a verification code
- Your Telegram bot will send you a message
- Check email: arsal.ebubechukwu@forliion.com
- Reply to Telegram bot: `/code 123456`
- OR go to: `https://YOUR-RAILWAY-URL/admin` and enter code there

This only happens ONCE. After that the session stays active.

---

## Step 3 — Update Frontend with Railway URL

Open `client/index.html`, find line ~220:
```
'wss://YOUR-RAILWAY-URL.up.railway.app'
```
Replace with your actual Railway URL.

---

## Step 4 — Deploy to Cloudflare Pages

1. Go to pages.cloudflare.com
2. Create application → Pages → Upload assets
3. Upload the `client` folder (just the index.html file)
4. Done — you get a free URL like `https://rafix-signal.pages.dev`

---

## How Signals Work

```
Second 0-44:   Analysing — shows WAIT or LEAN
Second 45-50:  Signal fires — "⚡ 12s — BUY SIGNAL"
Second 50-55:  "⚡ 8s — Prepare to enter"
Second 55-59:  🔴 RED FLASH — "ENTER BUY NOW"
Second 0:      New candle opens — place your trade
```

## Best Trading Hours (Dubai UTC+4)
- 🌟 17:00–20:00 = London+NY overlap = BEST
- ✅ 12:00–17:00 = London = GOOD
- ✅ 20:00–22:00 = New York = GOOD
- ❌ 04:00–12:00 = Asian = SKIP (bad signals)
