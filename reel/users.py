"""Users: only the built-in local user.

Nothing is stored per user (watch progress was decided against). The users
table and current_user() in main.py are just where login would plug in, if it's
ever added: GET /api/me is the only thing that uses them.
"""
import sqlite3


def local_user(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT id, uid, name FROM users WHERE is_local = 1").fetchone()
    return {"id": row["id"], "uid": row["uid"], "name": row["name"]}
