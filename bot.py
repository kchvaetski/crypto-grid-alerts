import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests


# ========= Telegram =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def tg_send(text: str) -> bool:
    """
    Sends a Telegram message.
    Returns True if sent successfully, False otherwise (does not crash the workflow).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # Чтобы workflow не падал, просто печатаем в лог
        print("Telegram env vars missing. Message would be:\n", text)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
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


# ========= HTTP helpers =========
def http_get_json(url: str, params: Optional[dict] = None, timeout: int = 30, retries: int = 3) -> dict:
    """
    GET JSON with simple retry/backoff for transient errors (429/5xx/network).
    """
    last_err = None
    headers = {"Accept": "application/json", "User-Agent": "grid-alert-bot/1.1"}

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            # Retry on rate limit / server errors
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
            else:
                break

    raise RuntimeError(f"Failed GET {url} after {retries} attempts: {last_err!r}")


# ========= Settings from ENV =========
def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(str(v).strip())
    except ValueError:
        print(f"Invalid float in ENV {name}={v!r}, using default {default}")
        return float(default)


BTC_LOWER = env_float("BTC_LOWER", 65800.0)
BTC_UPPER = env_float("BTC_UPPER", 69600.0)
BTC_NEAR_PCT = env_float("BTC_NEAR_PCT", 0.7)  # percent

SOL_LOWER = env_float("SOL_LOWER", 80.0)
SOL_UPPER = env_float("SOL_UPPER", 88.0)
SOL_NEAR_PCT = env_float("SOL_NEAR_PCT", 1.0)

# RSI thresholds (общие для всех пар)
RSI_OB = env_float("RSI_OVERBOUGHT", 70.0)
RSI_OS = env_float("RSI_OVERSOLD", 30.0)

# Минимальная "зона допуска" для выхода за диапазон (чтобы не шумело на копейки)
# Например 0.05 = 0.05%
OUTSIDE_TOL_PCT = env_float("OUTSIDE_TOL_PCT", 0.0)


# ========= CoinGecko =========
CG_BASE = "https://api.coingecko.com/api/v3"

COINS: Dict[str, str] = {
    "BTC": "bitcoin",
    "SOL": "solana",
}

PAIR_CONFIG = {
    "BTC": {"lower": BTC_LOWER, "upper": BTC_UPPER, "near_pct": BTC_NEAR_PCT},
    "SOL": {"lower": SOL_LOWER, "upper": SOL_UPPER, "near_pct": SOL_NEAR_PCT},
}


def cg_simple_price_usd(symbol: str) -> float:
    coin_id = COINS[symbol]
    url = f"{CG_BASE}/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    data = http_get_json(url, params=params, timeout=30, retries=3)
    if coin_id not in data or "usd" not in data[coin_id]:
        raise ValueError(f"Unexpected CoinGecko response for {symbol}: {data}")
    return float(data[coin_id]["usd"])


def cg_daily_closes_usd(symbol: str, days: int = 220) -> List[float]:
    """
    Возвращает список daily close (USD) из CoinGecko market_chart (берём последнюю цену дня).
    """
    coin_id = COINS[symbol]
    url = f"{CG_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": str(days)}
    data = http_get_json(url, params=params, timeout=30, retries=3)

    prices = data.get("prices", [])
    if not isinstance(prices, list):
        raise ValueError(f"Unexpected CoinGecko market_chart response for {symbol}: {data}")

    # prices = [[ms, price], ...] (обычно много точек/день)
    # соберём по дате (UTC) и возьмём последнюю цену дня
    by_day: Dict[str, List[float]] = {}
    for item in prices:
        if not isinstance(item, list) or len(item) < 2:
            continue
        ms, p = item[0], item[1]
        day = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).date().isoformat()
        by_day.setdefault(day, [])
        by_day[day].append(float(p))

    # упорядочим по дате
    days_sorted = sorted(by_day.keys())
    closes = [by_day[d][-1] for d in days_sorted if by_day.get(d)]
    return closes


def rsi_14(closes: Optional[List[float]]) -> Optional[float]:
    """
    RSI(14) по классической формуле (Wilder).
    """
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

    # Wilder smoothing
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
    Берём weekly close как каждую 7-ю дневную цену (последний день недели).
    Это приближение, но для сигналов RSI обычно достаточно.
    """
    if not daily_closes or len(daily_closes) < 30:
        return None

    weekly: List[float] = []
    # Берём с конца назад по 7 дней, потом перевернём
    for i in range(len(daily_closes) - 1, -1, -7):
        weekly.append(daily_closes[i])
    weekly.reverse()
    return weekly


