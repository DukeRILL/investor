import json

from conftest import FakeHttp, FakeTelegram, FakeTelegraph

from investor_agent import chart, media
from investor_agent.http import is_public_url
from investor_agent.sources import SourceRegistry
from investor_agent.telegram import TelegramError
from investor_agent.unicorn import pipeline as unicorn
from test_agents import cand, good_report

JPEG = b"\xff\xd8\xff" + b"0" * 20_000


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
                     "https://news.com/real.jpg": (JPEG, "image/jpeg")})
    uploads = []
    items = [{"source": "S1", "kind": "photo", "caption": "Основатели", "role": "hero", "section": "founders_story"},
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
         "section": "founders_story", "source_url": "https://s.com/a", "publisher": "Fortune", "source_id": "S1"},
        {"kind": "photo", "src": "https://telegra.ph/file/p2.jpg", "caption": "Офис", "section": "founders_story",
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
    assert first == "https://telegra.ph/test-page-6"  # pages are created last-to-first, so the first part is the newest
    assert graph.pages[-1][0] == "Заголовок" and graph.pages[0][0].endswith("часть 6")
    assert "Продолжение: часть 2" in json.dumps(graph.pages[-1][1], ensure_ascii=False)


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
    assert len(app.telegram.sent) == 1
    assert unicorn.publish(app, "2026-11-09") == "already_published" and len(app.telegram.sent) == 1
    assert unicorn.visible_len(app.telegram.sent[0]["html"]) <= unicorn.CAPTION_LIMIT
