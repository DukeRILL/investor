"""Subscribe block, share links and the per-article hook. Pure functions: no network, no state."""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

SUBSCRIBE_TITLE = "Завтра — новая история большого бизнеса"
SUBSCRIBE_TEXT = ("Каждый день разбираем реальные компании: с чего начинали, как находили точки роста, "
                  "на чём зарабатывали и какие решения привели к успеху.")
SUBSCRIBE_LABEL = "→ Подписаться на «ШКОЛУ ЕДИНОРОГОВ»"
SHARE_TITLE = "Хорошими идеями стоит делиться"
SHARE_TEXT = ("Опубликуйте этот разбор в своих соцсетях или отправьте знакомым. "
              "Возможно, именно эта история вдохновит кого-то создать собственный бизнес.")
SHARE_LABEL = "Поделиться статьёй:"
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
    """'Поделиться в TG': Telegram share of the channel post itself."""
    return f"https://t.me/share/url?url={_q(post_url)}&text={_q(share_text(company, hook))}"


def _a(href: str, label: str) -> dict:
    return {"tag": "a", "attrs": {"href": href}, "children": [label]}


def subscribe_nodes(href: str) -> list:
    """Footer part 1 (under the banner): heading, short text, one bold link with the article's personal invite link
    (it opens the channel). Only elements the Telegraph API supports (h4, p, b, a)."""
    return [{"tag": "h4", "children": [SUBSCRIBE_TITLE]},
            {"tag": "p", "children": [SUBSCRIBE_TEXT]},
            {"tag": "p", "children": [{"tag": "b", "children": [_a(href, SUBSCRIBE_LABEL)]}]}]


def share_nodes(urls: dict[str, str]) -> list:
    """Footer part 2: heading, short text and three separate share links of THIS article (not of the channel)."""
    return [{"tag": "h4", "children": [SHARE_TITLE]},
            {"tag": "p", "children": [SHARE_TEXT]},
            {"tag": "p", "children": [{"tag": "b", "children": [SHARE_LABEL]}, {"tag": "br"},
                                      _a(urls["telegram"], "Telegram"), " · ", _a(urls["vk"], "VK"), " · ",
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
