"""Numeric triggers computed by code from the snapshot and thresholds in config/radar.yaml."""

from datetime import datetime
from pathlib import Path

import yaml

from . import market
from .market import Snapshot, stale


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _move(snap: Snapshot, key: str, threshold: float, window_start: datetime, out: list, group: str) -> None:
    q = snap.quotes.get(key)
    if not q or q.change is None or stale(q, window_start):
        return
    if abs(q.change) >= threshold:
        direction = "up" if q.change > 0 else "down"
        out.append({"trigger_key": f"move|{key}|{q.observed_at[:10]}|{direction}", "group": group, "asset": key,
                    "text": f"{q.label}: {q.change:+.2f}{q.change_unit} ({q.basis}), цена {q.price:g}, "
                            f"наблюдение {q.observed_at} UTC, источник {q.source}"})


def compute(snap: Snapshot, cfg: dict, window_start: datetime, etf_history: dict | None = None) -> list[dict]:
    out: list[dict] = []
    crypto = cfg.get("crypto", {})
    for key in ("BTC", "ETH", "SOL"):
        if key in crypto:
            _move(snap, key, crypto[key]["move_pct"], window_start, out, "crypto")
    for key in ("BTC", "ETH"):
        q = snap.quotes.get(key)
        if not q:
            continue
        fr = q.extra.get("funding_rate_pct")
        if fr is not None and abs(fr) >= crypto.get("funding_abs_pct", 1e9):
            out.append({"trigger_key": f"funding|{key}|{q.observed_at[:13]}", "group": "crypto", "asset": key,
                        "text": f"{key}: экстремальная ставка финансирования {fr:+.4f}% за 8ч (Binance)"})
        oi = q.extra.get("open_interest_change_24h_pct")
        if oi is not None and abs(oi) >= crypto.get("open_interest_change_pct", 1e9):
            out.append({"trigger_key": f"oi|{key}|{q.observed_at[:10]}", "group": "crypto", "asset": key,
                        "text": f"{key}: открытый интерес фьючерсов {oi:+.1f}% за 24ч (Binance)"})

    eq = cfg.get("equities", {})
    s = snap.sessions
    if not s.get("weekend"):
        _move(snap, "SPX", eq.get("SPX", {}).get("move_pct", 1e9), window_start, out, "equities")
        _move(snap, "NDX", eq.get("NDX", {}).get("move_pct", 1e9), window_start, out, "equities")
        _move(snap, "RUT", eq.get("RUT", {}).get("move_pct", 1e9), window_start, out, "equities")
        if not s.get("us_cash_open") and s.get("futures_open"):
            _move(snap, "ES", eq.get("ES", {}).get("move_pct", 1e9), window_start, out, "equities")
        vix = snap.quotes.get("VIX")
        vcfg = eq.get("VIX", {})
        if vix and not stale(vix, window_start) and (
                vix.price >= vcfg.get("level", 1e9) or abs(vix.change or 0) >= vcfg.get("change_pct", 1e9)):
            out.append({"trigger_key": f"vix|{vix.observed_at[:10]}|{int(vix.price)}", "group": "equities",
                        "asset": "VIX", "text": f"VIX {vix.price:.2f} ({vix.change:+.1f}% к закрытию)"})
        for key, c in cfg.get("oil", {}).items():
            _move(snap, key, c["move_pct"], window_start, out, "oil")
        for key, c in cfg.get("rates", {}).items():
            q = snap.quotes.get(key)
            if q and q.change is not None and abs(q.change) >= c["change_bp"]:
                out.append({"trigger_key": f"rates|{key}|{q.observed_at[:10]}", "group": "rates", "asset": key,
                            "text": f"{q.label}: {q.price:.2f}% ({q.change:+.0f} б.п., {q.basis})"})
        for key, c in cfg.get("dollar", {}).items():
            _move(snap, key, c["move_pct"], window_start, out, "macro")

    ru = cfg.get("russia", {})
    if not snap.sessions.get("weekend"):
        _move(snap, "IMOEX", ru.get("IMOEX", {}).get("move_pct", 1e9), window_start, out, "russia")
        for key in market.MOEX_STOCKS:
            _move(snap, key, ru.get("stocks_move_pct", 1e9), window_start, out, "russia")
        _move(snap, "USDRUB", ru.get("USDRUB", {}).get("move_pct", 1e9), window_start, out, "russia")

    etf_cfg = cfg.get("etf_flows", {})
    for asset, data in snap.etf.items():
        final = [r for r in data.get("rows", []) if r["status"] == "final" and r["total_musd"] is not None]
        if not final:
            continue
        last = final[-1]
        limit = etf_cfg.get(asset, {}).get("abs_musd", 1e9)
        if abs(last["total_musd"]) >= limit:
            out.append({"trigger_key": f"etf|{asset}|{last['session_date']}", "group": "etf_flows", "asset": asset,
                        "text": f"Спотовые {asset}-ETF США: чистый поток {last['total_musd']:+.1f} млн $ за сессию "
                                f"{last['session_date']} (итог, Farside)"})
        n = etf_cfg.get("reversal_after_days", 5)
        hist = (etf_history or {}).get(asset) or final[:-1]
        prev = [r["total_musd"] for r in hist[-n:] if r.get("total_musd") is not None]
        if len(prev) >= n and last["total_musd"] and all(p * last["total_musd"] < 0 for p in prev):
            out.append({"trigger_key": f"etf_reversal|{asset}|{last['session_date']}", "group": "etf_flows",
                        "asset": asset, "text": f"{asset}-ETF: разворот потоков после {n} сессий в одну сторону "
                                                f"({last['total_musd']:+.1f} млн $ за {last['session_date']})"})
    return out
