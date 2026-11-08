"""Glossary of Unicorn School: a pinned channel post (not an article), grown with every issue.

Terms are grouped by topic; each group is a collapsed (expandable) quote. Only general economics/investing terms
and abbreviations are explained, as many as an issue uses. If the text outgrows one Telegram message (4096
characters) whole groups move to follow-up messages; the first one is pinned. docs/RULES.md section 5.
"""

import json
import logging
from html import escape

from .app import App
from .quality import is_russian
from .telegram import TelegramError
from .textfmt import reading_line, visible_len
from .timeutil import now_bj, ru_date

log = logging.getLogger(__name__)

TERMS_KEY = "glossary_terms"
STATE_KEY = "glossary_state"
MESSAGE_LIMIT = 3900

_SEED = {
    "ARR": "Годовая повторяющаяся выручка: регулярные платежи по подпискам в пересчёте на год.",
    "MRR": "Monthly Recurring Revenue, месячная повторяющаяся выручка по подпискам. ARR примерно равен MRR × 12.",
    "Burn rate": "Скорость сжигания денег: сколько компания тратит сверх заработанного за месяц.",
    "Runway": "Запас хода: на сколько месяцев хватит денег на счёте при текущем burn rate.",
    "Unit economics": "Юнит-экономика: сколько компания зарабатывает и тратит на одном клиенте или одном заказе.",
    "CAC": "Customer Acquisition Cost, стоимость привлечения одного клиента: маркетинг и продажи, делённые на число новых клиентов.",
    "LTV": "Lifetime Value, сколько прибыли приносит один клиент за всё время работы с компанией.",
    "LTV/CAC": "Отношение прибыли от клиента к стоимости его привлечения.",
    "Churn": "Отток: доля клиентов или выручки, которая уходит за период.",
    "NRR": "Удержание выручки: доля выручки прошлогодних клиентов, сохранённая с учётом доплат и оттока.",
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


GROUPS = list(['Выручка и метрики', 'Деньги и юнит-экономика', 'Раунды и оценка', 'Доли и права инвесторов', 'Рынок и продукт', 'Выход на биржу'])
GROUP_OF = {'ARR': 'Выручка и метрики', 'MRR': 'Выручка и метрики', 'GMV': 'Выручка и метрики', 'Gross margin': 'Выручка и метрики', 'EBITDA': 'Выручка и метрики', 'NRR': 'Выручка и метрики', 'Churn': 'Выручка и метрики', 'Burn rate': 'Деньги и юнит-экономика', 'Runway': 'Деньги и юнит-экономика', 'Unit economics': 'Деньги и юнит-экономика', 'CAC': 'Деньги и юнит-экономика', 'LTV': 'Деньги и юнит-экономика', 'LTV/CAC': 'Деньги и юнит-экономика', 'Pre-money': 'Раунды и оценка', 'Post-money': 'Раунды и оценка', 'Down round': 'Раунды и оценка', 'Seed': 'Раунды и оценка', 'Series A/B/C': 'Раунды и оценка', 'Unicorn': 'Раунды и оценка', 'SAFE': 'Раунды и оценка', 'Term sheet': 'Раунды и оценка', 'Cap table': 'Доли и права инвесторов', 'Dilution': 'Доли и права инвесторов', 'Liquidation preference': 'Доли и права инвесторов', 'Vesting': 'Доли и права инвесторов', 'ESOP': 'Доли и права инвесторов', 'Secondary': 'Доли и права инвесторов', 'Tender offer': 'Доли и права инвесторов', 'TAM': 'Рынок и продукт', 'PMF': 'Рынок и продукт', 'GTM': 'Рынок и продукт', 'MVP': 'Рынок и продукт', 'Network effect': 'Рынок и продукт', 'Moat': 'Рынок и продукт', 'Bootstrapping': 'Рынок и продукт', 'B2B / B2C': 'Рынок и продукт', 'SaaS': 'Рынок и продукт', 'IPO': 'Выход на биржу', 'SPAC': 'Выход на биржу'}
SEED = {k: (GROUP_OF.get(k, GROUPS[0]), v) for k, v in _SEED.items()}
DEFAULT_GROUP = "Прочее"


Term = tuple[str, str]  # (group, definition)


def clean_terms(raw) -> dict[str, Term]:
    """Terms proposed by the model: short name, known group, Russian definition, no duplicates."""
    out: dict[str, Term] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        term = " ".join(str(item.get("term", "")).split())
        text = " ".join(str(item.get("definition", "")).split())
        group = str(item.get("group", "")).strip()
        if 1 < len(term) <= 60 and 15 <= len(text) <= 160 and is_russian(text) and term.lower() not in {k.lower() for k in out}:
            out[term] = (group if group in GROUPS else DEFAULT_GROUP, text)
    return out


def _stored(app: App) -> dict[str, Term]:
    try:
        raw = json.loads(app.store.get(TERMS_KEY, "{}"))
    except ValueError:
        raw = {}
    return {k: (v[0], v[1]) for k, v in raw.items() if isinstance(v, list) and len(v) == 2}


def load(app: App) -> dict[str, Term]:
    return {**SEED, **_stored(app)}


def known_terms(app: App) -> list[str]:
    return sorted(load(app))


def add_terms(app: App, terms: dict[str, Term]) -> list[str]:
    """Merge new terms (no cap); returns the ones that were really new (case-insensitive)."""
    lower = {k.lower() for k in load(app)}
    new = {k: v for k, v in terms.items() if k.lower() not in lower}
    if new:
        app.store.set(TERMS_KEY, json.dumps({**_stored(app), **new}, ensure_ascii=False))
    return list(new)


def _group_html(title: str, items: list[tuple[str, str]], group: str) -> tuple[str, str]:
    body = "\n".join(f"• <b>{escape(t)}</b> — {escape(d)}" for t, d in items)
    return group, f"<b>{escape(title)}</b>\n<blockquote expandable>{body}</blockquote>"


def build_messages(terms: dict[str, Term]) -> list[str]:
    """Group blocks packed into messages; the header and reading time are in the first/last."""
    by_group: dict[str, list[tuple[str, str]]] = {}
    for term, (group, text) in terms.items():
        by_group.setdefault(group, []).append((term, text))
    order = [g for g in GROUPS + [DEFAULT_GROUP] if g in by_group]
    header = (f"📚 <b>ГЛОССАРИЙ «ШКОЛЫ ЕДИНОРОГОВ»</b>\nТермины и сокращения из выпусков простыми словами: "
              f"{len(terms)}. Нажмите на тему, чтобы раскрыть. Список пополняется с каждым выпуском.\n"
              f"Обновлён: {escape(ru_date(now_bj()))}")
    footer = reading_line(" ".join(f"{k} {v[1]}" for k, v in terms.items()))
    blocks = []
    for g in order:  # a group longer than one message is cut into several blocks of the same group
        chunk, size = [], 0
        for item in sorted(by_group[g], key=lambda x: x[0].lower()):
            if chunk and size + len(item[1]) + len(item[0]) + 30 > MESSAGE_LIMIT - 600:
                blocks.append(_group_html(g if not blocks or blocks[-1][0] != g else f"{g} (продолжение)", chunk, g))
                chunk, size = [], 0
            chunk.append(item)
            size += len(item[1]) + len(item[0]) + 30
        blocks.append(_group_html(g if not blocks or blocks[-1][0] != g else f"{g} (продолжение)", chunk, g))
    messages, current = [], header
    for _, block in blocks:
        if len(current) + len(block) + 2 > MESSAGE_LIMIT and current != header:
            messages.append(current)
            current = "📚 <b>Глоссарий, продолжение</b>"
        current += "\n\n" + block
    messages.append(current + "\n\n" + footer)
    return messages


def sync(app: App, force: bool = False) -> str:
    """Create or update the pinned glossary post(s). Returns created | updated | failed."""
    texts = build_messages(load(app))
    try:
        state = json.loads(app.store.get(STATE_KEY, "{}"))
    except ValueError:
        state = {}
    chat = app.target_chat()
    if state.get("sending") is not None and not force:  # a send whose delivery is unknown: not repeated automatically
        if not state.get("told"):
            state["told"] = True
            app.store.set(STATE_KEY, json.dumps(state))
            app.notify_admin("Глоссарий: неизвестно, ушло ли последнее сообщение в канал. Автоматически не повторяю. "
                             "Проверьте канал и выполните вручную: python -m investor_agent glossary (если сообщения "
                             "нет) или удалите дубль после запуска.")
        return "needs_manual"
    if force:
        state.pop("sending", None), state.pop("told", None)
    ids: list[int] = list(state.get("message_ids") or []) if state.get("chat") == chat else []
    result = "updated" if ids else "created"
    try:
        for i, text in enumerate(texts):
            if i < len(ids):
                try:
                    app.telegram.edit_text(chat, ids[i], text)
                    continue
                except TelegramError as exc:
                    msg = str(exc).lower()
                    if "not modified" in msg:
                        continue
                    if "not found" not in msg:
                        raise
            state.update(chat=chat, message_ids=ids, sending=i)  # intent saved BEFORE the send
            app.store.set(STATE_KEY, json.dumps(state))
            try:
                new_id = app.telegram.send(chat, text)
            except TelegramError as exc:
                if not exc.ambiguous:
                    state.pop("sending", None)
                raise
            state.pop("sending", None)
            if i < len(ids):
                ids[i] = new_id
            else:
                ids.append(new_id)
            state.update(chat=chat, message_ids=ids)  # the id is kept at once: a crash cannot lose it
            app.store.set(STATE_KEY, json.dumps(state))
            if i == 0:
                try:
                    app.telegram.pin(chat, new_id)
                except TelegramError as exc:
                    app.notify_admin(f"Глоссарий опубликован, но не закреплён: {exc}. Закрепите сообщение вручную "
                                     "или дайте боту право закреплять сообщения.")
        for extra in ids[len(texts):]:
            try:
                app.telegram.delete(chat, extra)
            except TelegramError:
                pass
        state.update(chat=chat, message_ids=ids[:len(texts)])
        app.store.set(STATE_KEY, json.dumps(state))
        return result
    except Exception as exc:  # noqa: BLE001 - the glossary never blocks an issue
        log.warning("glossary sync failed: %s", exc)
        app.notify_admin(f"Глоссарий не обновлён: {exc}")
        state.update(chat=chat, message_ids=ids)
        app.store.set(STATE_KEY, json.dumps(state))
        return "failed"


def posted(app: App) -> bool:
    try:
        return bool(json.loads(app.store.get(STATE_KEY, "{}")).get("message_ids"))
    except ValueError:
        return False
