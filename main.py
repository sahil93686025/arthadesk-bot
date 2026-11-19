from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from logzero import logger

from smartapi_service import FAST_SYMBOLS, AngelOneService
from strategy_engine import IntradayStrategyEngine, RiskSettings, compute_indicators

IST = ZoneInfo("Asia/Kolkata")
SCAN_LIST: List[str] = list(FAST_SYMBOLS.keys())

broker = AngelOneService()
engine = IntradayStrategyEngine()
settings = RiskSettings(
    symbol="AUTO_ALL",
    quantity=1,
    stop_loss_pct=0.45,
    target_profit_pct=0.80,
    max_daily_loss=2500.0,
    paper_trading=True,
)
last_tick_info: Dict[str, Any] = {
    "status": "Initializing Top 8 Fast-Momentum Sniper...",
    "ltp": 0.0,
    "unrealized_pnl": 0.0,
    "active_symbol": "-",
    "updated_at": "-",
}
scan_index: int = 0


async def trading_loop() -> None:
    global last_tick_info, scan_index
    await asyncio.sleep(2)
    while True:
        try:
            now_ist = datetime.now(IST)
            now_t = now_ist.time()

            if engine.active_position:
                target_sym = engine.active_position.symbol
            elif settings.symbol == "AUTO_ALL":
                target_sym = SCAN_LIST[scan_index % len(SCAN_LIST)]
                scan_index += 1
            else:
                target_sym = settings.symbol

            # एक ही कॉल में कैंडल + LTP दोनों लाएं (Rate Limit से पूरी तरह बचाव)
            market_data = await asyncio.to_thread(broker.get_candles_and_ltp, target_sym)
            candles = market_data["candles"]
            ltp = market_data["ltp"]

            if candles and ltp > 0:
                df = compute_indicators(candles)
                res = engine.evaluate_bar(
                    df=df,
                    settings=settings,
                    ltp=ltp,
                    now_ist=now_ist,
                    execute_live_order=broker.place_intraday_order,
                    candidate_symbol=target_sym,
                )
                if engine.active_position:
                    pos = engine.active_position
                    pnl_val = res.get("unrealized_pnl", 0.0)
                    status_msg = (
                        f"🔥 सक्रिय ट्रेड ({pos.side} {pos.symbol}): भाव ₹{ltp} | "
                        f"चल रहा P&L: {'+' if pnl_val >= 0 else ''}₹{pnl_val} | "
                        f"SL: ₹{pos.stop_loss_price} | Target: ₹{pos.target_price}"
                    )
                elif engine.bot_running and time(11, 30) <= now_t < time(13, 15):
                    status_msg = f"☕ लंच-टाइम सुरक्षा ब्रेक (11:30–1:15 PM नो एंट्री) | अगला सत्र 1:15 PM पर"
                elif engine.bot_running and time(9, 25) <= now_t <= time(14, 50):
                    status_msg = f"🎯 हाई-वॉल्यूम ब्रेकआउट स्कैन ({target_sym} @ ₹{ltp})..."
                else:
                    status_msg = f"⏸️ ट्रेडिंग समय की प्रतीक्षा ({target_sym} @ ₹{ltp})"

                last_tick_info = {
                    "status": status_msg,
                    "ltp": ltp,
                    "unrealized_pnl": res.get("unrealized_pnl", 0.0),
                    "active_symbol": target_sym,
                    "updated_at": now_ist.strftime("%H:%M:%S IST"),
                }
            else:
                err = broker.last_error or "मार्केट डेटा प्रतीक्षा"
                last_tick_info["status"] = f"⏳ डेटा प्रतीक्षा ({target_sym}): {err}"
                last_tick_info["updated_at"] = now_ist.strftime("%H:%M:%S IST")
        except Exception as exc:
            logger.error(f"Trading loop error: {exc}")
            last_tick_info["status"] = f"⚠️ त्रुटि: {exc}"

        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(broker.login)
    task = asyncio.create_task(trading_loop())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="ArthaDesk Top-8 Breakout Sniper Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "connected": broker.connected, "running": engine.bot_running}


