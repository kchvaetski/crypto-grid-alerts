import os
import math
import time
import requests
from datetime import datetime, timezone


# ========= Telegram =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def tg_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # чтобы workflow не падал, просто печатаем в лог
        print("Telegram env vars missing. Message would be:\n", text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


# ========= Settings from ENV =========
def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return float(default)
    return float(str(v).strip())

BTC_LOWER = env_float("BTC_LOWER", 65800.0)
BTC_UPPER = env_float("BTC_UPPER", 69600.0)
BTC_NEAR_PCT = env_float("BTC_NEAR_PCT", 0.7)  # percent

SOL_LOWER = env_float("SOL_LOWER", 80.0)
SOL_UPPER = env_float("SOL_UPPER", 88.0)
SOL_NEAR_PCT = env_float("SOL_NEAR_PCT", 1.0)

DOGE_LOWER = env_float("DOGE_LOWER", 0.094)
DOGE_UPPER = env_float("DOGE_UPPER", 0.112)
DOGE_NEAR_PCT = env_float("DOGE_NEAR_PCT", 1.0)

# RSI thresholds
RSI_OB = env_float("RSI_OVERBOUGHT", 70.0)
RSI_OS = env_float("RSI_OVERSOLD", 30.0)


# ========= CoinGecko =========
CG_BASE = "https://api.coingecko.com/api/v3"

COINS = {
    "BTC": "bitcoin",
    "SOL": "solana",
    "DOGE": "dogecoin",
}

def cg_simple_price_usd(symbol: str) -> float:
    coin_id = COINS[symbol]
    url = f"{CG_BASE}/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return float(data[coin_id]["usd"])

def cg_daily_closes_usd(symbol: str, days: int = 200):
    """
    Возвращает список daily close (USD) из CoinGecko market_chart (берём последний price дня).
    """
    coin_id = COINS[symbol]
    url = f"{CG_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": str(days)}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    prices = data.get("prices", [])
    # prices = [[ms, price], ...] (обычно много точек/день)
    # соберём по дате (UTC) и возьмём последнюю цену дня
    by_day = {}
    for ms, p in prices:
        day = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
        by_day.setdefault(day, [])
        by_day[day].append(float(p))
    # упорядочим по дате
    days_sorted = sorted(by_day.keys())
    closes = [by_day[d][-1] for d in days_sorted if len(by_day[d]) > 0]
    return closes

def rsi_14(closes):
    """
    RSI(14) по классической формуле (Wilder).
    """
    period = 14
    if closes is None or len(closes) < period + 1:
        return None

    gains = []
    losses = []
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

def weekly_closes_from_daily(daily_closes):
    """
    Берём weekly close как каждую 7-ю дневную цену (последний день недели).
    Это приближение, но для сигналов RSI обычно достаточно.
    """
    if not daily_closes or len(daily_closes) < 30:
        return None
    weekly = []
    # возьмём с конца назад по 7 дней, потом перевернём
    for i in range(len(daily_closes) - 1, -1, -7):
        weekly.append(daily_closes[i])
    weekly.reverse()
    return weekly


