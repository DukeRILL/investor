import json

from conftest import FakeHttp, FakeTelegram, FakeTelegraph

from investor_agent import chart, media
from investor_agent.http import is_public_url
from investor_agent.sources import SourceRegistry
from investor_agent.telegram import TelegramError
from investor_agent.unicorn import pipeline as unicorn
from test_agents import cand, good_report

JPEG = b"\xff\xd8\xff" + b"0" * 20_000
OEMBED_URL = ("https://www.youtube.com/oembed?format=json&url="
              "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ")
OEMBED_OK = {OEMBED_URL: (json.dumps({"title": "Founder interview", "author_name": "Sequoia Capital"}).encode(), "application/json")}


def test_video_embed_accepts_youtube_and_vimeo_only():
    yt = media.video_embed("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10")
    assert yt[0] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert yt[1].startswith("/embed/youtube?url=https%3A%2F%2Fwww.youtube.com")
    assert media.video_embed("https://youtu.be/dQw4w9WgXcQ")[0] == yt[0]
    assert media.video_embed("https://vimeo.com/123456")[1].startswith("/embed/vimeo?url=")
    assert media.video_embed("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert media.video_embed("https://www.youtube.com/watch?v=short") is None


def test_find_page_image_reads_og_and_twitter_tags():
    html = '<head><meta property="og:image" content="/img/a.jpg?x=1&amp;y=2"><title>t</title></head>'
    assert media.find_page_image(html, "https://site.com/news/1") == "https://site.com/img/a.jpg?x=1&y=2"
    assert media.find_page_image('<meta name="twitter:image" content="https://c.com/t.png">', "https://s.com") \
        == "https://c.com/t.png"
    assert media.find_page_image("<p>no tags</p>", "https://s.com") is None


def test_public_url_blocks_private_and_bad_schemes():
    for url in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://169.254.169.254/latest", "ftp://1.1.1.1/x",
                "file:///etc/passwd", "not a url"):
        assert not is_public_url(url)


def _registry():
    reg = SourceRegistry()
    page = reg.add("https://news.com/founders", "Founders", "News")
    reg.mark_fetched(page.id, page.title, 'text with https://news.com/real.jpg inside')
    vid = reg.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Interview", "YouTube")
    reg.mark_fetched(vid.id, vid.title, "video page")
    return reg


def test_resolve_media_photo_video_and_rejects_fakes():
    reg = _registry()
    http = FakeHttp({"https://news.com/founders": (b'<meta property="og:image" content="https://news.com/og.jpg">', "text/html"),
                     "https://news.com/og.jpg": (JPEG, "image/jpeg"),
                     "https://news.com/real.jpg": (JPEG, "image/jpeg"), **OEMBED_OK})
    uploads = []
    items = [{"source": "S1", "kind": "photo", "caption": "Основатели", "role": "hero", "section": "start"},
             {"source": "S2", "kind": "video", "caption": "Интервью"},
             {"source": "S1", "kind": "photo", "image_url": "https://invented.com/x.jpg", "caption": "Выдумка"},
             {"source": "S99", "kind": "photo", "caption": "Нет такого источника"}]
    out = media.resolve_media(items, reg, http, lambda data, ctype: uploads.append(ctype) or "https://telegra.ph/file/a.jpg")
    assert [m["kind"] for m in out] == ["photo", "video"]
    assert out[0]["src"] == "https://telegra.ph/file/a.jpg" and out[0]["role"] == "hero"
    assert out[1]["embed"].startswith("/embed/youtube") and uploads == ["image/jpeg"]


def test_resolve_media_drops_tiny_and_non_image_files():
    reg = _registry()
    http = FakeHttp({"https://news.com/founders": (b'<meta property="og:image" content="https://news.com/pixel.gif">', "text/html"),
                     "https://news.com/pixel.gif": (b"GIF89a", "image/gif")})
    assert media.resolve_media([{"source": "S1", "kind": "photo", "caption": "x"}], reg, http, lambda d, c: "u") == []


