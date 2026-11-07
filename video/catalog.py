"""Статья Telegraph -> текст и каталог оригинальных изображений (подпись, контекст, источник, лица, приоритет)."""
import hashlib, io, json, os, re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image

FACE_MODEL = os.environ.get("FACE_MODEL", "/app/models/face_detection_yunet_2023mar.onnx")
MIN_SIDE = 400  # меньше этого снимок не растянуть до 1080x1920 без мусора
FOUNDER_RE = re.compile(r"основател|сооснователь|founder|\bceo\b|гендиректор", re.I)
CHART_RE = re.compile(r"график|диаграмм|по раундам|динамик", re.I)
PRODUCT_RE = re.compile(r"слайд|продукт|интерфейс|платформ|скриншот|приложени|картинка со страницы", re.I)
OFFICE_RE = re.compile(r"\bофис", re.I)
NAME_RE = re.compile(r"[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+")


def page_path(url: str) -> str:
    m = re.match(r"^https://telegra\.ph/([A-Za-z0-9%_\-]+)$", url.strip())
    if not m:
        raise ValueError("нужен URL вида https://telegra.ph/…")
    return m.group(1)


def fetch_page(url: str) -> dict:
    r = httpx.get(f"https://api.telegra.ph/getPage/{page_path(url)}", params={"return_content": "true"}, timeout=30)
    d = r.json()
    if not d.get("ok"):
        raise ValueError(f"Telegraph: {d.get('error')}")
    return d["result"]


def node_text(n) -> str:
    if isinstance(n, str):
        return n
    return "".join(node_text(c) for c in n.get("children", []))


def node_links(n) -> list[str]:
    if isinstance(n, str):
        return []
    out = [n["attrs"]["href"]] if n.get("tag") == "a" and n.get("attrs", {}).get("href") else []
    for c in n.get("children", []):
        out += node_links(c)
    return out


def clean_url(u: str) -> str:
    """Источник без рекламных меток."""
    p = urlsplit(u)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def parse_page(page: dict) -> tuple[str, str, list[dict]]:
    """Возвращает заголовок, текст и фигуры: всё, что удаётся определить без скачивания."""
    blocks: list[str] = []
    figures: list[dict] = []

    def walk(n):
        if isinstance(n, str):
            if n.strip():
                blocks.append(n.strip())
            return
        if n.get("tag") in {"img", "figure"}:
            imgs = [n] if n["tag"] == "img" else [c for c in n.get("children", []) if isinstance(c, dict) and c.get("tag") == "img"]
            cap = next((c for c in n.get("children", []) if isinstance(c, dict) and c.get("tag") == "figcaption"), None)
            for im in imgs:
                src = im.get("attrs", {}).get("src", "")
                if src.startswith("/"):
                    src = "https://telegra.ph" + src
                links = node_links(cap) if cap else []
                figures.append({"src": src, "caption": node_text(cap).strip() if cap else "", "source_url": clean_url(links[0]) if links else None,
                                "context_before": blocks[-1][:300] if blocks else ""})
            if imgs:
                return
        if n.get("tag") in {"p", "h3", "h4", "li", "blockquote"}:
            t = node_text(n).strip()
            if t:
                blocks.append(t)
            return
        for c in n.get("children", []):
            walk(c)

    for n in page["content"]:
        walk(n)
    for i, f in enumerate(figures):
        f["id"] = f"img{i + 1:02d}"
    return page["title"], "\n".join(blocks), figures


def detect_faces(img: Image.Image) -> list[list[int]]:
    """Рамки лиц [x, y, w, h] в пикселях оригинала, нейросеть YuNet (картины и логотипы лицами не считает)."""
    import cv2
    import numpy as np
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (w, h), score_threshold=0.75, nms_threshold=0.3)
    _, found = det.detect(bgr)
    if found is None:
        return []
    return sorted([int(v) for v in f[:4]] for f in found if f[2] >= min(h, w) * 0.05)


def classify(fig: dict, founder_names: set[str]) -> tuple[str, int]:
    """Вид и приоритет по правилу владельца: 1 основатели, 2 продукт/офис, 3 всё остальное."""
    cap, faces = fig["caption"], fig.get("faces") or []
    if CHART_RE.search(cap):
        return "chart", 3
    if faces and not cap.lower().startswith("слайд") and (FOUNDER_RE.search(cap) or any(n in cap for n in founder_names)):
        return "founder", 1
    if PRODUCT_RE.search(cap):
        return "product", 2
    if OFFICE_RE.search(cap):
        return "office", 2
    return "other", 3


def build_catalog(figures: list[dict], outdir: Path, fetch=None) -> list[dict]:
    """Скачивает оригиналы как есть, байт в байт. Недоступное помечается, а не выбрасывается."""
    fetch = fetch or (lambda u: httpx.get(u, timeout=60, follow_redirects=True).raise_for_status().content)
    outdir.mkdir(parents=True, exist_ok=True)
    catalog = []
    for f in figures:
        item = {k: f[k] for k in ("id", "src", "caption", "source_url", "context_before")}
        item.update(available=False, width=0, height=0, faces=[], file=None, sha256=None, error=None,
                    rights="владелец согласует использование сам (решение Ильи, 2026-11-07)")
        try:
            raw = fetch(f["src"])
            img = Image.open(io.BytesIO(raw))
            img.load()
            item.update(width=img.width, height=img.height, sha256=hashlib.sha256(raw).hexdigest())
            ext = Path(urlsplit(f["src"]).path).suffix.lower() or ".img"
            path = outdir / f"{f['id']}{ext}"
            path.write_bytes(raw)
            item["file"] = str(path)
            if min(img.size) < MIN_SIDE:
                item["error"] = f"слишком маленькое: {img.width}x{img.height}"
            else:
                item["available"] = True
                item["faces"] = detect_faces(img)
        except (httpx.HTTPError, OSError) as e:
            item["error"] = f"{type(e).__name__}: {e}"[:200]
        catalog.append(item)
    names = {n for it in catalog if FOUNDER_RE.search(it["caption"]) for n in NAME_RE.findall(it["caption"])}
    for it in catalog:
        it["kind"], it["priority"] = classify(it, names)
    (outdir / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    return catalog


def for_prompt(catalog: list[dict]) -> list[dict]:
    """То, что видит DeepSeek: только реально доступные снимки."""
    return [{"image_id": c["id"], "kind": c["kind"], "priority": c["priority"], "faces": len(c["faces"]),
             "caption": c["caption"], "context": c["context_before"]} for c in catalog if c["available"]]
