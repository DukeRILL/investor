"""DeepSeek client: chat, Search/Fetch tools and a research agent loop.

Every call goes through the off-peak guard (docs/RULES.md, rule 1).
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .http import Http, HttpError
from .sources import SourceRegistry
from .timeutil import BEIJING, PeakHoursError, is_peak, minutes_until_peak, now_bj

log = logging.getLogger(__name__)

# Off-peak list prices, USD per 1M tokens (estimate for cost reports, not billing truth).
PRICES = {
    "deepseek-flash": {"hit": 0.003, "miss": 0.15, "out": 0.60},
    "deepseek-v4-pro": {"hit": 0.022, "miss": 0.66, "out": 1.98},
}
TOOL_PRICE = 0.75 / 1000  # per successful search or fetch


class BudgetExceededError(RuntimeError):
    """The day's DeepSeek spend passed the hard limit (2 x DAILY_BUDGET_USD); calls stop until tomorrow."""


@dataclass
class Usage:
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    searches: int = 0
    fetches: int = 0

    def add(self, model: str, usage: dict | None) -> None:
        if not usage:
            return
        bucket = self.by_model.setdefault(model, {"hit": 0, "miss": 0, "out": 0})
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or max(int(usage.get("prompt_tokens") or 0) - hit, 0))
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += int(usage.get("completion_tokens") or 0)

    def cost_usd(self) -> float:
        total = (self.searches + self.fetches) * TOOL_PRICE
        for model, b in self.by_model.items():
            p = PRICES.get(model, PRICES["deepseek-v4-pro"])
            total += (b["hit"] * p["hit"] + b["miss"] * p["miss"] + b["out"] * p["out"]) / 1_000_000
        return round(total, 4)


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Supports operators site:, -site:, \"quoted\", after:YYYY-MM-DD, before:YYYY-MM-DD. "
                       "Returns results with source ids.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query, max 128 characters"},
            "count": {"type": "integer", "description": "Number of results, 1-20", "default": 8}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Read the full text of a page. Pass a source id (S3) from search results or a URL.",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "Source id like S3, or a URL"}},
            "required": ["ref"]}}},
]


