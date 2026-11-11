"""Postmypost v4.1 gateway.

The API's create-publication call is non-idempotent.  A durable ``creating``
row is therefore written before it is made.  A timeout/5xx is deliberately
left for reconciliation instead of risking a second VK post.
"""
from __future__ import annotations

import logging
import mimetypes
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .http import Http, HttpError
from .storage import Store

log = logging.getLogger(__name__)
BASE_URL = "https://api.postmypost.io/v4.1"
UPLOAD_READY, UPLOAD_ERROR = 1, 2


class PostmypostError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


def _as_dict(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PostmypostError("Postmypost returned an invalid JSON object")
    # The public API may wrap resources in data; accepting both makes the
    # client compatible with the documented v4.1 responses and test doubles.
    data = value.get("data", value)
    if not isinstance(data, dict):
        raise PostmypostError("Postmypost response has no resource object")
    return data


class PostmypostClient:
    def __init__(self, http: Http, settings: Settings):
        self.http, self.settings = http, settings

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.postmypost_enabled and s.postmypost_api_token and s.postmypost_project_id and s.postmypost_vk_account_id)

    def _request(self, method: str, path: str, *, json_body: dict | None = None,
                 params: dict | None = None, retry: bool = True) -> Any:
        headers = {"Authorization": f"Bearer {self.settings.postmypost_api_token}", "Accept": "application/json"}
        for attempt in range(3):
            try:
                return self.http.request(method, BASE_URL + path, json_body=json_body, params=params,
                                         headers=headers, retry=retry)
            except HttpError as exc:
                # A 429 is an explicit rejection, so retrying it cannot create
                # a duplicate.  Use Retry-After when the provider supplied it.
                if exc.status == 429 and attempt < 2:
                    time.sleep(exc.retry_after if exc.retry_after is not None else 2 ** attempt)
                    continue
                # Do not include headers or response bodies here: both may contain
                # provider data and must never disclose the bearer token.
                ambiguous = exc.status == 408 or exc.status >= 500
                raise PostmypostError(f"Postmypost HTTP {exc.status}", ambiguous=ambiguous) from exc

    def projects(self) -> Any:
        return self._request("GET", "/projects")

    def accounts(self) -> Any:
        return self._request("GET", "/accounts")

    def publication(self, publication_id: str) -> dict:
        return _as_dict(self._request("GET", f"/publications/{publication_id}"))

    def _wait_upload(self, upload_id: int, attempts: int = 12) -> int:
        for attempt in range(attempts):
            item = _as_dict(self._request("GET", "/upload/status", params={"id": upload_id}))
            status = int(item.get("status", 0))
            if status == UPLOAD_READY and item.get("file_id"):
                return int(item["file_id"])
            if status == UPLOAD_ERROR:
                raise PostmypostError("Postmypost could not process the uploaded file")
            time.sleep(min(2 ** attempt, 10))
        raise PostmypostError("Postmypost upload processing timed out")

    def upload_url(self, url: str) -> int:
        """Import a public image URL using the v4.1 n8n cookbook sequence."""
        init = _as_dict(self._request("POST", "/upload/init", json_body={
            "project_id": self.settings.postmypost_project_id, "url": url,
        }, retry=False))
        self._request("POST", "/upload/complete", json_body={"id": int(init["id"])}, retry=False)
        return self._wait_upload(int(init["id"]))

    def upload_file(self, path: Path) -> int:
        """Upload a local image through the documented init -> object store -> complete flow."""
        data = path.read_bytes()
        init = _as_dict(self._request("POST", "/upload/init", json_body={
            "project_id": self.settings.postmypost_project_id, "name": path.name, "size": len(data),
        }, retry=False))
        upload_url = init.get("url")
        if not isinstance(upload_url, str):
            raise PostmypostError("Postmypost upload initialization has no upload URL")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            self.http.put_bytes(upload_url, data, content_type)
        except HttpError as exc:
            raise PostmypostError(f"object-storage upload failed: HTTP {exc.status}") from exc
        self._request("POST", "/upload/complete", json_body={"id": int(init["id"])}, retry=False)
        return self._wait_upload(int(init["id"]))

    def create_publication(self, text: str, file_ids: list[int], scheduled_at: datetime | None = None) -> dict:
        payload: dict[str, Any] = {
            "project_id": self.settings.postmypost_project_id,
            "account_ids": [self.settings.postmypost_vk_account_id],
            "publication_status": 4,
            "details": [{"publication_type": "post", "content": text, "file_ids": file_ids}],
        }
        if scheduled_at:
            payload["post_at"] = scheduled_at.isoformat()
        return _as_dict(self._request("POST", "/publications", json_body=payload, retry=False))


def _publication_url(item: dict) -> str | None:
    """Provider fields vary per network; retain only an actual http(s) result URL."""
    for key in ("url", "post_url", "published_url", "link"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    for detail in item.get("details", []) if isinstance(item.get("details"), list) else []:
        if isinstance(detail, dict):
            found = _publication_url(detail)
            if found:
                return found
    return None


class PostmypostPublisher:
    """Durable service layer, reusable for VK now and other accounts later."""
    network = "vk"

    def __init__(self, store: Store, client: PostmypostClient):
        self.store, self.client = store, client

    def publish(self, article_id: str, text: str, image: str | Path | None,
                scheduled_at: datetime | None = None) -> str:
        if not self.client.configured:
            return "disabled"
        existing = self.store.social_get(article_id, self.network)
        if existing:
            return existing["status"]
        if not self.store.social_begin(article_id, self.network, scheduled_at.isoformat() if scheduled_at else None):
            return self.store.social_get(article_id, self.network)["status"]
        try:
            file_ids = []
            if image:
                file_ids = [self.client.upload_file(image) if isinstance(image, Path) else self.client.upload_url(image)]
            created = self.client.create_publication(text, file_ids, scheduled_at)
            publication_id = str(created.get("id") or "")
            if not publication_id:
                raise PostmypostError("Postmypost create response has no publication id", ambiguous=True)
            status = "scheduled" if scheduled_at else "created"
            self.store.social_mark(article_id, self.network, status, publication_id=publication_id,
                                   post_url=_publication_url(created))
            return status
        except PostmypostError as exc:
            self.store.social_mark(article_id, self.network, "unknown" if exc.ambiguous else "failed", error=str(exc))
            raise

    def refresh(self, article_id: str) -> str:
        row = self.store.social_get(article_id, self.network)
        if not row or not row.get("publication_id"):
            return "missing"
        item = self.client.publication(str(row["publication_id"]))
        url = _publication_url(item)
        # API documentation has no stable public enum for published state.
        # A provider-supplied destination URL is the success criterion.
        status = "published" if url else "scheduled"
        self.store.social_mark(article_id, self.network, status, post_url=url)
        return status

    def refresh_pending(self) -> int:
        """Poll only known provider IDs; this never creates a publication."""
        refreshed = 0
        for row in self.store.social_pending():
            if row["social_network"] != self.network or not row.get("publication_id"):
                continue
            try:
                self.refresh(row["article_id"])
                refreshed += 1
            except PostmypostError as exc:
                self.store.social_mark(row["article_id"], self.network, row["status"], error=str(exc))
        return refreshed
