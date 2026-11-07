"""Line chart of the company's key metric as a PNG, drawn with Pillow. Returns None when it cannot be drawn
(no Pillow, no Cyrillic font, fewer than 3 numeric points); the page then shows a table instead."""

import io
import logging

log = logging.getLogger(__name__)

FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "DejaVuSans.ttf"]
SURFACE, INK, INK2, GRID, BLUE = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2de", "#2a78d6"


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _fmt(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def render_chart(points: list[dict], unit: str = "") -> bytes | None:
    pts = [(str(p.get("label", "")), _number(p.get("value"))) for p in points or []]
    pts = [(label, v) for label, v in pts if v is not None]
    if len(pts) < 3:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
    except ImportError:
        return None
    font = None
    for path in FONT_PATHS:
        try:
            font = (ImageFont.truetype(path, 22), ImageFont.truetype(path, 20), ImageFont.truetype(path, 24))
            break
        except OSError:
            continue
    if not font:
        log.warning("no TrueType font with Cyrillic found; chart skipped")
        return None
    small, mid, bold = font
    w, h, left, right, top, bottom = 1600, 900, 150, 70, 90, 110
    img = Image.new("RGB", (w, h), SURFACE)
    d = ImageDraw.Draw(img)
    top_val = max(v for _, v in pts)
    step = next(s * 10 ** e for e in range(-3, 12) for s in (1, 2, 2.5, 5) if top_val / (s * 10 ** e) <= 4)
    ymax = step * 4
    plot_w, plot_h = w - left - right, h - top - bottom

    def xy(i, v):
        return left + plot_w * (i / (len(pts) - 1)), top + plot_h * (1 - v / ymax)
    if unit:
        d.text((left, 28), unit, font=mid, fill=INK2)
    for k in range(5):
        y = top + plot_h * (1 - k / 4)
        d.line([(left, y), (w - right, y)], fill=GRID, width=2)
        d.text((left - 18, y), _fmt(step * k), font=small, fill=INK2, anchor="rm")
    line = [xy(i, v) for i, (_, v) in enumerate(pts)]
    d.line(line, fill=BLUE, width=5, joint="curve")
    for i, ((label, v), (x, y)) in enumerate(zip(pts, line)):
        d.ellipse([x - 11, y - 11, x + 11, y + 11], fill=BLUE, outline=SURFACE, width=4)
        d.text((x, h - bottom + 30), label[:10], font=small, fill=INK2, anchor="mm")
        if i in (0, len(pts) - 1) or v in (max(p[1] for p in pts), min(p[1] for p in pts)):
            d.text((x, y - 24), _fmt(v), font=bold, fill=INK, anchor="mb" if i < len(pts) - 1 else "rb")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
