"""
Binance Meme Coin — ATH Decline Tracker
Detecta meme coins en Binance Futures que están empezando a caer desde su techo.
Corre cada 2 horas via GitHub Actions (gratis).

Señales monitoreadas (se necesitan 2 o más para alertar):
  1. Precio cayó -10% o más desde el máximo de las últimas 24h
  2. Funding rate alto (>0.1%) pero bajando vs medición anterior
  3. Open interest cayó -8% o más en las últimas 4h
"""

import os
import json
import requests
from datetime import datetime, timezone

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = "decline_state.json"

BINANCE_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_TICKER_24H    = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_FUNDING       = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OPEN_INTEREST = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_OI_HISTORY    = "https://fapi.binance.com/futures/data/openInterestHist"
BINANCE_KLINES        = "https://fapi.binance.com/fapi/v1/klines"

MEME_KEYWORDS = [
    "meme", "pepe", "doge", "shib", "floki", "inu", "elon", "moon",
    "safe", "baby", "bonk", "wif", "brett", "popcat", "dog", "cat",
    "frog", "wojak", "chad", "pnut", "goat", "cow", "neiro", "mog",
    "turbo", "fwog", "giga", "pup", "ponke", "slerf", "myro", "bome",
    "act", "vine", "pippin", "harambe", "degen", "wen", "cope", "ngmi",
    "fartcoin", "kekius", "michi", "nub", "maneki", "lobster", "fun",
    "dogs", "hmstr", "nft", "people", "burger",
]

# Umbrales de señales
PRICE_DROP_FROM_HIGH_PCT = -10.0   # caída desde máximo 24h
FUNDING_HIGH_THRESHOLD   = 0.08    # funding rate "alto" (%)
OI_DROP_PCT              = -8.0    # caída de open interest


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_meme(symbol: str) -> bool:
    s = symbol.lower().replace("usdt", "").replace("busd", "")
    return any(kw in s for kw in MEME_KEYWORDS)


def get_all_meme_symbols() -> list[str]:
    try:
        resp = requests.get(BINANCE_EXCHANGE_INFO, timeout=15)
        resp.raise_for_status()
        symbols = [
            s["symbol"] for s in resp.json().get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and is_meme(s["symbol"])
        ]
        return symbols
    except Exception as e:
        print(f"[ERROR] Exchange info: {e}")
        return []


