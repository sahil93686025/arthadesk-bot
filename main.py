from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from smartapi_service import AngelOneClient, NSE_INSTRUMENTS
from strategy_engine import IntradayStrategyEngine, RiskSettings, compute_indicators

IST = ZoneInfo("Asia/Kolkata")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("arthadesk.api")

angel_client = AngelOneClient()
engine = IntradayStrategyEngine()
settings = RiskSettings()
settings.symbol = "AUTO_ALL"
settings.quantity = 1
settings.paper_trading = True
scheduler = BackgroundScheduler(timezone=IST)

last_status_note = "ऑटो-स्कैनर तैयार है (15 हाई-वॉल्यूम शेयर)"
scan_index = 0
STOCK_LIST = list(NSE_INSTRUMENTS.keys())


def load_env_and_login() -> None:
    global last_status_note
    api_key = os.getenv("ANGEL_API_KEY", "").strip()
    client_id = os.getenv("ANGEL_CLIENT_ID", "").strip()
    mpin = os.getenv("ANGEL_MPIN", "").strip()
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

    if api_key and client_id and mpin and totp_secret:
        angel_client.configure(
            api_key=api_key,
            client_id=client_id,
            mpin=mpin,
            totp_secret=totp_secret,
            auto_login_9am=True,
        )
        try:
            angel_client.login()
            last_status_note = "Angel One SmartAPI लॉगिन सफल! 15 शेयर स्कैन के लिए तैयार।"
        except Exception as exc:
            last_status_note = f"लॉगिन त्रुटि: {exc}"
    else:
        last_status_note = "Environment में API चाबियाँ अधूरी हैं"


def scheduled_9am_login() -> None:
    if angel_client.auto_login_9am:
        load_env_and_login()


def scheduled_315pm_square_off() -> None:
    if engine.active_position:
        now_str = datetime.now(IST).strftime("%H:%M:%S IST")
        ltp = angel_client.get_ltp(engine.active_position.symbol) if angel_client.jwt_token else engine.active_position.entry_price
        engine.close_position(ltp, "Mandatory 3:15 PM IST Auto Square-Off", now_str, settings, angel_client.place_intraday_market_order)


