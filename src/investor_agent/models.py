from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


Status = Literal["PUBLISH", "DRAFT", "SKIP", "BLOCKED"]


class Source(BaseModel):
    id: str
    title: str
    url: HttpUrl
    publisher: str | None = None
    published_at: datetime | None = None
    verified_at: datetime | None = None
    supports: list[str] = Field(default_factory=list)
    reliability: Literal["high", "medium", "low"] = "medium"


class KeyFact(BaseModel):
    claim: str
    value: str | int | float | None = None
    unit: str | None = None
    period: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class ResearchInput(BaseModel):
    run_mode: Literal["UNICORN_DAILY", "MARKET_RADAR", "FULL_RESEARCH", "TELEGRAM_EDITOR", "FACT_CHECK"]
    run_timestamp_utc: datetime
    current_date: str
    previous_reports: list[dict[str, Any]] = Field(default_factory=list)
    previous_alerts: list[dict[str, Any]] = Field(default_factory=list)
    search_results: list[dict[str, Any]] = Field(default_factory=list)
    source_documents: list[dict[str, Any]] = Field(default_factory=list)
    market_data: list[dict[str, Any]] = Field(default_factory=list)
    investor_data: list[dict[str, Any]] = Field(default_factory=list)
    publication_context: dict[str, Any] = Field(default_factory=dict)


class ResearchResult(BaseModel):
    status: Status
    run_mode: str
    generated_at_utc: datetime
    verified_at_utc: datetime | None = None
    subject: str
    event_id: str
    executive_summary: str
    key_facts: list[KeyFact] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    private_report: str = ""
    telegram_post: str = ""
    sources: list[Source] = Field(default_factory=list)
    verification_notes: str = ""
    watch_triggers: list[str] = Field(default_factory=list)
    editorial_quality: str = ""
    reason_for_decision: str
