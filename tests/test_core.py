import json
from datetime import datetime

import pytest

from investor_agent.config import Settings, load_settings
from investor_agent.deepseek import DeepSeekClient, parse_json
from investor_agent.quality import count_sentences, is_russian
from investor_agent.scheduler import next_daily, next_radar
from investor_agent.sources import SourceRegistry
from investor_agent.textfmt import split_html, text_to_nodes, to_tg_html
from investor_agent.timeutil import BEIJING, PeakHoursError, is_peak, minutes_until_peak, next_offpeak


def bj(*args):
    return datetime(*args, tzinfo=BEIJING)


# --- off-peak rule -------------------------------------------------------------------------
@pytest.mark.parametrize("dt,peak", [
    (bj(2026, 11, 9, 9, 0), True),     # Monday 09:00
    (bj(2026, 11, 9, 8, 50), True),    # 15-min safety margin
    (bj(2026, 11, 9, 12, 20), False),
    (bj(2026, 11, 9, 15, 0), True),
    (bj(2026, 11, 9, 18, 15), False),
    (bj(2026, 11, 9, 3, 0), False),
    (bj(2026, 11, 7, 10, 0), False),   # Saturday
])
def test_is_peak(dt, peak):
    assert is_peak(dt) is peak


def test_next_offpeak_and_budget():
    assert next_offpeak(bj(2026, 11, 9, 9, 30)) == bj(2026, 11, 9, 12, 15)
    assert next_offpeak(bj(2026, 11, 9, 3, 0)) == bj(2026, 11, 9, 3, 0)
    assert minutes_until_peak(bj(2026, 11, 9, 8, 0)) == 45
    assert minutes_until_peak(bj(2026, 11, 7, 10, 0)) > 60 * 24  # weekend: next peak is Monday


def test_scheduler_times():
    assert next_radar(bj(2026, 11, 9, 7, 10)) == bj(2026, 11, 9, 12, 15)   # 09:00 slot moved out of peak
    assert next_radar(bj(2026, 11, 9, 12, 30)) == bj(2026, 11, 9, 18, 15)  # 15:00 slot moved
    assert next_radar(bj(2026, 11, 7, 7, 10)) == bj(2026, 11, 7, 9, 0)     # Saturday: no shift
    assert next_daily(bj(2026, 11, 9, 4, 0), ((3, 0), (5, 0), (7, 0))) == bj(2026, 11, 9, 5, 0)
    assert next_daily(bj(2026, 11, 9, 10, 0), ((10, 0),)) == bj(2026, 11, 10, 10, 0)


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, **kw):
        self.requests.append((url, kw.get("json_body")))
        return self.responses.pop(0)


def test_deepseek_refuses_peak_hours():
    client = DeepSeekClient(FakeHttp([]), "key", clock=lambda: bj(2026, 11, 9, 10, 0))
    with pytest.raises(PeakHoursError):
        client.search("anything")


def test_research_loop_registers_real_sources_only():
    tool_search = {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": "x"})}}
    tool_fetch = {"id": "c2", "type": "function", "function": {"name": "fetch_page", "arguments": json.dumps({"ref": "S1"})}}
    http = FakeHttp([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tool_search]}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        {"results": [{"url": "https://reuters.com/a", "title": "A", "site_name": "Reuters", "summary": "s"}]},
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tool_fetch]}}]},
        {"title": "A", "content": "full text"},
        {"choices": [{"message": {"role": "assistant", "content": '```json\n{"ok": true}\n```'}}]},
    ])
    client = DeepSeekClient(http, "key", clock=lambda: bj(2026, 11, 9, 3, 0))
    reg = SourceRegistry()
    assert client.research("deepseek-flash", "sys", {"a": 1}, reg) == {"ok": True}
    assert reg.get("S1").fetched and reg.get("S1").url == "https://reuters.com/a"
    assert client.usage.searches == 1 and client.usage.fetches == 1 and client.usage.cost_usd() > 0


def test_parse_json_tolerates_wrapping():
    assert parse_json('Here: {"a": 1} thanks') == {"a": 1}


# --- config --------------------------------------------------------------------------------
def test_settings_from_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=secret123\nTELEGRAM_PUBLISH_MODE=draft\nDEEPSEEK_MODEL=deepseek-chat\n")
    for k in ("TELEGRAM_BOT_TOKEN", "PUBLISH_MODE", "TELEGRAM_PUBLISH_MODE"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(env)
    assert s.telegram_bot_token == "secret123" and s.publish_mode == "draft"
    assert s.deepseek_model_pro == "deepseek-v4-pro"  # legacy deepseek-chat is ignored
    assert "secret123" not in repr(s) and "secret123" not in str(s)


def test_default_mode_is_auto():
    assert Settings().publish_mode == "auto"


# --- formatting ----------------------------------------------------------------------------
def test_tg_html_escapes_and_links():
    reg = SourceRegistry()
    reg.add("https://example.com/x?a=1&b=2", "T", "Example")
    html = to_tg_html("<script> **жирный** факт [S1] и [S9]", reg)
    assert "&lt;script&gt;" in html and "<b>жирный</b>" in html
    assert 'href="https://example.com/x?a=1&amp;b=2"' in html and "[S9]" not in html


def test_split_keeps_blocks_and_labels_parts():
    parts = split_html(["а" * 3000, "б" * 3000, "в" * 100])
    assert len(parts) == 2 and parts[0].endswith("<i>1/2</i>") and "в" * 100 in parts[1]


def test_telegraph_nodes_bullets():
    nodes = text_to_nodes("Абзац **важно**\n\n• один\n• два")
    assert nodes[0]["tag"] == "p" and nodes[1]["tag"] == "ul" and len(nodes[1]["children"]) == 2


def test_quality_helpers():
    assert is_russian("Компания выросла в три раза [S1] ARR")
    assert not is_russian("The company tripled revenue")
    assert count_sentences("Раз. Два! Три? Четыре…") == 4
