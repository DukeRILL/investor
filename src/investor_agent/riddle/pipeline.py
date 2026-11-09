"""«Разгадка бизнеса»: a documentary business investigation, prepared off-peak at night and published at 11:00 Moscow
(16:00 Beijing). For now it goes to the admin chat only (RIDDLE_PUBLISH_MODE=draft, docs/RULES.md §5).

One DeepSeek research call chooses the company, researches it and writes the issue (prompt business_riddle.md), one
more call fixes validation problems. Pictures and videos are found and checked by code. An issue without 12 images or
3 YouTube videos is BLOCKED: kept as a draft, never published, and the admin gets the list of what is missing.
"""

import json
import logging
import re
from datetime import datetime
from html import escape
from pathlib import Path

from ..app import App
from ..chart import render_chart
from ..media import resolve_media
from ..quality import is_russian, strip_meta_notes
from ..sources import CITE_RE, SourceRegistry
from ..storage import norm_name
from ..telegram import TelegramError
from ..textfmt import minutes_ru, text_to_nodes, visible_len
from ..timeutil import PeakHoursError, now_bj, ru_date
from ..growth import publish as growth_publish
from ..unicorn.pipeline import _channel_button, _figure, logo_url, _interleave, _registry_from, _uploader, create_pages
from ..unicorn.precheck import image_alive, video_alive
from . import factcheck, visuals, vk

log = logging.getLogger(__name__)

RUBRIC = "Разгадка бизнеса"
# Owner's master prompt «редакционная стратегия» of 2026-11-09 (prompts/business_riddle.md, docs/RULES.md §5).
ACTS = ["riddle", "before", "first_steps", "turn", "solution", "evidence", "flip_side", "cis", "discovery"]
VERDICTS = ("ПОВТОРИТЬ РЕАЛИСТИЧНО", "ТРЕБУЕТ ПРОВЕРКИ", "КОПИРОВАТЬ НЕЦЕЛЕСООБРАЗНО")
VIDEO_PLACE = {"founders": "before", "product": "turn", "market": "solution"}
MIN_IMAGES = 12      # §12: at least 12 different images inside the published article
MAX_IMAGES = 16
MIN_VIDEOS = 3       # §13: at least 3 different working YouTube videos inside the article
MIN_WORDS, MAX_WORDS = 2400, 3700   # §15 (08:28 UTC edition): target 2,700–3,200 words; slack both ways, the reading time rules
MIN_READ_MINUTES, MAX_READ_MINUTES = 15.0, 20.0   # owner's rule: words / 180 + 1.5 (12 images) + 0.1 per image above 12
READ_WPM = 180
MIN_SECTION_WORDS = 120
CIS_WORDS = (100, 210)             # §11: the CIS block is about 120–180 words
MAX_ANALOGS = 3
MIN_SOURCES = 7
MAX_PUBLIC_SOURCES = 10            # §19: 6–10 key sources in the public list
MIN_QUALITY = 8.0                  # §20: weighted editorial score; below it, or with repeats, the issue is sent back
QUALITY_WEIGHTS = {"interest": 0.20, "depth": 0.20, "mindset": 0.20, "analytics": 0.20, "clarity": 0.10, "shareability": 0.10}
TEASER_MIN, TEASER_MAX = 350, 700  # §17: about 400–650 characters
CAPTION_LIMIT = 1000
SEARCH_CACHE_HOURS = 24
MAX_OUTPUT_TOKENS = 64000          # the 3,000-word article as JSON is far over the API default of 4,096 tokens (cut the first run)
ARCHIVE_KEY = "riddle_archive"
PREVIEW_FILE = Path(__file__).with_name("preview.jpg")  # owner template with fields for logo, name, sector (2026-11-09)
COVER_FILE = Path(__file__).with_name("cover.jpg")  # owner's rubric cover (2026-11-09): the picture of every post
COVER_KEY = "riddle_cover_url"
HEADER_FILE = Path(__file__).with_name("header.jpg")  # owner's article header (2026-11-09), 1000 px JPEG ≈200 KB
HEADER_KEY = "riddle_header_url_v3"
LICENSE_PAGE = "кадр со страницы-источника; права на использование не подтверждены — проверить перед публикацией в канал"
LICENSE_OWN = "собственная визуализация по данным выпуска"
LICENSE_PIXABAY = "Pixabay Content License (бесплатно, коммерческое использование)"
LICENSE_AI = "сгенерировано Cloudflare FLUX, иллюстрация"


class RiddleError(RuntimeError):
    pass


def draft_key(day: str) -> str:
    return f"riddle:{day}"


def issue_date(now: datetime | None = None) -> str:
    return (now or now_bj()).date().isoformat()


# --- archive ----------------------------------------------------------------------------------

def archive(app: App) -> list[dict]:
    try:
        return json.loads(app.store.get(ARCHIVE_KEY) or "[]")
    except ValueError:
        return []


def _archive_add(app: App, report: dict, day: str, url: str) -> None:
    items = [a for a in archive(app) if norm_name(a.get("name", "")) != norm_name(report["company_name"])]
    items.insert(0, {"name": report["company_name"], "industry": report.get("industry", ""),
                     "country": report.get("country", ""), "date": day, "url": url})
    app.store.set(ARCHIVE_KEY, json.dumps(items, ensure_ascii=False))


