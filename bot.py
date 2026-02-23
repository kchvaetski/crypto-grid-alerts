import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import requests


# ==========================================================
# 1) TELEGRAM (вставь свои значения прямо сюда)
# ==========================================================
TELEGRAM_BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "PASTE_YOUR_CHAT_ID_HERE"


# ==========================================================
# 2) НАСТРОЙКИ СТРАТЕГИИ (УЖЕ ГОТОВЫ ПОД ТВОЙ АКТИВНЫЙ РЕЖИМ)
#    Можешь пока НЕ трогать
# ==========================================================
# BTC grid
BTC_LOWER = 65800.0
BTC_UPPER = 69600.0
BTC_NEAR_PCT = 0.7

# SOL grid
SOL_LOWER = 80.0
SOL_UPPER = 88.0
SOL_NEAR_PCT = 1.2

# RSI (для BTC и SOL)
RSI_OVERBOUGHT = 72.0
RSI_OVERSOLD = 28.0

# Только для SOL: tolerance (игнорировать микровыходы за диапазон)
SOL_OUTSIDE_TOL_PCT = 0.20  # 0.20%

# Мягкий порог: если выход меньше этого %, будет MONITOR вместо PAUSE
SOFT_OUTSIDE_PCT = 0.75  # 0.75%


# ==========================================================
# 3) API / COINS
# ==========================================================
CG_BASE = "https://api.coingecko.com/api/v3"

COINS: Dict[str, str] = {
    "BTC": "bitcoin",
    "SOL": "solana",
}

PAIR_CONFIG: Dict[str, Dict[str, float]] = {
    "BTC": {
        "lower": BTC_LOWER,
        "upper": BTC_UPPER,
        "near_pct": BTC_NEAR_PCT,
        "outside_tol_pct": 0.0,  # для BTC tolerance выключен
    },
    "SOL": {
        "lower": SOL_LOWER,
        "upper": SOL_UPPER,
        "near_pct": SOL_NEAR_PCT,
        "outside_tol_pct": SOL_OUTSIDE_TOL_PCT,  # только для SOL
    },
}


# ==========================================================
# 4) TELEGRAM SEND
# ==========================================================
def tg_send(text: str) -> bool:
    """
    Отправка сообщения в Telegram.
    """
    token = (TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (TELEGRAM_CHAT_ID or "").strip()

    if not token or not chat_id or "PASTE_YOUR_" in token or "PASTE_YOUR_" in chat_id:
        print("Telegram token/chat_id не заполнены. Сообщение было бы таким:\n")
        print(text)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram send error: {e!r}")
        print("Message was:\n", text)
        return False


# ==========================================================
# 5) HTTP HELPER (с retry)
# ==========================================================
def http_get_json(url: str, params: Optional[dict] = None, timeout: int = 30, retries: int = 3) -> dict:
    last_err = None
    headers = {"Accept": "application/json", "User-Agent": "grid-alert-bot/1.4"}

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}", response=r)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                sleep_sec = min(2 ** (attempt - 1), 10)
                print(f"HTTP attempt {attempt}/{retries} failed: {e!r}. Retrying in {sleep_sec}s...")
                time.sleep(sleep_sec)

    raise RuntimeError(f"Failed GET {url} after {retries} attempts: {last_err!r}")


# ==========================================================
# 6) COINGECKO DATA
# ==========================================================
def cg_simple_price_usd(symbol: str) -> float:
    coin_id = COINS[symbol]
    url = f"{CG_BASE}/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    data = http_get_json(url, params=params)

    if coin_id not in data or "usd" not in data[coin_id]:
        raise ValueError(f"Unexpected CoinGecko response for {symbol}: {data}")

    return float(data[coin_id]["usd"])


def cg_daily_closes_usd(symbol: str, days: int = 220) -> List[float]:
    """
    Берём daily close из CoinGecko market_chart (последняя цена дня).
    """
    coin_id = COINS[symbol]
    url = f"{CG_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": str(days)}
    data = http_get_json(url, params=params)

    prices = data.get("prices", [])
    if not isinstance(prices, list):
        raise ValueError(f"Unexpected CoinGecko market_chart response for {symbol}: {data}")

    by_day: Dict[str, List[float]] = {}
    for item in prices:
        if not isinstance(item, list) or len(item) < 2:
            continue
        ms, p = item[0], item[1]
        day = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).date().isoformat()
        by_day.setdefault(day, []).append(float(p))

    days_sorted = sorted(by_day.keys())
    closes = [by_day[d][-1] for d in days_sorted if by_day.get(d)]
    return closes


