"""Tracking links: Telegram invite links per source, optional redirect, safe targets."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from .db import GrowthDb, normalize_source
from .share import CHANNEL_URL

log = logging.getLogger(__name__)
ALLOWED_TARGET_HOSTS = {"t.me", "telegram.me", "telegra.ph"}  # the redirect never leaves this list


def target_allowed(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and (p.hostname or "") in ALLOWED_TARGET_HOSTS


def db(app) -> GrowthDb:
    cached = getattr(app, "_growth_db", None)
    if cached is None:
        cached = GrowthDb(app.store)
        app._growth_db = cached  # noqa: SLF001
    return cached


def invites_enabled(app, platform: str = "telegram") -> bool:
    """Real invite links only in auto mode of that rubric (a draft/test run must not create links in the channel)."""
    mode = app.settings.riddle_publish_mode if platform == "riddle" else app.settings.publish_mode
    return mode == "auto" and bool(app.settings.telegram_bot_token)


def ensure_invite(app, link: dict, issue_date: str, platform: str = "telegram") -> dict:
    """Give the link its own Telegram invite link (idempotent). On failure the channel's public link is used and
    joins through it are counted as UNKNOWN."""
    if link.get("invite_link") or not invites_enabled(app, platform):
        return link
    try:
        name = f"{link['source'][:9]}-{link['medium'][:8]}-{issue_date}"[:32]
        url = app.telegram.create_invite_link(app.settings.telegram_chat_id, name)
        db(app).link_set(link["id"], invite_link=url)
        link = {**link, "invite_link": url}
    except Exception as exc:  # noqa: BLE001
        log.warning("invite link not created (%s)", type(exc).__name__)
    return link


def destination(link: dict) -> str:
    return link.get("invite_link") or link["target_url"]


def public_href(app, link: dict) -> str:
    """What a reader clicks: our redirect when PUBLIC_BASE_URL is set (clicks become measurable), else the target."""
    base = app.settings.public_base_url.rstrip("/")
    return f"{base}/go/{link['code']}" if base else destination(link)


def make_link(app, pub_id: int, issue_date: str, source: str, medium: str, target: str = CHANNEL_URL,
              platform: str = "telegram") -> dict:
    tag = issue_date if platform == "telegram" else f"{platform}-{issue_date}"
    link = db(app).link_get_or_create(pub_id, normalize_source(source), medium, target, f"issue-{tag}")
    return ensure_invite(app, link, tag, platform)
