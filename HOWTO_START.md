# How to Start Regime Trader

Complete step-by-step guide from zero to live TradingView alerts hitting your dashboard.

---

## What You Need Before Starting

| Requirement | Why |
|---|---|
| TradingView **Pro** plan or higher | Free plan cannot send webhooks |
| ngrok installed | Gives TradingView a public URL to reach your local machine |
| Python 3.12 installed | Backend runtime |
| Node.js 18+ installed | Frontend runtime |
| The `.env` file configured | Backend won't start without it |

---

## Part 1 — First-Time Setup (do this once)

### 1.1 Create your `.env` file

1. Open File Explorer and navigate to `c:\Users\User\Desktop\AUTO_TRADING_BOT\regime-trader\`
2. Find the file named `.env.example`
3. Right-click it → **Copy**, then right-click in the same folder → **Paste**
4. Rename the copy from `.env.example` to `.env` (remove the `.example` part)
5. Right-click `.env` → **Open with** → **Notepad** (or VS Code)
6. Change the line to:
   ```
   WEBHOOK_SECRET=mysecret123
   ```
   Replace `mysecret123` with any password you want — just remember it, you will type it into TradingView later.
7. Save and close the file.

### 1.2 Install Python dependencies (first time only)

1. Open **PowerShell** (press `Win + X` → **Windows PowerShell**)
2. Run these commands one at a time:
   ```powershell
   cd "c:\Users\User\Desktop\AUTO_TRADING_BOT\regime-trader"
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Wait for it to finish. You should see packages being installed.

### 1.3 Install frontend dependencies (first time only)

1. Open a **second PowerShell** window
2. Run:
   ```powershell
   cd "c:\Users\User\Desktop\AUTO_TRADING_BOT\regime-trader\frontend"
   npm install
   ```
3. Wait for it to finish.

### 1.4 Install ngrok (first time only)

