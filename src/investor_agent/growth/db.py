"""Persistent growth statistics in the project's SQLite (same file as everything else, survives restarts)."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from ..storage import Store, utcnow

SOURCES = ("telegram", "telegraph", "instagram", "tiktok", "youtube", "vk", "whatsapp", "other")
UNKNOWN = "UNKNOWN"  # a subscription whose source cannot be determined; never assigned to a guessed source

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, issue_date TEXT NOT NULL, company_name TEXT NOT NULL,
        article_url TEXT, telegram_message_id INTEGER, published_at TEXT, platform TEXT NOT NULL DEFAULT 'telegram',
        campaign_id TEXT, share_done INTEGER NOT NULL DEFAULT 0, UNIQUE(issue_date, platform))""",
    """CREATE TABLE IF NOT EXISTS tracking_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT, publication_id INTEGER NOT NULL REFERENCES publications(id),
        source TEXT NOT NULL, medium TEXT NOT NULL, campaign TEXT, target_url TEXT NOT NULL,
        code TEXT NOT NULL UNIQUE, invite_link TEXT, created_at TEXT NOT NULL,
        UNIQUE(publication_id, source, medium))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS tracking_links_invite ON tracking_links(invite_link) WHERE invite_link IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS traffic_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tracking_link_id INTEGER NOT NULL REFERENCES tracking_links(id),
        event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, event_deduplication_key TEXT NOT NULL UNIQUE)""",
    "CREATE INDEX IF NOT EXISTS traffic_events_link ON traffic_events(tracking_link_id, event_type)",
    """CREATE TABLE IF NOT EXISTS subscription_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, invite_link_id INTEGER REFERENCES tracking_links(id),
        publication_id INTEGER, event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
        event_deduplication_key TEXT NOT NULL UNIQUE)""",
    "CREATE INDEX IF NOT EXISTS subscription_events_time ON subscription_events(occurred_at, event_type)",
    """CREATE TABLE IF NOT EXISTS metric_snapshots (
        publication_id INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
        measured_at TEXT NOT NULL, provider TEXT NOT NULL,
        PRIMARY KEY (provider, publication_id, source, metric, measured_at))""",  # publication_id 0 = whole channel
    """CREATE TABLE IF NOT EXISTS provider_status (
        provider TEXT PRIMARY KEY, last_ok_at TEXT, last_error TEXT, last_error_at TEXT, disabled_until TEXT,
        calls_month TEXT, calls INTEGER NOT NULL DEFAULT 0)""",
]


def normalize_source(source: str) -> str:
    s = (source or "").strip().lower()
    return s if s in SOURCES else "other"


