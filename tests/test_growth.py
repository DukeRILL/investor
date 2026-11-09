import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from conftest import Crash, FakeDeepSeek, FakeTelegram
from test_agents import cand, good_report

from investor_agent.growth import collect, publish as gpub, redirect, report as greport, share
from investor_agent.growth.links import db, target_allowed
from investor_agent.http import HttpError
from investor_agent.unicorn import pipeline as unicorn

DAY = "2026-11-09"


def published_app(make_app, **kw):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}
    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg)]), **kw)
    assert unicorn.prepare(app, DAY) == "ready"
    assert unicorn.publish(app, DAY) == "published"
    return app


def flat(nodes):
    return json.dumps(nodes, ensure_ascii=False)


# --- links / encoding -----------------------------------------------------------------------------------------
def test_share_links_encode_cyrillic_and_use_official_endpoints():
    urls = share.share_urls("https://telegra.ph/Acme-11-09", "Acme & Co", "Рост в 3 раза: как?", "issue-x")
    assert urls["telegram"].startswith("https://t.me/share/url?url=")
    assert urls["vk"].startswith("https://vk.com/share.php?url=") and urls["whatsapp"].startswith("https://wa.me/?text=")
    q = parse_qs(urlparse(urls["telegram"]).query)
    assert q["text"] == ["Acme & Co: Рост в 3 раза: как?"] and "utm_source=telegram" in q["url"][0]
    assert " " not in urls["whatsapp"] and "&" not in urlparse(urls["whatsapp"]).query.split("text=")[1]
    assert "Acme" in parse_qs(urlparse(urls["whatsapp"]).query)["text"][0]


def test_hook_comes_from_the_article_and_differs_between_articles():
    a = share.build_hook({"subtitle": "Как Acme выросла до 100 млн долларов", "teaser": "x"})
    b = share.build_hook({"teaser": "Beta вышла на рынок. Потом удвоилась [S2]."})
    assert "Acme" in a and b == "Beta вышла на рынок." and a != b
    assert len(share.clean_hook("слово " * 100)) <= 141


def test_subscribe_block_has_approved_text_and_only_supported_tags():
    nodes = share.subscribe_nodes("https://t.me/+abc")
    text = "\n".join(share._text(n) for n in nodes)
    assert text.startswith("Завтра — новая история большого бизнеса\n")
    assert "Каждый день разбираем реальные компании: с чего начинали, как находили точки роста, на чём зарабатывали и какие решения привели к успеху." in text
    assert "→ Подписаться на «ШКОЛУ ЕДИНОРОГОВ»" in text
    link = nodes[2]["children"][0]["children"][0]  # the bold line is the link, all of it
    assert link == {"tag": "a", "attrs": {"href": "https://t.me/+abc"}, "children": ["→ Подписаться на «ШКОЛУ ЕДИНОРОГОВ»"]}
    assert {n["tag"] for n in nodes} == {"h4", "p"}  # no horizontal rule, no buttons


# --- publication integration ----------------------------------------------------------------------------------
def test_publish_adds_cta_share_buttons_and_records_everything(make_app):
    app = published_app(make_app)
    page_nodes = flat(app.telegraph.by_url["https://telegra.ph/test-page"][1])
    assert "https://t.me/+inv1" in page_nodes and "ШКОЛУ ЕДИНОРОГОВ" in page_nodes
    assert "Подписаться на Школу единорогов" not in page_nodes
    assert "Хорошими идеями стоит делиться" in page_nodes and "t.me/share/url" in page_nodes and "wa.me" in page_nodes
    gdb = db(app)
    pub = gdb.publication_by_date(DAY)
    assert pub["article_url"] == "https://telegra.ph/test-page" and pub["telegram_message_id"] == 1 and pub["share_done"]
    rows = app.telegram.buttons[1]
    assert rows[0][0][0] == "📖 Читать разбор" and rows[1][0][0] == "📤 Поделиться в TG"
    assert "https%3A%2F%2Ft.me%2Finvestor_duker%2F1" in rows[1][0][1]  # shares the channel post
    assert app.telegram.invites == ["telegraph-cta-2026-11-09"]


def test_banner_and_phrase_share_one_personal_invite_link(make_app):
    app = published_app(make_app)
    nodes = app.telegraph.by_url["https://telegra.ph/test-page"][1]
    hrefs = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("tag") == "a" and n["attrs"]["href"].startswith("https://t.me/+"):
                hrefs.append((n["attrs"]["href"], flat(n)))
            for c in n.get("children", []):
                walk(c)
    for n in nodes:
        walk(n)
    assert [h for h, _ in hrefs] == ["https://t.me/+inv1", "https://t.me/+inv1"]  # banner image link, phrase link
    assert "img" in hrefs[0][1] and "ШКОЛУ ЕДИНОРОГОВ" in hrefs[1][1]
    assert app.telegram.invites == ["telegraph-cta-2026-11-09"]  # one invite link serves both
    texts = [share._text(n) for n in nodes]
    fig = max(i for i, n in enumerate(nodes) if n.get("tag") == "figure")
    i_sub = next(i for i, t in enumerate(texts) if t.startswith("Завтра — новая история большого бизнеса"))
    i_share = next(i for i, t in enumerate(texts) if t.startswith("Хорошими идеями стоит делиться"))
    assert fig < i_sub < i_share and not any(n.get("tag") == "hr" for n in nodes[fig:])  # banner, subscribe, share


def test_republish_and_retry_do_not_duplicate_the_block(make_app):
    app = published_app(make_app)
    assert unicorn.publish(app, DAY) == "already_published"
    pub = db(app).publication_by_date(DAY)
    assert gpub.ensure_share_block(app, pub) is True
    assert gpub.ensure_share_block(app, pub) is True
    assert flat(app.telegraph.by_url["https://telegra.ph/test-page"][1]).count("Хорошими идеями стоит делиться") == 1
    assert len(app.telegraph.edited) == 1 and len(app.telegram.invites) == 1


def test_telegraph_edit_failure_does_not_break_publication_and_is_retried(make_app):
    app = make_app(deepseek=FakeDeepSeek([lambda reg, p: {"candidates": [cand("Acme"), cand("B", industry="hr"),
                                                                         cand("C", industry="edtech")]},
                                          lambda reg, p: good_report(reg)]))
    app.telegraph.fail_edit = True
    unicorn.prepare(app, DAY)
    assert unicorn.publish(app, DAY) == "published"
    pub = db(app).publication_by_date(DAY)
    assert not pub["share_done"] and [p["id"] for p in db(app).share_pending()] == [pub["id"]]
    app.telegraph.fail_edit = False
    assert gpub.retry_pending(app) == 1 and db(app).publication_by_date(DAY)["share_done"]


def test_draft_mode_creates_no_invite_links_and_falls_back_to_public_link(make_app):
    app = published_app(make_app, mode="draft")
    assert not getattr(app.telegram, "invites", [])
    assert "https://t.me/investor_duker" in flat(app.telegraph.by_url["https://telegra.ph/test-page"][1])


