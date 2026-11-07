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
