"""Automatic statistics collection: Telegraph views, channel size, TGStat (quota-guarded), chat_member events."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..http import HttpError
from ..timeutil import to_bj
from .db import UNKNOWN, iso
from .links import db

log = logging.getLogger(__name__)
JOINED, LEFT = {"member", "administrator", "creator"}, {"left", "kicked"}
UPDATES_KEY = "growth_update_offset"
TGSTAT_URL = "https://api.tgstat.ru/channels/stat"
TGSTAT_METRICS = ("participants_count", "avg_post_reach", "adv_post_reach_24h", "err_percent", "err24_percent",
                  "er_percent", "daily_reach", "forwards_count", "mentions_count")


def month_key(now: datetime) -> str:
    return to_bj(now).strftime("%Y-%m")  # quota month = Beijing month, like every schedule in the project


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Telegraph views --------------------------------------------------------------------------------------------
def collect_telegraph_views(app, now: datetime | None = None, max_calls: int = 40) -> int:
    """getViews (free, no quota): daily for issues up to 14 days old, weekly for older ones. Only the first part of
    a multi-part article is counted (its link is the one shared and posted)."""
    now, gdb, n = now or _now(), db(app), 0
    for pub in gdb.publications():
        if not pub["article_url"] or n >= max_calls:
            continue
        last = gdb.snapshot_latest(pub["id"], "telegraph", "views", "telegraph")
        age = (now - datetime.fromisoformat(pub["issue_date"]).replace(tzinfo=timezone.utc)).days
        every = timedelta(days=1 if age <= 14 else 7)
        if last and now - datetime.fromisoformat(last["measured_at"]) < every - timedelta(hours=2):
            continue
        n += 1
        try:
            gdb.snapshot_add(pub["id"], "telegraph", "views", app.telegraph.get_views(pub["article_url"]),
                             "telegraph", iso(now))
            gdb.provider_ok("telegraph")
        except Exception as exc:  # noqa: BLE001
            gdb.provider_error("telegraph", f"{type(exc).__name__}: {exc}")
    return n


# --- channel size (Bot API, free) ------------------------------------------------------------------------------
def collect_channel(app, now: datetime | None = None) -> int | None:
    gdb = db(app)
    try:
        count = app.telegram.member_count(app.settings.telegram_chat_id)
    except Exception as exc:  # noqa: BLE001
        gdb.provider_error("telegram_bot", f"{type(exc).__name__}: {exc}")
        return None
    gdb.snapshot_add(0, "telegram", "subscribers", count, "telegram_bot", iso(now or _now()))
    gdb.provider_ok("telegram_bot")
    return count


# --- TGStat STAT API FREE ---------------------------------------------------------------------------------------
def tgstat_state(app, now: datetime | None = None) -> str:
    """'ok' or the reason the daily request is not made."""
    s, gdb, now = app.settings, db(app), now or _now()
    if not s.tgstat_api_token:
        return "not_configured"
    if to_bj(now).date().isoformat() > s.tgstat_access_until:
        return "expired"
    p = gdb.provider("tgstat")
    if p["disabled_until"] and now.isoformat() < p["disabled_until"]:
        return "paused"
    month = month_key(now)
    if gdb.provider_calls("tgstat", month) >= s.tgstat_monthly_limit - s.tgstat_reserve:
        return "quota_guard"
    last = gdb.snapshot_latest(0, "telegram", "participants_count", "tgstat")
    if last and last["measured_at"][:10] == now.date().isoformat():
        return "done_today"
    return "ok"


def collect_tgstat(app, now: datetime | None = None) -> str:
    """One channels/stat request per day. The request is counted before it is made; any quota/auth error pauses
    polling (a quota error until the 1st of next month); older data stays and is flagged as stale in the report."""
    now, gdb, s = now or _now(), db(app), app.settings
    state = tgstat_state(app, now)
    if state != "ok":
        return state
    gdb.provider_count_call("tgstat", month_key(now))
    try:
        data = app.http.request("GET", TGSTAT_URL, params={"token": s.tgstat_api_token,
                                                           "channelId": s.tgstat_channel_id or s.telegram_chat_id})
    except HttpError as exc:
        pause = now + timedelta(hours=24)
        if exc.status in (401, 402, 403, 429):
            first_next = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0)
            pause = first_next if exc.status in (402, 429) else now + timedelta(days=3)
        gdb.provider_error("tgstat", f"HTTP {exc.status}", iso(pause))
        return "error"
    except Exception as exc:  # noqa: BLE001  (network)
        gdb.provider_error("tgstat", type(exc).__name__)
        return "error"
    if not isinstance(data, dict) or data.get("status") != "ok" or not isinstance(data.get("response"), dict):
        err = str(data.get("error") if isinstance(data, dict) else "bad response")[:120]
        gdb.provider_error("tgstat", err, iso(now + timedelta(hours=24)))
        return "error"
    saved = 0
    for metric in TGSTAT_METRICS:
        value = data["response"].get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            gdb.snapshot_add(0, "telegram", metric, value, "tgstat", iso(now))
            saved += 1
    gdb.provider_ok("tgstat")
    return "ok" if saved else "empty"


# --- chat_member events -----------------------------------------------------------------------------------------
def process_update(app, update: dict) -> str | None:
    """join / leave / None. A join through one of our invite links is attributed to that link's source; everything
    else (public username, foreign link, leave) is recorded as UNKNOWN - never guessed."""
    cm = update.get("chat_member")
    if not cm:
        return None
    chat = cm.get("chat", {})
    wanted = app.settings.telegram_chat_id
    if wanted.startswith("@"):
        if (chat.get("username") or "").lower() != wanted[1:].lower():
            return None
    elif str(chat.get("id")) != wanted:
        return None
    old, new = cm.get("old_chat_member", {}).get("status"), cm.get("new_chat_member", {}).get("status")
    when = datetime.fromtimestamp(cm.get("date", 0), timezone.utc).isoformat(timespec="seconds")
    key = f"chat_member:{update['update_id']}"
    gdb = db(app)
    if new in JOINED and old not in JOINED:
        link = gdb.link_by_invite((cm.get("invite_link") or {}).get("invite_link", "")) if cm.get("invite_link") else None
        added = gdb.add_subscription(link["source"] if link else UNKNOWN, link["id"] if link else None,
                                     link["publication_id"] if link else None, "join", key, when)
        return "join" if added else "duplicate"
    if new in LEFT and old in JOINED:
        added = gdb.add_subscription(UNKNOWN, None, None, "leave", key, when)
        return "leave" if added else "duplicate"
    return None


def poll_updates(app, batches: int = 5) -> dict:
    """getUpdates (no webhook exists; setting one would be incompatible). The offset is persisted, events are
    deduplicated by update_id, so a restart or a repeated delivery never double-counts."""
    out = {"join": 0, "leave": 0, "duplicate": 0, "error": None}
    for _ in range(batches):
        raw = app.store.get(UPDATES_KEY)
        try:
            updates = app.telegram.get_updates(int(raw) if raw else None, ["chat_member"])
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]
            try:
                if app.telegram.webhook_url():
                    out["error"] = "webhook is set: chat_member polling is impossible until it is removed (not touched)"
                    flag = f"growth_webhook_alert:{to_bj(_now()).date()}"
                    if not app.store.get(flag):
                        app.store.set(flag, "1")
                        app.notify_admin("Статистика подписок: у бота настроен webhook, getUpdates не работает. "
                                         "Вступления не собираются. Webhook не трогаю: решите, нужен ли он.")
            except Exception:  # noqa: BLE001
                pass
            db(app).provider_error("telegram_updates", out["error"])
            return out
        if not updates:
            break
        for u in updates:
            kind = process_update(app, u)
            if kind:
                out[kind] += 1
        app.store.set(UPDATES_KEY, str(max(u["update_id"] for u in updates) + 1))
    if out["error"] is None:
        db(app).provider_ok("telegram_updates")
    return out
