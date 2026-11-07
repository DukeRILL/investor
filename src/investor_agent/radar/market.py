"""Market snapshot collected by code (DeepSeek never quotes prices from memory)."""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..http import Http

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

NYSE_HOLIDAYS = {date.fromisoformat(d) for d in [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24"]}


@dataclass
class Quote:
    key: str
    label: str
    price: float
    change: float | None          # percent, or basis points for yields
    change_unit: str              # "%" or "bp"
    basis: str                    # what the change is measured against (Russian)
    observed_at: str              # UTC ISO
    source: str
    extra: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    taken_at: str
    sessions: dict
    quotes: dict[str, Quote] = field(default_factory=dict)
    etf: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {"taken_at_utc": self.taken_at, "sessions": self.sessions,
                "quotes": {k: asdict(v) for k, v in self.quotes.items()}, "etf_flows": self.etf,
                "data_gaps": self.errors}


# --- sessions ------------------------------------------------------------------------------

def us_cash_open(now: datetime) -> bool:
    ny = now.astimezone(NY)
    if ny.weekday() >= 5 or ny.date() in NYSE_HOLIDAYS:
        return False
    return time(9, 30) <= ny.time() < time(16, 0)


def futures_open(now: datetime) -> bool:
    """CME/ICE energy and equity futures: Sun 18:00 – Fri 17:00 New York, daily break 17:00–18:00."""
    ny = now.astimezone(NY)
    wd, t = ny.weekday(), ny.time()
    if ny.date() in NYSE_HOLIDAYS:
        return False
    if wd == 5 or (wd == 6 and t < time(18, 0)) or (wd == 4 and t >= time(17, 0)):
        return False
    return not (time(17, 0) <= t < time(18, 0))


def sessions(now: datetime) -> dict:
    ny = now.astimezone(NY)
    return {"now_new_york": ny.strftime("%a %Y-%m-%d %H:%M"), "us_cash_open": us_cash_open(now),
            "futures_open": futures_open(now), "crypto_open": True,
            "us_holiday_today": ny.date() in NYSE_HOLIDAYS, "weekend": ny.weekday() >= 5}


# --- sources -------------------------------------------------------------------------------

def _ts(ms_or_s: float) -> str:
    value = float(ms_or_s)
    if value > 1e11:
        value /= 1000
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


CRYPTO = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


def binance_spot(http: Http) -> dict[str, Quote]:
    out = {}
    for key, sym in CRYPTO.items():
        d = http.request("GET", "https://api.binance.com/api/v3/ticker/24hr", params={"symbol": sym}, timeout=20)
        out[key] = Quote(key, f"{key}/USDT спот (Binance)", float(d["lastPrice"]), float(d["priceChangePercent"]), "%",
                         "скользящие 24 часа", _ts(d["closeTime"]), "Binance spot")
    return out


def bybit_spot(http: Http) -> dict[str, Quote]:
    out = {}
    for key, sym in CRYPTO.items():
        d = http.request("GET", "https://api.bybit.com/v5/market/tickers", params={"category": "spot", "symbol": sym},
                         timeout=20)
        t = d["result"]["list"][0]
        out[key] = Quote(key, f"{key}/USDT спот (Bybit)", float(t["lastPrice"]), float(t["price24hPcnt"]) * 100, "%",
                         "скользящие 24 часа", _ts(d["time"]), "Bybit spot")
    return out


def binance_derivatives(http: Http, quotes: dict[str, Quote]) -> None:
    for key in ("BTC", "ETH"):
        sym = CRYPTO[key]
        prem = http.request("GET", "https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": sym}, timeout=20)
        hist = http.request("GET", "https://fapi.binance.com/futures/data/openInterestHist",
                            params={"symbol": sym, "period": "1h", "limit": 25}, timeout=20)
        extra = {"funding_rate_pct": round(float(prem["lastFundingRate"]) * 100, 4),
                 "funding_basis": "последняя ставка финансирования бессрочного фьючерса Binance, за 8 часов"}
        if isinstance(hist, list) and len(hist) >= 2:
            first, last = float(hist[0]["sumOpenInterestValue"]), float(hist[-1]["sumOpenInterestValue"])
            extra["open_interest_usd"] = round(last)
            extra["open_interest_change_24h_pct"] = round((last / first - 1) * 100, 2) if first else None
        if key in quotes:
            quotes[key].extra.update(extra)


YAHOO = {
    "SPX": ("^GSPC", "S&P 500 (кассовый индекс)"),
    "ES": ("ES=F", "E-mini S&P 500, ближайший фьючерс CME"),
    "NDX": ("^NDX", "Nasdaq 100"),
    "RUT": ("^RUT", "Russell 2000"),
    "VIX": ("^VIX", "VIX (CBOE)"),
    "BRENT": ("BZ=F", "Brent, ближайший фьючерс ICE"),
    "WTI": ("CL=F", "WTI, ближайший фьючерс NYMEX"),
    "DXY": ("DX-Y.NYB", "Индекс доллара DXY"),
}


YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_CACHE_KEY = "yahoo_last_good"
YAHOO_CACHE_MAX_HOURS = 12  # an older cached quote is not shown at all


def _yahoo_fetch(http: Http, sym: str, key: str, label: str, host: str) -> Quote:
    d = http.request("GET", f"https://{host}/v8/finance/chart/{sym}",
                     params={"range": "5d", "interval": "1d"}, timeout=20)
    meta = d["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    change = round((price / float(prev) - 1) * 100, 2) if prev else None
    return Quote(key, label, price, change, "%", "к предыдущему закрытию/расчётной цене",
                 _ts(meta["regularMarketTime"]), "Yahoo Finance (возможна задержка до 15 мин)")


def yahoo_quotes(http: Http, errors: list[str], store=None, now: datetime | None = None) -> dict[str, Quote]:
    """Yahoo with a second host on failure. When both refuse (429), the last good quote (at most 12 h old) is used
    and marked as cached; after the first double refusal the rest of the run does not hammer Yahoo."""
    out: dict[str, Quote] = {}
    now = now or datetime.now(timezone.utc)
    cache: dict = {}
    if store is not None:
        try:
            cache = json.loads(store.get(YAHOO_CACHE_KEY, "{}"))
        except ValueError:
            cache = {}
    blocked = False
    for key, (sym, label) in YAHOO.items():
        quote, last_exc = None, None
        for host in () if blocked else YAHOO_HOSTS:
            try:
                quote = _yahoo_fetch(http, sym, key, label, host)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if quote:
            out[key] = quote
            cache[key] = asdict(quote)
            continue
        if last_exc is not None and getattr(last_exc, "status", None) == 429:
            blocked = True
        saved = cache.get(key)
        if saved:
            try:
                age = now - datetime.fromisoformat(saved["observed_at"])
                if age <= timedelta(hours=YAHOO_CACHE_MAX_HOURS):
                    out[key] = Quote(**{**saved, "source": saved["source"] + " — последнее удачное значение (кэш)"})
                    errors.append(f"{key}: Yahoo недоступен, использовано значение из кэша от {saved['observed_at'][:16]}Z")
                    continue
            except (ValueError, KeyError, TypeError):
                pass
        reason = type(last_exc).__name__ if last_exc is not None else "пропущен после отказа Yahoo"
        errors.append(f"{key}: Yahoo недоступен ({reason})")
    if store is not None and out:
        store.set(YAHOO_CACHE_KEY, json.dumps(cache))
    return out


FRED = {"US2Y": ("DGS2", "Доходность 2-летних US Treasuries"), "US10Y": ("DGS10", "Доходность 10-летних US Treasuries")}


def fred_quotes(http: Http, api_key: str, errors: list[str]) -> dict[str, Quote]:
    out = {}
    if not api_key:
        errors.append("FRED_API_KEY не задан: доходности облигаций недоступны")
        return out
    for key, (series, label) in FRED.items():
        try:
            d = http.request("GET", "https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": series, "api_key": api_key, "file_type": "json",
                                     "sort_order": "desc", "limit": 10}, timeout=20)
            obs = [o for o in d.get("observations", []) if o.get("value") not in (".", None)]
            if len(obs) < 2:
                continue
            last, prev = float(obs[0]["value"]), float(obs[1]["value"])
            out[key] = Quote(key, label, last, round((last - prev) * 100, 1), "bp",
                             f"дневное изменение, данные на {obs[0]['date']}",
                             f"{obs[0]['date']}T21:00:00+00:00", "Доходности US Treasuries (дневные данные)")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: FRED недоступен ({type(exc).__name__})")
    return out


# --- Russia: MOEX ISS (stocks, index) and Bank of Russia (official USD/RUB) -----------------

MSK = timezone(timedelta(hours=3))
MOEX_STOCKS = {"SBER": "Сбербанк", "GAZP": "Газпром", "LKOH": "Лукойл", "ROSN": "Роснефть", "GMKN": "Норникель",
               "NVTK": "Новатэк", "TATN": "Татнефть", "YDEX": "Яндекс", "PLZL": "Полюс", "MGNT": "Магнит"}
MOEX_URL = "https://iss.moex.com/iss/engines/stock/markets/{market}.json"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"


def _moex_rows(d: dict) -> list[dict]:
    md = d["marketdata"]
    return [dict(zip(md["columns"], row)) for row in md["data"]]


def _moex_time(row: dict) -> str:
    raw = str(row.get("SYSTIME") or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).astimezone(timezone.utc).isoformat(
            timespec="seconds")
    except ValueError:
        return "1970-01-01T00:00:00+00:00"  # unknown time -> treated as stale


def moex_quotes(http: Http, errors: list[str]) -> dict[str, Quote]:
    """IMOEX and Russian blue chips from MOEX ISS (free, no key). Change is against the previous close."""
    out: dict[str, Quote] = {}
    try:
        d = http.request("GET", MOEX_URL.format(market="index/securities/IMOEX"),
                         params={"iss.meta": "off", "iss.only": "marketdata"}, timeout=20)
        row = _moex_rows(d)[0]
        if row.get("CURRENTVALUE") is not None and row.get("LASTCHANGEPRC") is not None:
            out["IMOEX"] = Quote("IMOEX", "Индекс Мосбиржи (IMOEX)", float(row["CURRENTVALUE"]),
                                 round(float(row["LASTCHANGEPRC"]), 2), "%", "к предыдущему закрытию",
                                 _moex_time(row), "Московская биржа (ISS)")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"IMOEX: Мосбиржа недоступна ({type(exc).__name__})")
    try:
        d = http.request("GET", MOEX_URL.format(market="shares/boards/TQBR/securities"),
                         params={"iss.meta": "off", "iss.only": "marketdata",
                                 "securities": ",".join(MOEX_STOCKS)}, timeout=20)
        for row in _moex_rows(d):
            sec = row.get("SECID")
            if sec in MOEX_STOCKS and row.get("LAST") is not None and row.get("LASTTOPREVPRICE") is not None:
                out[sec] = Quote(sec, f"{MOEX_STOCKS[sec]} ({sec}, Мосбиржа)", float(row["LAST"]),
                                 round(float(row["LASTTOPREVPRICE"]), 2), "%", "к предыдущему закрытию",
                                 _moex_time(row), "Московская биржа (ISS)")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"акции РФ: Мосбиржа недоступна ({type(exc).__name__})")
    return out