def test_chain_of_parts_gets_share_block_on_last_part_only(make_app):
    app = make_app()
    app.store.draft_set = None
    last = app.telegraph.create_page("Part 2", [{"tag": "p", "children": ["end"]}])
    first = app.telegraph.create_page("Part 1", [{"tag": "p", "children": ["a"]}, {"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": last}, "children": ["Читать дальше: Конец →"]}]}])
    pub_id = db(app).publication_upsert(DAY, "Acme", article_url=first)
    gpub.ensure_share_block(app, db(app).publication(pub_id))
    assert "Поделиться" not in flat(app.telegraph.by_url[first][1])
    assert "Хорошими идеями стоит делиться" in flat(app.telegraph.by_url[last][1])


# --- invite attribution / chat_member -------------------------------------------------------------------------
def member_update(uid, old, new, invite=None, ts=1_700_000_000, username="investor_duker"):
    cm = {"chat": {"id": -100, "username": username, "type": "channel"}, "date": ts, "from": {"id": 1},
          "old_chat_member": {"status": old}, "new_chat_member": {"status": new}}
    if invite:
        cm["invite_link"] = {"invite_link": invite}
    return {"update_id": uid, "chat_member": cm}


def test_join_via_invite_link_is_attributed_and_others_are_unknown(make_app):
    app = published_app(make_app)
    app.telegram.updates = [member_update(10, "left", "member", "https://t.me/+inv1"),
                            member_update(11, "left", "member"),                      # public username
                            member_update(12, "left", "member", "https://t.me/+foreign"),
                            member_update(13, "member", "left"),
                            member_update(14, "left", "member", username="other_channel")]
    out = collect.poll_updates(app)
    assert out["join"] == 3 and out["leave"] == 1
    counts = db(app).subscription_counts()
    assert counts[("telegraph", "join")] == 1 and counts[("UNKNOWN", "join")] == 2 and counts[("UNKNOWN", "leave")] == 1
    assert app.telegram.updates_calls[0][1] == ["chat_member"]


def test_repeated_webhook_delivery_and_restart_do_not_double_count(make_app):
    app = published_app(make_app)
    app.telegram.updates = [member_update(10, "left", "member", "https://t.me/+inv1")]
    collect.poll_updates(app)
    assert app.store.get(collect.UPDATES_KEY) == "11"
    app.store.set(collect.UPDATES_KEY, "0")  # offset lost: Telegram redelivers the same update
    out = collect.poll_updates(app)
    assert out["join"] == 0 and out["duplicate"] == 1
    assert sum(db(app).subscription_counts().values()) == 1


def test_get_updates_failure_is_recorded_and_not_raised(make_app):
    app = make_app()
    app.telegram.get_updates = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("409 webhook"))
    assert "409" in collect.poll_updates(app)["error"]
    assert "409" in db(app).provider("telegram_updates")["last_error"]


def test_opening_a_link_is_not_a_subscription(make_app):
    app = published_app(make_app)
    link = db(app).links()[0]
    db(app).add_traffic(link["id"], "click", "c1")
    assert db(app).subscription_counts() == {}


# --- redirect -------------------------------------------------------------------------------------------------
def test_redirect_serves_only_stored_safe_targets_and_dedups(make_app):
    app = published_app(make_app)
    link = db(app).links()[0]
    assert redirect.resolve_click(app, link["code"], "Mozilla/5.0 Safari", "1.2.3.4", "s") == "https://t.me/+inv1"
    redirect.resolve_click(app, link["code"], "Mozilla/5.0 Safari", "1.2.3.4", "s")  # same visitor same day
    redirect.resolve_click(app, link["code"], "TelegramBot (like TwitterBot)", "9.9.9.9", "s")
    counts = db(app).traffic_counts()
    assert counts == {("telegraph", "click"): 1, ("telegraph", "bot_click"): 1}
    assert redirect.resolve_click(app, "nope", "ua", "ip", "s") is None
    assert redirect.resolve_click(app, "../../etc", "ua", "ip", "s") is None
    assert not target_allowed("https://evil.example/x") and not target_allowed("http://t.me/x")
    db(app).link_set(link["id"], target_url="https://evil.example/")
    with db(app).store._conn() as c:
        c.execute("UPDATE tracking_links SET invite_link=NULL WHERE id=?", (link["id"],))
    assert redirect.resolve_click(app, link["code"], "Mozilla", "ip", "s") is None


def test_redirect_href_used_when_public_base_url_is_set(make_app):
    app = make_app()
    app.settings.public_base_url = "https://go.example.com/"
    nodes = flat(gpub.block(gpub.begin(app, DAY, {"company_name": "Acme"})))
    assert "https://go.example.com/go/" in nodes


# --- statistics -----------------------------------------------------------------------------------------------
def test_telegraph_views_snapshots_are_kept_and_throttled(make_app):
    app = published_app(make_app)
    app.telegraph.views["https://telegra.ph/test-page"] = 42
    now = datetime.now(timezone.utc)
    assert collect.collect_telegraph_views(app, now) == 1
    assert collect.collect_telegraph_views(app, now + timedelta(hours=1)) == 0
    app.telegraph.views["https://telegra.ph/test-page"] = 60
    assert collect.collect_telegraph_views(app, now + timedelta(days=1, minutes=1)) == 1
    pid = db(app).publication_by_date(DAY)["id"]
    assert db(app).snapshot_latest(pid, "telegraph", "views")["value"] == 60
    assert len(db(app).snapshots()) == 2  # history kept


def test_telegraph_error_is_stored_and_old_data_stays(make_app):
    app = published_app(make_app)
    app.telegraph.views["https://telegra.ph/test-page"] = 5
    collect.collect_telegraph_views(app)
    app.telegraph.views.clear()
    collect.collect_telegraph_views(app, datetime.now(timezone.utc) + timedelta(days=2))
    assert "PAGE_NOT_FOUND" in db(app).provider("telegraph")["last_error"] and len(db(app).snapshots()) == 1


class TgStatHttp:
    def __init__(self, result):
        self.result, self.calls = result, []

    def request(self, method, url, **kw):
        self.calls.append((url, kw.get("params")))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


OK = {"status": "ok", "response": {"participants_count": 120, "avg_post_reach": 80.5, "err_percent": 30.1,
                                    "er_percent": 4.0, "title": "x"}}


def tgstat_app(make_app, result, **settings):
    app = make_app(http=TgStatHttp(result))
    app.settings.tgstat_api_token = "T"
    for k, v in settings.items():
        setattr(app.settings, k, v)
    return app


def test_tgstat_saves_available_metrics_once_a_day(make_app):
    app = tgstat_app(make_app, OK)
    now = datetime(2026, 11, 9, 1, tzinfo=timezone.utc)
    assert collect.collect_tgstat(app, now) == "ok"
    assert collect.collect_tgstat(app, now + timedelta(hours=3)) == "done_today"
    assert len(app.http.calls) == 1 and app.http.calls[0][1]["channelId"] == "@investor_duker"
    assert db(app).snapshot_latest(0, "telegram", "avg_post_reach", "tgstat")["value"] == 80.5
    assert db(app).snapshot_latest(0, "telegram", "daily_reach", "tgstat") is None  # absent metric is not invented
    assert db(app).provider_calls("tgstat", "2026-11") == 1


