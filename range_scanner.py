
"""
Wyckoff Range Scanner — TCT Method
Detecta rangos válidos en Binance Futures (1h) y alerta en Discord cuando:
  1. Se confirma un rango nuevo (1 toque high + 1 toque low + cruce de equilibrium)
  2. Se detecta primera desviación (precio sale del rango y vuelve)
  3. Se detecta segunda desviación — señal de entry

Corre cada 2 horas via GitHub Actions (gratis).
"""

import os
import json
import requests
from datetime import datetime, timezone

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STATE_FILE = "range_state.json"

BINANCE_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_KLINES        = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_TICKER        = "https://fapi.binance.com/fapi/v1/ticker/24hr"

# Mínimo de velas en rango para considerarlo válido (24 velas de 1h = 1 día)
MIN_RANGE_CANDLES = 24

# Cuántas velas lookback para buscar rangos
LOOKBACK_CANDLES = 200

# Top coins por volumen a escanear
TOP_N_COINS = 35

# Tolerancia para "toque" del high/low (0.3% del precio)
TOUCH_TOLERANCE_PCT = 0.003

# Cuánto tiene que salir del rango para contar como desviación (0.5%)
DEVIATION_THRESHOLD_PCT = 0.005

# Cooldown entre alertas del mismo símbolo y tipo (horas)
ALERT_COOLDOWN_HOURS = 8


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


def get_top_symbols_by_volume(n: int) -> list[str]:
    try:
        resp = requests.get(BINANCE_TICKER, timeout=15)
        resp.raise_for_status()
        tickers = [
            t for t in resp.json()
            if t["symbol"].endswith("USDT")
            and not any(x in t["symbol"] for x in ["BULL", "BEAR", "UP", "DOWN"])
        ]
        tickers.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        return [t["symbol"] for t in tickers[:n]]
    except Exception as e:
        print(f"[ERROR] Top symbols: {e}")
        return []


def get_klines(symbol: str, interval: str = "1h", limit: int = LOOKBACK_CANDLES) -> list[dict]:
    try:
        resp = requests.get(BINANCE_KLINES, params={
            "symbol": symbol, "interval": interval, "limit": limit
        }, timeout=15)
        resp.raise_for_status()
        candles = []
        for c in resp.json():
            candles.append({
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
                "ts":     int(c[0]),
            })
        return candles
    except Exception as e:
        print(f"[WARN] Klines {symbol}: {e}")
        return []


def find_range(candles: list[dict]) -> dict | None:
    """
    Busca el rango más reciente válido según método TCT:
    - Al menos MIN_RANGE_CANDLES velas de consolidación
    - 1 toque del high y 1 toque del low
    - El precio cruza el equilibrium (50%) de un lado al otro
    Devuelve el rango si lo encuentra, None si no.
    """
    if len(candles) < MIN_RANGE_CANDLES + 10:
        return None

    # Buscar desde las velas más recientes hacia atrás
    # Tomamos las últimas 150 velas para buscar el rango activo
    search_candles = candles[-150:]

    best_range = None

    for start_idx in range(len(search_candles) - MIN_RANGE_CANDLES):
        window = search_candles[start_idx:]
        if len(window) < MIN_RANGE_CANDLES:
            break

        range_high = max(c["high"] for c in window)
        range_low  = min(c["low"]  for c in window)
        range_size = range_high - range_low

        if range_size == 0:
            continue

        equilibrium = (range_high + range_low) / 2
        tolerance_h = range_high * TOUCH_TOLERANCE_PCT
        tolerance_l = range_low  * TOUCH_TOLERANCE_PCT

        # Contar toques del high y low
        touches_high = sum(1 for c in window if abs(c["high"] - range_high) <= tolerance_h)
        touches_low  = sum(1 for c in window if abs(c["low"]  - range_low)  <= tolerance_l)

        if touches_high < 1 or touches_low < 1:
            continue

        # Verificar cruce del equilibrium (precio pasa de un lado al otro)
        crossed_above = any(c["close"] > equilibrium for c in window)
        crossed_below = any(c["close"] < equilibrium for c in window)

        if not (crossed_above and crossed_below):
            continue

        # Rango válido — guardamos el más reciente (más largo en ventana)
        range_pct = (range_size / range_low) * 100

        best_range = {
            "high":        round(range_high, 8),
            "low":         round(range_low,  8),
            "equilibrium": round(equilibrium, 8),
            "range_pct":   round(range_pct, 2),
            "candles":     len(window),
            "start_ts":    window[0]["ts"],
            "touches_high": touches_high,
            "touches_low":  touches_low,
        }
        break  # tomamos el primer rango válido encontrado (más reciente)

    return best_range


