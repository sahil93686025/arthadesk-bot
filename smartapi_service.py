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
# ध्यान: कंपनी के नाम/टोकन बदल सकते हैं (जैसे TATAMOTORS का डिमर्जर)। अगर किसी शेयर में लगातार
# "डेटा प्रतीक्षा" दिखे, तो Angel One की scrip-master फ़ाइल से उसका टोकन दोबारा जाँचें।
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

RATE_LIMIT_MARKERS = ("exceeding access rate", "too many requests", "ab1021", "access denied because")
RATE_BACKOFF_STEPS = (20, 40, 80, 160, 300)   # लगातार रेट-लिमिट पर रुकने के सेकंड (अधिकतम 5 मिनट)
LOGIN_RETRY_GAP = 30                           # फेल लॉगिन के बाद कम से कम इतने सेकंड रुकें


def is_rate_limit_message(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in RATE_LIMIT_MARKERS)


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
        self._rate_strikes: int = 0          # लगातार कितनी बार रेट-लिमिट मिली
        self._cooldown_until: float = 0.0     # इस समय तक कोई डेटा-कॉल नहीं
        self._login_block_until: float = 0.0  # इस समय तक दोबारा लॉगिन की कोशिश नहीं

    # ---------------- रेट-लिमिट सुरक्षा ----------------
    def _throttle(self) -> None:
        # Angel One Rate Limit से बचने के लिए कम से कम 1.2 सेकंड का गैप
        now = time.time()
        elapsed = now - self._last_api_call_ts
        if elapsed < 1.2:
            time.sleep(1.2 - elapsed)
        self._last_api_call_ts = time.time()

    def cooldown_remaining(self) -> int:
        """रेट-लिमिट के बाद कितने सेकंड और रुकना है (0 = कॉल कर सकते हैं)"""
        return max(0, int(self._cooldown_until - time.time() + 0.999))

    def _register_rate_limit(self) -> None:
        step = RATE_BACKOFF_STEPS[min(self._rate_strikes, len(RATE_BACKOFF_STEPS) - 1)]
        self._rate_strikes += 1
        self._cooldown_until = time.time() + step
        self.last_error = f"Angel One रेट-लिमिट (Access rate) — {step} सेकंड रुककर दोबारा कोशिश होगी"
        logger.warning(self.last_error)

    def _register_ok(self) -> None:
        self._rate_strikes = 0
        self._cooldown_until = 0.0

    def has_credentials(self) -> bool:
        return all([self.api_key, self.client_id, self.mpin, self.totp_secret])

    # ---------------- लॉगिन ----------------
    def login(self, force: bool = False) -> Dict[str, Any]:
        if not self.has_credentials():
            self.connected = False
            self.last_error = "Missing Render Environment Variables"
            return {"ok": False, "error": self.last_error}

        # बार-बार लॉगिन ठोकने से रेट-लिमिट और बिगड़ती है; मैन्युअल Re-Login (force) पर रोक नहीं
        wait = int(self._login_block_until - time.time())
        if wait > 0 and not force:
            return {"ok": False, "error": f"लॉगिन {wait} सेकंड बाद दोबारा होगा"}

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
                    self._login_block_until = 0.0
                    logger.info(f"Angel One login successful for {self.client_id}")
                    return {"ok": True, "client_id": self.client_id}

                self.connected = False
                self.last_error = str((session or {}).get("message", "Login failed"))
                self._login_block_until = time.time() + LOGIN_RETRY_GAP
                return {"ok": False, "error": self.last_error}
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            self._login_block_until = time.time() + LOGIN_RETRY_GAP
            if is_rate_limit_message(self.last_error):
                self._register_rate_limit()
            logger.error(f"Angel One login exception: {exc}")
            return {"ok": False, "error": self.last_error}

    # ---------------- मार्केट डेटा ----------------
    def get_candles_and_ltp(self, symbol: str = "TATASTEEL") -> Dict[str, Any]:
        """एक ही API कॉल में ताज़ा 5-मिनट कैंडल और मौजूदा भाव (LTP) दोनों लाता है"""
        empty: Dict[str, Any] = {"candles": [], "ltp": 0.0}

        wait = self.cooldown_remaining()
        if wait > 0:
            # रेट-लिमिट के दौरान API को बिल्कुल न छेड़ें
            return {**empty, "rate_limited": True, "wait": wait}

        if not self.connected or not self.smart_api:
            res = self.login()
            if not res.get("ok"):
                return empty

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
                code = str(resp.get("errorcode", "")) if resp else ""
                self.last_error = msg
                if is_rate_limit_message(f"{msg} {code}"):
                    self._register_rate_limit()
                    return {**empty, "rate_limited": True, "wait": self.cooldown_remaining()}
                if "invalid token" in msg.lower() or "session" in msg.lower():
                    self.connected = False
                return empty

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
            self._register_ok()
            return {"candles": candles, "ltp": ltp}
        except Exception as exc:
            msg = str(exc)
            self.last_error = msg
            if is_rate_limit_message(msg):
                self._register_rate_limit()
                return {**empty, "rate_limited": True, "wait": self.cooldown_remaining()}
            return empty

    # ---------------- ऑर्डर ----------------
    def place_intraday_order(self, symbol: str, side: str, quantity: int) -> str:
        if not self.connected or not self.smart_api:
            login_res = self.login(force=True)
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