def excluded_names(app: App) -> set[str]:
    """Companies already covered by this rubric or by Unicorn School are never repeated."""
    return {norm_name(a["name"]) for a in archive(app) if a.get("name")} | app.store.archived_names()


# --- validation -------------------------------------------------------------------------------

def _words(text: str) -> int:
    return len(re.findall(r"\w+", re.sub(r"\[S\d+\]", " ", text or "")))


def clean_report(report: dict, registry: SourceRegistry) -> None:
    def clean(text: str) -> str:
        return strip_meta_notes(CITE_RE.sub(lambda m: m.group(0) if registry.get(m.group(1)) else "", text or ""))
    for section in report.get("sections") or []:
        if isinstance(section, dict):
            section["body"] = clean(section.get("body", ""))
    report["teaser"] = re.sub(r"\s*\[S\d+\]", "", clean(report.get("teaser", ""))).strip()


def sections_by_act(report: dict) -> dict[str, dict]:
    return {s.get("act"): s for s in report.get("sections") or [] if isinstance(s, dict) and s.get("act") in ACTS}


def validate_story(report: dict, registry: SourceRegistry, excluded: set[str] = frozenset()) -> list[str]:
    """Content rules of the master prompt that code can check. Media are checked later, on real pictures."""
    errors: list[str] = []
    name = str(report.get("company_name", "")).strip()
    if not name:
        errors.append("company_name is empty")
    elif norm_name(name) in excluded:
        errors.append(f"{name} is in `excluded` (already covered): choose another company")
    concept = report.get("concept") or {}
    if not concept.get("riddle") or len([h for h in concept.get("headlines") or [] if h]) < 3:
        errors.append("concept: give the riddle and three headlines (парадокс, скрытый механизм, неожиданное решение)")
    angles = [a for a in (report.get("editorial_notes") or {}).get("angles") or [] if isinstance(a, dict) and a.get("scores")]
    if len(angles) < 3:
        errors.append("editorial_notes.angles: form three different angles and score each (surprise, story, evidence, viral)")
    score = quality_score(report)
    if score is None:
        errors.append("quality: run the editorial audit and give the five scores")
    elif score < MIN_QUALITY:
        errors.append(f"editorial audit total is {score}, below {MIN_QUALITY}: revise the weakest parts")
    if (report.get("quality") or {}).get("repeats_found"):
        errors.append("the editorial audit found repeated meaning: keep the strongest explanation of each thesis, "
                      "delete the duplicates")
    if not str(report.get("discussion_question", "")).strip():
        errors.append("discussion_question is empty")
    if not str(report.get("headline", "")).strip():
        errors.append("headline is empty")
    if not str(report.get("category", "")).strip() or not str(report.get("business_type", "")).strip():
        errors.append("give `category` (the section/industry of the company) and `business_type` (its type of business)")
    acts = sections_by_act(report)
    for act in ACTS:
        if _words((acts.get(act) or {}).get("body", "")) < MIN_SECTION_WORDS:
            errors.append(f"section with act '{act}' is missing or shorter than {MIN_SECTION_WORDS} words")
    text = "\n".join(s.get("body", "") for s in acts.values())
    words = _words(text)
    if words < MIN_WORDS:
        errors.append(f"the story has {words} words, need 2,700–3,200: deepen the episodes with confirmed facts, decisions and "
                      "actions of the founders, not with filler")
    elif words > MAX_WORDS:
        errors.append(f"the story has {words} words, need 2,700–3,200: cut repeats and secondary chronology, keep the founders' thinking")
    minutes = read_minutes(report)
    if not MIN_READ_MINUTES <= minutes <= MAX_READ_MINUTES:
        errors.append(f"the page reads for {minutes} minutes (words/180 + 1.5 for 12 images), required 15–20")
    cis_words = _words((acts.get("cis") or {}).get("body", ""))
    if acts.get("cis") and not (CIS_WORDS[0] <= cis_words <= CIS_WORDS[1]):
        errors.append(f"the cis section has {cis_words} words, need 100–180")
    if text and not is_russian(text):
        errors.append("text must be in Russian")
    teaser = report.get("teaser", "")
    if not (TEASER_MIN <= len(teaser) <= TEASER_MAX):
        errors.append(f"teaser has {len(teaser)} characters, need 400–650")
    elif not is_russian(teaser):
        errors.append("teaser must be in Russian")
    if any(v in teaser.upper() for v in VERDICTS):
        errors.append("teaser must not reveal the CIS verdict")
    if len([c for c in report.get("mechanism_chain") or [] if isinstance(c, dict) and c.get("text")]) < 5:
        errors.append("mechanism_chain: give the six links need → solution → why chosen → revenue → growth → advantage")
    cis = report.get("cis") or {}
    analogs = [a for a in cis.get("analogs") or [] if isinstance(a, dict) and a.get("name")]
    unsourced = [a["name"] for a in analogs if not registry.get(str(a.get("source", "")))]
    if unsourced:
        errors.append(f"cis.analogs without a source id of a page you found: {', '.join(unsourced)}")
    if not analogs and not str(cis.get("no_direct_note", "")).strip():
        errors.append("cis.analogs: up to 3 real analogues in Russia/CIS, or say in no_direct_note that there are none")
    if len(analogs) > MAX_ANALOGS:
        errors.append(f"cis.analogs: at most {MAX_ANALOGS} closest analogues")
    if str(cis.get("verdict", "")).strip().upper() not in VERDICTS:
        errors.append("cis.verdict must be exactly one of: " + ", ".join(VERDICTS))
    verified = registry.verified(registry.cited_ids(text))
    if len(verified) < MIN_SOURCES:
        errors.append(f"only {len(verified)} cited sources were opened with fetch_page; need at least {MIN_SOURCES}")
    if len([v for v in report.get("videos") or [] if isinstance(v, dict)]) < MIN_VIDEOS:
        errors.append("videos: nominate at least 3 (better 4–5) real YouTube videos found with web_search")
    return errors


