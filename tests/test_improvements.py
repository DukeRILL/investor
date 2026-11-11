"""Owner's improvements of 2026-11-07: Yahoo fallback, retries, fact check, digest, continuation, glossary, cache."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeDeepSeek, FakeHttp, FakeTelegram
from test_agents import cand, good_report

from investor_agent import glossary
from investor_agent.deepseek import BudgetExceededError, DeepSeekClient
from investor_agent.http import HttpError
from investor_agent.radar import digest, market, pipeline as radar
from investor_agent.scheduler import Scheduler
from investor_agent.sources import SourceRegistry
from investor_agent.timeutil import BEIJING
from investor_agent.unicorn import precheck, pipeline as unicorn


# --- Yahoo ---------------------------------------------------------------------------------
class YahooHttp:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def request(self, method, url, **kw):
        self.calls.append(url)
        out = self.behaviour(url)
        if isinstance(out, Exception):
            raise out
        return out


def yahoo_json(price=100.0, prev=98.0, ts=1_800_000_000):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "previousClose": prev, "regularMarketTime": ts}}]}}


def test_yahoo_second_host_and_cache_on_429(make_app):
    app = make_app()
    now = datetime.fromtimestamp(1_800_000_100, tz=timezone.utc)
    # query1 refuses, query2 answers
    http = YahooHttp(lambda url: HttpError(429, "x") if "query1" in url else yahoo_json())
    errors: list[str] = []
    got = market.yahoo_quotes(http, errors, app.store, now)
    assert set(got) == set(market.YAHOO) and not errors
    # both refuse: the last good values (fresh enough) come from the cache, marked as such
    http = YahooHttp(lambda url: HttpError(429, "x"))
    errors = []
    got = market.yahoo_quotes(http, errors, app.store, now + timedelta(hours=1))
    assert set(got) == set(market.YAHOO) and "кэш" in got["SPX"].source and len(errors) == len(market.YAHOO)
    assert len(http.calls) == 2  # after the first double refusal Yahoo is not hammered again
    # too old for the cache -> a clear gap, no fake data
    errors = []
    assert market.yahoo_quotes(YahooHttp(lambda u: HttpError(429, "x")), errors, app.store, now + timedelta(hours=13)) == {}
    assert errors and all("Yahoo недоступен" in e for e in errors)


# --- DeepSeek cache and budget ---------------------------------------------------------------
def test_search_cache_and_daily_budget(make_app):
    app = make_app()
    http = YahooHttp(lambda url: {"results": [{"url": "https://a.com", "title": "A"}]})
    ds = DeepSeekClient(http, "k", store=app.store, daily_budget_usd=0.01, enforce_offpeak=False)
    assert ds.search("q", 5, cache_ttl=24) and ds.search("q", 5, cache_ttl=24)
    assert len(http.calls) == 1 and ds.usage.searches == 1  # the second one was free
    ds.search("q", 5)  # no ttl -> a real call again (radar needs fresh news)
    assert len(http.calls) == 2
    ds.usage.searches = 100  # about $0.075 > 2 x $0.01
    with pytest.raises(BudgetExceededError):
        ds.search("other")


def test_budget_alert_once_per_day(make_app):
    app = make_app()
    app.settings.daily_budget_usd = 1.0
    app.store.usage_add("radar", 1.5, {})
    app.deepseek.usage = type(app.deepseek.usage)()
    app.record_usage("radar")
    app.record_usage("radar")
    assert len([m for m in app.telegram.sent if m["chat"] == "111" and "лимит" in m["html"]]) == 1


# --- scheduler: retries every 3 hours ----------------------------------------------------------
def test_failed_prepare_retries_every_3_hours_until_22(make_app):
    app = make_app()
    clock = [datetime(2026, 11, 8, 4, 40, tzinfo=BEIJING)]  # a Sunday: no peak windows
    sch = Scheduler(app, clock=lambda: clock[0])
    job = sch._job("unicorn_prepare")
    sch._after(job, "failed")
    assert job.next_run == datetime(2026, 11, 8, 7, 40, tzinfo=BEIJING)
    clock[0] = datetime(2026, 11, 8, 19, 30, tzinfo=BEIJING)
    sch._after(job, "failed")  # 22:30 would be too late: wait for tomorrow's 04:30
    assert job.next_run == datetime(2026, 11, 9, 4, 30, tzinfo=BEIJING)
    assert any("повторных попыток больше нет" in m["html"] for m in app.telegram.sent)
    sch._after(job, "ready")
    assert job.next_run == datetime(2026, 11, 9, 4, 30, tzinfo=BEIJING)


def test_issue_ready_after_10_is_published_at_once(make_app):
    app = make_app()
    clock = datetime(2026, 11, 8, 13, 0, tzinfo=BEIJING)
    sch = Scheduler(app, clock=lambda: clock)
    sch._after(sch._job("unicorn_prepare"), "ready")
    assert sch._job("unicorn_publish").next_run == clock
    early = datetime(2026, 11, 8, 5, 0, tzinfo=BEIJING)
    sch2 = Scheduler(app, clock=lambda: early)
    sch2._after(sch2._job("unicorn_prepare"), "ready")
    assert sch2._job("unicorn_publish").next_run == datetime(2026, 11, 8, 10, 0, tzinfo=BEIJING)


def test_restart_redoes_failed_or_missing_preparation(make_app):
    app = make_app()
    clock = datetime(2026, 11, 8, 14, 0, tzinfo=BEIJING)
    app.store.draft_save("2026-11-08", "failed", {}, error="x")
    assert Scheduler(app, clock=lambda: clock)._job("unicorn_prepare").next_run == clock
    app.store.draft_save("2026-11-08", "published", {})
    assert Scheduler(app, clock=lambda: clock)._job("unicorn_prepare").next_run > clock


# --- radar digest ----------------------------------------------------------------------------
def _alert(app, n, sent_at):
    aid = app.store.alert_create(f"k{n}", f"Заголовок {n}", {"plain_words": [f"Вывод номер {n}."]})
    app.store.alert_mark(aid, "sent", [n])
    with app.store._conn() as c:
        c.execute("UPDATE radar_alerts SET sent_at=? WHERE id=?", (sent_at.isoformat(), aid))
    return aid


def test_digest_only_with_three_alerts_once(make_app):
    app = make_app()
    now = datetime(2026, 11, 8, 3, 0, tzinfo=BEIJING)  # 22:00 Moscow on 7 November
    start, end = digest.day_window(now)
    assert end == datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc) and start == end - timedelta(days=1)
    _alert(app, 1, end - timedelta(hours=20))
    _alert(app, 2, end - timedelta(hours=10))
    assert digest.run(app, now) == "SKIPPED" and not app.telegram.sent
    app.store.set("radar_digest:2026-11-07", "")  # allow a second try in the test
    _alert(app, 3, end - timedelta(hours=1))
    _alert(app, 4, end + timedelta(minutes=5))  # after the day's end: belongs to the next digest
    assert digest.run(app, now) == "SENT"
    html = app.telegram.sent[-1]["html"]
    assert "ИТОГИ ДНЯ" in html and "алертов: 3" in html and "Заголовок 3" in html and "Заголовок 4" not in html
    assert "Время чтения" in html and "Пекин" not in html
    assert digest.run(app, now) == "EXISTS"


# --- radar continuation ------------------------------------------------------------------------
def test_continuation_replies_to_and_quotes_previous_alert(make_app):
    app = make_app()
    prev_id = _alert(app, 7, datetime.now(timezone.utc))
    prev = radar._previous_alert(app, prev_id)
    assert prev and prev["message_ids"] == [7]
    assert radar._previous_alert(app, 999) is None and radar._previous_alert(app, None) is None
    line = radar.continuation_line(prev)
    assert "Продолжение" in line and "Заголовок 7" in line and "Вывод номер 7." in line
    parts = app.telegram.send_series("@c", ["a", "b"], reply_to=7)
    assert app.telegram.sent[0]["reply_to"] == 7 and app.telegram.sent[1]["reply_to"] is None and len(parts) == 2


# --- glossary ----------------------------------------------------------------------------------
def test_glossary_is_a_grouped_collapsed_pinned_post_that_grows(make_app):
    app = make_app()
    assert glossary.sync(app) == "created"
    first, last = app.telegram.sent[0], app.telegram.sent[-1]
    assert "ГЛОССАРИЙ" in first["html"] and "Время чтения" in last["html"] and first["button"] is None
    assert "<blockquote expandable>" in first["html"] and "<b>Раунды и оценка</b>" in "".join(m["html"] for m in app.telegram.sent)
    assert app.telegram.pinned == [1] and not app.telegraph.pages  # only the first message is pinned
    n = len(glossary.load(app))
    new = glossary.add_terms(app, glossary.clean_terms([
        {"term": "Acqui-hire", "group": "Раунды и оценка", "definition": "Покупка компании ради её команды, а не продукта или клиентов."},
        {"term": "Hedge", "group": "нет такой", "definition": "Страховка от потерь с помощью противоположной сделки на рынке."},
        {"term": "ARR", "definition": "Дубликат существующего термина, его добавлять не нужно вовсе."},
        {"term": "Bad", "definition": "english only definition that is rejected"}]))
    assert new == ["Acqui-hire", "Hedge"] and len(glossary.load(app)) == n + 2  # no per-issue cap
    assert glossary.sync(app) == "updated"
    assert "Acqui-hire" in "".join(e["html"] for e in app.telegram.edited)
    assert "Прочее" in "".join(e["html"] for e in app.telegram.edited)


def test_glossary_splits_by_group_when_too_long(make_app):
    app = make_app()
    terms = dict(glossary.load(app))
    for i in range(60):
        terms[f"Термин {i}"] = ("Рынок и продукт", "Длинное определение для проверки. " * 8)
    messages = glossary.build_messages(terms)
    assert len(messages) > 1 and all(len(m) <= 4096 for m in messages) and "Время чтения" in messages[-1]


def test_publish_adds_issue_terms_to_glossary_not_to_page(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}

    def report(reg, p):
        r = good_report(reg)
        r["glossary"] = [{"term": "Acqui-hire", "group": "Раунды и оценка",
                          "definition": "Покупка компании ради её команды, а не продукта."},
                         {"term": "ARR", "definition": "Уже есть в глоссарии, поэтому не должен добавляться снова."}]
        return r

    app = make_app(deepseek=FakeDeepSeek([stage1, report]), http=FakeHttp())
    assert unicorn.prepare(app, "2026-11-09") == "ready"
    assert [t["term"] for t in app.store.draft_get("2026-11-09")["payload"]["report"]["glossary"]] == ["Acqui-hire"]
    assert unicorn.publish(app, "2026-11-09") == "published"
    assert "Термины выпуска" not in json.dumps(app.telegraph.pages[0][1], ensure_ascii=False)
    assert "Acqui-hire" in glossary.load(app) and any("ГЛОССАРИЙ" in m.get("html", "") for m in app.telegram.sent)


# --- fact check --------------------------------------------------------------------------------
def test_school_fact_gate_sends_errors_back_to_deepseek_and_blocks_until_both_checks_pass(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}

    opened = {}

    def check(reg, p):  # approximate mode: only the important claims; a tenfold error in a metric
        assert p["mode"] == "approx" and p["max_claims"] == 20 and p["article"]["metrics"]
        src = reg.add("https://reuters.com/acme", "Acme", "Reuters")
        reg.mark_fetched(src.id, "Acme", "Acme revenue reached $100 million in 2025.")
        opened["id"] = src.id
        return {"claims": [{"id": "C1", "field": "metrics[0].value", "text": "выручка", "material": True,
                            "source": src.id, "source_value": "100", "source_unit": "million USD",
                            "article_value": "1", "article_unit": "млрд $", "status": "VERIFIED",
                            "source_quote": "Acme revenue reached $100 million in 2025."}]}

    def rework(reg, p):  # back to DeepSeek: it corrects the metric with the source
        assert p["recheck"][0]["id"] == "C1" and "программная проверка" in p["recheck"][0]["note"]
        value = p["article"]["metrics"][0]["value"]
        return {"claims": [{"id": "C1", "field": "metrics[0].value", "text": "выручка", "material": True, "source": opened["id"],
                            "source_value": "100", "source_unit": "million USD", "article_value": "100",
                            "article_unit": "млн $", "status": "ERROR", "fix": {"find": value, "replace": "$100 млн"},
                            "source_quote": "Acme revenue reached $100 million in 2025."}]}

    def confirm(reg, p):
        assert p["article"]["metrics"][0]["value"] == "$100 млн"
        return {"claims": [{"id": "C1", "field": "metrics[0].value", "text": "выручка", "material": True, "source": opened["id"],
                            "source_value": "100", "source_unit": "million USD", "article_value": "100",
                            "article_unit": "млн $", "status": "VERIFIED",
                            "source_quote": "Acme revenue reached $100 million in 2025."}]}

    http = FakeHttp()
    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg), check, rework, confirm]), http=http)
    assert unicorn.prepare(app, "2026-11-09") == "ready"
    report = app.store.draft_get("2026-11-09")["payload"]["report"]
    assert report["metrics"][0]["value"] == "$100 млн" and not report["fact_check"]["open"]
    assert report["fact_check"]["errors_found"] == 1 and http.jev_calls  # TypeSafe Jev confirmed the corrected claim


def test_school_fact_gate_failure_stops_the_issue(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}

    def boom(reg, p):
        raise RuntimeError("deepseek down")

    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg), boom]), http=FakeHttp())
    assert unicorn.prepare(app, "2026-11-09") == "failed"
    assert "фактчек не пройден" in app.telegram.sent[-1]["html"]


# --- pre-publication check ------------------------------------------------------------------------
def test_dead_media_is_dropped_before_publication():
    http = FakeHttp()
    http.dead = {"https://img/dead.jpg"}
    report = {"media_resolved": [{"kind": "photo", "src": "https://img/ok.jpg"}, {"kind": "photo", "src": "https://img/dead.jpg"}],
              "chart_images": ["https://img/dead.jpg", "https://img/ok.jpg"]}
    removed = precheck.prune_dead_media(report, http)
    assert [m["src"] for m in report["media_resolved"]] == ["https://img/ok.jpg"]
    assert report["chart_images"] == [None, "https://img/ok.jpg"] and removed["photos"] == 1 and removed["charts"] == 1


# --- Russia ---------------------------------------------------------------------------------
def test_russia_triggers_only_on_strong_moves():
    from investor_agent.radar import triggers

    def fake(url, **kw):
        if "cbr-xml" in url:
            return {"Date": "2026-11-07T11:30:00+03:00", "Valute": {"USD": {"Value": 95.0, "Previous": 92.0}}}
        if "IMOEX" in url:
            return {"marketdata": {"columns": ["CURRENTVALUE", "LASTCHANGEPRC", "SYSTIME"],
                                   "data": [[2800.0, -0.5, "2026-11-07 15:00:00"]]}}
        return {"marketdata": {"columns": ["SECID", "LAST", "LASTTOPREVPRICE", "SYSTIME"],
                               "data": [["SBER", 300.0, -3.4, "2026-11-07 15:00:00"],
                                        ["GAZP", 150.0, 2.9, "2026-11-07 15:00:00"]]}}

    class H:
        def request(self, method, url, **kw):
            return fake(url)

    errors: list[str] = []
    quotes = {**market.moex_quotes(H(), errors), **market.cbr_usdrub(H(), errors)}
    assert not errors and set(quotes) == {"IMOEX", "SBER", "GAZP", "USDRUB"}
    snap = market.Snapshot(taken_at="x", sessions={"weekend": False}, quotes=quotes)
    cfg = triggers.load_config(__import__("pathlib").Path("config/radar.yaml"))
    got = triggers.compute(snap, cfg, datetime(2026, 11, 7, 0, tzinfo=timezone.utc))
    assets = {t["asset"] for t in got if t["group"] == "russia"}
    assert assets == {"SBER", "USDRUB"}  # GAZP 2.9% and IMOEX -0.5% stay silent


def test_russia_source_failure_is_reported():
    class Down:
        def request(self, *a, **kw):
            raise HttpError(503, "x")

    errors: list[str] = []
    assert market.moex_quotes(Down(), errors) == {} and market.cbr_usdrub(Down(), errors) == {}
    assert len(errors) == 3
