"""Проверка сценариев DeepSeek и правило «сначала оригиналы»: решает код, а не модель."""
import re
from difflib import SequenceMatcher

HYPOTHESES = ["ordinary_founders", "billion_pain", "against_expectations", "simplicity"]
EFFECTS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "face_sweep", "parallax"]
MIN_ORIGINALS = 3  # FLUX разрешён, только если пригодных оригиналов меньше этого числа
MIN_WORDS, MAX_WORDS = 60, 88  # реальный темп голоса Lev +12 %: 2,4 слова в секунду
CTA_VOICE = "Полный разбор бизнес-модели — в Telegram, Школа единорогов."
CTA_SCREEN = "Полный разбор бизнес-модели — в Telegram"
PEOPLE_RE = re.compile(r"portrait|founder|ceo|person|people|man\b|woman|face|photo of|selfie|crowd|team|handshake|"
                       r"портрет|основател|человек|люди|лицо|фото", re.I)
GEN_STYLE = "Abstract editorial illustration, no people, no faces, no text, no letters, no logos. "


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower().replace("ё", "е"))).strip()


def quote_found(quote: str, article_norm: str) -> bool:
    q = norm(quote)
    if len(q) < 12:
        return False
    if q in article_norm:
        return True
    # допускаем мелкие расхождения в склонении: ищем лучшее совпадение по длинному блоку
    m = SequenceMatcher(None, q, article_norm, autojunk=False).find_longest_match(0, len(q), 0, len(article_norm))
    return m.size >= 0.7 * len(q)


def article_numbers(text: str) -> set[str]:
    out = set()
    for raw in re.findall(r"\d[\d\s.,]*\d|\d", text):
        d = re.sub(r"\s", "", raw).replace(",", ".").rstrip(".")
        out.add(d.lstrip("0") or "0")
        out.add(d.replace(".", ""))
    return out


def number_ok(value, numbers: set[str]) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    s = f"{v:g}"
    # 0,232 млрд в статье может стать 232 млн в сцене: это та же цифра
    return s in numbers or s.replace(".", "") in numbers or f"{v / 1000:g}" in numbers or f"0{v / 1000:g}" in numbers or f"{v * 1000:g}" in numbers


def validate_scenario(sc: dict, article: str, catalog_ids: set[str]) -> list[str]:
    """Что код может проверить сам. Непустой список означает: сценарий не годится."""
    errs: list[str] = []
    scenes = sc.get("scenes") or []
    if not 5 <= len(scenes) <= 7:
        errs.append(f"сцен {len(scenes)}, нужно 5–7")
        return errs
    art, nums = norm(article), article_numbers(article)
    body = scenes[:-1]
    words = sum(len(s.get("voice", "").split()) for s in body) + len(CTA_VOICE.split())
    if not MIN_WORDS <= words <= MAX_WORDS:
        errs.append(f"слов в озвучке {words}, нужно {MIN_WORDS}–{MAX_WORDS}")
    hook = len(body[0].get("voice", "").split())
    if hook > 10:
        errs.append(f"хук {hook} слов, нужно не больше 10")
    for i, s in enumerate(body):
        if not s.get("voice", "").strip():
            errs.append(f"сцена {i + 1}: нет текста")
        if not quote_found(s.get("source_quote", ""), art):
            errs.append(f"сцена {i + 1}: source_quote нет в статье")
        for d in re.findall(r"\d[\d.,]*", s.get("voice", "") + " " + s.get("on_screen", "")):
            if not number_ok(d.replace(",", ".").rstrip("."), nums):
                errs.append(f"сцена {i + 1}: числа {d} нет в статье")
        bn = s.get("big_number")
        if bn and not number_ok(bn.get("value"), nums):
            errs.append(f"сцена {i + 1}: big_number {bn.get('value')} нет в статье")
    for i, s in enumerate(scenes):
        if not s.get("needs_generation") and s.get("image_id") not in catalog_ids:
            errs.append(f"сцена {i + 1}: нет изображения {s.get('image_id')}")
    return errs


NEGATED_RE = re.compile(r"\b(no|without|empty of)\s+(people|person|faces?|humans?|hands|text|letters|logos?)\b", re.I)


