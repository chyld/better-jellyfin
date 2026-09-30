"""English subtitles made by Whisper (a stand-in worker here; see the last test for the real one)."""
import os
import shutil
import sys
import time
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reel import subtitles
from reel.db import connect, init_db
from reel.main import create_app
from reel.scan_manager import ScanManager
from reel.scanner import scan_library
from reel.subtitles import cues, timestamp, to_vtt

from conftest import make_files

FAKE = [sys.executable, str(Path(__file__).parent / "fake_subtitle_worker.py")]


# ---- Timing and the file ------------------------------------------------------------------


def test_lines_show_about_as_long_as_they_take_to_read():
    got = cues([
        (8.1, 12.7, " By the way, miss, was the cake you just made delicious? "),
        (42.6, 62.7, "It's dirty."),                     # stretched over 20 s of silence
        (65.5, 65.8, "It doesn't work."),                # a flash
        (66.2, 69.0, "It's heavy."),                     # starts before the flash would end
        (70.0, 71.0, "  "),                              # nothing said
        (80.0, 90.0, "Thank you for watching!"),         # Whisper's favourite invention
    ])
    assert [t for _, _, t in got] == ["By the way, miss, was the cake you just made delicious?", "It's dirty.",
                                      "It doesn't work.", "It's heavy."]
    assert got[0] == (8.1, 12.7, got[0][2])              # long enough already
    assert got[1][1] == pytest.approx(42.6 + 1.0 + 0.08 * 11)   # cut to reading time
    assert got[2][1] == pytest.approx(66.2)                 # at least a second, but not over the next
    assert all(a[1] <= b[0] for a, b in zip(got, got[1:]))


def test_no_line_stays_up_more_than_seven_seconds():
    [(start, end, _)] = cues([(0, 60, "x" * 200)])
    assert end - start == 7.0


def test_vtt():
    assert timestamp(3723.45) == "01:02:03.450"
    assert timestamp(-1) == "00:00:00.000"
    assert to_vtt([(1, 2.5, "Hello --> there")]) == "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nHello -> there\n"


# ---- Through the API ------------------------------------------------------------------------


def start(settings, worker=FAKE):
    init_db(settings.db_path)
    from conftest import FakeProbe
    app = create_app(settings, ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=FakeProbe())))
    app.state.subtitles.worker = worker
    return TestClient(app)


@pytest.fixture
def client(settings):
    with start(settings) as c:
        yield c