def test_upload_failure_falls_back_to_original_url():
    reg = _registry()
    http = FakeHttp({"https://news.com/founders": (b'<meta property="og:image" content="https://news.com/og.jpg">', "text/html"),
                     "https://news.com/og.jpg": (JPEG, "image/jpeg")})

    def boom(data, ctype):
        raise TelegramError("down")
    out = media.resolve_media([{"source": "S1", "kind": "photo", "caption": "x"}], reg, http, boom)
    assert out[0]["src"] == "https://news.com/og.jpg"


def _report_with_media(reg):
    report = good_report(reg)
    report["media_resolved"] = [
        {"kind": "photo", "src": "https://telegra.ph/file/hero.jpg", "caption": "Основатели", "role": "hero",
         "section": "start", "source_url": "https://s.com/a", "publisher": "Fortune", "source_id": "S1"},
        {"kind": "photo", "src": "https://telegra.ph/file/p2.jpg", "caption": "Офис", "section": "start",
         "source_url": "https://s.com/b", "publisher": "TC", "source_id": "S2"},
        {"kind": "video", "embed": "/embed/youtube?url=x", "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
         "caption": "Интервью", "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "publisher": "YouTube",
         "source_id": "S3"}]
    report["chart"] = {"title": "Оценка", "unit": "млрд $", "points": [{"label": "2021", "value": 1.6},
                                                                      {"label": "2022", "value": 8.1},
                                                                      {"label": "2023", "value": 5.8}], "source": "S2"}
    report["chart_image"] = "https://telegra.ph/file/chart.png"
    return report


def _tags(nodes):
    found = []
    for n in nodes:
        if isinstance(n, dict):
            found.append(n["tag"])
            found += _tags(n.get("children", []))
    return found


def test_build_page_has_photos_video_metrics_chart_and_numbered_cites():
    reg = SourceRegistry()
    report = _report_with_media(reg)
    _, nodes = unicorn.build_page(report, reg, "9 ноября 2026")
    tags = _tags(nodes)
    assert tags.count("img") == 3 and "iframe" in tags  # hero, one inline photo, chart; one video
    flat = json.dumps(nodes, ensure_ascii=False)
    assert "0 млн" in flat and "9/10" in flat and "Как Acme выросла" in flat
    assert "[1]" in flat and "[Pub0]" not in flat  # citations are short numbers, not publisher names
    assert nodes[2]["tag"] == "figure"  # lead photo directly under the intro


def test_chart_falls_back_to_table_without_image():
    reg = SourceRegistry()
    report = _report_with_media(reg)
    report["chart_image"] = None
    _, nodes = unicorn.build_page(report, reg, "9 ноября 2026")
    assert "1.6 млрд $" in json.dumps(nodes, ensure_ascii=False)


def test_paginate_splits_long_issue_and_chains_parts():
    nodes = []
    for i in range(6):
        nodes.append({"tag": "h3", "children": [f"Раздел {i}"]})
        nodes.append({"tag": "p", "children": ["я" * 20_000]})  # ~40 KB in UTF-8
    pages = unicorn.paginate(nodes)
    assert len(pages) == 6 and all(unicorn._size(p) <= unicorn.TELEGRAPH_PAGE_BYTES for p in pages)
    graph = FakeTelegraph()
    first = unicorn.create_pages(graph, "Заголовок", nodes)
    assert first == "https://telegra.ph/test-page-6"  # parts are created last-to-first
    assert graph.pages[-1][0] == "Заголовок" and graph.pages[0][0] == "Раздел 5 · Заголовок"
    first_page = json.dumps(graph.pages[-1][1], ensure_ascii=False)
    assert "Читать дальше: Раздел 1 →" in first_page and "часть" not in first_page.lower()


def test_paginate_avoids_a_tiny_tail_page():
    """An issue a little over the limit is cut into two similar halves, not a full page plus one small block."""
    nodes = []
    for i in range(10):
        nodes.append({"tag": "h3", "children": [f"Раздел {i}"]})
        nodes.append({"tag": "p", "children": ["я" * 3_300]})  # ~6.6 KB each
    nodes.append({"tag": "h3", "children": ["Суть"]})
    nodes.append({"tag": "p", "children": ["я" * 1_000]})
    total = unicorn._size(nodes)
    limit = total - 1_500  # just over: greedy would leave only the last section on page 2
    pages = unicorn.paginate(nodes, limit)
    assert len(pages) == 2 and all(unicorn._size(p) <= limit for p in pages)
    assert unicorn._size(pages[1]) > total * 0.3
    assert unicorn.paginate(nodes, total + 10) == [nodes]  # fits: a single page