class GrowthDb:
    def __init__(self, store: Store):
        self.store = store
        with store._conn() as c:
            for stmt in STATEMENTS:
                c.execute(stmt)

    # publications -------------------------------------------------------------------------------------------
    def publication_upsert(self, issue_date: str, company: str, article_url: str | None = None,
                           message_id: int | None = None, published_at: str | None = None,
                           platform: str = "telegram", campaign: str | None = None) -> int:
        with self.store._conn() as c:
            c.execute(
                """INSERT INTO publications(issue_date, company_name, article_url, telegram_message_id, published_at,
                       platform, campaign_id) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(issue_date, platform) DO UPDATE SET
                       article_url = COALESCE(excluded.article_url, article_url),
                       telegram_message_id = COALESCE(excluded.telegram_message_id, telegram_message_id),
                       published_at = COALESCE(excluded.published_at, published_at)""",
                (issue_date, company, article_url, message_id, published_at, platform, campaign or f"issue-{issue_date}"))
            return c.execute("SELECT id FROM publications WHERE issue_date=? AND platform=?",
                             (issue_date, platform)).fetchone()["id"]

    def publication(self, pub_id: int) -> dict | None:
        with self.store._conn() as c:
            row = c.execute("SELECT * FROM publications WHERE id=?", (pub_id,)).fetchone()
        return dict(row) if row else None

    def publication_by_date(self, issue_date: str) -> dict | None:
        with self.store._conn() as c:
            row = c.execute("SELECT * FROM publications WHERE issue_date=? AND platform='telegram'",
                            (issue_date,)).fetchone()
        return dict(row) if row else None

    def publications(self, limit: int = 1000) -> list[dict]:
        with self.store._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM publications ORDER BY issue_date DESC LIMIT ?", (limit,))]

    def share_pending(self) -> list[dict]:
        with self.store._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM publications p WHERE share_done=0 AND article_url IS NOT NULL AND EXISTS "
                "(SELECT 1 FROM tracking_links l WHERE l.publication_id=p.id AND l.medium='cta')")]  # only issues born with the CTA

    def mark_share_done(self, pub_id: int) -> None:
        with self.store._conn() as c:
            c.execute("UPDATE publications SET share_done=1 WHERE id=?", (pub_id,))

    # tracking links -----------------------------------------------------------------------------------------
    def link_get_or_create(self, pub_id: int, source: str, medium: str, target_url: str,
                           campaign: str | None = None) -> dict:
        source = normalize_source(source)
        with self.store._conn() as c:
            row = c.execute("SELECT * FROM tracking_links WHERE publication_id=? AND source=? AND medium=?",
                            (pub_id, source, medium)).fetchone()
            if row:
                return dict(row)
            c.execute("INSERT INTO tracking_links(publication_id, source, medium, campaign, target_url, code, created_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (pub_id, source, medium, campaign, target_url, secrets.token_urlsafe(6), utcnow()))
            return dict(c.execute("SELECT * FROM tracking_links WHERE publication_id=? AND source=? AND medium=?",
                                  (pub_id, source, medium)).fetchone())

    def link_set(self, link_id: int, *, invite_link: str | None = None, target_url: str | None = None) -> None:
        with self.store._conn() as c:
            if invite_link:
                c.execute("UPDATE tracking_links SET invite_link=? WHERE id=?", (invite_link, link_id))
            if target_url:
                c.execute("UPDATE tracking_links SET target_url=? WHERE id=?", (target_url, link_id))

    def link_by_code(self, code: str) -> dict | None:
        with self.store._conn() as c:
            row = c.execute("SELECT * FROM tracking_links WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def link_by_invite(self, invite_link: str) -> dict | None:
        with self.store._conn() as c:
            row = c.execute("SELECT * FROM tracking_links WHERE invite_link=?", (invite_link,)).fetchone()
        return dict(row) if row else None

    def links(self) -> list[dict]:
        with self.store._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM tracking_links ORDER BY id")]

    # events -------------------------------------------------------------------------------------------------
    def add_traffic(self, link_id: int, event_type: str, key: str, at: str | None = None) -> bool:
        with self.store._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO traffic_events(tracking_link_id, event_type, occurred_at, "
                            "event_deduplication_key) VALUES (?,?,?,?)", (link_id, event_type, at or utcnow(), key))
            return cur.rowcount == 1

    def add_subscription(self, source: str, link_id: int | None, pub_id: int | None, event_type: str, key: str,
                         at: str | None = None) -> bool:
        with self.store._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO subscription_events(source, invite_link_id, publication_id, event_type, "
                            "occurred_at, event_deduplication_key) VALUES (?,?,?,?,?,?)",
                            (source, link_id, pub_id, event_type, at or utcnow(), key))
            return cur.rowcount == 1

    def subscription_counts(self, since: str | None = None, pub_id: int | None = None) -> dict[tuple[str, str], int]:
        """{(source, event_type): n}"""
        sql, args = "SELECT source, event_type, COUNT(*) n FROM subscription_events WHERE 1=1", []
        if since:
            sql += " AND occurred_at >= ?"
            args.append(since)
        if pub_id is not None:
            sql += " AND publication_id = ?"
            args.append(pub_id)
        with self.store._conn() as c:
            return {(r["source"], r["event_type"]): r["n"] for r in c.execute(sql + " GROUP BY 1, 2", args)}

    def traffic_counts(self, pub_id: int | None = None) -> dict[tuple[str, str], int]:
        """{(source, event_type): n} over tracking links."""
        sql = ("SELECT l.source, e.event_type, COUNT(*) n FROM traffic_events e JOIN tracking_links l "
               "ON l.id = e.tracking_link_id WHERE 1=1")
        args: list = []
        if pub_id is not None:
            sql += " AND l.publication_id = ?"
            args.append(pub_id)
        with self.store._conn() as c:
            return {(r["source"], r["event_type"]): r["n"] for r in c.execute(sql + " GROUP BY 1, 2", args)}

    def traffic_today(self, link_id: int, day: str) -> int:
        with self.store._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM traffic_events WHERE tracking_link_id=? AND occurred_at >= ?",
                             (link_id, day)).fetchone()["n"]

    # snapshots ----------------------------------------------------------------------------------------------
    def snapshot_add(self, pub_id: int, source: str, metric: str, value: float, provider: str,
                     measured_at: str | None = None) -> None:
        with self.store._conn() as c:
            c.execute("INSERT OR IGNORE INTO metric_snapshots VALUES (?,?,?,?,?,?)",
                      (pub_id, source, metric, float(value), measured_at or utcnow(), provider))

    def snapshot_latest(self, pub_id: int, source: str, metric: str, provider: str | None = None,
                        before: str | None = None) -> dict | None:
        sql = "SELECT * FROM metric_snapshots WHERE publication_id=? AND source=? AND metric=?"
        args: list = [pub_id, source, metric]
        if provider:
            sql += " AND provider=?"
            args.append(provider)
        if before:
            sql += " AND measured_at <= ?"
            args.append(before)
        with self.store._conn() as c:
            row = c.execute(sql + " ORDER BY measured_at DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None

    def snapshots(self) -> list[dict]:
        with self.store._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM metric_snapshots ORDER BY measured_at, provider")]

    # provider health / quota --------------------------------------------------------------------------------
    def provider(self, name: str) -> dict:
        with self.store._conn() as c:
            row = c.execute("SELECT * FROM provider_status WHERE provider=?", (name,)).fetchone()
        return dict(row) if row else {"provider": name, "last_ok_at": None, "last_error": None, "last_error_at": None,
                                      "disabled_until": None, "calls_month": None, "calls": 0}

    def provider_ok(self, name: str) -> None:
        with self.store._conn() as c:
            c.execute("INSERT INTO provider_status(provider, last_ok_at) VALUES (?,?) ON CONFLICT(provider) DO UPDATE "
                      "SET last_ok_at=excluded.last_ok_at, last_error=NULL", (name, utcnow()))

    def provider_error(self, name: str, error: str, disabled_until: str | None = None) -> None:
        with self.store._conn() as c:
            c.execute("INSERT INTO provider_status(provider, last_error, last_error_at, disabled_until) VALUES (?,?,?,?) "
                      "ON CONFLICT(provider) DO UPDATE SET last_error=excluded.last_error, "
                      "last_error_at=excluded.last_error_at, disabled_until=COALESCE(excluded.disabled_until, "
                      "disabled_until)", (name, error[:300], utcnow(), disabled_until))

    def provider_count_call(self, name: str, month: str) -> int:
        """Count one request against the month's quota *before* it is made (conservative)."""
        with self.store._conn() as c:
            c.execute("INSERT OR IGNORE INTO provider_status(provider) VALUES (?)", (name,))
            row = c.execute("SELECT calls_month, calls FROM provider_status WHERE provider=?", (name,)).fetchone()
            calls = (row["calls"] if row["calls_month"] == month else 0) + 1
            c.execute("UPDATE provider_status SET calls_month=?, calls=? WHERE provider=?", (month, calls, name))
            return calls

    def provider_calls(self, name: str, month: str) -> int:
        p = self.provider(name)
        return p["calls"] if p["calls_month"] == month else 0


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
