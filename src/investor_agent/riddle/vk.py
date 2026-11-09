"""VK post for «Разгадка бизнеса»: text is written at night (off-peak DeepSeek) and sent to an n8n webhook at publish time.

n8n only publishes (wall.post with the community token); it never calls DeepSeek, because 11:00 Moscow is a
peak window for DeepSeek (docs/RULES.md)."""

import json
import logging
import re

from ..deepseek import parse_json

log = logging.getLogger(__name__)

MIN_LEN, MAX_LEN = 600, 1400  # target 800-1200; the hard check is wider to avoid pointless retries
_NUM = re.compile(r"\d[\d\s.,]*\d|\d")


def _digits(text: str) -> set[str]:
    return {re.sub(r"\D", "", m) for m in _NUM.findall(text) if re.sub(r"\D", "", m)}


def validate(body: str, article: str) -> list[str]:
    errors = []
    if not MIN_LEN <= len(body) <= MAX_LEN:
        errors.append(f"длина {len(body)} знаков, нужно {MIN_LEN}-{MAX_LEN}")
    if re.search(r"https?://|t\.me/|\[S\d+\]|@\w", body):
        errors.append("в тексте есть ссылка, метка источника или @упоминание")
    unknown = sorted(_digits(body) - _digits(article))
    if unknown:
        errors.append("числа не найдены в статье: " + ", ".join(unknown))
    if "разбор" not in body.lower().rsplit("\n", 1)[-1]:
        errors.append("последняя строка должна приглашать читать полный разбор по ссылке")
    return errors


def build(app, report: dict) -> dict:
    """Returns {"text": ..., "errors": [...]}; one repair attempt. Never raises for validation problems."""
    from .pipeline import article_text  # local import: pipeline imports this module
    article = article_text(report)
    payload = {"company_name": report.get("company_name"), "headline": report.get("headline"),
               "teaser": report.get("teaser"), "sections": report.get("sections"), "article_facts": article}
    prompt = app.prompt("vk_post")
    errors: list[str] = []
    body = ""
    for attempt in range(2):
        data = dict(payload)
        if errors:
            data["fix_these_problems"] = errors
            data["previous_body"] = body
        msg = app.deepseek.chat(app.settings.deepseek_model_fast,
                                [{"role": "system", "content": prompt},
                                 {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
                                json_mode=True, temperature=0.4)
        body = str(parse_json(msg.get("content") or "").get("body") or "").strip()
        errors = validate(body, article)
        if not errors:
            break
    return {"text": body, "errors": errors}
