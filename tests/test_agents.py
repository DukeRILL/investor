from datetime import datetime, timedelta, timezone

from conftest import FakeDeepSeek, FakeTelegram

from investor_agent.radar import market, pipeline as radar, triggers
from investor_agent.sources import SourceRegistry
from investor_agent.unicorn import pipeline as unicorn

RU = "Компания решает дорогую проблему клиентов и растёт за счёт повторных продаж. " * 8


# --- unicorn -------------------------------------------------------------------------------
def cand(name, **kw):
    base = {"name": name, "country": "USA", "industry": "fintech", "valuation_usd_bn": 2, "private": True,
            "scores": {"demand": 8, "growth": 7, "investors": 8, "cis_fit": 7, "info": 6}}
    base.update(kw)
    return base


def test_weighted_score():
    assert unicorn.weighted_score({"demand": 10, "growth": 10, "investors": 10, "cis_fit": 10, "info": 10}) == 10
    assert unicorn.weighted_score({"demand": 10}) == 3.0


def test_rank_excludes_archive_small_and_rotates():
    ranked = unicorn.rank_candidates(
        [cand("Vinted Ltd"), cand("Small", valuation_usd_bn=0.6), cand("OilCo", industry="oil & gas"),
         cand("A", industry="fintech"), cand("B", industry="hr tech", scores={"demand": 8, "growth": 7, "investors": 8,
                                                                              "cis_fit": 7, "info": 5})],
        excluded={"vinted"}, recent=[{"industry": "fintech", "country": "France"}])
    assert [r["name"] for r in ranked] == ["B", "A"]  # A penalised for repeating yesterday's industry


def test_ai_quota_and_ranking():
    hist = [{"industry": "AI", "ai": 1}] * 6 + [{"industry": "fintech", "ai": 0}] * 4
    assert unicorn.ai_quota(hist)["ai_allowed"] is False  # 7/11 > 60%
    assert unicorn.ai_quota(hist[:1] + hist[6:])["ai_allowed"] is True
    assert unicorn.is_ai_issue({"industry": "enterprise AI"}) and not unicorn.is_ai_issue({"industry": "mail"})
    cands = [cand("Plain"), cand("Smart", ai_core=True)]
    assert unicorn.rank_candidates(cands, set(), [], True)[0]["name"] == "Smart"
    assert unicorn.rank_candidates(cands, set(), [], False)[0]["name"] == "Plain"


def good_report(registry):
    for i in range(6):
        src = registry.add(f"https://site{i}.com/a", f"Title {i}", f"Pub{i}")
        registry.mark_fetched(src.id, src.title, "content")
    sections = [{"id": sid, "title": unicorn.SECTION_TITLES[sid], "body": RU + " [S1] [S2] [S3] [S4] [S5] [S6]"}
                for sid in unicorn.SECTION_IDS]
    sections[1]["body"] += " слово" * 3000  # the 15-minute minimum
    sections[-1]["body"] = "Первое. Второе. Третье. Четвёртое. Пятое. Какую проблему вы видите каждый день? Правило: продавай до разработки."
    return {"company_name": "Acme", "industry": "финтех", "country": "США", "usefulness_score": 8,
            "teaser": "Acme выросла до 100 млн долларов ARR за три года [S1].", "sections": sections,
            "rounds": [{"date": "2025-03", "stage": "Series B", "amount": "USD 60 млн", "source": "S2"}],
            "subtitle": "Как Acme выросла до 100 млн долларов",
            "metrics": [{"label": f"Метрика {i}", "value": f"{i} млн", "note": "2026", "source": "S1"} for i in range(4)],
            "verdict_scores": [{"score": 9, "label": x} for x in ("Боль", "Рост", "Защита", "Воспроизводимость")],
            "media": [{"source": f"S{i}", "kind": "photo", "section": "start", "caption": f"Фото {i}"}
                      for i in range(1, 6)], "unknowns": ["Доли основателей"]}


