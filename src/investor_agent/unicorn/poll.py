"""Reader poll for Unicorn School: which topic to cover in the next issue.

Posted right after the 10:00 issue, closed (and deleted) at 04:00 Beijing = 23:00 Moscow, 30 minutes before the
night preparation at 04:30.
The 4 options come from a pool of 100 (poll_topics.py), no repeats within 7 days;
a non-winner with >= 50% of the winner's votes is carried into the next poll. The winning topic gives only a
search direction, not a company; with no votes the agent picks on its own. See docs/RULES.md.
"""

import json
import logging
import random
from datetime import date, datetime, timedelta

from ..app import App
from ..telegram import TelegramError
from ..storage import utcnow as utc_iso
from .poll_topics import POOL
from ..timeutil import BEIJING, now_bj

log = logging.getLogger(__name__)

CLOSE_TIME = (4, 0)  # Beijing; = 23:00 Moscow. The owner's rule: the poll never closes earlier.
QUESTION = "Какую компанию разобрать в следующем выпуске «Школы единорогов»?"
OPTION_COUNT = 4
OPTION_MAX = 100  # Telegram limit per poll option
HISTORY_KEY = "unicorn_poll_history"  # kv: {option text: ISO date it was last offered}
CARRY_KEY = "unicorn_poll_carry"  # kv: topics that must go into the next poll (>= 50% of the winner's votes)
CARRY_SHARE = 0.5
REPEAT_DAYS = 7  # an option shown in the last 7 days must not be offered again


def poll_key(issue: str) -> str:
    return f"unicorn_poll:{issue}"


def topic_key(issue: str) -> str:
    return f"unicorn_topic:{issue}"


def next_issue(issue: str) -> str:
    return (date.fromisoformat(issue) + timedelta(days=1)).isoformat()


def _history(app: App) -> dict[str, str]:
    try:
        return json.loads(app.store.get(HISTORY_KEY) or "{}")
    except ValueError:
        return {}


def make_options(app: App, issue: str, recent: list[dict] | None = None, rng: random.Random | None = None) -> list[str]:
    """Four random options from the pool for the poll about issue `issue`: from four different categories,
    none offered in the last 7 days. No DeepSeek call. The choice is recorded in the history."""
    rng = rng or random.Random()
    day = date.fromisoformat(issue)
    seen = {t for t, d in _history(app).items() if (day - date.fromisoformat(d)).days < REPEAT_DAYS}
    carry = _carry(app)[:OPTION_COUNT]  # strongest first; ignore the 7-day and one-category rules
    seen -= set(carry)
    free = [(c, t) for c, t in POOL if t not in seen and t not in carry]
    need = OPTION_COUNT - len(carry)
    if need <= 0:
        rng.shuffle(carry)
        return carry
    carry_cats = {c for c, t in POOL if t in carry}
    if len(free) < OPTION_COUNT:  # pool exhausted by a very long streak; cannot happen with 100 topics / 7 days
        free = list(POOL)
    by_cat: dict[str, list[str]] = {}
    for c, t in free:
        by_cat.setdefault(c, []).append(t)
    cats_all = list(by_cat)
    rng.shuffle(cats_all)
    cats_all.sort(key=lambda c: c in carry_cats)  # prefer categories not already taken by carried topics
    options = [rng.choice(by_cat[c]) for c in cats_all[:need]]
    rest = [t for _, t in free if t not in options]
    options += rng.sample(rest, need - len(options))
    options += carry
    rng.shuffle(options)
    return options


def _carry(app: App) -> list[str]:
    try:
        return list(json.loads(app.store.get(CARRY_KEY) or "[]"))
    except ValueError:
        return []


def carry_over(options: list[str], counts: list[int]) -> list[str]:
    """Non-winning options with >= 50% of the winner's votes, strongest first: they must be in the next poll."""
    best = max(counts, default=0)
    if best <= 0:
        return []
    win = set(tally(options, counts))
    pairs = [(c, o) for o, c in zip(options, counts) if o not in win and c >= best * CARRY_SHARE]
    return [o for _, o in sorted(pairs, key=lambda x: -x[0])]


def remember(app: App, issue: str, options: list[str]) -> None:
    """Record options as shown on `issue`; entries older than the repeat window are dropped."""
    day = date.fromisoformat(issue)
    hist = {t: d for t, d in _history(app).items() if (day - date.fromisoformat(d)).days < REPEAT_DAYS}
    hist.update({o: issue for o in options})
    app.store.set(CARRY_KEY, "[]")  # the carried topics are now in a posted poll
    app.store.set(HISTORY_KEY, json.dumps(hist, ensure_ascii=False))


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
    remember(app, issue, options)
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
    topic["carry"] = carry_over(options, counts)
    app.store.set(CARRY_KEY, json.dumps(topic["carry"], ensure_ascii=False))
    app.store.set(topic_key(issue), json.dumps(topic, ensure_ascii=False))
    log.info("unicorn poll %s closed: votes=%s chosen=%s", issue, counts, topic["chosen"] or "agent's choice")
    return topic


def chosen_topics(app: App, issue: str, now: datetime | None = None) -> list[str]:
    topic = close(app, issue, now)
    return list(topic["chosen"]) if topic else []