1. Go to [https://ngrok.com/download](https://ngrok.com/download)
2. Download the Windows version — it is a single `.exe` file
3. Save `ngrok.exe` somewhere easy to find, for example `C:\ngrok\ngrok.exe`
4. (Optional but recommended) Sign up for a free ngrok account at [https://ngrok.com](https://ngrok.com)
5. After signing up, go to your ngrok dashboard → **Your Authtoken** → copy the token
6. Open PowerShell and run:
   ```powershell
   C:\ngrok\ngrok.exe config add-authtoken YOUR_TOKEN_HERE
   ```
   This gives you a permanent static domain so your URL does not change every restart.

---

## Part 2 — Every Time You Want to Run the Bot

You need **three PowerShell windows** open at the same time. Do not close any of them while the bot is running.

### Step 1 — Start the Backend (FastAPI server)

1. Open **PowerShell window #1**
2. Run:
   ```powershell
   cd "c:\Users\User\Desktop\AUTO_TRADING_BOT\regime-trader"
   .venv\Scripts\activate
   uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
   ```
3. Wait until you see this line:
   ```
   INFO:     Application startup complete.
   ```
4. **Leave this window open.** Do not close it.

### Step 2 — Start the Frontend (React dashboard)

1. Open **PowerShell window #2**
2. Run:
   ```powershell
   cd "c:\Users\User\Desktop\AUTO_TRADING_BOT\regime-trader\frontend"
   npm run dev
   ```
3. Wait until you see:
   ```
   Local:   http://localhost:5173/
   ```
4. Open your browser and go to **http://localhost:5173**
5. You should see the dark trading dashboard. **Leave this window open.**

### Step 3 — Start ngrok (public URL tunnel)

1. Open **PowerShell window #3**
2. Run:
   ```powershell
   C:\ngrok\ngrok.exe http 8000
   ```
   > If you saved ngrok somewhere else, adjust the path. Or if ngrok is in your system PATH, just run `ngrok http 8000`.
3. You will see output like this:
   ```
   Forwarding  https://a1b2c3d4.ngrok-free.app -> http://localhost:8000
   ```
4. **Copy the `https://` URL** — this is what TradingView will send alerts to.
5. **Leave this window open.** If you close it, the URL stops working.

> **Important:** On the free ngrok plan without an authtoken, the URL changes every time you restart ngrok. If you added your authtoken (Step 1.4), you can get a free static domain from your ngrok dashboard under **Domains** — use that instead.

### Step 4 — Verify everything is running

Open your browser and go to:
```
http://localhost:8000/docs
```
You should see the FastAPI interactive API documentation page. If you see it, the backend is working correctly.

---

## Part 3 — Add the Pine Script to TradingView (do this once)

### Step 5 — Open the Pine Script Editor

1. Go to [https://www.tradingview.com](https://www.tradingview.com) and log in
2. Open a chart for the instrument you want to trade (e.g. search `MNQ1!` for Micro Nasdaq futures)
3. At the very bottom of the screen, click the **Pine Script Editor** tab — it looks like `</>`
4. The editor panel opens at the bottom of the screen

### Step 6 — Paste the script

1. In VS Code (or any text editor), open this file:
   ```
   c:\Users\User\Desktop\AUTO_TRADING_BOT\regime-trader\pinescript\regime_trader.pine
   ```
2. Press **Ctrl+A** to select everything, then **Ctrl+C** to copy
3. Click inside the TradingView Pine Script editor
4. Press **Ctrl+A** to select all existing code, then **Ctrl+V** to paste
5. Click the **Add to chart** button (blue button in the top-right of the editor panel)

You should see the strategy load on the chart with:
- Coloured background (green = low vol, orange = mid vol, red = high vol)
- Three moving average lines (blue EMA fast, purple EMA slow, grey SMA 200)
- A small stats table in the bottom-right corner of the chart

### Step 7 — Configure the Pine Script inputs

1. Find the strategy name **"RT"** in the top-left of the chart (it appears with the other indicator names)
2. Click the **Settings gear icon ⚙️** that appears next to it
3. Click the **Inputs** tab at the top of the settings window
4. Set these three fields under the **Webhook** group:

   | Input field | What to type |
   |---|---|
   | **Webhook Secret** | `mysecret123` (exactly what you put in your `.env` file) |
   | **Strategy Name** | `regime_trader_v1` (leave as default) |
   | **Account Name** | `Demo NQ` (or any name — this becomes the account in your dashboard) |

5. Click **OK**

---

## Part 4 — Create the TradingView Alert (do this once per chart)

### Step 8 — Open the Create Alert dialog

There are two ways to open it:
- Press **Alt+A** on your keyboard
- Or click the **clock icon** in the right-side toolbar, then click **+ Create alert**

### Step 9 — Fill in the alert settings

The Create Alert dialog has several fields. Fill them in exactly as follows:

**Condition (first dropdown):**
- Click the dropdown and select **"RT (14, 0.8, 2, 5...)"** — this is the Regime Trader strategy you added

**Condition (second dropdown, below the first):**
- Click it and select **"Order fills and alert() function"**
- This is the equivalent of "Any alert() call" — it fires whenever the Pine Script calls `alert()`

**Interval:**
- Change this to match the timeframe of your chart
- Example: if your chart is on a 5-minute timeframe, select `5 minutes`
- If your chart is on a 1-hour timeframe, select `1 hour`
- **Do not leave it on `1D` (daily) unless you are trading daily bars** — otherwise you will only get one signal per day

**Expiration:**
- Click the dropdown and set it as far into the future as possible (TradingView allows up to 1 year on Pro plans)

**Message:**
- **Clear this field completely — leave it blank**
- The Pine Script builds and sends the full JSON payload itself via `alert()`, so whatever is in the Message field is ignored

### Step 10 — Add the Webhook URL

This is the step that sends alerts to your dashboard:

1. In the Create Alert dialog, look for the **Notifications** row near the bottom
2. Click on **Notifications** to expand it — it reveals a list of delivery methods
3. You will see checkboxes for: **App**, **Email**, **SMS**, **Webhook URL**
4. Check the **Webhook URL** checkbox
5. A text field appears directly below the checkbox
6. Paste your ngrok URL into that field, adding `/webhook/alert` at the end:
   ```
   https://a1b2c3d4.ngrok-free.app/webhook/alert
   ```
   Replace `a1b2c3d4` with your actual ngrok subdomain from Step 3.

7. Click **Create**

The alert is now active. TradingView will POST to your URL every time the strategy fires a buy, sell, or flat signal.

---

## Part 5 — Test the Connection

### Step 11 — Send a manual test alert

Before waiting for a real signal, verify the entire pipeline works by sending a test payload manually.

Open **PowerShell** and run:

```powershell
$body = @{
    account       = "Demo NQ"
    symbol        = "MNQ1"
    action        = "buy"
    contracts     = 1
    price         = 19420.0
    stop          = 19250.0
    take_profit   = 19700.0
    strategy      = "regime_trader_v1"
    regime        = "LOW_VOL"
    strategy_equity = 100000.0
    strategy_pnl  = 0.0
    position_size = 1
    secret        = "mysecret123"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/webhook/alert" -Method POST -Body $body -ContentType "application/json"
```

You should get back a response like:
```json
{"status":"ok","signal_id":1,"trade_action":"opened","message":"BUY MNQ1 logged"}
```

### Step 12 — Check the dashboard

1. Go to **http://localhost:5173** in your browser
2. You should see:
   - The account **"Demo NQ"** appear in the account dropdown (top of the sidebar)
   - The **Live Signals** feed show a BUY signal for MNQ1
   - The **Strategy Equity** card show `$100,000.00`
3. Send a matching sell to close the trade:
   ```powershell
   $body = @{
       account       = "Demo NQ"
       symbol        = "MNQ1"
       action        = "sell"
       contracts     = 1
       price         = 19600.0
       stop          = 0
       take_profit   = 0
       strategy      = "regime_trader_v1"
       regime        = "LOW_VOL"
       strategy_equity = 100180.0
       strategy_pnl  = 180.0
       position_size = 0
       secret        = "mysecret123"
   } | ConvertTo-Json

   Invoke-RestMethod -Uri "http://localhost:8000/webhook/alert" -Method POST -Body $body -ContentType "application/json"
   ```
4. Check the **Past Trades** page — you should see the completed trade with a P&L of `$180.00`

---

## Part 6 — Daily Routine

Every time you sit down to trade:

1. Open 3 PowerShell windows and run Steps 1, 2, and 3 above
2. Open your browser to **http://localhost:5173**
3. Check that your TradingView alert is still active (alerts expire — renew if needed)
4. If you restarted ngrok and got a new URL, update the Webhook URL in the TradingView alert settings

When you are done for the day, you can close all three PowerShell windows.

---

## Troubleshooting

**"Application startup complete" never appears in the backend window**
- Make sure you ran `.venv\Scripts\activate` first (you should see `(.venv)` at the start of the prompt)
- Make sure you are in the `regime-trader` folder, not the `frontend` folder

**Dashboard shows no account in the dropdown**
- No webhook has been received yet. Send the manual test from Step 11 first.
- The account is created automatically on the first alert.

**TradingView says "Webhook URL is not a valid URL"**
- Make sure the URL starts with `https://` (not `http://`)
- Make sure there is no space before or after the URL
- Make sure ngrok is still running in its PowerShell window

**Webhook arrives but dashboard does not update live**
- The WebSocket connection may have dropped. Refresh the browser (F5).
- Make sure both the backend (port 8000) and frontend (port 5173) are running.

**Signal shows in the backend logs but dashboard equity card still shows `—`**
- Check that your Pine Script Webhook Secret input matches exactly what is in your `.env` file (case-sensitive)
- Check that the Account Name in Pine Script inputs matches what is in the dashboard dropdown

**Alert fires but you get HTTP 401 Unauthorized**
- The `secret` field in the payload does not match `WEBHOOK_SECRET` in your `.env` file
- In TradingView Pine Script inputs, set Webhook Secret to exactly `mysecret123` (or whatever you chose)

**ngrok URL changes every day**
- Sign up for a free ngrok account, add your authtoken (Step 1.4), and claim a free static domain from your ngrok dashboard under **Domains**
