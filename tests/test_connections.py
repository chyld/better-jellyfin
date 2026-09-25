"""Database connections are short-lived: none is held while a response is sent."""
import sqlite3

from fastapi.responses import FileResponse

from conftest import make_files
from reel import main


def test_database_is_closed_before_a_file_is_sent(client, media_root, monkeypatch):
    """A long direct-play download mustn't hold a database connection open."""
    make_files(media_root, "V/clip.mp4")
    lib = client.post("/api/libraries", json={"name": "V", "path": str(media_root / "V")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = client.get(f"/api/libraries/{lib}/browse").json()["items"][0]["id"]

    opened = []
    real_connect = main.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    def still_open(conn):
        try:
            conn.execute("SELECT 1")
            return True
        except sqlite3.ProgrammingError:
            return False

    open_while_sending = []
    real_call = FileResponse.__call__

    async def sending(self, scope, receive, send):
        open_while_sending.append(sum(still_open(c) for c in opened))
        await real_call(self, scope, receive, send)

    monkeypatch.setattr(main, "connect", tracking_connect)
    monkeypatch.setattr(FileResponse, "__call__", sending)
    assert client.get(f"/api/items/{video}/file").status_code == 200
    assert opened and open_while_sending == [0]


def test_connections_use_wal_with_normal_sync(tmp_path):
    from reel.db import connect

    conn = connect(tmp_path / "r.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1       # NORMAL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
