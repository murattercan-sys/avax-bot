import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

TOKEN = os.getenv("TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

BITGET_BASE_URL = "https://api.bitget.com"
BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE")
FULL_AUTO_ENABLED = os.getenv("FULL_AUTO_ENABLED", "true").lower() == "true"

MIN_SCORE = 4
MAX_TRADES_PER_SYMBOL_PER_DAY = 2
MAX_OPEN_TRADES = 2
COOLDOWN_MINUTES = 45
RISK_REWARD = 1.5

last_update = None
last_signal_state: dict[str, str] = {}
last_signal_time: dict[str, datetime] = {}
trade_counter: dict[str, int] = {}
open_positions: dict[str, dict[str, Any]] = {}


@dataclass
class SignalResult:
    symbol: str
    side: str
    setup_type: str
    score: int
    entry: float
    stop_loss: float
    take_profit: float
    htf_trend: str
    reasons: list[str]


def send_message(chat_id: int, text: str) -> None:
    if not URL:
        return
    requests.get(f"{URL}/sendMessage", params={"chat_id": chat_id, "text": text}, timeout=10)


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    ema_value = sum(values[:period]) / period
    for value in values[period:]:
        ema_value = (value * k) + (ema_value * (1 - k))
    return ema_value


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0)
        loss = abs(min(delta, 0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def trend_1h(candles_1h: list[dict[str, float]]) -> str | None:
    closes = [c["close"] for c in candles_1h]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    if ema9 is None or ema21 is None:
        return None

    if ema9 > ema21:
        return "UP"
    if ema9 < ema21:
        return "DOWN"
    return "FLAT"


def find_levels(candles_15m: list[dict[str, float]]) -> tuple[float, float]:
    recent = candles_15m[-20:] if len(candles_15m) >= 20 else candles_15m
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)
    return support, resistance


def detect_setup(candles_15m: list[dict[str, float]], support: float, resistance: float) -> tuple[str | None, str | None]:
    if len(candles_15m) < 2:
        return None, None

    prev_close = candles_15m[-2]["close"]
    last_close = candles_15m[-1]["close"]
    last_volume = candles_15m[-1]["volume"]
    avg_volume = sum(c["volume"] for c in candles_15m[-20:]) / min(20, len(candles_15m))

    if prev_close < support and last_close > support:
        return "RECLAIM", "LONG"
    if prev_close > resistance and last_close < resistance:
        return "RECLAIM", "SHORT"

    if last_close > resistance and last_volume > avg_volume * 1.2:
        return "BREAKOUT", "LONG"
    if last_close < support and last_volume > avg_volume * 1.2:
        return "BREAKOUT", "SHORT"

    near_upper = abs(last_close - resistance) / max(resistance, 1e-8) < 0.002
    near_lower = abs(last_close - support) / max(support, 1e-8) < 0.002
    if near_upper and last_close < candles_15m[-1]["open"]:
        return "RANGE_EDGE", "SHORT"
    if near_lower and last_close > candles_15m[-1]["open"]:
        return "RANGE_EDGE", "LONG"

    return None, None


def in_cooldown(symbol: str) -> bool:
    if symbol not in last_signal_time:
        return False
    return datetime.now(timezone.utc) - last_signal_time[symbol] < timedelta(minutes=COOLDOWN_MINUTES)


def can_trade(symbol: str) -> tuple[bool, str | None]:
    if symbol in open_positions:
        return False, "one_position_per_symbol"
    if len(open_positions) >= MAX_OPEN_TRADES:
        return False, "max_open_trades"
    if trade_counter.get(symbol, 0) >= MAX_TRADES_PER_SYMBOL_PER_DAY:
        return False, "daily_limit"
    if in_cooldown(symbol):
        return False, "cooldown_active"
    return True, None


def evaluate_signal(payload: dict[str, Any]) -> SignalResult | None:
    symbol = payload["symbol"]
    candles_15m = payload["candles_15m"]
    candles_1h = payload["candles_1h"]
    btc_trend = payload.get("btc_trend", "NEUTRAL").upper()

    can_trade_now, reason = can_trade(symbol)
    if not can_trade_now:
        print(f"[{symbol}] blocked: {reason}")
        return None

    htf = trend_1h(candles_1h)
    if htf not in {"UP", "DOWN"}:
        return None

    support, resistance = find_levels(candles_15m)
    setup_type, side = detect_setup(candles_15m, support, resistance)
    if not setup_type or not side:
        return None

    if (htf == "UP" and side != "LONG") or (htf == "DOWN" and side != "SHORT"):
        return None

    closes = [c["close"] for c in candles_15m]
    last_close = closes[-1]
    rsi_value = rsi(closes, 14)
    vol_now = candles_15m[-1]["volume"]
    vol_avg = sum(c["volume"] for c in candles_15m[-20:]) / min(20, len(candles_15m))

    score = 0
    reasons = []

    if setup_type == "RECLAIM":
        score += 2
        reasons.append("reclaim")
    else:
        score += 1
        reasons.append(setup_type.lower())

    score += 1
    reasons.append("trend_aligned")

    if vol_now > vol_avg:
        score += 1
        reasons.append("volume")

    if rsi_value is not None and ((side == "LONG" and rsi_value > 50) or (side == "SHORT" and rsi_value < 50)):
        score += 1
        reasons.append("rsi")

    btc_ok = (btc_trend == "BULLISH" and side == "LONG") or (btc_trend == "BEARISH" and side == "SHORT")
    if btc_ok:
        score += 1
        reasons.append("btc_aligned")

    if score < MIN_SCORE:
        return None

    if setup_type == "RECLAIM":
        sl = support * 0.998 if side == "LONG" else resistance * 1.002
    else:
        sl = support if side == "LONG" else resistance

    if side == "LONG":
        risk = max(last_close - sl, 1e-8)
        tp = last_close + (risk * RISK_REWARD)
    else:
        risk = max(sl - last_close, 1e-8)
        tp = last_close - (risk * RISK_REWARD)

    signal_tag = f"{symbol}:{side}:{setup_type}:{round(last_close, 4)}"
    if last_signal_state.get(symbol) == signal_tag:
        return None

    return SignalResult(
        symbol=symbol,
        side=side,
        setup_type=setup_type,
        score=score,
        entry=last_close,
        stop_loss=sl,
        take_profit=tp,
        htf_trend=htf,
        reasons=reasons,
    )


def execute_trade(signal: SignalResult) -> tuple[bool, str]:
    if not FULL_AUTO_ENABLED:
        return False, "full_auto_disabled"

    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_PASSPHRASE):
        # Fallback mode: structured paper execution
        open_positions[signal.symbol] = {
            "side": signal.side,
            "entry": signal.entry,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "paper": True,
        }
        trade_counter[signal.symbol] = trade_counter.get(signal.symbol, 0) + 1
        last_signal_state[signal.symbol] = f"{signal.symbol}:{signal.side}:{signal.setup_type}:{round(signal.entry, 4)}"
        last_signal_time[signal.symbol] = datetime.now(timezone.utc)
        return True, "paper_trade_opened"

    # Credentials are present. In production, sign and send the Bitget order request.
    # For safety in this template, we only indicate readiness.
    return False, "bitget_live_execution_not_implemented"


