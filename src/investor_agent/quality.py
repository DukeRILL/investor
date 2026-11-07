"""Small text checks used by both agents."""

import re

SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]+", re.S)


def cyrillic_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", re.sub(r"\[S\d+\]|https?://\S+", "", text or ""))
    if not letters:
        return 0.0
    return sum(1 for ch in letters if re.match(r"[А-Яа-яЁё]", ch)) / len(letters)


def is_russian(text: str, threshold: float = 0.6) -> bool:
    return cyrillic_ratio(text) >= threshold


def count_sentences(text: str) -> int:
    return len([s for s in SENTENCE_RE.findall(text or "") if len(s.strip()) > 3])


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", re.sub(r"\[S\d+\]", "", text or "")))


# Owner's rule (docs/RULES.md §5): never say the time is Beijing time and never mention FRED in published text.
_SCRUB = [
    (re.compile(r"\s*\((?:по\s+)?(?:Пекину|Пекин|пекинскому\s+времени|пекинское\s+время|Beijing(?:\s+time)?)\)", re.I), ""),
    (re.compile(r",?\s*по\s+(?:Пекину|пекинскому\s+времени)\b", re.I), ""),
    (re.compile(r",?\s*(?:по\s+)?пекинско(?:му|го|е)\s+времени?\b", re.I), ""),
    (re.compile(r"\s*\([^()]*\bFRED\b[^()]*\)"), ""),
    (re.compile(r"(?:,\s*)?\b(?:по\s+данным|согласно(?:\s+данным)?|данные|источник:?)\s+FRED\b(?:\s*\([^)]*\))?,?", re.I), ""),
    (re.compile(r"\bFRED\b"), "ФРС"),
]


def scrub_text(text: str) -> str:
    out = text or ""
    for rx, repl in _SCRUB:
        out = rx.sub(repl, out)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"^[ \t]+|[ \t]+(?=[.,;:!?])", "", out, flags=re.M))


def forbidden_mentions(text: str) -> list[str]:
    found = []
    if re.search(r"пекин|beijing", text or "", re.I):
        found.append("Beijing time")
    if re.search(r"\bFRED\b", text or ""):
        found.append("FRED")
    return found


# Service remarks a model likes to append ("added to the list of covered companies", "material prepared on ...").
_META_SENTENCE = re.compile(
    r"[^.!?\n]*(?:добавлен\w*\s+в\s+(?:перечень|список|архив|базу)|чтобы\s+не\s+повторя\w+|"
    r"в\s+следующих\s+выпусках|материал\s+подготовлен|как\s+(?:языковая\s+модель|ИИ)|"
    r"данный\s+(?:текст|материал)\s+(?:создан|сгенерирован)|сгенерирован\w*\s+(?:ИИ|нейросет))[^.!?\n]*[.!?]?",
    re.I)


def strip_meta_notes(text: str) -> str:
    """Remove AI/service remarks (owner's rule, docs/RULES.md section 5)."""
    out = _META_SENTENCE.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()
