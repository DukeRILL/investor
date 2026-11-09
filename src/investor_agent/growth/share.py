"""Subscribe block, share links and the per-article hook. Pure functions: no network, no state."""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

SUBSCRIBE_LINES = ("Каждый день разбираем бизнес, который смог вырасти до миллиардной оценки.",
                   "Подписывайтесь на канал „🦄 ШКОЛА ЕДИНОРОГОВ“, чтобы находить идеи для собственного бизнеса.")
LINKED_PHRASE = "„🦄 ШКОЛА ЕДИНОРОГОВ“"  # the personal invite link sits on these words, no separate link line
SHARE_TITLE = "Поделиться разбором"
CHANNEL_URL = "https://t.me/investor_duker"


def _q(value: str) -> str:
    return quote(value, safe="")  # percent-encodes Cyrillic, spaces, &, ?, #, newlines


def clean_hook(text: str, limit: int = 140) -> str:
    text = re.sub(r"\[S\d+\]|<[^>]+>|[*_`]", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return cut + "…"


def build_hook(report: dict) -> str:
    """A hook from the facts of this very article: the subtitle, else the first sentence of the teaser."""
    hook = clean_hook(report.get("share_fact") or report.get("subtitle", ""))
    if not hook:
        first = re.split(r"(?<=[.!?])\s", clean_hook(report.get("teaser", ""), 400), maxsplit=1)[0]
        hook = clean_hook(first)
    return hook


def share_text(company: str, hook: str) -> str:
    return f"{company}: {hook}" if hook else f"{company}: разбор в «Школе единорогов»"


def with_utm(url: str, source: str, medium: str, campaign: str) -> str:
    sep = "&" if "?" in url else "?"
    return url + sep + urlencode({"utm_source": source, "utm_medium": medium, "utm_campaign": campaign})


def share_urls(article_url: str, company: str, hook: str, campaign: str) -> dict[str, str]:
    """Official share endpoints (t.me/share/url?url&text, vk.com/share.php?url&title, wa.me/?text)."""
    text = share_text(company, hook)
    link = lambda src: with_utm(article_url, src, "share", campaign)  # noqa: E731
    return {
        "telegram": f"https://t.me/share/url?url={_q(link('telegram'))}&text={_q(text)}",
        "vk": f"https://vk.com/share.php?url={_q(link('vk'))}&title={_q(text)}",
        "whatsapp": f"https://wa.me/?text={_q(text + chr(10) + link('whatsapp'))}",
    }


def friend_share_url(post_url: str, company: str, hook: str) -> str:
    """'Отправить другу': Telegram share of the channel post itself."""
    return f"https://t.me/share/url?url={_q(post_url)}&text={_q(share_text(company, hook))}"


def _a(href: str, label: str) -> dict:
    return {"tag": "a", "attrs": {"href": href}, "children": [label]}


def subscribe_nodes(href: str) -> list:
    """The approved text, no horizontal rule. The personal invite link is the channel name inside the phrase (no extra
    line). Only elements the Telegraph API supports (p, a)."""
    before, after = SUBSCRIBE_LINES[1].split(LINKED_PHRASE)
    return [{"tag": "p", "children": [SUBSCRIBE_LINES[0]]},
            {"tag": "p", "children": [before, _a(href, LINKED_PHRASE), after]}]


def share_nodes(urls: dict[str, str]) -> list:
    return [{"tag": "h4", "children": [SHARE_TITLE]},
            {"tag": "p", "children": [_a(urls["telegram"], "Telegram"), " · ", _a(urls["vk"], "VK"), " · ",
                                      _a(urls["whatsapp"], "WhatsApp")]}]


def _text(node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_text(c) for c in node.get("children", [])) if isinstance(node, dict) else ""


def has_share_block(nodes: list) -> bool:
    return any(isinstance(n, dict) and n.get("tag") == "h4" and _text(n).strip() == SHARE_TITLE for n in nodes)


def next_part_url(nodes: list) -> str | None:
    """The 'Читать дальше' link that chains long issues, if the page has one."""
    for n in reversed(nodes):
        if isinstance(n, dict) and n.get("tag") == "p" and _text(n).startswith("Читать дальше"):
            for c in n.get("children", []):
                if isinstance(c, dict) and c.get("tag") == "a":
                    return c["attrs"]["href"]
    return None