def safe_image_prompt(p: str | None) -> bool:
    """Без людей. Запреты вида «no people, no faces» людьми не считаются."""
    return bool(p and p.strip()) and not PEOPLE_RE.search(NEGATED_RE.sub(" ", p))


def best_original(catalog: list[dict], used: dict[str, int], kind_hint: str | None = None) -> dict:
    return min(catalog, key=lambda c: (c["kind"] != kind_hint, used.get(c["id"], 0), c["priority"], c["id"]))


def enforce_images(sc: dict, catalog: list[dict]) -> list[dict]:
    """Приводит сцены к правилу владельца и возвращает список нужных FLUX-генераций (обычно пустой).

    FLUX разрешён только при нехватке: пригодных оригиналов меньше MIN_ORIGINALS, и модель сама запросила
    генерацию с безопасным промтом без людей. Иначе сцена получает оригинал.
    """
    usable = [c for c in catalog if c["available"]]
    by_id = {c["id"]: c for c in usable}
    shortage = len(usable) < MIN_ORIGINALS
    used: dict[str, int] = {}
    generate = []
    for i, s in enumerate(sc["scenes"]):
        wanted = bool(s.get("needs_generation"))
        if wanted and shortage and safe_image_prompt(s.get("image_prompt")):
            s.update(image_id=f"gen{i + 1:02d}", source_type="generated", needs_generation=True,
                     image_prompt=GEN_STYLE + s["image_prompt"].strip())
            generate.append({"image_id": s["image_id"], "image_prompt": s["image_prompt"]})
            continue
        if s.get("image_id") not in by_id:
            if not usable:
                raise ValueError("нет ни одного оригинала, а безопасного промта для генерации нет")
            s["image_id"] = best_original(usable, used)["id"]
            s["why"] = f"Подставлено кодом: {'запрос генерации отклонён, ' if wanted else ''}оригиналов достаточно. " + str(s.get("why") or "")
        s.update(source_type="original", needs_generation=False, image_prompt=None)
        used[s["image_id"]] = used.get(s["image_id"], 0) + 1
    # один и тот же кадр подряд с одним эффектом выглядит как зависшее видео
    prev = None
    for s in sc["scenes"]:
        if s.get("effect") not in EFFECTS:
            s["effect"] = "zoom_in"
        if prev and s["image_id"] == prev["image_id"] and s["effect"] == prev["effect"]:
            s["effect"] = "zoom_out" if s["effect"] != "zoom_out" else "zoom_in"
        prev = s
    return generate


def finalize_cta(sc: dict) -> None:
    """Финал дословно как просил владелец, модель его не переписывает."""
    last = sc["scenes"][-1]
    last.update(voice=CTA_VOICE, on_screen=CTA_SCREEN, big_number=None, is_cta=True)


# ---------- текст для голоса Azure: только целые русские слова ----------
VOICE_BAD = [
    (re.compile(r"\d"), "цифры"),
    (re.compile(r"[A-Za-z]"), "латиница"),
    (re.compile(r"[$%€₽&@#/+=*<>«»\"()\[\]]"), "символы"),
    (re.compile(r"\b(ИИ|ПО|ИТ|США|млн|млрд|тыс|руб|долл)\b|\b[А-ЯЁ]{2,}\b"), "сокращения"),
]


def voice_problems(text: str) -> list[str]:
    """Что помешает голосу прочитать текст правильно. Пустой список: текст годен для Azure."""
    out = []
    for rx, what in VOICE_BAD:
        found = rx.findall(text)
        if found:
            sample = [f if isinstance(f, str) else "".join(f) for f in found][:3]
            out.append(f"{what}: {', '.join(sorted(set(sample)))}")
    return out


# ---------- план кадров: правила ревью владельца ----------
CLICHE_RE = re.compile(r"\bcoins?\b|money rain|rocket|light ?bulb|puzzle|handshake|success arrow|\bgold\b", re.I)
MAX_SHOT = 4.05


