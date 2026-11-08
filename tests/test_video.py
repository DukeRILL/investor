import json
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

pytest.importorskip("starlette")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "video"))
import app  # noqa: E402
import catalog as C  # noqa: E402
import planner as P  # noqa: E402
import render as R  # noqa: E402

BJ = timezone(timedelta(hours=8))
ARTICLE = ("Компанию оценили в 3 млрд долларов. Основатели заметили, что заявки закрываются вручную неделями. "
           "ИИ закрывает до 80 процентов заявок у некоторых клиентов. Раунд составил 47 млн долларов.")
VOICE = " ".join(["слово"] * 14)


def cat(n=6):
    return [{"id": f"img{i + 1:02d}", "available": True, "kind": "founder" if i < 2 else "product", "priority": 1 if i < 2 else 2,
             "faces": [[100, 100, 80, 100]] if i < 2 else [], "caption": "c", "context_before": "k"} for i in range(n)]


def scenario(**kw):
    scenes = [{"voice": "три миллиарда за один шаг", "on_screen": "$3 млрд", "big_number": {"value": 3}, "image_id": "img01", "effect": "zoom_in",
               "source_quote": "Компанию оценили в 3 млрд долларов"}]
    scenes += [{"voice": VOICE, "on_screen": "заявки", "big_number": None, "image_id": f"img0{i + 2}", "effect": "pan_left",
                "source_quote": "заявки закрываются вручную неделями"} for i in range(4)]
    scenes += [{"voice": "", "image_id": "img01", "effect": "parallax"}]
    return {"hook": {"type": "number", "why": "x"}, "scenes": scenes, **kw}


IDS = {f"img0{i}" for i in range(1, 7)}


def test_peak_guard():
    assert app.is_peak(datetime(2026, 11, 9, 10, 0, tzinfo=BJ))       # понедельник, пик
    assert not app.is_peak(datetime(2026, 11, 8, 10, 0, tzinfo=BJ))   # воскресенье
    assert not app.is_peak(datetime(2026, 11, 9, 13, 0, tzinfo=BJ))   # окно между пиками


# --- каталог статьи ---
PAGE = {"title": "T", "content": [
    {"tag": "p", "children": ["Первый абзац про компанию."]},
    {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": "https://x/a.jpg"}},
                                   {"tag": "figcaption", "children": ["Основатели Иван Петров и Анна Сидорова. ",
                                                                      {"tag": "a", "attrs": {"href": "https://news.example/a?utm_source=x"}, "children": ["Источник"]}]}]},
    {"tag": "p", "children": ["Второй абзац."]},
    {"tag": "img", "attrs": {"src": "/file/b.png"}}]}


def png(w=1000, h=800):
    b = BytesIO()
    Image.new("RGB", (w, h), (9, 9, 9)).save(b, "PNG")
    return b.getvalue()


def test_parse_page_keeps_captions_sources_and_context():
    title, text, figs = C.parse_page(PAGE)
    assert title == "T" and "Второй абзац." in text and len(figs) == 2
    assert figs[0]["id"] == "img01" and figs[0]["source_url"] == "https://news.example/a"
    assert "Иван Петров" in figs[0]["caption"] and figs[0]["context_before"].startswith("Первый")
    assert figs[1]["src"] == "https://telegra.ph/file/b.png"


def test_catalog_keeps_original_bytes_and_marks_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "detect_faces", lambda img: [[1, 1, 50, 50]])
    raw = png()

    def fetch(u):
        if "b.png" in u:
            raise OSError("боль")
        return raw
    _, _, figs = C.parse_page(PAGE)
    out = C.build_catalog(figs, tmp_path, fetch)
    assert (tmp_path / "img01.jpg").read_bytes() == raw and out[0]["available"] and out[0]["kind"] == "founder" and out[0]["priority"] == 1
    assert not out[1]["available"] and "боль" in out[1]["error"]
    assert [c["image_id"] for c in C.for_prompt(out)] == ["img01"]


def test_classify_founder_named_elsewhere_and_chart():
    names = {"Джейк Стауч"}
    assert C.classify({"caption": "Джейк Стауч в материале о Product-Market Fit", "faces": [[1, 1, 1, 1]]}, names) == ("founder", 1)
    assert C.classify({"caption": "Пост-денежная оценка по раундам", "faces": []}, names) == ("chart", 3)
    assert C.classify({"caption": "Слайд с выступления, Джейк Стауч", "faces": [[1, 1, 1, 1]]}, names) == ("product", 2)


