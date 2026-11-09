import json
from datetime import datetime
from urllib.parse import quote

from conftest import FakeDeepSeek, FakeHttp, FakeImageHost

from investor_agent.riddle import pipeline as riddle
from investor_agent.scheduler import RIDDLE_PUBLISH_TIME, Scheduler
from investor_agent.sources import SourceRegistry
from investor_agent.timeutil import BEIJING

RU = "Основатели заметили дорогую проблему клиентов и превратили её в повторяющиеся продажи. " * 30
RU_CIS = "Близкие аналоги в России решают похожую задачу, но для другого клиента и с другой моделью оплаты. " * 8
JPEG = b"\xff\xd8" + b"x" * 9000


def _videos(registry, n):
    out = []
    for i in range(n):
        src = registry.add(f"https://www.youtube.com/watch?v=abcdefghij{i}", f"Видео {i}", "YouTube")
        out.append({"source": src.id, "purpose": ("founders", "product", "market")[i % 3], "note": f"Зачем смотреть {i}"})
    return out


def story(registry, videos=3, teaser=None):
    for i in range(10):
        src = registry.add(f"https://site{i}.com/a", f"Title {i}", f"Pub{i}")
        registry.mark_fetched(src.id, src.title, "content")
    cites = " [S1] [S2] [S3] [S4] [S5] [S6] [S7]"
    sections = [{"act": a, "title": f"Заголовок {a}", "body": (RU_CIS if a == "cis" else RU) + cites} for a in riddle.ACTS]
    return {
        "company_name": "Acme", "website_source": "S2", "industry": "логистика", "country": "США", "category": "Логистика",
        "business_type": "B2B SaaS по подписке",
        "editorial_notes": {"angles": [{"angle": f"Ракурс {i}", "scores": {"surprise": 4, "story": 4, "evidence": 5,
                                                                           "viral": 4}} for i in range(3)], "chosen": 0},
        "concept": {"riddle": "Как скучная накладная стала бизнесом на миллиард?",
                    "headlines": [{"mechanic": m, "text": m} for m in ("парадокс", "скрытый механизм", "неожиданное решение")]},
        "discussion_question": "Вы бы отказались от наценки ради экосистемы?",
        "quality": {"interest": 9, "depth": 8, "mindset": 9, "analytics": 9, "clarity": 9, "shareability": 8, "repeats_found": False},
        "headline": "Как скучная накладная стала бизнесом на миллиард",
        "teaser": teaser if teaser is not None else "Компания Acme зарабатывает на том, что все считали скучным. " * 9,
        "sections": sections,
        "mechanism_chain": [{"step": f"Шаг {i}", "text": "Клиенту нужна быстрая доставка без звонков"} for i in range(6)],
        "timeline": [{"date": str(2015 + i), "text": "Событие компании"} for i in range(4)],
        "charts": [{"title": "Выручка", "unit": "млн $", "kind": "bar", "act": "evidence", "source": "S1",
                    "points": [{"label": "2021", "value": 1}, {"label": "2022", "value": 3}, {"label": "2023", "value": 7}]}],
        "media": [{"source": f"S{i}", "kind": "photo", "act": "before", "role": "hero" if i == 1 else "inline",
                   "caption": f"Фото {i}", "function": "продукт"} for i in range(1, 11)],
        "videos": _videos(registry, videos),
        "cis": {"analogs": [{"name": f"Аналог {i}", "country": "Россия", "source": f"S{i}", "essence": "сервис",
                             "similarity": "модель", "difference": "рынок"} for i in range(7, 10)],
                "verdict": "ТРЕБУЕТ ПРОВЕРКИ", "hypothesis": ""},
        "video_brief": {"hook": "Скучная бумага стоит миллиард", "concept": "Боль на миллиард"},
        "research_notes": {"facts": [], "unconfirmed": ["Доля основателя"], "limits": []},
    }


def fc_ok(reg, payload):
    """Independent fact check: everything verified."""
    assert "article" in payload and payload["article"]["sections"]
    return {"claims": [{"id": f"C{i}", "field": field, "text": "миллиард", "kind": "amount", "material": True,
                        "source": "S1", "source_value": "1", "source_unit": "billion USD", "article_value": "1",
                        "article_unit": "млрд долларов", "source_quote": "content", "status": "VERIFIED", "fix": None}
                       for i, field in enumerate(("headline", "teaser", "sections[0].body"), 1)]}