def test_tgstat_quota_guard_stops_before_the_limit(make_app):
    app = tgstat_app(make_app, OK, tgstat_monthly_limit=50, tgstat_reserve=5)
    now = datetime(2026, 11, 20, 1, tzinfo=timezone.utc)
    for _ in range(45):
        db(app).provider_count_call("tgstat", "2026-11")
    assert collect.collect_tgstat(app, now) == "quota_guard" and not app.http.calls
    assert collect.collect_tgstat(app, datetime(2026, 12, 1, 1, tzinfo=timezone.utc)) == "ok"  # new month


def test_tgstat_quota_error_pauses_until_next_month_and_keeps_data(make_app):
    app = tgstat_app(make_app, OK)
    collect.collect_tgstat(app, datetime(2026, 11, 9, 1, tzinfo=timezone.utc))
    app.http.result = HttpError(429, "limit")
    assert collect.collect_tgstat(app, datetime(2026, 11, 10, 1, tzinfo=timezone.utc)) == "error"
    assert collect.collect_tgstat(app, datetime(2026, 11, 11, 1, tzinfo=timezone.utc)) == "paused"
    assert len(app.http.calls) == 2
    assert db(app).provider("tgstat")["disabled_until"].startswith("2026-11-30T16")
    assert db(app).snapshot_latest(0, "telegram", "participants_count", "tgstat")["value"] == 120


def test_tgstat_unavailable_expired_and_not_configured(make_app):
    app = tgstat_app(make_app, ConnectionError("down"))
    assert collect.collect_tgstat(app, datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == "error"
    assert "ConnectionError" in db(app).provider("tgstat")["last_error"]
    assert collect.collect_tgstat(app, datetime(2026, 12, 8, 1, tzinfo=timezone.utc)) == "expired"
    app2 = make_app(http=TgStatHttp(OK))
    assert collect.collect_tgstat(app2) == "not_configured" and not app2.http.calls
    app3 = tgstat_app(make_app, {"status": "error", "error": "token invalid"})
    assert collect.collect_tgstat(app3, datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == "error"


def test_data_survives_restart(make_app, tmp_path):
    app = published_app(make_app)
    collect.collect_channel(app)
    from investor_agent.growth.db import GrowthDb
    from investor_agent.storage import Store
    again = GrowthDb(Store(app.settings.database_path))
    assert len(again.publications()) == 1 and again.snapshot_latest(0, "telegram", "subscribers")["value"] == 100
    assert again.links()[0]["invite_link"] == "https://t.me/+inv1"


# --- report ---------------------------------------------------------------------------------------------------
def test_daily_report_with_empty_data_marks_unknowns(make_app):
    app = make_app()
    text = greport.build_report(app)
    assert "н/д" in text and "накапливается" in text and "TGStat" in text and "не измеряются" in text


def test_daily_report_sent_to_admin_only_and_survives_failing_sources(make_app):
    app = published_app(make_app)
    app.telegraph.views["https://telegra.ph/test-page"] = 30
    app.telegram.members = 150
    now_ts = int(datetime.now(timezone.utc).timestamp())
    app.telegram.updates = [member_update(20, "left", "member", "https://t.me/+inv1", ts=now_ts),
                            member_update(21, "member", "left", ts=now_ts)]
    before = len(app.telegram.sent)
    assert greport.run_daily(app) == "sent"
    msg = app.telegram.sent[before]
    assert msg["chat"] == "111"
    for needle in ("Acme", "150", "просмотры Telegraph 30", "UNKNOWN", "отписок: 1", "telegraph 1", "3.3% (расчёт)"):
        assert needle in msg["html"], needle
    app.telegram.member_count = lambda chat: (_ for _ in ()).throw(RuntimeError("down"))
    assert greport.run_daily(app) == "sent"


def test_growth_delta_is_computed_from_snapshots_only(make_app):
    app = make_app()
    now = datetime(2026, 11, 20, 1, tzinfo=timezone.utc)
    for days, n in ((31, 80), (8, 90), (2, 95), (0, 100)):
        db(app).snapshot_add(0, "telegram", "subscribers", n, "telegram_bot", (now - timedelta(days=days)).isoformat())
    text = greport.build_report(app, now)
    assert "сутки +5" in text and "неделя +10" in text and "месяц +20" in text


def test_export_and_backfill(make_app, tmp_path):
    app = published_app(make_app)
    with app.store._conn() as c:
        c.execute("DELETE FROM publications")
    assert greport.backfill(app) == 1
    files = greport.export(app, tmp_path / "out")
    assert "publications.csv" in files and json.loads((tmp_path / "out" / "publications.json").read_text())[0]["company_name"] == "Acme"


def test_growth_disabled_changes_nothing(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("B", industry="hr"), cand("C", industry="edtech")]}
    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg)]))
    app.settings.growth_enabled = False
    unicorn.prepare(app, DAY)
    assert unicorn.publish(app, DAY) == "published"
    assert "Подписаться" not in flat(app.telegraph.pages[0][1]) and not getattr(app.telegram, "buttons", {})


# --- review fixes ---------------------------------------------------------------------------------------------
def test_growth_is_off_by_default():
    from investor_agent.config import Settings
    assert Settings().growth_enabled is False and Settings().trust_proxy is False


def test_click_flood_is_capped_and_spoofed_ua_does_not_multiply(make_app, monkeypatch):
    app = published_app(make_app)
    link = db(app).links()[0]
    for i in range(5):
        redirect.resolve_click(app, link["code"], f"Mozilla/{i}", "1.1.1.1", "s")
    assert db(app).traffic_counts() == {("telegraph", "click"): 1}
    monkeypatch.setattr(redirect, "DAILY_EVENT_CAP", 3)
    for i in range(10):
        assert redirect.resolve_click(app, link["code"], "Mozilla", f"2.2.2.{i}", "s")
    assert db(app).traffic_counts()[("telegraph", "click")] == 3


def test_webhook_conflict_is_reported_not_removed(make_app):
    app = make_app()
    app.telegram.webhook = "https://example.com/hook"
    app.telegram.get_updates = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("409 Conflict"))
    assert "webhook is set" in collect.poll_updates(app)["error"]
    collect.poll_updates(app)
    alerts = [m for m in app.telegram.sent if "webhook" in m["html"]]
    assert len(alerts) == 1 and app.telegram.webhook  # one alert per day, webhook untouched


import pytest


@pytest.mark.parametrize("status,until", [(401, "2026-11-12"), (403, "2026-11-12"), (402, "2026-11-30T16"), (429, "2026-11-30T16")])
def test_tgstat_http_errors_pause(make_app, status, until):
    app = tgstat_app(make_app, HttpError(status, "x"))
    assert collect.collect_tgstat(app, datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == "error"
    assert db(app).provider("tgstat")["disabled_until"].startswith(until)


def test_tgstat_200_with_error_body_pauses_a_day(make_app):
    app = tgstat_app(make_app, {"status": "error", "error": "limit exceeded"})
    assert collect.collect_tgstat(app, datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == "error"
    assert collect.collect_tgstat(app, datetime(2026, 11, 9, 5, tzinfo=timezone.utc)) == "paused"


def test_tgstat_quota_month_is_beijing_month(make_app):
    app = tgstat_app(make_app, OK)
    late = datetime(2026, 11, 30, 17, tzinfo=timezone.utc)  # 01:00 on 1 December in Beijing
    collect.collect_tgstat(app, late)
    assert db(app).provider_calls("tgstat", "2026-12") == 1 and db(app).provider_calls("tgstat", "2026-11") == 0


def prepared_app(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("B", industry="hr"), cand("C", industry="edtech")]}
    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg)]))
    unicorn.prepare(app, DAY)
    return app


