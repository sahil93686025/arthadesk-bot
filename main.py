from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from smartapi_service import AngelOneClient, NSE_INSTRUMENTS
from strategy_engine import IntradayStrategyEngine, RiskSettings, compute_indicators

IST = ZoneInfo("Asia/Kolkata")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("arthadesk.api")

angel_client = AngelOneClient()
engine = IntradayStrategyEngine()
settings = RiskSettings()
scheduler = BackgroundScheduler(timezone=IST)


def scheduled_9am_login() -> None:
    if not angel_client.auto_login_9am:
        return
    if angel_client.api_key and angel_client.client_id and angel_client.mpin and angel_client.totp_secret:
        try:
            angel_client.login()
            logger.info("09:00 AM IST automated SmartAPI session created.")
        except Exception as exc:
            logger.error("09:00 AM IST auto-login failed: %s", exc)


def scheduled_315pm_square_off() -> None:
    if engine.active_position:
        now_str = datetime.now(IST).strftime("%H:%M:%S IST")
        ltp = angel_client.get_ltp(engine.active_position.symbol) if not settings.paper_trading else engine.active_position.entry_price
        engine.close_position(ltp, "Mandatory 3:15 PM IST Auto Square-Off", now_str, settings, angel_client.place_intraday_market_order)


def strategy_tick() -> None:
    if not engine.bot_running or settings.paper_trading:
        return
    try:
        candles = angel_client.fetch_5min_candles(settings.symbol)
        df = compute_indicators(candles)
        ltp = angel_client.get_ltp(settings.symbol)
        engine.evaluate_bar(df, settings, ltp, datetime.now(IST), angel_client.place_intraday_market_order)
    except Exception as exc:
        logger.error("Strategy tick failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
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
    if not settings.paper_trading and not angel_client.jwt_token:
        raise HTTPException(status_code=400, detail="Authenticate Angel One SmartAPI before starting live trading")
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
    ltp = angel_client.get_ltp(engine.active_position.symbol) if not settings.paper_trading else engine.active_position.entry_price
    result = engine.close_position(ltp, "Emergency Exit All Triggered", now_str, settings, angel_client.place_intraday_market_order)
    return {"status": "exited", "result": result, "bot_running": False}