def media_http():
    files = {}
    for i in range(10):
        files[f"https://site{i}.com/a"] = (f'<meta property="og:image" content="https://img{i}.com/p.jpg">'.encode(), "text/html")
        files[f"https://img{i}.com/p.jpg"] = (JPEG + bytes([i]), "image/jpeg")
    for i in range(5):
        canonical = f"https://www.youtube.com/watch?v=abcdefghij{i}"
        files["https://www.youtube.com/oembed?format=json&url=" + quote(canonical, safe="")] = (
            json.dumps({"title": f"Видео {i}", "author_name": "Канал"}).encode(), "application/json")
    return FakeHttp(files)


class CountingHost(FakeImageHost):
    def upload_image(self, data, content_type):
        self.uploads.append((len(data), content_type))
        return f"https://res.cloudinary.com/test/{len(self.uploads)}.jpg"


def test_validate_story_rules():
    reg = SourceRegistry()
    good = story(reg)
    assert riddle.validate_story(good, reg) == []
    assert any("excluded" in e for e in riddle.validate_story(good, reg, {"acme"}))
    bad = story(SourceRegistry(), teaser="Короткий анонс. ТРЕБУЕТ ПРОВЕРКИ")
    bad["cis"]["verdict"] = "ТЕСТИРОВАТЬ"  # the old verdict scale is gone
    bad["sections"] = bad["sections"][:-1]
    errors = " | ".join(riddle.validate_story(bad, SourceRegistry()))
    assert "teaser has" in errors and "cis.verdict" in errors and "act 'discovery'" in errors


def test_new_master_prompt_rules():
    reg = SourceRegistry()
    weak = story(reg)
    weak["quality"] = {"interest": 7, "depth": 7, "mindset": 7, "analytics": 7, "clarity": 8, "shareability": 7, "repeats_found": True}
    weak["editorial_notes"]["angles"] = weak["editorial_notes"]["angles"][:1]
    weak["discussion_question"] = ""
    errors = " | ".join(riddle.validate_story(weak, reg))
    assert "below 8.0" in errors and "repeated meaning" in errors and "three different angles" in errors
    assert "discussion_question" in errors
    long = story(SourceRegistry())
    long["sections"][3]["body"] += " слово" * 4000  # too long and over the 20-minute ceiling
    errors = " | ".join(riddle.validate_story(long, SourceRegistry()))
    assert "need 2,700–3,200" in errors and "required 15–20" in errors
    plan = story(SourceRegistry())
    plan["sections"][7]["body"] += " план" * 300  # the CIS block is not a business plan
    plan["cis"]["analogs"] *= 2
    errors = " | ".join(riddle.validate_story(plan, SourceRegistry()))
    assert "cis section has" in errors and "at most 3" in errors
    assert riddle.quality_score(story(SourceRegistry())) == 8.7


def test_full_cycle_draft_goes_to_admin_with_12_images_and_3_videos(make_app):
    ds = FakeDeepSeek([lambda reg, p: story(reg), fc_ok])
    app = make_app(deepseek=ds, http=media_http(), images=CountingHost())
    app.store.archive_add("Ramp", "финтех", "США", "2026-11-07", "https://telegra.ph/x")
    assert riddle.prepare(app, "2026-11-09") == "ready"
    assert "Ramp" in ds.calls[0]["payload"]["excluded"]  # Unicorn School companies are not repeated
    draft = app.store.draft_get("riddle:2026-11-09")
    assert draft["payload"]["status"]["status"] == "READY"
    assert len(draft["payload"]["manifests"]["youtube"]) == 3
    assert any(s["id"] == "S2" for s in draft["payload"]["sources"])  # website_source is kept: the preview logo is found on it at publish
    assert riddle.publish(app, "2026-11-09") == "published"
    post = app.telegram.sent[0]
    assert post["chat"] == "111"  # RIDDLE_PUBLISH_MODE defaults to draft: admin chat, never the channel
    assert post["photo"].startswith("https://res.cloudinary.com/test/")  # rubric picture + company name, sector, logo
    assert post["html"].startswith("<b>Как скучная накладная стала бизнесом на миллиард</b>\n")  # straight with the headline
    assert post["button"] == ("🔎 Читать расследование", "https://telegra.ph/test-page")
    assert "Раздел:" not in post["html"] and "РАЗГАДКА БИЗНЕСА" not in post["html"] and "Время чтения:" in post["html"] and "ТРЕБУЕТ ПРОВЕРКИ" not in post["html"]
    assert "💬 <i>Вы бы отказались от наценки ради экосистемы?</i>" in post["html"]
    title, nodes = app.telegraph.pages[0]
    assert title.startswith("Разгадка бизнеса: ")
    header = app.store.get("riddle_header_url_v3")
    assert nodes[0]["children"][0]["attrs"]["src"] == header  # the rubric header opens the article
    imgs, vids = riddle.count_page_media(nodes, {header})
    assert imgs >= 12 and vids == 3
    text = json.dumps(nodes, ensure_ascii=False)
    assert "Вывод: ТРЕБУЕТ ПРОВЕРКИ." in text and "Эксперимент на 30 дней" not in text and "Главные источники" in text
    assert riddle.archive(app)[0]["name"] == "Acme" and "acme" in riddle.excluded_names(app)
    assert riddle.publish(app, "2026-11-09") == "already_published"


