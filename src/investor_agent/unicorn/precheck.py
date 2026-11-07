"""Check right before publication that the pictures and videos of a prepared issue still exist (docs/RULES.md §5).

The issue is prepared hours earlier. A deleted Cloudinary file or a video taken down meanwhile would leave a broken
picture in the article, so dead items are dropped here. Network errors never drop anything (unknown = keep).
No DeepSeek calls.
"""

import logging

from ..http import Http, HttpError
from ..media import oembed_info

log = logging.getLogger(__name__)

DEAD = {404, 410}


def image_alive(http: Http, url: str) -> bool:
    head = getattr(http, "head", None)
    if not head or not url:
        return True
    try:
        return head(url) not in DEAD
    except Exception:  # noqa: BLE001 - a network error is not proof that the file is gone
        return True


def video_alive(http: Http, canonical: str) -> bool:
    try:
        return oembed_info(http, canonical) is not None
    except (HttpError, OSError):
        return True


def prune_dead_media(report: dict, http: Http) -> dict[str, int]:
    """Remove dead photos, videos and chart images from the report in place; returns what was removed."""
    removed = {"photos": 0, "videos": 0, "charts": 0}
    kept = []
    for m in report.get("media_resolved") or []:
        if m.get("kind") == "photo" and not image_alive(http, m.get("src", "")):
            removed["photos"] += 1
        elif m.get("kind") == "video" and m.get("video_url") and not video_alive(http, m["video_url"]):
            removed["videos"] += 1
        else:
            kept.append(m)
    if "media_resolved" in report:
        report["media_resolved"] = kept
    images = report.get("chart_images") or []
    for i, url in enumerate(images):
        if url and not image_alive(http, url):
            images[i] = None  # the page then shows the chart as a table
            removed["charts"] += 1
    if any(removed.values()):
        log.warning("pre-publication check dropped dead media: %s", removed)
    return removed