def quality_score(report: dict) -> float | None:
    """§20: 0.20·interest + 0.20·depth + 0.20·mindset + 0.20·analytics + 0.10·clarity + 0.10·shareability (internal)."""
    q = report.get("quality") or {}
    try:
        return round(sum(float(q[k]) * w for k, w in QUALITY_WEIGHTS.items()), 2)
    except (KeyError, TypeError, ValueError):
        return None


# --- media ------------------------------------------------------------------------------------

def _image(src: str, kind: str, act: str, caption: str, source_url: str = "", publisher: str = "",
           license_: str = LICENSE_OWN, function: str = "", role: str = "inline") -> dict:
    return {"kind": "photo", "src": src, "type": kind, "act": act if act in ACTS else "before", "caption": caption,
            "source_url": source_url, "publisher": publisher, "license": license_, "function": function, "role": role}


def build_media(app: App, report: dict, registry: SourceRegistry) -> None:
    """report['images'] and report['videos_resolved']: real, uploaded and checked. Never raises."""
    upload = _uploader(app)
    images: list[dict] = []
    seen: set[str] = set()

    def add(item: dict | None) -> None:
        if item and item["src"] and item["src"] not in seen and len(images) < MAX_IMAGES:
            seen.add(item["src"])
            images.append(item)

    # 1. real materials from the pages the model opened
    nominated = [{**m, "section": m.get("act"), "kind": "photo"} for m in report.get("media") or [] if isinstance(m, dict)]
    meta = {str(m.get("source")): m for m in nominated}
    try:
        for m in resolve_media(nominated, registry, app.http, upload, max_items=MAX_IMAGES):
            if m.get("kind") == "photo":
                extra = meta.get(m.get("source_id"), {})
                add(_image(m["src"], extra.get("type") or "photo", m.get("section"), m.get("caption", ""),
                           m.get("source_url", ""), m.get("publisher", ""), LICENSE_PAGE, extra.get("function", ""),
                           "hero" if m.get("role") == "hero" else "inline"))
    except Exception as exc:  # noqa: BLE001
        log.warning("riddle real media failed: %s", type(exc).__name__)

    def own(png: bytes | None, kind: str, act: str, caption: str, function: str, source=None) -> None:
        if not png:
            return
        try:
            url = upload(png, "image/png")
        except Exception as exc:  # noqa: BLE001
            log.warning("scheme upload failed: %s", type(exc).__name__)
            return
        src = registry.get(str(source or "").split(",")[0].strip())
        add(_image(url, kind, act, caption, src.url if src else "", src.publisher if src else "", LICENSE_OWN, function))

    # 2. visualisation of verified facts
    for ch in [c for c in report.get("charts") or [] if isinstance(c, dict) and c.get("points")][:3]:
        own(render_chart(ch["points"], ch.get("unit", ""), ch.get("kind", "line")), "график", ch.get("act") or "evidence",
            f"{ch.get('title', 'Динамика')}" + (f" ({ch['unit']})" if ch.get("unit") else ""), "доказательство в цифрах",
            ch.get("source"))
    own(visuals.render_chain(report.get("mechanism_chain") or []), "схема", "solution",
        "Разгадка в одной схеме: от потребности клиента до преимущества", "механизм успеха")
    own(visuals.render_timeline(report.get("timeline") or []), "хронология", "first_steps",
        "Ключевые события компании", "развитие компании")
    # no picture for the CIS block: its compact text list would repeat it (§9, ban on repeated meaning)

    # 3. stock photos, 4. AI illustrations — only while the article is short of 12 pictures
    used: set[str] = set()
    for s in report.get("stock") or []:
        if len(images) >= MIN_IMAGES or not isinstance(s, dict):
            break
        got = visuals.pixabay_image(app.http, app.settings.pixabay_api_key, str(s.get("query", "")), used)
        if got:
            try:
                url = upload(got[0], got[1])
            except Exception:  # noqa: BLE001
                continue
            add(_image(url, "сток", s.get("act"), f"{s.get('caption', '')} Фото: Pixabay".strip(), got[2]["page"],
                       "Pixabay", LICENSE_PIXABAY, s.get("function", "")))
    for a in report.get("ai_images") or []:
        if len(images) >= MIN_IMAGES or not isinstance(a, dict):
            break
        raw = visuals.flux_image(app.http, app.settings.cloudflare_account_id, app.settings.cloudflare_api_token,
                                 str(a.get("prompt", "")))
        if raw:
            try:
                url = upload(raw, "image/jpeg")
            except Exception:  # noqa: BLE001
                continue
            add(_image(url, "ИИ-иллюстрация", a.get("act"), f"{a.get('caption', '')} (иллюстрация создана ИИ)".strip(),
                       "", "", LICENSE_AI, a.get("function", "")))
    for i, img in enumerate(images, 1):
        img["id"] = f"IMG{i:02d}"
    report["images"] = images

    # YouTube videos, checked through oEmbed (exists and embeddable)
    vids = [{"source": v.get("source"), "video_url": v.get("video_url"), "kind": "video",
             "role": v.get("purpose") if v.get("purpose") in VIDEO_PLACE else "market",
             "section": VIDEO_PLACE.get(v.get("purpose"), "evidence"),
             "caption": ("Контекст отрасли. " if v.get("context") else "") + str(v.get("note") or "")}
            for v in report.get("videos") or [] if isinstance(v, dict)]
    try:
        resolved = resolve_media(vids, registry, app.http, upload, max_items=6)
    except Exception as exc:  # noqa: BLE001
        log.warning("riddle videos failed: %s", type(exc).__name__)
        resolved = []
    report["videos_resolved"] = [{**m, "purpose": m.get("role"), "act": m.get("section"), "status": "oEmbed: доступно"}
                                 for m in resolved if m.get("kind") == "video" and "youtube.com" in m.get("video_url", "")]
    log.info("riddle media: %d images, %d videos", len(images), len(report["videos_resolved"]))