def shot_problems(shots: list[dict], catalog: dict[str, dict]) -> list[str]:
    """Нарушения правил кадров. Пустой список: план можно отдавать в генерацию и монтаж."""
    errs = []
    if shots and shots[0].get("source_type") != "original":
        errs.append("первый кадр должен быть реальным фото")
    for i, s in enumerate(shots):
        if s["end"] - s["start"] > MAX_SHOT:
            errs.append(f"кадр {i + 1}: дольше 4 секунд")
        if i and s.get("image_id") and s.get("image_id") == shots[i - 1].get("image_id") and s.get("focus") == shots[i - 1].get("focus"):
            errs.append(f"кадр {i + 1}: тот же кадр подряд")
        if s.get("source_type") == "generated":
            p = s.get("image_prompt") or ""
            if not safe_image_prompt(p):
                errs.append(f"кадр {i + 1}: в промте люди или пусто")
            if CLICHE_RE.search(p):
                errs.append(f"кадр {i + 1}: штамп в промте ({CLICHE_RE.search(p).group(0)})")
        elif s.get("image_id") not in catalog:
            errs.append(f"кадр {i + 1}: нет изображения {s.get('image_id')}")
    return errs


LOGO_GAP = 7


def library_shot_problems(shots: list[dict], library: dict[str, dict]) -> list[str]:
    """Правила подбора кадров из библиотеки (ревью Ильи 2026-11-08, оценка 2/10)."""
    errs = []
    if shots and library.get(shots[0].get("image_id"), {}).get("kind") != "founder":
        errs.append("первый кадр должен быть реальным фото основателей")
    logo_at: list[int] = []
    seen: dict[str, int] = {}
    founder_uses: dict[str, int] = {}
    for i, s in enumerate(shots):
        img = s.get("image_id")
        item = library.get(img)
        if item is None:
            errs.append(f"кадр {i + 1}: нет в библиотеке {img}")
            continue
        if s["end"] - s["start"] > MAX_SHOT:
            errs.append(f"кадр {i + 1}: дольше 4 секунд")
        if item.get("kind") == "brand":
            if logo_at and i - logo_at[-1] < LOGO_GAP:
                errs.append(f"кадр {i + 1}: логотип ближе {LOGO_GAP} кадров к прошлому")
            logo_at.append(i)
        elif item.get("kind") == "founder" and img in seen:
            # решение Ильи 2026-11-08: фото основателей можно показать второй раз с другим кадрированием, не ближе 7 кадров
            if i - seen[img] < LOGO_GAP or shots[seen[img]].get("focus") == s.get("focus") or founder_uses.get(img, 1) >= 2:
                errs.append(f"кадр {i + 1}: {img} слишком близко или то же кадрирование")
            founder_uses[img] = founder_uses.get(img, 1) + 1
            seen[img] = i
            continue
        elif img in seen:
            errs.append(f"кадр {i + 1}: {img} уже был в кадре {seen[img] + 1}")
        seen.setdefault(img, i)
    if len(logo_at) > 2:
        errs.append("логотип больше 2 раз")
    return errs


def split_library(generated: list[dict], scripts: list[dict]) -> list[list[str]]:
    """Делит сгенерированные картинки между роликами: у каждого ролика свои (решение Ильи 2026-11-08).

    Картинка уходит ролику, где её факт звучит; при равенстве — ролику, у которого пока меньше картинок.
    """
    out: list[list[str]] = [[] for _ in scripts]
    facts = [{l.get("fact_id") for l in s["lines"]} for s in scripts]
    for g in generated:
        cand = [i for i, f in enumerate(facts) if g.get("fact_id") in f] or list(range(len(scripts)))
        best = min(cand, key=lambda i: (len(out[i]), i))
        out[best].append(g["id"])
    return out


