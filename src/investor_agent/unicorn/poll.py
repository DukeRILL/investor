"""Reader poll for Unicorn School: which topic to cover in the next issue.

Posted right after the 10:00 issue, closed (and deleted) at 04:00 Beijing = 23:00 Moscow, 30 minutes before the
night preparation at 04:30.
The winning topic narrows the candidate search; with no votes the agent picks on its own. See docs/RULES.md.
"""

import json
import logging
from datetime import date, datetime, timedelta

from ..app import App
from ..quality import is_russian
from ..telegram import TelegramError
from ..storage import utcnow as utc_iso
from ..timeutil import BEIJING, now_bj

log = logging.getLogger(__name__)

CLOSE_TIME = (4, 0)  # Beijing; = 23:00 Moscow. The owner's rule: the poll never closes earlier.
QUESTION = "Какую компанию разобрать в следующем выпуске «Школы единорогов»?"
OPTION_COUNT = 4
OPTION_MAX = 100  # Telegram limit per poll option
# Used when DeepSeek could not propose options; rotated by date. Broad on purpose: market, sector, year, region.
FALLBACK_TOPICS = [
    "Финтех и платежи", "Компании, основанные после 2020 года", "Европейский стартап", "Сервис для малого бизнеса",
    "HR, найм и управление командой", "Логистика и доставка", "Компании из Азии или Латинской Америки",
    "Маркетплейс", "Здоровье и медицина", "Основаны до 2015 года и до сих пор частные", "Образование",
    "Потребительское приложение", "B2B-софт для автоматизации", "Компании из Индии", "Страхование и финуслуги",
    "Агротех и еда",
]


def poll_key(issue: str) -> str:
    return f"unicorn_poll:{issue}"


def topic_key(issue: str) -> str:
    return f"unicorn_topic:{issue}"


def next_issue(issue: str) -> str:
    return (date.fromisoformat(issue) + timedelta(days=1)).isoformat()


def fallback_options(issue: str) -> list[str]:
    start = date.fromisoformat(issue).toordinal() * OPTION_COUNT % len(FALLBACK_TOPICS)
    return [FALLBACK_TOPICS[(start + i) % len(FALLBACK_TOPICS)] for i in range(OPTION_COUNT)]


def clean_options(raw) -> list[str]:
    """Exactly four distinct Russian options of at most 100 characters, or an empty list."""
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = " ".join(str(item.get("text", "") if isinstance(item, dict) else item).split()).strip(" .")
        if text and len(text) <= OPTION_MAX and is_russian(text) and text.lower() not in {o.lower() for o in out}:
            out.append(text)
    return out[:OPTION_COUNT] if len(out) >= OPTION_COUNT else []


def make_options(app: App, issue: str, recent: list[dict]) -> list[str]:
    """Topics for the poll about issue `issue`. Called during the night preparation (off-peak); never fails."""
    try:
        result = app.deepseek.complete_json(app.settings.deepseek_model_fast, app.prompt("unicorn_poll"), {
            "issue_date": issue,
            "recent": [{"industry": r.get("industry"), "country": r.get("country")} for r in recent],
        }, temperature=0.9)
        options = clean_options(result.get("options"))
    except Exception as exc:  # noqa: BLE001 - the poll must go out even without DeepSeek
        log.warning("poll options failed: %s", type(exc).__name__)
        options = []
    return options or fallback_options(issue)


def tally(options: list[str], counts: list[int]) -> list[str]:
    """Winning topics: the option(s) with the most votes. A single vote counts. No votes -> []."""
    best = max(counts, default=0)
    return [o for o, c in zip(options, counts) if best > 0 and c == best]


def post(app: App, issue: str, options: list[str]) -> int | None:
    """Send the poll for issue `issue` to the channel. Failures go to the admin, never stop the publication."""
    if app.store.get(poll_key(issue)):
        return None
    chat = app.target_chat()
    try:
        mid = app.telegram.send_poll(chat, QUESTION, options)
    except TelegramError as exc:
        app.notify_admin(f"Школа единорогов: голосование за тему на {issue} не отправлено: {exc}")
        return None
    app.store.set(poll_key(issue), json.dumps({"chat": chat, "message_id": mid, "options": options,
                                               "posted_at": utc_iso()}, ensure_ascii=False))
    return mid


def close_at(issue: str) -> datetime:
    return datetime.combine(date.fromisoformat(issue), datetime.min.time(), BEIJING).replace(
        hour=CLOSE_TIME[0], minute=CLOSE_TIME[1])


def close(app: App, issue: str, now: datetime | None = None) -> dict | None:
    """Stop the poll for issue `issue`, record the result and delete the poll. Idempotent; no DeepSeek.
    Before 04:00 Beijing (23:00 Moscow) on the issue day the poll stays open and nothing is returned."""
    saved = app.store.get(topic_key(issue))
    if saved:
        return json.loads(saved)
    if (now or now_bj()) < close_at(issue):
        return None
    raw = app.store.get(poll_key(issue))
    if not raw:
        return None
    poll = json.loads(raw)
    options, counts = poll["options"], [0] * len(poll["options"])
    try:
        result = app.telegram.stop_poll(poll["chat"], poll["message_id"])
        votes = {str(o.get("text")): int(o.get("voter_count") or 0) for o in result.get("options") or []}
        counts = [votes.get(o, 0) for o in options]
    except TelegramError as exc:
        log.warning("stop poll failed: %s", exc)
    try:
        app.telegram.delete(poll["chat"], poll["message_id"])
    except TelegramError as exc:
        log.warning("delete poll failed: %s", exc)
    topic = {"issue": issue, "options": options, "votes": counts, "total": sum(counts),
             "chosen": tally(options, counts), "closed_at": utc_iso()}
    app.store.set(topic_key(issue), json.dumps(topic, ensure_ascii=False))
    log.info("unicorn poll %s closed: votes=%s chosen=%s", issue, counts, topic["chosen"] or "agent's choice")
    return topic


def chosen_topics(app: App, issue: str, now: datetime | None = None) -> list[str]:
    topic = close(app, issue, now)
    return list(topic["chosen"]) if topic else []