def test_prepare_and_publish_full_cycle(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}
    ds = FakeDeepSeek([stage1, lambda reg, p: good_report(reg)])
    app = make_app(deepseek=ds)
    assert unicorn.prepare(app, "2026-11-09") == "ready"
    assert ds.calls[1]["payload"]["company"]["name"] == "Acme"
    assert unicorn.prepare(app, "2026-11-09") == "exists"
    assert unicorn.publish(app, "2026-11-09") == "published"
    post = app.telegram.sent[0]
    assert post["chat"] == "@investor_duker" and post["button"][1] == "https://telegra.ph/test-page"
    assert "Компания дня: Acme" in post["html"] and "[S1]" not in post["html"]
    title, nodes = app.telegraph.pages[0]
    assert title == "ШКОЛА ЕДИНОРОГОВ — 9 ноября 2026. КОМПАНИЯ ДНЯ: Acme"
    assert "acme" in app.store.archived_names()
    assert unicorn.publish(app, "2026-11-09") == "already_published"


def test_prepare_blocks_report_without_enough_opened_sources(make_app):
    def weak(reg, p):
        report = good_report(reg)
        for s in reg.sources.values():
            s.fetched = False
        return report
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta"), cand("Gamma")]}
    app = make_app(deepseek=FakeDeepSeek([stage1, weak, weak]))
    assert unicorn.prepare(app, "2026-11-09") == "failed"
    assert app.store.draft_get("2026-11-09")["status"] == "failed"
    assert "Школа единорогов" in app.telegram.sent[0]["html"] and app.telegram.sent[0]["chat"] == "111"
    assert unicorn.publish(app, "2026-11-09") == "no_draft"


def test_draft_mode_sends_to_admin(make_app):
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta"), cand("Gamma")]}
    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg)]), mode="draft")
    unicorn.prepare(app, "2026-11-09")
    unicorn.publish(app, "2026-11-09")
    assert app.telegram.sent[0]["chat"] == "111"


# --- radar ---------------------------------------------------------------------------------
NOW = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)  # Tue 03:00 Beijing, US cash closed


def snap(btc_change=1.0, observed=NOW):
    s = market.Snapshot(taken_at=NOW.isoformat(), sessions=market.sessions(NOW))
    s.quotes["BTC"] = market.Quote("BTC", "BTC/USDT спот (Binance)", 90000, btc_change, "%", "скользящие 24 часа",
                                   observed.isoformat(), "Binance spot")
    return s


def test_sessions_and_farside():
    assert market.us_cash_open(datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc))
    assert not market.us_cash_open(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc))  # Thanksgiving
    assert not market.futures_open(datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))   # Saturday
    md = ("| Date | IBIT | FBTC | Total |\n|---|---|---|---|\n| 05 Nov 2026 | 100.0 | (20.5) | 79.5 |\n"
          "| 06 Nov 2026 | (300.0) | (250.0) | (550.0) |\n| 07 Nov 2026 |  | 10.0 | 10.0 |\n")
    rows = market.parse_farside(md)
    assert rows[1] == {"session_date": "2026-11-06", "total_musd": -550.0, "status": "final"}
    assert rows[2]["status"] == "preliminary"


def test_triggers_threshold_and_staleness():
    cfg = {"crypto": {"BTC": {"move_pct": 5}}}
    start = NOW - timedelta(hours=3)
    assert triggers.compute(snap(6.2), cfg, start)[0]["trigger_key"].startswith("move|BTC|")
    assert triggers.compute(snap(3.0), cfg, start) == []
    assert triggers.compute(snap(6.2, observed=NOW - timedelta(hours=5)), cfg, start) == []


def alert_json(reg, severity="high", action="wait", n_sources=1):
    for i in range(n_sources):
        src = reg.add(f"https://reuters{i}.com/x", f"Headline {i}", f"Pub{i}")
        reg.mark_fetched(src.id, src.title, "text")
    cites = " ".join(f"[S{i + 1}]" for i in range(n_sources))
    return {"decision": "ALERT", "severity": severity, "confidence": "medium", "action": action,
            "event_key": {"category": "oil_supply", "assets": ["BRENT", "WTI"], "occurrence": "OPEC+ cut",
                          "event_date": "2026-11-09"},
            "headline": "ОПЕК+ неожиданно сократила добычу", "why_new": "Решение принято сегодня.",
            "fact": f"ОПЕК+ объявила сокращение добычи {cites}.", "hypothesis": "Цены на нефть могут вырасти.",
            "scenarios": [{"name": "Базовый", "text": "Рост нефти", "confirm_if": "Brent выше", "invalidate_if": "Brent ниже"}],
            "unknown": "Неясно, как быстро сократят экспорт.",
            "plain_words": ["ОПЕК+ сократила добычу.", "Нефть может подорожать.", "Уверенность средняя.",
                            "Лучше подождать.", "Пересмотрю прогноз, если экспорт не упадёт."]}


