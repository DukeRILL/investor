"""Unicorn School: prepare a report at night (off-peak, 04:30), publish at 10:00 Beijing, then the topic poll."""

import json
import logging
import re
from datetime import datetime
from html import escape

from ..app import App
from ..chart import render_chart
from ..media import resolve_media
from ..quality import count_sentences, is_russian, strip_meta_notes
from ..sources import CITE_RE, SourceRegistry
from ..storage import norm_name
from ..telegram import TelegramError
from ..textfmt import READING_WPM, reading_line, reading_minutes, table_nodes, text_to_nodes, to_tg_html, visible_len
from ..timeutil import PeakHoursError, now_bj, ru_date
from .. import glossary
from ..growth import publish as growth_publish
from . import factcheck, poll
from .precheck import prune_dead_media

log = logging.getLogger(__name__)

WEIGHTS = {"demand": 0.30, "growth": 0.25, "investors": 0.20, "cis_fit": 0.15, "info": 0.10}
SECTION_IDS = ["summary", "idea", "start", "breakthrough", "money", "russia_cis", "lesson", "investor_verdict", "essence"]
SECTION_TITLES = {
    "summary": "Итог за 30 секунд", "idea": "Идея и боль", "start": "Старт", "breakthrough": "Прорыв",
    "money": "Деньги", "russia_cis": "Аналоги и конкуренты в России", "lesson": "Урок",
    "investor_verdict": "Инвесторский вердикт", "essence": "Суть в пяти предложениях",
}
MIN_VERIFIED_SOURCES = 5
MIN_READ_MINUTES = 15  # owner's rule: the article reads for at least 15 minutes
MIN_MEDIA_NOMINATED = 5  # photos and videos the model must propose; unusable ones are dropped by media.py
TELEGRAPH_PAGE_BYTES = 63_500  # Telegraph rejects content over 64 KB (65,536 bytes); keep a margin
CAPTION_LIMIT = 1000  # Telegram caption limit is 1024
# AI share (owner's rule): prefer AI-related unicorns, but at most 60% of the last AI_WINDOW issues.
AI_SHARE_MAX = 0.6
AI_WINDOW = 10
AI_MIN_BASE = 5  # denominator floor, so the first issues are not blocked by a tiny history
AI_BONUS = 1.0
AI_OVER_PENALTY = 5.0
SEARCH_CACHE_HOURS = 24  # retries of a failed night preparation reuse searches and pages already paid for
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


def is_ai_issue(row: dict) -> bool:
    """AI flag of an archived issue; old rows without the flag are judged by the industry label."""
    if row.get("ai") is not None:
        return bool(row["ai"])
    words = re.findall(r"[a-zа-яё]+", str(row.get("industry", "")).lower())
    return any(w in ("ai", "ии", "ml", "llm", "genai") for w in words) or "artificial intelligence" in \
        str(row.get("industry", "")).lower()


def ai_quota(history: list[dict]) -> dict:
    """AI share of the last issues and whether one more AI company keeps it within AI_SHARE_MAX."""
    window = history[:AI_WINDOW]
    ai = sum(1 for r in window if is_ai_issue(r))
    allowed = (ai + 1) / max(len(window) + 1, AI_MIN_BASE) <= AI_SHARE_MAX + 1e-9
    return {"window": len(window), "ai_issues": ai, "max_share": AI_SHARE_MAX, "ai_allowed": allowed}


def rank_candidates(candidates: list[dict], excluded: set[str], recent: list[dict],
                    ai_allowed: bool = True) -> list[dict]:
    """Drop ineligible candidates, apply rotation penalties and the AI-share rule, sort by final score.

    AI-related candidates get a bonus while the AI share allows one more, and a heavy penalty otherwise
    (they win only when nothing else is eligible)."""
    recent_industries = {str(r.get("industry", "")).lower() for r in recent[:7]}
    recent_countries = {str(r.get("country", "")).lower() for r in recent[:3]}
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
        if c.get("ai_core"):
            penalty += -AI_BONUS if ai_allowed else AI_OVER_PENALTY
        ranked.append({**c, "weighted_score": base, "final_score": round(base - penalty, 2), "penalty": penalty})
    ranked.sort(key=lambda x: x["final_score"], reverse=True)
    return ranked


