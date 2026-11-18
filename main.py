from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from smartapi_service import DEFAULT_WATCHLIST, AngelSmartAPIService
from strategy_engine import IST, IntradayStrategyEngine, RiskSettings, compute_indicators

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("arthadesk-bot")

broker = AngelSmartAPIService()
engine = IntradayStrategyEngine()
settings = RiskSettings()
last_scan_message: str = "बॉट तैयार है..."
scan_index: int = 0


def _live_order_callback(symbol: str, side: str, qty: int) -> str:
    res = broker.place_intraday_market_order(symbol, side, qty)
    if not res.get("status"):
        raise RuntimeError(res.get("message", "Order placement failed"))
    return str(res.get("order_id", "LIVE-ORDER"))


async def trading_loop() -> None:
    global last_scan_message, scan_index
    watchlist_keys: List[str] = list(DEFAULT_WATCHLIST.keys())

    while True:
        try:
            now_ist = datetime.now(IST)
            if not broker.is_connected and broker.has_credentials():
                broker.login()

            if engine.active_position:
                pos = engine.active_position
                ltp = await asyncio.to_thread(broker.get_ltp, pos.symbol)
                if ltp is not None:
                    res = engine.evaluate_bar(None, settings, ltp, now_ist, _live_order_callback, pos.symbol)
                    if pos.side == "BUY":
                        unrealized = (ltp - pos.entry_price) * pos.quantity
                    else:
                        unrealized = (pos.entry_price - ltp) * pos.quantity
                    sign = "+" if unrealized >= 0 else ""
                    last_scan_message = (
                        f"🔥 सक्रिय ट्रेड ({pos.side} {pos.symbol}): भाव ₹{ltp} | "
                        f"चल रहा P&L: {sign}₹{unrealized:.2f} | SL: ₹{pos.stop_loss_price} | Target: ₹{pos.target_price}"
                    )
                    if res.get("signal") == "EXITED":
                        last_scan_message = f"✅ ट्रेड बंद ({pos.symbol}): {res.get('reason')} | P&L: ₹{res.get('pnl')}"
                await asyncio.sleep(10)
                continue

            if not engine.bot_running:
                last_scan_message = "⏸️ बॉट रुका हुआ है (STOPPED)"
                await asyncio.sleep(10)
                continue

            if not engine.is_entry_window(now_ist):
                last_scan_message = f"🕒 बाज़ार समय से बाहर ({now_ist.strftime('%H:%M:%S')} IST) - 9:25 AM से 2:55 PM तक चलेगा"
                await asyncio.sleep(15)
                continue

            if settings.symbol == "AUTO_ALL":
                sym = watchlist_keys[scan_index % len(watchlist_keys)]
                scan_index += 1
            else:
                sym = settings.symbol

            candles = await asyncio.to_thread(broker.get_intraday_candles, sym)
            ltp = await asyncio.to_thread(broker.get_ltp, sym)

            if ltp is not None and candles:
                df = compute_indicators(candles)
                res = engine.evaluate_bar(df, settings, ltp, now_ist, _live_order_callback, sym)
                if res.get("signal") == "BUY_EXECUTED":
                    last_scan_message = f"🚀 नया ट्रेड लिया: {res.get('side')} {sym} @ ₹{ltp}!"
                else:
                    last_scan_message = f"🔍 स्कैनिंग ({scan_index % len(watchlist_keys) + 1}/{len(watchlist_keys)}): {sym} (₹{ltp}) - मौका ढूँढ रहा है..."
            else:
                last_scan_message = f"⚠️ डेटा प्रतीक्षा ({sym}): {broker.last_error or 'कैंडल बन रही हैं...'}"

        except Exception as exc:
            logger.exception("Loop error: %s", exc)
            last_scan_message = f"⚠️ त्रुटि: {exc}"

        await asyncio.sleep(12)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if broker.has_credentials():
        broker.login()
    task = asyncio.create_task(trading_loop())
    yield
    task.cancel()


app = FastAPI(title="ArthaDesk Fast-Stock Auto Bot", lifespan=lifespan)


