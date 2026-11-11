"""Правило владельца (2026-11-08): в субтитрах и надписях названия брендов и имена пишутся латиницей.
Исключение: названия российских компаний остаются кириллицей. Озвучка при этом использует русское произношение."""
import re

# русское написание/произношение (с падежами) -> как писать в субтитрах; задаётся паспортом компании
DISPLAY_NAMES: dict[str, str] = {
    r"Уоп\w*": "Whop", r"Стивен\w*": "Steven", r"Шварц\w*": "Schwartz", r"Кэмерон\w*": "Cameron",
    r"Зуб(?:а|у|ом|е)?": "Zoub", r"Джек\w*": "Jack", r"Шарки": "Sharkey",
    r"Дискорд\w*": "Discord", r"Телеграм\w*": "Telegram", r"Фейсбук\w*": "Facebook",
}
RUSSIAN_COMPANIES = {"Школа единорогов", "Сбер", "Яндекс", "Тинькофф"}  # остаются кириллицей


def display(text: str, names: dict[str, str] | None = None) -> str:
    """Подменяет русские названия и имена латиницей, не трогая российские компании."""
    names = DISPLAY_NAMES if names is None else names
    for pat, en in names.items():
        text = re.sub(rf"\b{pat}\b", en, text, flags=re.I)
    return text