def hero(report: dict) -> dict | None:
    real = [m for m in report.get("images") or [] if m.get("license") == LICENSE_PAGE]
    return next((m for m in real if m.get("role") == "hero"), real[0] if real else None)


def media_problems(report: dict) -> list[str]:
    missing = []
    n_img = len({m["src"] for m in report.get("images") or []})
    n_vid = len({m["video_url"] for m in report.get("videos_resolved") or []})
    if n_img < MIN_IMAGES:
        missing.append(f"изображений {n_img} из {MIN_IMAGES}")
    if n_vid < MIN_VIDEOS:
        missing.append(f"проверенных видео YouTube {n_vid} из {MIN_VIDEOS}")
    return missing


def manifests(report: dict) -> dict:
    """C. MEDIA MANIFEST and D. YOUTUBE MANIFEST of the master prompt."""
    return {
        "media": [{k: m.get(k) for k in ("id", "src", "type", "caption", "source_url", "license", "act", "function")}
                  for m in report.get("images") or []],
        "youtube": [{"url": v.get("video_url"), "title": v.get("video_title"), "channel": v.get("publisher"),
                     "purpose": v.get("purpose"), "status": v.get("status")} for v in report.get("videos_resolved") or []],
    }


# --- page and post ----------------------------------------------------------------------------

def _cis_nodes(cis: dict, registry: SourceRegistry) -> list:
    """Compact CIS block (§11): up to three analogues with links, the verdict and an optional hypothesis."""
    nodes: list = []
    items = []
    for a in [a for a in cis.get("analogs") or [] if isinstance(a, dict) and a.get("name")][:MAX_ANALOGS]:
        src = registry.get(str(a.get("source", "")))
        name = {"tag": "a", "attrs": {"href": src.url}, "children": [a["name"]]} if src else a["name"]
        tail = "; ".join(x for x in (a.get("country"), a.get("essence") or a.get("product"),
                                     f"сходство: {a['similarity']}" if a.get("similarity") else "",
                                     f"отличие: {a['difference']}" if a.get("difference") else "") if x)
        items.append({"tag": "li", "children": [{"tag": "b", "children": [name]}, " — " + tail]})
    if items:
        nodes.append({"tag": "ul", "children": items})
    elif cis.get("no_direct_note"):
        nodes.append({"tag": "p", "children": [{"tag": "i", "children": [cis["no_direct_note"]]}]})
    line: list = [{"tag": "b", "children": [f"Вывод: {cis.get('verdict', '')}."]}]
    if str(cis.get("hypothesis") or "").strip():
        line += [" Гипотеза: ", str(cis["hypothesis"]).strip()]
    nodes.append({"tag": "p", "children": line})
    return nodes


def public_sources(report: dict, registry: SourceRegistry) -> list:
    """§19: the 5–8 key sources — the ones the story cites most; the full list stays in the research notes."""
    text = "\n".join(s.get("body", "") for s in report.get("sections") or [] if isinstance(s, dict))
    counts: dict[str, int] = {}
    for sid in CITE_RE.findall(text):
        counts[sid] = counts.get(sid, 0) + 1
    ranked = sorted((sid for sid in counts if registry.get(sid)), key=lambda sid: (-counts[sid], int(sid[1:])))
    return sorted((registry.get(sid) for sid in ranked[:MAX_PUBLIC_SOURCES]), key=lambda s: int(s.id[1:]))


