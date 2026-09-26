"""Things two parts of Reel must agree on, pinned in one place."""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from reel.db import init_db
from reel.plan import KNOWN_AUDIO, KNOWN_VIDEO

STATIC = Path(__file__).resolve().parent.parent / "reel" / "static"


def test_live_schema(tmp_path):
    """The tables a fresh database ends up with: SCHEMA (the version-1 baseline) plus
    every migration. Adding a column or index means updating this on purpose."""
    path = tmp_path / "reel.db"
    init_db(path)
    conn = sqlite3.connect(path)
    tables = {
        t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    }
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}
    conn.close()
    assert tables == {
        "libraries": ["id", "uid", "name", "path", "created_at", "last_scan_at", "last_scan_error",
                      "last_scan_warning"],
        "media_items": ["id", "uid", "library_id", "rel_path", "title", "year", "poster_path", "size", "mtime",
                        "container", "video_codec", "audio_codec", "pix_fmt", "width", "height", "duration",
                        "interlaced", "probe_error", "custom_image", "missing_since", "scanned_at", "fingerprint",
                        "probe_version", "parent_dir", "title_key", "poster_rev"],
        "tags": ["id", "uid", "name", "created_at", "image_version"],
        "item_tags": ["item_id", "tag_id", "added_at"],
        "folder_images": ["uid", "library_id", "rel_dir", "version"],
        "folder_art": ["library_id", "rel_dir", "art_path", "art_rev"],
        "users": ["id", "uid", "name", "is_local", "created_at"],
        "marks": ["id", "uid", "item_id", "seconds", "created_at"],
    }
    # Browsing, tag order, move detection and the one local user depend on these.
    assert indexes == {"media_items_browse", "item_tags_order", "item_tags_tag", "media_items_fingerprint",
                       "users_one_local", "marks_item"}


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_browser_and_server_name_the_same_codecs():
    """caps.js asks the browser about these codecs and sends the names to /plan;
    plan.py ignores names it doesn't know. A mismatch fails silently: extra
    conversion, or a codec the browser can't play."""
    script = (
        f"const c = await import({json.dumps((STATIC / 'caps.js').as_uri())});"
        "console.log(JSON.stringify({video: Object.keys(c.VIDEO_TYPES), audio: Object.keys(c.AUDIO_TYPES)}));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    names = json.loads(out.stdout)
    assert set(names["video"]) == KNOWN_VIDEO
    assert set(names["audio"]) == KNOWN_AUDIO