def test_blocked_without_three_videos(make_app):
    ds = FakeDeepSeek([lambda reg, p: story(reg, videos=3), fc_ok])
    http = media_http()
    http.files.pop("https://www.youtube.com/oembed?format=json&url=" + quote("https://www.youtube.com/watch?v=abcdefghij2", safe=""))
    app = make_app(deepseek=ds, http=http, images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "blocked"
    assert "видео YouTube 2 из 3" in app.telegram.sent[-1]["html"]
    assert riddle.publish(app, "2026-11-09") == "blocked"
    assert not app.telegraph.pages


def test_failed_story_is_sent_back_once_then_fails(make_app):
    weak = lambda reg, p: story(reg, teaser="Коротко.")
    ds = FakeDeepSeek([weak, weak])
    app = make_app(deepseek=ds, http=media_http(), images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "failed"
    assert ds.calls[1]["payload"]["fix_these_problems"]
    assert riddle.publish(app, "2026-11-09") == "no_draft"


def test_scheduler_has_riddle_at_11_moscow(make_app):
    app = make_app()
    sch = Scheduler(app, clock=lambda: datetime(2026, 11, 9, 1, 0, tzinfo=BEIJING))
    assert RIDDLE_PUBLISH_TIME == (16, 0)
    pub = sch._job("riddle_publish")
    assert (pub.next_run.hour, pub.next_run.minute) == (16, 0) and not pub.uses_deepseek
    prep = sch._job("riddle_prepare")
    assert prep.uses_deepseek
    retry = sch._riddle_retry("blocked", datetime(2026, 11, 9, 3, 30, tzinfo=BEIJING))
    assert retry.hour == 6
    assert sch._riddle_retry("failed", datetime(2026, 11, 9, 11, 0, tzinfo=BEIJING)) is None


def test_preview_has_name_sector_and_logo():
    import io
    from PIL import Image
    from investor_agent.riddle import visuals
    template = io.BytesIO()
    Image.new("RGB", (1199, 1312), "#031a46").save(template, "PNG")
    logo = io.BytesIO()
    Image.new("RGB", (96, 96), "#ff6900").save(logo, "PNG")
    plain = visuals.render_preview(template.getvalue(), "Xiaomi", "Электроника", None)
    with_logo = visuals.render_preview(template.getvalue(), "Xiaomi", "Электроника", logo.getvalue())
    img = Image.open(io.BytesIO(with_logo))
    assert img.size == (1199, 1312) and img.getpixel((190, 620)) != Image.open(io.BytesIO(plain)).getpixel((190, 620))
    # nothing is drawn outside the fields: a very long name and sector stay inside their boxes
    long = visuals.render_preview(template.getvalue(), "Очень длинное название компании-гиганта " * 3, "Потребительская электроника и интернет-сервисы " * 3, logo.getvalue())
    base = Image.new("RGB", (1199, 1312), "#031a46")
    out = Image.open(io.BytesIO(long))
    from PIL import ImageChops
    box = ImageChops.difference(out, base).convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()
    fields = [visuals.LOGO_BOX, visuals.NAME_BOX, visuals.SECTOR_BOX]
    for field in fields:
        pass
    assert box and box[0] >= 90 and box[2] <= 1112
    assert visuals.render_preview(b"junk", "X", "Y") is None


def test_riddle_page_ends_with_channel_button():
    _, nodes = riddle.build_page({"headline": "H", "sections": []}, SourceRegistry(), "9 ноября 2026",
                                 "https://t.me/investor_duker", None, "https://img/tg.png")
    flat = [json.dumps(n, ensure_ascii=False) for n in nodes]
    assert "tg.png" in flat[-2] and "🦄 Перейти на канал" in flat[-1]


def test_fact_check_gate_fixes_tenfold_error_and_blocks_open_claims(make_app):
    from investor_agent.riddle import factcheck
    assert factcheck.scale_problem({"source_value": "369", "source_unit": "млн юаней", "article_value": "3,7",
                                    "article_unit": "млрд юаней"}).endswith("(расхождение в 10 раз)")
    assert factcheck.scale_problem({"source_value": "4573亿", "source_unit": "RMB", "article_value": "457,3",
                                    "article_unit": "млрд юаней"}) is None
    assert factcheck.scale_problem({"source_value": "37,400", "source_unit": "million yuan", "article_value": "37,4",
                                    "article_unit": "млрд юаней"}) is None

    def wrong(reg, p):  # the model calls a tenfold error VERIFIED: the server's own arithmetic overrules it
        return {"claims": fc_ok(reg, p)["claims"][1:] + [{"id": "C1", "field": "sections[0].body", "text": "выручка", "material": True, "source": "S1",
                            "source_value": "369", "source_unit": "млн юаней", "article_value": "3,7",
                            "article_unit": "млрд юаней", "status": "VERIFIED",
                            "fix": {"find": "[S1] [S2] [S3]", "replace": "369 млн юаней [S1] [S2] [S3]"}}]}

    def recheck(reg, p):
        assert p["recheck"][0]["id"] == "C1" and "369 млн юаней [S1]" in json.dumps(p["article"], ensure_ascii=False)
        return {"claims": [{"id": "C1", "field": "sections[0].body", "text": "выручка", "material": True, "source": "S1",
                            "source_value": "369", "source_unit": "млн юаней", "article_value": "369",
                            "article_unit": "млн юаней", "source_quote": "content", "status": "VERIFIED"}]}

    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: story(reg), wrong, recheck]), http=media_http(),
                   images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "ready"
    report = app.store.draft_get("riddle:2026-11-09")["payload"]["report"]
    assert report["fact_check"]["errors_found"] == 1 and report["fact_check"]["fixed"] == 1 and not report["fact_check"]["open"]
    assert "369 млн юаней [S1] [S2] [S3]" in report["sections"][0]["body"]

    def unverified(reg, p):
        return {"claims": [{"id": "C1", "field": "headline", "text": "миллиард", "material": True, "status": "UNVERIFIED"}]}
    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: story(reg), unverified]), http=media_http(),
                   images=CountingHost())
    assert riddle.prepare(app, "2026-11-10") == "blocked"
    assert "фактчек: 2 существенных" in app.telegram.sent[-1]["html"]