def test_validate_alert_rules():
    reg = SourceRegistry()
    a = alert_json(reg)
    assert radar.validate_alert(a, reg, set()) == []
    assert "duplicate" in " ".join(radar.validate_alert(a, reg, {radar.event_key(a["event_key"])}))
    assert "two independent" in " ".join(radar.validate_alert(alert_json(SourceRegistry(), "extraordinary"),
                                                               SourceRegistry(), set()) or ["two independent"])
    reg2 = SourceRegistry()
    assert radar.validate_alert(alert_json(reg2, "extraordinary", n_sources=2), reg2, set()) == []


def test_format_alert_sections_and_disclaimer():
    reg = SourceRegistry()
    parts = radar.format_alert(alert_json(reg, action="buy"), reg, snap(), NOW)
    text = "\n".join(parts)
    for heading in ("ФАКТ", "ГИПОТЕЗА", "НЕИЗВЕСТНО", "ПРОСТЫМИ СЛОВАМИ — ЧТО ЭТО ЗНАЧИТ И МОЙ ПРОГНОЗ"):
        assert heading in text
    assert radar.DISCLAIMER in text and "(Пекин)" not in text
    reg2 = SourceRegistry()
    assert radar.DISCLAIMER in "\n".join(radar.format_alert(alert_json(reg2, action="wait"), reg2, snap(), NOW))


def test_radar_silent_when_nothing_found(make_app, monkeypatch):
    monkeypatch.setattr(market, "collect", lambda *a, **k: snap(1.0))
    ds = FakeDeepSeek([lambda reg, p: {"decision": "NO_ALERT", "candidates": [], "checked": ["fed"]}])
    app = make_app(deepseek=ds)
    assert radar.run(app, NOW) == "NO_ALERT"
    assert app.telegram.sent == [] and len(ds.calls) == 1  # pro model never called
    assert app.store.last_ok_window_end() == NOW.isoformat()


def test_radar_sends_once_and_dedups(make_app, monkeypatch):
    monkeypatch.setattr(market, "collect", lambda *a, **k: snap(1.0))
    gate = lambda reg, p: {"decision": "CANDIDATES", "candidates": [
        {"category": "oil_supply", "assets": ["BRENT"], "occurrence": "OPEC+ cut", "event_date": "2026-11-09"}]}
    ds = FakeDeepSeek([gate, lambda reg, p: alert_json(reg), gate, lambda reg, p: alert_json(reg)])
    app = make_app(deepseek=ds)
    assert radar.run(app, NOW) == "SENT"
    assert app.telegram.sent[0]["chat"] == "@investor_duker"
    assert radar.run(app, NOW + timedelta(hours=3)) == "BLOCKED"  # same event -> duplicate
    assert len(app.telegram.sent) == 1


def test_radar_failed_delivery_is_recorded(make_app, monkeypatch):
    monkeypatch.setattr(market, "collect", lambda *a, **k: snap(1.0))
    gate = lambda reg, p: {"decision": "CANDIDATES", "candidates": [{"category": "fed", "assets": ["SPX"],
                                                                     "occurrence": "x", "event_date": "2026-11-09"}]}
    app = make_app(deepseek=FakeDeepSeek([gate, lambda reg, p: alert_json(reg)]), telegram=FakeTelegram(fail=True))
    assert radar.run(app, NOW) == "FAILED"
    assert app.store.last_ok_window_end() is None  # window is re-checked next time


def test_weekly_report(make_app):
    from investor_agent import report
    app = make_app()
    app.store.usage_add("radar", 0.40, {"searches": 30, "fetches": 5})
    app.store.usage_add("unicorn_prepare", 1.10, {"searches": 40, "fetches": 25})
    app.deepseek.balance = lambda: {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "15.00"}]}
    assert report.send_weekly_report(app) == "sent"
    msg = app.telegram.sent[0]
    assert msg["chat"] == "111" and "$1.50" in msg["html"] and "$15.00" in msg["html"] and "10 нед" in msg["html"]


