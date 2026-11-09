"""Hooks called by the Unicorn School publication. None of them may break a publication: errors are logged and the
missing part (share block, buttons) is retried by the scheduler or simply skipped."""
from __future__ import annotations

import logging

from . import share
from .links import db, make_link, public_href
from .share import CHANNEL_URL

log = logging.getLogger(__name__)


def enabled(app) -> bool:
    return app.settings.growth_enabled


def begin(app, issue_date: str, report: dict) -> str | None:
    """Before the Telegraph page is built: the publication row and the article's personal invite link. Returns the
    href for the banner and the subscribe phrase (the public channel link if anything fails), None when growth is off."""
    if not enabled(app):
        return None
    try:
        gdb = db(app)
        pub_id = gdb.publication_upsert(issue_date, report["company_name"])
        return public_href(app, make_link(app, pub_id, issue_date, "telegraph", "cta"))
    except Exception as exc:  # noqa: BLE001
        log.warning("growth.begin failed (%s: %s); plain channel link", type(exc).__name__, exc)
        return CHANNEL_URL


def block(href: str | None) -> list:
    """The subscribe text that follows the banner (the share block is appended later, when the URL is known)."""
    return share.subscribe_nodes(href) if href else []


def _report(app, issue_date: str) -> dict:
    draft = app.store.draft_get(issue_date)
    return (draft or {}).get("payload", {}).get("report", {})


def ensure_share_block(app, pub: dict) -> bool:
    """Append 'Поделиться разбором' to the last part of the article unless it is already there (idempotent: the page
    itself is inspected, so a repeat run or a crash in between never duplicates the block)."""
    gdb = db(app)
    report = _report(app, pub["issue_date"])
    hook = share.build_hook(report)
    urls = share.share_urls(pub["article_url"], pub["company_name"], hook, pub["campaign_id"] or "")
    url, seen = pub["article_url"], set()
    while url and url not in seen:
        seen.add(url)
        page = app.telegraph.get_page_full(url)
        nodes = page.get("content") or []
        nxt = share.next_part_url(nodes)
        if nxt:
            url = nxt
            continue
        if not share.has_share_block(nodes):
            app.telegraph.edit_page(url, page.get("title") or pub["company_name"], nodes + share.share_nodes(urls))
        gdb.mark_share_done(pub["id"])
        return True
    return False


def page_created(app, issue_date: str, report: dict, url: str) -> None:
    if not enabled(app):
        return
    try:
        gdb = db(app)
        pub_id = gdb.publication_upsert(issue_date, report["company_name"], article_url=url)
        ensure_share_block(app, gdb.publication(pub_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("share block not added yet (%s); the scheduler retries", type(exc).__name__)


def post_sent(app, issue_date: str, report: dict, url: str, message_id: int) -> None:
    """Remember the post and give it the second button ('Отправить другу' = Telegram share of the channel post)."""
    if not enabled(app) or message_id <= 0:
        return
    try:
        from datetime import datetime, timezone  # noqa: PLC0415

        gdb = db(app)
        pub_id = gdb.publication_upsert(issue_date, report["company_name"], article_url=url, message_id=message_id,
                                        published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        chat = app.target_chat()
        post_url = f"https://t.me/{chat[1:]}/{message_id}" if chat.startswith("@") else url
        read_href = url
        if app.settings.public_base_url:
            read_href = public_href(app, make_link(app, pub_id, issue_date, "telegram", "post_button", url))
        hook = share.build_hook(report)
        app.telegram.set_buttons(chat, message_id, [
            [("📖 Читать разбор", read_href)],
            [("📤 Отправить другу", share.friend_share_url(post_url, report["company_name"], hook))]])
    except Exception as exc:  # noqa: BLE001
        log.warning("post buttons not updated (%s)", type(exc).__name__)


def retry_pending(app) -> int:
    """Scheduler: add share blocks that failed at publication time (Telegraph was down)."""
    done = 0
    if not enabled(app):
        return 0
    for pub in db(app).share_pending():
        try:
            done += bool(ensure_share_block(app, pub))
        except Exception as exc:  # noqa: BLE001
            log.warning("share block retry failed (%s)", type(exc).__name__)
    return done