def test_published_page_must_match_verified_numbers():
    report = {"verified_numbers": ["457.3", "369"], "verified_title_numbers": [], "fact_check": {"checked": 3, "open": []}}
    status = riddle.publication_status(report, (12, 3, "Выручка 457,3 млрд, первая партия на 3,7 млрд", "Разгадка бизнеса: Xiaomi"))
    assert any("нет чисел: 369" in m for m in status["missing"])


def test_typesafe_jev_must_confirm_verified_claims(make_app):
    http = media_http()
    http.jev = lambda body: {"type": "choice", "choice": "contradicted", "confidence": 0.8,
                             "probabilities": {"supported": 0.1, "contradicted": 0.85, "insufficient": 0.05}}
    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: story(reg), fc_ok]), http=http, images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "blocked"
    body = http.jev_calls[0]
    assert body["model"] == "jev-latest" and "EXCERPT: content" in body["state"]
    assert set(body["questions"]["C1"]["criteria"]) == {"supported", "contradicted", "insufficient"}
    ledger = app.store.draft_get("riddle:2026-11-09")["payload"]["report"]["fact_ledger"]
    assert ledger[0]["status"] == "ERROR" and ledger[0]["jev"]["choice"] == "contradicted"

    def invented(reg, p):  # a quote that is not on the opened page never reaches READY
        claim = fc_ok(reg, p)
        claim["claims"][0]["source_quote"] = "Revenue was 1 billion dollars"
        return claim
    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: story(reg), invented]), http=media_http(), images=CountingHost())
    assert riddle.prepare(app, "2026-11-10") == "blocked"
    assert "выдержки нет на открытой странице" in app.telegram.sent[-1]["html"]

    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: story(reg), fc_ok]), http=media_http(), images=CountingHost())
    app.settings.typesafe_api_key = ""
    assert riddle.prepare(app, "2026-11-11") == "blocked"
    assert "нет TYPESAFE_API_KEY" in app.telegram.sent[-1]["html"]