def format_signal(signal: SignalResult) -> str:
    return (
        f"SYMBOL: {signal.symbol}\n"
        f"TYPE: {signal.side}\n"
        f"ENTRY TF: 15m\n"
        f"HTF: 1H {signal.htf_trend}\n"
        f"SCORE: {signal.score}\n\n"
        f"REASONS:\n- " + "\n- ".join(signal.reasons) + "\n\n"
        f"ENTRY: {signal.entry:.4f}\n"
        f"SL: {signal.stop_loss:.4f}\n"
        f"TP: {signal.take_profit:.4f}"
    )


def handle_command(text: str) -> str:
    clean = text.strip()

    if clean == "/start":
        return "🚀 Quant engine aktif. Komutlar: STATUS, ANALYZE {json}, EXECUTE {json}"

    if clean.upper() == "STATUS":
        return (
            "Mode: FULL AUTO\n"
            f"Open positions: {len(open_positions)}/{MAX_OPEN_TRADES}\n"
            f"Min score: {MIN_SCORE}\n"
            f"Cooldown: {COOLDOWN_MINUTES}m"
        )

    if clean.startswith("ANALYZE ") or clean.startswith("EXECUTE "):
        action, raw = clean.split(" ", 1)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return "Geçersiz JSON payload."

        try:
            signal = evaluate_signal(payload)
        except Exception as exc:  # guard runtime input errors
            return f"Hata: payload değerlendirilemedi ({exc})"

        if not signal:
            return "NO SIGNAL"

        message = format_signal(signal)

        if action == "EXECUTE":
            ok, reason = execute_trade(signal)
            if ok:
                return message + f"\n\nEXECUTION: SUCCESS ({reason})"
            return message + f"\n\nEXECUTION: SKIPPED ({reason})"

        return message

    return f"Gelen mesaj: {text}"


while True:
    if not URL:
        print("TOKEN env missing. Bot cannot poll Telegram.")
        time.sleep(5)
        continue

    try:
        response = requests.get(f"{URL}/getUpdates", timeout=20)
        updates = response.json().get("result", [])
    except Exception as e:
        print(f"Poll error: {e}")
        time.sleep(3)
        continue

    for update in updates:
        update_id = update.get("update_id")
        if update_id is None:
            continue

        if last_update is not None and update_id <= last_update:
            continue

        last_update = update_id

        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text", "")

        if not chat_id or not text:
            continue

        reply = handle_command(text)
        send_message(chat_id, reply)

    time.sleep(2)
