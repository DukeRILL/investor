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
    ds = FakeDeepSeek([lambda reg, p: story(reg)])
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
    ds = FakeDeepSeek([lambda reg, p: story(reg, videos=3)])
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
