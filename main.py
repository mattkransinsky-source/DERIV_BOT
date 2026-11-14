import os, json, time, threading, requests
import websocket
from flask import Flask

# --- Flask for Render ---
app = Flask(__name__)

@app.route('/')
def home():
    return "DERIV BOT ALIVE - V100 Sniper", 200

@app.route('/health')
def health():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    print(f"Flask on {port}", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

# --- Config ---
DERIV_TOKEN = os.getenv("DERIV_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "1HZ100V"
STAKE = 0.35
DURATION = 5
MAX_CONSEC_LOSS = 3
MAX_DAILY_LOSS = 5.0

ticks = []
balance = 0
daily_pnl = 0
consec_loss = 0
trading_paused_until = 0
in_trade = False

ENDPOINTS = [
    "wss://ws.derivws.com/websockets/v3?app_id=1089",
    "wss://ws.binaryws.com/websockets/v3?app_id=1089",
]

def send_tg(msg):
    print(msg, flush=True)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("TG Error:", e, flush=True)

def get_signal():
    if len(ticks) < 20:
        return None
    last_20 = ticks[-20:]
    last_5 = ticks[-5:]
    avg_20 = sum(last_20) / len(last_20)
    avg_5 = sum(last_5) / len(last_5)
    current = ticks[-1]
    if current > avg_20 and avg_5 > avg_20 and last_5[-1] > last_5[0]:
        return "CALL"
    if current < avg_20 and avg_5 < avg_20 and last_5[-1] < last_5[0]:
        return "PUT"
    return None

def on_message(ws, message):
    global balance, daily_pnl, consec_loss, in_trade, trading_paused_until, ticks
    try:
        data = json.loads(message)
    except:
        return

    if data.get("msg_type") == "authorize":
        if "error" in data:
            print(f"AUTH FAILED: {data['error']}", flush=True)
            send_tg(f"❌ Deriv Auth Failed: {data['error'].get('message')}\nCheck DERIV_TOKEN in Render Env")
            return
        print(f"Authorized. Balance: {data['authorize']['balance']}", flush=True)
        send_tg(f"✅ Deriv Bot Started\nMarket: Volatility 100 (1s)\nStrategy: Rise/Fall Trend Sniper\nStake: ${STAKE} | Duration: {DURATION} ticks\nBalance: ${data['authorize']['balance']}")
        ws.send(json.dumps({"ticks_history": SYMBOL, "count": 50, "end": "latest", "style": "ticks", "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        return

    if data.get("msg_type") == "history":
        ticks = list(data["history"]["prices"])
        return

    if data.get("msg_type") == "tick":
        price = float(data["tick"]["quote"])
        ticks.append(price)
        if len(ticks) > 100:
            ticks.pop(0)
        if time.time() < trading_paused_until:
            return
        if daily_pnl <= -MAX_DAILY_LOSS:
            send_tg(f"🛑 Daily loss limit hit (-${MAX_DAILY_LOSS}). Bot stopped for today.")
            trading_paused_until = time.time() + 86400
            return
        if in_trade:
            return
        signal = get_signal()
        if signal:
            in_trade = True
            print(f"Signal {signal} at {price}", flush=True)
            ws.send(json.dumps({
                "buy": 1,
                "price": STAKE,
                "parameters": {
                    "amount": STAKE,
                    "basis": "stake",
                    "contract_type": signal,
                    "currency": "USD",
                    "duration": DURATION,
                    "duration_unit": "t",
                    "symbol": SYMBOL
                }
            }))
        return

    if data.get("msg_type") == "buy":
        if "error" in data:
            print(f"Buy error: {data['error']}", flush=True)
            in_trade = False
            send_tg(f"❌ Buy Failed: {data['error'].get('message')}")
            return
        contract_id = data["buy"]["contract_id"]
        ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        is_rise = "Rise" in data["buy"].get("longcode", "")
        send_tg(f"📈 Trade Placed: {'RISE' if is_rise else 'FALL'} | Stake ${STAKE}")
        return

    if data.get("msg_type") == "proposal_open_contract":
        poc = data.get("proposal_open_contract", {})
        if poc.get("is_sold"):
            profit = float(poc.get("profit", 0))
            daily_pnl += profit
            in_trade = False
            if profit >= 0:
                consec_loss = 0
                send_tg(f"✅ WIN +${profit:.2f} | Daily P&L: ${daily_pnl:.2f}")
            else:
                consec_loss += 1
                send_tg(f"❌ LOSS ${profit:.2f} | Daily P&L: ${daily_pnl:.2f} | Streak: {consec_loss} losses")
                if consec_loss >= MAX_CONSEC_LOSS:
                    trading_paused_until = time.time() + 1800
                    consec_loss = 0
                    send_tg("⏸️ 3 losses in a row. Pausing 30 mins to protect account.")
        return

    if data.get("msg_type") == "balance":
        balance = float(data["balance"]["balance"])

def on_open(ws):
    print("WS Open - Authorizing...", flush=True)
    ws.send(json.dumps({"authorize": DERIV_TOKEN}))

def on_error(ws, error):
    print(f"WS Error: {error}", flush=True)

def on_close(ws, code, msg):
    print(f"WS Closed {code} {msg} - will reconnect", flush=True)

def run_deriv():
    time.sleep(3)
    if not DERIV_TOKEN:
        print("NO DERIV_TOKEN", flush=True)
        send_tg("❌ NO DERIV_TOKEN set in Render Environment. Add it and redeploy.")
        return
    fail_count = 0
    endpoint_idx = 0
    while True:
        try:
            url = ENDPOINTS[endpoint_idx % len(ENDPOINTS)]
            print(f"Connecting to {url} - attempt {fail_count+1}", flush=True)
            ws = websocket.WebSocketApp(url,
                on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close,
                header=[
                    "Origin: https://app.deriv.com",
                    "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                ])
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print(f"Reconnect: {e}", flush=True)
        fail_count += 1
        endpoint_idx += 1
        wait = min(10 + fail_count * 5, 60)
        print(f"Waiting {wait}s before retry - Cloudflare 520 protection", flush=True)
        time.sleep(wait)

if __name__ == "__main__":
    threading.Thread(target=run_deriv, daemon=True).start()
    run_web()