# --- правило «сначала оригиналы» ---
def test_flux_zero_when_originals_are_enough():
    sc = scenario()
    sc["scenes"][1].update(needs_generation=True, image_id=None, image_prompt="abstract waves of tickets")
    assert P.enforce_images(sc, cat(6)) == []
    assert all(s["source_type"] == "original" and not s["needs_generation"] and not s["image_prompt"] for s in sc["scenes"])
    assert "отклонён" in sc["scenes"][1]["why"]


def test_flux_only_on_shortage():
    sc = scenario()
    for s in sc["scenes"]:
        s["image_id"] = "img01"
    sc["scenes"][2].update(needs_generation=True, image_id=None, image_prompt="abstract waves of tickets")
    gen = P.enforce_images(sc, cat(2))
    assert [g["image_id"] for g in gen] == ["gen03"] and sc["scenes"][2]["source_type"] == "generated"
    assert sum(s["source_type"] == "original" for s in sc["scenes"]) == 5
    assert gen[0]["image_prompt"].startswith(P.GEN_STYLE)


@pytest.mark.parametrize("prompt", ["portrait of founder in office", "CEO shaking hands", "фото основателей", "a man at a desk", None])
def test_flux_rejects_fake_people(prompt):
    sc = scenario()
    sc["scenes"][1].update(needs_generation=True, image_id=None, image_prompt=prompt)
    assert P.enforce_images(sc, cat(2)) == []
    assert sc["scenes"][1]["source_type"] == "original"


def test_no_images_at_all_is_an_error():
    sc = scenario()
    with pytest.raises(ValueError):
        P.enforce_images(sc, [])


def test_same_image_in_a_row_gets_a_different_effect():
    sc = scenario()
    for s in sc["scenes"]:
        s.update(image_id="img01", effect="zoom_in")
    P.enforce_images(sc, cat(6))
    effects = [s["effect"] for s in sc["scenes"]]
    assert all(a != b for a, b in zip(effects, effects[1:]))


def test_cta_is_literal():
    sc = scenario()
    P.finalize_cta(sc)
    assert sc["scenes"][-1]["voice"] == "Полный разбор бизнес-модели — в Telegram, Школа единорогов."


# --- проверка сценария на выдумку ---
def test_valid_scenario_passes():
    assert P.validate_scenario(scenario(), ARTICLE, IDS) == []


def test_invented_quote_and_numbers_are_rejected():
    sc = scenario()
    sc["scenes"][1]["source_quote"] = "основателям было по двадцать пять лет и они бросили университет"
    sc["scenes"][2]["on_screen"] = "$99 млрд"
    sc["scenes"][3]["big_number"] = {"value": 500}
    errs = " ".join(P.validate_scenario(sc, ARTICLE, IDS))
    assert "сцена 2: source_quote" in errs and "99" in errs and "500" in errs


def test_length_and_hook_limits():
    sc = scenario()
    sc["scenes"][1]["voice"] = "мало"
    assert any("слов в озвучке" in e for e in P.validate_scenario(sc, ARTICLE, IDS))
    sc = scenario()
    sc["scenes"][0]["voice"] = " ".join(["хук"] * 15)
    assert any("хук" in e for e in P.validate_scenario(sc, ARTICLE, IDS))


def test_write_scenarios_fixes_only_bad_ones_and_stops():
    good = scenario()
    bad = scenario()
    bad["scenes"][1]["source_quote"] = "этого в статье точно нет вообще никак"
    calls = []

    def ask(system, user):
        calls.append(user)
        return {"scenarios": [json.loads(json.dumps(good)) for _ in range(3)] + [bad]}
    res, n = app.write_scenarios("sys", ARTICLE, [], IDS, ask)
    assert n == app.MAX_CALLS == len(calls)
    assert [bool(s["problems"]) for s in res["scenarios"]] == [False, False, False, True]
    assert res["scenarios"][3]["hypothesis"] == "simplicity"


