"""Pictures for «Разгадка бизнеса» that do not come from the company's own pages.

- Schemes drawn by code from the verified data the model returned (mechanism chain, timeline, CIS analogue cards,
  verdict card). Pillow + DejaVu, the same way chart.py draws charts. Nothing is invented here: only text from the issue.
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


def render_cards(title: str, cards: list[dict]) -> bytes | None:
    """Cards: {'title': ..., 'tag': ..., 'lines': [...]}, two per row."""
    cards = [c for c in cards or [] if isinstance(c, dict) and c.get("title")][:6]
    fonts = _fonts()
    if not cards or not fonts:
        return None
    card_w, card_h, gap, top = (W - 140 - 40) // 2, 330, 40, 150
    rows = -(-len(cards) // 2)
    img, d = _canvas(top + rows * (card_h + gap) + 10, title, fonts)
    for i, c in enumerate(cards):
        x = 70 + (i % 2) * (card_w + gap)
        y = top + (i // 2) * (card_h + gap)
        d.rounded_rectangle([x, y, x + card_w, y + card_h], radius=22, fill="#ffffff", outline=LINE, width=3)
        d.text((x + 30, y + 26), str(c["title"])[:34], font=fonts["head"], fill=INK)
        if c.get("tag"):
            d.text((x + 30, y + 70), str(c["tag"])[:48], font=fonts["small"], fill=ACCENT)
        yy = y + 110
        for line in c.get("lines") or []:
            for part in _wrap(line, 46, 2):
                if yy > y + card_h - 40:
                    break
                d.text((x + 30, yy), part, font=fonts["small"], fill=INK2)
                yy += 32
            yy += 6
    return _png(img)


def render_verdict(cis: dict) -> bytes | None:
    verdict = str(cis.get("verdict") or "").strip()
    fonts = _fonts()
    if not verdict or not fonts:
        return None
    lines = [("Главные аргументы", cis.get("arguments") or []), ("Главный риск", [cis.get("main_risk") or ""]),
             ("Модель адаптации", [cis.get("adaptation") or ""])]
    img, d = _canvas(960, "Можно ли повторить в СНГ: вердикт", fonts)
    d.rounded_rectangle([70, 140, W - 70, 250], radius=22, fill=ACCENT_BG, outline=ACCENT, width=3)
    d.text((110, 195), verdict, font=fonts["title"], fill=ACCENT, anchor="lm")
    if cis.get("confidence"):
        d.text((W - 110, 195), f"уверенность: {cis['confidence']}", font=fonts["body"], fill=INK2, anchor="rm")
    y = 290
    for head, items in lines:
        d.text((70, y), head, font=fonts["head"], fill=INK)
        y += 46
        for item in [i for i in items if i][:3]:
            for k, part in enumerate(_wrap(item, 92, 2)):
                d.text((100, y), ("• " if k == 0 else "  ") + part, font=fonts["body"], fill=INK2)
                y += 36
        y += 22
        if y > 900:
            break
    return _png(img.crop((0, 0, W, min(960, y + 20))))


def analog_cards(cis: dict) -> list[dict]:
    out = []
    kinds = {"direct": "прямой конкурент", "functional": "функциональная альтернатива", "partial": "частично похожая модель"}
    for a in (cis or {}).get("analogs") or []:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        tag = " · ".join(x for x in (a.get("country"), kinds.get(str(a.get("kind")), "")) if x)
        out.append({"title": a["name"], "tag": tag,
                    "lines": [f"Что: {a.get('product', '')}", f"Сходство: {a.get('similarity', '')}",
                              f"Отличие: {a.get('difference', '')}"]})
    return out


def mvp_cards(cis: dict) -> list[dict]:
    heads = ["Аудитория", "Минимальный продукт", "Где клиенты", "Проверка оплаты", "Метрики успеха", "Когда отказаться"]
    steps = [str(x) for x in (cis or {}).get("mvp") or [] if x][:6]
    return [{"title": f"{i + 1}. {heads[i] if i < len(heads) else 'Шаг'}", "lines": [s]} for i, s in enumerate(steps)]


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


def render_preview(template: bytes, name: str, sector: str, logo: bytes | None = None) -> bytes | None:
    """The rubric picture (1080x1080) with the company logo, name and sector in its free bottom band.
    Returns JPEG bytes, or None when Pillow or a Cyrillic font is missing (the plain rubric picture is used then)."""
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
        img = Image.open(io.BytesIO(template)).convert("RGB").resize((1080, 1080))
    except Exception:  # noqa: BLE001
        return None
    d = ImageDraw.Draw(img)
    name = " ".join(str(name or "").split()).upper()[:28]
    sector = " ".join(str(sector or "").split())[:46]
    if not name or not _bold(10):
        return None
    x, top, band = 80, 930, 120
    if logo:
        try:
            mark = Image.open(io.BytesIO(logo)).convert("RGBA")
            mark.thumbnail((band - 24, band - 24), Image.LANCZOS)
            d.rounded_rectangle([x, top, x + band, top + band], radius=22, fill="#ffffff")
            img.paste(mark, (x + (band - mark.width) // 2, top + (band - mark.height) // 2), mark)
            x += band + 28
        except Exception:  # noqa: BLE001 - a broken logo file: name and sector alone
            pass
    size = 52
    while size > 30 and d.textlength(name, font=_bold(size)) > 1000 - x:
        size -= 2
    d.text((x, top + 6), name, font=_bold(size), fill=GOLD)
    d.text((x, top + 6 + size + 14), sector, font=_bold(30), fill=PREVIEW_INK)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


_ICON_RE = re.compile(r'<link\b[^>]*rel=["\'][^"\']*(?:apple-touch-icon|icon)[^"\']*["\'][^>]*>', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_SIZE_RE = re.compile(r'sizes=["\'](\d+)x', re.I)


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
    candidates = [u for _, u in urls] + [urljoin(site_url, "/favicon.ico")]
    for url in candidates[:4]:
        try:
            data, ctype = http.get_bytes(url, max_bytes=1_000_000, timeout=20)
            if len(data) > 400 and ctype in ("image/png", "image/jpeg", "image/x-icon", "image/vnd.microsoft.icon",
                                             "image/webp", "application/octet-stream"):
                from PIL import Image  # noqa: PLC0415
                Image.open(io.BytesIO(data)).verify()
                return data
        except Exception:  # noqa: BLE001
            continue
    return None