def test_cloudinary_upload_is_signed_and_returns_url():
    import hashlib
    from investor_agent.images import CloudinaryClient
    http = FakeHttp()
    url = CloudinaryClient(http, "demo", "key123", "s3cret").upload_image(b"x" * 100, "image/png")
    assert url.endswith("/unicorn/abc.jpg") and http.posted["url"] == "https://api.cloudinary.com/v1_1/demo/image/upload"
    f = http.posted["fields"]
    expected = hashlib.sha1(f"public_id={f['public_id']}&timestamp={f['timestamp']}s3cret".encode()).hexdigest()
    assert f["signature"] == expected and f["api_key"] == "key123" and "s3cret" not in json.dumps(f)
    assert f["public_id"] == "unicorn/" + hashlib.sha1(b"x" * 100).hexdigest()[:20]


def test_image_host_is_preferred_over_telegraph_upload(make_app):
    from conftest import FakeImageHost
    host = FakeImageHost()
    app = _app_with_report(make_app)
    app.images = host
    unicorn.prepare(app, "2026-11-09")
    saved = app.store.draft_get("2026-11-09")["payload"]["report"]
    assert host.uploads and saved["media_resolved"][0]["src"].startswith("https://res.cloudinary.com/")


def test_validate_requires_metrics_scores_and_media():
    reg = SourceRegistry()
    report = good_report(reg)
    assert unicorn.validate_report(report, reg) == []
    report["metrics"], report["verdict_scores"], report["media"] = [], [], []
    text = " ".join(unicorn.validate_report(report, reg))
    assert "metrics" in text and "verdict_scores" in text and "media" in text


def _app_with_report(make_app, telegram=None):
    ds_reports = {}

    def stage2(reg, p):
        report = good_report(reg)
        ds_reports["r"] = report
        return report
    from conftest import FakeDeepSeek
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}
    http = FakeHttp({"https://site0.com/a": (b'<meta property="og:image" content="https://site0.com/og.jpg">', "text/html"),
                     "https://site0.com/og.jpg": (JPEG, "image/jpeg")})
    return make_app(deepseek=FakeDeepSeek([stage1, stage2]), telegram=telegram, http=http)


def test_prepare_resolves_media_and_post_goes_out_with_photo(make_app):
    app = _app_with_report(make_app)
    assert unicorn.prepare(app, "2026-11-09") == "ready"
    saved = app.store.draft_get("2026-11-09")["payload"]["report"]
    assert saved["media_resolved"] and saved["media_resolved"][0]["src"] == "https://telegra.ph/file/test.jpg"
    assert unicorn.publish(app, "2026-11-09") == "published"
    assert app.telegram.sent[0]["photo"] == "https://telegra.ph/file/test.jpg"
    assert app.telegram.sent[0]["button"][1] == "https://telegra.ph/test-page"


def test_photo_post_failure_falls_back_to_text(make_app):
    class PhotoFails(FakeTelegram):
        def send_photo(self, *a, **k):
            raise TelegramError("sendPhoto failed: test")
    app = _app_with_report(make_app, telegram=PhotoFails())
    unicorn.prepare(app, "2026-11-09")
    assert unicorn.publish(app, "2026-11-09") == "published"
    assert "photo" not in app.telegram.sent[0] and "Компания дня: Acme" in app.telegram.sent[0]["html"]


def test_chart_renders_png_when_font_is_available():
    png = chart.render_chart([{"label": "2021", "value": 1.6}, {"label": "2022", "value": 8.1},
                              {"label": "2023", "value": 5.8}, {"label": "2024", "value": 60}], "млрд $")
    if png is None:
        import pytest
        pytest.skip("DejaVu font is not installed here")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert chart.render_chart([{"label": "a", "value": 1}]) is None  # too few points