def test_failed_send_is_retried_and_page_is_reused(make_app):
    app = prepared_app(make_app)
    app.telegram.fail = True
    assert unicorn.publish(app, DAY) == "failed"
    app.telegram.fail = False
    pages = len(app.telegraph.pages)
    assert unicorn.publish(app, DAY) == "published"
    assert len(app.telegraph.pages) == pages  # page_url was saved before the failing send
    assert sum("Компания дня" in m.get("html", "") for m in app.telegram.sent) == 1


def test_click_cap_holds_under_parallel_requests(make_app):
    from concurrent.futures import ThreadPoolExecutor
    app = published_app(make_app)
    link = db(app).links()[0]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: db(app).add_traffic(link["id"], "click", f"k{i}", daily_cap=5), range(40)))
    assert db(app).traffic_counts()[("telegraph", "click")] == 5


from investor_agent.telegram import TelegramError, TelegraphClient


def posts(app):
    return [m for m in app.telegram.sent if "Компания дня" in m.get("html", "")]


def boom_send(app, exc):
    def f(*a, **k):
        raise exc
    app.telegram.send = app.telegram.send_photo = f


def restore_send(app):
    for name in ("send", "send_photo"):
        app.telegram.__dict__.pop(name, None)


def test_crash_after_sent_state_resumes_without_a_second_post(make_app, monkeypatch):
    """1. crash after 'sent:<id>' was written, before draft_mark(published)."""
    app = prepared_app(make_app)
    monkeypatch.setattr(unicorn.growth_publish, "post_sent", lambda *a: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        unicorn.publish(app, DAY)
    assert len(posts(app)) == 1 and app.store.draft_get(DAY)["status"] == "ready"
    monkeypatch.undo()
    assert unicorn.publish(app, DAY) == "published"
    assert len(posts(app)) == 1  # no second post
    assert app.store.draft_get(DAY)["message_ids"] in ([1], "[1]") and "acme" in app.store.archived_names()
    assert db(app).publication_by_date(DAY)["telegram_message_id"] == 1
    assert any("question" in m for m in app.telegram.sent)  # the readers' poll was not skipped


def test_timeout_after_delivery_is_not_retried_automatically(make_app):
    """2. ambiguous failure (timeout/5xx): no resend, not 'published', explicit manual state."""
    app = prepared_app(make_app)
    told = []
    app.notify_admin = told.append
    boom_send(app, TelegramError("sendPhoto failed: ReadTimeout", ambiguous=True))
    assert unicorn.publish(app, DAY) == "requires_manual_reconcile"
    restore_send(app)
    assert unicorn.publish(app, DAY) == "requires_manual_reconcile"  # still no automatic retry
    assert not posts(app) and app.store.draft_get(DAY)["status"] == "ready"
    assert len(told) == 1 and "reconcile --date" in told[0]
    assert unicorn.reconcile(app, DAY, message_id=77) == "published"  # the owner saw post 77 in the channel
    d = app.store.draft_get(DAY)
    assert d["status"] == "published" and d["message_ids"] in ([77], "[77]") and "acme" in app.store.archived_names()
    assert db(app).publication_by_date(DAY)["telegram_message_id"] == 77 and not posts(app)
    assert unicorn.publish(app, DAY) == "already_published"


def test_ambiguous_photo_failure_does_not_fall_back_to_a_text_post(make_app, monkeypatch):
    app = prepared_app(make_app)
    monkeypatch.setattr(unicorn, "hero_photo", lambda report: {"src": "https://example.com/p.jpg"})
    app.telegram.send_photo = lambda *a, **k: (_ for _ in ()).throw(TelegramError("x", ambiguous=True))
    unicorn.publish(app, DAY)
    assert not posts(app)  # the text fallback would have been a duplicate


def test_definite_refusal_is_retried(make_app):
    app = prepared_app(make_app)
    boom_send(app, TelegramError("sendMessage failed: chat not found"))
    assert unicorn.publish(app, DAY) == "failed"
    restore_send(app)
    assert unicorn.publish(app, DAY) == "published" and len(posts(app)) == 1


def test_crash_before_the_telegram_call_is_not_published(make_app):
    """3. crash after intent=sending but before Telegram: not 'published', no downstream steps, owner decides."""
    app = prepared_app(make_app)
    boom_send(app, Crash())
    with pytest.raises(Crash):
        unicorn.publish(app, DAY)
    restore_send(app)
    assert unicorn.publish(app, DAY) == "requires_manual_reconcile"
    d = app.store.draft_get(DAY)
    assert d["status"] == "ready" and "acme" not in app.store.archived_names() and not posts(app)
    assert not any("question" in m for m in app.telegram.sent)
    assert unicorn.reconcile(app, DAY, not_sent=True) == "cleared"
    assert unicorn.publish(app, DAY) == "published" and len(posts(app)) == 1
    assert "acme" in app.store.archived_names()


def test_reconcile_needs_exactly_one_choice_and_a_pending_state(make_app):
    app = prepared_app(make_app)
    assert unicorn.reconcile(app, DAY, message_id=1) == "nothing_to_reconcile"
    app.store.set(unicorn._state_key(DAY), unicorn.MANUAL)
    with pytest.raises(ValueError):
        unicorn.reconcile(app, DAY)
    with pytest.raises(ValueError):
        unicorn.reconcile(app, DAY, message_id=1, not_sent=True)


def crash_after_page(app):
    """createPage ran (the page exists) but its answer was lost: the state says 'creating'."""
    app.telegraph.crash_after_create = 1
    with pytest.raises(Crash):
        unicorn.publish(app, DAY)
    app.telegraph.crash_after_create = 0
    assert len(app.telegraph.pages) == 1 and not app.store.draft_get(DAY).get("page_url")


def test_crash_after_page_creation_reuses_the_page(make_app):
    app = prepared_app(make_app)
    crash_after_page(app)
    assert unicorn.publish(app, DAY) == "published"
    assert len(app.telegraph.pages) == 1
    assert app.store.draft_get(DAY)["page_url"] == "https://telegra.ph/test-page"
    assert flat(app.telegraph.by_url["https://telegra.ph/test-page"][1]).count("Завтра — новая история большого бизнеса") == 1


def test_failed_page_lookup_never_creates_a_second_page(make_app):
    """4. getPageList fails while recovering: no createPage, a clear failure, recovery on the next run."""
    app = prepared_app(make_app)
    crash_after_page(app)
    app.telegraph.fail_lookup = True
    assert unicorn.publish(app, DAY) == "failed"
    assert len(app.telegraph.pages) == 1 and not posts(app)
    app.telegraph.fail_lookup = False
    assert unicorn.publish(app, DAY) == "published" and len(app.telegraph.pages) == 1


def test_same_title_collision_is_refused_not_guessed(make_app):
    """5. two pages with the title: unsafe to pick one, unsafe to create another."""
    app = prepared_app(make_app)
    crash_after_page(app)
    title = app.telegraph.pages[0][0]
    app.telegraph.create_page(title, [{"tag": "p", "children": ["older orphan"]}])
    assert unicorn.publish(app, DAY) == "failed"
    assert len(app.telegraph.pages) == 2 and not posts(app)


class ListHttp:
    def __init__(self, pages):
        self.pages = pages

    def request(self, method, url, json_body=None, **kw):
        assert url.endswith("/getPageList") and json_body["limit"] == 200
        return {"ok": True, "result": {"pages": self.pages}}


def test_telegraph_find_page_exact_title_unique_only():
    mk = lambda pages: TelegraphClient(ListHttp(pages), lambda: "tok", lambda t: None)
    page = lambda t, u: {"title": t, "url": u}
    assert mk([page("A", "u1"), page("B", "u2")]).find_page("B") == "u2"
    assert mk([page("A", "u1")]).find_page("B") is None
    with pytest.raises(TelegramError):
        mk([page("B", "u1"), page("B", "u2")]).find_page("B")


def test_network_errors_are_marked_ambiguous_and_4xx_are_not():
    from investor_agent.telegram import TelegramClient

    class H:
        def __init__(self, exc):
            self.exc = exc

        def request(self, *a, **k):
            raise self.exc
    for exc, ambiguous in ((HttpError(502, "bad"), True), (HttpError(599, "retries"), True), (TimeoutError(), True),
                           (HttpError(400, "bad"), False), (HttpError(429, "flood"), False)):
        with pytest.raises(TelegramError) as info:
            TelegramClient(H(exc), "tok").send("@c", "x")
        assert info.value.ambiguous is ambiguous and "tok" not in str(info.value)


# --- review 3 ---------------------------------------------------------------------------------------------------
import httpx

from investor_agent import glossary
from investor_agent.http import HttpxClient
from investor_agent.telegram import TelegramClient
from investor_agent.unicorn import poll as upoll


class Wire:
    """A production HttpxClient on a mock transport; records every POST that actually left."""

    def __init__(self, responder):
        self.calls, self.responder = [], responder
        self.http = HttpxClient(retries=3)
        self.http._client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        body = json.loads(request.content or b"{}")
        self.calls.append((request.url.path.rsplit("/", 1)[-1], body))
        return self.responder(len(self.calls), request.url.path.rsplit("/", 1)[-1], body)


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def test_httpx_502_on_send_is_one_post_and_ambiguous():
    wire = Wire(lambda n, method, body: httpx.Response(502, text="bad gateway") if n == 1 else ok({"message_id": 9}))
    with pytest.raises(TelegramError) as info:
        TelegramClient(wire.http, "tok").send("@c", "x")
    assert len(wire.calls) == 1 and info.value.ambiguous  # 502 -> 200 would have posted twice


def test_httpx_idempotent_calls_still_retry_and_429_is_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    wire = Wire(lambda n, method, body: httpx.Response(502) if n == 1 else ok(True))
    assert TelegramClient(wire.http, "tok").pin("@c", 5) is True and len(wire.calls) == 2  # pin is idempotent
    wire = Wire(lambda n, method, body: httpx.Response(429, json={"ok": False}) if n == 1 else ok({"message_id": 3}))
    assert TelegramClient(wire.http, "tok").send("@c", "x") == 3 and len(wire.calls) == 2  # refused, not executed


def test_httpx_408_and_telegraph_create_page_are_not_repeated(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    wire = Wire(lambda n, method, body: httpx.Response(408, text="timeout"))
    with pytest.raises(TelegramError) as info:
        TelegramClient(wire.http, "tok").send("@c", "x")
    assert len(wire.calls) == 1 and info.value.ambiguous
    wire = Wire(lambda n, method, body: httpx.Response(502) if n == 1 else httpx.Response(200, json={"ok": True, "result": {"url": "u"}}))
    with pytest.raises(TelegramError) as info:
        TelegraphClient(wire.http, lambda: "tok", lambda t: None).create_page("T", [])
    assert [c[0] for c in wire.calls] == ["createPage"] and info.value.ambiguous


def test_publish_over_real_http_stack_502_means_manual_reconcile(make_app):
    app = prepared_app(make_app)
    channel_posts = []

    def responder(n, method, body):
        if body.get("chat_id") == "@investor_duker" and "Компания дня" in (body.get("text") or body.get("caption") or ""):
            channel_posts.append(n)
            return httpx.Response(502, text="bad gateway") if len(channel_posts) == 1 else ok({"message_id": 9})
        return ok({"message_id": 5})
    wire = Wire(responder)
    app.telegram = TelegramClient(wire.http, "tok")
    assert unicorn.publish(app, DAY) == "requires_manual_reconcile"
    assert unicorn.publish(app, DAY) == "requires_manual_reconcile"
    assert len(channel_posts) == 1  # exactly one network POST to the channel, ever
    assert unicorn.reconcile(app, DAY, message_id=9) == "published"
    assert len(channel_posts) == 1


# message_id
@pytest.mark.parametrize("result", [{"message_id": -5}, {"message_id": 0}, {}, {"message_id": "x"}])
def test_invalid_message_id_is_an_ambiguous_error(result):
    wire = Wire(lambda n, method, body: ok(result))
    with pytest.raises(TelegramError) as info:
        TelegramClient(wire.http, "tok").send("@c", "x")
    assert info.value.ambiguous
    with pytest.raises(TelegramError):
        TelegramClient(Wire(lambda n, m, b: ok(result)).http, "tok").send_poll("@c", "q", ["a", "b"])


def test_negative_message_id_cannot_be_reconciled_or_recorded(make_app):
    app = prepared_app(make_app)
    app.store.set(unicorn._state_key(DAY), unicorn.MANUAL)
    with pytest.raises(ValueError):
        unicorn.reconcile(app, DAY, message_id=-3)
    assert unicorn.reconcile(app, DAY, message_id=0 or None, not_sent=True) == "cleared"
    db(app).publication_upsert(DAY, "Acme")
    gpub.post_sent(app, DAY, {"company_name": "Acme"}, "https://telegra.ph/x", -1)
    assert db(app).publication_by_date(DAY)["telegram_message_id"] is None
    app.store.set(unicorn._state_key(DAY), "sent:-4")
    assert unicorn.publish(app, DAY) == "failed" and app.store.draft_get(DAY)["status"] == "ready"


# poll
def test_crash_after_poll_sent_does_not_send_it_twice(make_app):
    app = prepared_app(make_app)
    sent = []

    def poll_then_crash(chat, q, options):
        sent.append(q)
        raise Crash()
    app.telegram.send_poll = poll_then_crash
    with pytest.raises(Crash):
        unicorn.publish(app, DAY)
    assert app.store.draft_get(DAY)["status"] == "ready"
    app.telegram.send_poll = lambda chat, q, o: sent.append(q) or 50
    assert unicorn.publish(app, DAY) == "published"
    assert len(sent) == 1 and len(posts(app)) == 1  # the poll was not repeated, the post neither


def test_ambiguous_poll_error_is_not_retried_but_a_refusal_is(make_app):
    app = make_app()
    told = []
    app.notify_admin = told.append
    calls = []

    def amb(chat, q, o):
        calls.append(1)
        raise TelegramError("sendPoll: timeout", ambiguous=True)
    app.telegram.send_poll = amb
    assert upoll.post(app, "2026-11-10", ["a", "b"]) is None and upoll.post(app, "2026-11-10", ["a", "b"]) is None
    assert len(calls) == 1 and len(told) == 2
    def refuse(chat, q, o):
        calls.append(2)
        raise TelegramError("sendPoll: chat not found")
    app.telegram.send_poll = refuse
    upoll.post(app, "2026-11-11", ["a", "b"])
    upoll.post(app, "2026-11-11", ["a", "b"])
    assert calls.count(2) == 2


# glossary
def glossary_app(make_app):
    app = make_app()
    glossary.add_terms(app, glossary.clean_terms([{"term": "ARR", "group": glossary.GROUPS[0],
                                                   "definition": "Годовая повторяющаяся выручка компании."}]))
    return app


def test_crash_while_sending_the_glossary_is_not_repeated_automatically(make_app):
    app = glossary_app(make_app)
    app.telegram.send = lambda *a, **k: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        glossary.sync(app)
    app.telegram.__dict__.pop("send")
    assert glossary.sync(app) == "needs_manual" and not app.telegram.sent[1:]
    assert len([m for m in app.telegram.sent if "ГЛОССАРИЙ" in m["html"]]) == 0
    assert glossary.sync(app, force=True) == "created"  # the manual command after the owner checked the channel
    assert len([m for m in app.telegram.sent if "ГЛОССАРИЙ" in m["html"]]) == 1


def test_ambiguous_glossary_send_waits_but_a_refusal_retries(make_app):
    app = glossary_app(make_app)
    app.notify_admin = lambda t: None
    app.telegram.send = lambda *a, **k: (_ for _ in ()).throw(TelegramError("timeout", ambiguous=True))
    assert glossary.sync(app) == "failed"
    app.telegram.__dict__.pop("send")
    assert glossary.sync(app) == "needs_manual"


def test_refused_glossary_send_is_retried(make_app):
    app2 = glossary_app(make_app)
    app2.notify_admin = lambda t: None
    app2.telegram.send = lambda *a, **k: (_ for _ in ()).throw(TelegramError("chat not found"))
    assert glossary.sync(app2) == "failed"
    app2.telegram.__dict__.pop("send")
    assert glossary.sync(app2) == "created"


def test_glossary_message_id_is_saved_before_the_pin(make_app):
    app = glossary_app(make_app)
    app.telegram.pin = lambda *a: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        glossary.sync(app)
    state = json.loads(app.store.get(glossary.STATE_KEY))
    assert state["message_ids"] == [1] and "sending" not in state
    app.telegram.__dict__.pop("pin")
    assert glossary.sync(app) == "updated"
    heads = [m["html"] for m in app.telegram.sent if "ГЛОССАРИЙ «ШКОЛЫ" in m["html"]]
    assert len(heads) == 1 and len(app.telegram.sent) == 2  # message 1 edited (not re-posted), message 2 posted once


# telegraph parts
def big_nodes():
    nodes = []
    for i in range(6):
        nodes.append({"tag": "h3", "children": [f"Раздел {i}"]})
        nodes.append({"tag": "p", "children": ["я" * 20_000]})
    return nodes


class Progress:
    def __init__(self):
        self.state = {}

    def pair(self):
        return (lambda: json.loads(json.dumps(self.state)), lambda st: setattr(self, "state", json.loads(json.dumps(st))))


def chain_ok(graph, first):
    seen, url = [], first
    while url:
        seen.append(url)
        url = unicorn.growth_publish.share.next_part_url(graph.by_url[url][1])
    return seen


def test_multipage_chain_survives_a_crash_between_parts_without_duplicates():
    from conftest import FakeTelegraph
    graph, prog = FakeTelegraph(), Progress()
    graph.crash_after_create = 3  # parts 5, 4 saved, part 3 created but its answer lost
    with pytest.raises(Crash):
        unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair())
    graph.crash_after_create = 0
    assert prog.state["creating"] == 3 and len(prog.state["urls"]) == 2
    first = unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair())
    assert len(graph.pages) == 6 and len(chain_ok(graph, first)) == 6  # found by title, nothing created twice
    assert prog.state["creating"] is None and len(prog.state["urls"]) == 6


