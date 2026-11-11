import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "video"))
import imagedb as D  # noqa: E402

DAY = 86400


def mk(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    return D.connect(tmp_path / "db.sqlite"), str(f)


def test_add_is_idempotent_and_keeps_history(tmp_path):
    c, f = mk(tmp_path)
    assert D.add(c, id="pixabay-1", source="pixabay", kind="stock", file=f, tags="key, keychain")
    D.mark_used(c, "pixabay-1", "whop-2026-11-08", "v1", now=1000)
    assert not D.add(c, id="pixabay-1", source="pixabay", kind="stock", file=f, tags="key, door")
    assert D.report(c) == {"images": 1, "uses": 1, "by_source": {"pixabay": 1}}


def test_cooldown_30_days_between_issues_only(tmp_path):
    c, f = mk(tmp_path)
    D.add(c, id="g1", source="flux", kind="generated", file=f)
    D.mark_used(c, "g1", "issue-A", "v1", now=0)
    assert D.cooling_down(c, "g1", "issue-B", now=29 * DAY)
    assert not D.cooling_down(c, "g1", "issue-B", now=31 * DAY)
    assert not D.cooling_down(c, "g1", "issue-A", now=DAY)  # внутри своего выпуска кулдаун не мешает


def test_find_local_matches_tags_skips_cooling_and_excluded(tmp_path):
    c, f = mk(tmp_path)
    D.add(c, id="s1", source="pixabay", kind="stock", file=f, tags="key, keychain, close up")
    D.add(c, id="s2", source="pixabay", kind="stock", file=f, tags="padlock, door, key")
    D.add(c, id="s3", source="pixabay", kind="stock", file=f, tags="harbour crane")
    D.add(c, id="s4", source="pixabay", kind="stock", file=f, tags="key", status="rejected")
    D.mark_used(c, "s2", "old", "v1", now=0)
    got = [r["id"] for r in D.find_local(c, "door key on table", issue="new", now=5 * DAY)]
    assert got == ["s1"]
    assert [r["id"] for r in D.find_local(c, "door key on table", issue="new", now=40 * DAY)] == ["s2", "s1"] or \
           {r["id"] for r in D.find_local(c, "door key on table", issue="new", now=40 * DAY)} == {"s1", "s2"}
    assert D.find_local(c, "door key", exclude={"s1", "s2"}) == []