def test_one_issue_is_exactly_one_chat_message(make_app):
    """The owner forbids multi-message articles: the full text lives on Telegraph, the chat gets one post."""
    app = _app_with_report(make_app)
    unicorn.prepare(app, "2026-11-09")
    assert unicorn.publish(app, "2026-11-09") == "published"
    # the readers' poll and the pinned glossary post are separate standing posts, not part of the issue
    posts = lambda: [m for m in app.telegram.sent if "html" in m and "blockquote" not in m["html"]]
    assert len(posts()) == 1
    assert unicorn.publish(app, "2026-11-09") == "already_published" and len(posts()) == 1
    assert unicorn.visible_len(app.telegram.sent[0]["html"]) <= unicorn.CAPTION_LIMIT


def test_reading_time_is_one_concrete_number_with_correct_russian_form():
    from investor_agent.textfmt import minutes_ru, reading_line, reading_minutes
    assert [minutes_ru(n) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25, 111)] == [
        "1 минута", "2 минуты", "4 минуты", "5 минут", "11 минут", "12 минут", "21 минута", "22 минуты", "25 минут", "111 минут"]
    assert reading_minutes("слово " * 2000) == 10 and reading_minutes("") == 1
    assert reading_minutes("<b>слово</b> [S3] " * 400) == 2  # tags and citations are not words
    assert "–" not in reading_line("слово " * 3000) and reading_line("слово " * 3000) == "⏱ Время чтения: 15 минут"


def test_unicorn_post_and_radar_alert_carry_reading_time():
    from test_agents import alert_json, snap, NOW
    from investor_agent.radar import pipeline as radar
    reg = SourceRegistry()
    report = good_report(reg)
    post = unicorn.build_post(report, "9 ноября 2026")
    assert "Время чтения:" in post and "–" not in post.split("Время чтения:")[1]
    reg2 = SourceRegistry()
    first = radar.format_alert(alert_json(reg2), reg2, snap(), NOW)[0]
    assert "⏱ Время чтения: 1 минута" in first or "⏱ Время чтения: 2 минуты" in first


def test_edit_caption_keeps_the_button():
    from investor_agent.telegram import TelegramClient

    class Capture:
        def request(self, method, url, json_body=None, **kw):
            self.url, self.body = url, json_body
            return {"ok": True, "result": True}
    http = Capture()
    TelegramClient(http, "tok").edit_caption("@chan", 7, "<b>x</b>", button=("Читать", "https://t.example/a"))
    assert http.url.endswith("/editMessageCaption") and http.body["message_id"] == 7
    assert http.body["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.example/a"


def test_video_is_dropped_when_youtube_does_not_know_it_and_gets_channel_name_when_it_does():
    reg = _registry()
    items = [{"source": "S2", "kind": "video", "caption": ""}]
    assert media.resolve_media(items, reg, FakeHttp(), lambda d, c: "u") == []  # oEmbed 404: private or deleted
    out = media.resolve_media(items, reg, FakeHttp(OEMBED_OK), lambda d, c: "u")
    assert out[0]["publisher"] == "Sequoia Capital" and out[0]["caption"] == "Founder interview"


def test_up_to_three_charts_are_placed_in_their_sections_and_old_chart_field_still_works():
    reg = SourceRegistry()
    report = _report_with_media(reg)
    pts = [{"label": "2021", "value": 1}, {"label": "2022", "value": 2}, {"label": "2023", "value": 4}]
    report["charts"] = [{"title": "Оценка", "unit": "млрд $", "kind": "line", "points": pts, "source": "S1"},
                        {"title": "Выручка", "unit": "млн $", "kind": "bar", "section": "money", "points": pts, "source": "S1"},
                        {"title": "Раунды", "unit": "млн $", "kind": "bar", "section": "unknown-id", "points": pts}]
    report["chart_images"] = ["https://c/1.png", None, "https://c/3.png"]
    _, nodes = unicorn.build_page(report, reg, "9 ноября 2026")
    flat = json.dumps(nodes, ensure_ascii=False)
    assert flat.count("https://c/1.png") == 1 and flat.count("https://c/3.png") == 1  # unknown section -> traction
    assert "Выручка" in flat and "2022" in flat  # no image: the data stays visible as a table
    legacy = _report_with_media(SourceRegistry())  # single `chart` + `chart_image`, as stored for Serval
    assert "chart.png" in json.dumps(unicorn.build_page(legacy, SourceRegistry(), "x")[1])


def test_bar_chart_renders_png_when_font_is_available():
    png = chart.render_chart([{"label": f"0{i}.26", "value": v} for i, v in enumerate([25, 30, 115, 300, 1850], 1)], "млн $", "bar")
    if png is None:
        import pytest
        pytest.skip("DejaVu font is not installed here")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_prompt_and_docs_keep_the_agreed_standards():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "unicorn_school.md").read_text(encoding="utf-8")
    for must in ("role `hero`", "logo", "reviews", "`charts`", "verdict_scores", "metrics"):
        assert must in prompt, must
    rules = (root / "docs" / "RULES.md").read_text(encoding="utf-8")
    for must in ("одно сообщение", "личк", "Cloudinary", "Время чтения", "Вердикт и оценки", "docs/reference/ramp-2026-11-07.md"):
        assert must in rules, must
    assert (root / "docs" / "reference" / "ramp-2026-11-07.md").exists()