@pytest.fixture
def videos(client, media_root):
    make_files(media_root, "Films/a.mkv", "Films/slow.mkv", "Films/fail.mkv")
    lib = client.post("/api/libraries", json={"name": "Films", "path": str(media_root / "Films")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.app.state.scans.wait_idle()
    items = client.get(f"/api/libraries/{lib}/browse").json()["items"]
    return {"lib": lib, **{i["title"]: i["id"] for i in items}}


def wait(client, video, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = client.get(f"/api/items/{video}").json()
        if not item["subtitle_job"] or item["subtitle_job"]["state"] == "error":
            return item
        time.sleep(0.05)
    raise AssertionError("never finished")


def test_making_subtitles(client, videos, settings):
    video = videos["a"]
    before = client.get(f"/api/items/{video}").json()
    assert (before["can_subtitle"], before["subtitles"], before["subtitle_job"]) == (True, None, None)
    assert client.get(f"/api/items/{video}/subtitles.vtt").status_code == 404

    res = client.post(f"/api/items/{video}/subtitles")
    assert res.status_code == 202 and res.json()["state"] in ("queued", "running")
    item = wait(client, video)
    assert item["subtitle_job"] is None
    assert item["subtitles"]["source_language"] == "ja" and item["subtitles"]["model"] == "large-v3"

    vtt = client.get(f"/api/items/{video}/subtitles.vtt")
    assert vtt.status_code == 200 and vtt.headers["content-type"].startswith("text/vtt")
    assert vtt.text.startswith("WEBVTT") and "cake" in vtt.text
    download = client.get(f"/api/items/{video}/subtitles.vtt", params={"download": True})
    assert 'attachment; filename="a.en.vtt"' in download.headers["content-disposition"]
    assert [p.name.endswith(".en.vtt") for p in settings.subtitles_dir.iterdir()] == [True]

    listed = client.get("/api/subtitles").json()
    assert listed["jobs"] == [] and [(s["id"], s["source_language"]) for s in listed["subtitles"]] == [(video, "ja")]


def test_the_spoken_language_can_be_given(client, videos):
    client.post(f"/api/items/{videos['a']}/subtitles", json={"language": "ko"})
    assert wait(client, videos["a"])["subtitles"]["source_language"] == "ko"
    assert client.post(f"/api/items/{videos['a']}/subtitles", json={"language": "Japanese!"}).status_code == 422


def test_making_them_again_replaces_them(client, videos, settings):
    for _ in range(2):
        client.post(f"/api/items/{videos['a']}/subtitles")
        wait(client, videos["a"])
    settle(settings)
    assert len(list(settings.subtitles_dir.glob("*.vtt"))) == 1


def settle(settings):
    old = time.time() - 7200
    for path in settings.subtitles_dir.glob("*.vtt"):
        os.utime(path, (old, old))
    conn = connect(settings.db_path)
    subtitles.prune(conn, settings.subtitles_dir)
    conn.close()


def test_removing_them(client, videos, settings):
    client.post(f"/api/items/{videos['a']}/subtitles")
    wait(client, videos["a"])
    assert client.delete(f"/api/items/{videos['a']}/subtitles").status_code == 204
    assert client.get(f"/api/items/{videos['a']}").json()["subtitles"] is None
    assert client.get(f"/api/items/{videos['a']}/subtitles.vtt").status_code == 404
    settle(settings)
    assert list(settings.subtitles_dir.glob("*.vtt")) == []


def test_a_failure_says_why(client, videos, settings):
    client.post(f"/api/items/{videos['fail']}/subtitles")
    item = wait(client, videos["fail"])
    assert item["subtitle_job"]["state"] == "error" and "couldn't be read" in item["subtitle_job"]["error"]
    assert item["subtitles"] is None and list(settings.subtitles_dir.iterdir()) == []
    assert client.get("/api/subtitles").json()["jobs"][0]["state"] == "error"


def test_one_at_a_time_in_order_and_cancelling(client, videos):
    for name in ("slow", "a"):
        client.post(f"/api/items/{videos[name]}/subtitles")
    jobs = client.get("/api/subtitles").json()["jobs"]
    assert [(j["title"], j["state"], j.get("place")) for j in jobs] == [("slow", "running", None), ("a", "queued", 1)]
    time.sleep(0.5)
    assert 0 < client.get(f"/api/items/{videos['slow']}").json()["subtitle_job"]["progress"] < 1
    client.delete(f"/api/items/{videos['slow']}/subtitles")                     # stopped: the next one runs
    assert wait(client, videos["a"])["subtitles"]
    assert client.get(f"/api/items/{videos['slow']}").json()["subtitles"] is None


def test_paused_while_someone_watches(client, videos):
    manager = client.app.state.subtitles
    watching = [False]
    manager.watching = lambda: watching[0]
    client.post(f"/api/items/{videos['slow']}/subtitles")
    time.sleep(0.4)
    watching[0] = True
    deadline = time.monotonic() + 10
    while client.get(f"/api/items/{videos['slow']}").json()["subtitle_job"]["state"] != "paused":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    state = Path(f"/proc/{manager._proc.pid}/stat").read_text().split(") ")[1][0]
    assert state == "T"                                                           # really stopped
    progress = client.get(f"/api/items/{videos['slow']}").json()["subtitle_job"]["progress"]
    time.sleep(1)
    assert client.get(f"/api/items/{videos['slow']}").json()["subtitle_job"]["progress"] == progress
    watching[0] = False
    assert wait(client, videos["slow"])["subtitles"]


def test_videos_without_sound_and_unknown_ones(client, videos, settings):
    conn = connect(settings.db_path)
    conn.execute("UPDATE media_items SET audio_codec = NULL WHERE uid = ?", (videos["a"],))
    conn.commit()
    conn.close()
    assert client.get(f"/api/items/{videos['a']}").json()["can_subtitle"] is False
    assert client.post(f"/api/items/{videos['a']}/subtitles").status_code == 409
    assert client.post("/api/items/nope/subtitles").status_code == 404


def test_subtitles_go_with_their_library(client, videos, settings):
    client.post(f"/api/items/{videos['a']}/subtitles")
    wait(client, videos["a"])
    assert client.delete(f"/api/libraries/{videos['lib']}").status_code == 204
    settle(settings)
    assert list(settings.subtitles_dir.glob("*.vtt")) == []


def test_leftovers_are_removed_at_startup(settings):
    settings.subtitles_dir.mkdir(parents=True)
    (settings.subtitles_dir / "x-1.en.part").write_text("half")
    with start(settings):
        pass
    assert list(settings.subtitles_dir.iterdir()) == []


def test_settings_from_environment(monkeypatch):
    from reel.config import Settings
    monkeypatch.setenv("REEL_SUBTITLE_MODEL", "medium")
    monkeypatch.setenv("REEL_SUBTITLE_THREADS", "0")
    s = Settings.from_env()
    assert (s.subtitle_model, s.subtitle_threads) == ("medium", 1)
    monkeypatch.delenv("REEL_SUBTITLE_THREADS")
    assert Settings.from_env().subtitle_threads == max(1, (os.cpu_count() or 2) // 2)


# ---- The real Whisper (slow, downloads a model: only when asked) ------------------------------------


@pytest.mark.skipif(not os.environ.get("REEL_TEST_WHISPER") or not shutil.which("ffmpeg"),
                    reason="set REEL_TEST_WHISPER=1 to run Whisper for real (downloads the tiny model)")
def test_the_real_worker(tmp_path):
    """Whisper's tiny model on a test tone: the worker runs end to end and writes a
    valid (probably empty) WebVTT file."""
    import json
    import subprocess

    src = tmp_path / "tone.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=5", "-f", "lavfi", "-i",
                    "color=s=64x64:d=5", "-shortest", str(src)], check=True)
    out = tmp_path / "out.vtt"
    run = subprocess.run([sys.executable, "-m", "reel.subtitle_worker", str(src), str(out), "--model", "tiny",
                          "--threads", "2", "--models", str(tmp_path / "models")], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    news = [json.loads(line) for line in run.stdout.splitlines()]
    assert {"stage": "listening"} in news and news[-1] == {"progress": 1.0}
    assert out.read_text().startswith("WEBVTT")
