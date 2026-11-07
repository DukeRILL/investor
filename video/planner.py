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


def safe_image_prompt(p: str | None) -> bool:
    return bool(p and p.strip()) and not PEOPLE_RE.search(p)


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
