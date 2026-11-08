"""Бесплатный подбор фото (Pexels, Pixabay) вместо генерации. Только свободные лицензии, без людей."""
import hashlib, io, os
from pathlib import Path

import httpx
from PIL import Image

MIN_SIDE = 1000
PEXELS = "https://api.pexels.com/v1/search"
PIXABAY = "https://pixabay.com/api/"
LICENSES = {"pexels": "Pexels License (бесплатно, коммерческое использование, без обязательной ссылки)",
            "pixabay": "Pixabay Content License (бесплатно, коммерческое использование, без обязательной ссылки)"}


def search(query: str, http=None) -> list[dict]:
    """Кандидаты по запросу: вертикальные фото с обоих сервисов. Нет ключа - сервис пропускается."""
    http = http or httpx.Client(timeout=30, follow_redirects=True)
    out: list[dict] = []
    if os.environ.get("PEXELS_API_KEY"):
        r = http.get(PEXELS, params={"query": query, "orientation": "portrait", "per_page": 15},
                     headers={"Authorization": os.environ["PEXELS_API_KEY"]})
        if r.status_code == 200:
            for p in r.json().get("photos", []):
                out.append({"source": "pexels", "id": f"pexels-{p['id']}", "url": p["src"].get("large2x") or p["src"]["original"],
                            "page": p["url"], "author": p.get("photographer", ""), "width": p["width"], "height": p["height"], "query": query})
    if os.environ.get("PIXABAY_API_KEY"):
        r = http.get(PIXABAY, params={"key": os.environ["PIXABAY_API_KEY"], "q": query, "orientation": "vertical", "image_type": "photo",
                                      "safesearch": "true", "per_page": 15, "min_height": MIN_SIDE})
        if r.status_code == 200:
            for p in r.json().get("hits", []):
                out.append({"source": "pixabay", "id": f"pixabay-{p['id']}", "url": p.get("largeImageURL") or p["webformatURL"],
                            "page": p["pageURL"], "author": p.get("user", ""), "width": p["imageWidth"], "height": p["imageHeight"], "query": query})
    return out


def usable(c: dict, faces, used_hashes: set[str], raw: bytes) -> str | None:
    """Почему кандидат не годится (None - годится)."""
    if min(c["width"], c["height"]) < MIN_SIDE // 2 or c["height"] < MIN_SIDE:
        return "мало пикселей"
    if c["height"] < c["width"] * 1.2:
        return "не вертикальное"
    if hashlib.sha1(raw).hexdigest() in used_hashes:
        return "уже использовано"
    if faces():  # дорогая проверка последней
        return "на фото люди"
    return None


def pick(query: str, outdir: Path, detect_faces, used_hashes: set[str], http=None, fetch=None) -> dict | None:
    """Лучшее подходящее фото по запросу: скачивает кандидатов по порядку, пока не найдёт без людей."""
    http = http or httpx.Client(timeout=60, follow_redirects=True)
    fetch = fetch or (lambda u: http.get(u).raise_for_status().content)
    rejected = []
    for c in search(query, http):
        try:
            raw = fetch(c["url"])
            img = Image.open(io.BytesIO(raw))
            img.load()
        except (httpx.HTTPError, OSError):
            continue
        c["width"], c["height"] = img.size
        why = usable(c, lambda: detect_faces(img), used_hashes, raw)
        if why:
            rejected.append((c["id"], why))
            continue
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / f"{c['id']}.jpg"
        img.convert("RGB").save(path, quality=92)
        used_hashes.add(hashlib.sha1(raw).hexdigest())
        return {**c, "file": str(path), "license": LICENSES[c["source"]], "rejected_before": rejected}
    return None