def get_ticker_24h(symbol: str) -> dict | None:
    try:
        resp = requests.get(BINANCE_TICKER_24H, params={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        d = resp.json()
        return {
            "price":      float(d["lastPrice"]),
            "high_24h":   float(d["highPrice"]),
            "low_24h":    float(d["lowPrice"]),
            "change_pct": float(d["priceChangePercent"]),
            "volume":     float(d["quoteVolume"]),
        }
    except Exception as e:
        print(f"[WARN] Ticker {symbol}: {e}")
        return None


def get_funding_rate(symbol: str) -> float | None:
    try:
        resp = requests.get(BINANCE_FUNDING, params={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        d = resp.json()
        if isinstance(d, list):
            d = d[0]
        return round(float(d.get("lastFundingRate", 0)) * 100, 4)
    except Exception:
        return None


def get_open_interest(symbol: str) -> float | None:
    try:
        resp = requests.get(BINANCE_OPEN_INTEREST, params={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        return float(resp.json().get("openInterest", 0))
    except Exception:
        return None


def price_drop_signal(ticker: dict) -> tuple[bool, float]:
    """¿Cayó >= 10% desde el máximo de las últimas 24h?"""
    if not ticker or ticker["high_24h"] == 0:
        return False, 0.0
    drop = ((ticker["price"] - ticker["high_24h"]) / ticker["high_24h"]) * 100
    return drop <= PRICE_DROP_FROM_HIGH_PCT, round(drop, 2)


def funding_signal(symbol: str, state: dict) -> tuple[bool, float, float]:
    """¿Funding rate alto Y bajando vs medición anterior?"""
    current = get_funding_rate(symbol)
    if current is None:
        return False, 0.0, 0.0
    prev = state.get(symbol, {}).get("last_funding", current)
    is_high = current > FUNDING_HIGH_THRESHOLD
    is_dropping = current < prev
    triggered = is_high and is_dropping
    return triggered, current, prev


def oi_signal(symbol: str, state: dict) -> tuple[bool, float, float]:
    """¿Open interest cayó >= 8% vs medición anterior?"""
    current_oi = get_open_interest(symbol)
    if current_oi is None:
        return False, 0.0, 0.0
    prev_oi = state.get(symbol, {}).get("last_oi", current_oi)
    if prev_oi == 0:
        return False, 0.0, 0.0
    change = ((current_oi - prev_oi) / prev_oi) * 100
    return change <= OI_DROP_PCT, round(change, 2), current_oi


def already_alerted_recently(symbol: str, state: dict) -> bool:
    """Evita mandar la misma alerta más de una vez cada 12 horas."""
    last = state.get(symbol, {}).get("last_alert_ts", 0)
    now = datetime.now(tz=timezone.utc).timestamp()
    return (now - last) < 43200  # 12 horas


def funding_color(rate: float) -> int:
    if rate > 0.3:
        return 0xED4245
    elif rate > 0.1:
        return 0xFEE75C
    elif rate >= 0:
        return 0x57F287
    else:
        return 0xEB459E


def build_embed(symbol: str, ticker: dict, signals: dict) -> dict:
    base = symbol.replace("USDT", "")
    score = signals["score"]
    color = 0xED4245 if score >= 3 else 0xFEE75C  # rojo si 3 señales, amarillo si 2

    strength = "🔴 Fuerte (3/3 señales)" if score >= 3 else "🟡 Moderado (2/3 señales)"

    fields = [
        {
            "name": "Precio actual",
            "value": f"${ticker['price']}",
            "inline": True,
        },
        {
            "name": "Máximo 24h",
            "value": f"${ticker['high_24h']}",
            "inline": True,
        },
        {
            "name": "Caída desde máximo",
            "value": f"**{signals['price_drop']}%**",
            "inline": True,
        },
        {
            "name": "Funding rate",
            "value": f"{signals['funding_current']}% (antes: {signals['funding_prev']}%)",
            "inline": True,
        },
        {
            "name": "Open interest",
            "value": f"{signals['oi_change']}% vs medición anterior",
            "inline": True,
        },
        {
            "name": "Señales activas",
            "value": "  ".join(signals["active_signals"]),
            "inline": False,
        },
        {
            "name": "Intensidad",
            "value": strength,
            "inline": False,
        },
        {
            "name": "Sugerencia",
            "value": "Considerar short · stop loss sugerido +20% del precio de entrada · cerrar en 7–14 días",
            "inline": False,
        },
    ]

    return {
        "embeds": [{
            "title": f"📉  Posible techo detectado — ${base}",
            "url": f"https://www.binance.com/en/futures/{symbol}",
            "color": color,
            "fields": fields,
            "footer": {"text": "Binance Futures · MemeTracker — Decline Scanner"},
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }],
        "username": "MemeTracker",
    }


def send_discord(payload: dict) -> bool:
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Discord: {e}")
        return False


def main():
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now_str}] Iniciando decline scan...")

    state = load_state()
    symbols = get_all_meme_symbols()
    print(f"Meme coins activos en Binance Futures: {len(symbols)}")
    print(f"Symbols: {symbols}")

    alerts_sent = 0

    for symbol in symbols:
        print(f"\n  Analizando {symbol}...")

        if already_alerted_recently(symbol, state):
            print(f"    Alerta reciente, skipping.")
            continue

        ticker = get_ticker_24h(symbol)
        if not ticker:
            continue

        # Evaluar señales
        price_triggered, price_drop    = price_drop_signal(ticker)
        fund_triggered, fund_cur, fund_prev = funding_signal(symbol, state)
        oi_triggered, oi_change, oi_cur = oi_signal(symbol, state)

        active_signals = []
        if price_triggered:
            active_signals.append(f"📉 Precio -{abs(price_drop):.1f}% desde max")
        if fund_triggered:
            active_signals.append(f"📊 Funding bajando ({fund_prev}%→{fund_cur}%)")
        if oi_triggered:
            active_signals.append(f"📦 OI cayó {oi_change:.1f}%")

        score = len(active_signals)
        print(f"    Score: {score}/3 | price={price_drop}% | funding={fund_cur}% | OI={oi_change}%")

        # Actualizar estado para próxima corrida
        if symbol not in state:
            state[symbol] = {}
        state[symbol]["last_funding"] = fund_cur if fund_cur is not None else state[symbol].get("last_funding", 0)
        state[symbol]["last_oi"]      = oi_cur if oi_cur else state[symbol].get("last_oi", 0)

        if score >= 2:
            signals = {
                "score": score,
                "price_drop": price_drop,
                "funding_current": fund_cur,
                "funding_prev": fund_prev,
                "oi_change": oi_change,
                "active_signals": active_signals,
            }
            payload = build_embed(symbol, ticker, signals)
            if send_discord(payload):
                print(f"    Alerta enviada a Discord.")
                state[symbol]["last_alert_ts"] = datetime.now(tz=timezone.utc).timestamp()
                alerts_sent += 1

    save_state(state)
    print(f"\nScan completado. Alertas enviadas: {alerts_sent}")


if __name__ == "__main__":
    main()