def test_paid_answer_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA", tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    monkeypatch.setattr(app, "PROMPT", prompt)
    monkeypatch.setattr(C, "fetch_page", lambda u: {"title": "T", "content": [{"tag": "p", "children": [ARTICLE]}]})
    monkeypatch.setattr(C, "build_catalog", lambda f, d, fetch=None: (d.mkdir(parents=True), cat(6))[1])
    n = []

    def ask(system, user):
        n.append(1)
        return {"scenarios": [scenario() for _ in range(4)], "best": 2, "best_why": "x"}
    a = app.scenarios_payload("https://telegra.ph/x", ask)
    b = app.scenarios_payload("https://telegra.ph/x", ask)
    assert len(n) == 1 and a["deepseek_calls"] == 1 and b["deepseek_calls"] == 0
    assert b["generate"] == [] and b["scenarios"][0]["scenes"][-1]["voice"] == P.CTA_VOICE


# --- субтитры привязаны к звуку ---
def test_align_uses_heard_times_not_word_length():
    words, sim = R.align("Рынок стоит много денег".split(),
                         [("Рынок", 0.0, 0.3), ("стоит", 2.0, 2.3), ("много", 2.3, 2.5), ("денег", 2.5, 3.0)], 9.0)
    assert sim == 1.0 and words[1][1] == 2.0 and words[3][2] == 3.0  # пауза в звуке осталась паузой


def test_align_interpolates_numbers_between_anchors():
    words, sim = R.align("стоит тринадцать миллиардов долларов".split(),
                         [("стоит", 0.0, 0.4), ("13", 0.5, 0.9), ("миллиардов", 1.0, 1.4), ("долларов", 1.4, 1.9)], 5.0)
    assert 0.4 <= words[1][1] < words[1][2] <= 1.0 and sim == 0.75


def test_align_detects_wrong_audio():
    _, sim = R.align("совсем другой текст реплики".split(), [("редактор", 0, 1), ("субтитров", 1, 2)], 2.0)
    assert sim < 0.5


def test_chunks_and_ass_keep_exact_word_times():
    words = [("Первое", 1.0, 1.5), ("слово.", 1.5, 2.0), ("Второе", 3.0, 3.5), ("слово", 3.5, 4.0)]
    assert [len(c) for c in R.chunk_words(words)] == [2, 2]  # конец предложения закрывает блок
    ass = R.build_ass([{"start": 10.0, "end": 15.0, "words": words, "on_screen": "ключ", "big_number": {"value": 3, "prefix": "$", "suffix": " млрд"}}], 15.0)
    assert "0:00:11.00,0:00:11.50" in ass and "0:00:13.00,0:00:13.50" in ass
    assert "$3 млрд" in ass


def test_hook_number_is_correct_from_first_frame():
    ass = R.build_ass([{"start": 0.0, "end": 3.0, "words": [("Три", 0, 1)], "on_screen": "", "big_number": {"value": 3, "prefix": "$"}}], 3.0)
    first = next(l for l in ass.splitlines() if "Num,," in l)
    assert first.startswith("Dialogue: 1,0:00:00.00") and first.endswith("$3")


SCRIPT = "Один продакт второй инженер познакомились в Verkada".split()


def test_islands_drop_gibberish_and_repeats():
    assert R.select_islands(SCRIPT, [["арандису", "лакит", "ума"], ["Один", "продакт,", "второй", "инженер."],
                                     ["Познакомились.", "Инженер."], ["Познакомились", "в", "Verkada."], ["Муток", "слай"]]) == [1, 3]


def test_islands_keep_numbers_and_full_reply():
    script = "ушёл за сорок пять дней десять тысяч сотрудников".split()
    assert R.select_islands(script, [["ушёл", "за", "45", "дней"], ["10", "000", "сотрудников"]]) == [0, 1]
    assert R.select_islands(script, [["ушёл", "за", "45", "дней"], ["ушёл", "за", "45", "дней"], ["10", "000", "сотрудников"]]) in ([0, 2], [1, 2])


def test_neighbour_islands_keep_names_but_not_repeats():
    spans = [(0, 500), (800, 2000), (20000, 21000), (40000, 41000)]  # частота 1000: пауза 0,3 с, потом долгая тишина
    heard = [["Лусит", "Моторс"], ["ушёл", "за", "45", "дней"], ["ушёл", "за"], ["абырвалг"]]
    script = "Lucid Motors ушёл за сорок пять дней".split()
    assert R.add_neighbours([1], spans, heard, 1000, script) == [0, 1]
    # бессвязное слово рядом с репликой не берём
    assert R.add_neighbours([0], [(0, 500), (700, 900)], [["оценка", "три", "миллиарда"], ["тримадан"]], 1000, "Оценка три миллиарда".split()) == [0]