def strategy_tick() -> None:
    global last_status_note, scan_index
    if not engine.bot_running:
        return
    if not angel_client.jwt_token:
        last_status_note = "Angel One लॉगिन नहीं है (Re-Login दबाएं)"
        return

    try:
        # अगर पहले से कोई ट्रेड खुला है, तो सिर्फ उसी शेयर की निगरानी करें
        if engine.active_position:
            active_sym = engine.active_position.symbol
            ltp = angel_client.get_ltp(active_sym)
            now_t = datetime.now(IST).strftime("%H:%M:%S")
            unrealized = round((ltp - engine.active_position.entry_price) * engine.active_position.quantity, 2)
            last_status_note = f"🔥 सक्रिय ट्रेड ({now_t}): {active_sym} | भाव: ₹{ltp} | चल रहा P&L: ₹{unrealized}"
            engine.evaluate_bar(None, settings, ltp, datetime.now(IST), angel_client.place_intraday_market_order, candidate_symbol=active_sym)
            return

        # वरना 15 शेयरों की लिस्ट में से अगला शेयर चुनकर स्कैन करें
        if settings.symbol == "AUTO_ALL":
            target_sym = STOCK_LIST[scan_index % len(STOCK_LIST)]
            scan_index += 1
        else:
            target_sym = settings.symbol

        ltp = angel_client.get_ltp(target_sym)
        time.sleep(1.5)  # Angel One Rate Limit से बचने के लिए 1.5 सेकंड का ब्रेक
        candles = angel_client.fetch_5min_candles(target_sym)
        df = compute_indicators(candles)

        now_t = datetime.now(IST).strftime("%H:%M:%S")
        res = engine.evaluate_bar(df, settings, ltp, datetime.now(IST), angel_client.place_intraday_market_order, candidate_symbol=target_sym)

        if res.get("signal") == "BUY_EXECUTED":
            last_status_note = f"🚀 नया ट्रेड लिया गया! {target_sym} @ ₹{ltp} ({now_t})"
        else:
            last_status_note = f"🔍 स्कैनिंग ({now_t}): {target_sym} | भाव: ₹{ltp} (मौका ढूँढ रहा है...)"

    except Exception as exc:
        err_txt = str(exc)
        if "exceeding access rate" in err_txt.lower():
            logger.warning("Rate limit pause, next tick will continue smoothly.")
        else:
            last_status_note = f"स्कैन संदेश: {err_txt}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_env_and_login()
    scheduler.add_job(scheduled_9am_login, CronTrigger(hour=9, minute=0, timezone=IST), id="daily_9am_login", replace_existing=True)
    scheduler.add_job(scheduled_315pm_square_off, CronTrigger(hour=15, minute=15, timezone=IST), id="daily_315pm_exit", replace_existing=True)
    scheduler.add_job(strategy_tick, "interval", seconds=20, id="strategy_tick", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="ArthaDesk NSE Auto-Scanner Bot", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class SettingsPayload(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    stop_loss_pct: float = Field(gt=0, le=10)
    target_profit_pct: float = Field(gt=0, le=25)
    max_daily_loss: float = Field(gt=0)
    paper_trading: bool


@app.get("/", response_class=HTMLResponse)
def dashboard_ui():
    symbols_options = '<option value="AUTO_ALL">🌐 सभी 15 हाई-वॉल्यूम शेयर (Auto-Scan All)</option>' + "".join(
        [f'<option value="{s}">{s}</option>' for s in NSE_INSTRUMENTS.keys()]
    )
    return f"""
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ArthaDesk Auto-Scanner</title>
        <style>
            body {{ font-family: sans-serif; background: #0b1120; color: #f8fafc; margin: 0; padding: 14px; }}
            .card {{ background: #1e293b; border-radius: 12px; padding: 15px; margin-bottom: 12px; border: 1px solid #334155; }}
            h2 {{ margin: 0 0 10px 0; color: #38bdf8; font-size: 20px; }}
            .badge {{ display: inline-block; padding: 5px 10px; border-radius: 16px; font-size: 12px; font-weight: bold; margin-right: 6px; }}
            .green {{ background: #065f46; color: #34d399; }}
            .red {{ background: #7f1d1d; color: #fca5a5; }}
            .row {{ display: flex; justify-content: space-between; margin: 8px 0; font-size: 14px; }}
            input, select {{ width: 100%; padding: 9px; margin-top: 4px; margin-bottom: 10px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: white; box-sizing: border-box; }}
            button {{ width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 15px; font-weight: bold; cursor: pointer; margin-top: 6px; }}
            .btn-start {{ background: #16a34a; color: white; }}
            .btn-stop {{ background: #dc2626; color: white; }}
            .btn-blue {{ background: #2563eb; color: white; }}
            .btn-warn {{ background: #d97706; color: white; }}
            .log-item {{ background: #0f172a; padding: 8px; border-radius: 6px; margin-top: 6px; font-size: 12px; border-left: 3px solid #38bdf8; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>⚡ ArthaDesk 15-Stock Auto Bot</h2>
            <span id="conn" class="badge red">जाँच हो रही है...</span>
            <span id="mode" class="badge green">PAPER MODE</span>
            <p id="note" style="font-size:14px; color:#38bdf8; font-weight:bold; margin:10px 0 0 0;">लोड हो रहा है...</p>
        </div>

        <div class="card">
            <div class="row"><span>बॉट स्थिति:</span><strong id="bstatus">रुका हुआ</strong></div>
            <div class="row"><span>स्कैन मोड:</span><strong id="cursym">AUTO_ALL (15 शेयर)</strong></div>
            <div class="row"><span>सक्रिय पोजीशन:</span><strong id="pos">कोई नहीं</strong></div>
            <div class="row"><span>कुल ट्रेड / नफा-नुकसान:</span><strong id="pnl">0 | ₹0.00</strong></div>
            <button class="btn-start" onclick="apiCall('/api/bot/start')">▶️ बॉट START करें</button>
            <button class="btn-stop" onclick="apiCall('/api/bot/stop')">⏸️ बॉट STOP करें</button>
            <button class="btn-blue" onclick="apiCall('/api/auth/auto-login')">🔑 Angel One Re-Login करें</button>
            <button class="btn-warn" onclick="emergencyExit()">🚨 EMERGENCY EXIT ALL</button>
        </div>

        <div class="card">
            <strong>⚙️ ट्रेडिंग सेटिंग्स बदलें:</strong><br><br>
            <label>शेयर चुनें (Stock):</label>
            <select id="sym">{symbols_options}</select>
            <label>मात्रा (Quantity):</label>
            <input type="number" id="qty" value="1" min="1">
            <label>मोड चुनें:</label>
            <select id="pmode">
                <option value="true">🧪 Paper Trading (नकली पैसे - सुरक्षित)</option>
                <option value="false">💰 Live Trading (असली पैसे)</option>
            </select>
            <button class="btn-blue" onclick="saveSettings()">💾 सेटिंग सेव करें</button>
        </div>

        <div class="card">
            <strong>📜 आज के ट्रेड लॉग (Trade History):</strong>
            <div id="logs">अभी कोई ट्रेड नहीं हुआ है।</div>
        </div>

        <script>
            let initialized = false;
            async function loadState() {{
                try {{
                    let r = await fetch('/api/dashboard-state');
                    let d = await r.json();
                    let c = document.getElementById('conn');
                    c.innerText = d.connected ? '🟢 Angel One Connected' : '🔴 Disconnected';
                    c.className = 'badge ' + (d.connected ? 'green' : 'red');

                    let m = document.getElementById('mode');
                    m.innerText = d.paper_trading ? '🧪 PAPER MODE (नकली पैसे)' : '💰 LIVE MODE (असली पैसे)';
                    m.className = 'badge ' + (d.paper_trading ? 'green' : 'red');

                    document.getElementById('note').innerText = d.note;
                    document.getElementById('bstatus').innerText = d.bot_running ? '✅ चालू है (RUNNING)' : '⏸️ रुका हुआ (STOPPED)';
                    document.getElementById('cursym').innerText = (d.symbol === 'AUTO_ALL' ? '🌐 सभी 15 शेयर' : d.symbol) + ' (' + d.quantity + ' qty)';
                    document.getElementById('pos').innerText = d.active_position || 'कोई नहीं';
                    document.getElementById('pnl').innerText = d.trade_count + ' ट्रेड | ₹' + d.realized_pnl.toFixed(2);

                    if (d.logs && d.logs.length > 0) {{
                        document.getElementById('logs').innerHTML = d.logs.map(l =>
                            `<div class="log-item"><b>${{l.time_ist}}</b> | ${{l.action}} <b>${{l.symbol}}</b> (${{l.quantity}} qty @ ₹${{l.price}}) <br>${{l.reason}} ${{l.pnl !== null ? ' | P&L: ₹' + l.pnl : ''}}</div>`
                        ).join('');
                    }}

                    if (!initialized) {{
                        document.getElementById('sym').value = d.symbol;
                        document.getElementById('qty').value = d.quantity;
                        document.getElementById('pmode').value = d.paper_trading ? 'true' : 'false';
                        initialized = true;
                    }}
                }} catch(e) {{}}
            }}
            async function apiCall(url) {{
                let r = await fetch(url, {{method:'POST'}});
                let d = await r.json();
                if(d.detail) alert(d.detail);
                loadState();
            }}
            async function emergencyExit() {{
                if(confirm('क्या आप वाकई सभी पोजीशन तुरंत बंद करना चाहते हैं?')) {{
                    await apiCall('/api/bot/emergency-exit');
                }}
            }}
            async function saveSettings() {{
                let payload = {{
                    symbol: document.getElementById('sym').value,
                    quantity: parseInt(document.getElementById('qty').value),
                    stop_loss_pct: 0.75,
                    target_profit_pct: 1.50,
                    max_daily_loss: 2500.0,
                    paper_trading: document.getElementById('pmode').value === 'true'
                }};
                let r = await fetch('/api/settings', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(payload)
                }});
                if(r.ok) {{
                    alert('✅ सेटिंग सेव हो गई!');
                    loadState();
                }}
            }}
            setInterval(loadState, 5000);
            loadState();
        </script>
    </body>
    </html>
    """


@app.get("/api/dashboard-state")
def dashboard_state():
    pos_text = None
    if engine.active_position:
        p = engine.active_position
        pos_text = f"{p.symbol} ({p.quantity} qty @ ₹{p.entry_price})"
    logs_data = [
        {
            "time_ist": l.time_ist,
            "symbol": l.symbol,
            "action": l.action,
            "quantity": l.quantity,
            "price": l.price,
            "reason": l.reason,
            "pnl": l.pnl,
        }
        for l in engine.trade_logs[:10]
    ]
    return {
        "connected": bool(angel_client.jwt_token),
        "paper_trading": settings.paper_trading,
        "bot_running": engine.bot_running,
        "symbol": settings.symbol,
        "quantity": settings.quantity,
        "note": last_status_note,
        "active_position": pos_text,
        "trade_count": len(engine.trade_logs),
        "realized_pnl": engine.realized_pnl,
        "logs": logs_data,
    }


@app.post("/api/auth/auto-login")
def trigger_auto_login():
    load_env_and_login()
    if not angel_client.jwt_token:
        raise HTTPException(status_code=400, detail=last_status_note)
    return {"status": "connected", "note": last_status_note}


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    if payload.symbol != "AUTO_ALL" and payload.symbol not in NSE_INSTRUMENTS:
        raise HTTPException(status_code=400, detail="Unsupported NSE symbol")
    settings.symbol = payload.symbol
    settings.quantity = payload.quantity
    settings.stop_loss_pct = payload.stop_loss_pct
    settings.target_profit_pct = payload.target_profit_pct
    settings.max_daily_loss = payload.max_daily_loss
    settings.paper_trading = payload.paper_trading
    return {"status": "updated", "symbol": settings.symbol}


@app.post("/api/bot/start")
def start_bot():
    if not angel_client.jwt_token:
        load_env_and_login()
    if not angel_client.jwt_token:
        raise HTTPException(status_code=400, detail="Angel One लॉगिन नहीं हो पाया")
    engine.bot_running = True
    strategy_tick()
    return {"bot_running": True}


@app.post("/api/bot/stop")
def stop_bot():
    engine.bot_running = False
    return {"bot_running": False}


@app.post("/api/bot/emergency-exit")
def emergency_exit_all():
    engine.bot_running = False
    if not engine.active_position:
        return {"status": "no_open_positions", "bot_running": False}
    now_str = datetime.now(IST).strftime("%H:%M:%S IST")
    ltp = angel_client.get_ltp(engine.active_position.symbol) if angel_client.jwt_token else engine.active_position.entry_price
    result = engine.close_position(ltp, "Emergency Exit All Triggered", now_str, settings, angel_client.place_intraday_market_order)
    return {"status": "exited", "result": result, "bot_running": False}