def channel_url(app: App) -> str:
    chat = app.settings.telegram_chat_id
    return f"https://t.me/{chat.lstrip('@')}" if chat.startswith("@") else "https://t.me/investor_duker"


def build_page(report: dict, registry: SourceRegistry, date_str: str, channel: str,
               header: str | None = None, button: str | None = None, subscribe: list | None = None) -> tuple[str, list]:
    title = f"{RUBRIC}: {report['headline']}"[:256]
    top = hero(report)
    by_act: dict[str, list] = {}
    for m in report.get("images") or []:
        if m is not top:
            by_act.setdefault(m["act"], []).append(_figure(m))
    videos: dict[str, list] = {}
    for v in report.get("videos_resolved") or []:
        videos.setdefault(v.get("act") or "evidence", []).append(_figure({**v, "kind": "video"}))
    nodes: list = [{"tag": "figure", "children": [{"tag": "img", "attrs": {"src": header}}]}] if header else []
    nodes.append({"tag": "p", "children": [{"tag": "i", "children": [f"«{RUBRIC}» · Школа единорогов · {date_str}"]}]})
    if top:
        nodes.append(_figure(top))
    acts = sections_by_act(report)
    for act in ACTS:
        section = acts.get(act)
        if not section:
            continue
        nodes.append({"tag": "h3", "children": [section.get("title") or act]})
        nodes.extend(_interleave(text_to_nodes(section.get("body", ""), registry, cite="number"), by_act.pop(act, [])))
        if act == "cis":
            nodes.extend(_cis_nodes(report.get("cis") or {}, registry))
        for v in videos.pop(act, []):
            nodes.append(v)
    for rest in list(by_act.values()) + list(videos.values()):  # pictures of unknown acts still make the page
        nodes.extend(rest)
    nodes.append({"tag": "h3", "children": ["Главные источники"]})
    nodes.append({"tag": "ul", "children": [
        {"tag": "li", "children": [f"[{s.id[1:]}] ", {"tag": "a", "attrs": {"href": s.url}, "children": [s.title or s.url]},
                                   f" — {s.publisher}" + (f", {s.date[:10]}" if s.date else "")]}
        for s in public_sources(report, registry)]})
    if not subscribe:  # growth off: the old one-line invitation before the banner
        nodes.append({"tag": "p", "children": ["Новые расследования каждый день — подписывайтесь на ",
                                               {"tag": "a", "attrs": {"href": channel}, "children": ["«Школу единорогов»"]}, "."]})
    nodes.extend(_channel_button(button, channel))  # the same button as at the end of Unicorn School articles
    nodes.extend(subscribe or [])  # banner (personal link), the channel line, then the subscribe text
    return title, nodes


def count_page_media(nodes: list, ignore: set[str] = frozenset()) -> tuple[int, int]:
    """(unique images, unique YouTube embeds) in Telegraph nodes; the rubric header is not a story picture."""
    imgs, vids = set(), set()

    def walk(items):
        for n in items or []:
            if not isinstance(n, dict):
                continue
            src = (n.get("attrs") or {}).get("src", "")
            if n.get("tag") == "img" and src and src not in ignore:
                imgs.add(src)
            elif n.get("tag") == "iframe" and "youtube" in src:
                vids.add(src)
            walk(n.get("children"))
    walk(nodes)
    return len(imgs), len(vids)


def read_minutes(report: dict, n_images: int | None = None) -> float:
    """Owner's model (2026-11-09): words / 180 + 1.5 min for studying 12 images, +0.1 per content image above 12.
    Counts the headline, section texts and meaningful captions; not notes, manifests, sources or video watch time."""
    n = n_images if n_images is not None else max(12, len(report.get("images") or []))
    words = len(re.findall(r"\w+", re.sub(r"\[S\d+\]", " ", article_text(report))))
    return round(words / READ_WPM + 1.5 + 0.1 * max(0, n - 12), 1)


def article_text(report: dict) -> str:
    """Everything the reader gets on the page, for the reading time (and the 20-minute ceiling)."""
    parts = [str(report.get("headline") or "")]
    parts += [s.get("title", "") + "\n" + s.get("body", "") for s in report.get("sections") or [] if isinstance(s, dict)]
    cis = report.get("cis") or {}
    parts += [" ".join(str(a.get(k, "")) for k in ("name", "essence", "similarity", "difference"))
              for a in cis.get("analogs") or [] if isinstance(a, dict)]
    parts += [str(cis.get("hypothesis") or "")]
    parts += [m.get("caption", "") for m in (report.get("images") or []) + (report.get("videos_resolved") or [])]
    return "\n".join(parts)


