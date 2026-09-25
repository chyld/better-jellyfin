"""Numbered schema migrations."""
import sqlite3

import pytest

from reel import db
from reel.db import connect, init_db, latest_version, schema_version


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def version_of(path):
    conn = connect(path)
    try:
        return schema_version(conn)
    finally:
        conn.close()


def test_new_database_is_at_the_latest_version(tmp_path):
    path = tmp_path / "reel.db"
    init_db(path)
    assert version_of(path) == latest_version()
    assert {"libraries", "media_items", "tags", "item_tags", "folder_images", "folder_art"} <= tables(path)


def test_running_again_changes_nothing(tmp_path):
    path = tmp_path / "reel.db"
    init_db(path)
    before = sqlite3.connect(path).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    init_db(path)
    after = sqlite3.connect(path).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert before == after and version_of(path) == latest_version()


def test_database_from_before_versioning_is_upgraded(tmp_path):
    """Version 0 with tables: the database a pre-versioning Reel left behind."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            path TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_scan_at TEXT, last_scan_error TEXT);
        INSERT INTO libraries (name, path) VALUES ('Tapes', '/media/Tapes');
    """)
    old.close()
    init_db(path)
    conn = connect(path)
    row = conn.execute("SELECT name, uid, last_scan_warning FROM libraries").fetchone()
    assert row["name"] == "Tapes" and row["uid"] and row["last_scan_warning"] is None
    assert schema_version(conn) == latest_version()


def test_database_from_a_newer_reel_is_refused(tmp_path):
    path = tmp_path / "reel.db"
    init_db(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {latest_version() + 1}")
    conn.close()
    with pytest.raises(SystemExit, match="from a newer version of Reel"):
        init_db(path)


def test_migrations_apply_in_order_and_only_once(tmp_path, monkeypatch):
    path = tmp_path / "reel.db"
    init_db(path)
    ran = []

    def add_notes(conn):
        ran.append(2)
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")

    def add_colour(conn):
        ran.append(3)
        conn.execute("ALTER TABLE notes ADD COLUMN colour TEXT")

    base = latest_version()
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [(base + 1, "notes", add_notes), (base + 2, "colour", add_colour)])
    init_db(path)
    init_db(path)
    assert ran == [2, 3] and version_of(path) == base + 2


def test_a_failed_migration_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "reel.db"
    init_db(path)
    base = latest_version()

    def half_done(conn):
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [(base + 1, "broken", half_done)])
    with pytest.raises(RuntimeError):
        init_db(path)
    assert version_of(path) == base
    assert "notes" not in tables(path)


# ---- Users and tag order ----------------------------------------------------------------


def test_there_is_exactly_one_local_user(tmp_path):
    path = tmp_path / "reel.db"
    init_db(path)
    init_db(path)
    conn = connect(path)
    rows = conn.execute("SELECT uid, name, is_local FROM users").fetchall()
    assert len(rows) == 1 and rows[0]["is_local"] == 1 and rows[0]["name"] == "Local"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (uid, name, is_local) VALUES ('x', 'Another', 1)")


def test_me_is_the_local_user(client):
    me = client.get("/api/me").json()
    assert me["name"] == "Local" and len(me["id"]) == 36


def test_upgrading_keeps_existing_tag_order(tmp_path, monkeypatch):
    """Tags added before migration 4 keep the order they had."""
    path = tmp_path / "reel.db"
    monkeypatch.setattr(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] < 4])
    init_db(path)
    conn = connect(path)
    conn.execute("INSERT INTO libraries (uid, name, path) VALUES ('l', 'L', '/m')")
    for i, name in enumerate(["first", "second", "third"], start=1):
        conn.execute("INSERT INTO media_items (uid, library_id, rel_path, title, size, mtime, play_mode) "
                     "VALUES (?, 1, ?, ?, 1, 1, 'direct')", (name, f"{name}.mp4", name))
    conn.execute("INSERT INTO tags (uid, name) VALUES ('t', 'family')")
    for item_id in (3, 1, 2):  # tagged in this order: third, first, second
        conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, 1)", (item_id,))
    conn.commit()
    conn.close()
    monkeypatch.undo()
    init_db(path)
    from reel.tags import tag_videos
    order = [i["title"] for i in tag_videos(connect(path), "t")["items"]]
    assert order == ["third", "first", "second"]


def test_tag_order_survives_a_rebuild(client, media_root):
    from conftest import make_files

    make_files(media_root, "V/a.mp4", "V/b.mp4", "V/c.mp4")
    lib = client.post("/api/libraries", json={"name": "V", "path": str(media_root / "V")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    ids = {i["title"]: i["id"] for i in client.get(f"/api/libraries/{lib}/browse").json()["items"]}
    for title in ("c", "a", "b"):
        tag = client.post(f"/api/items/{ids[title]}/tags", json={"name": "family"}).json()[0]
    conn = sqlite3.connect(client.app.state.settings.db_path)
    conn.execute("VACUUM")
    conn.close()
    assert [i["title"] for i in client.get(f"/api/tags/{tag['id']}").json()["items"]] == ["c", "a", "b"]
