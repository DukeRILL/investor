"""Бесплатный подбор фото (Pexels, Pixabay) вместо генерации. Только свободные лицензии, без людей."""
import hashlib, io, json, os, re, time
from pathlib import Path

import httpx
from PIL import Image

MIN_SIDE = 1000
PEOPLE_TAGS = re.compile(r"\b(people|person|man|men|woman|women|girl|boy|child|kid|portrait|face|team|crowd|couple|family|"
                         r"student|worker|businessman|businesswoman|model|selfie)\b", re.I)
PEXELS = "https://api.pexels.com/v1/search"
PIXABAY = "https://pixabay.com/api/"
LICENSES = {"pexels": "Pexels License (бесплатно, коммерческое использование, без обязательной ссылки)",
            "pixabay": "Pixabay Content License (бесплатно, коммерческое использование, без обязательной ссылки)"}


CACHE_TTL = 24 * 3600  # условия Pixabay: ответы API кешировать минимум 24 часа
RETRIES = 2
STOP = set("a an the of and or in on at with for to from by is are no not without abstract editorial illustration "
           "faces face people text letters logos logo style background image photo".split())


def query_from_prompt(prompt: str, limit: int = 5) -> str:
    """Поисковая фраза из английского описания кадра: смысловые слова, без стиля и дублей."""
    words = []
    for w in re.findall(r"[a-z]{3,}", (prompt or "").lower()):
        if w not in STOP and w not in words:
            words.append(w)
    return " ".join(words[:limit])


def cache_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data")) / "cache" / "stock"


def get_json(http, url: str, *, params: dict, headers: dict | None = None, name: str, sleep=time.sleep) -> dict | None:
    """GET с кешем 24 ч, ограниченными повторами на 429/5xx/таймаут. Недоступность сервиса -> None, не исключение."""
    safe = {k: v for k, v in params.items() if k != "key"}  # ключ не попадает ни в имя кеша, ни в логи
    f = cache_dir() / (hashlib.sha1(json.dumps([name, safe], sort_keys=True).encode()).hexdigest() + ".json")
    try:
        if f.exists() and time.time() - f.stat().st_mtime < CACHE_TTL:
            return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    for attempt in range(RETRIES + 1):
        try:
            r = http.get(url, params=params, headers=headers or {})
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            data = r.json()
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass
            return data
        if r is not None and r.status_code != 429 and r.status_code < 500:
            return None  # 4xx (неверный ключ и т.п.) повторять бессмысленно
        if attempt < RETRIES:
            wait = 2 ** attempt
            if r is not None and r.status_code == 429:
                try:
                    wait = min(float(r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset") or 60), 65)
                except ValueError:
                    wait = 60
            sleep(wait)
    return None


def search(query: str, http=None) -> list[dict]:
    """Кандидаты по запросу: вертикальные фото с обоих сервисов. Нет ключа - сервис пропускается."""
    http = http or httpx.Client(timeout=30, follow_redirects=True)
    out: list[dict] = []
    if os.environ.get("PEXELS_API_KEY"):
        d = get_json(http, PEXELS, params={"query": query, "orientation": "portrait", "per_page": 15},
                     headers={"Authorization": os.environ["PEXELS_API_KEY"]}, name="pexels")
        if d:
            for p in d.get("photos", []):
                out.append({"source": "pexels", "id": f"pexels-{p['id']}", "url": p["src"].get("large2x") or p["src"]["original"],
                            "page": p["url"], "author": p.get("photographer", ""), "width": p["width"], "height": p["height"], "query": query})
    if os.environ.get("PIXABAY_API_KEY"):
        d = get_json(http, PIXABAY, params={"key": os.environ["PIXABAY_API_KEY"], "q": query, "orientation": "vertical", "image_type": "photo",
                                            "safesearch": "true", "per_page": 20, "min_height": MIN_SIDE}, name="pixabay")
        if d:
            for p in d.get("hits", []):
                out.append({"source": "pixabay", "id": f"pixabay-{p['id']}", "url": p.get("largeImageURL") or p["webformatURL"],
                            "page": p["pageURL"], "author": p.get("user", ""), "tags": p.get("tags", ""), "width": p["imageWidth"], "height": p["imageHeight"], "query": query})
    return out


def usable(c: dict, faces, used_hashes: set[str], raw: bytes) -> str | None:
    """Почему кандидат не годится (None - годится)."""
    if min(c["width"], c["height"]) < MIN_SIDE // 2 or c["height"] < MIN_SIDE:
        return "мало пикселей"
    if PEOPLE_TAGS.search(c.get("tags", "")):
        return "люди по тегам"
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
    qwords = set(query.lower().split())
    cands = search(query, http)
    # смысл: у Pixabay берём только кадры, где хоть одно слово запроса есть в тегах
    cands = [c for c in cands if c["source"] != "pixabay" or qwords & set(re.findall(r"[a-z]+", c.get("tags", "").lower()))]
    for c in cands:
        done = outdir / f"{c['id']}.jpg"
        if done.exists():  # уже сохранено ранее: повторно не качаем и не берём как дубль
            continue
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
