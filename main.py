from __future__ import annotations

import asyncio
import html
import os
import secrets
import time as _time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from logzero import logger

from smartapi_service import FAST_SYMBOLS, AngelOneService
from strategy_engine import IntradayStrategyEngine, RiskSettings, compute_indicators

IST = ZoneInfo("Asia/Kolkata")
SCAN_LIST: List[str] = list(FAST_SYMBOLS.keys())

# ---- सुरक्षा (Render Environment Variables में सेट करें) ----------------
DASH_USER = os.getenv("DASH_USER", "admin").strip() or "admin"
DASH_PASSWORD = os.getenv("DASH_PASSWORD", "").strip()          # खाली = बटन/सेटिंग बंद रहेंगे
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "").strip().upper() == "YES"  # YES के बिना LIVE मोड चालू नहीं होगा

broker = AngelOneService()
engine = IntradayStrategyEngine()
engine_lock = asyncio.Lock()   # एक ही समय में सिर्फ़ एक जगह से ट्रेड/एग्ज़िट (डबल ऑर्डर से बचाव)
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
last_prices: Dict[str, float] = {}
scan_index: int = 0
flash_msg: str = ""
flash_ts: float = 0.0


def set_flash(msg: str) -> None:
    global flash_msg, flash_ts
    flash_msg, flash_ts = msg, _time.time()


# ---------------- लॉगिन-सुरक्षा ----------------
security = HTTPBasic(auto_error=False)


def _check_auth(credentials: Optional[HTTPBasicCredentials], write: bool) -> None:
    if not DASH_PASSWORD:
        if write:
            raise HTTPException(
                status_code=503,
                detail="DASH_PASSWORD सेट नहीं है। Render > Environment में DASH_PASSWORD जोड़ें, तभी बटन/सेटिंग चलेंगे।",
            )
        return
    ok = False
    if credentials is not None:
        user_ok = secrets.compare_digest(credentials.username.encode("utf-8"), DASH_USER.encode("utf-8"))
        pass_ok = secrets.compare_digest(credentials.password.encode("utf-8"), DASH_PASSWORD.encode("utf-8"))
        ok = user_ok and pass_ok
    if not ok:
        raise HTTPException(status_code=401, detail="लॉगिन ज़रूरी है", headers={"WWW-Authenticate": 'Basic realm="ArthaDesk"'})


def read_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    _check_auth(credentials, write=False)


def write_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    _check_auth(credentials, write=True)


# ---------------- मुख्य ट्रेडिंग लूप ----------------
def idle_reason(now_ist: datetime) -> Optional[str]:
    """None = डेटा लाना चाहिए; वरना बताता है कि अभी API कॉल क्यों नहीं कर रहे"""
    now_t = now_ist.time()
    session_open = now_ist.weekday() < 5 and time(9, 15) <= now_t <= time(15, 30)
    if not session_open:
        return "🌙 बाज़ार बंद (सोम–शुक्र, 9:15–3:30) — API कॉल बंद हैं, अगले सत्र में अपने-आप चालू होंगे"
    if engine.active_position:
        return None
    if not engine.bot_running:
        return "⛔ बॉट STOP है — नई एंट्री नहीं हो रही"
    if engine.loss_halted:
        return "🛑 आज की अधिकतम नुकसान-सीमा पूरी — आज नई एंट्री बंद"
    if time(11, 30) <= now_t < time(13, 15):
        return "☕ लंच-टाइम सुरक्षा ब्रेक (11:30–1:15 PM नो एंट्री) | अगला सत्र 1:15 PM पर"
    if not engine.is_entry_window(now_ist):
        return "⏸️ अगली एंट्री विंडो की प्रतीक्षा (सुबह 9:25–11:30, दोपहर 1:15–2:50)"
    return None


