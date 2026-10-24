"""Prices that change every minute — crypto and currency rates — straight from a public data source, without the AI
and without a web search: «какая сейчас цена Bitcoin?», «курс доллара», «сколько стоит эфир в тенге».

Crypto: CoinGecko (no key; the coin's id is the only thing sent). Currencies: ExchangeRate-API's open endpoint
(daily rates, no key). The home currency is the person's choice (kv home_currency) or follows the computer's time
zone (Asia/Almaty → тенге, Europe/Moscow → рубли). Each answer names its source and the time of the figure.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

import aiohttp

from .base import Risk, Tool, ToolError, ToolInputError, ToolResult, ToolSpec, Verification

UA = "JARVIS-WorkOS/1 (personal desktop assistant)"
COINS = {  # spoken / written name → (CoinGecko id, name)
    r"биткоин\w*|биткойн\w*|bitcoin|btc|битк\w*": ("bitcoin", "Bitcoin"),
    r"эфир(?:иум\w*|а|у|ом)?|ethereum|ether|eth": ("ethereum", "Ethereum"),
    r"солан\w*|solana|sol": ("solana", "Solana"),
    r"тонкоин\w*|toncoin|\bton\b|\bтон\b": ("the-open-network", "Toncoin"),
    r"usdt|тезер\w*|tether": ("tether", "Tether"),
    r"доги\w*|dogecoin|doge": ("dogecoin", "Dogecoin"),
    r"рипл\w*|ripple|xrp": ("ripple", "XRP"),
    r"\bbnb\b|бинанс ?коин\w*": ("binancecoin", "BNB"),
    r"лайткоин\w*|litecoin|ltc": ("litecoin", "Litecoin"),
}
FIAT = {  # spoken name → ISO code
    r"доллар\w*|usd|бакс\w*|\$": "USD", r"евро|eur|€": "EUR", r"рубл\w*|rub|₽": "RUB", r"тенге|теңге|kzt|₸": "KZT",
    r"юан\w*|cny": "CNY", r"фунт\w*|gbp|£": "GBP", r"лир\w*|try": "TRY", r"дирхам\w*|aed": "AED",
    r"сом\b|сома|kgs": "KGS", r"сум\b|сума|uzs": "UZS", r"иен\w*|йен\w*|jpy": "JPY", r"вон\w*|krw": "KRW",
    r"франк\w*|chf": "CHF", r"гривн\w*|uah": "UAH",
}
SIGN = {"USD": "$", "EUR": "€", "RUB": "₽", "KZT": "₸", "CNY": "¥", "GBP": "£", "JPY": "¥", "TRY": "₺", "UAH": "₴"}
NAME_RU = {"USD": "доллар", "EUR": "евро", "RUB": "рубль", "KZT": "тенге", "CNY": "юань", "GBP": "фунт", "TRY": "лира",
           "AED": "дирхам", "KGS": "сом", "UZS": "сум", "JPY": "иена", "KRW": "вона", "CHF": "франк", "UAH": "гривна"}
_CACHE: dict[str, tuple[float, dict]] = {}


def home_currency(db=None) -> str:
    c = str(db.get_kv("home_currency") or "") if db is not None else ""
    if re.fullmatch(r"[A-Z]{3}", c):
        return c
    try:
        tz = str(Path("/etc/localtime").resolve())
    except OSError:
        tz = ""
    return "KZT" if re.search(r"Almaty|Astana|Aqtobe|Aqtau|Atyrau|Oral|Qostanay|Qyzylorda", tz) else \
        "RUB" if re.search(r"Moscow|Volgograd|Kaliningrad|Samara|Yekaterinburg|Omsk|Novosibirsk", tz) else "USD"


def _find(table: dict, text: str):
    """(value, end of the matched word) of the first name from the table found in the text."""
    best = None
    for pat, val in table.items():
        m = re.search(rf"(?<!\w)(?:{pat})(?!\w)", text)
        if m and (best is None or m.start() < best[1]):
            best = (val, m.start(), m.end())
    return (best[0], best[2]) if best else (None, -1)


def parse_quote(t: str) -> dict | None:
    """«цена биткоина», «сколько стоит эфир в тенге», «курс доллара», «курс евро к рублю», «почём доллар» → args."""
    asks = re.search(r"\b(?:курс\w*|цен[аеуы]|стоимост\w*|почем|почём|сколько (?:сейчас )?стоит|котировк\w*|price|rate)\b", t)
    if not asks:
        return None
    if re.search(r"\b(?:акци\w*|нефт\w*|золот\w*|бензин\w*|квартир\w*|доставк\w*|билет\w*|курсов\w* работ|курс лечени)",
                 t):
        return None
    coin, _ = _find(COINS, t)
    if coin:
        to, _ = _find(FIAT, t[asks.end():]) if re.search(r"\b(?:в|к|за)\s", t[asks.end():]) else (None, -1)
        return {"asset": coin[0], "kind": "crypto", **({"to": to} if to else {})}
    m = re.search(r"\bкурс\w*\s+(.+)$", t) or re.search(r"\b(?:почем|почём|сколько (?:сейчас )?стоит)\s+(.+)$", t)
    if not m:
        return None
    rest = m.group(1)
    base, end = _find(FIAT, rest)
    if not base:
        return None
    to, _ = _find(FIAT, rest[end:]) if re.search(r"\b(?:к|в|на)\s", rest[end:]) else (None, -1)
    return {"asset": base, "kind": "fiat", **({"to": to} if to and to != base else {})}


def money(v: float, code: str, digits: int | None = None) -> str:
    """140 400 ₸ — narrow no-break spaces between thousands (JARVIS's rule for money)."""
    digits = (2 if abs(v) < 100 else 0) if digits is None else digits
    s = f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")
    return f"{s} {SIGN.get(code, code)}"


async def _get(url: str, params: dict, ttl: float) -> dict:
    key = url + repr(sorted(params.items()))
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers={"User-Agent": UA}) as s:
        async with s.get(url, params=params) as r:
            if r.status == 429:
                raise ToolError("источник курсов просит подождать минуту (лимит запросов)")
            r.raise_for_status()
            data = await r.json(content_type=None)
    _CACHE[key] = (time.time() + ttl, data)
    return data


