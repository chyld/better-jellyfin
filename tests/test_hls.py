"""HLS: a full playlist up front, segments encoded on demand into a bounded cache."""
import asyncio
import struct
import subprocess
import time
from pathlib import Path

import pytest

from reel import hls
from reel.hls import HlsManager, Source, playlist, segment_count
from reel.plan import Plan
from reel.playback import StreamLimits, StreamManager

from conftest import make_files, requires_ffmpeg

CONVERT = Plan("transcode", "encode", "encode")


# ---- Playlists (no ffmpeg needed) ------------------------------------------------------


def test_segment_count():
    assert segment_count(30.0) == 5
    assert segment_count(30.04) == 6
    assert segment_count(1.0) == 1
    assert segment_count(0.0) == 1


def test_playlist_covers_the_whole_video():
    text = playlist(20.0, "abc")
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U" and lines[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    durations = [float(line.split(":")[1].rstrip(",")) for line in lines if line.startswith("#EXTINF")]
    assert durations == [6.0, 6.0, 6.0, 2.0]
    assert [line for line in lines if line.endswith(".ts")] == [f"hls/abc/{n}.ts" for n in range(4)]


def test_sessions_are_shared_per_video_and_plan():
    a = hls.session_id("video-1", CONVERT)
    assert a == hls.session_id("video-1", CONVERT)
    assert a != hls.session_id("video-2", CONVERT)
    assert a != hls.session_id("video-1", Plan("transcode", "encode", "copy"))


# ---- Encoding on demand (real ffmpeg) ------------------------------------------------------


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory):
    """A 40-second old-format video (Xvid + MP3): seven 6-second segments."""
    path = tmp_path_factory.mktemp("hls") / "old.avi"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=40",
                    "-f", "lavfi", "-i", "sine=duration=40", "-c:v", "mpeg4", "-c:a", "libmp3lame", "-shortest",
                    str(path)], check=True)
    return path


def run(coro):
    return asyncio.run(coro)