def test_channel_post_never_carries_the_verdict_or_scores():
    reg = SourceRegistry()
    report = good_report(reg)
    report["verdict_scores"] = [{"score": 9, "label": "Клиентская боль"}] * 4
    post = unicorn.build_post(report, "9 ноября 2026")
    assert "ердикт" not in post and "/10" not in post and "Клиентская боль" not in post
    _, nodes = unicorn.build_page(report, reg, "9 ноября 2026")
    assert "9/10" in json.dumps(nodes, ensure_ascii=False)


def test_validate_requires_15_minutes_of_reading():
    reg = SourceRegistry()
    report = good_report(reg)
    assert unicorn.validate_report(report, reg) == []
    report["sections"][1]["body"] = report["sections"][1]["body"][:600]
    assert any("need at least 15" in e for e in unicorn.validate_report(report, reg))


def test_short_report_is_topped_up_by_sections_not_regenerated():
    from types import SimpleNamespace
    reg = SourceRegistry()
    report = good_report(reg)
    for sec in report["sections"]:
        if sec["id"] in ("idea", "start", "money", "breakthrough"):
            sec["body"] = sec["body"][:600]
    assert any("need at least 15" in e for e in unicorn.validate_report(report, reg))
    calls = []

    class FakeDS:
        def complete_json(self, model, system, payload, temperature=0.3, max_tokens=None):
            calls.append(payload["expand"])
            assert max_tokens
            return {"sections": [{"id": x["id"], "body": " ".join(["слово"] * (x["target_words"] + 400))}
                                 for x in payload["expand"]]}

    app = SimpleNamespace(deepseek=FakeDS(), settings=SimpleNamespace(deepseek_model_pro="pro"))
    errors = unicorn._expand_short(app, report, reg, {"company": {"name": "X"}})
    assert not any("need at least 15" in e for e in errors)
    assert 1 <= len(calls) <= unicorn.EXPAND_ROUNDS


def test_cover_goes_first_and_file_is_light():
    reg = SourceRegistry()
    _, nodes = unicorn.build_page(_report_with_media(reg), reg, "9 ноября 2026", "https://img/cover.jpg")
    assert nodes[0]["tag"] == "figure" and nodes[0]["children"][0]["attrs"]["src"] == "https://img/cover.jpg"
    _, plain = unicorn.build_page(_report_with_media(reg), reg, "9 ноября 2026")
    assert plain[0]["tag"] == "p"  # no cover: layout as before
    assert 0 < unicorn.COVER_PATH.stat().st_size < 250_000


def test_channel_button_only_at_end():
    reg = SourceRegistry()
    _, nodes = unicorn.build_page(_report_with_media(reg), reg, "9 ноября 2026", "https://img/cover.jpg", "https://img/tg.png")
    flat = [json.dumps(n, ensure_ascii=False) for n in nodes]
    assert "t.me/investor_duker" not in "".join(flat[:3])  # not under the cover
    assert "tg.png" in flat[-2] and "🦄 Перейти на канал" in flat[-1]
