from datetime import datetime, timezone

import pytest

from investor_agent.config import Settings
from investor_agent.http import HttpError
from investor_agent.postmypost import PostmypostClient, PostmypostError, PostmypostPublisher
from investor_agent.storage import Store


class PmpHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls, self.puts = [], []

    def request(self, method, url, *, json_body=None, params=None, headers=None, timeout=60, retry=True):
        self.calls.append((method, url, json_body, params, headers, retry))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def put_bytes(self, url, data, content_type, timeout=60):
        self.puts.append((url, data, content_type))


def configured(tmp_path, responses):
    settings = Settings(database_path=tmp_path / "db.sqlite3", postmypost_enabled=True,
                        postmypost_api_token="test-token", postmypost_project_id=12,
                        postmypost_vk_account_id=34)
    http = PmpHttp(responses)
    return Store(settings.database_path), http, PostmypostClient(http, settings)


def test_url_upload_and_scheduled_publication_are_documented_shape(tmp_path):
    store, http, client = configured(tmp_path, [
        {"id": 10, "status": 5}, {}, {"id": 10, "status": 1, "file_id": 99},
        {"id": 55},
    ])
    result = PostmypostPublisher(store, client).publish("2026-11-11", "text", "https://cdn.example/a.jpg",
                                                         datetime(2026, 11, 12, 10, tzinfo=timezone.utc))
    assert result == "scheduled"
    assert http.calls[0][0:3] == ("POST", "https://api.postmypost.io/v4.1/upload/init",
                                   {"project_id": 12, "url": "https://cdn.example/a.jpg"})
    assert http.calls[1][1].endswith("/upload/complete")
    create = http.calls[-1]
    assert create[0] == "POST" and create[-1] is False
    assert create[2]["publication_status"] == 4
    assert create[2]["account_ids"] == [34]
    assert create[2]["details"] == [{"publication_type": "post", "content": "text", "file_ids": [99]}]
    assert store.social_get("2026-11-11", "vk")["publication_id"] == "55"
    assert all(call[4]["Authorization"] == "Bearer test-token" for call in http.calls)


def test_local_file_uses_init_object_store_complete_and_status(tmp_path):
    path = tmp_path / "hero.png"
    path.write_bytes(b"png")
    store, http, client = configured(tmp_path, [
        {"id": 10, "url": "https://storage.example/upload"}, {}, {"id": 10, "status": 1, "file_id": 9}, {"id": 55},
    ])
    assert PostmypostPublisher(store, client).publish("a", "text", path) == "created"
    assert http.puts == [("https://storage.example/upload", b"png", "image/png")]
    assert http.calls[1][1].endswith("/upload/complete")


def test_duplicate_and_ambiguous_create_never_send_a_second_post(tmp_path):
    store, http, client = configured(tmp_path, [
        HttpError(502, "temporary"),
    ])
    publisher = PostmypostPublisher(store, client)
    with pytest.raises(PostmypostError) as exc:
        publisher.publish("same", "text", None)
    assert exc.value.ambiguous is True
    assert store.social_get("same", "vk")["status"] == "unknown"
    assert publisher.publish("same", "text", None) == "unknown"
    assert len(http.calls) == 1


def test_refresh_requires_a_real_destination_url(tmp_path):
    store, http, client = configured(tmp_path, [{"id": 55, "details": [{"post_url": "https://vk.com/wall-1_2"}]}])
    store.social_begin("a", "vk")
    store.social_mark("a", "vk", "scheduled", publication_id="55")
    assert PostmypostPublisher(store, client).refresh("a") == "published"
    assert store.social_get("a", "vk")["post_url"] == "https://vk.com/wall-1_2"


def test_disabled_never_calls_network(tmp_path):
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    http = PmpHttp([])
    assert PostmypostPublisher(Store(settings.database_path), PostmypostClient(http, settings)).publish("a", "x", None) == "disabled"
    assert http.calls == []


def test_rate_limit_retries_with_provider_retry_after(tmp_path, monkeypatch):
    store, http, client = configured(tmp_path, [HttpError(429, "slow down", retry_after=7), {"id": 55}])
    sleeps = []
    monkeypatch.setattr("investor_agent.postmypost.time.sleep", sleeps.append)
    assert PostmypostPublisher(store, client).publish("a", "x", None) == "created"
    assert sleeps == [7]
    assert len(http.calls) == 2