def cbr_usdrub(http: Http, errors: list[str]) -> dict[str, Quote]:
    """Official USD/RUB rate of the Bank of Russia vs the previous official rate (the rate is set once per day)."""
    try:
        d = http.request("GET", CBR_URL, timeout=20)
        usd = d["Valute"]["USD"]
        price, prev = float(usd["Value"]), float(usd["Previous"])
        stamp = datetime.fromisoformat(d["Date"]).astimezone(timezone.utc).isoformat(timespec="seconds")
        return {"USDRUB": Quote("USDRUB", "Курс доллара к рублю (официальный, ЦБ РФ)", price,
                                round((price / prev - 1) * 100, 2), "%", "к предыдущему официальному курсу",
                                stamp, "Банк России (cbr-xml-daily.ru)")}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"USD/RUB: курс ЦБ недоступен ({type(exc).__name__})")
        return {}


# --- ETF flows (Farside, an aggregator) -----------------------------------------------------

DATE_RE = re.compile(r"^\s*\|?\s*(\d{1,2} \w{3} \d{4})\s*\|")


def _num(cell: str) -> float | None:
    cell = cell.strip().replace(",", "")
    if cell in ("", "-", "–"):
        return None
    neg = cell.startswith("(") and cell.endswith(")")
    try:
        value = float(cell.strip("()"))
    except ValueError:
        return None
    return -value if neg else value