def build_post(report: dict, date_str: str, channel_name: str = "", question: bool = True) -> str:
    """Owner's rule (2026-11-09): the post starts straight with the headline, no lines above it; then the teaser, the
    discussion question (§17), the reading time. Section, type and rubric name live on the picture."""
    q = str(report.get("discussion_question") or "").strip()
    return (f"<b>{escape(report['headline'])}</b>\n\n"
            f"{escape(report.get('teaser', ''))}\n\n"
            + (f"💬 <i>{escape(q)}</i>\n\n" if q and question else "")
            + f"⏱ Время чтения: {minutes_ru(max(1, round(read_minutes(report))))}")


def publication_status(report: dict, page_check: tuple[int, int] | None = None) -> dict:
    """I. PUBLICATION STATUS."""
    missing = media_problems(report)
    minutes = read_minutes(report)
    if not MIN_READ_MINUTES <= minutes <= MAX_READ_MINUTES:
        missing.append(f"расчётное время чтения {minutes} мин, нужно 15–20")
    if page_check is not None:
        img, vid = page_check[0], page_check[1]
        if img < MIN_IMAGES:
            missing.append(f"на опубликованной странице изображений {img} из {MIN_IMAGES}")
        if vid < MIN_VIDEOS:
            missing.append(f"на опубликованной странице видео {vid} из {MIN_VIDEOS}")
    fc = report.get("fact_check")
    if not isinstance(fc, dict) or not fc.get("checked"):
        missing.append("независимый фактчек не выполнен")
    elif fc.get("open"):
        missing.append(f"фактчек: {len(fc['open'])} существенных утверждений в статусе ERROR/UNVERIFIED — "
                       + "; ".join(fc["open"][:3]))
    if page_check is not None and len(page_check) > 2 and report.get("verified_numbers"):
        lost = sorted(set(report["verified_numbers"]) - factcheck.numbers(page_check[2]))
        if lost:
            missing.append("опубликованная страница расходится с проверенной версией, нет чисел: " + ", ".join(lost[:8]))
    limits = [str(x) for x in (report.get("research_notes") or {}).get("limits") or [] if x]
    return {"status": "BLOCKED" if missing else "READY", "missing": missing, "limits": limits,
            "quality": quality_score(report),
            "images": len(report.get("images") or []), "videos": len(report.get("videos_resolved") or []),
            "rights_to_check": sum(1 for m in report.get("images") or [] if m.get("license") == LICENSE_PAGE),
            "fact_check": {k: v for k, v in (fc or {}).items() if k != "open"} if isinstance(fc, dict) else {}}


# --- jobs -------------------------------------------------------------------------------------

def prepare(app: App, today: str | None = None, company: str | None = None) -> str:
    """Night job. Returns ready | blocked | exists | locked | failed. Raises PeakHoursError to be retried later."""
    today = today or issue_date()
    existing = app.store.draft_get(draft_key(today))
    if existing and existing["status"] in ("ready", "published"):
        return "exists"
    if not app.store.acquire("riddle_prepare", minutes=180):
        return "locked"
    try:
        return _prepare(app, today, company)
    except PeakHoursError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("riddle prepare failed")
        app.store.draft_save(draft_key(today), "failed", {}, error=str(exc)[:500])
        app.notify_admin(f"{RUBRIC}: подготовка на {today} не удалась: {exc}")
        return "failed"
    finally:
        app.record_usage("riddle_prepare")
        app.store.release("riddle_prepare")


def _prepare(app: App, today: str, company: str | None = None) -> str:
    s, ds = app.settings, app.deepseek
    registry = SourceRegistry()
    excluded = excluded_names(app)
    payload = {"date": today,
               "excluded": sorted({a["name"] for a in archive(app)} | {a["name"] for a in app.store.archive_list()}),
               "recent": [{"industry": a.get("industry"), "country": a.get("country")} for a in archive(app)[:7]]}
    if company:  # the owner chose the company (manual run): skip Step 1
        payload["company_chosen_by_editor"] = company
    report = ds.research(s.deepseek_model_pro, app.prompt("business_riddle"), payload, registry, max_tool_calls=60,
                         cache_ttl=SEARCH_CACHE_HOURS, max_tokens=MAX_OUTPUT_TOKENS)
    clean_report(report, registry)
    errors = validate_story(report, registry, excluded)
    if errors:
        report = ds.research(s.deepseek_model_pro, app.prompt("business_riddle"), {
            **payload, "known_sources": registry.brief(), "previous_draft": report, "fix_these_problems": errors,
            "already_cited": registry.cited_ids(*[x.get("body", "") for x in report.get("sections") or []
                                                  if isinstance(x, dict)]),
        }, registry, max_tool_calls=25, cache_ttl=SEARCH_CACHE_HOURS, max_tokens=MAX_OUTPUT_TOKENS)
        clean_report(report, registry)
        errors = validate_story(report, registry, excluded)
    if errors:
        raise RiddleError("расследование не прошло проверку: " + "; ".join(errors))
    build_media(app, report, registry)
    report["fact_check"] = factcheck.run(app, report, registry)  # §14–19: independent check of the FINAL text
    report["verified_numbers"] = sorted(factcheck.numbers(CITE_RE.sub(" ", article_text(report))))  # compared with the page later
    status = publication_status(report)
    used = set(registry.cited_ids(*[x.get("body", "") for x in report["sections"]]))
    used |= {str(a.get("source")) for a in (report.get("cis") or {}).get("analogs") or [] if registry.get(str(a.get("source", "")))}
    used |= {m.get("source_id") for m in report.get("videos_resolved") or [] if m.get("source_id")}
    used |= {str(report["website_source"])} if registry.get(str(report.get("website_source", ""))) else set()  # the logo is found on it at publish time
    sources = registry.brief(sorted((u for u in used if u), key=lambda x: int(x[1:])))
    state = "ready" if status["status"] == "READY" else "blocked"
    vk_post = _vk_post(app, report) if state == "ready" and app.settings.vk_n8n_webhook_url else None
    app.store.draft_save(draft_key(today), state, {"report": report, "sources": sources, "status": status,
                                                   "manifests": manifests(report), "vk_post": vk_post})
    if state == "blocked":
        app.notify_admin(f"{RUBRIC} на {today}: BLOCKED ({report['company_name']}) — не хватает: "
                         + "; ".join(status["missing"]) + ". Материал сохранён черновиком, следующая попытка через 3 часа.")
    return state


