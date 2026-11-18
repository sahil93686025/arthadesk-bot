from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pyotp
from SmartApi import SmartConnect

IST = ZoneInfo("Asia/Kolkata")

# 15 तेज़ रफ़्तार, हाई-वॉल्यूम नॉन-बैंकिंग शेयर (₹120 - ₹750 रेंज)
DEFAULT_WATCHLIST: Dict[str, Dict[str, str]] = {
    "TATASTEEL": {"exchange": "NSE", "tradingsymbol": "TATASTEEL-EQ", "symboltoken": "3499"},
    "TATAMOTORS": {"exchange": "NSE", "tradingsymbol": "TATAMOTORS-EQ", "symboltoken": "3456"},
    "HINDALCO": {"exchange": "NSE", "tradingsymbol": "HINDALCO-EQ", "symboltoken": "1363"},
    "VEDL": {"exchange": "NSE", "tradingsymbol": "VEDL-EQ", "symboltoken": "3063"},
    "WIPRO": {"exchange": "NSE", "tradingsymbol": "WIPRO-EQ", "symboltoken": "3787"},
    "ONGC": {"exchange": "NSE", "tradingsymbol": "ONGC-EQ", "symboltoken": "2475"},
    "NTPC": {"exchange": "NSE", "tradingsymbol": "NTPC-EQ", "symboltoken": "11630"},
    "POWERGRID": {"exchange": "NSE", "tradingsymbol": "POWERGRID-EQ", "symboltoken": "14977"},
    "COALINDIA": {"exchange": "NSE", "tradingsymbol": "COALINDIA-EQ", "symboltoken": "20374"},
    "BEL": {"exchange": "NSE", "tradingsymbol": "BEL-EQ", "symboltoken": "383"},
    "BHEL": {"exchange": "NSE", "tradingsymbol": "BHEL-EQ", "symboltoken": "438"},
    "SAIL": {"exchange": "NSE", "tradingsymbol": "SAIL-EQ", "symboltoken": "2963"},
    "IRCTC": {"exchange": "NSE", "tradingsymbol": "IRCTC-EQ", "symboltoken": "13611"},
    "DLF": {"exchange": "NSE", "tradingsymbol": "DLF-EQ", "symboltoken": "14732"},
    "JINDALSTEL": {"exchange": "NSE", "tradingsymbol": "JINDALSTEL-EQ", "symboltoken": "6733"},
}


class AngelSmartAPIService:
    """Thread-safe wrapper around Angel One SmartAPI with rate-limit protection."""

    def __init__(self) -> None:
        self.api_key = os.getenv("ANGEL_API_KEY", "").strip()
        self.client_id = os.getenv("ANGEL_CLIENT_ID", "").strip()
        self.mpin = os.getenv("ANGEL_MPIN", "").strip()
        self.totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

        self.smart_api: Optional[SmartConnect] = None
        self.is_connected: bool = False
        self.last_error: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        self.lock = threading.Lock()
        self._last_call_ts: float = 0.0
        self._candle_cache: Dict[str, Dict[str, Any]] = {}

    def _pace_request(self, min_interval: float = 0.45) -> None:
        now = time.monotonic()
        elapsed = now - self._last_call_ts
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call_ts = time.monotonic()

    def has_credentials(self) -> bool:
        return all([self.api_key, self.client_id, self.mpin, self.totp_secret])

    def login(self) -> Dict[str, Any]:
        if not self.has_credentials():
            self.is_connected = False
            self.last_error = "Missing Angel One environment variables on Render."
            return {"status": False, "message": self.last_error}

        with self.lock:
            try:
                self._pace_request(0.5)
                self.smart_api = SmartConnect(api_key=self.api_key)
                totp_code = pyotp.TOTP(self.totp_secret).now()
                session = self.smart_api.generateSession(self.client_id, self.mpin, totp_code)

                if not session or not session.get("status"):
                    self.is_connected = False
                    self.last_error = str(session.get("message", "Unknown SmartAPI login error"))
                    return {"status": False, "message": self.last_error}

                data = session.get("data", {})
                self.auth_token = data.get("jwtToken")
                self.feed_token = self.smart_api.getfeedToken()
                self.is_connected = True
                self.last_error = None
                return {"status": True, "message": "Connected to Angel One SmartAPI"}
            except Exception as exc:
                self.is_connected = False
                self.last_error = str(exc)
                return {"status": False, "message": self.last_error}

    def get_ltp(self, symbol: str) -> Optional[float]:
        meta = DEFAULT_WATCHLIST.get(symbol.upper())
        if not meta or not self.smart_api or not self.is_connected:
            return None

        with self.lock:
            try:
                self._pace_request(0.45)
                res = self.smart_api.ltpData(
                    meta["exchange"], meta["tradingsymbol"], meta["symboltoken"]
                )
                if res and res.get("status") and res.get("data"):
                    return float(res["data"]["ltp"])
                err_msg = str(res.get("message", "")) if isinstance(res, dict) else ""
                if "access rate" in err_msg.lower():
                    time.sleep(1.5)
                return None
            except Exception as exc:
                self.last_error = str(exc)
                return None

    def get_intraday_candles(self, symbol: str, interval: str = "FIVE_MINUTE") -> List[Dict[str, Any]]:
        sym_key = symbol.upper()
        meta = DEFAULT_WATCHLIST.get(sym_key)
        if not meta or not self.smart_api or not self.is_connected:
            return []

        now_ist = datetime.now(IST)
        cached = self._candle_cache.get(sym_key)
        if cached and (time.monotonic() - cached["ts"]) < 60.0:
            return cached["candles"]

        start_dt = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        if now_ist < start_dt:
            start_dt = start_dt - timedelta(days=1)

        params = {
            "exchange": meta["exchange"],
            "symboltoken": meta["symboltoken"],
            "interval": interval,
            "fromdate": start_dt.strftime("%Y-%m-%d 09:15"),
            "todate": now_ist.strftime("%Y-%m-%d %H:%M"),
        }

        with self.lock:
            try:
                self._pace_request(0.6)
                res = self.smart_api.getCandleData(params)
                if not res or not res.get("status") or not res.get("data"):
                    err_msg = str(res.get("message", "")) if isinstance(res, dict) else ""
                    if "access rate" in err_msg.lower():
                        time.sleep(2.0)
                    return cached["candles"] if cached else []

                candles: List[Dict[str, Any]] = []
                for row in res["data"]:
                    candles.append(
                        {
                            "timestamp": row[0],
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                        }
                    )
                self._candle_cache[sym_key] = {"ts": time.monotonic(), "candles": candles}
                return candles
            except Exception as exc:
                self.last_error = str(exc)
                return cached["candles"] if cached else []

    def place_intraday_market_order(self, symbol: str, transaction_type: str, quantity: int) -> Dict[str, Any]:
        meta = DEFAULT_WATCHLIST.get(symbol.upper())
        if not meta or not self.smart_api or not self.is_connected:
            return {"status": False, "message": "SmartAPI not connected or invalid symbol"}

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": meta["tradingsymbol"],
            "symboltoken": meta["symboltoken"],
            "transactiontype": transaction_type.upper(),
            "exchange": meta["exchange"],
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(int(quantity)),
        }

        with self.lock:
            try:
                self._pace_request(0.5)
                order_id = self.smart_api.placeOrder(order_params)
                return {"status": True, "order_id": str(order_id)}
            except Exception as exc:
                self.last_error = str(exc)
                return {"status": False, "message": str(exc)}