def test_multipage_resume_after_a_clean_stop_and_failed_lookup():
    from conftest import FakeTelegraph
    graph, prog = FakeTelegraph(), Progress()
    graph.crash_after_create = 2
    with pytest.raises(Crash):
        unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair())
    graph.crash_after_create, graph.fail_lookup = 0, True
    with pytest.raises(TelegramError):
        unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair())
    assert len(graph.pages) == 2  # the failed lookup created nothing
    graph.fail_lookup = False
    first = unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair())
    assert len(graph.pages) == 6 and len(chain_ok(graph, first)) == 6


def test_multipage_changed_split_after_a_crash_needs_manual_check():
    from conftest import FakeTelegraph
    graph, prog = FakeTelegraph(), Progress()
    graph.crash_after_create = 2
    with pytest.raises(Crash):
        unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair())
    with pytest.raises(RuntimeError):
        unicorn.create_pages(graph, "Заголовок", big_nodes()[:4], prog.pair())
    assert len(graph.pages) == 2


# TGStat UTC / Beijing
def test_tgstat_daily_check_uses_the_beijing_date(make_app):
    app = tgstat_app(make_app, OK)
    assert collect.collect_tgstat(app, datetime(2026, 11, 30, 15, tzinfo=timezone.utc)) == "ok"   # 23:00 on 30 Nov, Beijing
    assert collect.collect_tgstat(app, datetime(2026, 11, 30, 17, tzinfo=timezone.utc)) == "ok"   # 01:00 on 1 Dec: new day
    assert len(app.http.calls) == 2
    assert collect.collect_tgstat(app, datetime(2026, 12, 1, 3, tzinfo=timezone.utc)) == "done_today"  # same Beijing day


