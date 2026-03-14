"""
Wyckoff Range Scanner — TCT Method
Usa CCXT para evitar bloqueos de IP de Binance en GitHub Actions.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "range_state.json"

MIN_RANGE_CANDLES       = 24
LOOKBACK_CANDLES        = 200
TOP_N_COINS             = 35
TOUCH_TOLERANCE_PCT     = 0.003
DEVIATION_THRESHOLD_PCT = 0.005
ALERT_COOLDOWN_HOURS    = 8


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


def get_top_symbols(n: int) -> list[str]:
    """Usa CoinGecko para obtener top coins por volumen, después mapea a símbolos de Binance."""
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "volume_desc",
            "per_page": n * 2,
            "page": 1,
            "sparkline": False,
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        coins = resp.json()
        symbols = [f"{c['symbol'].upper()}USDT" for c in coins]
        # Excluir stablecoins
        stable = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD"}
        symbols = [s for s in symbols if s.replace("USDT", "") not in stable]
        return symbols[:n]
    except Exception as e:
        print(f"[ERROR] CoinGecko: {e}")
        # Fallback: lista hardcoded de los más líquidos
        return [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
            "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
            "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "NEARUSDT",
            "APTUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "SUIUSDT",
        ]


def get_klines_ccxt(symbol: str) -> list[dict]:
    """Obtiene velas usando CCXT con Binance Futures."""
    try:
        import ccxt
        exchange = ccxt.binanceusdm({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        ohlcv = exchange.fetch_ohlcv(
            symbol.replace("USDT", "/USDT"),
            timeframe="1h",
            limit=LOOKBACK_CANDLES
        )
        return [
            {
                "ts":     c[0],
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            }
            for c in ohlcv
        ]
    except Exception as e:
        print(f"[WARN] CCXT klines {symbol}: {e}")
        return []


def find_range(candles: list[dict]) -> dict | None:
    if len(candles) < MIN_RANGE_CANDLES + 10:
        return None

    search_candles = candles[-150:]

    for start_idx in range(len(search_candles) - MIN_RANGE_CANDLES):
        window = search_candles[start_idx:]
        if len(window) < MIN_RANGE_CANDLES:
            break

        range_high = max(c["high"] for c in window)
        range_low  = min(c["low"]  for c in window)
        range_size = range_high - range_low

        if range_size == 0:
            continue

        equilibrium  = (range_high + range_low) / 2
        tolerance_h  = range_high * TOUCH_TOLERANCE_PCT
        tolerance_l  = range_low  * TOUCH_TOLERANCE_PCT

        touches_high = sum(1 for c in window if abs(c["high"] - range_high) <= tolerance_h)
        touches_low  = sum(1 for c in window if abs(c["low"]  - range_low)  <= tolerance_l)

        if touches_high < 1 or touches_low < 1:
            continue

        crossed_above = any(c["close"] > equilibrium for c in window)
        crossed_below = any(c["close"] < equilibrium for c in window)

        if not (crossed_above and crossed_below):
            continue

        range_pct = (range_size / range_low) * 100

        return {
            "high":         round(range_high, 8),
            "low":          round(range_low,  8),
            "equilibrium":  round(equilibrium, 8),
            "range_pct":    round(range_pct, 2),
            "candles":      len(window),
            "touches_high": touches_high,
            "touches_low":  touches_low,
        }

    return None


def detect_deviation(candles: list[dict], rng: dict) -> dict | None:
    if not rng or len(candles) < 3:
        return None

    last = candles[-1]
    prev = candles[-2]
    rh   = rng["high"]
    rl   = rng["low"]

    prev_inside = rl <= prev["close"] <= rh

    if not prev_inside:
        return None

    threshold_h = rh * (1 + DEVIATION_THRESHOLD_PCT)
    threshold_l = rl * (1 - DEVIATION_THRESHOLD_PCT)

    if last["close"] > threshold_h:
        return {
            "direction": "HIGH",
            "price":     round(last["close"], 8),
            "breach":    round(((last["close"] - rh) / rh) * 100, 3),
        }
    elif last["close"] < threshold_l:
        return {
            "direction": "LOW",
            "price":     round(last["close"], 8),
            "breach":    round(((rl - last["close"]) / rl) * 100, 3),
        }

    return None


def already_alerted(symbol: str, alert_type: str, state: dict) -> bool:
    key  = f"{symbol}_{alert_type}"
    last = state.get(key, {}).get("ts", 0)
    now  = datetime.now(tz=timezone.utc).timestamp()
    return (now - last) < ALERT_COOLDOWN_HOURS * 3600


def mark_alerted(symbol: str, alert_type: str, state: dict):
    key = f"{symbol}_{alert_type}"
    state[key] = {"ts": datetime.now(tz=timezone.utc).timestamp()}


def increment_deviation(symbol: str, direction: str, state: dict) -> int:
    count = state.get(f"{symbol}_dev_count", 0) + 1
    state[f"{symbol}_dev_count"]    = count
    state[f"{symbol}_last_dev_dir"] = direction
    return count


def reset_deviations(symbol: str, state: dict):
    state[f"{symbol}_dev_count"] = 0
    state.pop(f"{symbol}_last_dev_dir", None)


def send_discord(payload: dict) -> bool:
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Discord: {e}")
        return False


def build_range_embed(symbol: str, rng: dict) -> dict:
    base       = symbol.replace("USDT", "")
    duration_d = round(rng["candles"] / 24, 1)
    return {
        "embeds": [{
            "title": f"📦  Rango confirmado — ${base}",
            "url":   f"https://www.binance.com/en/futures/{symbol}",
            "color": 0x5865F2,
            "fields": [
                {"name": "Range High",   "value": f"`{rng['high']}`",        "inline": True},
                {"name": "Range Low",    "value": f"`{rng['low']}`",         "inline": True},
                {"name": "Equilibrium",  "value": f"`{rng['equilibrium']}`", "inline": True},
                {"name": "Tamaño",       "value": f"{rng['range_pct']}%",    "inline": True},
                {"name": "Duración",     "value": f"~{duration_d} días",     "inline": True},
                {"name": "Toques",       "value": f"High: {rng['touches_high']}  Low: {rng['touches_low']}", "inline": True},
                {"name": "Acción",       "value": "Rango válido. Esperá la primera desviación.", "inline": False},
            ],
            "footer":    {"text": "Binance Futures · TCT Range Scanner"},
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }],
        "username": "TCT Scanner",
    }


def build_deviation_embed(symbol: str, rng: dict, dev: dict, dev_number: int) -> dict:
    base      = symbol.replace("USDT", "")
    is_second = dev_number >= 2
    title  = f"🚨  SEGUNDA DESVIACIÓN — ${base} — ENTRY" if is_second else f"⚠️  Primera desviación — ${base}"
    color  = 0xED4245 if is_second else 0xFEE75C
    dir_str = "⬆️ Por arriba del Range High" if dev["direction"] == "HIGH" else "⬇️ Por abajo del Range Low"

    if is_second:
        if dev["direction"] == "HIGH":
            action = f"Segunda desviación arriba → objetivo Range Low `{rng['low']}`\nConsiderá SHORT al retorno al rango"
        else:
            action = f"Segunda desviación abajo → objetivo Range High `{rng['high']}`\nConsiderá LONG al retorno al rango"
    else:
        action = "Primera desviación. Esperá la segunda para el entry."

    return {
        "embeds": [{
            "title": title,
            "url":   f"https://www.binance.com/en/futures/{symbol}",
            "color": color,
            "fields": [
                {"name": "Dirección",     "value": dir_str,                   "inline": True},
                {"name": "Precio",        "value": f"`{dev['price']}`",        "inline": True},
                {"name": "Breach",        "value": f"{dev['breach']}% fuera",  "inline": True},
                {"name": "Range High",    "value": f"`{rng['high']}`",         "inline": True},
                {"name": "Range Low",     "value": f"`{rng['low']}`",          "inline": True},
                {"name": "Equilibrium",   "value": f"`{rng['equilibrium']}`",  "inline": True},
                {"name": "Desviación #",  "value": f"**{dev_number} de 2**",   "inline": True},
                {"name": "Acción",        "value": action,                     "inline": False},
            ],
            "footer":    {"text": "Binance Futures · TCT Range Scanner"},
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }],
        "username": "TCT Scanner",
    }


def main():
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now_str}] Iniciando TCT Range Scanner...")

    state   = load_state()
    symbols = get_top_symbols(TOP_N_COINS)
    print(f"Escaneando {len(symbols)} symbols...")

    alerts_sent = 0

    for symbol in symbols:
        print(f"  {symbol}...")
        time.sleep(0.3)  # evitar rate limit

        candles = get_klines_ccxt(symbol)
        if len(candles) < MIN_RANGE_CANDLES + 10:
            print(f"    Datos insuficientes ({len(candles)} velas).")
            continue

        rng = find_range(candles)
        if not rng:
            print(f"    Sin rango válido.")
            continue

        print(f"    Rango: {rng['low']} — {rng['high']} ({rng['range_pct']}%)")

        state_key    = f"{symbol}_range"
        prev_range   = state.get(state_key)
        range_is_new = (
            not prev_range
            or abs(prev_range.get("high", 0) - rng["high"]) > rng["high"] * 0.001
            or abs(prev_range.get("low",  0) - rng["low"])  > rng["low"]  * 0.001
        )

        if range_is_new:
            state[state_key] = rng
            reset_deviations(symbol, state)
            if not already_alerted(symbol, "range", state):
                if send_discord(build_range_embed(symbol, rng)):
                    mark_alerted(symbol, "range", state)
                    alerts_sent += 1
                    print(f"    Alerta rango enviada.")

        dev = detect_deviation(candles, rng)
        if dev:
            print(f"    Desviación: {dev['direction']} {dev['breach']}%")
            dev_count = increment_deviation(symbol, dev["direction"], state)
            if not already_alerted(symbol, f"dev{dev_count}", state):
                if send_discord(build_deviation_embed(symbol, rng, dev, dev_count)):
                    mark_alerted(symbol, f"dev{dev_count}", state)
                    alerts_sent += 1
                    print(f"    Alerta desviación #{dev_count} enviada.")
        else:
            print(f"    Sin desviación.")

    save_state(state)
    print(f"\nListo. Alertas enviadas: {alerts_sent}")


if __name__ == "__main__":
    main()
