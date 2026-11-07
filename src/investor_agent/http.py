"""Thin HTTP layer so business logic can be tested with fakes."""

import json
import logging
import time
from typing import Any, Protocol

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message[:300]}")
        self.status = status


class Http(Protocol):
    def request(self, method: str, url: str, *, json_body: Any = None, params: dict | None = None,
                headers: dict | None = None, timeout: float = 60) -> Any: ...


class HttpxClient:
    """Production client (httpx is imported lazily). Retries 408/429/5xx."""

    def __init__(self, retries: int = 3):
        import httpx  # noqa: PLC0415

        self._client = httpx.Client(follow_redirects=True, headers={"User-Agent": "investor-agent/1.0"})
        self.retries = retries

    def request(self, method, url, *, json_body=None, params=None, headers=None, timeout=60):
        delay = 2.0
        for attempt in range(self.retries + 1):
            response = self._client.request(method, url, json=json_body, params=params, headers=headers, timeout=timeout)
            if response.status_code in (408, 429) or response.status_code >= 500:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
            if response.status_code >= 400:
                raise HttpError(response.status_code, response.text)
            ctype = response.headers.get("content-type", "")
            if "json" in ctype:
                return response.json()
            try:
                return json.loads(response.text)
            except ValueError:
                return response.text
        raise HttpError(599, "retries exhausted")

    def close(self) -> None:
        self._client.close()
