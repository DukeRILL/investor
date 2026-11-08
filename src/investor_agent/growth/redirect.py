"""Click redirect /go/<code>. Only targets stored in our own table are served (no open redirect), and only to
allowed hosts. No IP/UA is stored: the dedup key is a salted hash that changes daily."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from .links import db, destination, target_allowed

BOT_UA = re.compile(r"bot|crawl|spider|preview|facebookexternalhit|whatsapp|curl|wget|python|httpx|slack|vkshare",
                    re.I)
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,16}$")


def resolve_click(app, code: str, user_agent: str, ip: str, salt: str) -> str | None:
    """Target URL (and the click is recorded), or None for an unknown code / disallowed target."""
    if not CODE_RE.match(code or ""):
        return None
    gdb = db(app)
    link = gdb.link_by_code(code)
    if not link:
        return None
    target = destination(link)
    if not target_allowed(target):
        return None
    now = datetime.now(timezone.utc)
    kind = "bot_click" if BOT_UA.search(user_agent or "") or not user_agent else "click"
    visitor = hashlib.sha256(f"{salt}|{now.date()}|{ip}|{user_agent}".encode()).hexdigest()[:20]
    gdb.add_traffic(link["id"], kind, f"{kind}:{link['id']}:{visitor}", now.isoformat(timespec="seconds"))
    return target