def detect_deviation(candles: list[dict], rng: dict) -> dict | None:
    """
    Detecta si la última vela (o las últimas 3) muestran una desviación:
    - Precio cierra fuera del rango por más de DEVIATION_THRESHOLD_PCT
    - Y la vela anterior estaba dentro del rango
    Devuelve info de la desviación o None.
    """
    if not rng or len(candles) < 3:
        return None

    last    = candles[-1]
    prev    = candles[-2]
    rh      = rng["high"]
    rl      = rng["low"]
    threshold_h = rh * (1 + DEVIATION_THRESHOLD_PCT)
    threshold_l = rl * (1 - DEVIATION_THRESHOLD_PCT)

    prev_inside = rl <= prev["close"] <= rh

    if not prev_inside:
        return None

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
    key = f"{symbol}_{alert_type}"
    last = state.get(key, {}).get("ts", 0)
    now  = datetime.now(tz=timezone.utc).timestamp()
    return (now - last) < ALERT_COOLDOWN_HOURS * 3600


def mark_alerted(symbol: str, alert_type: str, state: dict):
    key = f"{symbol}_{alert_type}"
    state[key] = {"ts": datetime.now(tz=timezone.utc).timestamp()}


def deviation_count(symbol: str, state: dict) -> int:
    """Cuántas desviaciones registradas tiene este símbolo en el estado."""
    return state.get(f"{symbol}_dev_count", 0)


def increment_deviation(symbol: str, direction: str, state: dict):
    count = state.get(f"{symbol}_dev_count", 0) + 1
    state[f"{symbol}_dev_count"] = count
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


def build_range_confirmed_embed(symbol: str, rng: dict) -> dict:
    base = symbol.replace("USDT", "")
    duration_h = round(rng["candles"])
    duration_d = round(duration_h / 24, 1)

    return {
        "embeds": [{
            "title": f"📦  Rango confirmado — ${base}",
            "url": f"https://www.binance.com/en/futures/{symbol}",
            "color": 0x5865F2,  # azul
            "fields": [
                {"name": "Range High",    "value": f"`{rng['high']}`",        "inline": True},
                {"name": "Range Low",     "value": f"`{rng['low']}`",         "inline": True},
                {"name": "Equilibrium",   "value": f"`{rng['equilibrium']}`", "inline": True},
                {"name": "Tamaño rango",  "value": f"{rng['range_pct']}%",    "inline": True},
                {"name": "Duración",      "value": f"~{duration_d} días ({duration_h} velas 1h)", "inline": True},
                {"name": "Toques",        "value": f"High: {rng['touches_high']}  Low: {rng['touches_low']}", "inline": True},
                {"name": "Qué hacer",     "value": "Rango válido activo. Esperá la primera desviación.", "inline": False},
            ],
            "footer": {"text": "Binance Futures · TCT Range Scanner"},
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }],
        "username": "TCT Scanner",
    }


