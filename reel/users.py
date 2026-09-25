"""Users. For now there's only the built-in local user; login will add more.

Everything that belongs to a person (watch progress, next) is stored against a
user from the start, and requests find theirs through current_user() in
main.py, so adding login means changing that one place.
"""
import sqlite3


def local_user(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT id, uid, name FROM users WHERE is_local = 1").fetchone()
    return {"id": row["id"], "uid": row["uid"], "name": row["name"]}
