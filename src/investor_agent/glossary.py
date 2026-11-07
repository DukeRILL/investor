"""Glossary of Unicorn School: one Telegraph page + one pinned channel post, grown with every issue.

The pinned post is a short message with a button (the article lives on telegra.ph, never as a series of messages).
Each issue brings up to GLOSSARY_PER_ISSUE new terms; they are merged here after publication. docs/RULES.md section 5.
"""

import json
import logging
from html import escape

from .app import App
from .quality import is_russian
from .telegram import TelegramError
from .textfmt import reading_line
from .timeutil import now_bj, ru_date

log = logging.getLogger(__name__)

TERMS_KEY = "glossary_terms"
STATE_KEY = "glossary_state"
GLOSSARY_PER_ISSUE = 6
TITLE = "Глоссарий «Школы единорогов»"

SEED = {
    "ARR": "Annual Recurring Revenue, годовая повторяющаяся выручка: сумма регулярных платежей по подпискам, пересчитанная на год. Разовые платежи не входят.",
    "MRR": "Monthly Recurring Revenue, месячная повторяющаяся выручка по подпискам. ARR примерно равен MRR × 12.",
    "Burn rate": "Скорость сжигания денег: сколько компания тратит сверх заработанного за месяц.",
    "Runway": "Запас хода: на сколько месяцев хватит денег на счёте при текущем burn rate.",
    "Unit economics": "Юнит-экономика: сколько компания зарабатывает и тратит на одном клиенте или одном заказе.",
    "CAC": "Customer Acquisition Cost, стоимость привлечения одного клиента: маркетинг и продажи, делённые на число новых клиентов.",
    "LTV": "Lifetime Value, сколько прибыли приносит один клиент за всё время работы с компанией.",
    "LTV/CAC": "Отношение выгоды от клиента к цене его привлечения. Ориентир для многих моделей — выше 3, но это не закон.",
    "Churn": "Отток: доля клиентов или выручки, которая уходит за период.",
    "NRR": "Net Revenue Retention, удержание выручки: сколько выручки осталось от прошлогодних клиентов с учётом апсейлов и оттока. Выше 100% — клиенты в среднем платят больше.",
    "GMV": "Gross Merchandise Volume, оборот платформы: сумма всех сделок на ней. Это не выручка платформы, она берёт только комиссию.",
    "TAM": "Total Addressable Market, общий объём рынка, который теоретически может занять продукт.",
    "PMF": "Product-market fit, соответствие продукта рынку: клиенты сами приходят, платят и остаются.",
    "GTM": "Go to market, способ выхода на рынок: кому, как и по каким каналам компания продаёт.",
    "MVP": "Minimum Viable Product, минимальная версия продукта, достаточная, чтобы проверить спрос.",
    "Pre-money": "Оценка компании до вложений нового раунда.",
    "Post-money": "Оценка компании после вложений: pre-money плюс сумма раунда.",
    "Down round": "Раунд по оценке ниже предыдущей. Болезненный для основателей и ранних инвесторов из-за размытия долей.",
    "Cap table": "Таблица капитализации: кому какая доля в компании принадлежит.",
    "Dilution": "Размытие: доля существующих владельцев уменьшается, когда компания выпускает новые акции.",
    "Term sheet": "Условия сделки: короткий документ с ключевыми параметрами инвестиции до подписания договоров.",
    "Liquidation preference": "Приоритет при ликвидации: инвесторы получают свои деньги раньше остальных акционеров при продаже или закрытии компании.",
    "Vesting": "Постепенное получение долей: акции основателей и сотрудников переходят в собственность по графику.",
    "ESOP": "Employee Stock Option Pool, резерв опционов для сотрудников.",
    "SAFE": "Simple Agreement for Future Equity, простое соглашение: деньги сегодня, акции в будущем раунде по заранее заданным условиям.",
    "Seed": "Посевной раунд: первые серьёзные инвестиции в идею и ранний продукт.",
    "Series A/B/C": "Следующие раунды после seed: инвестиции в уже работающий бизнес для роста и масштабирования.",
    "Secondary": "Вторичная сделка: продаются уже существующие акции, деньги получают продавцы, а не компания.",
    "Tender offer": "Выкуп акций у сотрудников и основателей, часто за счёт нового инвестора.",
    "Unicorn": "Частная компания с оценкой от $1 млрд.",
    "IPO": "Initial Public Offering, первое публичное размещение акций на бирже.",
    "SPAC": "Компания-«пустышка», которая выходит на биржу, чтобы потом слиться с настоящим бизнесом. Альтернатива IPO.",
    "EBITDA": "Прибыль до вычета процентов, налогов и амортизации. Грубый показатель операционной прибыльности.",
    "Gross margin": "Валовая маржа: доля выручки, которая остаётся после прямых затрат на производство или оказание услуги.",
    "Network effect": "Сетевой эффект: чем больше пользователей, тем ценнее сервис для каждого из них.",
    "Moat": "«Ров вокруг замка»: устойчивое преимущество, которое трудно скопировать конкурентам.",
    "Bootstrapping": "Рост без внешних инвестиций, на деньги основателей и выручку.",
    "B2B / B2C": "Продажи бизнесу (B2B) или обычным потребителям (B2C).",
    "SaaS": "Software as a Service, программа по подписке вместо разовой покупки.",
}