# --- validation -------------------------------------------------------------------------------

def strip_unknown_citations(report: dict, registry: SourceRegistry) -> None:
    def clean(text: str) -> str:
        return CITE_RE.sub(lambda m: m.group(0) if registry.get(m.group(1)) else "", text or "")
    for section in report.get("sections", []):
        section["body"] = strip_meta_notes(clean(section.get("body", "")))
    report["teaser"] = strip_meta_notes(clean(report.get("teaser", "")))


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
    minutes = reading_minutes(all_text)
    if minutes < MIN_READ_MINUTES:
        errors.append(f"article reads for {minutes} min, need at least {MIN_READ_MINUTES} (about "
                      f"{MIN_READ_MINUTES * READING_WPM} words): expand the existing sections with more facts, dates, "
                      "numbers, quotes and examples, and write the sections that are too short; keep the structure")
    if all_text and not is_russian(all_text):
        errors.append("text must be in Russian")
    if report.get("teaser") and not is_russian(report["teaser"]):
        errors.append("teaser must be in Russian")
    metrics = [m for m in report.get("metrics") or [] if isinstance(m, dict) and m.get("label") and m.get("value")]
    if len(metrics) < 3:
        errors.append("metrics: give 4 headline numbers, each with label, value and note")
    scores = [x for x in report.get("verdict_scores") or [] if isinstance(x, dict) and x.get("label")]
    if len(scores) != 4:
        errors.append("verdict_scores: give exactly 4 scores out of 10 with labels")
    if len([m for m in report.get("media") or [] if isinstance(m, dict)]) < MIN_MEDIA_NOMINATED:
        errors.append(f"media: nominate at least {MIN_MEDIA_NOMINATED} photos and videos from pages you opened "
                      "(founder photos, product screens, interviews, demos)")
    verified = registry.verified(registry.cited_ids(all_text))
    if len(verified) < MIN_VERIFIED_SOURCES:
        errors.append(f"only {len(verified)} cited sources were actually opened with fetch_page; "
                      f"need at least {MIN_VERIFIED_SOURCES}")
    return errors


# --- length top-up -----------------------------------------------------------------------------

EXPAND_ROUNDS = 4
EXPAND_TARGET_WORDS = MIN_READ_MINUTES * READING_WPM + 300  # margin over the minimum
EXPAND_MAX_TOKENS = 8000
EXPAND_PROMPT = (
    "You are an editor of a Russian-language business article. Some sections are too short. Rewrite ONLY the "
    "sections listed in `expand`: keep their content, citations like [S3] and tone, and add depth - concrete facts, "
    "dates, numbers, quotes, examples, mechanisms, comparisons, from `known_sources` only (no invented facts, no "
    "filler, no repetition). Each section must reach at least `target_words` words. Write in Russian. "
    'Return JSON: {"sections":[{"id":"...","body":"..."}]} with the listed sections only.')


def _is_length_error(err: str) -> bool:
    return err.startswith("article reads for") or (err.startswith("section '") and "too short" in err)


def _words(text: str) -> int:
    return len((text or "").split())