# ==========================================================
# 7) RSI
# ==========================================================
def rsi_14(closes: Optional[List[float]]) -> Optional[float]:
    period = 14
    if closes is None or len(closes) < period + 1:
        return None

    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def weekly_closes_from_daily(daily_closes: Optional[List[float]]) -> Optional[List[float]]:
    """
    Приближённый weekly close: каждую 7-ю дневную цену.
    """
    if not daily_closes or len(daily_closes) < 30:
        return None

    weekly: List[float] = []
    for i in range(len(daily_closes) - 1, -1, -7):
        weekly.append(daily_closes[i])
    weekly.reverse()
    return weekly


def rsi_status(rsi_value: Optional[float]) -> str:
    if rsi_value is None:
        return "RSI: n/a"
    if rsi_value > RSI_OVERBOUGHT:
        return f"RSI: OVERBOUGHT ({rsi_value:.1f})"
    if rsi_value < RSI_OVERSOLD:
        return f"RSI: OVERSOLD ({rsi_value:.1f})"
    return f"RSI: neutral ({rsi_value:.1f})"


def is_rsi_trigger(rsi_value: Optional[float]) -> bool:
    if rsi_value is None:
        return False
    return (rsi_value > RSI_OVERBOUGHT) or (rsi_value < RSI_OVERSOLD)