def first_pts(segment: Path, stream: str = "v:0") -> float:
    """When the segment's first packet plays, on the whole video's timeline."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=pts_time",
                          "-read_intervals", "%+#1", "-of", "csv=p=0", str(segment)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip().strip(","))


def codecs(segment: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                          str(segment)], capture_output=True, text=True, check=True).stdout
    return list(dict.fromkeys(out.split()))  # TS lists each stream twice (with its program)


def manager(tmp_path, **limits):
    return HlsManager(tmp_path / "hls", StreamManager(StreamLimits(**limits)))


def source(path, plan=CONVERT):
    return Source(path, plan, 40.0, False, 240, "mp3")


@requires_ffmpeg
def test_first_segment(tmp_path, long_clip):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(m.open("vid", source(long_clip)))
        seg0 = await m.media_segment(s, 0)
        result = (codecs(seg0), first_pts(seg0))
        await m.shutdown()
        return result

    names, start = run(scenario())
    assert names == ["h264", "aac"] and start < 2  # TS adds a small fixed muxing delay


@requires_ffmpeg
def test_a_restarted_encoder_lines_up_with_the_playlist(tmp_path, long_clip):
    """Segment 5 from a fresh encoder must sit where segment 5 of a full run would."""
    whole, jumped = manager(tmp_path / "a"), manager(tmp_path / "b")

    async def scenario():
        a = whole.get(whole.open("vid", source(long_clip)))
        for n in range(6):
            reference = await whole.media_segment(a, n)
        b = jumped.get(jumped.open("vid", source(long_clip)))
        restarted = await jumped.media_segment(b, 5)     # nothing encoded yet: starts at 5
        result = (b.start, first_pts(reference), first_pts(restarted))
        await whole.shutdown()
        await jumped.shutdown()
        return result

    started_at, reference, restarted = run(scenario())
    assert started_at == 5
    assert restarted == pytest.approx(reference, abs=0.2)
    assert restarted == pytest.approx(30 + 1.4, abs=0.3)


@requires_ffmpeg
def test_segments_already_made_are_served_without_restarting(tmp_path, long_clip):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(m.open("vid", source(long_clip)))
        await m.media_segment(s, 3)
        proc = s.proc
        await m.media_segment(s, 3)              # again: from the cache
        await m.media_segment(s, 4)              # just ahead: wait, don't restart
        same = s.proc is proc or s.start == 3
        await m.shutdown()
        return same

    assert run(scenario())


@requires_ffmpeg
def test_encoder_stops_when_far_enough_ahead(tmp_path, long_clip, monkeypatch):
    monkeypatch.setattr(hls, "AHEAD_LIMIT", 2)
    m = manager(tmp_path)

    async def scenario():
        s = m.get(m.open("vid", source(long_clip)))
        await m.media_segment(s, 0)
        for _ in range(100):
            if not s.running():
                break
            await asyncio.sleep(0.1)
        stopped, produced = not s.running(), s.produced()
        await m.shutdown()
        return stopped, produced

    stopped, produced = run(scenario())
    assert stopped and produced <= 4             # paused about AHEAD_LIMIT past the viewer


@requires_ffmpeg
def test_encoders_use_stream_slots(tmp_path, long_clip):
    m = manager(tmp_path, max_streams=1, wait_for_slot=0.2)

    async def scenario():
        first = m.get(m.open("a", source(long_clip)))
        second = m.get(m.open("b", source(long_clip)))
        await m.media_segment(first, 0)
        from reel.playback import StreamBusy
        with pytest.raises(StreamBusy):
            await m.media_segment(second, 0)
        await m.shutdown()

    run(scenario())


@requires_ffmpeg
def test_idle_sessions_are_removed(tmp_path, long_clip):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(m.open("vid", source(long_clip)))
        await m.media_segment(s, 0)
        folder = s.folder
        removed = await m.remove_idle(idle_seconds=0)
        result = (removed, folder.exists(), m.get(s.sid), s.running())
        await m.shutdown()
        return result

    assert run(scenario()) == (1, False, None, False)


@requires_ffmpeg
def test_shutdown_stops_encoders_and_clears_the_cache(tmp_path, long_clip):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(m.open("vid", source(long_clip)))
        await m.media_segment(s, 0)
        proc = s.proc
        await m.shutdown()
        return proc

    proc = run(scenario())
    assert proc.returncode is not None
    assert not (tmp_path / "hls").exists()


def test_out_of_range_segment(tmp_path):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(m.open("vid", source(Path("/nope.avi"))))
        with pytest.raises(hls.HlsError):
            await m.media_segment(s, 999)

    run(scenario())


# ---- Through the API ----------------------------------------------------------------------


@requires_ffmpeg
def test_hls_through_the_api(client, media_root, long_clip, tmp_path):
    folder = media_root / "Tapes"
    folder.mkdir()
    (folder / "old.avi").write_bytes(long_clip.read_bytes())
    (folder / "other.avi").write_bytes(long_clip.read_bytes()[:-10])
    lib = client.post("/api/libraries", json={"name": "T", "path": str(folder)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = next(i["id"] for i in client.get(f"/api/libraries/{lib}/browse").json()["items"] if i["title"] == "old")

    plan = client.get(f"/api/items/{video}/plan", params={"hls_support": "mse"}).json()
    assert (plan["mode"], plan["delivery"]) == ("transcode", "hls")
    assert client.get(f"/api/items/{video}/plan").json()["delivery"] == "progressive"   # no HLS support

    res = client.get(plan["url"])
    assert res.status_code == 200 and "mpegurl" in res.headers["content-type"]
    uris = [line for line in res.text.splitlines() if line and not line.startswith("#")]
    duration = client.get(f"/api/items/{video}").json()["duration"]   # (this client's probe is a fake)
    assert len(uris) == segment_count(duration)
    base = f"/api/items/{video}/"
    seg = client.get(base + uris[2])
    assert seg.status_code == 200 and seg.headers["content-type"] == "video/mp2t"
    (tmp_path / "seg.ts").write_bytes(seg.content)
    assert codecs(tmp_path / "seg.ts") == ["h264", "mp3"]   # MP3 copied as is
    assert first_pts(tmp_path / "seg.ts") == pytest.approx(12.0 + 1.4, abs=0.3)
    assert client.get(base + "hls/nosuchsession/0.ts").status_code == 404


@requires_ffmpeg
def test_safari_gets_hls_even_for_copyable_video(client, media_root, clips):
    folder = media_root / "Films"
    folder.mkdir()
    (folder / "a.mkv").write_bytes((clips / "h264_aac.mkv").read_bytes())
    (folder / "b.mkv").write_bytes((clips / "h264_ac3.mkv").read_bytes())
    lib = client.post("/api/libraries", json={"name": "F", "path": str(folder)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = next(i["id"] for i in client.get(f"/api/libraries/{lib}/browse").json()["items"] if i["title"] == "a")
    chrome = client.get(f"/api/items/{video}/plan", params={"hls_support": "mse"}).json()
    safari = client.get(f"/api/items/{video}/plan", params={"hls_support": "native"}).json()
    assert (chrome["mode"], chrome["delivery"]) == ("remux", "progressive")   # copy: keep it cheap
    assert (safari["mode"], safari["delivery"]) == ("remux", "hls")           # Safari needs HLS