def _expand_short(app: App, report: dict, registry: SourceRegistry, payload: dict) -> list[str]:
    """Several cheap rounds that lengthen the shortest sections only. Returns the remaining validation errors."""
    ds = app.deepseek
    errors = validate_report(report, registry)
    for rnd in range(1, EXPAND_ROUNDS + 1):
        if not errors or not all(_is_length_error(e) for e in errors):
            break
        sections = {x.get("id"): x for x in report.get("sections", []) if isinstance(x, dict)}
        total = sum(_words(x.get("body", "")) for x in sections.values())
        missing = max(EXPAND_TARGET_WORDS - total, 0)
        weak = sorted((sid for sid in SECTION_IDS if sid != "essence"),
                      key=lambda sid: _words((sections.get(sid) or {}).get("body", "")))
        picked = weak[:max(3, min(len(weak), 3 + rnd))]
        per = max(120, missing // max(len(picked), 1) + 80)
        job = [{"id": sid, "body": (sections.get(sid) or {}).get("body", ""),
                "target_words": _words((sections.get(sid) or {}).get("body", "")) + per} for sid in picked]
        log.info("unicorn length top-up %d/%d: %d words of %d, expanding %s", rnd, EXPAND_ROUNDS, total,
                 EXPAND_TARGET_WORDS, picked)
        try:
            out = app.deepseek.complete_json(app.settings.deepseek_model_pro, EXPAND_PROMPT, {
                "expand": job, "known_sources": registry.brief(), "company": payload.get("company", {}).get("name"),
                "need_more_words_total": missing}, max_tokens=EXPAND_MAX_TOKENS)
        except PeakHoursError:
            raise
        except Exception:  # noqa: BLE001 - a failed round must not lose the draft
            log.exception("length top-up round failed")
            continue
        for item in out.get("sections") or []:
            sec = sections.get(item.get("id")) if isinstance(item, dict) else None
            body = str(item.get("body", "")) if isinstance(item, dict) else ""
            if sec is not None and item.get("id") in picked and _words(body) > _words(sec.get("body", "")):
                sec["body"] = body
        strip_unknown_citations(report, registry)
        errors = validate_report(report, registry)
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
    topics = poll.chosen_topics(app, today)  # readers' vote; [] -> the agent picks the topic itself
    quota = ai_quota(app.store.last_issues(AI_WINDOW))
    stage1_payload = {
        "date": today, "excluded": [a["name"] for a in archive],
        "recent": [{"industry": r["industry"], "country": r["country"], "date": r["published_date"]} for r in recent],
        "ai_quota": quota,
    }
    ranked: list[dict] = []
    if topics:
        stage1 = ds.research(s.deepseek_model_fast, app.prompt("unicorn_candidates"),
                             {**stage1_payload, "reader_topics": topics}, registry, max_tool_calls=30,
                             cache_ttl=SEARCH_CACHE_HOURS)
        ranked = rank_candidates(stage1.get("candidates") or [], app.store.archived_names(), recent,
                                 quota["ai_allowed"])
        if len(ranked) < 3:
            log.warning("too few candidates for reader topics %s; agent picks the topic", topics)
            topics = []
    if not topics:
        stage1 = ds.research(s.deepseek_model_fast, app.prompt("unicorn_candidates"), stage1_payload, registry,
                             max_tool_calls=30, cache_ttl=SEARCH_CACHE_HOURS)
        ranked = rank_candidates(stage1.get("candidates") or [], app.store.archived_names(), recent,
                                 quota["ai_allowed"])
    if len(ranked) < 3:
        raise UnicornError(f"нужно минимум 3 подходящих кандидата, найдено {len(ranked)}")
    chosen, rejected = ranked[0], ranked[1:4]

    payload = {
        "date": today, "company": chosen,
        "rejected": [{"name": r["name"], "final_score": r["final_score"], "industry": r.get("industry"),
                      "note": r.get("score_notes", "")} for r in rejected],
        "known_sources": registry.brief(), "known_glossary_terms": glossary.known_terms(app),
    }
    report = ds.research(s.deepseek_model_pro, app.prompt("unicorn_school"), payload, registry, max_tool_calls=45,
                         cache_ttl=SEARCH_CACHE_HOURS)
    strip_unknown_citations(report, registry)
    errors = validate_report(report, registry)
    if errors:
        cited = registry.cited_ids(*[x.get("body", "") for x in report.get("sections", [])])
        report = ds.research(s.deepseek_model_pro, app.prompt("unicorn_school"), {
            **payload, "known_sources": registry.brief(), "previous_draft": report,
            "fix_these_problems": errors,
            "instruction": "Fix the problems (if the article is too short, add depth to every section, not filler). Open more pages with fetch_page if sources are missing. "
                           "Return the full corrected JSON.",
            "already_cited": cited,
        }, registry, max_tool_calls=20, cache_ttl=SEARCH_CACHE_HOURS)
        strip_unknown_citations(report, registry)
        errors = validate_report(report, registry)
    if errors and all(_is_length_error(e) for e in errors):
        errors = _expand_short(app, report, registry, payload)  # top up thin sections, never regenerate the whole issue
    if errors:
        raise UnicornError("разбор не прошёл проверку: " + "; ".join(errors))

    report["company_name"] = report.get("company_name") or chosen["name"]
    factcheck.run(app, report, registry, chosen)  # second pass over the numbers; never blocks the issue
    known = {t.lower() for t in glossary.known_terms(app)}
    report["glossary"] = [{"term": k, "group": g, "definition": d} for k, (g, d) in glossary.clean_terms(report.get("glossary")).items()
                          if k.lower() not in known]
    _attach_media(app, report, registry)
    all_text = "\n".join(x.get("body", "") for x in report["sections"])
    used = set(registry.cited_ids(all_text, report.get("teaser", "")))
    used |= {m.get("source") for m in report.get("media") or [] if registry.get(str(m.get("source", "")))}
    used |= {r.get("source") for r in report.get("rounds") or [] if registry.get(str(r.get("source", "")))}
    used |= {m.get("source") for m in report.get("metrics") or [] if registry.get(str(m.get("source", "")))}
    app.store.draft_save(today, "ready", {
        "report": report, "chosen": {k: chosen.get(k) for k in ("name", "industry", "country", "final_score",
                                                                 "weighted_score", "website", "ai_core")},
        "rejected": payload["rejected"], "sources": registry.brief(sorted(used, key=lambda x: int(x[1:]))),
        "reader_topics": topics, "poll_options": poll.make_options(app, poll.next_issue(today), recent),
    })
    return "ready"


def report_charts(report: dict) -> list[dict]:
    """Up to three charts; the old single `chart` field still works."""
    charts = [c for c in report.get("charts") or [] if isinstance(c, dict) and c.get("points")]
    if not charts and isinstance(report.get("chart"), dict) and report["chart"].get("points"):
        charts = [report["chart"]]
    return charts[:3]


def _chart_nodes(ch: dict, image: str | None, registry: SourceRegistry) -> list:
    src = registry.get(str(ch.get("source", "")).split(",")[0].strip())
    if image:
        caption: list = [ch.get("title", "Динамика"), f" ({ch['unit']})" if ch.get("unit") else ""]
        if src:
            caption += [". Источник: ", {"tag": "a", "attrs": {"href": src.url}, "children": [src.publisher]}]
        return [{"tag": "figure", "children": [{"tag": "img", "attrs": {"src": image}},
                                               {"tag": "figcaption", "children": caption}]}]
    rows = [[p.get("label"), f"{p.get('value')} {ch.get('unit', '')}".strip()] for p in ch["points"]]
    out = [{"tag": "p", "children": [{"tag": "b", "children": [ch.get("title", "Динамика")]}]}]
    out.extend(table_nodes(["Период", "Значение"], rows))
    if src:
        out.append({"tag": "p", "children": ["Источник: ", {"tag": "a", "attrs": {"href": src.url}, "children": [src.publisher]}]})
    return out


def _uploader(app: App):
    return (app.images or app.telegraph).upload_image


def _attach_media(app: App, report: dict, registry: SourceRegistry) -> None:
    """Find real pictures for the nominated media and draw the chart. Failures only make the issue plainer."""
    try:
        report["media_resolved"] = resolve_media(report.get("media") or [], registry, app.http, _uploader(app))
    except Exception as exc:  # noqa: BLE001
        log.warning("media resolution failed: %s", type(exc).__name__)
        report["media_resolved"] = []
    report["chart_images"] = []
    for chart in report_charts(report):
        try:
            png = render_chart(chart.get("points") or [], chart.get("unit", ""), chart.get("kind", "line"))
            report["chart_images"].append(_uploader(app)(png, "image/png") if png else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("chart image failed: %s", type(exc).__name__)
            report["chart_images"].append(None)
    kinds = [m["kind"] for m in report["media_resolved"]]
    log.info("media: %d photos, %d videos, %d pages; chart images: %d of %d", kinds.count("photo"), kinds.count("video"),
             kinds.count("page"), sum(bool(x) for x in report["chart_images"]), len(report["chart_images"]))


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


def _cite(registry: SourceRegistry, sid) -> dict | None:
    src = registry.get(str(sid or ""))
    return {"tag": "a", "attrs": {"href": src.url}, "children": [f"[{src.id[1:]}]"]} if src else None


def _figure(m: dict) -> dict:
    inner = ({"tag": "img", "attrs": {"src": m["src"]}} if m["kind"] == "photo"
             else {"tag": "iframe", "attrs": {"src": m["embed"]}})
    link = m.get("video_url") or m.get("source_url")
    caption: list = [m.get("caption") or ""]
    if link:
        caption += [" ", {"tag": "a", "attrs": {"href": link}, "children": [m.get("publisher") or "источник"]}]
    return {"tag": "figure", "children": [inner, {"tag": "figcaption", "children": caption}]}


def _interleave(body: list, figures: list) -> list:
    """Spread figures through the section instead of stacking them under the heading."""
    if not figures:
        return body
    step = max(1, len(body) // (len(figures) + 1))
    out, queue = [], list(figures)
    for i, node in enumerate(body, 1):
        out.append(node)
        if queue and i % step == 0:
            out.append(queue.pop(0))
    return out + queue


def hero_photo(report: dict) -> dict | None:
    photos = [m for m in report.get("media_resolved") or [] if m.get("kind") == "photo"]
    return next((m for m in photos if m.get("role") == "hero"), photos[0] if photos else None)


def build_page(report: dict, registry: SourceRegistry, date_str: str) -> tuple[str, list]:
    title = f"ШКОЛА ЕДИНОРОГОВ — {date_str}. КОМПАНИЯ ДНЯ: {report['company_name']}"
    media = report.get("media_resolved") or []
    hero = hero_photo(report)
    charts = report_charts(report)
    nodes: list = []
    if report.get("subtitle"):
        nodes.append({"tag": "p", "children": [{"tag": "b", "children": [report["subtitle"]]}]})
    if report.get("facts_line"):
        nodes.append({"tag": "p", "children": [report["facts_line"]]})
    nodes.append({"tag": "p", "children": [{"tag": "i", "children": [
        f"Оценка полезности кейса: {report.get('usefulness_score', '—')}/10 (редакционная оценка). "
        "Материал образовательный, не является инвестиционной рекомендацией."]}]})
    if hero:
        nodes.append(_figure(hero))
    photos: dict[str, list] = {}
    for m in media:
        if m["kind"] == "photo" and m is not hero:
            sid = m.get("section") if m.get("section") in SECTION_IDS else "start"
            photos.setdefault(sid, []).append(_figure(m))
    for section in report["sections"]:
        sid = section.get("id")
        nodes.append({"tag": "h3", "children": [section.get("title") or SECTION_TITLES.get(sid, sid)]})
        if sid == "summary":
            for metric in report.get("metrics") or []:
                line: list = [{"tag": "b", "children": [str(metric.get("value"))]}, f" — {metric.get('label')}"]
                if metric.get("note"):
                    line += [{"tag": "br"}, {"tag": "i", "children": [str(metric["note"])]}]
                cite = _cite(registry, metric.get("source"))
                if cite:
                    line += [" ", cite]
                nodes.append({"tag": "p", "children": line})
        if sid == "investor_verdict" and report.get("verdict_scores"):
            nodes.append({"tag": "ul", "children": [
                {"tag": "li", "children": [{"tag": "b", "children": [f"{x.get('score')}/10"]}, f" — {x.get('label')}"]}
                for x in report["verdict_scores"]]})
        body = text_to_nodes(section.get("body", ""), registry, cite="number")
        nodes.extend(_interleave(body, photos.get(sid, [])))
        for i, ch in enumerate(charts):
            where = ch.get("section") if ch.get("section") in SECTION_IDS else "money"
            if where == sid:
                images = report.get("chart_images") or ([report.get("chart_image")] if report.get("chart_image") else [])
                nodes.extend(_chart_nodes(ch, images[i] if i < len(images) else None, registry))
        if sid == "money" and report.get("rounds"):
            headers = ["Раунд", "Сумма", "Pre-money", "Post-money", "Лид", "Другие", "Тип"]
            rows = [[f"{r.get('date', '')} · {r.get('stage', '')}", r.get("amount"), r.get("pre_money"),
                     r.get("post_money"), r.get("lead"), r.get("others"), r.get("type")] for r in report["rounds"]]
            nodes.append({"tag": "p", "children": [{"tag": "b", "children": ["Раунды финансирования"]}]})
            nodes.extend(table_nodes(headers, rows))
        if sid == "lesson":
            extras = [m for m in media if m["kind"] in ("video", "page")]
            if extras:
                nodes.append({"tag": "h3", "children": ["Интервью, видео и материалы"]})
                for m in extras:
                    if m["kind"] == "video":
                        nodes.append(_figure(m))
                    else:
                        nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": m["source_url"]},
                                                                "children": [m.get("caption") or m["source_url"]]}]})
    if report.get("unknowns"):
        nodes.append({"tag": "h3", "children": ["Что публично неизвестно"]})
        nodes.append({"tag": "ul", "children": [{"tag": "li", "children": [u]} for u in report["unknowns"]]})
    nodes.append({"tag": "h3", "children": ["Источники"]})
    nodes.append({"tag": "ul", "children": [
        {"tag": "li", "children": [f"[{s.id[1:]}] ", {"tag": "a", "attrs": {"href": s.url}, "children": [s.title or s.url]},
                                   f" — {s.publisher}" + (f", {s.date[:10]}" if s.date else "")]}
        for s in registry.sources.values()]})
    return title, nodes