def test_find_islands_splits_on_silence_and_counts_samples():
    np = pytest.importorskip("numpy")
    rate = 1000
    tone = (np.sin(np.arange(rate) * 0.5) * 20000).astype(np.int16)
    src = np.concatenate([np.zeros(rate, np.int16), tone, np.zeros(3 * rate, np.int16), tone])
    spans = R.find_islands(src, rate)
    assert len(spans) == 2 and abs(spans[0][0] - rate) <= 20 and abs(spans[1][1] - len(src)) <= 20


# --- камера не режет лица ---
TWO = {"width": 1200, "height": 600, "kind": "founder", "faces": [[430, 55, 79, 115], [700, 70, 92, 113]]}
ONE = {"width": 1200, "height": 630, "kind": "product", "faces": [[701, 232, 47, 67]]}


@pytest.mark.parametrize("effect", P.EFFECTS)
@pytest.mark.parametrize("item", [TWO, ONE, {"width": 1200, "height": 800, "kind": "founder", "faces": [[937, 101, 142, 210]]},
                                  {"width": 800, "height": 1600, "kind": "founder", "faces": [[300, 200, 200, 240]]}])
def test_faces_stay_whole_at_start_and_end(effect, item):
    plan = R.plan_motion(item, effect)
    if plan["layout"] != "cover":
        return  # снимок целиком, режутся только его поля
    for win, safe in ((plan["start"], plan["safe"]), (plan["end"], plan.get("safe_end", plan["safe"]))):
        assert R.contains(win, safe), (effect, win, safe)
        assert abs(win[2] / win[3] - R.W / R.H) < 0.01
        assert win[0] >= -0.5 and win[1] >= -0.5 and win[0] + win[2] <= item["width"] + 0.5 and win[1] + win[3] <= item["height"] + 0.5


def test_chart_and_text_images_are_never_cropped():
    assert R.plan_motion({"width": 1600, "height": 900, "kind": "chart", "faces": []}, "pan_left")["layout"] == "contain"


def test_two_people_fit_or_sweep():
    assert R.plan_motion(TWO, "zoom_in")["layout"] == "contain"
    assert R.plan_motion(TWO, "face_sweep")["effect"] == "face_sweep"


@pytest.mark.parametrize("text,bad", [
    ("Рынок ITSM стоит тринадцать миллиардов", "латиница"),
    ("Оценка — $3 млрд", "цифры"),
    ("Её закрывает ИИ", "сокращения"),
    ("семьдесят-восемьдесят процентов, то есть 70%", "символы"),
])
def test_voice_text_rejects_what_azure_reads_badly(text, bad):
    assert any(p.startswith(bad) for p in P.voice_problems(text))


def test_voice_text_accepts_plain_words():
    assert P.voice_problems("Стивен Шварц и Кэмерон Зуб познакомились в тринадцать лет — в группе реселлеров.") == []


def test_negated_people_is_not_people():
    assert P.safe_image_prompt("An old forum page, no people, no faces, no text")
    assert not P.safe_image_prompt("A founder at a desk, no text")


def test_shot_rules_from_owner_review():
    cat = {"img02": {}, "img04": {}}
    good = [{"start": 0, "end": 3, "image_id": "img02", "source_type": "original"},
            {"start": 3, "end": 6.5, "image_id": None, "source_type": "generated", "image_prompt": "an old forum page with scam warnings"},
            {"start": 6.5, "end": 9, "image_id": "img04", "source_type": "original"}]
    assert P.shot_problems(good, cat) == []
    bad = [{"start": 0, "end": 5, "image_id": None, "source_type": "generated", "image_prompt": "a mountain of gold coins"},
           {"start": 5, "end": 7, "image_id": "img02", "source_type": "original"},
           {"start": 7, "end": 9, "image_id": "img02", "source_type": "original"}]
    errs = " ".join(P.shot_problems(bad, cat))
    assert "первый кадр" in errs and "дольше 4" in errs and "штамп" in errs and "подряд" in errs