class MarketQuote(Tool):
    def __init__(self, db=None):
        self.db = db
        self.spec = ToolSpec(
            id="market_quote", title="Курс и цена",
            description=("Current price of a cryptocurrency (CoinGecko: bitcoin, ethereum, solana, the-open-network, "
                         "tether, dogecoin, ripple, binancecoin, litecoin) or a currency exchange rate (ISO codes, "
                         "e.g. USD→KZT). Fast, no web search. kind: crypto|fiat; to: ISO code (default: home currency)."),
            params={"type": "object", "required": ["asset"], "properties": {
                "asset": {"type": "string", "maxLength": 40}, "kind": {"type": "string", "enum": ["crypto", "fiat"]},
                "to": {"type": "string", "maxLength": 3}}},
            risk=Risk.LOW, side_effects="none", timeout_s=12, verification="Число получено от источника с его временем")

    def validate(self, args, ctx):
        a = Tool.validate(self, args, ctx)
        a["kind"] = a.get("kind") or ("fiat" if re.fullmatch(r"[A-Za-z]{3}", a["asset"]) and
                                      a["asset"].upper() in NAME_RU else "crypto")
        if a["kind"] == "fiat":
            a["asset"] = a["asset"].upper()
            if not re.fullmatch(r"[A-Z]{3}", a["asset"]):
                raise ToolInputError("Код валюты — три буквы, например USD")
        elif not re.fullmatch(r"[a-z0-9-]{2,40}", a["asset"]):
            raise ToolInputError("Неизвестная криптовалюта")
        if a.get("to"):
            a["to"] = a["to"].upper()
        return a

    def describe(self, args):
        return f"Курс: {args['asset']}" + (f" → {args['to']}" if args.get("to") else "")

    async def run(self, args, ctx):
        home = home_currency(self.db)
        if args["kind"] == "crypto":
            to = args.get("to") or home
            vs = ["usd"] + ([to.lower()] if to != "USD" else [])
            d = await _get("https://api.coingecko.com/api/v3/simple/price",
                           {"ids": args["asset"], "vs_currencies": ",".join(vs), "include_24hr_change": "true",
                            "include_last_updated_at": "true"}, 30)
            row = d.get(args["asset"])
            if not row or "usd" not in row:
                raise ToolError("CoinGecko не знает такую монету")
            name = next((n for i, n in COINS.values() if i == args["asset"]), args["asset"])
            at = datetime.fromtimestamp(row.get("last_updated_at") or time.time())
            usd = row["usd"]
            text = f"{name}: {money(usd, 'USD')}"
            if to != "USD" and row.get(to.lower()):
                text += f" ({money(row[to.lower()], to)})"
            ch = row.get("usd_24h_change")
            if ch is not None:
                text += f", за сутки {'+' if ch >= 0 else '−'}{abs(ch):.1f}".replace(".", ",") + " %"
            text += f". CoinGecko, {at:%H:%M}."
            return ToolResult({"asset": args["asset"], "name": name, "usd": usd, "to": to, "value": row.get(to.lower()),
                               "change_24h": ch, "at": at.isoformat(timespec="minutes"), "source": "CoinGecko",
                               "url": f"https://www.coingecko.com/ru/coins/{args['asset']}"}, text, card="quote")
        base = args["asset"]
        to = args.get("to") or (home if base != home else "USD")
        d = await _get(f"https://open.er-api.com/v6/latest/{base}", {}, 1800)
        if d.get("result") != "success" or to not in (d.get("rates") or {}):
            raise ToolError(f"курс {base} → {to} недоступен")
        rate = float(d["rates"][to])
        at = datetime.fromtimestamp(d.get("time_last_update_unix") or time.time())
        text = f"1 {NAME_RU.get(base, base)} = {money(rate, to, 2 if rate < 10000 else 0)}. ExchangeRate-API, курс на {at:%d.%m %H:%M}."
        return ToolResult({"asset": base, "to": to, "rate": rate, "at": at.isoformat(timespec="minutes"),
                           "source": "ExchangeRate-API", "url": "https://www.exchangerate-api.com"}, text, card="quote")

    async def verify(self, args, result, ctx):
        d = result.data
        ok = (d.get("usd") or d.get("rate") or 0) > 0
        return Verification("verified" if ok else "failed", f"{self.spec.verification}: {d.get('source')}, {d.get('at')}")