def parse_farside(markdown: str) -> list[dict]:
    """Return rows [{session_date, total_musd, status}] newest last. Blank cells -> preliminary."""
    rows = []
    for line in markdown.splitlines():
        m = DATE_RE.match(line)
        if not m:
            continue
        try:
            day = datetime.strptime(m.group(1), "%d %b %Y").date()
        except ValueError:
            continue
        cells = [c for c in line.strip().strip("|").split("|")][1:]
        if not cells:
            continue
        total = _num(cells[-1])
        blanks = sum(1 for c in cells[:-1] if c.strip() in ("", "–"))
        rows.append({"session_date": day.isoformat(), "total_musd": total,
                     "status": "preliminary" if total is None or blanks else "final"})
    rows.sort(key=lambda r: r["session_date"])
    if rows and rows[-1]["status"] == "final":
        # the newest row can still be incomplete even without blanks; treat as final only if it is not today in NY
        today_ny = datetime.now(NY).date().isoformat()
        if rows[-1]["session_date"] >= today_ny:
            rows[-1]["status"] = "preliminary"
    return rows


FARSIDE = {"BTC": "https://farside.co.uk/btc/", "ETH": "https://farside.co.uk/eth/"}


# --- collect ------------------------------------------------------------------------------

def collect(http: Http, fred_key: str, fetch_page=None, now: datetime | None = None, store=None) -> Snapshot:
    now = now or datetime.now(timezone.utc)
    snap = Snapshot(taken_at=now.isoformat(timespec="seconds"), sessions=sessions(now))
    try:
        snap.quotes.update(binance_spot(http))
    except Exception as exc:  # noqa: BLE001
        snap.errors.append(f"Binance spot недоступен ({type(exc).__name__}); пробую Bybit")
        try:
            snap.quotes.update(bybit_spot(http))
        except Exception as exc2:  # noqa: BLE001
            snap.errors.append(f"Bybit недоступен ({type(exc2).__name__})")
    try:
        binance_derivatives(http, snap.quotes)
    except Exception as exc:  # noqa: BLE001
        snap.errors.append(f"деривативы Binance недоступны ({type(exc).__name__})")
    snap.quotes.update(yahoo_quotes(http, snap.errors, store, now))
    snap.quotes.update(fred_quotes(http, fred_key, snap.errors))
    snap.quotes.update(moex_quotes(http, snap.errors))
    snap.quotes.update(cbr_usdrub(http, snap.errors))
    if fetch_page:
        for asset, url in FARSIDE.items():
            try:
                rows = parse_farside(fetch_page(url).get("content", ""))
                snap.etf[asset] = {"source": f"Farside Investors (агрегатор), {url}", "rows": rows[-7:]}
            except Exception as exc:  # noqa: BLE001
                snap.errors.append(f"потоки ETF {asset} недоступны ({type(exc).__name__})")
    return snap


def stale(quote: Quote, window_start: datetime) -> bool:
    try:
        return datetime.fromisoformat(quote.observed_at) < window_start - timedelta(minutes=1)
    except ValueError:
        return True
