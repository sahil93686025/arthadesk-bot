from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
ENTRY_START = time(9, 25)
LUNCH_START = time(11, 30)   # दोपहर की सुस्ती शुरू
LUNCH_END = time(13, 15)     # दोपहर बाद की तेज़ रैली शुरू
ENTRY_END = time(14, 50)
SQUARE_OFF_TIME = time(15, 15)

MAX_HOLD_MINUTES = 35          # 35 मिनट में टारगेट न आए तो सुस्त ट्रेड तुरंत बंद
COOLDOWN_MINUTES = 45          # एक बार निकले हुए शेयर में 45 मिनट तक दोबारा एंट्री नहीं
VOLUME_SPIKE_MULTIPLIER = 1.5  # औसत से कम से कम 1.5 गुना असली वॉल्यूम धमाका ज़रूरी


@dataclass
class RiskSettings:
    symbol: str = "AUTO_ALL"
    quantity: int = 1
    stop_loss_pct: float = 0.45
    target_profit_pct: float = 0.80
    max_daily_loss: float = 2500.0
    paper_trading: bool = True


@dataclass
class ActivePosition:
    symbol: str
    side: str
    quantity: int
    entry_price: float
    stop_loss_price: float
    target_price: float
    entry_time_ist: str
    entry_ts: float
    order_id: str


@dataclass
class TradeLog:
    id: str
    time_ist: str
    symbol: str
    action: str
    quantity: int
    price: float
    reason: str
    pnl: Optional[float] = None
    mode: str = "PAPER"