def build_deviation_embed(symbol: str, rng: dict, dev: dict, dev_number: int) -> dict:
    base = symbol.replace("USDT", "")
    is_second = dev_number >= 2

    if is_second:
        title  = f"🚨  SEGUNDA DESVIACIÓN — ${base} — ENTRY"
        color  = 0xED4245   # rojo
        action = "⚡ SEÑAL DE ENTRY\n"
        if dev["direction"] == "HIGH":
            action += f"Segunda desviación por arriba → precio debería volver al Range Low `{rng['low']}`\nConsiderá SHORT desde el retorno al rango"
        else:
            action += f"Segunda desviación por abajo → precio debería volver al Range High `{rng['high']}`\nConsiderá LONG desde el retorno al rango"
    else:
        title  = f"⚠️  Primera desviación — ${base}"
        color  = 0xFEE75C   # amarillo
        action = "Primera desviación detectada. Esperá la segunda para el entry."

    direction_str = "⬆️ Por arriba del Range High" if dev["direction"] == "HIGH" else "⬇️ Por abajo del Range Low"

    return {
        "embeds": [{
            "title": title,
            "url": f"https://www.binance.com/en/futures/{symbol}",
            "color": color,
            "fields": [
                {"name": "Dirección",     "value": direction_str,              "inline": True},
                {"name": "Precio actual", "value": f"`{dev['price']}`",        "inline": True},
                {"name": "Breach",        "value": f"{dev['breach']}% fuera",  "inline": True},
                {"name": "Range High",    "value": f"`{rng['high']}`",         "inline": True},
                {"name": "Range Low",     "value": f"`{rng['low']}`",          "inline": True},
                {"name": "Equilibrium",   "value": f"`{rng['equilibrium']}`",  "inline": True},
                {"name": "Desviación #",  "value": f"**{dev_number} de 2**",   "inline": True},
                {"name": "Acción",        "value": action,                     "inline": False},
            ],
            "footer": {"text": "Binance Futures · TCT Range Scanner"},
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }],
        "username": "TCT Scanner",
    }


def main():
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now_str}] Iniciando TCT Range Scanner...")

    state   = load_state()
    symbols = get_top_symbols_by_volume(TOP_N_COINS)
    print(f"Escaneando {len(symbols)} symbols...")

    alerts_sent = 0

    for symbol in symbols:
        print(f"\n  {symbol}...")
        candles = get_klines(symbol)
        if len(candles) < MIN_RANGE_CANDLES + 10:
            print(f"    Datos insuficientes.")
            continue

        rng = find_range(candles)
        if not rng:
            print(f"    Sin rango válido.")
            continue

        print(f"    Rango: {rng['low']} — {rng['high']} ({rng['range_pct']}% | {rng['candles']}h)")

        # ¿Rango nuevo? (no lo teníamos en estado o cambió)
        state_key     = f"{symbol}_range"
        prev_range    = state.get(state_key)
        range_is_new  = (
            not prev_range
            or abs(prev_range.get("high", 0) - rng["high"]) > rng["high"] * 0.001
            or abs(prev_range.get("low",  0) - rng["low"])  > rng["low"]  * 0.001
        )

        if range_is_new:
            print(f"    Rango nuevo detectado.")
            state[state_key] = rng
            reset_deviations(symbol, state)
            if not already_alerted(symbol, "range", state):
                payload = build_range_confirmed_embed(symbol, rng)
                if send_discord(payload):
                    mark_alerted(symbol, "range", state)
                    alerts_sent += 1
                    print(f"    Alerta rango enviada.")

        # Detectar desviación
        dev = detect_deviation(candles, rng)
        if dev:
            print(f"    Desviación detectada: {dev['direction']} breach={dev['breach']}%")
            dev_count = increment_deviation(symbol, dev["direction"], state)
            alert_key = f"dev{dev_count}"
            if not already_alerted(symbol, alert_key, state):
                payload = build_deviation_embed(symbol, rng, dev, dev_count)
                if send_discord(payload):
                    mark_alerted(symbol, alert_key, state)
                    alerts_sent += 1
                    print(f"    Alerta desviación #{dev_count} enviada.")
        else:
            print(f"    Sin desviación.")

    save_state(state)
    print(f"\nScan completado. Alertas enviadas: {alerts_sent}")


if __name__ == "__main__":
    main()
