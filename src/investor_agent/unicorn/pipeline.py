"""Unicorn School: prepare a report at night (off-peak), publish at 10:00 Beijing."""

import logging
import re
from datetime import datetime
from html import escape

from ..app import App
from ..quality import count_sentences, is_russian
from ..sources import CITE_RE, SourceRegistry
from ..storage import norm_name
from ..textfmt import table_nodes, text_to_nodes, to_tg_html
from ..timeutil import PeakHoursError, now_bj, ru_date

log = logging.getLogger(__name__)

WEIGHTS = {"demand": 0.30, "growth": 0.25, "investors": 0.20, "cis_fit": 0.15, "info": 0.10}
SECTION_IDS = ["summary", "money_trend", "founders_story", "traction", "gtm_unit_economics", "capital_equity",
               "why_won", "russia_cis", "build_yourself", "investor_verdict", "essence"]
SECTION_TITLES = {
    "summary": "Итог за 30 секунд", "money_trend": "Денежный тренд", "founders_story": "Основатели и путь от нуля",
    "traction": "Трекшн и рост", "gtm_unit_economics": "Go to market и unit economics",
    "capital_equity": "Капитал, доли и кэшаут", "why_won": "Почему они победили",
    "russia_cis": "Аналоги в России и СНГ", "build_yourself": "Что можно создать самому",
    "investor_verdict": "Инвесторский вердикт", "essence": "Суть в пяти предложениях",
}
MIN_VERIFIED_SOURCES = 5
EXCLUDED_INDUSTRIES = ("oil", "gas", "mining", "defense", "defence", "нефт", "газ", "оборон")


class UnicornError(RuntimeError):
    pass


# --- selection (pure functions, unit-tested) ------------------------------------------------

def weighted_score(scores: dict) -> float:
    total = 0.0
    for key, weight in WEIGHTS.items():
        try:
            value = float(scores.get(key, 0))
        except (TypeError, ValueError):
            value = 0.0
        total += max(0.0, min(10.0, value)) * weight
    return round(total, 2)


def rank_candidates(candidates: list[dict], excluded: set[str], recent: list[dict]) -> list[dict]:
    """Drop ineligible candidates, apply rotation penalties, sort by final score."""
    recent_industries = {str(r.get("industry", "")).lower() for r in recent[:7]}
    recent_countries = {str(r.get("country", "")).lower() for r in recent[:3]}
    recent_ai = sum(1 for r in recent[:3] if "ai" in str(r.get("industry", "")).lower().split()
                    or "ии" in str(r.get("industry", "")).lower())
    ranked = []
    for c in candidates:
        name = str(c.get("name", "")).strip()
        industry = str(c.get("industry", "")).lower()
        if not name or norm_name(name) in excluded:
            continue
        if c.get("private") is False:
            continue
        try:
            if float(c.get("valuation_usd_bn") or 0) < 1.0:
                continue
        except (TypeError, ValueError):
            continue
        if any(word in industry for word in EXCLUDED_INDUSTRIES):
            continue
        base = weighted_score(c.get("scores") or {})
        penalty = 0.0
        if industry and industry in recent_industries:
            penalty += 1.0
        if str(c.get("country", "")).lower() in recent_countries:
            penalty += 0.5
        if c.get("ai_core") and recent_ai >= 2:
            penalty += 1.0
        ranked.append({**c, "weighted_score": base, "final_score": round(base - penalty, 2), "penalty": penalty})
    ranked.sort(key=lambda x: x["final_score"], reverse=True)
    return ranked


# --- validation -------------------------------------------------------------------------------

def strip_unknown_citations(report: dict, registry: SourceRegistry) -> None:
    def clean(text: str) -> str:
        return CITE_RE.sub(lambda m: m.group(0) if registry.get(m.group(1)) else "", text or "")
    for section in report.get("sections", []):
        section["body"] = clean(section.get("body", ""))
    report["teaser"] = clean(report.get("teaser", ""))


def validate_report(report: dict, registry: SourceRegistry) -> list[str]:
    errors: list[str] = []
    if not str(report.get("company_name", "")).strip():
        errors.append("company_name is empty")
    sections = {s.get("id"): s for s in report.get("sections", []) if isinstance(s, dict)}
    for sid in SECTION_IDS:
        body = (sections.get(sid) or {}).get("body", "")
        minimum = 100 if sid == "essence" else 400
        if len(body) < minimum:
            errors.append(f"section '{sid}' is missing or too short (min {minimum} chars)")
    essence = (sections.get("essence") or {}).get("body", "")
    if essence and ("?" not in essence or count_sentences(essence) < 7):
        errors.append("essence must have exactly five sentences, then one question and one practical rule")
    all_text = "\n".join(s.get("body", "") for s in sections.values())
    if all_text and not is_russian(all_text):
        errors.append("text must be in Russian")
    if report.get("teaser") and not is_russian(report["teaser"]):
        errors.append("teaser must be in Russian")
    verified = registry.verified(registry.cited_ids(all_text))
    if len(verified) < MIN_VERIFIED_SOURCES:
        errors.append(f"only {len(verified)} cited sources were actually opened with fetch_page; "
                      f"need at least {MIN_VERIFIED_SOURCES}")
    return errors