def _hosted(app: App, key: str, path: Path) -> str | None:
    """A rubric picture uploaded to the image host once and remembered."""
    url = app.store.get(key)
    if url:
        return url
    try:
        url = _uploader(app)(path.read_bytes(), "image/jpeg")
    except Exception as exc:  # noqa: BLE001 - the post / page then go without it
        log.warning("riddle picture upload failed: %s", type(exc).__name__)
        return None
    app.store.set(key, url)
    return url


def cover_url(app: App) -> str | None:
    return _hosted(app, COVER_KEY, COVER_FILE)


def preview_url(app: App, report: dict, registry: SourceRegistry) -> str | None:
    """Post picture (owner's rule, 2026-11-09): the rubric picture with the company's logo, name and sector.
    Falls back to the plain rubric picture when the preview cannot be drawn or uploaded."""
    try:
        site = registry.get(str(report.get("website_source", "")))
        logo = visuals.find_logo(app.http, site.url) if site else None
        png = visuals.render_preview(PREVIEW_FILE.read_bytes(), report.get("company_name", ""),
                                     report.get("category") or report.get("industry") or "", logo)
        if png:
            return _uploader(app)(png, "image/jpeg")
    except Exception as exc:  # noqa: BLE001
        log.warning("riddle preview failed: %s", type(exc).__name__)
    return cover_url(app)


BUTTON_KEY = "channel_button_url"


def button_url(app: App) -> str | None:
    """The channel button picture (shared with Unicorn School), uploaded once and remembered."""
    url = app.store.get(BUTTON_KEY)
    if not url:
        url = logo_url(app)
        if url:
            app.store.set(BUTTON_KEY, url)
    return url


def header_url(app: App) -> str | None:
    return _hosted(app, HEADER_KEY, HEADER_FILE)


def _send_post(app: App, report: dict, date_str: str, url: str, registry: SourceRegistry) -> int:
    html = build_post(report, date_str)
    if visible_len(html) > CAPTION_LIMIT:  # the picture matters more than the question: drop it to fit the caption
        html = build_post(report, date_str, question=False)
    button = ("🔎 Читать расследование", url)
    chat = app.target_chat(riddle=True)
    top = hero(report)
    picture = preview_url(app, report, registry) or (top or {}).get("src")
    if picture and visible_len(html) <= CAPTION_LIMIT:
        try:
            return app.telegram.send_photo(chat, picture, html, button=button)
        except TelegramError as exc:
            log.warning("riddle photo post failed (%s); sending text", exc)
    return app.telegram.send(chat, html, button=button)


def _prune_dead(app: App, report: dict) -> None:
    report["images"] = [m for m in report.get("images") or [] if image_alive(app.http, m.get("src", ""))]
    report["videos_resolved"] = [v for v in report.get("videos_resolved") or []
                                 if video_alive(app.http, v.get("video_url", ""))]


def _verify_pages(app: App, url: str) -> tuple[int, int, str]:
    """Read the created page (and its continuation parts) back from Telegraph: images, videos and the text
    (compared with the fact-checked version, §19)."""
    imgs = vids = 0
    texts: list[str] = []
    seen = set()
    while url and url not in seen and len(seen) < 5:
        seen.add(url)
        nodes = app.telegraph.get_page(url)
        i, v = count_page_media(nodes, {app.store.get(HEADER_KEY) or "", app.store.get(BUTTON_KEY) or ""})
        imgs, vids = imgs + i, vids + v
        texts.append(factcheck.page_text(nodes))
        url = _next_part(nodes)
    return imgs, vids, "\n".join(texts)


def _vk_post(app: App, report: dict) -> dict | None:
    """VK text is written now (off-peak); a failure never blocks the Telegram issue."""
    try:
        result = vk.build(app, report)
    except Exception as exc:  # noqa: BLE001
        log.warning("riddle vk post failed: %s", type(exc).__name__)
        app.notify_admin(f"{RUBRIC}: текст для VK не получился ({type(exc).__name__}), выпуск в Telegram не затронут.")
        return None
    if result["errors"]:
        app.notify_admin(f"{RUBRIC}: текст для VK не прошёл проверку — " + "; ".join(result["errors"]))
        return None
    return result


