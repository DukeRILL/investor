"""SQLite state: unicorn archive, drafts, radar alerts and events, usage, locks."""
from __future__ import annotations


import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

INITIAL_EXCLUSIONS = [("Vinted", "marketplace", "Lithuania"), ("Whatnot", "live commerce", "USA"),
                      ("Brevo", "B2B SaaS", "France")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unicorn_archive (
    norm_name TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT, country TEXT,
    published_date TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS unicorn_drafts (
    issue_date TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL, published_at TEXT, page_url TEXT, message_ids TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS radar_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL, headline TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, message_ids TEXT);
CREATE INDEX IF NOT EXISTS radar_alerts_key ON radar_alerts(event_key);
CREATE TABLE IF NOT EXISTS radar_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, window_start TEXT, window_end TEXT,
    decision TEXT, note TEXT, ok INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS etf_flows (
    asset TEXT NOT NULL, session_date TEXT NOT NULL, total_musd REAL, status TEXT,
    PRIMARY KEY (asset, session_date));
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, job TEXT NOT NULL, cost_usd REAL NOT NULL,
    detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, until TEXT NOT NULL);
"""


def norm_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\b(inc|ltd|llc|gmbh|corp|corporation|co|technologies|labs|ai|hq|plc|sa|ag)\b\.?", " ", name)
    return re.sub(r"[^a-z0-9а-яё]+", "", name)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.path)
        raw.executescript(SCHEMA)
        cols = {r[1] for r in raw.execute("PRAGMA table_info(unicorn_archive)")}
        if "ai" not in cols:  # 1 = AI-related company (Unicorn School AI share), NULL = unknown (old rows)
            raw.execute("ALTER TABLE unicorn_archive ADD COLUMN ai INTEGER")
            raw.commit()
        raw.close()
        with self._conn() as c:
            for name, industry, country in INITIAL_EXCLUSIONS:
                c.execute("INSERT OR IGNORE INTO unicorn_archive(norm_name, name, industry, country, published_date) "
                          "VALUES (?,?,?,?, 'before-2026-11')", (norm_name(name), name, industry, country))

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # kv
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # locks (prevent overlapping runs, survive crashes via expiry)
    def acquire(self, name: str, minutes: int) -> bool:
        now = datetime.now(timezone.utc)
        with self._conn() as c:
            row = c.execute("SELECT until FROM locks WHERE name=?", (name,)).fetchone()
            if row and datetime.fromisoformat(row["until"]) > now:
                return False
            c.execute("INSERT INTO locks(name, until) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET until=excluded.until",
                      (name, (now + timedelta(minutes=minutes)).isoformat()))
        return True

    def release(self, name: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM locks WHERE name=?", (name,))

    # unicorn
    def archived_names(self) -> set[str]:
        with self._conn() as c:
            return {r["norm_name"] for r in c.execute("SELECT norm_name FROM unicorn_archive")}

    def archive_list(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT name, industry, country, published_date FROM unicorn_archive "
                                               "ORDER BY published_date DESC")]

    def archive_add(self, name: str, industry: str, country: str, date: str, url: str,
                    ai: bool | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO unicorn_archive(norm_name, name, industry, country, published_date, "
                      "url, ai) VALUES (?,?,?,?,?,?,?)",
                      (norm_name(name), name, industry, country, date, url, None if ai is None else int(bool(ai))))

    def last_issues(self, limit: int) -> list[dict]:
        """The latest published issues (newest first), with the AI flag, for the AI-share window."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT name, industry, published_date, ai FROM unicorn_archive WHERE published_date LIKE '20%' "
                "ORDER BY published_date DESC LIMIT ?", (limit,))]

    def recent_archive(self, days: int, today: str) -> list[dict]:
        since = (datetime.fromisoformat(today) - timedelta(days=days)).date().isoformat()
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT name, industry, country, published_date FROM unicorn_archive WHERE published_date >= ? "
                "AND published_date LIKE '20%' ORDER BY published_date DESC", (since,))]

    def draft_get(self, issue_date: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM unicorn_drafts WHERE issue_date=?", (issue_date,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        return out

    def draft_save(self, issue_date: str, status: str, payload: dict, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO unicorn_drafts(issue_date, status, payload, created_at, error) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(issue_date) DO UPDATE SET status=excluded.status, payload=excluded.payload, "
                      "error=excluded.error", (issue_date, status, json.dumps(payload, ensure_ascii=False), utcnow(), error))

    def draft_mark(self, issue_date: str, status: str, page_url: str | None = None,
                   message_ids: list[int] | None = None, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE unicorn_drafts SET status=?, page_url=COALESCE(?, page_url), "
                      "message_ids=COALESCE(?, message_ids), error=?, published_at=CASE WHEN ?='published' "
                      "THEN ? ELSE published_at END WHERE issue_date=?",
                      (status, page_url, json.dumps(message_ids) if message_ids else None, error, status, utcnow(),
                       issue_date))

    # radar
    def radar_history(self, limit: int = 25) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, event_key, headline, payload, status, created_at FROM radar_alerts "
                             "WHERE status IN ('sent','pending') ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            out.append({"id": r["id"], "event_key": r["event_key"], "headline": r["headline"], "sent_at": r["created_at"],
                        "conclusion": " ".join(p.get("plain_words", [])[:3]), "levels": p.get("_snapshot_levels", {})})
        return out

    def alert_exists(self, event_key: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM radar_alerts WHERE event_key=? AND status IN ('sent','pending')",
                             (event_key,)).fetchone() is not None

    def alert_create(self, event_key: str, headline: str, payload: dict) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO radar_alerts(event_key, headline, payload, status, created_at) "
                            "VALUES (?,?,?, 'pending', ?)", (event_key, headline, json.dumps(payload, ensure_ascii=False),
                                                             utcnow()))
            return int(cur.lastrowid)

    def alert_mark(self, alert_id: int, status: str, message_ids: list[int] | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE radar_alerts SET status=?, sent_at=?, message_ids=? WHERE id=?",
                      (status, utcnow() if status == "sent" else None, json.dumps(message_ids or []), alert_id))

    def pending_alerts(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM radar_alerts WHERE status='pending'")]

    def run_start(self, window_start: str, window_end: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO radar_runs(started_at, window_start, window_end) VALUES (?,?,?)",
                            (utcnow(), window_start, window_end))
            return int(cur.lastrowid)

    def run_finish(self, run_id: int, decision: str, note: str, ok: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE radar_runs SET decision=?, note=?, ok=? WHERE id=?", (decision, note[:2000], int(ok), run_id))

    def last_ok_window_end(self) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT window_end FROM radar_runs WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        return row["window_end"] if row else None

    def etf_save(self, asset: str, session_date: str, total: float | None, status: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO etf_flows VALUES (?,?,?,?) ON CONFLICT(asset, session_date) DO UPDATE SET "
                      "total_musd=excluded.total_musd, status=excluded.status", (asset, session_date, total, status))

    def etf_recent(self, asset: str, n: int = 10) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM etf_flows WHERE asset=? AND status='final' "
                                               "ORDER BY session_date DESC LIMIT ?", (asset, n))]

    # usage
    def usage_add(self, job: str, cost_usd: float, detail: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO usage(at, job, cost_usd, detail) VALUES (?,?,?,?)",
                      (utcnow(), job, cost_usd, json.dumps(detail)))

    def usage_by_job(self, since_iso: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self._conn() as c:
            for r in c.execute("SELECT job, cost_usd, detail FROM usage WHERE at >= ?", (since_iso,)):
                item = out.setdefault(r["job"], {"cost": 0.0, "runs": 0, "searches": 0, "fetches": 0})
                d = json.loads(r["detail"])
                item["cost"] += r["cost_usd"]
                item["runs"] += 1
                item["searches"] += int(d.get("searches", 0))
                item["fetches"] += int(d.get("fetches", 0))
        return out

    def usage_since(self, since_iso: str) -> float:
        with self._conn() as c:
            row = c.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE at >= ?", (since_iso,)).fetchone()
        return float(row["s"])