def assemble_shots(lines: list[dict], matches: dict[str, list[str]], library: dict[str, dict],
                   used_elsewhere: dict[str, int], max_videos: int = 2, fill_spare: bool = True) -> list[dict]:
    """Раскладывает кадры по правилам владельца; модель только подсказывает подходящие картинки.

    Кадр не длиннее 4 с, каждая картинка один раз, первый кадр — основатели, логотип только в финале (так он
    никогда не ближе 7 кадров), сгенерированная картинка не больше чем в max_videos роликах.
    Если подходящих картинок для фразы меньше, чем нужно кадров, добираем неиспользованными сгенерированными
    (сначала реже всего показанными) и помечаем кадр fit=False, чтобы это было видно в отчёте.
    """
    import math
    shots: list[dict] = []
    used: set[str] = set()
    founder = next((k for k, v in library.items() if v.get("kind") == "founder"), None)
    brand = next((k for k, v in library.items() if v.get("kind") == "brand"), None)

    def free(img: str) -> bool:
        item = library.get(img)
        if item is None or img in used or item.get("kind") == "brand":
            return False
        return item.get("kind") != "generated" or used_elsewhere.get(img, 0) < max_videos

    for li, ln in enumerate(lines):
        dur = ln["end"] - ln["start"]
        need = max(1, math.ceil(dur / MAX_SHOT - 1e-9))
        if ln["line"] == "CTA" and brand:
            shots.append({"line": "CTA", "start": ln["start"], "end": ln["end"], "image_id": brand, "source_type": "original", "fit": True})
            continue
        cands = list(matches.get(ln["line"], []))
        if li == 0 and founder:
            cands = [founder] + [c for c in cands if c != founder]
        picks = [(c, True) for c in dict.fromkeys(cands) if free(c)][:need]
        for c, _ in picks:
            used.add(c)
        while fill_spare and len(picks) < need:
            spare = sorted((k for k, v in library.items() if v.get("kind") == "generated" and free(k)),
                           key=lambda k: (used_elsewhere.get(k, 0), k))
            if not spare:
                break
            used.add(spare[0])
            picks.append((spare[0], False))
        gaps = need - len(picks)
        if not fill_spare and gaps > 0:  # пустые места отдаём вызывающему: он добирает картинки (FLUX, потом стоки)
            picks += [(None, False)] * gaps
        if not picks:
            if shots:
                shots[-1]["end"] = ln["end"]
            continue
        step = dur / len(picks)
        for i, (img, fit) in enumerate(picks):
            shots.append({"line": ln["line"], "start": round(ln["start"] + i * step, 2), "end": round(ln["start"] + (i + 1) * step, 2),
                          "image_id": img, "source_type": "generated" if img and library[img].get("kind") == "generated" else "original", "fit": fit})
    return shots


BANNED_QUERY_RE = re.compile(r"toolbox|tool ?belt|wrench|screw|bolt|puzzle|rubik|jigsaw|light ?bulb|rocket|\bcoins?\b|money|gear|vault|chain|mousetrap|"
                             r"paper airplane|unicorn|robot|arrow|chart|graph|screen|monitor|laptop|keyboard|sign\b|logo|book|document|receipt", re.I)


def clean_queries(queries: list[str]) -> list[str]:
    """Запросы для стоков без метафор и без предметов с текстом: DeepSeek нарушает запрет в промте, поэтому проверяет код."""
    return [q for q in queries if q and not BANNED_QUERY_RE.search(q) and 2 <= len(q.split()) <= 5]


def fill_gaps(videos: list[list[dict]], library: dict[str, dict], used: dict[str, int], max_videos: int = 3) -> int:
    """Закрывает пробелы в раскладке: сначала неиспользованные в ролике сгенерированные (у кого меньше показов),
    потом второй показ фото основателей другим кадрированием не ближе LOGO_GAP кадров. Возвращает число оставшихся пробелов."""
    founder = next((k for k, v in library.items() if v.get("kind") == "founder"), None)
    for _ in range(40):
        progress = False
        order = sorted(range(len(videos)), key=lambda i: -sum(s["image_id"] is None for s in videos[i]))
        for vi in order:
            shots = videos[vi]
            idx = next((i for i, s in enumerate(shots) if s["image_id"] is None), None)
            if idx is None:
                continue
            have = {s["image_id"] for s in shots}
            spare = sorted((k for k, v in library.items() if v.get("kind") == "generated" and k not in have and used.get(k, 0) < max_videos),
                           key=lambda k: (used.get(k, 0), k))
            if spare:
                shots[idx].update(image_id=spare[0], source_type="generated", fit=False)
                used[spare[0]] = used.get(spare[0], 0) + 1
                progress = True
                continue
            pos = [i for i, s in enumerate(shots) if s["image_id"] == founder]
            far = [i for i, s in enumerate(shots) if s["image_id"] is None and all(abs(i - j) >= LOGO_GAP for j in pos)]
            if founder and len(pos) == 1 and far:
                shots[far[0]].update(image_id=founder, source_type="original", fit=False, focus=0, effect="zoom_in")
                progress = True
        if not progress:
            break
    return sum(s["image_id"] is None for sh in videos for s in sh)
