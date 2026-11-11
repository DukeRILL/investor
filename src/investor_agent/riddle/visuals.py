"""Pictures for «Разгадка бизнеса» that do not come from the company's own pages.

- Schemes drawn by code from the verified data the model returned (mechanism chain, timeline). Pillow + DejaVu, the same way chart.py draws charts. Nothing is invented here: only text from the issue.
- Pixabay stock photos (PIXABAY_API_KEY), downloaded and re-hosted (Pixabay forbids hot-linking).
- Cloudflare Workers AI FLUX illustrations (CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN), last resort, marked as AI.
Every function returns None instead of raising: a missing picture only makes the issue plainer (and maybe BLOCKED).
"""

import base64
import io
import logging
import re
import textwrap

from ..chart import FONT_PATHS
from ..http import Http, HttpError

log = logging.getLogger(__name__)

SURFACE, INK, INK2, LINE, ACCENT, ACCENT_BG = "#fcfcfb", "#0b0b0b", "#52514e", "#d9d8d4", "#2a78d6", "#e8f1fb"
W = 1600
PIXABAY = "https://pixabay.com/api/"
FLUX = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/black-forest-labs/flux-1-schnell"
AI_STYLE = (" Editorial concept illustration, clean modern style, soft light, no people faces, no text, no letters, "
            "no logos, no user interface.")


def _fonts():
    try:
        from PIL import ImageFont  # noqa: PLC0415
    except ImportError:
        return None
    for path in FONT_PATHS:
        try:
            return {"title": ImageFont.truetype(path, 46), "head": ImageFont.truetype(path, 30),
                    "body": ImageFont.truetype(path, 26), "small": ImageFont.truetype(path, 22)}
        except OSError:
            continue
    log.warning("no TrueType font with Cyrillic found; scheme skipped")
    return None


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _wrap(text: str, width: int, lines: int) -> list[str]:
    out = textwrap.wrap(" ".join(str(text or "").split()), width=width) or [""]
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1].rstrip(" ,.;:") + "…"
    return out


def _canvas(height: int, title: str, fonts):
    from PIL import Image, ImageDraw  # noqa: PLC0415
    img = Image.new("RGB", (W, height), SURFACE)
    d = ImageDraw.Draw(img)
    d.text((70, 50), title, font=fonts["title"], fill=INK)
    return img, d


def render_chain(steps: list[dict], title: str = "Разгадка: как устроен успех") -> bytes | None:
    """Six boxes connected by arrows: need → solution → why chosen → revenue → growth → advantage."""
    steps = [s for s in steps or [] if isinstance(s, dict) and (s.get("text") or s.get("step"))][:6]
    fonts = _fonts()
    if len(steps) < 4 or not fonts:
        return None
    box_h, gap, top = 150, 46, 150
    img, d = _canvas(top + len(steps) * (box_h + gap) + 30, title, fonts)
    for i, s in enumerate(steps):
        y = top + i * (box_h + gap)
        d.rounded_rectangle([70, y, W - 70, y + box_h], radius=22, fill=ACCENT_BG, outline=ACCENT, width=3)
        d.text((110, y + 24), f"{i + 1}. {s.get('step', '')}"[:60], font=fonts["head"], fill=ACCENT)
        for k, line in enumerate(_wrap(s.get("text", ""), 82, 2)):
            d.text((110, y + 70 + k * 34), line, font=fonts["body"], fill=INK)
        if i < len(steps) - 1:
            x, y2 = W // 2, y + box_h
            d.line([(x, y2 + 6), (x, y2 + gap - 8)], fill=ACCENT, width=5)
            d.polygon([(x - 14, y2 + gap - 20), (x + 14, y2 + gap - 20), (x, y2 + gap - 4)], fill=ACCENT)
    return _png(img)


def render_timeline(events: list[dict], title: str = "Хронология") -> bytes | None:
    events = [e for e in events or [] if isinstance(e, dict) and e.get("date") and e.get("text")][:9]
    fonts = _fonts()
    if len(events) < 3 or not fonts:
        return None
    row, top = 120, 150
    img, d = _canvas(top + len(events) * row + 20, title, fonts)
    x = 300
    d.line([(x, top + 10), (x, top + len(events) * row - 40)], fill=LINE, width=6)
    for i, e in enumerate(events):
        y = top + i * row
        d.ellipse([x - 16, y + 4, x + 16, y + 36], fill=ACCENT, outline=SURFACE, width=4)
        d.text((x - 40, y + 20), str(e["date"])[:12], font=fonts["head"], fill=INK, anchor="rm")
        for k, line in enumerate(_wrap(e["text"], 66, 2)):
            d.text((x + 50, y + 4 + k * 36), line, font=fonts["body"], fill=INK if k == 0 else INK2)
    return _png(img)


# --- external images -------------------------------------------------------------------------