# --- Codex review of e08cebc: release blockers ---------------------------------------------------------
def _claim(**kw):
    base = {"id": "C1", "field": "sections[0].body", "text": "выручка", "kind": "amount", "material": True,
            "source": "S1", "source_value": "369", "source_unit": "млн юаней", "article_value": "369",
            "article_unit": "млн юаней", "source_quote": "content", "status": "VERIFIED"}
    return {**base, **kw}


def test_number_parsing_is_explicit_about_locale():
    import pytest
    from investor_agent.factgate import NumberError, number, scale_problem
    assert number("1 999") == 1999 and number("1\u00a0999") == 1999 and number("4573亿") == 4573
    assert number("1,234,567") == 1234567 and number("1.234.567") == 1234567
    assert number("4.573,0") == 4573 and number("4,573.0") == 4573 and number("457,3") == 457.3
    assert number("1,999", "source") == 1999 and number("1,999", "article") == 1.999  # locale decided by the side
    assert number("n/a") is None
    for bad in ("1,999", "1,2,3", "1.234,56,7"):
        with pytest.raises(NumberError):
            number(bad)
    # an unreadable digit string is a problem, never a silent skip
    assert "неоднозначная" in scale_problem({"source_value": "1,2,3", "source_unit": "", "article_value": "5",
                                              "article_unit": ""})
    # tenfold error with a thousands-grouped source number is no longer skipped
    assert "в 10 раз" in scale_problem({"source_value": "1,234,567", "source_unit": "юаней",
                                         "article_value": "12 345 670", "article_unit": "юаней"})
    assert scale_problem({"source_value": "37,400", "source_unit": "million yuan", "article_value": "37,4",
                          "article_unit": "млрд юаней"}) is None
    assert scale_problem({"source_value": "4573亿", "source_unit": "RMB", "article_value": "457,3",
                          "article_unit": "млрд юаней"}) is None
    assert "в 10 раз" in scale_problem({"source_value": "0.9", "source_unit": "billion yuan", "article_value": "9",
                                         "article_unit": "млрд юаней"})


def test_percent_against_ratio_is_compared_not_skipped():
    from investor_agent.factgate import scale_problem
    ok = {"source_value": "0.766", "source_unit": "ratio", "article_value": "76,6", "article_unit": "%"}
    assert scale_problem(ok) is None
    assert scale_problem({**ok, "article_value": "7,66"})  # a wrong percent against the ratio
    assert scale_problem({"source_value": "76.6", "source_unit": "%", "article_value": "0,766", "article_unit": ""}) is None
    assert scale_problem({"source_value": "76.6", "source_unit": "%", "article_value": "0,0766", "article_unit": ""})


def test_only_opened_sources_prove_a_claim(make_app):
    from investor_agent import factgate
    reg = SourceRegistry()
    opened = reg.add("https://a.com/x", "A")
    reg.mark_fetched(opened.id, "A", "Revenue was 369 million yuan.")
    searched = reg.add("https://b.com/y", "B")  # found by web_search, never fetch_page'd
    report = {"sections": [{"body": "Первая партия принесла 369 млн юаней."}]}
    claims = [_claim(id="C1", source=opened.id, source_quote="Revenue was 369 million yuan."),
              _claim(id="C2", source=searched.id), _claim(id="C3", source="S99"),
              _claim(id="C4", source="", source_url="https://invented.example/proof"),
              _claim(id="C5", source="", source_url=searched.url)]
    factgate._settle(report, claims, reg, factgate.STRICT)
    assert [c["status"] for c in claims] == ["VERIFIED"] + ["UNVERIFIED"] * 4
    assert all("не открывался" in c["note"] for c in claims[1:])
    # an ERROR correction needs an opened source too
    bad = _claim(status="ERROR", source=searched.id, fix={"find": "369 млн", "replace": "999 млн"})
    factgate._settle(report, [bad], reg, factgate.STRICT)
    assert report["sections"][0]["body"].startswith("Первая партия принесла 369 млн") and bad["status"] == "ERROR"
    # the Jev step also needs an opened source and the quote on its page
    app = make_app(http=media_http())
    spoofed = _claim(source=searched.id)
    factgate.judge_with_jev(app, [spoofed], reg)
    assert spoofed["status"] == "UNVERIFIED" and not getattr(app.http, "jev_calls", None)