def _size(nodes: list) -> int:
    return len(json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _pack(nodes: list, limit: int) -> list[list]:
    """Greedy packing by section; cuts only before a section heading unless one section alone is too big."""
    groups: list[list] = [[]]
    for node in nodes:
        if node.get("tag") == "h3" and groups[-1]:
            groups.append([])
        groups[-1].append(node)
    pages: list[list] = [[]]
    for group in groups:
        if pages[-1] and _size(pages[-1] + group) > limit:
            pages.append([])
        for node in group:
            if pages[-1] and _size(pages[-1] + [node]) > limit:
                pages.append([])
            pages[-1].append(node)
    return pages


def paginate(nodes: list, limit: int = TELEGRAPH_PAGE_BYTES) -> list[list]:
    """One page when it fits; otherwise the fewest parts, of similar size (no tiny tail page)."""
    total = _size(nodes)
    if total <= limit:
        return [nodes]
    parts = -(-total // limit)
    pages = _pack(nodes, min(limit, int(total / parts * 1.15)))
    return pages if len(pages) <= parts else _pack(nodes, limit)


def _first_heading(nodes: list, default: str) -> str:
    for node in nodes:
        if node.get("tag") == "h3":
            text = "".join(c for c in node.get("children", []) if isinstance(c, str)).strip()
            if text:
                return text
    return default


def create_pages(telegraph, title: str, nodes: list) -> str:
    """One Telegraph page, or linked parts when the issue is long. The link to the next part carries the title of
    the section it starts with (not "part 2"). Returns the first URL."""
    pages = paginate(nodes)
    next_url, next_name = None, ""
    for index in range(len(pages) - 1, -1, -1):
        content = list(pages[index])
        name = _first_heading(pages[index], "Продолжение")
        if next_url:
            content.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": next_url},
                                                      "children": [f"Читать дальше: {next_name} →"]}]})
        page_title = title if index == 0 else f"{name} · {title}"
        next_url, next_name = telegraph.create_page(page_title, content), name
    return next_url


