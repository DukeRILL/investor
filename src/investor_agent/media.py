"""Photos and videos for the Unicorn School page.

The model only nominates media: a source id, a kind and a Russian caption. This module finds the real picture
(og:image of the cited page, or an image URL that literally occurs in the page text), downloads it, checks that it
really is an image, and uploads it to Telegraph. Videos are accepted only when the URL is YouTube or Vimeo.
Nothing here calls DeepSeek.
"""

import html as html_lib
import json
import logging
import re
from typing import Callable
from urllib.parse import parse_qs, quote, urljoin, urlparse

from .http import Http, HttpError
from .sources import SourceRegistry

log = logging.getLogger(__name__)

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}  # Telegraph accepts nothing else
MIN_IMAGE_BYTES = 8_000  # smaller files are icons and tracking pixels
MAX_ITEMS = 10
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_PROP_RE = re.compile(r'(?:property|name)\s*=\s*["\'](og:image(?::secure_url)?|twitter:image(?::src)?)["\']', re.I)
_CONTENT_RE = re.compile(r'content\s*=\s*["\']([^"\']+)["\']', re.I)
_YT_ID = re.compile(r"^[\w-]{11}$")


def video_embed(url: str) -> tuple[str, str] | None:
    """(canonical URL, Telegraph embed path) for YouTube and Vimeo links, else None."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        vid = parsed.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        vid = (parse_qs(parsed.query).get("v") or [""])[0]
        if not vid and parsed.path.startswith(("/embed/", "/shorts/", "/live/")):
            vid = parsed.path.split("/")[2] if parsed.path.count("/") >= 2 else ""
    elif host in ("vimeo.com", "player.vimeo.com"):
        vid = next((part for part in parsed.path.split("/") if part.isdigit()), "")
        if vid:
            canonical = f"https://vimeo.com/{vid}"
            return canonical, "/embed/vimeo?url=" + quote(canonical, safe="")
        return None
    else:
        return None
    if not _YT_ID.match(vid or ""):
        return None
    canonical = f"https://www.youtube.com/watch?v={vid}"
    return canonical, "/embed/youtube?url=" + quote(canonical, safe="")


def oembed_info(http: Http, canonical: str) -> dict | None:
    """Ask YouTube/Vimeo whether the video exists. Returns {'title', 'author'} or None (private, deleted, unknown)."""
    base = ("https://vimeo.com/api/oembed.json?url=" if "vimeo.com" in canonical
            else "https://www.youtube.com/oembed?format=json&url=")
    try:
        data, _ = http.get_bytes(base + quote(canonical, safe=""), max_bytes=100_000, timeout=20)
        info = json.loads(data.decode("utf-8", "ignore"))
    except (HttpError, OSError, ValueError):
        return None
    if not isinstance(info, dict) or not info.get("title"):
        return None
    return {"title": str(info["title"]), "author": str(info.get("author_name") or "")}


def find_page_image(page_html: str, base_url: str) -> str | None:
    """og:image / twitter:image of an HTML page, as an absolute URL."""
    for tag in _META_RE.findall(page_html or ""):
        if _PROP_RE.search(tag):
            m = _CONTENT_RE.search(tag)
            if m:
                return urljoin(base_url, html_lib.unescape(m.group(1)).strip())
    return None


def _download_image(http: Http, url: str) -> tuple[bytes, str] | None:
    try:
        data, ctype = http.get_bytes(url, max_bytes=5_000_000, timeout=30)
    except (HttpError, OSError) as exc:
        log.info("image download failed: %s", exc)
        return None
    if ctype not in IMAGE_TYPES or len(data) < MIN_IMAGE_BYTES:
        return None
    return data, ctype


def _image_for(item: dict, src, http: Http, upload: Callable[[bytes, str], str], used: set[str]) -> str | None:
    candidates: list[str] = []
    wanted = str(item.get("image_url") or "").strip()
    if wanted and wanted in (src.content or ""):  # only URLs that really occur in the page the model read
        candidates.append(wanted)
    try:
        page, ctype = http.get_bytes(src.url, max_bytes=1_500_000, timeout=30)
        if "html" in ctype:
            found = find_page_image(page.decode("utf-8", "ignore"), src.url)
            if found and found not in candidates:
                candidates.append(found)
    except (HttpError, OSError) as exc:
        log.info("page fetch for og:image failed: %s", exc)
    for url in candidates:
        if url in used:  # the same picture is already on the page
            continue
        used.add(url)
        image = _download_image(http, url)
        if not image:
            continue
        try:
            return upload(*image)
        except Exception as exc:  # noqa: BLE001 - fall back to hot-linking the original
            log.warning("telegraph upload failed (%s); using the original URL", type(exc).__name__)
            return url
    return None


def resolve_media(items: list[dict], registry: SourceRegistry, http: Http,
                  upload: Callable[[bytes, str], str], max_items: int = MAX_ITEMS) -> list[dict]:
    """Turn the model's nominations into media that can be placed on the page. Unusable items are dropped."""
    out: list[dict] = []
    seen: set[str] = set()
    used_images: set[str] = set()
    for item in (items or [])[:max_items + 4]:
        if not isinstance(item, dict) or len(out) >= max_items:
            continue
        src = registry.get(str(item.get("source", "")))
        if not src:
            continue
        base = {"caption": str(item.get("caption") or src.title or "").strip(), "section": item.get("section"),
                "role": item.get("role"), "source_id": src.id, "source_url": src.url, "publisher": src.publisher}
        kind = str(item.get("kind") or "").lower()
        video = None
        for url in (src.url, str(item.get("video_url") or "") if str(item.get("video_url") or "") in (src.content or "") else ""):
            video = video_embed(url) if url else None
            if video:
                break
        if video and kind in ("video", "page", ""):
            info = oembed_info(http, video[0]) if video[0] not in seen else None
            if info:  # the video really exists; a private or deleted one is dropped
                seen.add(video[0])
                out.append({**base, "kind": "video", "video_url": video[0], "embed": video[1],
                            "caption": str(item.get("caption") or "").strip() or info["title"], "video_title": info["title"],
                            "publisher": info["author"] or base["publisher"]})
            continue
        if kind == "photo":
            try:
                image = _image_for(item, src, http, upload, used_images)
            except Exception as exc:  # noqa: BLE001 - one bad item must not break the issue
                log.warning("media item %s failed: %s", src.id, type(exc).__name__)
                image = None
            if image and image not in seen:
                seen.add(image)
                out.append({**base, "kind": "photo", "src": image})
            continue
        if src.url not in seen:
            seen.add(src.url)
            out.append({**base, "kind": "page"})
    return out