# --- jobs --------------------------------------------------------------------------------------

def issue_date(now: datetime | None = None) -> str:
    return (now or now_bj()).date().isoformat()


def prepare(app: App, today: str | None = None) -> str:
    """Night job. Returns status: ready | exists | failed. Raises PeakHoursError to be retried later."""
    today = today or issue_date()
    existing = app.store.draft_get(today)
    if existing and existing["status"] in ("ready", "published"):
        return "exists"
    if not app.store.acquire("unicorn_prepare", minutes=180):
        return "locked"
    try:
        return _prepare(app, today)
    except PeakHoursError:
        raise
    except Exception as exc:  # noqa: BLE001 - report every failure to admin
        log.exception("unicorn prepare failed")
        app.store.draft_save(today, "failed", {}, error=str(exc)[:500])
        app.notify_admin(f"Школа единорогов: подготовка на {today} не удалась: {exc}")
        return "failed"
    finally:
        app.record_usage("unicorn_prepare")
        app.store.release("unicorn_prepare")


def _prepare(app: App, today: str) -> str:
    s, ds = app.settings, app.deepseek
    registry = SourceRegistry()
    archive = app.store.archive_list()
    recent = app.store.recent_archive(14, today)
    stage1 = ds.research(s.deepseek_model_fast, app.prompt("unicorn_candidates"), {
        "date": today, "excluded": [a["name"] for a in archive],
        "recent": [{"industry": r["industry"], "country": r["country"], "date": r["published_date"]} for r in recent],
    }, registry, max_tool_calls=30)
    ranked = rank_candidates(stage1.get("candidates") or [], app.store.archived_names(), recent)
    if len(ranked) < 3:
        raise UnicornError(f"нужно минимум 3 подходящих кандидата, найдено {len(ranked)}")
    chosen, rejected = ranked[0], ranked[1:4]

    payload = {
        "date": today, "company": chosen,
        "rejected": [{"name": r["name"], "final_score": r["final_score"], "industry": r.get("industry"),
                      "note": r.get("score_notes", "")} for r in rejected],
        "known_sources": registry.brief(),
    }
    report = ds.research(s.deepseek_model_pro, app.prompt("unicorn_school"), payload, registry, max_tool_calls=45)
    strip_unknown_citations(report, registry)
    errors = validate_report(report, registry)
    if errors:
        cited = registry.cited_ids(*[x.get("body", "") for x in report.get("sections", [])])
        report = ds.research(s.deepseek_model_pro, app.prompt("unicorn_school"), {
            **payload, "known_sources": registry.brief(), "previous_draft": report,
            "fix_these_problems": errors,
            "instruction": "Fix the problems. Open more pages with fetch_page if sources are missing. "
                           "Return the full corrected JSON.",
            "already_cited": cited,
        }, registry, max_tool_calls=20)
        strip_unknown_citations(report, registry)
        errors = validate_report(report, registry)
    if errors:
        raise UnicornError("разбор не прошёл проверку: " + "; ".join(errors))

    report["company_name"] = report.get("company_name") or chosen["name"]
    all_text = "\n".join(x.get("body", "") for x in report["sections"])
    used = set(registry.cited_ids(all_text, report.get("teaser", "")))
    used |= {m.get("source") for m in report.get("media") or [] if registry.get(str(m.get("source", "")))}
    used |= {r.get("source") for r in report.get("rounds") or [] if registry.get(str(r.get("source", "")))}
    app.store.draft_save(today, "ready", {
        "report": report, "chosen": {k: chosen.get(k) for k in ("name", "industry", "country", "final_score",
                                                                 "weighted_score", "website")},
        "rejected": payload["rejected"], "sources": registry.brief(sorted(used, key=lambda x: int(x[1:]))),
    })
    return "ready"


def _registry_from(sources: list[dict]) -> SourceRegistry:
    reg = SourceRegistry()
    for src in sources:
        item = reg.add(src["url"], src.get("title", ""), src.get("publisher", ""), src.get("date"))
        # keep the original ids so [S12] in the text still resolves
        if item.id != src["id"]:
            reg.sources.pop(item.id)
            item.id = src["id"]
            reg.sources[item.id] = item
            reg._by_url[item.url] = item.id
        item.fetched = src.get("fetched", False)
    return reg


