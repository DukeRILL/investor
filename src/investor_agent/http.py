"""Thin HTTP layer so business logic can be tested with fakes."""

import ipaddress
import json
import logging
import socket
import time
from typing import Any, Protocol
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message[:300]}")
        self.status = status


def is_public_url(url: str) -> bool:
    """True for http(s) URLs whose host resolves only to public addresses (blocks localhost/LAN/metadata)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


class Http(Protocol):
    def request(self, method: str, url: str, *, json_body: Any = None, params: dict | None = None,
                headers: dict | None = None, timeout: float = 60) -> Any: ...

    def get_bytes(self, url: str, max_bytes: int = 6_000_000, timeout: float = 30) -> tuple[bytes, str]: ...

    def post_file(self, url: str, filename: str, data: bytes, content_type: str, timeout: float = 60,
                  fields: dict | None = None) -> Any: ...

    def head(self, url: str, timeout: float = 15) -> int: ...


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

    def get_bytes(self, url: str, max_bytes: int = 6_000_000, timeout: float = 30) -> tuple[bytes, str]:
        """Download a public file (image or HTML) with a size cap. Returns (body, lower-case content type)."""
        if not is_public_url(url):
            raise HttpError(400, "url is not a public http(s) address")
        with self._client.stream("GET", url, timeout=timeout) as response:
            if response.status_code >= 400:
                raise HttpError(response.status_code, "download failed")
            if not is_public_url(str(response.url)):  # redirected somewhere private
                raise HttpError(400, "redirect to a non-public address")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise HttpError(413, "file is too large")
            return bytes(body), response.headers.get("content-type", "").split(";")[0].strip().lower()

    def head(self, url: str, timeout: float = 15) -> int:
        """HTTP status of a public URL without downloading it (follows redirects)."""
        if not is_public_url(url):
            return 400
        return self._client.head(url, timeout=timeout).status_code

    def post_file(self, url: str, filename: str, data: bytes, content_type: str, timeout: float = 60,
                  fields: dict | None = None) -> Any:
        response = self._client.post(url, files={"file": (filename, data, content_type)}, data=fields or None,
                                     timeout=timeout)
        if response.status_code >= 400:
            raise HttpError(response.status_code, response.text)
        return response.json()

    def close(self) -> None:
        self._client.close()
