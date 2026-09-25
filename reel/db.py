"""The database: schema, numbered migrations and connections.

The schema version lives in SQLite's own `PRAGMA user_version`:

    0   made before versioning (or brand new): brought up to version 1
    1   the baseline schema below
    2+  each entry in MIGRATIONS, applied once, in order, in a transaction

To change the schema, add a migration at the end of MIGRATIONS. Never edit an
existing one: databases out there have already run it.
"""
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id               INTEGER PRIMARY KEY,     -- internal only; never shown
    uid              TEXT NOT NULL UNIQUE,    -- random UUID used in URLs and the API
    name             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    path             TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_scan_at     TEXT,
    last_scan_error  TEXT,
    last_scan_warning TEXT                    -- e.g. folders the last scan couldn't read
);

CREATE TABLE IF NOT EXISTS media_items (
    id           INTEGER PRIMARY KEY,         -- internal only; never shown
    uid          TEXT NOT NULL UNIQUE,        -- random UUID used in URLs and the API
    library_id   INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    rel_path     TEXT NOT NULL,            -- relative to the library folder
    title        TEXT NOT NULL,
    year         INTEGER,
    poster_path  TEXT,                     -- relative to the library folder
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    container    TEXT,                     -- ffprobe format_name
    video_codec  TEXT,
    audio_codec  TEXT,
    pix_fmt      TEXT,
    width        INTEGER,
    height       INTEGER,
    duration     REAL,                     -- seconds
    interlaced   INTEGER NOT NULL DEFAULT 0,
    play_mode    TEXT NOT NULL,            -- direct | remux | transcode | unsupported
    probe_error  TEXT,
    custom_image TEXT,                     -- version of an uploaded image (when there's none on the NAS)
    missing_since TEXT,                    -- set when a scan no longer finds the file; removed after a grace period
    scanned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (library_id, rel_path)
);

CREATE TABLE IF NOT EXISTS tags (
    id             INTEGER PRIMARY KEY,
    uid            TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    image_version  TEXT               -- set when the tag has an uploaded image; changes on each upload
);

-- Which videos have which tags. Rows go away with their video or tag.
CREATE TABLE IF NOT EXISTS item_tags (
    item_id  INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS item_tags_tag ON item_tags (tag_id);

-- Images uploaded for folders that have no folder.<ext> on the NAS.
CREATE TABLE IF NOT EXISTS folder_images (
    uid         TEXT NOT NULL UNIQUE,      -- names the file: images/folders/<uid>.jpg
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    rel_dir     TEXT NOT NULL,             -- '' for the library root
    version     TEXT NOT NULL,             -- changes on each upload
    PRIMARY KEY (library_id, rel_dir)
);

CREATE TABLE IF NOT EXISTS folder_art (
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    rel_dir     TEXT NOT NULL,             -- '' for the library root
    art_path    TEXT NOT NULL,
    PRIMARY KEY (library_id, rel_dir)
);
"""


def _upgrade_unversioned(conn: sqlite3.Connection) -> None:
    """Version 0 -> 1: create the baseline, and patch databases from before versioning."""
    conn.executescript(SCHEMA)
    # Libraries and videos used to be addressed by their integer id; give each a UUID.
    for table in ("libraries", "media_items"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "uid" in columns:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
        ids = [row[0] for row in conn.execute(f"SELECT id FROM {table}")]
        conn.executemany(f"UPDATE {table} SET uid = ? WHERE id = ?", [(new_uid(), i) for i in ids])
        conn.execute(f"CREATE UNIQUE INDEX {table}_uid ON {table} (uid)")
        conn.commit()
    # Tags gained uploadable images, then videos did.
    for table, column in (
        ("tags", "image_version"),
        ("media_items", "custom_image"),
        ("media_items", "missing_since"),
        ("libraries", "last_scan_warning"),
    ):
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            conn.commit()


def new_uid() -> str:
    return str(uuid.uuid4())


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# (version, what it does, function). Append only.
MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = []


def latest_version() -> int:
    return MIGRATIONS[-1][0] if MIGRATIONS else 1


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db(db_path: Path) -> None:
    """Create the database, or bring an existing one up to the latest version."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        version = schema_version(conn)
        if version > latest_version():
            raise SystemExit(
                f"The database {db_path} is from a newer version of Reel (schema {version}); "
                f"this one understands up to schema {latest_version()}. Update Reel."
            )
        if version == 0:
            _upgrade_unversioned(conn)
            conn.execute("PRAGMA user_version = 1")
            version = 1
        for number, _description, migrate in MIGRATIONS:
            if number <= version:
                continue
            # All or nothing: a failed migration leaves the database as it was.
            conn.execute("BEGIN")
            try:
                migrate(conn)
                conn.execute(f"PRAGMA user_version = {number}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            version = number
    finally:
        conn.close()