def compute_indicators(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    if df.empty:
        return df

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    df["vwap"] = (typical_price * df["volume"]).cumsum() / cum_vol

    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema13"] = df["close"].ewm(span=13, adjust=False).mean()

    # पिछली 10 कैंडलों का औसत वॉल्यूम और पिछली 3 कैंडलों का हाई/लो (ब्रेकआउट के लिए)
    df["avg_vol"] = df["volume"].shift(1).rolling(window=10, min_periods=3).mean()
    df["prev3_high"] = df["high"].shift(1).rolling(window=3, min_periods=3).max()
    df["prev3_low"] = df["low"].shift(1).rolling(window=3, min_periods=3).min()

    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / 9, min_periods=5, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 9, min_periods=5, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100.0 - (100.0 / (1.0 + rs))
    df["rsi"] = df["rsi"].fillna(50.0)
    return df


class IntradayStrategyEngine:
    def __init__(self) -> None:
        self.virtual_capital: float = 100000.0
        self.realized_pnl: float = 0.0
        self.active_position: Optional[ActivePosition] = None
        self.trade_logs: List[TradeLog] = []
        self.bot_running: bool = True
        self.symbol_cooldowns: Dict[str, float] = {}  # शेयर का नाम -> कब तक कूलडाउन है

    def is_entry_window(self, now_ist: Optional[datetime] = None) -> bool:
        t = (now_ist or datetime.now(IST)).time()
        if not (ENTRY_START <= t <= ENTRY_END):
            return False
        # लंच टाइम (11:30 AM से 1:15 PM) में सुस्ती से बचने के लिए नई एंट्री बंद
        if LUNCH_START <= t < LUNCH_END:
            return False
        return True

    def is_square_off_time(self, now_ist: Optional[datetime] = None) -> bool:
        t = (now_ist or datetime.now(IST)).time()
        return t >= SQUARE_OFF_TIME

    def evaluate_bar(
        self,
        df: pd.DataFrame,
        settings: RiskSettings,
        ltp: float,
        now_ist: Optional[datetime] = None,
        execute_live_order: Optional[Any] = None,
        candidate_symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = now_ist or datetime.now(IST)
        now_ts = now.timestamp()
        now_str = now.strftime("%H:%M:%S IST")
        target_sym = candidate_symbol or settings.symbol

        if self.active_position:
            pos = self.active_position
            if pos.side == "BUY":
                unrealized = (ltp - pos.entry_price) * pos.quantity
                sl_hit = ltp <= pos.stop_loss_price
                tp_hit = ltp >= pos.target_price
            else:
                unrealized = (pos.entry_price - ltp) * pos.quantity
                sl_hit = ltp >= pos.stop_loss_price
                tp_hit = ltp <= pos.target_price

            held_minutes = (now_ts - pos.entry_ts) / 60.0

            if self.is_square_off_time(now):
                return self.close_position(ltp, "Auto Square-Off at 3:15 PM IST", now_str, settings, execute_live_order)
            if sl_hit:
                return self.close_position(pos.stop_loss_price, f"Stop Loss Hit ({settings.stop_loss_pct}%)", now_str, settings, execute_live_order)
            if tp_hit:
                return self.close_position(pos.target_price, f"Target Profit Hit ({settings.target_profit_pct}%)", now_str, settings, execute_live_order)
            if held_minutes >= MAX_HOLD_MINUTES:
                return self.close_position(ltp, f"Fast Time Exit ({MAX_HOLD_MINUTES} min)", now_str, settings, execute_live_order)

            total_pnl = self.realized_pnl + unrealized
            if total_pnl <= -abs(settings.max_daily_loss):
                res = self.close_position(ltp, "Max Daily Loss Limit Breached", now_str, settings, execute_live_order)
                self.bot_running = False
                return res

            return {"signal": "HOLD", "unrealized_pnl": round(unrealized, 2)}

        if not self.bot_running:
            return {"signal": "IDLE"}

        if self.realized_pnl <= -abs(settings.max_daily_loss):
            self.bot_running = False
            return {"signal": "HALTED_DAILY_LOSS"}

        if not self.is_entry_window(now):
            return {"signal": "OUTSIDE_WINDOW"}

        # अगर इस शेयर में अभी-अभी ट्रेड खत्म हुआ है, तो 45 मिनट तक दोबारा न घुसें
        cooldown_until = self.symbol_cooldowns.get(target_sym, 0.0)
        if now_ts < cooldown_until:
            return {"signal": "COOLDOWN"}

        if df is None or len(df) < 6:
            return {"signal": "WARMING_UP"}

        curr = df.iloc[-1]
        avg_vol = curr.get("avg_vol", np.nan)
        prev3_high = curr.get("prev3_high", np.nan)
        prev3_low = curr.get("prev3_low", np.nan)

        if pd.isna(avg_vol) or avg_vol <= 0 or pd.isna(prev3_high) or pd.isna(prev3_low):
            return {"signal": "WARMING_UP"}

        vol_ratio = float(curr["volume"]) / float(avg_vol)
        strong_volume = vol_ratio >= VOLUME_SPIKE_MULTIPLIER

        # असली ब्रेकआउट + वॉल्यूम स्पाइक सिग्नल (ताकि घुसते ही तेज़ चाल मिले)
        buy_signal = (
            strong_volume
            and (ltp > float(prev3_high))
            and (ltp > float(curr["vwap"]))
            and (float(curr["ema5"]) > float(curr["ema13"]))
            and (55.0 <= float(curr["rsi"]) <= 80.0)
        )
        sell_signal = (
            strong_volume
            and (ltp < float(prev3_low))
            and (ltp < float(curr["vwap"]))
            and (float(curr["ema5"]) < float(curr["ema13"]))
            and (20.0 <= float(curr["rsi"]) <= 45.0)
        )

        if buy_signal or sell_signal:
            side = "BUY" if buy_signal else "SELL"
            if side == "BUY":
                sl_price = round(ltp * (1.0 - settings.stop_loss_pct / 100.0), 2)
                tp_price = round(ltp * (1.0 + settings.target_profit_pct / 100.0), 2)
            else:
                sl_price = round(ltp * (1.0 + settings.stop_loss_pct / 100.0), 2)
                tp_price = round(ltp * (1.0 - settings.target_profit_pct / 100.0), 2)

            order_id = f"PAPER-{int(now_ts)}"
            if not settings.paper_trading and execute_live_order:
                order_id = execute_live_order(target_sym, side, settings.quantity)

            self.active_position = ActivePosition(
                symbol=target_sym,
                side=side,
                quantity=settings.quantity,
                entry_price=round(ltp, 2),
                stop_loss_price=sl_price,
                target_price=tp_price,
                entry_time_ist=now_str,
                entry_ts=now_ts,
                order_id=order_id,
            )
            self.trade_logs.insert(
                0,
                TradeLog(
                    id=order_id,
                    time_ist=now_str,
                    symbol=target_sym,
                    action=side,
                    quantity=settings.quantity,
                    price=round(ltp, 2),
                    reason=f"Breakout {side}: 3-Bar + VWAP + RSI({curr['rsi']:.1f}) | Vol: {vol_ratio:.1f}x",
                    mode="PAPER" if settings.paper_trading else "LIVE",
                ),
            )
            return {"signal": "BUY_EXECUTED", "symbol": target_sym, "side": side, "order_id": order_id}

        return {"signal": "NO_SIGNAL"}

    def close_position(
        self,
        exit_price: float,
        reason: str,
        time_str: str,
        settings: RiskSettings,
        execute_live_order: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if not self.active_position:
            return {"signal": "NO_OPEN_POSITION"}

        pos = self.active_position
        now_ts = datetime.now(IST).timestamp()
        # इस शेयर पर 45 मिनट का कूलडाउन लगा दें ताकि तुरंत दोबारा न घुसे
        self.symbol_cooldowns[pos.symbol] = now_ts + (COOLDOWN_MINUTES * 60)

        exit_side = "SELL" if pos.side == "BUY" else "BUY"
        order_id = f"EXIT-{int(now_ts)}"
        if not settings.paper_trading and execute_live_order:
            order_id = execute_live_order(pos.symbol, exit_side, pos.quantity)

        if pos.side == "BUY":
            pnl = round((exit_price - pos.entry_price) * pos.quantity, 2)
        else:
            pnl = round((pos.entry_price - exit_price) * pos.quantity, 2)

        self.realized_pnl = round(self.realized_pnl + pnl, 2)
        if settings.paper_trading:
            self.virtual_capital = round(100000.0 + self.realized_pnl, 2)

        self.trade_logs.insert(
            0,
            TradeLog(
                id=order_id,
                time_ist=time_str,
                symbol=pos.symbol,
                action=exit_side,
                quantity=pos.quantity,
                price=round(exit_price, 2),
                reason=reason,
                pnl=pnl,
                mode="PAPER" if settings.paper_trading else "LIVE",
            ),
        )
        self.active_position = None
        return {"signal": "EXITED", "pnl": pnl, "reason": reason}