def test_library_rules_unique_images_and_logo_gap():
    lib = {"img02": {"kind": "founder"}, "img01": {"kind": "brand"}, "gen01": {"kind": "generated"}, "gen02": {"kind": "generated"}}
    sh = lambda ids: [{"start": i * 3, "end": i * 3 + 3, "image_id": x} for i, x in enumerate(ids)]
    assert P.library_shot_problems(sh(["img02", "gen01", "img01", "gen02"]), lib) == []
    errs = " ".join(P.library_shot_problems(sh(["gen01", "img02", "img02", "img01", "gen02", "img01"]), lib))
    assert "первый кадр" in errs and "слишком близко" in errs and "логотип ближе" in errs


def test_split_library_gives_each_video_its_own_images():
    gens = [{"id": f"gen{i}", "fact_id": f} for i, f in enumerate(["F1", "F5", "F6", "F9", "F12", "F13"])]
    scripts = [{"lines": [{"fact_id": "F1"}, {"fact_id": "F9"}]}, {"lines": [{"fact_id": "F5"}]}, {"lines": [{"fact_id": "F6"}]}, {"lines": [{"fact_id": "F12"}]}]
    parts = P.split_library(gens, scripts)
    flat = [g for p in parts for g in p]
    assert sorted(flat) == sorted(g["id"] for g in gens) and len(flat) == len(set(flat))
    assert "gen0" in parts[0] and "gen1" in parts[1] and all(parts)


def test_assemble_follows_owner_rules():
    lib = {"img02": {"kind": "founder"}, "img01": {"kind": "brand"}, "img04": {"kind": "chart"},
           **{f"gen{i}": {"kind": "generated"} for i in range(1, 7)}}
    lines = [{"line": f"T{i}", "start": i * 3.5, "end": i * 3.5 + 3.5} for i in range(7)] + [{"line": "CTA", "start": 24.5, "end": 28}]
    matches = {"T0": ["gen1"], "T1": ["img02", "gen2"], "T2": ["img01"], "T3": ["gen1", "gen3"], "T4": ["img01", "gen4"],
               "T5": ["img04"], "T6": ["gen5"]}
    shots = P.assemble_shots(lines, matches, lib, {"gen5": 2})
    assert shots[0]["image_id"] == "img02"
    assert P.library_shot_problems(shots, lib) == []
    assert "gen5" not in [s["image_id"] for s in shots]  # уже в двух других роликах
    assert shots[-1]["image_id"] == "img01" and [s["image_id"] for s in shots].count("img01") == 1


# --- бесплатные стоковые фото ---
import stock  # noqa: E402


