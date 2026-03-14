
"""
Binance Meme Coin Futures Listing Tracker
- Detecta nuevos contratos perpetuos de meme coins en Binance
- Notifica por Discord con embed visual (funding rate incluido)
- Optimizado para GitHub Actions plan gratuito (cada 30 min = ~1440 min/mes)
"""

import os
import json
import re
import requests
from datetime import datetime, timezone

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

SEEN_FILE = "seen_listings.json"

BINANCE_ANNOUNCEMENTS_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    "?type=1&pageNo=1&pageSize=15"
)
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

MEME_KEYWORDS = [
    "meme", "pepe", "doge", "shib", "floki", "inu", "elon", "moon",
    "safe", "baby", "bonk", "wif", "brett", "popcat", "dog", "cat",
    "frog", "wojak", "chad", "pnut", "goat", "cow", "neiro", "mog",
    "turbo", "fwog", "giga", "pup", "ponke", "slerf", "myro", "bome",
    "act", "vine", "pippin", "harambe", "degen", "wen", "cope", "ngmi",
    "fartcoin", "kekius", "michi", "nub", "maneki",
]

FUTURES_KEYWORDS = [
    "perpetual", "futures", "usdt-margined", "usd-margined",
    "will launch", "perpetual contract", "perp",
]


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            try:
                return set(json.load(f))
            except Exception:
                return set()
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(list(seen)), f)


def fetch_announcements() -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MemeTracker/1.0)"}
    try:
        resp = requests.get(BINANCE_ANNOUNCEMENTS_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("articles", [])
    except Exception as e:
        print(f"[ERROR] Announcements API: {e}")
        return []


def fetch_funding_rate(symbol: str) -> dict | None:
    if not symbol:
        return None
    try:
        resp = requests.get(
            BINANCE_FUNDING_URL, params={"symbol": f"{symbol}USDT"}, timeout=10
        )
        if resp.status_code == 400:
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        rate = float(data.get("lastFundingRate", 0))
        next_ts = int(data.get("nextFundingTime", 0))
        mark = float(data.get("markPrice", 0))
        next_str = ""
        if next_ts:
            dt = datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc)
            next_str = dt.strftime("%H:%M UTC")
        return {
            "rate_pct": round(rate * 100, 4),
            "next_funding": next_str,
            "mark_price": mark,
        }
    except Exception as e:
        print(f"[WARN] Funding rate fetch failed for {symbol}: {e}")
        return None


def funding_color(rate_pct: float) -> int:
    """Color del embed según qué tan caro está el funding para shortear."""
    if rate_pct > 0.3:
        return 0xED4245   # rojo — muy caro
    elif rate_pct > 0.1:
        return 0xFEE75C   # amarillo — caro pero manejable
    elif rate_pct >= 0:
        return 0x57F287   # verde — barato, buen momento
    else:
        return 0xEB459E   # rosa — negativo, shorts pagan a longs


def funding_interpretation(rate_pct: float) -> str:
    if rate_pct > 0.3:
        return "🔴 Muy caro — esperá que baje el rate"
    elif rate_pct > 0.1:
        return "🟡 Caro pero manejable si el trade es corto"
    elif rate_pct >= 0:
        return "🟢 Barato, buen momento para entrar"
    else:
        return "🟣 Negativo — shorts pagan a longs, cuidado"


def is_futures_announcement(title: str) -> bool:
    return any(kw in title.lower() for kw in FUTURES_KEYWORDS)


def is_meme_coin(title: str) -> tuple[bool, list[str]]:
    title_l = title.lower()
    found = [kw for kw in MEME_KEYWORDS if kw in title_l]
    return bool(found), found


def extract_symbol(title: str) -> str:
    match = re.search(r"\(([A-Z0-9]{2,12})\)", title)
    return match.group(1) if match else ""


def format_timestamp(ts_ms) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ""


def build_article_url(article: dict) -> str:
    code = article.get("code", "")
    if code:
        return f"https://www.binance.com/en/support/announcement/{code}"
    return "https://www.binance.com/en/futures"


def build_discord_embed(title: str, keywords: list[str], url: str,
                        date_str: str, symbol: str, funding: dict | None) -> dict:
    """Construye el payload de Discord con embed visual."""

    color = 0x7289DA  # azul Discord por default
    fields = []

    # Keywords como texto
    kw_str = "  ".join(f"`#{kw}`" for kw in keywords[:6])

    if funding:
        rate = funding["rate_pct"]
        color = funding_color(rate)
        sign = "+" if rate >= 0 else ""
        daily_cost = abs(rate) * 3

        fields = [
            {
                "name": "Funding rate",
                "value": f"**{sign}{rate}%** cada 8h",
                "inline": True,
            },
            {
                "name": "Próximo cobro",
                "value": funding["next_funding"] or "—",
                "inline": True,
            },
            {
                "name": "Mark price",
                "value": f"${funding['mark_price']}",
                "inline": True,
            },
            {
                "name": "Costo/día por $1000 short",
                "value": f"~${daily_cost:.2f}",
                "inline": True,
            },
            {
                "name": "Interpretación",
                "value": funding_interpretation(rate),
                "inline": False,
            },
        ]
    else:
        fields = [
            {
                "name": "Funding rate",
                "value": "Contrato aún no activo",
                "inline": False,
            }
        ]

    fields.append({
        "name": "Timing recomendado para short",
        "value": "Entrar **4–24h** post-anuncio · cerrar en **7–14 días**\nPump típico: +150–300% · Caída desde ATH: 60–85%",
        "inline": False,
    })

    embed = {
        "title": f"🚀  {title}",
        "url": url,
        "color": color,
        "fields": [
            {
                "name": "Token",
                "value": f"**${symbol}**" if symbol else "—",
                "inline": True,
            },
            {
                "name": "Keywords",
                "value": kw_str or "—",
                "inline": True,
            },
            {
                "name": "Anunciado",
                "value": date_str or "—",
                "inline": True,
            },
            *fields,
        ],
        "footer": {
            "text": "Binance Futures · MemeTracker"
        },
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    return {"embeds": [embed], "username": "MemeTracker"}


def send_discord(payload: dict) -> bool:
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Discord webhook: {e}")
        return False


def main():
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] Iniciando scan...")

    seen = load_seen()
    articles = fetch_announcements()

    if not articles:
        print("Sin artículos. Terminando.")
        return

    alerts_sent = 0

    for art in articles:
        uid = str(art.get("id", ""))
        title = art.get("title", "")

        if not uid or uid in seen:
            continue

        seen.add(uid)

        is_futures = is_futures_announcement(title)
        is_meme, keywords = is_meme_coin(title)

        if is_futures and is_meme:
            symbol = extract_symbol(title)
            date_str = format_timestamp(art.get("releaseDate", 0))
            url = build_article_url(art)
            funding = fetch_funding_rate(symbol) if symbol else None

            print(f"[MATCH] {title} | symbol={symbol} | funding={funding}")

            payload = build_discord_embed(title, keywords, url, date_str, symbol, funding)
            if send_discord(payload):
                print("  Discord OK")
                alerts_sent += 1
        else:
            tag = "[futures]" if is_futures else "[meme]" if is_meme else "[skip]"
            print(f"{tag} {title}")

    save_seen(seen)
    print(f"\nListo. Alertas enviadas: {alerts_sent}")


if __name__ == "__main__":
    main()