async def trading_loop() -> None:
    global last_tick_info, scan_index
    await asyncio.sleep(2)
    while True:
        try:
            now_ist = datetime.now(IST)
            engine.roll_day(now_ist)
            engine.loss_halted = engine.realized_pnl <= -abs(settings.max_daily_loss)

            reason = idle_reason(now_ist)
            if reason:
                last_tick_info["status"] = reason
                last_tick_info["updated_at"] = now_ist.strftime("%H:%M:%S IST")
                await asyncio.sleep(60 if "बाज़ार बंद" in reason else 15)
                continue

            wait = broker.cooldown_remaining()
            if wait > 0:
                extra = " (पोजीशन खुली है — भाव मिलने तक इंतज़ार)" if engine.active_position else ""
                last_tick_info["status"] = f"⏳ Angel One रेट-लिमिट: {wait} सेकंड रुककर दोबारा कोशिश{extra}"
                last_tick_info["updated_at"] = now_ist.strftime("%H:%M:%S IST")
                await asyncio.sleep(min(wait, 15))
                continue

            if engine.active_position:
                target_sym = engine.active_position.symbol
            elif settings.symbol == "AUTO_ALL":
                target_sym = SCAN_LIST[scan_index % len(SCAN_LIST)]
                scan_index += 1
            else:
                target_sym = settings.symbol

            # एक ही कॉल में कैंडल + LTP दोनों लाएं
            market_data = await asyncio.to_thread(broker.get_candles_and_ltp, target_sym)
            candles = market_data["candles"]
            ltp = market_data["ltp"]

            if candles and ltp > 0:
                last_prices[target_sym] = ltp
                df = compute_indicators(candles)
                async with engine_lock:
                    res = await asyncio.to_thread(
                        engine.evaluate_bar,
                        df,
                        settings,
                        ltp,
                        now_ist,
                        broker.place_intraday_order,
                        target_sym,
                    )
                if engine.active_position:
                    pos = engine.active_position
                    pnl_val = res.get("unrealized_pnl", 0.0)
                    status_msg = (
                        f"🔥 सक्रिय ट्रेड ({pos.side} {pos.symbol}): भाव ₹{round(ltp, 2)} | "
                        f"चल रहा P&L: {'+' if pnl_val >= 0 else ''}₹{pnl_val} | "
                        f"SL: ₹{pos.stop_loss_price} | Target: ₹{pos.target_price}"
                    )
                else:
                    status_msg = f"🎯 हाई-वॉल्यूम ब्रेकआउट स्कैन ({target_sym} @ ₹{round(ltp, 2)})..."
                if res.get("signal") in ("ORDER_FAILED", "EXIT_FAILED"):
                    status_msg = f"❌ {engine.last_error}"

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
    return {
        "status": "ok",
        "connected": broker.connected,
        "running": engine.bot_running,
        "rate_limit_wait_sec": broker.cooldown_remaining(),
    }


