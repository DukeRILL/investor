from datetime import datetime, timezone
from pathlib import Path

import pytest

from investor_agent.config import Settings
from investor_agent.models import ResearchResult, Source
from investor_agent.service import ResearchService
from investor_agent.storage import PublicationStore
from investor_agent.clients import telegram_chunks


class FakeDeepSeek:
    def __init__(self, result): self.result = result
    async def research(self, _): return self.result


class FakeTelegram:
    def __init__(self): self.posts = []
    async def publish(self, text): self.posts.append(text)


def result(status="PUBLISH", sources=True):
    return ResearchResult(
        status=status, run_mode="FULL_RESEARCH", generated_at_utc=datetime.now(timezone.utc), subject="Test", event_id="event-1",
        executive_summary="Summary", telegram_post="Post", reason_for_decision="Reason",
        sources=[Source(id="s1", title="Source", url="https://example.com", supports=["Test"])] if sources else [],
    )


def make_service(tmp_path: Path, publish_mode="auto"):
    settings = Settings(telegram_publish_mode=publish_mode, database_path=tmp_path / "db.sqlite3")
    telegram = FakeTelegram()
    return ResearchService(settings, FakeDeepSeek(result()), telegram, PublicationStore(settings.database_path)), telegram


@pytest.mark.asyncio
async def test_auto_publishes_verified_unique_result(tmp_path):
    service, telegram = make_service(tmp_path)
    output, published, decision = await service.run(None)
    assert output.status == "PUBLISH" and published and decision == "published"
    assert telegram.posts == ["Post"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["DRAFT", "BLOCKED", "SKIP"])
async def test_non_publish_status_is_not_sent(tmp_path, status):
    service, telegram = make_service(tmp_path)
    service.deepseek = FakeDeepSeek(result(status=status))
    _, published, decision = await service.run(None)
    assert not published and decision == f"status_{status.lower()}" and not telegram.posts


@pytest.mark.asyncio
async def test_source_and_duplicate_gate(tmp_path):
    service, telegram = make_service(tmp_path)
    service.deepseek = FakeDeepSeek(result(sources=False))
    _, published, decision = await service.run(None)
    assert not published and decision == "no_sources"
    service.deepseek = FakeDeepSeek(result())
    await service.run(None)
    _, published, decision = await service.run(None)
    assert not published and decision == "duplicate"
    assert len(telegram.posts) == 1


def test_telegram_chunks_escape_and_split():
    chunks = telegram_chunks("<b>" + "x" * 4100)
    assert len(chunks) == 2
    assert "&lt;b&gt;" in chunks[0]
