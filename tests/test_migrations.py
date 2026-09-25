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