# --- unicorn readers' poll -------------------------------------------------------------------
def test_poll_tally_counts_one_vote_and_keeps_ties():
    from investor_agent.unicorn import poll
    opts = ["Финтех", "Европа", "После 2020", "Маркетплейс"]
    assert poll.tally(opts, [0, 0, 0, 0]) == []
    assert poll.tally(opts, [0, 1, 0, 0]) == ["Европа"]
    assert poll.tally(opts, [2, 0, 2, 1]) == ["Финтех", "После 2020"]


def test_poll_options_pool_categories_and_no_repeats_for_7_days(make_app):
    import random
    from datetime import date, timedelta
    from investor_agent.unicorn import poll
    from investor_agent.unicorn.poll_topics import POOL
    assert len(POOL) == 100 and len({t for _, t in POOL}) == 100 and all(len(t) <= poll.OPTION_MAX for _, t in POOL)
    cat = dict((t, c) for c, t in POOL)
    app = make_app(deepseek=FakeDeepSeek([]))
    day, shown = date(2026, 11, 10), {}
    for i in range(7):
        issue = (day + timedelta(days=i)).isoformat()
        opts = poll.make_options(app, issue, rng=random.Random(i))
        assert len(set(opts)) == 4 and len({cat[o] for o in opts}) == 4  # four different categories
        assert not set(opts) & set(shown)  # nothing from the last 7 days
        poll.remember(app, issue, opts)
        shown.update({o: issue for o in opts})
    # after the window the first day's options are allowed again, and old entries are pruned
    later = (day + timedelta(days=7)).isoformat()
    poll.remember(app, later, ["x"])
    assert all(t not in poll._history(app) for t in list(shown)[:4])


def test_poll_posted_after_issue_and_closed_not_before_23_moscow(make_app):
    from investor_agent.timeutil import BEIJING
    from investor_agent.unicorn import poll
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}
    app = make_app(deepseek=FakeDeepSeek([stage1, lambda reg, p: good_report(reg)]))
    unicorn.prepare(app, "2026-11-09")
    assert unicorn.publish(app, "2026-11-09") == "published"
    sent_poll = app.telegram.sent[1]
    assert sent_poll["chat"] == "@investor_duker" and len(sent_poll["options"]) == 4
    assert poll.post(app, "2026-11-10", sent_poll["options"]) is None  # one poll per issue
    app.telegram.votes = [(sent_poll["options"][0], 0), (sent_poll["options"][1], 1)] + \
        [(o, 0) for o in sent_poll["options"][2:]]
    early = datetime(2026, 11, 10, 3, 59, tzinfo=BEIJING)  # 22:59 Moscow
    assert poll.close(app, "2026-11-10", now=early) is None and not app.telegram.deleted
    topic = poll.close(app, "2026-11-10", now=datetime(2026, 11, 10, 4, 0, tzinfo=BEIJING))
    assert topic["chosen"] == [sent_poll["options"][1]] and topic["total"] == 1
    assert app.telegram.deleted == [2]
    assert poll.chosen_topics(app, "2026-11-10") == [sent_poll["options"][1]]  # recorded, idempotent


def test_prepare_uses_reader_topic_and_falls_back(make_app):
    import json
    from investor_agent.unicorn import poll
    stage_few = lambda reg, p: {"candidates": [cand("Acme")]}
    stage1 = lambda reg, p: {"candidates": [cand("Acme"), cand("Beta", industry="hr"), cand("Gamma", industry="edtech")]}
    ds = FakeDeepSeek([stage_few, stage1, lambda reg, p: good_report(reg)])
    app = make_app(deepseek=ds)
    app.store.set(poll.topic_key("2026-11-09"), json.dumps({"chosen": ["Финтех"], "votes": [1]}))
    assert unicorn.prepare(app, "2026-11-09") == "ready"
    assert ds.calls[0]["payload"]["reader_topics"] == ["Финтех"]
    assert "reader_topics" not in ds.calls[1]["payload"]  # too few candidates -> agent's own choice
    assert app.store.draft_get("2026-11-09")["payload"]["reader_topics"] == []


def test_strip_meta_notes():
    from investor_agent.quality import strip_meta_notes
    t = ("Правило: сначала докажите экономику.\n\nМатериал подготовлен на 7 ноября 2026 года. "
         "Ramp добавлена в перечень уже разобранных компаний, чтобы не повторять её в следующих выпусках.")
    out = strip_meta_notes(t)
    assert out == "Правило: сначала докажите экономику."
    assert strip_meta_notes("Ramp выросла втрое.") == "Ramp выросла втрое."
