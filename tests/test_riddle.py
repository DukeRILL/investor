import json
from datetime import datetime
from urllib.parse import quote

from conftest import FakeDeepSeek, FakeHttp, FakeImageHost

from investor_agent.riddle import pipeline as riddle
from investor_agent.scheduler import RIDDLE_PUBLISH_TIME, Scheduler
from investor_agent.sources import SourceRegistry
from investor_agent.timeutil import BEIJING

RU = "Основатели заметили дорогую проблему клиентов и превратили её в повторяющиеся продажи. " * 16
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
    cites = " [S1] [S2] [S3] [S4] [S5] [S6]"
    sections = [{"act": a, "title": f"Заголовок {a}", "body": RU + cites} for a in riddle.ACTS]
    return {
        "company_name": "Acme", "industry": "логистика", "country": "США", "category": "Логистика",
        "business_type": "B2B SaaS по подписке",
        "concept": {"riddle": "Как скучная накладная стала бизнесом на миллиард?", "angle": "невидимая проблема",
                    "mechanism": "подписка", "headlines": ["Один", "Два", "Три"]},
        "headline": "Как скучная накладная стала бизнесом на миллиард",
        "teaser": teaser if teaser is not None else "Компания Acme зарабатывает на том, что все считали скучным. " * 9,
        "sections": sections,
        "mechanism_chain": [{"step": f"Шаг {i}", "text": "Клиенту нужна быстрая доставка без звонков"} for i in range(6)],
        "timeline": [{"date": str(2015 + i), "text": "Событие компании"} for i in range(4)],
        "charts": [{"title": "Выручка", "unit": "млн $", "kind": "bar", "act": "evidence", "source": "S1",
                    "points": [{"label": "2021", "value": 1}, {"label": "2022", "value": 3}, {"label": "2023", "value": 7}]}],
        "media": [{"source": f"S{i}", "kind": "photo", "act": "before", "role": "hero" if i == 1 else "inline",
                   "caption": f"Фото {i}", "function": "продукт"} for i in range(1, 9)],
        "videos": _videos(registry, videos),
        "cis": {"analogs": [{"name": f"Аналог {i}", "country": "Россия", "source": f"S{i}", "kind": "direct",
                             "product": "сервис", "similarity": "модель", "difference": "рынок"} for i in range(7, 10)],
                "verdict": "ТЕСТИРОВАТЬ", "arguments": ["спрос есть", "конкуренты слабые"], "main_risk": "платежи",
                "confidence": "средняя", "would_change": "данные о CAC", "adaptation": "сервис для малого опта",
                "mvp": [f"шаг {i}" for i in range(6)], "experiment_30d": "30 дней, 20 звонков"},
        "video_brief": {"hook": "Скучная бумага стоит миллиард", "format": "Боль на миллиард"},
        "fact_check": {"verified": [], "unconfirmed": ["Доля основателя"], "limits": []},
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
    bad = story(SourceRegistry(), teaser="Короткий анонс. ТЕСТИРОВАТЬ")
    bad["cis"]["verdict"] = "может быть"
    bad["sections"] = bad["sections"][:-1]
    errors = " | ".join(riddle.validate_story(bad, SourceRegistry()))
    assert "teaser has" in errors and "cis.verdict" in errors and "act 'cis'" in errors


def test_full_cycle_draft_goes_to_admin_with_12_images_and_3_videos(make_app):
    ds = FakeDeepSeek([lambda reg, p: story(reg)])
    app = make_app(deepseek=ds, http=media_http(), images=CountingHost())
    app.store.archive_add("Ramp", "финтех", "США", "2026-11-07", "https://telegra.ph/x")
    assert riddle.prepare(app, "2026-11-09") == "ready"
    assert "Ramp" in ds.calls[0]["payload"]["excluded"]  # Unicorn School companies are not repeated
    draft = app.store.draft_get("riddle:2026-11-09")
    assert draft["payload"]["status"]["status"] == "READY"
    assert len(draft["payload"]["manifests"]["youtube"]) == 3
    assert riddle.publish(app, "2026-11-09") == "published"
    post = app.telegram.sent[0]
    assert post["chat"] == "111"  # RIDDLE_PUBLISH_MODE defaults to draft: admin chat, never the channel
    assert post["photo"] == app.store.get("riddle_cover_url")  # the rubric cover, uploaded once
    assert post["html"].startswith("<b>Раздел:</b> Логистика · <b>Тип:</b> B2B SaaS по подписке\n")
    assert post["button"] == ("🔎 Читать расследование", "https://telegra.ph/test-page")
    assert "РАЗГАДКА БИЗНЕСА" in post["html"] and "Время чтения:" in post["html"] and "ТЕСТИРОВАТЬ" not in post["html"]
    title, nodes = app.telegraph.pages[0]
    assert title.startswith("Разгадка бизнеса: ")
    imgs, vids = riddle.count_page_media(nodes)
    assert imgs >= 12 and vids == 3
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