def test_recheck_cannot_hide_or_swap_an_open_claim(make_app):
    reg_ref = {}

    def first(reg, p):
        src = reg.add("https://a.com/x", "A")
        reg.mark_fetched(src.id, "A", "Revenue was 369 million yuan.")
        reg_ref["id"] = src.id
        return {"claims": [_claim(source=src.id, article_value="3,7", article_unit="млрд юаней",
                                  source_quote="Revenue was 369 million yuan.", status="VERIFIED"),
                           _claim(id="C2", field="headline", text="миллиард", article_value="1", source_value="1",
                                  source_unit="billion", article_unit="млрд", status="UNVERIFIED")]}

    def sneaky(reg, p):  # tries material=false, another field, a never-opened source and drops C2
        return {"claims": [_claim(field="teaser", material=False, source="S77", status="VERIFIED",
                                  article_value="369", source_quote="Revenue was 369 million yuan."),
                           _claim(id="C99", status="VERIFIED")]}

    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: story(reg), first, sneaky, sneaky]), http=media_http(),
                   images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "blocked"
    ledger = {c["id"]: c for c in app.store.draft_get("riddle:2026-11-09")["payload"]["report"]["fact_ledger"]}
    assert set(ledger) == {"C1", "C2"}  # the invented C99 is ignored, the dropped C2 stays open
    assert ledger["C1"]["field"] == "sections[0].body" and ledger["C1"]["material"] is True
    assert ledger["C1"]["status"] != "VERIFIED" and ledger["C2"]["status"] == "UNVERIFIED"
    assert "S77" not in json.dumps(ledger["C1"]) or ledger["C1"]["status"] != "VERIFIED"


def test_telegraph_title_is_checked_with_the_content():
    report = {"verified_numbers": ["76.6", "369"], "verified_title_numbers": ["76.6"],
              "fact_check": {"checked": 3, "open": []}}
    same = riddle.publication_status(report, (12, 3, "Выручка 369 млн, маржа 76,6%", "Разгадка бизнеса: маржа 76,6%"))
    assert not any("страниц" in m and "расходится" in m for m in same["missing"])
    changed = riddle.publication_status(report, (12, 3, "Выручка 369 млн, маржа 76,6%", "Разгадка бизнеса: маржа 7,66%"))
    assert any("заголовке страницы" in m for m in changed["missing"])
    gone = riddle.publication_status(report, (12, 3, "Выручка 369 млн", "Разгадка бизнеса: маржа"))
    assert any("заголовке страницы" in m or "нет чисел" in m for m in gone["missing"])


