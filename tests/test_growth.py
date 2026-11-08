import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from conftest import FakeDeepSeek, FakeTelegram
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
    text = flat(nodes)
    assert "Каждый день разбираем бизнес, который смог вырасти до миллиардной оценки." in text
    assert "Подписывайтесь на „Школу единорогов“, чтобы находить идеи для собственного бизнеса." in text
    assert "Подписаться на Школу единорогов" in text
    tags = {n["tag"] for n in nodes} | {"a", "b"}
    assert tags <= {"hr", "p", "a", "b", "h4", "br"}


# --- publication integration ----------------------------------------------------------------------------------
def test_publish_adds_cta_share_buttons_and_records_everything(make_app):
    app = published_app(make_app)
    page_nodes = flat(app.telegraph.by_url["https://telegra.ph/test-page"][1])
    assert "Подписаться на Школу единорогов" in page_nodes and "https://t.me/+inv1" in page_nodes
    assert "Поделиться разбором" in page_nodes and "t.me/share/url" in page_nodes and "wa.me" in page_nodes
    gdb = db(app)
    pub = gdb.publication_by_date(DAY)
    assert pub["article_url"] == "https://telegra.ph/test-page" and pub["telegram_message_id"] == 1 and pub["share_done"]
    rows = app.telegram.buttons[1]
    assert rows[0][0][0] == "📖 Читать разбор" and rows[1][0][0] == "📤 Отправить другу"
    assert "https%3A%2F%2Ft.me%2Finvestor_duker%2F1" in rows[1][0][1]  # shares the channel post
    assert app.telegram.invites == ["telegraph-cta-2026-11-09"]


def test_republish_and_retry_do_not_duplicate_the_block(make_app):
    app = published_app(make_app)
    assert unicorn.publish(app, DAY) == "already_published"
    pub = db(app).publication_by_date(DAY)
    assert gpub.ensure_share_block(app, pub) is True
    assert gpub.ensure_share_block(app, pub) is True
    assert flat(app.telegraph.by_url["https://telegra.ph/test-page"][1]).count("Поделиться разбором") == 1
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
    assert "Поделиться разбором" in flat(app.telegraph.by_url[last][1])


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
    nodes = flat(gpub.begin(app, DAY, {"company_name": "Acme"}))
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
    assert db(app).provider("tgstat")["disabled_until"].startswith("2026-12-01")
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


@pytest.mark.parametrize("status,until", [(401, "2026-11-12"), (403, "2026-11-12"), (402, "2026-12-01"), (429, "2026-12-01")])
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


class Crash(BaseException):
    """A killed process: not caught by `except Exception`."""


def prepared_app(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("B", industry="hr"), cand("C", industry="edtech")]}
    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg)]))
    unicorn.prepare(app, DAY)
    return app


def test_crash_while_sending_the_post_never_sends_it_twice(make_app):
    app = prepared_app(make_app)
    real = app.telegram.send_photo
    app.telegram.send_photo = app.telegram.send = lambda *a, **k: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        unicorn.publish(app, DAY)
    app.telegram.send_photo, app.telegram.send = real, FakeTelegram.send.__get__(app.telegram)
    assert unicorn.publish(app, DAY) == "unknown_sent"
    assert not any("Компания дня" in m.get("html", "") for m in app.telegram.sent)
    assert app.store.draft_get(DAY)["status"] == "published" and "Проверьте канал" in app.telegram.sent[-1]["html"]
    assert unicorn.publish(app, DAY) == "already_published"


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
