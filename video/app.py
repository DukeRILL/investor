"""Сервис монтажа: статья -> каталог фото -> сценарии DeepSeek -> рендер MP4. Ничего не публикует."""
import hashlib, json, os, re, threading, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

import catalog as C
import planner as P
import render as R

DATA = Path(os.environ.get("DATA_DIR", "/data"))
BJ = timezone(timedelta(hours=8))
PROMPT = Path(os.environ.get("PROMPT_FILE", "/app/prompts/video_scenarios.md"))
MAX_CALLS = 3  # на одну статью: первый запрос и не больше двух исправлений, дальше стоп
_render_lock = threading.Lock()  # два ролика разом не влезут в 2 CPU и 2 ГБ


def is_peak(now: datetime) -> bool:
    """Как в deepseek.py основного агента: будни 08:45–12:15 и 13:45–18:15 по Пекину."""
    now = now.astimezone(BJ)
    m = now.hour * 60 + now.minute
    return now.weekday() < 5 and (525 <= m < 735 or 825 <= m < 1095)


def call_deepseek(system: str, user: str) -> dict:
    if is_peak(datetime.now(BJ)):
        raise PermissionError("DeepSeek нельзя вызывать в пиковые часы (по Пекину)")
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY не задан")
    base = (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL_PRO") or os.environ.get("DEEPSEEK_MODEL_FAST") or "deepseek-chat"
    r = httpx.post(f"{base}/chat/completions", timeout=600, headers={"Authorization": f"Bearer {key}"}, json={
        "model": model, "temperature": 0.7, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def write_scenarios(system: str, article: str, images: list[dict], ids: set[str], ask=call_deepseek) -> tuple[dict, int]:
    """Запрашивает 4 сценария. При ошибках просит переписать только плохие, не больше MAX_CALLS запросов."""
    user = "ARTICLE:\n" + article[:14000] + "\n\nIMAGE_CATALOG:\n" + json.dumps(images, ensure_ascii=False)
    res: dict = ask(system, user)
    calls = 1
    items = res.get("scenarios") or []
    while True:
        items = (items + [{}] * 4)[:4]
        bad = {i: P.validate_scenario(s, article, ids) for i, s in enumerate(items)}
        bad = {i: e for i, e in bad.items() if e}
        if not bad or calls >= MAX_CALLS:
            break
        fix = ("\n\nYOUR PREVIOUS ANSWER HAD ERRORS. Rewrite ONLY these scenarios, keep the same order of 4 and return all 4. "
               "Errors by scenario index: " + json.dumps(bad, ensure_ascii=False) + "\nPrevious answer: " + json.dumps({"scenarios": items}, ensure_ascii=False))
        again = ask(system, user + fix)
        calls += 1
        fresh = again.get("scenarios") or []
        for i in bad:
            if i < len(fresh):
                items[i] = fresh[i]
    for i, s in enumerate(items):
        s["hypothesis"] = P.HYPOTHESES[i]
        s["problems"] = P.validate_scenario(s, article, ids)
    res["scenarios"] = items
    return res, calls


def scenarios_payload(url: str, ask=call_deepseek) -> dict:
    title, text, figures = C.parse_page(C.fetch_page(url))
    job = "v" + hashlib.sha1(f"{url}{time.time()}".encode()).hexdigest()[:10]
    jobdir = DATA / "jobs" / job
    catalog = C.build_catalog(figures, jobdir / "assets")
    ids = {c["id"] for c in catalog if c["available"]}
    system = PROMPT.read_text(encoding="utf-8")
    article = f"{title}\n\n{text}"
    # платный ответ не пропадает: тот же текст с тем же промтом и теми же фото больше не оплачивается
    key = hashlib.sha256(json.dumps([system, article, sorted(ids)], ensure_ascii=False).encode()).hexdigest()[:20]
    cache = DATA / "cache" / f"{key}.json"
    if cache.exists():
        res, calls = json.loads(cache.read_text(encoding="utf-8")), 0
    else:
        res, calls = write_scenarios(system, article, C.for_prompt(catalog), ids, ask)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    generate = []
    for i, sc in enumerate(res["scenarios"]):
        if sc["problems"]:
            continue
        P.finalize_cta(sc)
        for g in P.enforce_images(sc, catalog):
            generate.append({**g, "s": i})
    out = {"job": job, "title": title, "chars": len(text), "deepseek_calls": calls, "scenarios": res["scenarios"], "best": res.get("best"),
           "best_why": res.get("best_why"), "catalog": catalog, "generate": generate}
    (jobdir / "scenarios.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


async def scenarios(request):
    b = await request.json()
    try:
        return JSONResponse(await run_in_threadpool(scenarios_payload, b["article_url"]))
    except (ValueError, PermissionError, KeyError) as e:
        return JSONResponse({"error": str(e)}, 422)


def job_dir(job: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", job):
        raise ValueError("job")
    return DATA / "jobs" / job


async def put_audio(request):
    try:
        p = job_dir(request.path_params["job"]) / f"s{int(request.path_params['s'])}"
    except ValueError:
        return JSONResponse({"error": "job"}, 400)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{int(request.path_params['n']):02d}.mp3").write_bytes(await request.body())
    return JSONResponse({"ok": True})


def image_problem(raw: bytes) -> str | None:
    import io
    from PIL import Image, ImageStat
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (OSError, ValueError):
        return "не картинка"
    if min(img.size) < 512:
        return "мало пикселей"
    if max(ImageStat.Stat(img.convert("L")).stddev) < 8:
        return "пустой кадр"
    return None


async def put_image(request):
    image_id = request.path_params["image_id"]
    if not re.fullmatch(r"gen\d\d", image_id):
        return JSONResponse({"error": "image_id"}, 400)
    try:
        p = job_dir(request.path_params["job"]) / "generated" / f"s{int(request.path_params['s'])}"
    except ValueError:
        return JSONResponse({"error": "job"}, 400)
    p.mkdir(parents=True, exist_ok=True)
    body = await request.body()
    why = image_problem(body)
    if why:  # FLUX дал брак (мелкая или пустая картинка): n8n добирает из Pixabay
        return JSONResponse({"ok": False, "error": why})
    (p / f"{image_id}.png").write_bytes(body)
    return JSONResponse({"ok": True})


def stock_one(job: str, s: int, image_id: str, prompt: str) -> dict:
    """Приоритет 2: готовое фото Pixabay вместо FLUX. Нашлось -> сохранено как кадр сцены; нет -> found=false, дальше FLUX."""
    import stock
    from PIL import Image
    q = stock.query_from_prompt(prompt)
    if not q or not (os.environ.get("PIXABAY_API_KEY") or os.environ.get("PEXELS_API_KEY")):
        return {"found": False, "why": "нет запроса или ключа"}
    jd = job_dir(job)
    got = stock.pick(q, DATA / "stock", C.detect_faces, set())
    if not got:
        return {"found": False, "query": q}
    gdir = jd / "generated" / f"s{s}"
    gdir.mkdir(parents=True, exist_ok=True)
    Image.open(got["file"]).save(gdir / f"{image_id}.png")
    meta = {k: got[k] for k in ("source", "id", "page", "author", "width", "height", "query", "license")}
    (gdir / f"{image_id}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"found": True, **meta}


async def put_stock(request):
    b = await request.json()
    image_id = request.path_params["image_id"]
    if not re.fullmatch(r"gen\d\d", image_id):
        return JSONResponse({"error": "image_id"}, 400)
    try:
        return JSONResponse(await run_in_threadpool(stock_one, request.path_params["job"], int(request.path_params["s"]), image_id, str(b.get("prompt", ""))))
    except ValueError:
        return JSONResponse({"error": "job"}, 400)


def render_one(job: str, s: int) -> dict:
    jd = job_dir(job)
    meta = json.loads((jd / "scenarios.json").read_text(encoding="utf-8"))
    sc = meta["scenarios"][s]
    if sc.get("problems"):
        raise ValueError(f"сценарий {s} не прошёл проверку: {sc['problems']}")
    scenes = sc["scenes"]
    audio = [jd / f"s{s}" / f"{i:02d}.mp3" for i in range(len(scenes))]
    if not all(a.exists() for a in audio):
        raise ValueError("нет озвучки для всех сцен")
    images = {c["id"]: c for c in meta["catalog"] if c["available"]}
    for g in (jd / "generated" / f"s{s}").glob("*.png"):
        from PIL import Image
        w, h = Image.open(g).size
        images[g.stem] = {"id": g.stem, "file": str(g), "width": w, "height": h, "faces": [], "kind": "generated"}
        if g.with_suffix(".json").exists():  # стоковое фото: источник и лицензия идут в манифест
            images[g.stem]["stock"] = json.loads(g.with_suffix(".json").read_text(encoding="utf-8"))
    missing = [x["image_id"] for x in scenes if x["image_id"] not in images]
    if missing:
        raise ValueError(f"нет изображений: {missing}")
    out = DATA / "out" / f"{job}_{s}_{sc['hypothesis']}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    with _render_lock:
        info = R.render(scenes, audio, images, out)
        info["sync"] = R.verify_sync(out, info)
    info["hypothesis"] = sc["hypothesis"]
    info["stock"] = {k: v["stock"] for k, v in images.items() if v.get("stock") and any(x["image_id"] == k for x in scenes)}
    info["counts"] = {"original": sum(x["source_type"] == "original" for x in scenes), "generated": sum(x["source_type"] == "generated" for x in scenes)}
    out.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return {k: v for k, v in info.items() if k != "scenes"} | {"file": f"/out/{out.name}", "path": str(out)}


async def do_render(request):
    b = await request.json()
    try:
        return JSONResponse(await run_in_threadpool(render_one, b["job"], int(b["s"])))
    except (ValueError, KeyError) as e:
        return JSONResponse({"error": str(e)}, 422)


async def get_out(request):
    p = DATA / "out" / Path(request.path_params["name"]).name
    return FileResponse(p) if p.is_file() else JSONResponse({"error": "not found"}, 404)


async def health(request):
    return JSONResponse({"ok": True})

app = Starlette(routes=[
    Route("/health", health), Route("/scenarios", scenarios, methods=["POST"]),
    Route("/jobs/{job}/{s:int}/audio/{n:int}", put_audio, methods=["PUT"]),
    Route("/jobs/{job}/{s:int}/image/{image_id}", put_image, methods=["PUT"]),
    Route("/jobs/{job}/{s:int}/stock/{image_id}", put_stock, methods=["POST"]),
    Route("/render", do_render, methods=["POST"]), Route("/out/{name}", get_out)])
