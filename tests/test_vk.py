import json

from investor_agent.riddle import vk

ARTICLE = "Основатели Acme в 2015 году запустили сервис и за три года получили 120 млн долларов выручки."
GOOD = ("Acme: как скучная накладная стала бизнесом. " * 3 + "В 2015 году основатели запустили сервис, а за три года "
        "получили 120 млн долларов выручки. " * 2 + "Главный урок для предпринимателя: ищите дорогую проблему. " * 12
        + "Полный разбор в Telegram:")


def test_good_post_passes():
    assert vk.validate(GOOD, ARTICLE) == []


def test_invented_number_link_and_missing_cta_rejected():
    errors = vk.validate(GOOD.replace("120", "999") + " https://x.ru", ARTICLE)
    assert any("числа" in e for e in errors) and any("ссылка" in e for e in errors)
    assert any("Telegram" in e for e in vk.validate(GOOD.replace("Telegram", "канале"), ARTICLE))


def test_too_short_rejected():
    assert any("длина" in e for e in vk.validate("Коротко. Telegram", ARTICLE))