@app.post("/control")
async def control_bot(action: str = Form(...)) -> RedirectResponse:
    if action == "start":
        if not broker.connected:
            await asyncio.to_thread(broker.login)
        engine.bot_running = True
    elif action == "stop":
        engine.bot_running = False
    elif action == "relogin":
        await asyncio.to_thread(broker.login)
    elif action == "squareoff":
        if engine.active_position:
            sym = engine.active_position.symbol
            md = await asyncio.to_thread(broker.get_candles_and_ltp, sym)
            ltp = md["ltp"] or engine.active_position.entry_price
            engine.close_position(
                ltp,
                "Manual Emergency Square-Off",
                datetime.now(IST).strftime("%H:%M:%S IST"),
                settings,
                broker.place_intraday_order,
            )
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings")
async def update_settings(
    symbol: str = Form(...),
    quantity: int = Form(...),
    stop_loss_pct: float = Form(...),
    target_profit_pct: float = Form(...),
    max_daily_loss: float = Form(...),
    mode: str = Form(...),
) -> RedirectResponse:
    settings.symbol = symbol if (symbol == "AUTO_ALL" or symbol in FAST_SYMBOLS) else "AUTO_ALL"
    settings.quantity = max(1, int(quantity))
    settings.stop_loss_pct = max(0.1, float(stop_loss_pct))
    settings.target_profit_pct = max(0.1, float(target_profit_pct))
    settings.max_daily_loss = max(100.0, float(max_daily_loss))
    settings.paper_trading = mode == "PAPER"
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    conn_badge = (
        '<span class="badge ok">🟢 Angel One Connected</span>'
        if broker.connected
        else f'<span class="badge err">🔴 Disconnected ({broker.last_error or "Check Env"})</span>'
    )
    mode_badge = (
        '<span class="badge paper">🧪 PAPER MODE (नकली पैसे)</span>'
        if settings.paper_trading
        else '<span class="badge live">💸 LIVE TRADING (असली पैसे)</span>'
    )
    run_text = "✅ चालू है (RUNNING)" if engine.bot_running else "⛔ रुका हुआ है (STOPPED)"

    pos_html = "कोई नहीं"
    sl_tp_display = f"{settings.stop_loss_pct}% SL / {settings.target_profit_pct}% Target"
    if engine.active_position:
        p = engine.active_position
        pos_html = f"<strong>{p.side} {p.symbol}</strong> ({p.quantity} qty @ ₹{p.entry_price})"
        sl_tp_display = f"SL: ₹{p.stop_loss_price} | Target: ₹{p.target_price}"

    options_list = [
        f'<option value="AUTO_ALL" {"selected" if settings.symbol == "AUTO_ALL" else ""}>'
        f'🌐 सभी Top-8 तूफ़ानी शेयर (Breakout Auto-Scan)</option>'
    ]
    for sym, info in FAST_SYMBOLS.items():
        sel = "selected" if sym == settings.symbol else ""
        options_list.append(f'<option value="{sym}" {sel}>{sym} - {info["name"]}</option>')
    options_html = "".join(options_list)

    rows_html = ""
    for log in engine.trade_logs[:20]:
        pnl_str = f"₹{log.pnl}" if log.pnl is not None else "-"
        rows_html += (
            f"<tr><td>{log.time_ist}</td><td>{log.symbol}</td><td>{log.action}</td>"
            f"<td>{log.quantity}</td><td>₹{log.price}</td><td>{pnl_str}</td><td>{log.reason}</td></tr>"
        )
    if not rows_html:
        rows_html = '<tr><td colspan="7" style="text-align:center;color:#94a3b8;">अभी तक कोई ट्रेड नहीं हुआ है।</td></tr>'

    pnl_color = "#22c55e" if engine.realized_pnl >= 0 else "#ef4444"

    return f"""<!DOCTYPE html>
<html lang="hi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="12" />
  <title>ArthaDesk Top-8 Breakout Sniper</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 14px; }}
    .wrap {{ max-width: 780px; margin: auto; }}
    .card {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 14px; border: 1px solid #334155; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 10px; color: #38bdf8; }}
    .badge {{ display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 0.82rem; font-weight: 600; margin-right: 6px; }}
    .ok {{ background: #064e3b; color: #6ee7b7; }}
    .err {{ background: #7f1d1d; color: #fca5a5; }}
    .paper {{ background: #1e3a8a; color: #93c5fd; }}
    .live {{ background: #7c2d12; color: #fdba74; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }}
    .btn {{ width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 1rem; font-weight: 700; cursor: pointer; margin-top: 8px; }}
    .btn-start {{ background: #16a34a; color: white; }}
    .btn-stop {{ background: #dc2626; color: white; }}
    .btn-relogin {{ background: #2563eb; color: white; }}
    .btn-exit {{ background: #d97706; color: white; }}
    .btn-save {{ background: #0284c7; color: white; }}
    label {{ display: block; font-size: 0.85rem; color: #cbd5e1; margin-top: 10px; }}
    input, select {{ width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #f8fafc; margin-top: 4px; box-sizing: border-box; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-top: 8px; }}
    th, td {{ border-bottom: 1px solid #334155; padding: 8px 6px; text-align: left; }}
    th {{ color: #94a3b8; }}
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>⚡ ArthaDesk Top-8 Breakout Sniper</h1>
    <div>{conn_badge} {mode_badge}</div>
    <p style="margin:10px 0 0;color:#fde047;font-weight:600;">{last_tick_info["status"]}</p>
    <p style="margin:4px 0 0;font-size:0.78rem;color:#94a3b8;">अंतिम अपडेट: {last_tick_info["updated_at"]} (हर 12 सेकंड में ऑटो-रिफ्रेश)</p>
  </div>

  <div class="card">
    <div class="grid">
      <div>बॉट स्थिति:</div><div style="text-align:right;font-weight:700;">{run_text}</div>
      <div>स्कैन मोड:</div><div style="text-align:right;">{"सभी Top-8 तूफ़ानी शेयर" if settings.symbol == "AUTO_ALL" else settings.symbol} ({settings.quantity} qty)</div>
      <div>सक्रिय पोजीशन:</div><div style="text-align:right;">{pos_html}</div>
      <div>टारगेट / स्टॉप-लॉस:</div><div style="text-align:right;">{sl_tp_display}</div>
      <div>कुल ट्रेड / नफा-नुकसान:</div><div style="text-align:right;color:{pnl_color};font-weight:700;">{len(engine.trade_logs)} ट्रेड | ₹{engine.realized_pnl}</div>
    </div>

    <form method="post" action="/control">
      <button class="btn btn-start" name="action" value="start" type="submit">▶️ बॉट START करें</button>
      <button class="btn btn-stop" name="action" value="stop" type="submit">⏸️ बॉट STOP करें</button>
      <button class="btn btn-relogin" name="action" value="relogin" type="submit">🔑 Angel One Re-Login करें</button>
      <button class="btn btn-exit" name="action" value="squareoff" type="submit">🚨 EMERGENCY EXIT ALL</button>
    </form>
  </div>

  <div class="card">
    <h2 style="font-size:1.05rem;margin:0 0 6px;color:#38bdf8;">⚙️ ट्रेडिंग सेटिंग्स बदलें:</h2>
    <form method="post" action="/settings">
      <label>शेयर चुनें (Top-8 High-Volume Only):</label>
      <select name="symbol">{options_html}</select>

      <label>मात्रा (Quantity):</label>
      <input type="number" name="quantity" value="{settings.quantity}" min="1" required />

      <label>स्टॉप-लॉस (%):</label>
      <input type="number" step="0.05" name="stop_loss_pct" value="{settings.stop_loss_pct}" required />

      <label>टारगेट प्रॉफिट (%):</label>
      <input type="number" step="0.05" name="target_profit_pct" value="{settings.target_profit_pct}" required />

      <label>अधिकतम दैनिक नुकसान सीमा (₹):</label>
      <input type="number" step="50" name="max_daily_loss" value="{settings.max_daily_loss}" required />

      <label>ट्रेडिंग मोड (Mode):</label>
      <select name="mode">
        <option value="PAPER" {"selected" if settings.paper_trading else ""}>🧪 PAPER MODE (नकली पैसे - सुरक्षित टेस्ट)</option>
        <option value="LIVE" {"selected" if not settings.paper_trading else ""}>💸 LIVE TRADING (असली पैसे - Angel One)</option>
      </select>

      <button class="btn btn-save" type="submit">💾 सेटिंग सेव करें</button>
    </form>
  </div>

  <div class="card">
    <h2 style="font-size:1.05rem;margin:0 0 6px;color:#38bdf8;">📜 आज के ट्रेड लॉग्स:</h2>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr><th>समय</th><th>शेयर</th><th>साइड</th><th>Qty</th><th>भाव</th><th>P&L</th><th>कारण</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>"""
