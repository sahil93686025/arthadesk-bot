from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pyotp
from SmartApi import SmartConnect

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("arthadesk.smartapi")

NSE_INSTRUMENTS: Dict[str, Dict[str, str]] = {
    "RELIANCE": {"tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE"},
    "TATAMOTORS": {"tradingsymbol": "TATAMOTORS-EQ", "symboltoken": "3456", "exchange": "NSE"},
    "SBIN": {"tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "exchange": "NSE"},
    "HDFCBANK": {"tradingsymbol": "HDFCBANK-EQ", "symboltoken": "1333", "exchange": "NSE"},
    "INFY": {"tradingsymbol": "INFY-EQ", "symboltoken": "1594", "exchange": "NSE"},
    "ICICIBANK": {"tradingsymbol": "ICICIBANK-EQ", "symboltoken": "4963", "exchange": "NSE"},
    "AXISBANK": {"tradingsymbol": "AXISBANK-EQ", "symboltoken": "5900", "exchange": "NSE"},
    "TATASTEEL": {"tradingsymbol": "TATASTEEL-EQ", "symboltoken": "3499", "exchange": "NSE"},
    "BAJFINANCE": {"tradingsymbol": "BAJFINANCE-EQ", "symboltoken": "317", "exchange": "NSE"},
    "TCS": {"tradingsymbol": "TCS-EQ", "symboltoken": "11536", "exchange": "NSE"},
    "MARUTI": {"tradingsymbol": "MARUTI-EQ", "symboltoken": "10999", "exchange": "NSE"},
    "SUNPHARMA": {"tradingsymbol": "SUNPHARMA-EQ", "symboltoken": "3351", "exchange": "NSE"},
    "LT": {"tradingsymbol": "LT-EQ", "symboltoken": "11483", "exchange": "NSE"},
    "WIPRO": {"tradingsymbol": "WIPRO-EQ", "symboltoken": "3787", "exchange": "NSE"},
    "ADANIENT": {"tradingsymbol": "ADANIENT-EQ", "symboltoken": "25", "exchange": "NSE"},
}


class AngelOneClient:
    """Manages Angel One SmartAPI authentication via pyotp and intraday order/data calls."""

    def __init__(self) -> None:
        self.smart_api: Optional[SmartConnect] = None
        self.api_key: str = ""
        self.client_id: str = ""
        self.mpin: str = ""
        self.totp_secret: str = ""
        self.jwt_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        self.last_login_ist: Optional[str] = None
        self.auto_login_9am: bool = True

    def configure(self, api_key: str, client_id: str, mpin: str, totp_secret: str, auto_login_9am: bool = True) -> None:
        self.api_key = api_key.strip()
        self.client_id = client_id.strip()
        self.mpin = mpin.strip()
        self.totp_secret = totp_secret.strip().replace(" ", "").upper()
        self.auto_login_9am = auto_login_9am

    def generate_totp(self, secret: Optional[str] = None) -> Dict[str, Any]:
        key = (secret or self.totp_secret).strip().replace(" ", "").upper()
        if not key:
            raise ValueError("TOTP Secret Key is required")
        totp = pyotp.TOTP(key)
        code = totp.now()
        remaining = 30 - (int(datetime.now().timestamp()) % 30)
        return {"otp": code, "expires_in_seconds": remaining}

    def login(self) -> Dict[str, Any]:
        if not (self.api_key and self.client_id and self.mpin and self.totp_secret):
            raise ValueError("API Key, Client ID, 4-digit MPIN, and TOTP Secret Key are required")

        otp_info = self.generate_totp()
        self.smart_api = SmartConnect(api_key=self.api_key)
        response = self.smart_api.generateSession(self.client_id, self.mpin, otp_info["otp"])

        if not response or not response.get("status"):
            message = response.get("message", "SmartAPI login failed") if isinstance(response, dict) else "SmartAPI error"
            raise RuntimeError(message)

        data = response.get("data", {})
        self.jwt_token = data.get("jwtToken")
        self.refresh_token = data.get("refreshToken")
        self.feed_token = self.smart_api.getfeedToken()
        self.last_login_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

        logger.info("SmartAPI session created for client %s at %s", self.client_id, self.last_login_ist)
        return {
            "status": "authenticated",
            "client_id": self.client_id,
            "last_login_ist": self.last_login_ist,
            "feed_token_present": bool(self.feed_token),
        }

    def fetch_5min_candles(self, symbol: str, lookback_bars: int = 60) -> List[Dict[str, Any]]:
        if not self.smart_api or not self.jwt_token:
            raise RuntimeError("SmartAPI session not initialized. Login first.")

        meta = NSE_INSTRUMENTS[symbol]
        now_ist = datetime.now(IST)
        from_date = now_ist.strftime("%Y-%m-%d 09:15")
        to_date = now_ist.strftime("%Y-%m-%d %H:%M")

        historic_param = {
            "exchange": meta["exchange"],
            "symboltoken": meta["symboltoken"],
            "interval": "FIVE_MINUTE",
            "fromdate": from_date,
            "todate": to_date,
        }
        res = self.smart_api.getCandleData(historic_param)
        if not res or not res.get("status"):
            raise RuntimeError(res.get("message", "Failed to fetch 5-minute candles"))

        candles: List[Dict[str, Any]] = []
        for row in (res.get("data") or [])[-lookback_bars:]:
            candles.append(
                {
                    "timestamp": row[0],
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": int(row[5]),
                }
            )
        return candles

    def get_ltp(self, symbol: str) -> float:
        if not self.smart_api or not self.jwt_token:
            raise RuntimeError("SmartAPI session not initialized")
        meta = NSE_INSTRUMENTS[symbol]
        res = self.smart_api.ltpData(meta["exchange"], meta["tradingsymbol"], meta["symboltoken"])
        if not res or not res.get("status"):
            raise RuntimeError(res.get("message", "Failed to fetch LTP"))
        return float(res["data"]["ltp"])

    def place_intraday_market_order(self, symbol: str, side: str, quantity: int) -> str:
        if not self.smart_api or not self.jwt_token:
            raise RuntimeError("SmartAPI session not initialized")
        meta = NSE_INSTRUMENTS[symbol]
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": meta["tradingsymbol"],
            "symboltoken": meta["symboltoken"],
            "transactiontype": side,
            "exchange": meta["exchange"],
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(quantity),
        }
        order_id = self.smart_api.placeOrder(order_params)
        logger.info("Placed %s %s x%s order_id=%s", side, symbol, quantity, order_id)
        return str(order_id)
