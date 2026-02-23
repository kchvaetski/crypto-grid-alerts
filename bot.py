import os
import math
import requests
from datetime import datetime, timezone

# ====== USER SETTINGS (hardcoded for now) ======
BTC_LOWER = 65800.0
BTC_UPPER = 69600.0
BTC_NEAR_PCT = 0.007  # 0.7%

SOL_LOWER = 80.0
SOL_UPPER = 88.0
SOL_NEAR_PCT = 0.01   # 1.0%

DOGE_LOWER = 0.094
DOGE_UPPER = 0.112
DOGE_NEAR_PCT = 0.01  # 1.0%

# RSI thresholds
RSI_OB = 70
RSI_OS = 30

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BINANCE_BASE = "https://api.binance.com"


# ---------- Telegram ----------
def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in env vars (GitHub Secrets).")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()


# ---------- Market Data ----------
def binance_price(symbol: str) -> float:
    # symbol like BTCUSDT, SOLUSDT, DOGEUSDT
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=25)
    r.raise_for_status()
    return float(r.json()["price"])


def binance_klines(symbol: str, interval: str, limit: int = 200):
    # returns list of klines; each is an array
    url = f"{BINANCE_BASE}/api/v3/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=25)
    r.raise_for_status()
    return r.json()


def _wilder_rsi(closes, period: int = 14) -> float:
    if len(closes) < period + 2:
        return float("nan")

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    # initial average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi)


def _wilder_atr(highs, lows, closes, period: int = 14) -> float:
    if len(closes) < period + 2:
        return float("nan")

    trs = []
    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return float(atr)


def btc_rsi_and_atr(interval: str):
    # interval: "1d" or "1w"
    kl = binance_klines("BTCUSDT", interval, limit=200)
    closes = [float(x[4]) for x in kl]
    highs = [float(x[2]) for x in kl]
    lows = [float(x[3]) for x in kl]
    rsi = _wilder_rsi(closes, 14)
    atr = _wilder_atr(highs, lows, closes, 14)
    return rsi, atr


# ---------- Alert Logic ----------
def near_boundary(price: float, lower: float, upper: float, near_pct: float):
    near_low = price <= lower * (1 + near_pct)
    near_up = price >= upper * (1 - near_pct)
    outside = (price < lower) or (price > upper)
    return near_low, near_up, outside


def fmt_money(x: float) -> str:
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    return f"{x:.6f}".rstrip("0").rstrip(".")


def rsi_state(rsi: float) -> str:
    if math.isnan(rsi):
        return "unknown"
    if rsi > RSI_OB:
        return "overbought"
    if rsi < RSI_OS:
        return "oversold"
    return "neutral"


def recommend_action_btc(price: float, lower: float, upper: float, atr_d: float, triggers: list) -> str:
    # Simple rule: if outside or near edges, suggest pause & shift range using ±1.5 ATR (daily)
    if math.isnan(atr_d) or atr_d <= 0:
        atr_d = 0.0

    shift = 1.5 * atr_d if atr_d else 0.0

    if "OUTSIDE_RANGE" in triggers:
        if price > upper:
            if shift:
                return f"РЕКОМЕНДАЦИЯ: ПАУЗА бот. Цена выше диапазона. Пересоздать/сдвинуть диапазон вверх: новый LOWER≈{fmt_money(price - shift)} / UPPER≈{fmt_money(price + shift)} (±1.5 ATR)."
            return "РЕКОМЕНДАЦИЯ: ПАУЗА бот. Цена выше диапазона. Пересоздай диапазон выше (нет ATR в данных)."
        else:
            if shift:
                return f"РЕКОМЕНДАЦИЯ: ПАУЗА бот. Цена ниже диапазона. Пересоздать/сдвинуть диапазон вниз: новый LOWER≈{fmt_money(price - shift)} / UPPER≈{fmt_money(price + shift)} (±1.5 ATR)."
            return "РЕКОМЕНДАЦИЯ: ПАУЗА бот. Цена ниже диапазона. Пересоздай диапазон ниже (нет ATR в данных)."

    if "NEAR_LOWER" in triggers or "NEAR_UPPER" in triggers:
        if shift:
            return f"РЕКОМЕНДАЦИЯ: Пока НЕ выключать сразу. Если цена пробьёт границу — ПАУЗА и сдвиг диапазона вокруг цены: LOWER≈{fmt_money(price - shift)} / UPPER≈{fmt_money(price + shift)} (±1.5 ATR)."
        return "РЕКОМЕНДАЦИЯ: Пока оставить как есть. Если пробьём границу — ПАУЗА и пересоздать диапазон вокруг текущей цены."

    if "RSI_EXTREME" in triggers:
        return "РЕКОМЕНДАЦИЯ: RSI экстремальный. Для мини-грида безопаснее уменьшить агрессию/расширить диапазон или временно поставить на паузу, если начнутся резкие выносы."

    return "РЕКОМЕНДАЦИЯ: Leave as-is."