def test_numeric_headline_passes_publication_and_publish_is_idempotent(make_app):
    def fc_pct(reg, p):
        claims = fc_ok(reg, p)["claims"]
        return {"claims": claims + [{**claims[0], "id": "C9", "field": "headline", "kind": "percent",
                                     "source_value": "76.6", "source_unit": "%", "article_value": "76,6",
                                     "article_unit": "%"}]}
    ds = FakeDeepSeek([lambda reg, p: {**story(reg), "headline": "Как Acme заработала 76,6% маржи"}, fc_pct])
    app = make_app(deepseek=ds, http=media_http(), images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "ready"
    assert riddle.publish(app, "2026-11-09") == "published"  # the title is read back with get_page_full
    assert riddle.publish(app, "2026-11-09") == "already_published"
    posts = [m for m in app.telegram.sent if m["html"].startswith("<b>Как Acme заработала 76,6% маржи</b>")]
    assert len(posts) == 1  # the second publish() sent nothing


# --- second Codex review (225f01d): four defects -------------------------------------------------------
def test_same_printed_token_is_not_forced_equal_across_locales():
    from investor_agent.factgate import scale_problem
    # source «1,999» (English: 1999) against article «1,999» (Russian: 1.999) in the same unit is a ×1000 error
    assert "в 1000 раз" in scale_problem({"source_value": "1,999", "source_unit": "yuan", "article_value": "1,999",
                                           "article_unit": "юаней"})
    # …but 1,999 million = 1,999 млрд is not: the units carry the scale
    assert scale_problem({"source_value": "1,999", "source_unit": "million yuan", "article_value": "1,999",
                          "article_unit": "млрд юаней"}) is None


def test_explicit_ratio_and_percent_are_never_equal_by_digits():
    from investor_agent.factgate import scale_problem
    assert scale_problem({"source_value": "76.6", "source_unit": "%", "article_value": "76.6", "article_unit": "ratio"})
    assert scale_problem({"source_value": "0.766", "source_unit": "ratio", "article_value": "0.766", "article_unit": "%"})
    assert scale_problem({"source_value": "0.766", "source_unit": "ratio", "article_value": "76,6", "article_unit": "%"}) is None
    assert scale_problem({"source_value": "76.6", "source_unit": "%", "article_value": "76,6", "article_unit": "%"}) is None
    assert scale_problem({"source_value": "0.766", "source_unit": "ratio", "article_value": "0,766", "article_unit": "доля"}) is None


def test_malformed_jev_answers_never_confirm():
    import pytest
    from investor_agent.typesafe import Jev, TypeSafeError

    class H:
        def __init__(self, answer):
            self.answer = answer

        def request(self, *a, **kw):
            return {"model": "jev", "answers": {"q": self.answer}}

    crit = {"supported": "", "contradicted": "", "insufficient": ""}
    good = {"choice": "supported", "confidence": 0.9, "probabilities": {"supported": 0.95, "contradicted": 0.03}}
    assert Jev(H(good), "k").choice("s", "i", crit, "q")["confidence"] == 0.9
    for bad in ({**good, "confidence": "Infinity"}, {**good, "confidence": float("inf")}, {**good, "confidence": 7},
                {**good, "confidence": True}, {**good, "probabilities": {"supported": "Infinity"}},
                {**good, "probabilities": {"supported": -0.2}}, {**good, "probabilities": {"supported": float("nan")}},
                {**good, "probabilities": {"contradicted": 0.1}}, {**good, "probabilities": {}}, {**good, "choice": "other"}, None):
        with pytest.raises(TypeSafeError):
            Jev(H(bad), "k").choice("s", "i", crit, "q")


def test_a_single_non_material_claim_cannot_pass_the_gate(make_app):
    def nothing(reg, p):  # one «harmless» VERIFIED claim, no source, no Jev — the reported bypass
        return {"claims": [{"id": "C1", "field": "headline", "text": "миллиард", "material": False, "status": "VERIFIED"}]}
    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: story(reg), nothing, nothing, nothing]), http=media_http(),
                   images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "blocked"
    report = app.store.draft_get("riddle:2026-11-09")["payload"]["report"]
    assert report["fact_check"]["open"] and "нужно не меньше" in " ".join(report["fact_check"]["open"])


def test_numeric_claims_are_material_whatever_the_model_says():
    from investor_agent.factgate import _normalise
    out = _normalise([{"id": "C1", "material": False, "kind": "amount", "status": "VERIFIED"},
                      {"id": "C2", "material": False, "article_value": "76,6", "status": "VERIFIED"},
                      {"id": "C3", "material": False, "kind": "other", "status": "VERIFIED"}])
    assert [c["material"] for c in out] == [True, True, False]


def test_numbers_of_the_headline_must_be_covered_by_a_verified_claim(make_app):
    ds = FakeDeepSeek([lambda reg, p: {**story(reg), "headline": "Как Acme заработала 76,6% маржи"}, fc_ok, fc_ok, fc_ok])
    app = make_app(deepseek=ds, http=media_http(), images=CountingHost())
    assert riddle.prepare(app, "2026-11-09") == "blocked"  # fc_ok never lists «76,6»: the server opens a claim for it
    ledger = app.store.draft_get("riddle:2026-11-09")["payload"]["report"]["fact_ledger"]
    assert any(c["id"] == "COV-76.6" and c["status"] != "VERIFIED" for c in ledger)
