"""Invest Radar: every 3 hours (off-peak). Silent unless a verified, new, material event is found."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape

from ..app import App
from ..quality import is_russian, scrub_text, word_count
from ..sources import SourceRegistry
from ..textfmt import reading_line, split_html, to_tg_html
from ..timeutil import PeakHoursError, fmt_bj
from . import market, triggers

log = logging.getLogger(__name__)

CATEGORIES = {"oil_supply", "fed", "macro", "geopolitics", "crypto_market", "crypto_incident", "regulation",
              "equities", "etf_flows", "corporate", "price_move"}
ACTIONS = {"buy": "покупка", "reduce_risk": "сократить риск", "take_partial_profit": "частично зафиксировать прибыль",
           "avoid_new_risk": "не наращивать риск", "wait": "ждать без сделок"}
CONFIDENCE = {"low": "низкая", "medium": "средняя", "high": "высокая"}
DISCLAIMER = ("Информационный материал, не является индивидуальной инвестиционной рекомендацией. "
              "Решения вы принимаете самостоятельно и на свой риск.")
SEEN_TRIGGERS_KEY = "radar_seen_triggers"
ASSET_TO_QUOTE = {"SPX": ["SPX", "ES", "VIX"], "SP500": ["SPX", "ES", "VIX"], "S&P500": ["SPX", "ES", "VIX"],
                  "NDX": ["NDX"], "VIX": ["VIX"], "BRENT": ["BRENT"], "WTI": ["WTI"], "OIL": ["BRENT", "WTI"],
                  "BTC": ["BTC"], "ETH": ["ETH"], "SOL": ["SOL"], "CRYPTO": ["BTC", "ETH"], "ALTS": ["SOL"],
                  "UST": ["US2Y", "US10Y"], "RATES": ["US2Y", "US10Y"], "US10Y": ["US10Y"], "US2Y": ["US2Y"],
                  "DXY": ["DXY"], "USD": ["DXY"],
                  "IMOEX": ["IMOEX"], "MOEX": ["IMOEX"], "RUSSIA": ["IMOEX", "USDRUB"], "USDRUB": ["USDRUB"],
                  "RUB": ["USDRUB"], **{k: [k] for k in market.MOEX_STOCKS}}


def event_key(ev: dict) -> str:
    category = str(ev.get("category", "")).strip().lower()
    assets = ",".join(sorted({str(a).upper().strip() for a in ev.get("assets") or []}))
    occurrence = re.sub(r"[^a-z0-9]+", "-", str(ev.get("occurrence", "")).lower()).strip("-")[:60]
    return f"{category}|{assets}|{str(ev.get('event_date', ''))[:10]}|{occurrence}"


def _seen_triggers(app: App) -> list[str]:
    try:
        return json.loads(app.store.get(SEEN_TRIGGERS_KEY, "[]"))
    except ValueError:
        return []


def _remember_triggers(app: App, keys: list[str]) -> None:
    seen = _seen_triggers(app)
    seen.extend(k for k in keys if k not in seen)
    app.store.set(SEEN_TRIGGERS_KEY, json.dumps(seen[-300:]))


def validate_alert(alert: dict, registry: SourceRegistry, history_keys: set[str]) -> list[str]:
    errors = []
    for field in ("headline", "why_new", "fact", "hypothesis", "unknown"):
        if not str(alert.get(field, "")).strip():
            errors.append(f"{field} is empty")
    ek = alert.get("event_key") or {}
    if ek.get("category") not in CATEGORIES:
        errors.append("event_key.category is invalid")
    plain = alert.get("plain_words") or []
    if not 3 <= len(plain) <= 5:
        errors.append("plain_words must have 3 to 5 sentences")
    text = " ".join(str(alert.get(f, "")) for f in ("headline", "fact", "hypothesis", "unknown")) + " ".join(plain)
    if not is_russian(text):
        errors.append("alert text must be in Russian")
    if alert.get("action") not in ACTIONS:
        errors.append("action is invalid")
    cited = registry.cited_ids(alert.get("fact", ""), alert.get("hypothesis", ""))
    if registry.unknown_citations(alert.get("fact", ""), alert.get("hypothesis", "")):
        errors.append("cites unknown source ids")
    if not registry.verified(cited):
        errors.append("no cited source was opened with fetch_page")
    if alert.get("severity") == "extraordinary" and registry.independent_publishers(cited) < 2:
        errors.append("extraordinary event needs two independent opened sources")
    key = event_key(ek)
    if key in history_keys and not alert.get("updates_previous"):
        errors.append("duplicate of an already reported event")
    if word_count(text) > 1200:
        errors.append("alert is too long")
    return errors


def snapshot_lines(snap: market.Snapshot, assets: list[str]) -> list[str]:
    keys: list[str] = []
    for a in assets:
        for k in ASSET_TO_QUOTE.get(str(a).upper(), [str(a).upper()]):
            if k in snap.quotes and k not in keys:
                keys.append(k)
    lines = []
    for k in keys:
        q = snap.quotes[k]
        ch = f"{q.change:+.2f}{q.change_unit}" if q.change is not None and q.change_unit == "%" else (
            f"{q.change:+.0f} б.п." if q.change is not None else "н/д")
        observed = fmt_bj(datetime.fromisoformat(q.observed_at))
        price = f"{q.price:,.2f}".replace(",", " ")
        lines.append(f"• {escape(q.label)}: <b>{price}</b> ({ch}, {escape(q.basis)}; {observed})")
    return lines


def continuation_line(previous: dict) -> str:
    """Quote of the alert this one continues: its headline and its first conclusion."""
    plain = previous["payload"].get("plain_words") or []
    line = f"↩️ <b>Продолжение:</b> «{escape(scrub_text(previous['headline']))}»"
    if plain:
        line += f"\n<i>Тогда мы писали:</i> {escape(scrub_text(str(plain[0]).strip()))}"
    return line


def format_alert(alert: dict, registry: SourceRegistry, snap: market.Snapshot, checked_at: datetime,
                 previous: dict | None = None) -> list[str]:
    head = [f"🚨 <b>{escape(scrub_text(alert['headline']))}</b>",
            f"🕒 Проверка: {fmt_bj(checked_at)}"]
    if previous:
        head.insert(1, continuation_line(previous))
    if alert.get("event_time_utc"):
        try:
            head.append(f"📍 Событие: {fmt_bj(datetime.fromisoformat(alert['event_time_utc'].replace('Z', '+00:00')))}")
        except ValueError:
            pass
    head.append(f"<i>Почему это новое:</i> {to_tg_html(alert['why_new'], registry, radar_rules=True)}")
    blocks = ["\n".join(head)]
    lines = snapshot_lines(snap, (alert.get("event_key") or {}).get("assets") or [])
    if lines:
        blocks.append("📊 <b>Рынок сейчас</b>\n" + "\n".join(lines))
    blocks.append("<b>ФАКТ</b>\n" + to_tg_html(alert["fact"], registry, radar_rules=True))
    hyp = "<b>ГИПОТЕЗА</b>\n" + to_tg_html(alert["hypothesis"], registry, radar_rules=True)
    for sc in alert.get("scenarios") or []:
        hyp += (f"\n\n▸ <b>{escape(sc.get('name', 'Сценарий'))}:</b> {to_tg_html(sc.get('text', ''), registry, radar_rules=True)}"
                f"\n   ✅ Подтвердит: {escape(sc.get('confirm_if', ''))}"
                f"\n   ❌ Отменит: {escape(sc.get('invalidate_if', ''))}")
    blocks.append(hyp)
    blocks.append("<b>НЕИЗВЕСТНО</b>\n" + to_tg_html(alert["unknown"], registry, radar_rules=True))
    plain = " ".join(str(s).strip() for s in alert["plain_words"])
    final = ("<b>ПРОСТЫМИ СЛОВАМИ — ЧТО ЭТО ЗНАЧИТ И МОЙ ПРОГНОЗ</b>\n" + to_tg_html(plain, registry, radar_rules=True) +
             f"\n\nУверенность: <b>{CONFIDENCE.get(alert.get('confidence'), '—')}</b> · "
             f"Действие: <b>{ACTIONS[alert['action']]}</b>")
    final += f"\n\n<i>{DISCLAIMER}</i>"  # always, whatever the action (standing agreement, RULES.md section 5)
    blocks.append(final)
    blocks[0] += "\n" + reading_line("\n".join(blocks))
    return split_html(blocks)


def _previous_alert(app: App, ref) -> dict | None:
    """The sent alert that the model says this one continues (history id), if it really went to the channel."""
    try:
        prev = app.store.alert_get(int(ref))
    except (TypeError, ValueError):
        return None
    return prev if prev and prev["status"] == "sent" and prev["message_ids"] else None


def _window(app: App, now: datetime, cfg: dict) -> tuple[datetime, datetime]:
    wcfg = cfg.get("window", {})
    last = app.store.last_ok_window_end()
    start = datetime.fromisoformat(last) if last else now - timedelta(hours=wcfg.get("default_hours", 3))
    return max(start, now - timedelta(hours=wcfg.get("max_hours", 12))), now


def run(app: App, now: datetime | None = None) -> str:
    """Returns NO_ALERT | SENT | BLOCKED | FAILED | LOCKED. Raises PeakHoursError to be retried later."""
    now = now or datetime.now(timezone.utc)
    if not app.store.acquire("radar", minutes=90):
        return "LOCKED"
    cfg = triggers.load_config(app.settings.radar_config_path)
    start, end = _window(app, now, cfg)
    run_id = app.store.run_start(start.isoformat(), end.isoformat())
    try:
        decision, note = _run(app, now, start, end, cfg)
        app.store.run_finish(run_id, decision, note, ok=decision in ("NO_ALERT", "SENT", "BLOCKED"))
        return decision
    except PeakHoursError as exc:
        app.store.run_finish(run_id, "DEFERRED", str(exc), ok=False)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("radar failed")
        app.store.run_finish(run_id, "FAILED", str(exc), ok=False)
        app.notify_admin(f"Инвест радар: сбой запуска: {exc}")
        return "FAILED"
    finally:
        app.record_usage("radar")
        app.store.release("radar")


def _run(app: App, now: datetime, start: datetime, end: datetime, cfg: dict) -> tuple[str, str]:
    for stuck in app.store.pending_alerts():  # never resend blindly: delivery state is unknown
        app.store.alert_mark(stuck["id"], "unknown")
        app.notify_admin(f"Инвест радар: статус доставки алерта «{stuck['headline']}» неизвестен, повтор не отправлен.")

    s, ds = app.settings, app.deepseek
    snap = market.collect(app.http, s.fred_api_key, fetch_page=ds.fetch, now=now, store=app.store)
    for asset, data in snap.etf.items():
        for row in data.get("rows", []):
            app.store.etf_save(asset, row["session_date"], row["total_musd"], row["status"])
    etf_hist = {a: list(reversed(app.store.etf_recent(a, 10)))[:-1] for a in snap.etf}
    seen = set(_seen_triggers(app))
    trig = [t for t in triggers.compute(snap, cfg, start, etf_hist) if t["trigger_key"] not in seen]
    history = app.store.radar_history(20)
    history_keys = {h["event_key"] for h in history}
    window = {"window_start_utc": start.isoformat(timespec="minutes"), "window_end_utc": end.isoformat(timespec="minutes"),
              "window_start_beijing": fmt_bj(start), "window_end_beijing": fmt_bj(end)}

    registry = SourceRegistry()
    gate = ds.research(s.deepseek_model_fast, app.prompt("radar_gate"), {
        **window, "snapshot": snap.as_payload(), "triggers": trig,
        "history": [{k: h[k] for k in ("id", "event_key", "headline", "sent_at", "conclusion")} for h in history],
    }, registry, max_tool_calls=15)
    candidates = [c for c in gate.get("candidates") or []
                  if event_key(c) not in history_keys or c.get("updates_history_id")]
    if not candidates and not trig:
        return "NO_ALERT", "; ".join(gate.get("checked") or [])[:500]

    alert = ds.research(s.deepseek_model_pro, app.prompt("invest_radar"), {
        **window, "snapshot": snap.as_payload(), "triggers": trig, "history": history,
        "candidates": candidates, "known_sources": registry.brief(),
    }, registry, max_tool_calls=25)
    _remember_triggers(app, [t["trigger_key"] for t in trig])
    if str(alert.get("decision", "")).upper() != "ALERT":
        return "NO_ALERT", str(alert.get("reason", ""))[:500]

    errors = validate_alert(alert, registry, history_keys)
    if errors:
        log.warning("radar alert blocked: %s", errors)
        return "BLOCKED", "; ".join(errors)

    previous = _previous_alert(app, alert.get("updates_previous"))
    parts = format_alert(alert, registry, snap, now, previous)
    key = event_key(alert["event_key"])
    alert["_snapshot_levels"] = {k: q.price for k, q in snap.quotes.items()}
    alert_id = app.store.alert_create(key, alert["headline"], alert)
    try:
        mids = app.telegram.send_series(app.target_chat(), parts,
                                        reply_to=previous["message_ids"][0] if previous else None)
    except Exception as exc:  # noqa: BLE001
        app.store.alert_mark(alert_id, "failed")
        app.notify_admin(f"Инвест радар: алерт не доставлен: {exc}")
        return "FAILED", str(exc)
    app.store.alert_mark(alert_id, "sent", mids)
    return "SENT", key