@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(
        {
            "connected": broker.is_connected,
            "last_error": broker.last_error,
            "bot_running": engine.bot_running,
            "scan_message": last_scan_message,
            "settings": asdict(settings),
            "virtual_capital": engine.virtual_capital,
            "realized_pnl": engine.realized_pnl,
            "active_position": asdict(engine.active_position) if engine.active_position else None,
            "trade_logs": [asdict(t) for t in engine.trade_logs[:25]],
            "watchlist": list(DEFAULT_WATCHLIST.keys()),
        }
    )


@app.post("/api/control")
async def control_bot(request: Request) -> JSONResponse:
    data = await request.json()
    action = data.get("action")
    if action == "START":
        if not broker.is_connected:
            broker.login()
        engine.bot_running = True
    elif action == "STOP":
        engine.bot_running = False
    elif action == "RELOGIN":
        broker.login()
    elif action == "EMERGENCY_EXIT":
        if engine.active_position:
            pos = engine.active_position
            ltp = broker.get_ltp(pos.symbol) or pos.entry_price
            engine.close_position(ltp, "Manual Emergency Exit", datetime.now(IST).strftime("%H:%M:%S IST"), settings, _live_order_callback)
        engine.bot_running = False
    return JSONResponse({"status": True, "bot_running": engine.bot_running})