def _send_vk(app: App, draft_payload: dict, day: str, url: str) -> None:
    """Hand the ready post to n8n. Idempotent on the n8n side via guid (company + day). Never raises."""
    hook, post = app.settings.vk_n8n_webhook_url, (draft_payload.get("vk_post") or {})
    if not hook or not post.get("text"):
        return
    try:
        app.http.request("POST", hook, json_body={
            "guid": f"riddle-{day}", "text": post["text"], "article_url": url, "channel_url": channel_url(app),
            "company": draft_payload["report"].get("company_name"), "cover_url": cover_url(app)},
                         headers={"X-Webhook-Secret": app.settings.vk_n8n_webhook_secret}, retry=False, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.warning("riddle vk handoff failed: %s", type(exc).__name__)
        app.notify_admin(f"{RUBRIC}: VK-ветка не получила выпуск {day}: {type(exc).__name__}")


def _next_part(nodes: list) -> str | None:
    for n in reversed(nodes or []):
        if isinstance(n, dict):
            for c in n.get("children") or []:
                if isinstance(c, dict) and c.get("tag") == "a" and "Читать дальше" in "".join(
                        x for x in c.get("children") or [] if isinstance(x, str)):
                    return (c.get("attrs") or {}).get("href")
    return None


def publish(app: App, today: str | None = None) -> str:
    """11:00 Moscow job (16:00 Beijing). No DeepSeek calls. Draft mode posts to the admin chat."""
    today = today or issue_date()
    key = draft_key(today)
    draft = app.store.draft_get(key)
    if draft and draft["status"] == "published":
        return "already_published"
    if not draft or draft["status"] != "ready":
        why = "выпуск BLOCKED: " + "; ".join((draft["payload"].get("status") or {}).get("missing") or []) \
            if draft and draft["status"] == "blocked" else "нет готового выпуска"
        app.notify_admin(f"{RUBRIC}: на {today} {why} — публикация пропущена.")
        return "blocked" if draft and draft["status"] == "blocked" else "no_draft"
    payload = draft["payload"]
    report = payload["report"]
    registry = _registry_from(payload["sources"])
    date_str = ru_date(datetime.fromisoformat(today))
    try:
        url = draft.get("page_url")
        if not url:
            _prune_dead(app, report)
            missing = media_problems(report)
            if missing:
                payload["status"] = publication_status(report)
                app.store.draft_save(key, "blocked", payload, error="; ".join(missing))
                app.notify_admin(f"{RUBRIC}: {today} BLOCKED перед публикацией — " + "; ".join(missing))
                return "blocked"
            href = growth_publish.begin(app, today, report, "riddle")  # the article's personal invite link
            url = create_pages(app.telegraph, *build_page(report, registry, date_str, href or channel_url(app),
                                                          header_url(app), button_url(app), growth_publish.block(href)))
            app.store.draft_mark(key, "ready", page_url=url)
            growth_publish.page_created(app, today, report, url, "riddle")  # "Поделиться разбором"; never raises
        status = publication_status(report, _verify_pages(app, url))
        payload["status"] = status
        if status["status"] != "READY":
            app.store.draft_save(key, "blocked", payload, error="; ".join(status["missing"]))
            app.store.draft_mark(key, "blocked", page_url=url, error="; ".join(status["missing"]))
            app.notify_admin(f"{RUBRIC}: {today} BLOCKED после проверки страницы {url} — " + "; ".join(status["missing"]))
            return "blocked"
        mid = _send_post(app, report, date_str, url, registry)
    except Exception as exc:  # noqa: BLE001
        app.store.draft_mark(key, "ready", error=str(exc)[:500])
        app.notify_admin(f"{RUBRIC}: публикация {today} не удалась: {exc}")
        return "failed"
    app.store.draft_save(key, "published", payload)
    app.store.draft_mark(key, "published", page_url=url, message_ids=[mid])
    if app.settings.riddle_publish_mode == "auto":  # drafts in the admin chat must not reach the public VK group
        _send_vk(app, payload, today, url)
    growth_publish.post_sent(app, today, report, url, mid, "riddle", app.target_chat(riddle=True),
                             "🔎 Читать расследование")  # message id + "Поделиться в TG"; never raises
    _archive_add(app, report, today, url)
    mode = app.settings.riddle_publish_mode
    app.notify_admin(f"{RUBRIC} {today} ({mode}): {report['company_name']} — READY, изображений {status['images']}, "
                     f"видео {status['videos']}, редакционная оценка {status.get('quality')}/10; фактчек: проверено "
                     f"{status.get('fact_check', {}).get('checked', 0)}, ошибок найдено {status.get('fact_check', {}).get('errors_found', 0)}, "
                     f"исправлено {status.get('fact_check', {}).get('fixed', 0)}, открытых 0; права на "
                     f"{status['rights_to_check']} кадров со страниц-источников проверить перед выходом в канал. "
                     f"Заметки, видеобриф и исследование: python -m investor_agent riddle-brief --date {today}")
    return "published"
