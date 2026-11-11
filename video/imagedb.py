"""Своя база картинок для роликов: что есть, откуда, чья лицензия и когда последний раз показывали.

Правило владельца (2026-11-08): картинка, уже использованная в ролике, снова доступна через 30 дней.
Внутри одного выпуска (4 ролика на статью) действуют свои правила раскладки; кулдаун считается между выпусками.
Файл базы лежит в data/video/images.sqlite, сами файлы картинок остаются на диске.
"""
import hashlib, json, os, re, sqlite3, time
from pathlib import Path

COOLDOWN_DAYS = 30
SCHEMA = """
create table if not exists images(
  id text primary key, source text not null, kind text not null, file text not null, sha1 text,
  width int, height int, tags text default '', meaning text default '', query text default '',
  license text default '', page text default '', author text default '', status text default 'approved',
  created_at real not null);
create table if not exists uses(
  id integer primary key autoincrement, image_id text not null references images(id),
  issue text not null, video text not null, used_at real not null);
create index if not exists uses_image on uses(image_id, used_at);
"""
WORD = re.compile(r"[a-zа-яё]{3,}", re.I)


def db_path() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data")) / "images.sqlite"


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def sha1_of(path: Path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def add(c: sqlite3.Connection, *, id: str, source: str, kind: str, file: str, tags: str = "", meaning: str = "", query: str = "",
        license: str = "", page: str = "", author: str = "", width: int = 0, height: int = 0, status: str = "approved",
        now: float | None = None) -> bool:
    """Записывает картинку. Повторная запись того же id обновляет описание и не трогает историю показов."""
    exists = c.execute("select 1 from images where id=?", (id,)).fetchone()
    digest = sha1_of(Path(file)) if Path(file).exists() else None
    if exists:
        c.execute("update images set tags=?, meaning=?, query=?, status=? where id=?", (tags, meaning, query, status, id))
    else:
        c.execute("insert into images(id,source,kind,file,sha1,width,height,tags,meaning,query,license,page,author,status,created_at)"
                  " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (id, source, kind, file, digest, width, height, tags, meaning, query, license, page, author, status, time.time() if now is None else now))
    c.commit()
    return not exists


def mark_used(c: sqlite3.Connection, image_id: str, issue: str, video: str, now: float | None = None) -> None:
    c.execute("insert into uses(image_id, issue, video, used_at) values(?,?,?,?)", (image_id, issue, video, time.time() if now is None else now))
    c.commit()


def last_used(c: sqlite3.Connection, image_id: str) -> float | None:
    r = c.execute("select max(used_at) from uses where image_id=?", (image_id,)).fetchone()
    return r[0]


def cooling_down(c: sqlite3.Connection, image_id: str, issue: str | None = None, now: float | None = None,
                 days: int = COOLDOWN_DAYS) -> bool:
    """True: картинку нельзя брать, она показывалась меньше days дней назад в другом выпуске.
    Показы из текущего выпуска (issue) не блокируют: внутри выпуска правила раскладки свои."""
    now = time.time() if now is None else now
    r = c.execute("select max(used_at) from uses where image_id=? and issue<>?", (image_id, issue or "")).fetchone()
    return r[0] is not None and now - r[0] < days * 86400


def find_local(c: sqlite3.Connection, query: str, issue: str | None = None, kinds: tuple[str, ...] = ("stock", "generated"),
               exclude: set[str] | None = None, now: float | None = None, limit: int = 5) -> list[dict]:
    """Подходящие картинки из своей базы до обращения к Pixabay: по словам запроса в тегах, смысле и прошлых запросах.
    Сначала те, что совпали по большему числу слов; одобренные, не на кулдауне, не из exclude."""
    words = {w.lower() for w in WORD.findall(query)}
    if not words:
        return []
    rows = c.execute("select * from images where status='approved' and kind in (%s)" % ",".join("?" * len(kinds)), kinds).fetchall()
    scored = []
    for r in rows:
        if exclude and r["id"] in exclude or cooling_down(c, r["id"], issue, now):
            continue
        hay = {w.lower() for w in WORD.findall(f"{r['tags']} {r['meaning']} {r['query']}")}
        hit = len(words & hay)
        if hit:
            scored.append((hit, r["created_at"], dict(r)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in scored[:limit]]


def report(c: sqlite3.Connection) -> dict:
    n = c.execute("select count(*) from images").fetchone()[0]
    u = c.execute("select count(*) from uses").fetchone()[0]
    by = {r[0]: r[1] for r in c.execute("select source, count(*) from images group by source")}
    return {"images": n, "uses": u, "by_source": by}
