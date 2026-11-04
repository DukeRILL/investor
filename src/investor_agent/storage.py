import sqlite3
from pathlib import Path


class PublicationStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS publications (
                event_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                published_at TEXT NOT NULL, source_urls TEXT NOT NULL)"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def already_published(self, event_id: str, subject: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM publications WHERE event_id = ? OR lower(subject) = lower(?)",
                (event_id, subject),
            ).fetchone()
        return row is not None

    def save(self, event_id: str, subject: str, published_at: str, source_urls: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO publications(event_id, subject, published_at, source_urls) VALUES (?, ?, ?, ?)",
                (event_id, subject, published_at, source_urls),
            )