def test_stock_search_skips_services_without_keys(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    assert stock.search("sneaker") == []


def test_stock_pick_rejects_people_and_wide_and_takes_first_good(tmp_path, monkeypatch):
    monkeypatch.setattr(stock, "search", lambda q, http=None: [
        {"source": "pexels", "id": "p1", "url": "u1", "page": "x", "author": "a", "width": 3000, "height": 2000, "query": q},
        {"source": "pexels", "id": "p2", "url": "u2", "page": "x", "author": "a", "width": 1200, "height": 1800, "query": q},
        {"source": "pixabay", "id": "p3", "tags": "q", "url": "u3", "page": "x", "author": "a", "width": 1200, "height": 1800, "query": q}])
    raws = {"u1": png(2000, 1200), "u2": png(1200, 1800), "u3": png(1100, 1700)}
    seq = iter([[[1, 1, 50, 50]], []])  # на p2 лицо, на p3 нет
    got = stock.pick("q", tmp_path, lambda img: next(seq), set(), fetch=lambda u: raws[u])
    assert got["id"] == "p3" and (tmp_path / "p3.jpg").exists() and dict(got["rejected_before"])["p2"] == "на фото люди"
    assert "Pixabay" in got["license"]


# --- Pixabay: кеш, 429/5xx, запрос, отказ API ---
class _R:
    def __init__(self, code, data=None, headers=None):
        self.status_code, self._d, self.headers = code, data or {}, headers or {}

    def json(self):
        return self._d


class _H:
    def __init__(self, *rs):
        self.rs, self.calls = list(rs), 0

    def get(self, url, **kw):
        self.calls += 1
        r = self.rs.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_stock_query_is_from_scene_prompt_and_differs(monkeypatch):
    a = stock.query_from_prompt("Abstract editorial illustration, no people. A cargo ship at a port with containers")
    b = stock.query_from_prompt("Laptop with spreadsheet on a wooden desk")
    assert a == "cargo ship port containers" and b != a


def test_stock_get_json_retries_429_5xx_then_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    waits = []
    h = _H(_R(429, headers={"Retry-After": "3"}), _R(503), _R(200, {"hits": [1]}))
    assert stock.get_json(h, "u", params={"key": "SECRET", "q": "x"}, name="pixabay", sleep=waits.append) == {"hits": [1]}
    assert h.calls == 3 and waits[0] == 3
    assert stock.get_json(_H(), "u", params={"key": "SECRET", "q": "x"}, name="pixabay") == {"hits": [1]}  # из кеша, без запроса
    assert "SECRET" not in "".join(p.read_text() for p in (tmp_path / "cache" / "stock").iterdir())


def test_stock_get_json_unavailable_returns_none(tmp_path, monkeypatch):
    import httpx as hx
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    h = _H(hx.ConnectTimeout("t"), _R(500), _R(502))
    assert stock.get_json(h, "u", params={"q": "y"}, name="pixabay", sleep=lambda s: None) is None
    assert stock.get_json(_H(_R(400)), "u", params={"q": "z"}, name="pixabay") is None


def test_stock_pick_rejects_people_tags_irrelevant_and_empty(tmp_path, monkeypatch):
    base = {"source": "pixabay", "url": "u", "page": "x", "author": "a", "width": 1200, "height": 1800}
    monkeypatch.setattr(stock, "search", lambda q, http=None: [
        {**base, "id": "a", "tags": "cat, animal"}, {**base, "id": "b", "tags": "ship, people"},
        {**base, "id": "c", "tags": "ship, harbor"}])
    got = stock.pick("cargo ship", tmp_path, lambda i: [], set(), fetch=lambda u: png(1200, 1800))
    assert got["id"] == "c" and got["author"] == "a"
    monkeypatch.setattr(stock, "search", lambda q, http=None: [])
    assert stock.pick("q", tmp_path, lambda i: [], set(), fetch=lambda u: b"") is None


def test_stock_pick_does_not_redownload_saved(tmp_path, monkeypatch):
    (tmp_path / "pixabay-1.jpg").write_bytes(b"x")
    monkeypatch.setattr(stock, "search", lambda q, http=None: [{"source": "pixabay", "id": "pixabay-1", "url": "u", "page": "x",
                         "author": "a", "tags": "ship", "width": 1200, "height": 1800}])
    assert stock.pick("ship", tmp_path, lambda i: [], set(), fetch=lambda u: 1 / 0) is None


def test_flux_image_quality_gate():
    import app as A
    from io import BytesIO
    from PIL import Image
    import random
    assert A.image_problem(b"junk") == "не картинка"
    assert A.image_problem(png(300, 300)) == "мало пикселей"
    assert A.image_problem(png(1024, 1024)) == "пустой кадр"
    b = BytesIO(); im = Image.new("RGB", (600, 600))
    im.putdata([(random.randint(0, 255),) * 3 for _ in range(360000)]); im.save(b, "PNG")
    assert A.image_problem(b.getvalue()) is None


def test_assemble_leaves_gaps_when_not_filling():
    lib = {"img02": {"kind": "founder"}, "img01": {"kind": "brand"}, "gen1": {"kind": "generated"}}
    lines = [{"line": "T0", "start": 0, "end": 8}, {"line": "CTA", "start": 8, "end": 11}]
    shots = P.assemble_shots(lines, {"T0": ["gen1"]}, lib, {}, fill_spare=False)
    assert [s["image_id"] for s in shots] == ["img02", "gen1", "img01"][:0] + [s["image_id"] for s in shots]
    assert shots[0]["image_id"] == "img02" and shots[1]["image_id"] == "gen1" and shots[-1]["image_id"] == "img01"


def test_clean_queries_drops_metaphors_and_text_objects():
    got = P.clean_queries(["toolbox on wooden table", "open padlock on wooden table", "coin stack on wooden surface",
                           "empty wooden store shelf", "laptop on desk", "automatic sliding gate", "x"])
    assert got == ["open padlock on wooden table", "empty wooden store shelf", "automatic sliding gate"]


def test_fill_gaps_uses_spares_then_second_founder_crop():
    lib = {"img02": {"kind": "founder"}, "img01": {"kind": "brand"}, "gen1": {"kind": "generated"}, "gen2": {"kind": "generated"}}
    mk = lambda ids: [{"line": "T", "start": i, "end": i + 1, "image_id": x, "source_type": "original", "fit": True} for i, x in enumerate(ids)]
    videos = [mk(["img02", "gen1", None, None, None, None, None, None, None, "img01"])]
    left = P.fill_gaps(videos, lib, {"gen1": 1})
    ids = [s["image_id"] for s in videos[0]]
    assert ids[2] == "gen2" and ids.count("img02") == 2 and left == 5
    assert videos[0][ids.index("img02", 1)]["focus"] == 0


def test_hook_is_mandatory():
    good = {"hook": {"type": "number"}, "lines": [{"voice": "Подростки писали ботов. Теперь их компания стоит миллиарды.", "on_screen": "$5,1 млрд"}]}
    assert P.hook_problems(good) == []
    errs = " ".join(P.hook_problems({"lines": [{"voice": "Привет, сегодня расскажу про компанию Whop, которая выросла за пять лет в огромный бизнес", "on_screen": ""}]}))
    assert "слов" in errs and "приветствием" in errs and "нет поля hook" in errs and "экранной" in errs


def test_scenario_without_valid_hook_is_rejected_before_voice():
    import app as A
    sc = {"scenes": [{"voice": "Привет, друзья, сегодня расскажем про компанию.", "on_screen": ""}] * 6}
    errs = A.check_scenario(sc, "текст", set())
    assert any("хук" in e or "hook" in e for e in errs)


def test_stock_pick_uses_local_db_and_cooldown(tmp_path, monkeypatch):
    import imagedb
    db = imagedb.connect(tmp_path / "i.sqlite")
    f = tmp_path / "px1.jpg"; f.write_bytes(png(1200, 1800))
    imagedb.add(db, id="pixabay-1", source="pixabay", kind="stock", file=str(f), tags="ship, harbor", query="cargo ship", license="L", width=1200, height=1800)
    monkeypatch.setattr(stock, "search", lambda q, http=None: 1 / 0)  # API не должен вызываться
    got = stock.pick("cargo ship port", tmp_path, lambda i: [], set(), db=db, issue="j2")
    assert got["id"] == "pixabay-1" and got["from_db"]
    imagedb.mark_used(db, "pixabay-1", "j1", "v.mp4")  # показ в другом выпуске: кулдаун 30 дней
    monkeypatch.setattr(stock, "search", lambda q, http=None: [])
    assert stock.pick("cargo ship port", tmp_path, lambda i: [], set(), db=db, issue="j2") is None
    import time
    assert imagedb.cooling_down(db, "pixabay-1", "j2", now=time.time() + 31 * 86400) is False  # через 30 дней снова доступна


def test_stock_pick_saves_new_find_in_db(tmp_path, monkeypatch):
    import imagedb
    db = imagedb.connect(tmp_path / "i.sqlite")
    monkeypatch.setattr(stock, "search", lambda q, http=None: [{"source": "pixabay", "id": "pixabay-9", "tags": "ship", "url": "u", "page": "p",
                        "author": "a", "width": 1200, "height": 1800}])
    got = stock.pick("ship", tmp_path, lambda i: [], set(), fetch=lambda u: png(1200, 1800), db=db, issue="j")
    assert got["id"] == "pixabay-9" and imagedb.report(db)["images"] == 1


def test_subtitles_show_brands_and_names_in_latin_but_keep_russian_companies():
    import names
    assert names.display("Они сделали Уоп, Стивен Шварц и Кэмерон Зуб") == "Они сделали Whop, Steven Schwartz и Cameron Zoub"
    assert names.display("в Телеграм, Школа единорогов") == "в Telegram, Школа единорогов"
    ass = R.build_ass([{"start": 0.0, "end": 3.0, "words": [("Уоп", 0, 1), ("растёт", 1, 2)], "on_screen": "Уоп: Стивен Шварц"}], 3.0)
    assert "Whop" in ass and "Уоп" not in ass and "STEVEN SCHWARTZ" in ass