@app.post("/api/settings")
async def update_settings(request: Request) -> JSONResponse:
    global settings
    data = await request.json()
    settings.symbol = str(data.get("symbol", "AUTO_ALL")).upper()
    settings.quantity = max(1, int(data.get("quantity", 1)))
    settings.paper_trading = bool(data.get("paper_trading", True))
    return JSONResponse({"status": True, "settings": asdict(settings)})


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    options_html = '<option value="AUTO_ALL">🌐 सभी 15 तेज़ शेयर (Zero Banks - Auto-Scan)</option>'
    for sym in DEFAULT_WATCHLIST.keys():
        options_html += f'<option value="{sym}">{sym}</option>'

    return f"""<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ArthaDesk Fast-Stock Bot</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0b1120; color: #f8fafc; margin: 0; padding: 14px; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 14px; border: 1px solid #334155; }}
  h2 {{ margin: 0 0 8px 0; font-size: 1.25rem; color: #38bdf8; }}
  .badge {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 0.8rem; font-weight: 700; }}
  .green {{ background: #065f46; color: #6ee7b7; }}
  .red {{ background: #7f1d1d; color: #fca5a5; }}
  .amber {{ background: #78350f; color: #fcd34d; }}
  .btn {{ width: 100%; padding: 13px; border: none; border-radius: 8px; font-size: 1rem; font-weight: 700; cursor: pointer; margin-top: 8px; }}
  .btn-start {{ background: #10b981; color: #022c22; }}
  .btn-stop {{ background: #ef4444; color: #fff; }}
  .btn-blue {{ background: #3b82f6; color: #fff; }}
  .btn-warn {{ background: #f59e0b; color: #1e1b4b; }}
  select, input {{ width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #fff; font-size: 1rem; margin-top: 6px; box-sizing: border-box; }}
  .row {{ display: flex; justify-content: space-between; margin: 6px 0; font-size: 0.95rem; }}
  .log {{ background: #0f172a; padding: 10px; border-radius: 8px; margin-top: 8px; font-size: 0.85rem; border-left: 3px solid #38bdf8; }}
</style>
</head>
<body>
  <div class="card">
    <h2>⚡ ArthaDesk 15 Fast-Stock Bot</h2>
    <div id="connBadge" class="badge amber">Connecting...</div>
    <div id="modeBadge" class="badge green">PAPER MODE</div>
    <p id="scanMsg" style="margin: 10px 0 0 0; color: #7dd3fc; font-weight: 600;">लोड हो रहा है...</p>
  </div>

  <div class="card">
    <div class="row"><span>बॉट स्थिति:</span><strong id="botStatus">-</strong></div>
    <div class="row"><span>स्कैन मोड:</span><strong id="scanMode">-</strong></div>
    <div class="row"><span>सक्रिय पोजीशन:</span><strong id="activePos">कोई नहीं</strong></div>
    <div class="row"><span>टारगेट / स्टॉप-लॉस:</span><strong id="slTpInfo">-</strong></div>
    <div class="row"><span>कुल ट्रेड / नफा-नुकसान:</span><strong id="pnlInfo">0 | ₹0.00</strong></div>
    <button class="btn btn-start" onclick="ctrl('START')">▶️ बॉट START करें</button>
    <button class="btn btn-stop" onclick="ctrl('STOP')">⏸️ बॉट STOP करें</button>
    <button class="btn btn-blue" onclick="ctrl('RELOGIN')">🔑 Angel One Re-Login करें</button>
    <button class="btn btn-warn" onclick="ctrl('EMERGENCY_EXIT')">🚨 EMERGENCY EXIT ALL</button>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">⚙️ ट्रेडिंग सेटिंग्स बदलें:</h3>
    <label>शेयर चुनें (Stock):</label>
    <select id="symbolSel">{options_html}</select>
    <label style="display:block; margin-top:10px;">मात्रा (Quantity):</label>
    <input type="number" id="qtyInp" value="1" min="1">
    <label style="display:block; margin-top:10px;">मोड चुनें:</label>
    <select id="modeSel">
      <option value="true">🧪 Paper Trading (नकली पैसे - सुरक्षित)</option>
      <option value="false">💸 LIVE TRADING (असली पैसे)</option>
    </select>
    <button class="btn btn-blue" onclick="saveSettings()">💾 सेटिंग सेव करें</button>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">📜 आज के ट्रेड लॉग (Trade History):</h3>
    <div id="logsBox">कोई ट्रेड नहीं</div>
  </div>

<script>
async function refresh() {{
  try {{
    const r = await fetch('/api/state');
    const d = await r.json();
    const cb = document.getElementById('connBadge');
    cb.textContent = d.connected ? '🟢 Angel One Connected' : '🔴 Disconnected';
    cb.className = 'badge ' + (d.connected ? 'green' : 'red');

    const mb = document.getElementById('modeBadge');
    mb.textContent = d.settings.paper_trading ? '🧪 PAPER MODE (नकली पैसे)' : '💸 LIVE MODE (असली पैसे)';
    mb.className = 'badge ' + (d.settings.paper_trading ? 'green' : 'red');

    document.getElementById('scanMsg').textContent = d.scan_message;
    document.getElementById('botStatus').textContent = d.bot_running ? '✅ चालू है (RUNNING)' : '⏸️ रुका हुआ (STOPPED)';
    document.getElementById('scanMode').textContent = (d.settings.symbol === 'AUTO_ALL' ? '🌐 सभी 15 तेज़ शेयर' : d.settings.symbol) + ' (' + d.settings.quantity + ' qty)';

    if (d.active_position) {{
      const p = d.active_position;
      document.getElementById('activePos').textContent = `${{p.side}} ${{p.symbol}} (${{p.quantity}} qty @ ₹${{p.entry_price}})`;
      document.getElementById('slTpInfo').textContent = `SL: ₹${{p.stop_loss_price}} | Target: ₹${{p.target_price}}`;
    }} else {{
      document.getElementById('activePos').textContent = 'कोई नहीं';
      document.getElementById('slTpInfo').textContent = '0.45% SL / 0.80% Target';
    }}

    document.getElementById('pnlInfo').textContent = `${{d.trade_logs.length}} ट्रेड | ₹${{d.realized_pnl.toFixed(2)}}`;

    const logsBox = document.getElementById('logsBox');
    if (d.trade_logs.length === 0) {{
      logsBox.innerHTML = 'कोई ट्रेड नहीं';
    }} else {{
      logsBox.innerHTML = d.trade_logs.map(t =>
        `<div class="log"><strong>${{t.time_ist}} | ${{t.action}} ${{t.symbol}} (${{t.quantity}} qty @ ₹${{t.price}})</strong><br>${{t.reason}} ${{t.pnl !== null ? ' | P&L: ₹' + t.pnl : ''}}</div>`
      ).join('');
    }}
  }} catch (e) {{}}
}}

async function ctrl(action) {{
  await fetch('/api/control', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{action}})
  }});
  refresh();
}}

async function saveSettings() {{
  await fetch('/api/settings', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      symbol: document.getElementById('symbolSel').value,
      quantity: parseInt(document.getElementById('qtyInp').value || '1'),
      paper_trading: document.getElementById('modeSel').value === 'true'
    }})
  }});
  alert('✅ सेटिंग सेव हो गई!');
  refresh();
}}

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>"""