def pixabay_image(http: Http, key: str, query: str, used: set[str]) -> tuple[bytes, str, dict] | None:
    """First unused Pixabay photo for the query: (bytes, content type, info for the media manifest)."""
    if not key or not query:
        return None
    try:
        data = http.request("GET", PIXABAY, params={"key": key, "q": query[:100], "image_type": "photo",
                                                     "orientation": "horizontal", "safesearch": "true", "per_page": 10},
                            timeout=30)
    except (HttpError, OSError) as exc:
        log.info("pixabay search failed: %s", type(exc).__name__)  # never log the URL: it carries the key
        return None
    hits = data.get("hits") if isinstance(data, dict) else None
    for hit in hits or []:
        url = hit.get("largeImageURL") or hit.get("webformatURL")
        if not url or url in used:
            continue
        used.add(url)
        try:
            body, ctype = http.get_bytes(url, max_bytes=5_000_000, timeout=30)
        except (HttpError, OSError):
            continue
        if ctype in ("image/jpeg", "image/png") and len(body) > 8_000:
            return body, ctype, {"page": hit.get("pageURL") or "https://pixabay.com", "author": hit.get("user") or ""}
    return None


def flux_image(http: Http, account: str, token: str, prompt: str) -> bytes | None:
    """One FLUX-1 schnell picture from Cloudflare Workers AI, JPEG bytes."""
    if not (account and token and prompt):
        return None
    try:
        data = http.request("POST", FLUX.format(account=account), json_body={"prompt": prompt[:1500] + AI_STYLE, "steps": 6},
                            headers={"Authorization": f"Bearer {token}"}, timeout=90)
        raw = base64.b64decode(((data or {}).get("result") or {}).get("image") or "")
    except (HttpError, OSError, ValueError, AttributeError) as exc:
        log.info("flux generation failed: %s", type(exc).__name__)
        return None
    return raw if len(raw) > 8_000 else None


# --- post preview: rubric template + company logo, name and sector ----------------------------

BOLD_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "DejaVuSans-Bold.ttf"]
GOLD, PREVIEW_INK, PILL = "#ffe08a", "#f7f9fc", "#0f2d57"


def _bold(size: int):
    from PIL import ImageFont  # noqa: PLC0415
    for path in BOLD_PATHS + FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


# Fields of the owner's preview template (preview.jpg, 1199x1312, 2026-11-09): everything is drawn INSIDE them.
LOGO_BOX = (106, 543, 276, 702)         # white rounded square for the logo
NAME_BOX = (352, 558, 1102, 690)        # free area right of the separator inside the gold frame (margins keep off the lines)
SECTOR_BOX = (98, 1193, 372, 1227)      # dark field under «СЕКТОР:» (bottom left), margins keep off the yellow edge
NAME_INK, SECTOR_INK = "#ffffff", "#ffd414"


def _fit_lines(d, text: str, box: tuple, sizes: range, max_lines: int, font_for):
    """Biggest font size (descending `sizes`) at which `text` wraps into <= max_lines lines inside the box.
    Returns (font, lines), or the smallest size with the last line truncated with an ellipsis: never leaves the box."""
    w, h = box[2] - box[0], box[3] - box[1]
    words = text.split()
    for size in sizes:
        font = font_for(size)
        lines: list[str] = []
        cur = ""
        for word in words:
            trial = (cur + " " + word).strip()
            if d.textlength(trial, font=font) <= w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
        line_h = size * 1.15
        if len(lines) <= max_lines and len(lines) * line_h <= h and all(d.textlength(x, font=font) <= w for x in lines):
            return font, lines
    font = font_for(sizes[-1])
    line = " ".join(words)
    while line and d.textlength(line + "…", font=font) > w:
        line = line[:-1]
    return font, [line.rstrip() + "…"]


