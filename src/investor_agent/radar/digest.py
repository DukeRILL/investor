"""Evening digest of the Invest Radar: one post at 22:00 Moscow (03:00 Beijing) when the day had more than 2 alerts.

The day runs from 22:00 Moscow to 22:00 Moscow. Built from the alerts already sent (no DeepSeek, no new claims).
Times inside the post use the same unlabeled clock as the alerts. docs/RULES.md section 5.
"""

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from ..app import App
from ..quality import scrub_text
from ..textfmt import reading_line
from ..timeutil import to_bj
from .pipeline import DISCLAIMER

log = logging.getLogger(__name__)

MOSCOW = ZoneInfo("Europe/Moscow")
DIGEST_TIME_BEIJING = (3, 0)  # = 22:00 Moscow
MIN_ALERTS = 3                # "more than 2"
MAX_ALERTS = 12
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря"]


def day_window(now: datetime) -> tuple[datetime, datetime]:
    """(start, end) in UTC of the last finished Moscow-evening-to-evening day."""
    local = now.astimezone(MOSCOW)
    end = local.replace(hour=22, minute=0, second=0, microsecond=0)
    if end > local:
        end -= timedelta(days=1)
    return (end - timedelta(days=1)).astimezone(timezone.utc), end.astimezone(timezone.utc)


def _first_sentence(alert: dict) -> str:
    plain = alert["payload"].get("plain_words") or []
    return scrub_text(str(plain[0]).strip()) if plain else ""


def format_digest(alerts: list[dict], end: datetime) -> str:
    day = end.astimezone(MOSCOW)
    lines = [f"🧾 <b>ИТОГИ ДНЯ · Инвест радар</b> · {day.day} {MONTHS[day.month - 1]}",
             f"За сутки радар отправил алертов: {len(alerts)}.", ""]
    for i, a in enumerate(alerts[:MAX_ALERTS], 1):
        when = to_bj(datetime.fromisoformat(a["sent_at"])).strftime("%H:%M")
        lines.append(f"<b>{i}. {escape(scrub_text(a['headline']))}</b> · {when}")
        first = _first_sentence(a)
        if first:
            lines.append(escape(first))
        lines.append("")
    if len(alerts) > MAX_ALERTS:
        lines.append(f"…и ещё {len(alerts) - MAX_ALERTS}.\n")
    lines.append(f"<i>{DISCLAIMER}</i>")
    body = "\n".join(lines)
    return f"{body}\n\n{reading_line(body)}"


def run(app: App, now: datetime | None = None) -> str:
    """Returns SENT | SKIPPED (3 alerts or fewer) | EXISTS | FAILED. Idempotent per day; no DeepSeek."""
    now = now or datetime.now(timezone.utc)
    start, end = day_window(now)
    key = f"radar_digest:{end.astimezone(MOSCOW).date().isoformat()}"
    if app.store.get(key):
        return "EXISTS"
    alerts = app.store.alerts_between(start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))
    if len(alerts) < MIN_ALERTS:
        app.store.set(key, f"skipped:{len(alerts)}")
        return "SKIPPED"
    try:
        mid = app.telegram.send(app.target_chat(radar=True), format_digest(alerts, end))
    except Exception as exc:  # noqa: BLE001
        app.notify_admin(f"Инвест радар: итоги дня не отправлены: {exc}")
        return "FAILED"
    app.store.set(key, f"sent:{mid}")
    return "SENT"