def build_page(report: dict, registry: SourceRegistry, date_str: str) -> tuple[str, list]:
    title = f"ШКОЛА ЕДИНОРОГОВ — {date_str}. КОМПАНИЯ ДНЯ: {report['company_name']}"
    nodes: list = [{"tag": "p", "children": [{"tag": "i", "children": [
        f"Оценка полезности кейса: {report.get('usefulness_score', '—')}/10. "
        "Материал образовательный, не является инвестиционной рекомендацией."]}]}]
    for section in report["sections"]:
        sid = section.get("id")
        nodes.append({"tag": "h3", "children": [section.get("title") or SECTION_TITLES.get(sid, sid)]})
        nodes.extend(text_to_nodes(section.get("body", ""), registry))
        if sid == "traction" and report.get("chart") and report["chart"].get("points"):
            ch = report["chart"]
            src = registry.get(str(ch.get("source", "")))
            rows = [[p.get("label"), f"{p.get('value')} {ch.get('unit', '')}".strip()] for p in ch["points"]]
            nodes.append({"tag": "p", "children": [{"tag": "b", "children": [ch.get("title", "Динамика")]}]})
            nodes.extend(table_nodes(["Период", "Значение"], rows))
            if src:
                nodes.append({"tag": "p", "children": ["Источник: ", {"tag": "a", "attrs": {"href": src.url},
                                                                       "children": [src.publisher]}]})
        if sid == "capital_equity" and report.get("rounds"):
            headers = ["Раунд", "Сумма", "Pre-money", "Post-money", "Лид", "Другие", "Тип"]
            rows = [[f"{r.get('date', '')} · {r.get('stage', '')}", r.get("amount"), r.get("pre_money"),
                     r.get("post_money"), r.get("lead"), r.get("others"), r.get("type")] for r in report["rounds"]]
            nodes.append({"tag": "p", "children": [{"tag": "b", "children": ["Раунды финансирования"]}]})
            nodes.extend(table_nodes(headers, rows))
    media = [m for m in report.get("media") or [] if registry.get(str(m.get("source", "")))]
    if media:
        nodes.append({"tag": "h3", "children": ["Фото, видео и материалы"]})
        nodes.append({"tag": "ul", "children": [
            {"tag": "li", "children": [{"tag": "a", "attrs": {"href": registry.get(m["source"]).url},
                                        "children": [m.get("caption") or registry.get(m["source"]).title]}]}
            for m in media]})
    if report.get("unknowns"):
        nodes.append({"tag": "h3", "children": ["Что публично неизвестно"]})
        nodes.append({"tag": "ul", "children": [{"tag": "li", "children": [u]} for u in report["unknowns"]]})
    nodes.append({"tag": "h3", "children": ["Источники"]})
    nodes.append({"tag": "ol", "children": [
        {"tag": "li", "children": [{"tag": "a", "attrs": {"href": s.url}, "children": [s.title or s.url]},
                                   f" — {s.publisher}" + (f", {s.date[:10]}" if s.date else "")]}
        for s in registry.sources.values()]})
    return title, nodes


def build_post(report: dict, date_str: str) -> str:
    teaser = to_tg_html(re.sub(r"\[S\d+\]", "", report.get("teaser", "")))
    return (f"🦄 <b>ШКОЛА ЕДИНОРОГОВ</b> · {escape(date_str)}\n"
            f"<b>Компания дня: {escape(report['company_name'])}</b>\n\n"
            f"{teaser}\n\n"
            f"⏱ Полный разбор — 10–15 минут чтения")


def publish(app: App, today: str | None = None) -> str:
    """10:00 job: Telegraph page + short post with a button. No DeepSeek calls here."""
    today = today or issue_date()
    draft = app.store.draft_get(today)
    if draft and draft["status"] == "published":
        return "already_published"
    if not draft or draft["status"] != "ready":
        app.notify_admin(f"Школа единорогов: на {today} нет готового разбора — публикация пропущена.")
        return "no_draft"
    payload = draft["payload"]
    report = payload["report"]
    registry = _registry_from(payload["sources"])
    date_str = ru_date(datetime.fromisoformat(today))
    try:
        url = draft.get("page_url") or app.telegraph.create_page(*build_page(report, registry, date_str))
        app.store.draft_mark(today, "ready", page_url=url)
        mid = app.telegram.send(app.target_chat(), build_post(report, date_str), button=("📖 Читать разбор", url))
    except Exception as exc:  # noqa: BLE001
        app.store.draft_mark(today, "ready", error=str(exc)[:500])
        app.notify_admin(f"Школа единорогов: публикация {today} не удалась: {exc}")
        return "failed"
    app.store.draft_mark(today, "published", page_url=url, message_ids=[mid])
    chosen = payload.get("chosen", {})
    app.store.archive_add(report["company_name"], report.get("industry") or chosen.get("industry", ""),
                          report.get("country") or chosen.get("country", ""), today, url)
    return "published"