# ==========================================================
# 8) RANGE CHECKS (BTC / SOL)
# ==========================================================
def check_bounds(
    symbol: str,
    price: float,
    lower: float,
    upper: float,
    near_pct: float,
    outside_tol_pct: float = 0.0,
) -> Dict[str, Any]:
    """
    Возвращает структурированный результат:
    state = inside / near_lower / near_upper / outside_lower / outside_upper
    """
    result: Dict[str, Any] = {
        "symbol": symbol,
        "state": "inside",
        "triggers": [],
        "outside_dist_pct": None,
        "outside_dist_abs": None,
        "outside_side": None,
        "within_tolerance": False,
        "outside_tol_pct": outside_tol_pct,
    }

    # Границы "outside" с учетом tolerance
    lower_outside_level = lower * (1.0 - outside_tol_pct / 100.0)
    upper_outside_level = upper * (1.0 + outside_tol_pct / 100.0)

    # 1) Реальный OUTSIDE (с учетом tolerance)
    if price < lower_outside_level:
        dist_abs = lower - price
        dist_pct = (dist_abs / lower) * 100.0 if lower else 0.0
        result["state"] = "outside_lower"
        result["outside_dist_abs"] = dist_abs
        result["outside_dist_pct"] = dist_pct
        result["outside_side"] = "lower"
        result["triggers"].append(
            f"{symbol}: OUTSIDE ↓ ниже LOWER. Price={price:.8g} | LOWER={lower} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return result

    if price > upper_outside_level:
        dist_abs = price - upper
        dist_pct = (dist_abs / upper) * 100.0 if upper else 0.0
        result["state"] = "outside_upper"
        result["outside_dist_abs"] = dist_abs
        result["outside_dist_pct"] = dist_pct
        result["outside_side"] = "upper"
        result["triggers"].append(
            f"{symbol}: OUTSIDE ↑ выше UPPER. Price={price:.8g} | UPPER={upper} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return result

    # 2) Чуть вышли за границу, но в пределах tolerance (актуально для SOL)
    if price < lower:
        dist_abs = lower - price
        dist_pct = (dist_abs / lower) * 100.0 if lower else 0.0
        result["state"] = "near_lower"
        result["outside_dist_abs"] = dist_abs
        result["outside_dist_pct"] = dist_pct
        result["outside_side"] = "lower"
        result["within_tolerance"] = True
        result["triggers"].append(
            f"{symbol}: NEAR LOWER (within tolerance). Price={price:.8g} | LOWER={lower} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return result

    if price > upper:
        dist_abs = price - upper
        dist_pct = (dist_abs / upper) * 100.0 if upper else 0.0
        result["state"] = "near_upper"
        result["outside_dist_abs"] = dist_abs
        result["outside_dist_pct"] = dist_pct
        result["outside_side"] = "upper"
        result["within_tolerance"] = True
        result["triggers"].append(
            f"{symbol}: NEAR UPPER (within tolerance). Price={price:.8g} | UPPER={upper} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return result

    # 3) Обычные NEAR внутри диапазона
    near_lower_level = lower * (1.0 + near_pct / 100.0)
    if price <= near_lower_level:
        dist_abs = price - lower
        dist_pct = (dist_abs / lower) * 100.0 if lower else 0.0
        result["state"] = "near_lower"
        result["triggers"].append(
            f"{symbol}: NEAR LOWER. Price={price:.8g} | LOWER={lower} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return result

    near_upper_level = upper * (1.0 - near_pct / 100.0)
    if price >= near_upper_level:
        dist_abs = upper - price
        dist_pct = (dist_abs / upper) * 100.0 if upper else 0.0
        result["state"] = "near_upper"
        result["triggers"].append(
            f"{symbol}: NEAR UPPER. Price={price:.8g} | UPPER={upper} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return result

    return result


# ==========================================================
# 9) RECOMMENDATIONS (отдельно по каждой паре)
# ==========================================================
def pair_recommendation(
    symbol: str,
    bounds_result: Dict[str, Any],
    daily_rsi: Optional[float],
    weekly_rsi: Optional[float],
) -> List[str]:
    lines: List[str] = []
    state = bounds_result.get("state", "inside")
    outside_dist_pct = bounds_result.get("outside_dist_pct")
    within_tolerance = bool(bounds_result.get("within_tolerance", False))

    if state in ("outside_lower", "outside_upper"):
        # Мягкий режим: если выход небольшой -> MONITOR вместо PAUSE
        if outside_dist_pct is not None and outside_dist_pct < SOFT_OUTSIDE_PCT:
            lines.append(
                f"{symbol}: MONITOR (выход за диапазон небольшой: {outside_dist_pct:.3f}% < {SOFT_OUTSIDE_PCT:.3f}%)."
            )
            lines.append(
                f"{symbol}: Если отклонение увеличится или удержится, тогда PAUSE grid + shift range toward current price."
            )
        else:
            lines.append(f"{symbol}: PAUSE grid + shift range toward current price (эта пара вышла за диапазон).")
            lines.append(f"{symbol}: После стабилизации перенеси LOWER/UPPER ближе к текущей цене и включи заново.")

    elif state in ("near_lower", "near_upper"):
        if within_tolerance:
            lines.append(f"{symbol}: MONITOR (цена слегка вышла за границу, но в пределах tolerance).")
            lines.append(f"{symbol}: Можно пока не останавливать grid; смотри, не уйдёт ли дальше за диапазон.")
        else:
            lines.append(f"{symbol}: Consider PAUSE (если волатильность выросла) или WIDEN range.")

    else:
        lines.append(f"{symbol}: Leave as-is.")

    # RSI hints
    if is_rsi_trigger(daily_rsi):
        lines.append(f"{symbol}: Daily RSI hint → возможен перегрев/перепроданность, уменьшить агрессию/расширить диапазон.")
    if is_rsi_trigger(weekly_rsi):
        lines.append(f"{symbol}: Weekly RSI hint → более сильный сигнал, лучше MONITOR/PAUSE и серьёзно оценить диапазон.")

    return lines


# ==========================================================
# 10) FORMAT HELPERS
# ==========================================================
def fmt_price(symbol: str, price: float) -> str:
    if symbol == "BTC":
        return f"${price:,.2f}"
    if symbol == "SOL":
        return f"${price:,.4f}"
    return f"${price}"


def fmt_range_for_header(symbol: str, lower: float, upper: float) -> str:
    if symbol == "BTC":
        return f"[{lower:,.0f} .. {upper:,.0f}]"
    return f"[{lower:g} .. {upper:g}]"


# ==========================================================
# 11) MAIN
# ==========================================================
def main() -> int:
    symbols = ["BTC", "SOL"]

    # 1) Prices
    prices: Dict[str, float] = {}
    try:
        for s in symbols:
            prices[s] = cg_simple_price_usd(s)
    except Exception as e:
        print(f"Price fetch error: {e!r}")
        return 1

    # 2) Price triggers / state per pair
    bounds_by_symbol: Dict[str, Dict[str, Any]] = {}
    for s in symbols:
        cfg = PAIR_CONFIG[s]
        bounds_by_symbol[s] = check_bounds(
            symbol=s,
            price=prices[s],
            lower=cfg["lower"],
            upper=cfg["upper"],
            near_pct=cfg["near_pct"],
            outside_tol_pct=cfg["outside_tol_pct"],
        )

    # 3) RSI daily/weekly per pair
    rsi_data: Dict[str, Dict[str, Optional[float]]] = {}
    for s in symbols:
        daily_rsi: Optional[float] = None
        weekly_rsi: Optional[float] = None
        try:
            daily_closes = cg_daily_closes_usd(s, days=220)
            daily_rsi = rsi_14(daily_closes)
            weekly_closes = weekly_closes_from_daily(daily_closes)
            weekly_rsi = rsi_14(weekly_closes) if weekly_closes else None
        except Exception as e:
            print(f"RSI calc error for {s}: {e!r}")

        rsi_data[s] = {
            "daily": daily_rsi,
            "weekly": weekly_rsi,
        }

    # 4) Нужно ли отправлять alert
    any_price_trigger = any(len(bounds_by_symbol[s]["triggers"]) > 0 for s in symbols)
    any_rsi_trigger = any(
        is_rsi_trigger(rsi_data[s]["daily"]) or is_rsi_trigger(rsi_data[s]["weekly"])
        for s in symbols
    )

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if any_price_trigger or any_rsi_trigger:
        lines: List[str] = []
        lines.append(f"🚨 GRID ALERTS ({now_utc})")
        lines.append("")

        # Summary
        for s in symbols:
            cfg = PAIR_CONFIG[s]
            p = prices[s]
            range_txt = fmt_range_for_header(s, cfg["lower"], cfg["upper"])
            tol_txt = ""
            if s == "SOL" and cfg["outside_tol_pct"] > 0:
                tol_txt = f" | Tol={cfg['outside_tol_pct']}%"
            lines.append(f"{s}: {fmt_price(s, p)} | Range {range_txt} | Near={cfg['near_pct']}%{tol_txt}")

        lines.append(f"Soft outside threshold (MONITOR<PAUSE): {SOFT_OUTSIDE_PCT}%")
        lines.append("")

        # Детали по каждой паре
        for idx, s in enumerate(symbols, start=1):
            cfg = PAIR_CONFIG[s]
            p = prices[s]
            bounds_result = bounds_by_symbol[s]
            pair_price_triggers: List[str] = bounds_result["triggers"]
            daily_rsi = rsi_data[s]["daily"]
            weekly_rsi = rsi_data[s]["weekly"]

            lines.append(f"==== {s} ====")
            lines.append(
                f"Price: {fmt_price(s, p)} | Range [{cfg['lower']} .. {cfg['upper']}] | "
                f"Near={cfg['near_pct']}% | Tol={cfg['outside_tol_pct']}%"
            )
            lines.append("")

            lines.append("📌 Price triggers:")
            if pair_price_triggers:
                for t in pair_price_triggers:
                    lines.append(f"• {t}")
            else:
                lines.append("• none")

            state_label = str(bounds_result.get("state", "inside"))
            outside_dist_pct = bounds_result.get("outside_dist_pct")
            if outside_dist_pct is not None:
                lines.append(f"• State: {state_label} | Deviation={float(outside_dist_pct):.3f}%")
            else:
                lines.append(f"• State: {state_label}")

            lines.append("")

            lines.append(f"📈 {s} RSI(14):")
            lines.append(f"• Daily: {rsi_status(daily_rsi)}")
            lines.append(f"• Weekly: {rsi_status(weekly_rsi)}")

            pair_rsi_triggers: List[str] = []
            if is_rsi_trigger(daily_rsi):
                pair_rsi_triggers.append(f"{s} Daily {rsi_status(daily_rsi)}")
            if is_rsi_trigger(weekly_rsi):
                pair_rsi_triggers.append(f"{s} Weekly {rsi_status(weekly_rsi)}")

            if pair_rsi_triggers:
                lines.append("")
                lines.append("📌 RSI triggers:")
                for t in pair_rsi_triggers:
                    lines.append(f"• {t}")

            lines.append("")
            lines.append("🧭 Recommendation:")
            for rec in pair_recommendation(s, bounds_result, daily_rsi, weekly_rsi):
                lines.append(f"• {rec}")

            if idx < len(symbols):
                lines.append("")
                lines.append("------------------------------")
                lines.append("")

        sent = tg_send("\n".join(lines))
        print("Alert sent." if sent else "Alert not sent (check token/chat_id).")
        return 0

    # SAFE (без alert в Telegram)
    safe_parts: List[str] = [f"SAFE ({now_utc})"]
    for s in symbols:
        daily_val = rsi_data[s]["daily"]
        weekly_val = rsi_data[s]["weekly"]
        daily_txt = "n/a" if daily_val is None else f"{daily_val:.1f}"
        weekly_txt = "n/a" if weekly_val is None else f"{weekly_val:.1f}"
        safe_parts.append(f"{s} {fmt_price(s, prices[s])} | RSI D {daily_txt} / W {weekly_txt}")

    print(" | ".join(safe_parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