def test_tgstat_same_beijing_day_until_23_59(make_app):
    app2 = tgstat_app(make_app, OK)
    assert collect.collect_tgstat(app2, datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == "ok"   # 09:00 on 9 Nov
    assert collect.collect_tgstat(app2, datetime(2026, 11, 9, 15, 59, tzinfo=timezone.utc)) == "done_today"  # 23:59 Beijing
    assert collect.collect_tgstat(app2, datetime(2026, 11, 9, 16, 1, tzinfo=timezone.utc)) == "ok"  # 00:01 on 10 Nov


def test_tgstat_pause_ends_at_the_start_of_the_next_beijing_month(make_app):
    app = tgstat_app(make_app, HttpError(429, "limit"), tgstat_access_until="2027-12-31")
    assert collect.collect_tgstat(app, datetime(2026, 11, 30, 17, tzinfo=timezone.utc)) == "error"  # already 1 Dec in Beijing
    assert db(app).provider("tgstat")["disabled_until"] == "2026-12-31T16:00:00+00:00"  # 1 Jan 00:00 Beijing
    app.http.result = OK
    assert collect.collect_tgstat(app, datetime(2026, 12, 31, 15, 59, tzinfo=timezone.utc)) == "paused"
    assert collect.collect_tgstat(app, datetime(2026, 12, 31, 16, 1, tzinfo=timezone.utc)) == "ok"  # 00:01 on 1 Jan, Beijing


def test_tgstat_pause_mid_month_ends_on_the_beijing_first(make_app):
    app2 = tgstat_app(make_app, HttpError(429, "limit"))
    collect.collect_tgstat(app2, datetime(2026, 11, 10, 1, tzinfo=timezone.utc))
    assert db(app2).provider("tgstat")["disabled_until"] == "2026-11-30T16:00:00+00:00"


# --- review 4 ---------------------------------------------------------------------------------------------------
def test_crash_after_sendpoll_before_poll_key_is_recoverable_by_accept(make_app, monkeypatch):
    app = make_app()
    sent = []
    app.telegram.send_poll = lambda chat, q, o: sent.append(1) or 41
    monkeypatch.setattr(upoll, "remember", lambda *a: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        upoll.post(app, "2026-11-10", ["a", "b", "c", "d"])  # sendPoll succeeded, history/poll_key not written yet
    monkeypatch.undo()
    assert app.store.get(upoll.poll_key("2026-11-10")) is None
    assert upoll.post(app, "2026-11-10", ["a", "b", "c", "d"]) is None and len(sent) == 1  # never repeated
    with pytest.raises(ValueError):
        upoll.accept(app, "2026-11-10", -1)
    assert upoll.accept(app, "2026-11-10", 41) == "accepted"
    saved = json.loads(app.store.get(upoll.poll_key("2026-11-10")))
    assert saved["message_id"] == 41 and saved["options"] == ["a", "b", "c", "d"]
    assert upoll._history(app) and upoll.accept(app, "2026-11-10", 41) == "nothing_to_accept"
    assert upoll.post(app, "2026-11-10", ["a", "b", "c", "d"]) is None and len(sent) == 1


def test_poll_key_and_history_are_written_before_the_intent_is_cleared(make_app):
    app = make_app()
    app.telegram.send_poll = lambda chat, q, o: 7
    seen = []
    real_set = app.store.set

    def spy(key, value):
        if key.startswith("unicorn_poll_intent:") and value == "":
            seen.append(bool(app.store.get(upoll.poll_key("2026-11-10"))))
        real_set(key, value)
    app.store.set = spy
    upoll.post(app, "2026-11-10", ["a", "b"])
    assert seen == [True]


def test_pages_same_count_but_different_split_never_creates(make_app):
    from conftest import FakeTelegraph
    graph, prog = FakeTelegraph(), Progress()
    graph.crash_after_create = 2
    with pytest.raises(Crash):
        unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair())
    changed = big_nodes()
    changed[1]["children"] = ["ж" * 20_000]  # same number of parts, different content
    graph.crash_after_create = 0
    with pytest.raises(RuntimeError):
        unicorn.create_pages(graph, "Заголовок", changed, prog.pair())
    retitled = big_nodes()
    retitled[2]["children"] = ["Другой заголовок"]  # same count, other part title
    with pytest.raises(RuntimeError):
        unicorn.create_pages(graph, "Заголовок", retitled, prog.pair())
    assert len(graph.pages) == 2
    assert len(chain_ok(graph, unicorn.create_pages(graph, "Заголовок", big_nodes(), prog.pair()))) == 6


def test_tgstat_500_then_200_is_one_wire_request_and_one_counted_call(make_app):
    wire = Wire(lambda n, m, b: httpx.Response(500, text="boom") if n == 1 else httpx.Response(200, json=OK))
    app = make_app(http=wire.http)
    app.settings.tgstat_api_token = "T"
    assert collect.collect_tgstat(app, datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == "error"
    assert len(wire.calls) == 1 and db(app).provider_calls("tgstat", "2026-11") == 1


def test_tgstat_429_is_not_retried_inside_the_client_either(make_app):
    wire = Wire(lambda n, m, b: httpx.Response(429, text="limit") if n == 1 else httpx.Response(200, json=OK))
    app = make_app(http=wire.http)
    app.settings.tgstat_api_token = "T"
    assert collect.collect_tgstat(app, datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == "error"
    assert len(wire.calls) == 1 and db(app).provider_calls("tgstat", "2026-11") == 1
    assert db(app).provider("tgstat")["disabled_until"] is not None


def test_status_lists_every_unresolved_state_and_blocks_rollback(make_app):
    app = make_app()
    assert unicorn.unresolved_states(app) == {"publish": {}, "pages": {}, "poll": {}, "glossary": None}
    app.store.set(unicorn._state_key(DAY), "sent:5")
    app.store.set(f"unicorn_pages:{DAY}", json.dumps({"n": 2}))
    app.store.set("unicorn_poll_intent:2026-11-10", "sending")
    app.store.set("unicorn_poll_intent:2026-11-10:options", "[]")
    app.store.set(glossary.STATE_KEY, json.dumps({"sending": 0}))
    got = unicorn.unresolved_states(app)
    assert got == {"publish": {DAY: "sent:5"}, "pages": {DAY: "unfinished"}, "poll": {"2026-11-10": "sending"},
                   "glossary": "sending"}
    for key in (unicorn._state_key(DAY), f"unicorn_pages:{DAY}", "unicorn_poll_intent:2026-11-10"):
        app.store.set(key, "")
    app.store.set(glossary.STATE_KEY, json.dumps({"message_ids": [1]}))
    assert unicorn.unresolved_states(app) == {"publish": {}, "pages": {}, "poll": {}, "glossary": None}


def test_pages_state_is_cleared_after_a_normal_publication(make_app):
    app = published_app(make_app)
    assert unicorn.unresolved_states(app) == {"publish": {}, "pages": {}, "poll": {}, "glossary": None}


def test_telegraph_transport_errors_are_ambiguous(make_app):
    class Down:
        def request(self, *a, **k):
            raise httpx.ReadTimeout("slow")
    for exc in (httpx.ReadTimeout("slow"), httpx.ConnectError("down"), TimeoutError(), ConnectionResetError()):
        class H:
            def request(self, *a, _e=exc, **k):
                raise _e
        with pytest.raises(TelegramError) as info:
            TelegraphClient(H(), lambda: "tok", lambda t: None).create_page("T", [])
        assert info.value.ambiguous and "tok" not in str(info.value)
    wire = Wire(lambda n, m, b: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with pytest.raises(TelegramError) as info:
        TelegraphClient(wire.http, lambda: "tok", lambda t: None).create_page("T", [])
    assert info.value.ambiguous and len(wire.calls) == 1


def test_stale_poll_intent_is_cleared_when_poll_key_exists(make_app):
    app = make_app()
    app.telegram.send_poll = lambda chat, q, o: 5
    upoll.post(app, "2026-11-10", ["a", "b"])
    app.store.set("unicorn_poll_intent:2026-11-10", "sending")  # the crash left the intent behind
    app.store.set("unicorn_poll_intent:2026-11-10:told", "1")
    assert unicorn.unresolved_states(app)["poll"] == {"2026-11-10": "sending"}
    assert upoll.post(app, "2026-11-10", ["a", "b"]) is None
    assert unicorn.unresolved_states(app)["poll"] == {} and not app.store.get("unicorn_poll_intent:2026-11-10:told")


# --- «Разгадка бизнеса» gets the same footer ---------------------------------------------------------------------
from urllib.parse import unquote

from test_riddle import CountingHost, media_http, story
from investor_agent.riddle import pipeline as riddle


def riddle_app(make_app, mode="draft"):
    ds = FakeDeepSeek([lambda reg, p: story(reg)])
    app = make_app(deepseek=ds, http=media_http(), images=CountingHost())
    app.settings.riddle_publish_mode = mode
    assert riddle.prepare(app, DAY) == "ready"
    assert riddle.publish(app, DAY) == "published"
    return app


def anchors(nodes):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("tag") == "a":
                out.append((n["attrs"]["href"], flat(n)))
            for c in n.get("children", []):
                walk(c)
    for n in nodes:
        walk(n)
    return out


def test_riddle_footer_has_personal_link_subscribe_text_share_and_friend_button(make_app):
    app = riddle_app(make_app, "auto")
    nodes = app.telegraph.by_url["https://telegra.ph/test-page"][1]
    text = flat(nodes)
    mine = [a for a in anchors(nodes) if a[0] == "https://t.me/+inv1"]
    assert len(mine) == 2 and "img" in mine[0][1] and "ШКОЛУ ЕДИНОРОГОВ" in mine[1][1]  # banner + subscribe link: one link
    assert app.telegram.invites == ["telegraph-cta-riddle-2026-11-09"]
    assert "Завтра — новая история большого бизнеса" in text and "→ Подписаться на «ШКОЛУ ЕДИНОРОГОВ»" in text
    assert "Новые расследования каждый день" not in text and "Перейти на канал" not in text  # old lines are gone
    assert "Хорошими идеями стоит делиться" in text and "t.me/share/url" in text and "vk.com/share.php" in text and "wa.me" in text
    tg = [h for h, _ in anchors(nodes) if h.startswith("https://t.me/share/url")][0]
    shared = unquote(tg)
    assert "text=Как скучная накладная стала бизнесом на миллиард: " in shared  # the headline leads, not the company name
    gdb = db(app)
    pub = gdb.publication_by_date(DAY, "riddle")
    assert pub["telegram_message_id"] == 1 and pub["share_done"] and gdb.publication_by_date(DAY) is None
    rows = app.telegram.buttons[1]
    assert rows[0][0][0] == "🔎 Читать расследование" and rows[1][0][0] == "📤 Поделиться в TG"
    assert "https%3A%2F%2Ft.me%2Finvestor_duker%2F1" in rows[1][0][1]


def test_riddle_draft_mode_creates_no_invite_links_and_posts_to_admin(make_app):
    app = riddle_app(make_app)
    nodes = app.telegraph.by_url["https://telegra.ph/test-page"][1]
    assert not getattr(app.telegram, "invites", [])
    assert any(h == "https://t.me/investor_duker" and "img" in t for h, t in anchors(nodes))  # banner: public link
    assert "Хорошими идеями стоит делиться" in flat(nodes)
    assert app.telegram.sent[0]["chat"] == "111" and 1 in app.telegram.buttons
    assert riddle.publish(app, DAY) == "already_published"
    assert flat(app.telegraph.by_url["https://telegra.ph/test-page"][1]).count("Хорошими идеями стоит делиться") == 1


def test_riddle_and_unicorn_of_the_same_day_are_separate_publications(make_app):
    app = riddle_app(make_app)
    gdb = db(app)
    gdb.publication_upsert(DAY, "Other", platform="telegram")
    assert gdb.publication_by_date(DAY, "riddle")["company_name"] == "Acme"
    assert gdb.publication_by_date(DAY)["company_name"] == "Other"


def test_growth_off_keeps_the_old_riddle_footer(make_app):
    ds = FakeDeepSeek([lambda reg, p: story(reg)])
    app = make_app(deepseek=ds, http=media_http(), images=CountingHost())
    app.settings.growth_enabled = False
    riddle.prepare(app, DAY)
    assert riddle.publish(app, DAY) == "published"
    text = flat(app.telegraph.pages[0][1])
    assert "Новые расследования каждый день" in text and "Хорошими идеями стоит делиться" not in text
    assert not getattr(app.telegram, "buttons", {})


# --- footer v3 (Илья's text), the checks the owner asked for ---------------------------------------------------
def test_footer_with_a_real_article_url_and_three_share_links_of_that_article(make_app):
    app = published_app(make_app)
    page = app.telegraph.by_url["https://telegra.ph/test-page"][1]
    created = app.telegraph.pages[0][1]  # what createPage got: everything above the share part
    texts = [share._text(n) for n in page]
    assert [n for n in page if n.get("tag") == "h4"][-2:] == [{"tag": "h4", "children": ["Завтра — новая история большого бизнеса"]},
                                                              {"tag": "h4", "children": ["Хорошими идеями стоит делиться"]}]
    links = {share._text(a): a["attrs"]["href"] for a in page[-1]["children"] if isinstance(a, dict) and a.get("tag") == "a"}
    assert set(links) == {"Telegram", "VK", "WhatsApp"} and texts[-1].startswith("Поделиться статьёй:")
    for name, href in links.items():  # every link carries THIS article (never the channel), encoded
        assert "telegra.ph%2Ftest-page" in href and "investor_duker" not in href, name
    assert links["Telegram"].startswith("https://t.me/share/url?url=") and "&text=" in links["Telegram"]
    assert links["VK"].startswith("https://vk.com/share.php?url=") and links["WhatsApp"].startswith("https://wa.me/?text=")
    assert "%D0%" in links["Telegram"] and " " not in "".join(links.values())  # Cyrillic is percent-encoded
    # the banner and everything that was created stays: the footer only appended the share part
    assert page[:len(created)] == created and len(page) == len(created) + 3
    assert any(n.get("tag") == "figure" for n in created[-4:])  # the banner is still right above the subscribe text


def test_old_footer_texts_are_gone(make_app):
    app = published_app(make_app)
    text = flat(app.telegraph.by_url["https://telegra.ph/test-page"][1])
    for old in ("Перейти на канал", "Было полезно", "Эту статью прислали", "Поделиться разбором", "Подписаться на Школу",
                "Каждый день разбираем бизнес", "Подписывайтесь на"):
        assert old not in text, old
    assert '"hr"' not in text[-1500:]


def _footer(app):
    nodes = app.telegraph.by_url["https://telegra.ph/test-page"][1]
    start = next(i for i, n in enumerate(nodes) if share._text(n) == share.SUBSCRIBE_TITLE)
    return [(n.get("tag"), share._text(n)) for n in nodes[start:]]


def _expected_footer():
    urls = {"telegram": "t", "vk": "v", "whatsapp": "w"}
    return [(n.get("tag"), share._text(n)) for n in share.subscribe_nodes("h") + share.share_nodes(urls)]


def test_unicorn_footer_comes_from_the_one_template(make_app):
    assert _footer(published_app(make_app)) == _expected_footer()


def test_riddle_footer_comes_from_the_same_template(make_app):
    got = _footer(riddle_app(make_app))
    assert got == _expected_footer() and "10:00" not in "".join(t for _, t in got)
