from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pyotp
from logzero import logger
from SmartApi import SmartConnect

IST = ZoneInfo("Asia/Kolkata")

# सिर्फ Top 8 असली हाई-वॉल्यूम + हाई-स्पीड शेयर (कछुआ शेयर हटा दिए गए)
FAST_SYMBOLS: Dict[str, Dict[str, str]] = {
    "TATASTEEL": {"tradingsymbol": "TATASTEEL-EQ", "token": "3499", "name": "Tata Steel"},
    "TATAMOTORS": {"tradingsymbol": "TATAMOTORS-EQ", "token": "3456", "name": "Tata Motors"},
    "VEDL": {"tradingsymbol": "VEDL-EQ", "token": "3063", "name": "Vedanta"},
    "HINDALCO": {"tradingsymbol": "HINDALCO-EQ", "token": "1363", "name": "Hindalco"},
    "BEL": {"tradingsymbol": "BEL-EQ", "token": "383", "name": "Bharat Electronics"},
    "BHEL": {"tradingsymbol": "BHEL-EQ", "token": "438", "name": "BHEL"},
    "DLF": {"tradingsymbol": "DLF-EQ", "token": "14732", "name": "DLF"},
    "JINDALSTEL": {"tradingsymbol": "JINDALSTEL-EQ", "token": "6733", "name": "Jindal Steel"},
}


class AngelOneService:
    def __init__(self) -> None:
        self.api_key: str = os.getenv("ANGEL_API_KEY", "").strip()
        self.client_id: str = os.getenv("ANGEL_CLIENT_ID", "").strip()
        self.mpin: str = os.getenv("ANGEL_MPIN", "").strip()
        self.totp_secret: str = os.getenv("ANGEL_TOTP_SECRET", "").strip()
        self.smart_api: Optional[SmartConnect] = None
        self.connected: bool = False
        self.last_error: str = ""
        self._lock = threading.Lock()
        self._last_api_call_ts: float = 0.0

    def _throttle(self) -> None:
        # Angel One Rate Limit से पूरी तरह बचने के लिए कम से कम 1.2 सेकंड का गैप
        now = time.time()
        elapsed = now - self._last_api_call_ts
        if elapsed < 1.2:
            time.sleep(1.2 - elapsed)
        self._last_api_call_ts = time.time()

    def has_credentials(self) -> bool:
        return all([self.api_key, self.client_id, self.mpin, self.totp_secret])

    def login(self) -> Dict[str, Any]:
        if not self.has_credentials():
            self.connected = False
            self.last_error = "Missing Render Environment Variables"
            return {"ok": False, "error": self.last_error}

        try:
            with self._lock:
                self._throttle()
                totp = pyotp.TOTP(self.totp_secret).now()
                client = SmartConnect(api_key=self.api_key)
                session = client.generateSession(self.client_id, self.mpin, totp)
                if session and session.get("status"):
                    self.smart_api = client
                    self.connected = True
                    self.last_error = ""
                    logger.info(f"Angel One login successful for {self.client_id}")
                    return {"ok": True, "client_id": self.client_id}

                self.connected = False
                self.last_error = str(session.get("message", "Login failed"))
                return {"ok": False, "error": self.last_error}
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            logger.error(f"Angel One login exception: {exc}")
            return {"ok": False, "error": self.last_error}

    def get_candles_and_ltp(self, symbol: str = "TATASTEEL") -> Dict[str, Any]:
        """एक ही API कॉल में ताज़ा 5-मिनट कैंडल और मौजूदा भाव (LTP) दोनों लाता है"""
        if not self.connected or not self.smart_api:
            res = self.login()
            if not res.get("ok"):
                return {"candles": [], "ltp": 0.0}

        info = FAST_SYMBOLS.get(symbol, FAST_SYMBOLS["TATASTEEL"])
        now_ist = datetime.now(IST)
        from_dt = now_ist - timedelta(days=4)

        params = {
            "exchange": "NSE",
            "symboltoken": info["token"],
            "interval": "FIVE_MINUTE",
            "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
            "todate": now_ist.strftime("%Y-%m-%d %H:%M"),
        }
        try:
            with self._lock:
                self._throttle()
                resp = self.smart_api.getCandleData(params)
            if not resp or not resp.get("status"):
                msg = str(resp.get("message", "")) if resp else "Empty response"
                if "invalid token" in msg.lower() or "session" in msg.lower():
                    self.connected = False
                self.last_error = msg
                return {"candles": [], "ltp": 0.0}

            raw_rows = resp.get("data") or []
            candles: List[Dict[str, Any]] = []
            for row in raw_rows:
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
            ltp = candles[-1]["close"] if candles else 0.0
            self.last_error = ""
            return {"candles": candles, "ltp": ltp}
        except Exception as exc:
            self.last_error = str(exc)
            return {"candles": [], "ltp": 0.0}

    def place_intraday_order(self, symbol: str, side: str, quantity: int) -> str:
        if not self.connected or not self.smart_api:
            login_res = self.login()
            if not login_res.get("ok"):
                raise RuntimeError(f"Angel One not connected: {login_res.get('error')}")

        info = FAST_SYMBOLS.get(symbol)
        if not info:
            raise ValueError(f"Unsupported symbol: {symbol}")

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": info["tradingsymbol"],
            "symboltoken": info["token"],
            "transactiontype": side.upper(),
            "exchange": "NSE",
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(int(quantity)),
        }
        with self._lock:
            self._throttle()
            order_id = self.smart_api.placeOrder(order_params)
        if not order_id:
            raise RuntimeError("Angel One returned empty order ID")
        return str(order_id)