def render_preview(template: bytes, name: str, sector: str, logo: bytes | None = None) -> bytes | None:
    """The owner's template with the company's logo (white square), name (right of the logo) and sector (under «СЕКТОР:»).
    Text is fitted automatically to the fields and never leaves them. Returns JPEG bytes, or None when Pillow or a
    Cyrillic font is missing / the template is broken (the plain rubric picture is used then)."""
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
        img = Image.open(io.BytesIO(template)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    if img.size != (1199, 1312) or not _bold(10):
        return None
    d = ImageDraw.Draw(img)
    name = " ".join(str(name or "").split())
    sector = " ".join(str(sector or "").split())
    if not name:
        return None
    if logo:
        try:
            mark = Image.open(io.BytesIO(logo)).convert("RGBA")
            pad = 16
            box_w, box_h = LOGO_BOX[2] - LOGO_BOX[0] - 2 * pad, LOGO_BOX[3] - LOGO_BOX[1] - 2 * pad
            if max(mark.size) < min(box_w, box_h):  # small icon: enlarge to fill the square
                k = min(box_w / mark.width, box_h / mark.height)
                mark = mark.resize((int(mark.width * k), int(mark.height * k)), Image.LANCZOS)
            mark.thumbnail((LOGO_BOX[2] - LOGO_BOX[0] - 2 * pad, LOGO_BOX[3] - LOGO_BOX[1] - 2 * pad), Image.LANCZOS)
            cx, cy = (LOGO_BOX[0] + LOGO_BOX[2]) // 2, (LOGO_BOX[1] + LOGO_BOX[3]) // 2
            img.paste(mark, (cx - mark.width // 2, cy - mark.height // 2), mark)
        except Exception:  # noqa: BLE001 - a broken logo file: the white square stays empty, name and sector still drawn
            pass
    font, lines = _fit_lines(d, name.upper(), NAME_BOX, range(86, 27, -2), 2, _bold)
    line_h = font.size * 1.15
    y = (NAME_BOX[1] + NAME_BOX[3]) / 2 - line_h * (len(lines) - 1) / 2
    cx = (NAME_BOX[0] + NAME_BOX[2]) / 2
    for line in lines:
        d.text((cx, y), line, font=font, fill=NAME_INK, anchor="mm")   # centred in the field
        y += line_h
    if sector:
        font, lines = _fit_lines(d, sector, SECTOR_BOX, range(30, 15, -1), 1, _bold)
        d.text(((SECTOR_BOX[0] + SECTOR_BOX[2]) / 2, (SECTOR_BOX[1] + SECTOR_BOX[3]) / 2), lines[0], font=font, fill=SECTOR_INK,
               anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


_ICON_RE = re.compile(r'<link\b[^>]*rel=["\'][^"\']*(?:apple-touch-icon|icon)[^"\']*["\'][^>]*>', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_SIZE_RE = re.compile(r'sizes=["\'](\d+)x', re.I)


MIN_LOGO_PX = 64


def host_matches_company(host: str, company: str) -> bool:
    """True when the site's host belongs to the company (its Latin name is part of a host label): a logo taken from any
    other site (a news page, a registry) is somebody else's icon."""
    slug = re.sub(r"[^a-z0-9]", "", str(company or "").lower())
    labels = [re.sub(r"[^a-z0-9]", "", x) for x in str(host or "").lower().removeprefix("www.").split(".")[:-1]]
    return len(slug) >= 2 and any(slug in x or (len(x) >= 3 and x in slug) for x in labels)


def logo_big_enough(data: bytes) -> bool:
    try:
        from PIL import Image  # noqa: PLC0415
        w, h = Image.open(io.BytesIO(data)).size
        return min(w, h) >= MIN_LOGO_PX
    except Exception:  # noqa: BLE001
        return False


def find_logo(http: Http, site_url: str) -> bytes | None:
    """The company's own icon from its site: the largest apple-touch-icon / icon link, then /favicon.ico.
    PNG/JPEG/ICO only (SVG is skipped). None when nothing usable is found."""
    from urllib.parse import urljoin  # noqa: PLC0415
    if not site_url:
        return None
    urls: list[tuple[int, str]] = []
    try:
        page, ctype = http.get_bytes(site_url, max_bytes=1_500_000, timeout=30)
        if "html" in ctype:
            for tag in _ICON_RE.findall(page.decode("utf-8", "ignore")):
                href = _HREF_RE.search(tag)
                size = _SIZE_RE.search(tag)
                if href and not href.group(1).lower().split("?")[0].endswith(".svg"):
                    bonus = 1000 if "apple-touch" in tag.lower() else 0
                    urls.append((int(size.group(1)) + bonus if size else bonus, urljoin(site_url, href.group(1))))
    except (HttpError, OSError):
        pass
    urls.sort(reverse=True)
    from urllib.parse import urlparse  # noqa: PLC0415
    host = (urlparse(site_url).hostname or "").removeprefix("www.")
    candidates = [u for _, u in urls][:3] + [urljoin(site_url, "/favicon.ico")]
    if host:  # the site itself may be slow or blocked from the server: public favicon services by domain
        candidates += [f"https://www.google.com/s2/favicons?domain={host}&sz=256",
                       f"https://icons.duckduckgo.com/ip3/{host}.ico"]
    for url in candidates[:6]:
        try:
            data, ctype = http.get_bytes(url, max_bytes=1_000_000, timeout=20)
            if len(data) > 400 and ctype in ("image/png", "image/jpeg", "image/x-icon", "image/vnd.microsoft.icon",
                                             "image/webp", "application/octet-stream"):
                from PIL import Image  # noqa: PLC0415
                Image.open(io.BytesIO(data)).verify()
                if not logo_big_enough(data):
                    continue
                return data
        except Exception:  # noqa: BLE001
            continue
    return None