def article_text(report: dict) -> str:
    """Everything the reader gets on the page, for the reading-time estimate."""
    parts = [report.get("subtitle", ""), report.get("facts_line", "")]
    parts += [f"{m.get('value', '')} {m.get('label', '')} {m.get('note', '')}" for m in report.get("metrics") or []]
    parts += [s.get("body", "") for s in report.get("sections") or []]
    parts += [str(u) for u in report.get("unknowns") or []]
    parts += [m.get("caption", "") for m in report.get("media_resolved") or []]
    return "\n".join(parts)


def build_post(report: dict, date_str: str) -> str:
    teaser = to_tg_html(re.sub(r"\[S\d+\]", "", report.get("teaser", "")))
    return (f"🦄 <b>ШКОЛА ЕДИНОРОГОВ</b> · {escape(date_str)}\n"
            f"<b>Компания дня: {escape(report['company_name'])}</b>\n\n"
            f"{teaser}\n\n"
            f"{reading_line(article_text(report))}")


def _send_post(app: App, report: dict, date_str: str, url: str) -> int:
    """Lead photo with the teaser as caption when there is one; plain text post otherwise."""
    html = build_post(report, date_str)
    button = ("📖 Читать разбор", url)
    hero = hero_photo(report)
    if hero and visible_len(html) <= CAPTION_LIMIT:
        try:
            return app.telegram.send_photo(app.target_chat(), hero["src"], html, button=button)
        except TelegramError as exc:
            log.warning("photo post failed (%s); sending text", exc)
    return app.telegram.send(app.target_chat(), html, button=button)