class DeepSeekClient:
    def __init__(self, http: Http, api_key: str, base_url: str = "https://api.deepseek.com",
                 clock: Callable[[], datetime] = now_bj, enforce_offpeak: bool = True,
                 store=None, daily_budget_usd: float = 0.0):
        self.http, self.api_key, self.base_url = http, api_key, base_url.rstrip("/")
        self.clock, self.enforce_offpeak = clock, enforce_offpeak
        self.store, self.daily_budget_usd = store, daily_budget_usd
        self.usage = Usage()
        self.invalid_retries, self.invalid_retry_delay = 3, 5.0

    # --- guard -----------------------------------------------------------------------------
    def _guard(self, min_minutes: float = 0) -> None:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        if self.enforce_offpeak:
            now = self.clock()
            if is_peak(now) or minutes_until_peak(now) < min_minutes:
                raise PeakHoursError(f"DeepSeek is not called during peak hours (now {now:%a %H:%M} Beijing)")
        self._budget_guard()

    def spent_today(self) -> float:
        """Estimated spend since 00:00 Beijing: finished jobs from the store plus the run in progress."""
        if not self.store:
            return self.usage.cost_usd()
        midnight = self.clock().astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.store.usage_since(midnight.astimezone(timezone.utc).isoformat(timespec="seconds")) + self.usage.cost_usd()

    def _budget_guard(self) -> None:
        if self.daily_budget_usd > 0 and self.spent_today() >= 2 * self.daily_budget_usd:
            raise BudgetExceededError(f"daily DeepSeek budget exhausted (about ${self.spent_today():.2f} today, "
                                      f"hard limit ${2 * self.daily_budget_usd:.2f})")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    # --- raw endpoints -----------------------------------------------------------------------
    def chat(self, model: str, messages: list[dict], tools: list | None = None, json_mode: bool = False,
             temperature: float = 0.3, max_tokens: int | None = None) -> dict:
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            body["tools"] = tools
        if json_mode and not tools:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        for attempt in range(1, self.invalid_retries + 1):
            self._guard(min_minutes=2)
            data = self.http.request("POST", f"{self.base_url}/chat/completions", json_body=body,
                                     headers=self._headers(), timeout=600)
            if isinstance(data, dict) and data.get("choices"):
                self.usage.add(model, data.get("usage"))
                choice = data["choices"][0]
                if choice.get("finish_reason") not in (None, "stop", "tool_calls"):
                    log.warning("deepseek finish_reason=%s usage=%s", choice.get("finish_reason"), data.get("usage"))
                return choice["message"]
            # DeepSeek sometimes answers 200 with an empty/non-JSON body: retry, then fail with a clear error.
            snippet = (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str))[:300]
            log.warning("invalid chat response (attempt %d/%d): %s %s", attempt, self.invalid_retries,
                        type(data).__name__, snippet)
            if attempt < self.invalid_retries:
                time.sleep(self.invalid_retry_delay * attempt)
        raise HttpError(502, f"DeepSeek returned an invalid chat response: {type(data).__name__} {snippet}")

    # --- cache: same query/page within `cache_ttl` hours is not paid for twice (retries of a failed issue) ----
    def _cache_get(self, key: str, ttl: float):
        if not (self.store and ttl > 0):
            return None
        raw = self.store.cache_get(key, ttl)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _cache_put(self, key: str, value, ttl: float) -> None:
        if self.store and ttl > 0:
            self.store.cache_put(key, json.dumps(value, ensure_ascii=False, default=str))

    def search(self, query: str, count: int = 8, cache_ttl: float = 0) -> list[dict]:
        body = {"query": query[:128], "count": max(1, min(int(count or 8), 20))}
        key = "search:" + hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
        cached = self._cache_get(key, cache_ttl)
        if cached is not None:
            return cached
        self._guard(min_minutes=1)
        data = self.http.request("POST", f"{self.base_url}/v1/tools/search", json_body=body,
                                 headers=self._headers(), timeout=60)
        results = data.get("results") or []
        if results:
            self.usage.searches += 1
            self._cache_put(key, results, cache_ttl)
        return results

    def fetch(self, url: str, cache_ttl: float = 0) -> dict:
        key = "fetch:" + hashlib.sha1(url.encode()).hexdigest()
        cached = self._cache_get(key, cache_ttl)
        if cached is not None:
            return cached
        self._guard(min_minutes=1)
        data = self.http.request("POST", f"{self.base_url}/v1/tools/fetch", json_body={"url": url},
                                 headers=self._headers(), timeout=90)
        if data.get("content"):
            self.usage.fetches += 1
            self._cache_put(key, data, cache_ttl)
        return data

    def balance(self) -> dict:
        """Account balance (free call, not a model call)."""
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        return self.http.request("GET", f"{self.base_url}/user/balance", headers=self._headers(), timeout=30)

    # --- agent -------------------------------------------------------------------------------
    def complete_json(self, model: str, system: str, payload: Any, temperature: float = 0.3,
                      max_tokens: int | None = None) -> dict:
        message = self.chat(model, [{"role": "system", "content": system},
                                    {"role": "user", "content": _dump(payload)}], json_mode=True,
                            temperature=temperature, max_tokens=max_tokens)
        return parse_json(message.get("content") or "")

    def research(self, model: str, system: str, payload: Any, registry: SourceRegistry,
                 max_tool_calls: int = 40, page_chars: int = 12000, temperature: float = 0.3,
                 cache_ttl: float = 0, max_tokens: int | None = None) -> dict:
        """Let the model search and read pages, then return its final JSON answer."""
        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": _dump(payload)}]
        calls = 0
        empty_replies = 0
        while True:
            tools = AGENT_TOOLS if calls < max_tool_calls else None
            if tools is None:
                messages.append({"role": "user", "content": "Tool budget is exhausted. Return the final JSON now."})
            message = self.chat(model, messages, tools=tools, json_mode=tools is None, temperature=temperature,
                               max_tokens=max_tokens)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content") or ""
                if not content.strip() and empty_replies < 2:
                    # JSON mode / a huge context sometimes yields an empty answer: ask again, without tools
                    empty_replies += 1
                    log.warning("empty final answer (attempt %d/2); asking again", empty_replies)
                    messages.append({"role": "user", "content": "Your last reply was empty. Return the complete final "
                                                                 "JSON object now, nothing else."})
                    calls = max(calls, max_tool_calls)
                    continue
                return parse_json(content)
            messages.append({k: v for k, v in message.items() if v is not None})
            for call in tool_calls:
                calls += 1
                result = self._run_tool(call, registry, page_chars, cache_ttl)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": _dump(result)})

    def _run_tool(self, call: dict, registry: SourceRegistry, page_chars: int, cache_ttl: float = 0) -> Any:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            return {"error": "invalid JSON arguments"}
        try:
            if fn.get("name") == "web_search":
                results = self.search(args.get("query", ""), args.get("count", 8), cache_ttl)
                out = []
                for r in results:
                    if not r.get("url"):
                        continue
                    src = registry.add(r["url"], r.get("title", ""), r.get("site_name") or "", r.get("date"),
                                       r.get("summary") or r.get("snippet") or "")
                    out.append({"id": src.id, "title": src.title, "publisher": src.publisher, "date": src.date,
                                "url": src.url, "summary": src.summary[:500]})
                return out or {"results": [], "note": "no results"}
            if fn.get("name") == "fetch_page":
                ref = str(args.get("ref", "")).strip()
                src = registry.get(ref)
                url = src.url if src else ref
                if not url.startswith(("http://", "https://")):
                    return {"error": "unknown source id; search first"}
                page = self.fetch(url, cache_ttl)
                content = page.get("content") or ""
                if not content:
                    return {"error": "page has no readable content"}
                if not src:
                    src = registry.add(url, page.get("title", ""))
                registry.mark_fetched(src.id, page.get("title", ""), content)
                return {"id": src.id, "title": src.title, "url": src.url, "content": content[:page_chars]}
            return {"error": f"unknown tool {fn.get('name')}"}
        except PeakHoursError:
            raise
        except HttpError as exc:
            return {"error": f"tool failed: HTTP {exc.status}"}


def _dump(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
