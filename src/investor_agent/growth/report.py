"""Daily admin report + CSV/JSON export. Everything is labelled: измерено / расчёт / нет данных / устарело."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from html import escape

from . import collect
from .db import UNKNOWN, iso
from .links import db

STALE_AFTER = timedelta(hours=48)


def _fmt(n) -> str:
    return "н/д" if n is None else (f"{n:,.0f}".replace(",", " ") if float(n).is_integer() else f"{n:.1f}")


def _delta(gdb, now: datetime, days: int) -> int | None:
    last = gdb.snapshot_latest(0, "telegram", "subscribers", "telegram_bot")
    base = gdb.snapshot_latest(0, "telegram", "subscribers", "telegram_bot", before=iso(now - timedelta(days=days)))
    return int(last["value"] - base["value"]) if last and base else None


def _age_note(measured_at: str | None, now: datetime) -> str:
    if not measured_at:
        return "нет данных"
    stale = now - datetime.fromisoformat(measured_at) > STALE_AFTER
    return f"от {measured_at[:10]}" + (" ⚠️ устарело" if stale else "")


def _name(pub: dict) -> str:
    return pub['company_name'] + (" · Разгадка" if pub.get('platform') == 'riddle' else "")


def build_report(app, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    gdb = db(app)
    since = {d: iso(now - timedelta(days=d)) for d in (1, 7, 30)}
    L = ["📈 <b>Школа единорогов — статистика</b>", ""]
    subs = gdb.snapshot_latest(0, "telegram", "subscribers", "telegram_bot")
    L.append("<b>Канал</b> <i>(измерено Bot API)</i>")
    L.append(f"Подписчиков: <b>{_fmt(subs['value']) if subs else 'н/д'}</b> · {_age_note(subs and subs['measured_at'], now)}")
    L.append("Прирост <i>(расчёт по снимкам)</i>: сутки {} · неделя {} · месяц {}".format(
        *[("накапливается" if (d := _delta(gdb, now, n)) is None else f"{d:+d}") for n in (1, 7, 30)]))
    total = gdb.subscription_counts()
    for label, key in (("за сутки", 1), ("за неделю", 7), ("за месяц", 30)):
        c = gdb.subscription_counts(since[key])
        joins = sum(v for (s, t), v in c.items() if t == "join")
        leaves = c.get((UNKNOWN, "leave"), 0)
        L.append(f"Вступлений {label} <i>(события chat_member)</i>: {joins} · отписок: {leaves}")
    L.append("")
    L.append("<b>Подписки по источникам</b> <i>(за 30 дней, подтверждённые вступления)</i>")
    c30 = gdb.subscription_counts(since[30])
    by_src = {s: v for (s, t), v in c30.items() if t == "join"}
    L.append(" · ".join(f"{escape(s)} {v}" for s, v in sorted(by_src.items(), key=lambda kv: -kv[1])) or "нет вступлений")
    L.append("<i>Сумма по источникам ≠ общий прирост: отписки и вступления без ссылки в неё не входят; "
             "без известной ссылки источник = UNKNOWN.</i>")
    L.append("")
    pubs = gdb.publications(7)
    L.append("<b>Последние выпуски</b>")
    traffic = gdb.traffic_counts()
    if not pubs:
        L.append("нет данных")
    for pub in pubs:
        v = gdb.snapshot_latest(pub["id"], "telegraph", "views", "telegraph")
        views = int(v["value"]) if v else None
        joins = sum(n for (s, t), n in gdb.subscription_counts(pub_id=pub["id"]).items() if t == "join")
        clicks = sum(n for (s, t), n in gdb.traffic_counts(pub["id"]).items() if t == "click")
        conv = f"{joins / views * 100:.1f}% (расчёт)" if views else "н/д"
        click_txt = str(clicks) if app.settings.public_base_url else "не измеряются"
        L.append(f"• {escape(_name(pub))} ({pub['issue_date']}): просмотры Telegraph {_fmt(views)} "
                 f"({_age_note(v and v['measured_at'], now)}), переходы по ссылкам {click_txt}, вступления по "
                 f"ссылке выпуска {joins}, конверсия вступлений к просмотрам {conv}")
    top_views = sorted(((gdb.snapshot_latest(p["id"], "telegraph", "views", "telegraph"), p) for p in gdb.publications()),
                       key=lambda x: -(x[0]["value"] if x[0] else -1))[:3]
    if top_views and top_views[0][0]:
        L += ["", "<b>Лучшие по просмотрам</b>"] + [f"{i}. {escape(_name(p))} — {_fmt(v['value'])}"
                                                       for i, (v, p) in enumerate(top_views, 1) if v]
    best_join = sorted(((sum(n for (s, t), n in gdb.subscription_counts(pub_id=p["id"]).items() if t == "join"), p)
                        for p in gdb.publications()), key=lambda x: -x[0])[:3]
    if best_join and best_join[0][0]:
        L += ["", "<b>Лучшие по подпискам</b>"] + [f"{i}. {escape(_name(p))} — {n}"
                                                   for i, (n, p) in enumerate(best_join, 1) if n]
    L += ["", "<b>TGStat</b> <i>(внешний сервис, FREE)</i>"]
    st = collect.tgstat_state(app, now)
    parts = []
    for metric, label in (("avg_post_reach", "средний охват"), ("err_percent", "ERR %"), ("er_percent", "ER %")):
        s = gdb.snapshot_latest(0, "telegram", metric, "tgstat")
        if s:
            parts.append(f"{label} {_fmt(s['value'])} ({_age_note(s['measured_at'], now)})")
    L.append("; ".join(parts) or "данных нет")
    note = {"not_configured": "токен не задан", "expired": "бесплатный доступ закончился", "paused": "опрос приостановлен после ошибки/лимита",
            "quota_guard": "месячный лимит запросов почти исчерпан, опрос остановлен"}.get(st)
    calls = gdb.provider_calls("tgstat", collect.month_key(now))
    L.append(f"Запросов в этом месяце: {calls}/{app.settings.tgstat_monthly_limit}" + (f" · {note}" if note else ""))
    for name, label in (("telegraph", "Telegraph"), ("telegram_bot", "Telegram Bot API"), ("tgstat", "TGStat"),
                        ("telegram_updates", "chat_member")):
        p = gdb.provider(name)
        if p["last_error"]:
            L.append(f"⚠️ {label}: последняя ошибка {p['last_error_at'][:16]} — {escape(p['last_error'])}")
    L += ["", "<i>Просмотры поста Telegram Bot API не отдаёт — не измеряются. Просмотры Telegraph ≠ уникальные "
              "читатели. Переходы по invite-ссылкам Telegram не показывает — только вступления.</i>"]
    return "\n".join(L)


def run_daily(app) -> str:
    """08:00 Beijing: collect everything (each step isolated), then send the summary to the admin chat only."""
    steps = {}
    for name, fn in (("channel", collect.collect_channel), ("telegraph", collect.collect_telegraph_views),
                     ("tgstat", collect.collect_tgstat), ("updates", collect.poll_updates)):
        try:
            steps[name] = fn(app)
        except Exception as exc:  # noqa: BLE001
            steps[name] = f"error: {type(exc).__name__}"
    if not app.settings.telegram_admin_chat_id:
        return "no_admin_chat"
    app.telegram.send(app.settings.telegram_admin_chat_id, build_report(app))
    return "sent"


def export(app, out_dir) -> list[str]:
    """CSV + JSON of every table (no personal data is stored in any of them)."""
    from pathlib import Path  # noqa: PLC0415

    gdb, out = db(app), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = {"publications": gdb.publications(10_000), "tracking_links": gdb.links(),
            "metric_snapshots": gdb.snapshots()}
    with gdb.store._conn() as c:
        data["traffic_events"] = [dict(r) for r in c.execute("SELECT * FROM traffic_events")]
        data["subscription_events"] = [dict(r) for r in c.execute("SELECT * FROM subscription_events")]
    files = []
    for name, rows in data.items():
        (out / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        (out / f"{name}.csv").write_text(buf.getvalue(), encoding="utf-8")
        files += [f"{name}.json", f"{name}.csv"]
    return files


def backfill(app) -> int:
    """Register already published issues (no page edits) so their Telegraph views start to accumulate."""
    gdb, n = db(app), 0
    with app.store._conn() as c:
        rows = [dict(r) for r in c.execute("SELECT issue_date, page_url, message_ids, payload, published_at "
                                           "FROM unicorn_drafts WHERE status='published' AND page_url IS NOT NULL")]
    for r in rows:
        report = json.loads(r["payload"]).get("report", {})
        mids = json.loads(r["message_ids"] or "[]")
        gdb.publication_upsert(r["issue_date"], report.get("company_name", "?"), r["page_url"],
                               mids[0] if mids else None, r["published_at"])
        n += 1
    return n
