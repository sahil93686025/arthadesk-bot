from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
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
settings.paper_trading = True  # शुरुआत में सुरक्षित Paper Mode चालू
scheduler = BackgroundScheduler(timezone=IST)

last_status_note = "सर्वर तैयार है"


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
            last_status_note = "Angel One SmartAPI लॉगिन सफल!"
            logger.info("SmartAPI login successful from Render Environment.")
        except Exception as exc:
            last_status_note = f"लॉगिन त्रुटि: {exc}"
            logger.error("SmartAPI login failed: %s", exc)
    else:
        last_status_note = "Environment में API चाबियाँ अधूरी हैं"


def scheduled_9am_login() -> None:
    if not angel_client.auto_login_9am:
        return
    load_env_and_login()


def scheduled_315pm_square_off() -> None:
    if engine.active_position:
        now_str = datetime.now(IST).strftime("%H:%M:%S IST")
        ltp = angel_client.get_ltp(engine.active_position.symbol) if angel_client.jwt_token else engine.active_position.entry_price
        engine.close_position(ltp, "Mandatory 3:15 PM IST Auto Square-Off", now_str, settings, angel_client.place_intraday_market_order)


def strategy_tick() -> None:
    global last_status_note
    if not engine.bot_running:
        return
    if not angel_client.jwt_token:
        last_status_note = "Angel One लॉगिन नहीं है (Re-Login दबाएं)"
        return
    try:
        candles = angel_client.fetch_5min_candles(settings.symbol)
        df = compute_indicators(candles)
        ltp = angel_client.get_ltp(settings.symbol)
        engine.evaluate_bar(df, settings, ltp, datetime.now(IST), angel_client.place_intraday_market_order)
        last_status_note = f"लाइव स्कैनिंग चालू | {settings.symbol} LTP: ₹{ltp}"
    except Exception as exc:
        last_status_note = f"स्कैन संदेश: {exc}"
        logger.error("Strategy tick failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_env_and_login()
    scheduler.add_job(scheduled_9am_login, CronTrigger(hour=9, minute=0, timezone=IST), id="daily_9am_login", replace_existing=True)
    scheduler.add_job(scheduled_315pm_square_off, CronTrigger(hour=15, minute=15, timezone=IST), id="daily_315pm_exit", replace_existing=True)
    scheduler.add_job(strategy_tick, "interval", seconds=15, id="strategy_tick", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="ArthaDesk NSE Intraday Bot API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthConfigPayload(BaseModel):
    api_key: str
    client_id: str
    mpin: str = Field(min_length=4, max_length=4)
    totp_secret: str
    auto_login_9am: bool = True


class SettingsPayload(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    stop_loss_pct: float = Field(gt=0, le=10)
    target_profit_pct: float = Field(gt=0, le=25)
    max_daily_loss: float = Field(gt=0)
    paper_trading: bool


@app.get("/", response_class=HTMLResponse)
def dashboard_ui():
    symbols_options = "".join([f'<option value="{s}">{s}</option>' for s in NSE_INSTRUMENTS.keys()])
    return f"""
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ArthaDesk Live Control</title>
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
        </style>
    </head>
    <body>
        <div class="card">
            <h2>⚡ ArthaDesk Intraday Bot</h2>
            <span id="conn" class="badge red">जाँच हो रही है...</span>
            <span id="mode" class="badge green">PAPER MODE</span>
            <p id="note" style="font-size:13px; color:#cbd5e1; margin:10px 0 0 0;">लोड हो रहा है...</p>
        </div>

        <div class="card">
            <div class="row"><span>बॉट स्थिति:</span><strong id="bstatus">रुका हुआ</strong></div>
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

        <script>
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
                    document.getElementById('pos').innerText = d.active_position || 'कोई नहीं';
                    document.getElementById('pnl').innerText = d.trade_count + ' ट्रेड | ₹' + d.realized_pnl.toFixed(2);
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
                    stop_loss_pct: 1.0,
                    target_profit_pct: 2.0,
                    max_daily_loss: 2000.0,
                    paper_trading: document.getElementById('pmode').value === 'true'
                }};
                let r = await fetch('/api/settings', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(payload)
                }});
                if(r.ok) alert('सेटिंग सेव हो गई!');
                loadState();
            }}
            setInterval(loadState, 4000);
            loadState();
        </script>
    </body>
    </html>
    """


@app.get("/api/dashboard-state")
def dashboard_state():
    pos_text = None
    if getattr(engine, "active_position", None):
        p = engine.active_position
        pos_text = f"{p.symbol} ({p.qty} qty @ ₹{p.entry_price})"
    trades = getattr(engine, "closed_trades", [])
    pnl = sum(getattr(t, "pnl", 0.0) for t in trades)
    return {
        "connected": bool(angel_client.jwt_token),
        "paper_trading": settings.paper_trading,
        "bot_running": engine.bot_running,
        "note": last_status_note,
        "active_position": pos_text,
        "trade_count": len(trades),
        "realized_pnl": pnl,
    }


@app.post("/api/auth/auto-login")
def trigger_auto_login():
    load_env_and_login()
    if not angel_client.jwt_token:
        raise HTTPException(status_code=400, detail=last_status_note)
    return {"status": "connected", "note": last_status_note}


@app.get("/api/health")
def health():
    return {"status": "ok", "ist_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")}


@app.post("/api/totp/preview")
def preview_totp(secret: Optional[str] = None):
    try:
        return angel_client.generate_totp(secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/login")
def login_angel_one(payload: AuthConfigPayload):
    angel_client.configure(
        api_key=payload.api_key,
        client_id=payload.client_id,
        mpin=payload.mpin,
        totp_secret=payload.totp_secret,
        auto_login_9am=payload.auto_login_9am,
    )
    try:
        return angel_client.login()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    if payload.symbol not in NSE_INSTRUMENTS:
        raise HTTPException(status_code=400, detail="Unsupported NSE symbol")
    settings.symbol = payload.symbol
    settings.quantity = payload.quantity
    settings.stop_loss_pct = payload.stop_loss_pct
    settings.target_profit_pct = payload.target_profit_pct
    settings.max_daily_loss = payload.max_daily_loss
    settings.paper_trading = payload.paper_trading
    return {"status": "updated", "settings": settings}


@app.post("/api/bot/start")
def start_bot():
    if not angel_client.jwt_token:
        load_env_and_login()
    if not angel_client.jwt_token:
        raise HTTPException(status_code=400, detail="Angel One लॉगिन नहीं हो पाया—Environment चाबियाँ जांचें")
    engine.bot_running = True
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