# ========= Grid checks =========
def check_bounds(symbol: str, price: float, lower: float, upper: float, near_pct: float):
    """
    Возвращает list[str] триггеров (может быть пусто).
    near_pct = например 0.7 (то есть 0.7%)
    """
    triggers = []

    # outside range
    if price < lower:
        dist_abs = lower - price
        dist_pct = (dist_abs / lower) * 100.0
        triggers.append(
            f"{symbol}: OUTSIDE ↓ ниже LOWER. Price={price:.8g} | LOWER={lower} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return triggers

    if price > upper:
        dist_abs = price - upper
        dist_pct = (dist_abs / upper) * 100.0
        triggers.append(
            f"{symbol}: OUTSIDE ↑ выше UPPER. Price={price:.8g} | UPPER={upper} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )
        return triggers

    # near lower
    near_lower_level = lower * (1.0 + near_pct / 100.0)
    if price <= near_lower_level:
        dist_abs = price - lower
        dist_pct = (dist_abs / lower) * 100.0
        triggers.append(
            f"{symbol}: NEAR LOWER. Price={price:.8g} | LOWER={lower} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )

    # near upper
    near_upper_level = upper * (1.0 - near_pct / 100.0)
    if price >= near_upper_level:
        dist_abs = upper - price
        dist_pct = (dist_abs / upper) * 100.0
        triggers.append(
            f"{symbol}: NEAR UPPER. Price={price:.8g} | UPPER={upper} | Δ={dist_abs:.8g} ({dist_pct:.3f}%)"
        )

    return triggers


def rsi_status(rsi_value: float):
    if rsi_value is None:
        return "RSI: n/a"
    if rsi_value > RSI_OB:
        return f"RSI: OVERBOUGHT ({rsi_value:.1f})"
    if rsi_value < RSI_OS:
        return f"RSI: OVERSOLD ({rsi_value:.1f})"
    return f"RSI: neutral ({rsi_value:.1f})"


def recommended_action_for_grid(symbol: str, price: float, triggers: list, daily_rsi: float | None, weekly_rsi: float | None):
    """
    Очень простые рекомендации (без ATR — чтобы не усложнять и не ломать).
    Если хочешь ATR-правило ±1.5 ATR — скажи, добавлю.
    """
    lines = []
    if any("OUTSIDE" in t for t in triggers):
        lines.append("Action: PAUSE grid + shift range toward price (price вышел за диапазон).")
        lines.append("Tip: после стабилизации перенеси LOWER/UPPER ближе к текущей цене и заново включи.")
        return lines

    if any("NEAR LOWER" in t for t in triggers) or any("NEAR UPPER" in t for t in triggers):
        lines.append("Action: Consider PAUSE (если волатильность резко выросла) или WIDEN range.")
    else:
        lines.append("Action: Leave as-is.")

    # RSI hint (BTC only обычно)
    if daily_rsi is not None and (daily_rsi > RSI_OB or daily_rsi < RSI_OS):
        lines.append("RSI hint: возможен перегрев/перепроданность — лучше уменьшить агрессию или расширить диапазон.")
    if weekly_rsi is not None and (weekly_rsi > RSI_OB or weekly_rsi < RSI_OS):
        lines.append("Weekly RSI hint: более сильный сигнал — лучше PAUSE или серьёзно расширить диапазон.")

    return lines


def main():
    # 1) Prices
    btc_price = cg_simple_price_usd("BTC")
    sol_price = cg_simple_price_usd("SOL")
    doge_price = cg_simple_price_usd("DOGE")

    # 2) Grid triggers
    triggers = []
    triggers += check_bounds("BTC", btc_price, BTC_LOWER, BTC_UPPER, BTC_NEAR_PCT)
    triggers += check_bounds("SOL", sol_price, SOL_LOWER, SOL_UPPER, SOL_NEAR_PCT)
    triggers += check_bounds("DOGE", doge_price, DOGE_LOWER, DOGE_UPPER, DOGE_NEAR_PCT)

    # 3) BTC RSI(14) daily & weekly
    daily_rsi = None
    weekly_rsi = None
    try:
        btc_daily_closes = cg_daily_closes_usd("BTC", days=220)
        daily_rsi = rsi_14(btc_daily_closes)
        btc_weekly_closes = weekly_closes_from_daily(btc_daily_closes)
        weekly_rsi = rsi_14(btc_weekly_closes) if btc_weekly_closes else None
    except Exception as e:
        print("RSI calc error:", repr(e))

    rsi_triggers = []
    if daily_rsi is not None and (daily_rsi > RSI_OB or daily_rsi < RSI_OS):
        rsi_triggers.append(f"BTC Daily {rsi_status(daily_rsi)}")
    if weekly_rsi is not None and (weekly_rsi > RSI_OB or weekly_rsi < RSI_OS):
        rsi_triggers.append(f"BTC Weekly {rsi_status(weekly_rsi)}")

    # 4) Compose message
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if triggers or rsi_triggers:
        lines = []
        lines.append(f"🚨 GRID ALERTS ({now_utc})")
        lines.append("")
        lines.append(f"BTC: ${btc_price:,.2f} | Range [{BTC_LOWER:,.0f} .. {BTC_UPPER:,.0f}] | Near={BTC_NEAR_PCT}%")
        lines.append(f"SOL: ${sol_price:,.4f} | Range [{SOL_LOWER:g} .. {SOL_UPPER:g}] | Near={SOL_NEAR_PCT}%")
        lines.append(f"DOGE: ${doge_price:,.6f} | Range [{DOGE_LOWER:g} .. {DOGE_UPPER:g}] | Near={DOGE_NEAR_PCT}%")
        lines.append("")

        if triggers:
            lines.append("📌 Price triggers:")
            for t in triggers:
                lines.append(f"• {t}")
            lines.append("")

        lines.append("📈 BTC RSI(14):")
        lines.append(f"• Daily: {rsi_status(daily_rsi)}")
        lines.append(f"• Weekly: {rsi_status(weekly_rsi)}")
        if rsi_triggers:
            lines.append("")
            lines.append("📌 RSI triggers:")
            for t in rsi_triggers:
                lines.append(f"• {t}")

        lines.append("")
        lines.append("🧭 Recommendation:")
        for l in recommended_action_for_grid("BTC", btc_price, triggers, daily_rsi, weekly_rsi):
            lines.append(f"• {l}")

        tg_send("\n".join(lines))
        print("Alert sent.")
        return

# SAFE: do NOT send Telegram message (only log)
safe_line = (
    f"SAFE  "
    f"BTC ${btc_price:,.2f} | SOL ${sol_price:,.4f} | DOGE ${doge_price:,.6f} | "
    f"BTC RSI Daily {('n/a' if daily_rsi is None else f'{daily_rsi:.1f}')} / Weekly {('n/a' if weekly_rsi is None else f'{weekly_rsi:.1f}')}"
)
print(safe_line)


if __name__ == "__main__":
    main()