@app.post("/control", dependencies=[Depends(write_auth)])
async def control_bot(action: str = Form(...)) -> RedirectResponse:
    if action == "start":
        if not broker.connected:
            await asyncio.to_thread(broker.login, True)
        engine.bot_running = True
        set_flash("▶️ बॉट START हुआ।")
    elif action == "stop":
        engine.bot_running = False
        set_flash("⏸️ बॉट STOP हुआ (खुली पोजीशन, अगर कोई है, तब भी SL/टारगेट पर निगरानी चलती रहेगी)।")
    elif action == "relogin":
        res = await asyncio.to_thread(broker.login, True)
        set_flash("🔑 Re-Login सफल।" if res.get("ok") else f"🔑 Re-Login फेल: {res.get('error')}")
    elif action == "squareoff":
        async with engine_lock:
            if engine.active_position:
                sym = engine.active_position.symbol
                ltp = last_prices.get(sym) or engine.active_position.entry_price
                if broker.cooldown_remaining() == 0:
                    md = await asyncio.to_thread(broker.get_candles_and_ltp, sym)
                    ltp = md.get("ltp") or ltp
                res = await asyncio.to_thread(
                    engine.close_position,
                    ltp,
                    "Manual Emergency Square-Off",
                    datetime.now(IST).strftime("%H:%M:%S IST"),
                    settings,
                    broker.place_intraday_order,
                )
                if res.get("signal") == "EXIT_FAILED":
                    set_flash(f"❌ EXIT ऑर्डर फेल: {res.get('error')} — ब्रोकर ऐप में पोजीशन ख़ुद जाँचें!")
                else:
                    set_flash("🚨 पोजीशन बंद कर दी गई।")
            else:
                set_flash("कोई सक्रिय पोजीशन नहीं है।")
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings", dependencies=[Depends(write_auth)])
async def update_settings(
    symbol: str = Form(...),
    quantity: int = Form(...),
    stop_loss_pct: float = Form(...),
    target_profit_pct: float = Form(...),
    max_daily_loss: float = Form(...),
    mode: str = Form(...),
    slippage_pct: float = Form(0.03),
    include_costs: str = Form("YES"),
) -> RedirectResponse:
    notes: List[str] = []
    settings.symbol = symbol if (symbol == "AUTO_ALL" or symbol in FAST_SYMBOLS) else "AUTO_ALL"
    settings.quantity = max(1, int(quantity))
    settings.stop_loss_pct = max(0.1, float(stop_loss_pct))
    settings.target_profit_pct = max(0.1, float(target_profit_pct))
    settings.max_daily_loss = max(100.0, float(max_daily_loss))
    settings.slippage_pct = min(1.0, max(0.0, float(slippage_pct)))
    settings.include_costs = include_costs == "YES"

    want_live = mode == "LIVE"   # सिर्फ़ साफ़ "LIVE" चुनने पर ही लाइव
    if want_live and not ALLOW_LIVE:
        want_live = False
        notes.append("🔒 LIVE मोड बंद है — चालू करने के लिए Render Environment में ALLOW_LIVE=YES डालें। मोड PAPER ही रहा।")
    if engine.active_position and want_live == settings.paper_trading:
        want_live = not settings.paper_trading
        notes.append("⚠️ पोजीशन खुली है, इसलिए मोड नहीं बदला। पहले पोजीशन बंद होने दें।")
    settings.paper_trading = not want_live
    set_flash(" ".join(notes) if notes else "💾 सेटिंग सेव हो गई।")
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(read_auth)])
async def dashboard() -> str:
    esc = html.escape
    conn_badge = (
        '<span class="badge ok">🟢 Angel One Connected</span>'
        if broker.connected
        else f'<span class="badge err">🔴 Disconnected ({esc(broker.last_error or "Check Env")})</span>'
    )
    mode_badge = (
        '<span class="badge paper">🧪 PAPER MODE (नकली पैसे)</span>'
        if settings.paper_trading
        else '<span class="badge live">💸 LIVE TRADING (असली पैसे)</span>'
    )
    run_text = "✅ चालू है (RUNNING)" if engine.bot_running else "⛔ रुका हुआ है (STOPPED)"
    if engine.bot_running and engine.loss_halted:
        run_text = "🛑 आज की नुकसान-सीमा पूरी"

    warn_html = ""
    if not DASH_PASSWORD:
        warn_html += '<div class="warn">⚠️ DASH_PASSWORD सेट नहीं है — सुरक्षा के लिए बटन और सेटिंग बंद हैं। Render › Environment में DASH_PASSWORD जोड़ें।</div>'
    if flash_msg and _time.time() - flash_ts < 90:
        warn_html += f'<div class="flash">{esc(flash_msg)}</div>'

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
        chg_str = f"₹{log.charges}" if log.charges is not None else "-"
        rows_html += (
            f"<tr><td>{log.time_ist}</td><td>{log.symbol}</td><td>{log.action}</td>"
            f"<td>{log.quantity}</td><td>₹{log.price}</td><td>{pnl_str}</td><td>{chg_str}</td><td>{esc(log.reason)}</td></tr>"
        )
    if not rows_html:
        rows_html = '<tr><td colspan="8" style="text-align:center;color:#94a3b8;">अभी तक कोई ट्रेड नहीं हुआ है।</td></tr>'

    closed_trades = sum(1 for l in engine.trade_logs if l.pnl is not None)
    pnl_color = "#22c55e" if engine.realized_pnl >= 0 else "#ef4444"
    cost_note = "खर्च+स्लिपेज के बाद" if (settings.include_costs and settings.paper_trading) else "खर्च गिने बिना" if settings.paper_trading else "लाइव"

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
    .warn {{ background: #7f1d1d; color: #fecaca; padding: 10px; border-radius: 8px; margin-bottom: 10px; font-size: 0.88rem; }}
    .flash {{ background: #14532d; color: #bbf7d0; padding: 10px; border-radius: 8px; margin-bottom: 10px; font-size: 0.88rem; }}
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
  {warn_html}
  <div class="card">
    <h1>⚡ ArthaDesk Top-8 Breakout Sniper</h1>
    <div>{conn_badge} {mode_badge}</div>
    <p style="margin:10px 0 0;color:#fde047;font-weight:600;">{esc(str(last_tick_info["status"]))}</p>
    <p style="margin:4px 0 0;font-size:0.78rem;color:#94a3b8;">अंतिम अपडेट: {last_tick_info["updated_at"]} (हर 12 सेकंड में ऑटो-रिफ्रेश)</p>
  </div>

  <div class="card">
    <div class="grid">
      <div>बॉट स्थिति:</div><div style="text-align:right;font-weight:700;">{run_text}</div>
      <div>स्कैन मोड:</div><div style="text-align:right;">{"सभी Top-8 तूफ़ानी शेयर" if settings.symbol == "AUTO_ALL" else settings.symbol} ({settings.quantity} qty)</div>
      <div>सक्रिय पोजीशन:</div><div style="text-align:right;">{pos_html}</div>
      <div>टारगेट / स्टॉप-लॉस:</div><div style="text-align:right;">{sl_tp_display}</div>
      <div>आज के बंद ट्रेड / नफा-नुकसान:</div><div style="text-align:right;color:{pnl_color};font-weight:700;">{closed_trades} ट्रेड | ₹{engine.realized_pnl}<br><span style="font-size:0.72rem;font-weight:400;color:#94a3b8;">({cost_note}; खर्च ₹{engine.total_charges})</span></div>
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

      <label>पेपर मोड स्लिपेज (% — भाव कितना ख़राब मानें):</label>
      <input type="number" step="0.01" min="0" max="1" name="slippage_pct" value="{settings.slippage_pct}" required />

      <label>पेपर मोड में ब्रोकरेज/टैक्स काटें?</label>
      <select name="include_costs">
        <option value="YES" {"selected" if settings.include_costs else ""}>हाँ (असली जैसा नतीजा)</option>
        <option value="NO" {"selected" if not settings.include_costs else ""}>नहीं</option>
      </select>

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
        <thead><tr><th>समय</th><th>शेयर</th><th>साइड</th><th>Qty</th><th>भाव</th><th>P&L</th><th>खर्च</th><th>कारण</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </div>
</div>
</body>
</html>"""