def clean_terms(raw) -> dict[str, str]:
    """Terms proposed by the model: short name, Russian definition, no duplicates."""
    out: dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        term = " ".join(str(item.get("term", "")).split())
        text = " ".join(str(item.get("definition", "")).split())
        if 1 < len(term) <= 60 and 20 <= len(text) <= 400 and is_russian(text) and term.lower() not in {k.lower() for k in out}:
            out[term] = text
    return out


def load(app: App) -> dict[str, str]:
    try:
        stored = json.loads(app.store.get(TERMS_KEY, "{}"))
    except ValueError:
        stored = {}
    return {**SEED, **stored}


def known_terms(app: App) -> list[str]:
    return sorted(load(app))


def add_terms(app: App, terms: dict[str, str]) -> list[str]:
    """Merge new terms; returns the ones that were really new (case-insensitive)."""
    current = load(app)
    lower = {k.lower() for k in current}
    new = {k: v for k, v in list(terms.items())[:GLOSSARY_PER_ISSUE] if k.lower() not in lower}
    if new:
        try:
            stored = json.loads(app.store.get(TERMS_KEY, "{}"))
        except ValueError:
            stored = {}
        stored.update(new)
        app.store.set(TERMS_KEY, json.dumps(stored, ensure_ascii=False))
    return list(new)


def page_nodes(terms: dict[str, str]) -> list:
    nodes: list = [{"tag": "p", "children": [{"tag": "i", "children": [
        "Термины из выпусков «Школы единорогов» простыми словами. Список пополняется с каждым выпуском."]}]}]
    for term in sorted(terms, key=str.lower):
        nodes.append({"tag": "p", "children": [{"tag": "b", "children": [term]}, f" — {terms[term]}"]})
    return nodes


def post_html(count: int, text: str) -> str:
    return (f"📚 <b>ГЛОССАРИЙ «ШКОЛЫ ЕДИНОРОГОВ»</b>\n"
            f"Термины из выпусков простыми словами: ARR, burn rate, down round, cap table и другие. "
            f"Сейчас в нём {count}, список пополняется с каждым выпуском.\n"
            f"Обновлён: {escape(ru_date(now_bj()))}\n\n{reading_line(text)}")


def sync(app: App) -> str:
    """Create or update the Telegraph page and the pinned post. Returns created | updated | failed."""
    terms = load(app)
    nodes = page_nodes(terms)
    try:
        state = json.loads(app.store.get(STATE_KEY, "{}"))
    except ValueError:
        state = {}
    text = " ".join(f"{k} {v}" for k, v in terms.items())
    chat, button_text = app.target_chat(), "📖 Открыть глоссарий"
    try:
        if state.get("page_url"):
            app.telegraph.edit_page(state["page_url"], TITLE, nodes)
        else:
            state["page_url"] = app.telegraph.create_page(TITLE, nodes)
        button = (button_text, state["page_url"])
        html = post_html(len(terms), text)
        result = "updated"
        if state.get("message_id") and state.get("chat") == chat:
            try:
                app.telegram.edit_text(chat, state["message_id"], html, button=button)
            except TelegramError as exc:
                msg = str(exc).lower()
                if "not modified" in msg:
                    return "updated"
                if "not found" not in msg:  # a deleted pinned post is re-created below
                    raise
                state.pop("message_id")
        if not state.get("message_id"):
            state["message_id"] = app.telegram.send(chat, html, button=button)
            state["chat"] = chat
            result = "created"
            try:
                app.telegram.pin(chat, state["message_id"])
            except TelegramError as exc:
                app.notify_admin(f"Глоссарий опубликован, но не закреплён: {exc}. Закрепите сообщение вручную "
                                 "или дайте боту право закреплять сообщения.")
        app.store.set(STATE_KEY, json.dumps(state))
        return result
    except Exception as exc:  # noqa: BLE001 - the glossary never blocks an issue
        log.warning("glossary sync failed: %s", exc)
        app.notify_admin(f"Глоссарий не обновлён: {exc}")
        if state.get("page_url"):
            app.store.set(STATE_KEY, json.dumps(state))
        return "failed"


def page_url(app: App) -> str | None:
    try:
        return json.loads(app.store.get(STATE_KEY, "{}")).get("page_url")
    except ValueError:
        return None