# ========= Signals / checks =========
def rsi_status(rsi_value: Optional[float]) -> str:
    if rsi_value is None:
        return "RSI: n/a"
    if rsi_value > RSI_OB:
        return f"RSI: OVERBOUGHT ({rsi_value:.1f})"
    if rsi_value < RSI_OS:
        return f"RSI: OVERSOLD ({rsi_value:.1f})"
    return f"RSI: neutral ({rsi_value:.1f})"


def is_rsi_trigger(rsi_value: Optional[float]) -> bool:
    if rsi_value is None:
        return False
    return (rsi_value > RSI_OB) or (rsi_value < RSI_OS)


def check_bounds(symbol: str, price: float, lower: float, upper: float, near_pct: float) -> List[str]:
    """
    Возвращает list[str] триггеров (может быть пусто).
    near_pct = например 0.7 (то есть 0.7%)
    OUTSIDE_TOL_PCT позволяет игнорировать микровыходы за диапазон.
    """
    triggers: List[str] = []

    # Tolerance thresholds (outside only)
    lower_outside_level = lower * (1.0 - OUTSIDE_TOL_PCT / 100.0)
    upper_outside_level = upper * (1.0 + OUTSIDE_TOL_PCT / 100.0)

    # outside range (with tolerance)
    if price < lower_outside_level:
        dist_abs = lower - price
        dist_pct = (dist_abs / lower) * 100.0 if lower else 0.0
        triggers.append(
            f"{symbol}: OUTSIDE ↓ ниже LOWER. Price={price:.8g} | LOWER={lower} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return triggers

    if price > upper_outside_level:
        dist_abs = price - upper
        dist_pct = (dist_abs / upper) * 100.0 if upper else 0.0
        triggers.append(
            f"{symbol}: OUTSIDE ↑ выше UPPER. Price={price:.8g} | UPPER={upper} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return triggers

    # near lower (внутри диапазона, близко к нижней границе)
    near_lower_level = lower * (1.0 + near_pct / 100.0)
    if price >= lower and price <= near_lower_level:
        dist_abs = price - lower
        dist_pct = (dist_abs / lower) * 100.0 if lower else 0.0
        triggers.append(
            f"{symbol}: NEAR LOWER. Price={price:.8g} | LOWER={lower} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )

    # near upper (внутри диапазона, близко к верхней границе)
    near_upper_level = upper * (1.0 - near_pct / 100.0)
    if price <= upper and price >= near_upper_level:
        dist_abs = upper - price
        dist_pct = (dist_abs / upper) * 100.0 if upper else 0.0
        triggers.append(
            f"{symbol}: NEAR UPPER. Price={price:.8g} | UPPER={upper} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )

    return triggers


def pair_recommendation(symbol: str, pair_triggers: List[str], daily_rsi: Optional[float], weekly_rsi: Optional[float]) -> List[str]:
    """
    Рекомендации ТОЛЬКО для конкретной пары.
    """
    lines: List[str] = []

    has_outside = any("OUTSIDE" in t for t in pair_triggers)
    has_near = any(("NEAR LOWER" in t or "NEAR UPPER" in t) for t in pair_triggers)

    if has_outside:
        lines.append(f"{symbol}: PAUSE grid + shift range toward current price (эта пара вышла за диапазон).")
        lines.append(f"{symbol}: После стабилизации перенеси LOWER/UPPER ближе к текущей цене и включи заново.")
    elif has_near:
        lines.append(f"{symbol}: Consider PAUSE (если волатильность выросла) или WIDEN range.")
    else:
        lines.append(f"{symbol}: Leave as-is.")

    if is_rsi_trigger(daily_rsi):
        lines.append(f"{symbol}: Daily RSI hint → возможен перегрев/перепроданность, уменьшить агрессию/расширить диапазон.")
    if is_rsi_trigger(weekly_rsi):
        lines.append(f"{symbol}: Weekly RSI hint → более сильный сигнал, лучше PAUSE или серьёзно расширить диапазон.")

    return lines


def fmt_price(symbol: str, price: float) -> str:
    if symbol == "BTC":
        return f"${price:,.2f}"
    if symbol == "SOL":
        return f"${price:,.4f}"
    return f"${price}"


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

    # 2) Price triggers per pair
    price_triggers_by_symbol: Dict[str, List[str]] = {}
    for s in symbols:
        cfg = PAIR_CONFIG[s]
        price_triggers_by_symbol[s] = check_bounds(
            s,
            prices[s],
            cfg["lower"],
            cfg["upper"],
            cfg["near_pct"],
        )

    # 3) RSI daily & weekly per pair (BTC + SOL)
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

    # 4) Determine if alert is needed
    any_price_trigger = any(len(price_triggers_by_symbol[s]) > 0 for s in symbols)
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
            lower = cfg["lower"]
            upper = cfg["upper"]
            near_pct = cfg["near_pct"]

            if s == "BTC":
                lines.append(f"{s}: {fmt_price(s, p)} | Range [{lower:,.0f} .. {upper:,.0f}] | Near={near_pct}%")
            else:
                lines.append(f"{s}: {fmt_price(s, p)} | Range [{lower:g} .. {upper:g}] | Near={near_pct}%")
        lines.append("")

        # Detailed blocks per pair
        for idx, s in enumerate(symbols, start=1):
            cfg = PAIR_CONFIG[s]
            p = prices[s]
            pair_price_triggers = price_triggers_by_symbol[s]
            daily_rsi = rsi_data[s]["daily"]
            weekly_rsi = rsi_data[s]["weekly"]

            lines.append(f"==== {s} ====")
            lines.append(f"Price: {fmt_price(s, p)} | Range [{cfg['lower']} .. {cfg['upper']}] | Near={cfg['near_pct']}%")
            lines.append("")

            # Price triggers (specific pair)
            if pair_price_triggers:
                lines.append("📌 Price triggers:")
                for t in pair_price_triggers:
                    lines.append(f"• {t}")
            else:
                lines.append("📌 Price triggers:")
                lines.append("• none")

            lines.append("")

            # RSI (specific pair)
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
            for rec in pair_recommendation(s, pair_price_triggers, daily_rsi, weekly_rsi):
                lines.append(f"• {rec}")

            if idx < len(symbols):
                lines.append("")
                lines.append("------------------------------")
                lines.append("")

        sent = tg_send("\n".join(lines))
        print("Alert sent." if sent else "Alert not sent (see logs).")
        return 0

    # SAFE: no Telegram message, only log
    safe_parts = [f"SAFE ({now_utc})"]
    for s in symbols:
        safe_parts.append(
            f"{s} {fmt_price(s, prices[s])} | "
            f"RSI D {('n/a' if rsi_data[s]['daily'] is None else f'{rsi_data[s]['daily']:.1f}')} / "
            f"W {('n/a' if rsi_data[s]['weekly'] is None else f'{rsi_data[s]['weekly']:.1f}')}"
        )
    print(" | ".join(safe_parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