def _grow_glossary(app: App, report: dict) -> None:
    """New terms of the issue go into the pinned glossary. Never blocks the publication."""
    try:
        new = glossary.add_terms(app, glossary.clean_terms(report.get("glossary")))
        if new or not glossary.posted(app):
            glossary.sync(app)
    except Exception as exc:  # noqa: BLE001
        log.warning("glossary update failed: %s", exc)


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
    intent = f"unicorn_post_intent:{today}"
    if app.store.get(intent) == "sending":  # a crash between sending the post and recording it: never send twice
        app.store.draft_mark(today, "published", page_url=draft.get("page_url"), message_ids=[])
        app.notify_admin(f"Школа единорогов: процесс оборвался при отправке поста {today}. Пост мог уйти в канал, "
                         "повторно не отправляю. Проверьте канал.")
        return "unknown_sent"
    try:
        url = draft.get("page_url")
        if not url:
            prune_dead_media(report, app.http)  # dead pictures/videos are dropped just before the page is built
            title, nodes = build_page(report, registry, date_str)
            page_intent = f"unicorn_page_intent:{today}"
            if app.store.get(page_intent) == "creating":  # a crash after createPage, before page_url was saved
                try:
                    url = app.telegraph.find_page(title)
                except Exception as exc:  # noqa: BLE001
                    log.warning("page lookup failed: %s", exc)
            if not url:
                app.store.set(page_intent, "creating")
                url = create_pages(app.telegraph, title, nodes + growth_publish.begin(app, today, report))  # + CTA
        app.store.draft_mark(today, "ready", page_url=url)
        app.store.set(f"unicorn_page_intent:{today}", "")
        growth_publish.page_created(app, today, report, url)  # share block; never raises
        app.store.set(intent, "sending")
        mid = _send_post(app, report, date_str, url)
        app.store.set(intent, "sent")
    except Exception as exc:  # noqa: BLE001
        app.store.set(intent, "")  # the call failed (not a crash): a retry may send
        app.store.draft_mark(today, "ready", error=str(exc)[:500])
        app.notify_admin(f"Школа единорогов: публикация {today} не удалась: {exc}")
        return "failed"
    app.store.draft_mark(today, "published", page_url=url, message_ids=[mid])
    growth_publish.post_sent(app, today, report, url, mid)  # message id + "Отправить другу" button; never raises
    chosen = payload.get("chosen", {})
    app.store.archive_add(report["company_name"], report.get("industry") or chosen.get("industry", ""),
                          report.get("country") or chosen.get("country", ""), today, url,
                          ai=bool(chosen.get("ai_core")))
    tomorrow = poll.next_issue(today)  # readers vote on the topic of the next issue right after this one
    poll.post(app, tomorrow, payload.get("poll_options") or poll.make_options(app, tomorrow))
    _grow_glossary(app, report)  # after the poll: the issue post and the poll keep their places in the channel
    return "published"
