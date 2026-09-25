"""Libraries and videos are addressed by random UUIDs, never by internal row numbers."""
import sqlite3
import uuid

from reel.db import connect, init_db

from conftest import make_files


def scan(client, media_root, *files):
    make_files(media_root, *files)
    lib = client.post("/api/libraries", json={"name": "Media", "path": str(media_root)}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    return lib["id"]


def ids_by_title(client, lib, path=""):
    page = client.get(f"/api/libraries/{lib}/browse", params={"path": path}).json()
    return {i["title"]: i["id"] for i in page["items"]}


def test_items_have_uuids(client, media_root):
    lib = scan(client, media_root, "Tapes/1992.zoo-trip.mpg", "Tapes/1998.hiking.mpg")
    ids = ids_by_title(client, lib, "Tapes")
    for value in ids.values():
        assert str(uuid.UUID(value)) == value
    assert len(set(ids.values())) == 2
    item = client.get(f"/api/items/{ids['zoo-trip']}").json()
    assert item["id"] == ids["zoo-trip"] and item["title"] == "zoo-trip"


def test_uuid_survives_rescans_and_file_changes(client, media_root, settings):
    lib = scan(client, media_root, "Tapes/1992.zoo-trip.mpg", "Tapes/1998.hiking.mpg")
    before = ids_by_title(client, lib, "Tapes")["zoo-trip"]
    (media_root / "Tapes/1992.zoo-trip.mpg").write_bytes(b"re-encoded")  # forces a re-probe
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    assert ids_by_title(client, lib, "Tapes")["zoo-trip"] == before


def test_row_numbers_are_not_accepted(client, media_root):
    scan(client, media_root, "Tapes/1992.zoo-trip.mpg")
    for suffix in ("", "/thumb", "/file", "/stream"):
        assert client.get(f"/api/items/1{suffix}").status_code == 404
    for suffix in ("/browse", "/cover", "/folder-art"):
        assert client.get(f"/api/libraries/1{suffix}").status_code == 404
    assert client.post("/api/libraries/1/scan").status_code == 404
    assert client.patch("/api/libraries/1", json={"name": "x"}).status_code == 404
    assert client.delete("/api/libraries/1").status_code == 404


def test_libraries_have_uuids(client, media_root):
    (media_root / "a").mkdir()
    (media_root / "b").mkdir()
    a = client.post("/api/libraries", json={"name": "A", "path": str(media_root / "a")}).json()
    b = client.post("/api/libraries", json={"name": "B", "path": str(media_root / "b")}).json()
    assert str(uuid.UUID(a["id"])) == a["id"] and a["id"] != b["id"]
    assert "uid" not in a  # only one id is shown, and it's the UUID
    assert [lib["id"] for lib in client.get("/api/libraries").json()] == [a["id"], b["id"]]

    # Every library endpoint takes the UUID.
    assert client.patch(f"/api/libraries/{a['id']}", json={"name": "A2"}).json()["id"] == a["id"]
    assert client.get(f"/api/libraries/{a['id']}/browse").json()["library"] == {"id": a["id"], "name": "A2"}
    assert set(client.post("/api/libraries/scan").json()) == {a["id"], b["id"]}
    client.scans.wait_idle()
    assert client.delete(f"/api/libraries/{b['id']}").status_code == 204


def test_item_detail_links_to_library_by_uuid(client, media_root):
    lib = scan(client, media_root, "Tapes/1992.zoo-trip.mpg", "Tapes/1998.hiking.mpg")
    item = client.get(f"/api/items/{ids_by_title(client, lib, 'Tapes')['zoo-trip']}").json()
    assert item["library_id"] == lib


def test_old_database_gets_uuids(tmp_path):
    """A database from before UUIDs is upgraded in place, keeping everything."""
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            path TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_scan_at TEXT, last_scan_error TEXT);
        CREATE TABLE media_items (id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL, rel_path TEXT NOT NULL,
            title TEXT NOT NULL, year INTEGER, poster_path TEXT, size INTEGER NOT NULL, mtime REAL NOT NULL,
            container TEXT, video_codec TEXT, audio_codec TEXT, pix_fmt TEXT, width INTEGER, height INTEGER,
            duration REAL, interlaced INTEGER NOT NULL DEFAULT 0, play_mode TEXT NOT NULL, probe_error TEXT,
            scanned_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (library_id, rel_path));
        INSERT INTO libraries (id, name, path) VALUES (1, 'Tapes', '/media/Tapes');
        INSERT INTO media_items (library_id, rel_path, title, size, mtime, play_mode) VALUES
            (1, 'a.mpg', 'a', 1, 1, 'transcode'), (1, 'b.mpg', 'b', 1, 1, 'transcode');
    """)
    old.close()

    init_db(db)
    init_db(db)  # running it again is harmless
    conn = connect(db)
    uids = [r["uid"] for r in conn.execute("SELECT uid FROM media_items ORDER BY id")]
    assert len(uids) == 2 and len(set(uids)) == 2
    assert all(str(uuid.UUID(u)) == u for u in uids)
    (lib_uid,) = [r["uid"] for r in conn.execute("SELECT uid FROM libraries")]
    assert str(uuid.UUID(lib_uid)) == lib_uid
    # Duplicate uids are refused.
    try:
        conn.execute("UPDATE media_items SET uid = ? WHERE id = 2", (uids[0],))
        raise AssertionError("duplicate uid accepted")
    except sqlite3.IntegrityError:
        pass