def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- Prices ---
    btc = binance_price("BTCUSDT")
    sol = binance_price("SOLUSDT")
    doge = binance_price("DOGEUSDT")

    # --- BTC RSI/ATR ---
    rsi_d, atr_d = btc_rsi_and_atr("1d")
    rsi_w, atr_w = btc_rsi_and_atr("1w")

    # --- BTC boundary checks ---
    btc_near_low, btc_near_up, btc_outside = near_boundary(btc, BTC_LOWER, BTC_UPPER, BTC_NEAR_PCT)

    triggers = []
    details = []

    if btc_outside:
        triggers.append("OUTSIDE_RANGE")
        if btc > BTC_UPPER:
            dist_usd = btc - BTC_UPPER
            dist_pct = (dist_usd / BTC_UPPER) * 100
            details.append(f"BTC вне диапазона: ВЫШЕ upper {fmt_money(BTC_UPPER)} на {fmt_money(dist_usd)} USD ({dist_pct:.3f}%).")
        else:
            dist_usd = BTC_LOWER - btc
            dist_pct = (dist_usd / BTC_LOWER) * 100
            details.append(f"BTC вне диапазона: НИЖЕ lower {fmt_money(BTC_LOWER)} на {fmt_money(dist_usd)} USD ({dist_pct:.3f}%).")
    else:
        if btc_near_low:
            triggers.append("NEAR_LOWER")
            dist_usd = btc - BTC_LOWER
            dist_pct = (dist_usd / BTC_LOWER) * 100
            details.append(f"BTC близко к LOWER {fmt_money(BTC_LOWER)}: расстояние {fmt_money(dist_usd)} USD ({dist_pct:.3f}%).")
        if btc_near_up:
            triggers.append("NEAR_UPPER")
            dist_usd = BTC_UPPER - btc
            dist_pct = (dist_usd / BTC_UPPER) * 100
            details.append(f"BTC близко к UPPER {fmt_money(BTC_UPPER)}: расстояние {fmt_money(dist_usd)} USD ({dist_pct:.3f}%).")

    # --- RSI checks ---
    rsi_flags = []
    if not math.isnan(rsi_d) and (rsi_d > RSI_OB or rsi_d < RSI_OS):
        rsi_flags.append(f"RSI(14) Daily={rsi_d:.2f} ({rsi_state(rsi_d)})")
    if not math.isnan(rsi_w) and (rsi_w > RSI_OB or rsi_w < RSI_OS):
        rsi_flags.append(f"RSI(14) Weekly={rsi_w:.2f} ({rsi_state(rsi_w)})")

    if rsi_flags:
        triggers.append("RSI_EXTREME")
        details.append("BTC RSI сигнал: " + " | ".join(rsi_flags))

    # --- SOL boundary checks ---
    sol_near_low, sol_near_up, sol_outside = near_boundary(sol, SOL_LOWER, SOL_UPPER, SOL_NEAR_PCT)
    sol_msg = None
    if sol_outside or sol_near_low or sol_near_up:
        if sol_outside:
            if sol > SOL_UPPER:
                dist = sol - SOL_UPPER
                pct = (dist / SOL_UPPER) * 100
                sol_msg = f"SOL ALERT: вне диапазона ВЫШЕ upper {SOL_UPPER} на {dist:.4f} ({pct:.2f}%)."
            else:
                dist = SOL_LOWER - sol
                pct = (dist / SOL_LOWER) * 100
                sol_msg = f"SOL ALERT: вне диапазона НИЖЕ lower {SOL_LOWER} на {dist:.4f} ({pct:.2f}%)."
        else:
            if sol_near_low:
                dist = sol - SOL_LOWER
                pct = (dist / SOL_LOWER) * 100
                sol_msg = f"SOL ALERT: близко к LOWER {SOL_LOWER}, расстояние {dist:.4f} ({pct:.2f}%)."
            if sol_near_up:
                dist = SOL_UPPER - sol
                pct = (dist / SOL_UPPER) * 100
                extra = f"SOL ALERT: близко к UPPER {SOL_UPPER}, расстояние {dist:.4f} ({pct:.2f}%)."
                sol_msg = (sol_msg + " | " + extra) if sol_msg else extra

    # --- DOGE boundary checks ---
    doge_near_low, doge_near_up, doge_outside = near_boundary(doge, DOGE_LOWER, DOGE_UPPER, DOGE_NEAR_PCT)
    doge_msg = None
    if doge_outside or doge_near_low or doge_near_up:
        if doge_outside:
            if doge > DOGE_UPPER:
                dist = doge - DOGE_UPPER
                pct = (dist / DOGE_UPPER) * 100
                doge_msg = f"DOGE ALERT: вне диапазона ВЫШЕ upper {DOGE_UPPER} на {dist:.6f} ({pct:.2f}%)."
            else:
                dist = DOGE_LOWER - doge
                pct = (dist / DOGE_LOWER) * 100
                doge_msg = f"DOGE ALERT: вне диапазона НИЖЕ lower {DOGE_LOWER} на {dist:.6f} ({pct:.2f}%)."
        else:
            if doge_near_low:
                dist = doge - DOGE_LOWER
                pct = (dist / DOGE_LOWER) * 100
                doge_msg = f"DOGE ALERT: близко к LOWER {DOGE_LOWER}, расстояние {dist:.6f} ({pct:.2f}%)."
            if doge_near_up:
                dist = DOGE_UPPER - doge
                pct = (dist / DOGE_UPPER) * 100
                extra = f"DOGE ALERT: близко к UPPER {DOGE_UPPER}, расстояние {dist:.6f} ({pct:.2f}%)."
                doge_msg = (doge_msg + " | " + extra) if doge_msg else extra

    # --- Build Telegram text ---
    header = f"🕒 {now}\nBTC={fmt_money(btc)} | SOL={sol:.4f} | DOGE={doge:.6f}"
    btc_rsi_line = f"BTC RSI(14): Daily={rsi_d:.2f}({rsi_state(rsi_d)}) | Weekly={rsi_w:.2f}({rsi_state(rsi_w)})"
    btc_bounds_line = f"BTC mini-grid: LOWER={fmt_money(BTC_LOWER)} / UPPER={fmt_money(BTC_UPPER)} | near={BTC_NEAR_PCT*100:.1f}%"

    any_alert = bool(triggers) or (sol_msg is not None) or (doge_msg is not None)

    if any_alert:
        lines = [header, btc_bounds_line, btc_rsi_line, ""]
        if details:
            lines.append("⚠️ BTC TRIGGERS:")
            lines.extend([f"- {d}" for d in details])
            lines.append("")
            lines.append(recommend_action_btc(btc, BTC_LOWER, BTC_UPPER, atr_d, triggers))
            lines.append("")

        if sol_msg:
            lines.append(sol_msg)
        else:
            lines.append("SOL: SAFE (не рядом с границами)")

        if doge_msg:
            lines.append(doge_msg)
        else:
            lines.append("DOGE: SAFE (не рядом с границами)")

        send_telegram("\n".join(lines))
    else:
        # one-line status
        send_telegram(f"✅ {now} SAFE | BTC={fmt_money(btc)} (внутри диапазона) | RSI D={rsi_state(rsi_d)}, W={rsi_state(rsi_w)} | SOL SAFE | DOGE SAFE")


if __name__ == "__main__":
    main()
