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
V = "0" * 16          # a viewer id
V2 = "1" * 16


# ---- Playlists (no ffmpeg needed) ------------------------------------------------------


def test_segment_count():
    assert segment_count(30.0) == 5
    assert segment_count(30.04) == 6
    assert segment_count(1.0) == 1
    assert segment_count(0.0) == 1


def test_playlist_covers_the_whole_video():
    text = playlist(20.0, "abc", V)
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U" and lines[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    durations = [float(line.split(":")[1].rstrip(",")) for line in lines if line.startswith("#EXTINF")]
    assert durations == [6.0, 6.0, 6.0, 2.0]
    assert [line for line in lines if line.endswith(".ts")] == [f"hls/abc/{V}/{n}.ts" for n in range(4)]
    with_query = playlist(20.0, "abc", V, "video=h264").splitlines()
    assert f"hls/abc/{V}/0.ts?video=h264" in with_query


def facts(**changes):
    base = dict(path=Path("/m/a.avi"), plan=CONVERT, duration=40.0, interlaced=False, height=240,
                audio_codec="mp3", revision="r1")
    return Source(**{**base, **changes})


def test_sessions_are_shared_per_video_plan_file_version_and_facts(monkeypatch):
    a = hls.session_id("video-1", facts())
    assert a == hls.session_id("video-1", facts())
    assert a == hls.session_id("video-1", facts(path=Path("/m/moved.avi")))   # a move keeps it
    assert a != hls.session_id("video-2", facts())
    assert a != hls.session_id("video-1", facts(plan=Plan("transcode", "encode", "copy")))
    assert a != hls.session_id("video-1", facts(revision="r2"))               # the file changed
    for change in ({"duration": 41.0}, {"interlaced": True}, {"height": 480}, {"audio_codec": "aac"}):
        assert a != hls.session_id("video-1", facts(**change))                 # re-probed differently
    monkeypatch.setattr(hls, "PROFILE_VERSION", hls.PROFILE_VERSION + 1)
    assert a != hls.session_id("video-1", facts())                            # the encoder changed


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


def segment_uris(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


async def forget_from(m, session, n):
    """Stop every encoder of the session and delete segments from n on, so segment n
    must be encoded again (a tiny test clip is often encoded whole in a moment)."""
    for viewer in list(session.viewers.values()):
        await m._stop(viewer)               # waits until it's reaped and has published
    for path in session.folder.glob("*.ts"):
        if int(path.stem) >= n:
            path.unlink()


def manager(tmp_path, **limits):
    return HlsManager(tmp_path / "hls", StreamManager(StreamLimits(**limits)))


def source(path, plan=CONVERT):
    rev = hls.revision(path) if path.exists() else "none"
    return Source(path, plan, 40.0, False, 240, "mp3", rev)


@requires_ffmpeg
def test_first_segment(tmp_path, long_clip):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(long_clip)))
        seg0 = await m.media_segment(s, V, 0)
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
        a = whole.get(await whole.open("vid", source(long_clip)))
        for n in range(6):
            reference = await whole.media_segment(a, V, n)
        b = jumped.get(await jumped.open("vid", source(long_clip)))
        restarted = await jumped.media_segment(b, V, 5)     # nothing encoded yet: starts at 5
        result = (b.viewers[V].encoder.start, first_pts(reference), first_pts(restarted))
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
        s = m.get(await m.open("vid", source(long_clip)))
        await m.media_segment(s, V, 3)
        await m.media_segment(s, V, 3)              # again: from the cache
        await m.media_segment(s, V, 4)              # just ahead: wait, don't restart
        same = s.starts == 1
        await m.shutdown()
        return same

    assert run(scenario())


@requires_ffmpeg
def test_encoder_stops_when_far_enough_ahead(tmp_path, long_clip, monkeypatch):
    monkeypatch.setattr(hls, "AHEAD_LIMIT", 2)
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(long_clip)))
        await m.media_segment(s, V, 0)
        enc = s.viewers[V].encoder
        for _ in range(100):
            if not enc.running():
                break
            await asyncio.sleep(0.1)
        stopped, produced = not enc.running(), s.produced(enc)
        await m.shutdown()
        return stopped, produced

    stopped, produced = run(scenario())
    assert stopped and produced <= 4             # paused about AHEAD_LIMIT past the viewer


@requires_ffmpeg
def test_encoders_use_stream_slots(tmp_path, long_clip):
    m = manager(tmp_path, max_streams=1, wait_for_slot=0.2)

    async def scenario():
        first = m.get(await m.open("a", source(long_clip)))
        second = m.get(await m.open("b", source(long_clip)))
        await m.media_segment(first, V, 0)
        from reel.playback import StreamBusy
        with pytest.raises(StreamBusy):
            await m.media_segment(second, V, 0)
        await m.shutdown()

    run(scenario())


@requires_ffmpeg
def test_idle_sessions_are_removed(tmp_path, long_clip):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(long_clip)))
        await m.media_segment(s, V, 0)
        folder = s.folder
        removed = await m.remove_idle(idle_seconds=0)
        result = (removed, folder.exists(), m.get(s.sid), bool(s.encoders()))
        await m.shutdown()
        return result

    assert run(scenario()) == (1, False, None, False)


@requires_ffmpeg
def test_shutdown_stops_encoders_and_clears_the_cache(tmp_path, long_clip):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(long_clip)))
        await m.media_segment(s, V, 0)
        proc = s.viewers[V].encoder.proc
        await m.shutdown()
        return proc

    proc = run(scenario())
    assert proc.returncode is not None
    assert not (tmp_path / "hls").exists()


def test_out_of_range_segment(tmp_path):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(Path("/nope.avi"))))
        with pytest.raises(hls.HlsError):
            await m.media_segment(s, V, 999)

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
    uris = segment_uris(res.text)
    duration = client.get(f"/api/items/{video}").json()["duration"]   # (this client's probe is a fake)
    assert len(uris) == segment_count(duration)
    base = f"/api/items/{video}/"
    seg = client.get(base + uris[2])
    assert seg.status_code == 200 and seg.headers["content-type"] == "video/mp2t"
    (tmp_path / "seg.ts").write_bytes(seg.content)
    assert codecs(tmp_path / "seg.ts") == ["h264", "mp3"]   # MP3 copied as is
    assert first_pts(tmp_path / "seg.ts") == pytest.approx(12.0 + 1.4, abs=0.3)
    # An unknown session that can't be made again from the URL: the video changed.
    assert client.get(base + f"hls/nosuchsession/{V}/0.ts?" + plan["url"].split("?")[1]).status_code == 410


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
    # Safari needs HLS, whose segments must start on exact keyframes: the video is encoded.
    assert (safari["mode"], safari["video"], safari["audio"], safari["delivery"]) == ("transcode", "encode", "copy", "hls")
    assert "re-encoded" in safari["note"] and chrome["note"] is None
    assert "hls_support=native" in safari["url"]
    playlist_text = client.get(safari["url"]).text
    seg = client.get(f"/api/items/{video}/" + segment_uris(playlist_text)[0])
    assert seg.status_code == 200


@requires_ffmpeg
def test_flac_audio_is_converted_for_hls(client, media_root, clips, tmp_path, monkeypatch):
    """MP4 can carry FLAC but MPEG-TS can't: the HLS plan converts it, and the segment has AAC."""
    from conftest import FakeProbe
    from reel.probe import ProbeResult

    folder = media_root / "Old"
    folder.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=8",
                    "-f", "lavfi", "-i", "sine=duration=8", "-c:v", "mpeg4", "-c:a", "flac", "-shortest",
                    str(folder / "old.mkv")], check=True)
    monkeypatch.setitem(FakeProbe.PROFILES, ".mkv",
                        ProbeResult("matroska,webm", "mpeg4", "flac", "yuv420p", 320, 240, 8.0))
    lib = client.post("/api/libraries", json={"name": "O", "path": str(folder)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = client.get(f"/api/libraries/{lib}/browse").json()["items"][0]["id"]

    assert client.get(f"/api/items/{video}/plan").json()["audio"] == "copy"          # progressive MP4
    plan = client.get(f"/api/items/{video}/plan", params={"hls_support": "mse"}).json()
    assert (plan["delivery"], plan["audio"]) == ("hls", "encode") and "FLAC" in plan["note"]
    first = segment_uris(client.get(plan["url"]).text)[0]
    seg = client.get(f"/api/items/{video}/" + first)
    assert seg.status_code == 200
    (tmp_path / "seg.ts").write_bytes(seg.content)
    assert codecs(tmp_path / "seg.ts") == ["h264", "aac"]


def test_playlist_refuses_a_video_that_isnt_hls(client, media_root):
    make_files(media_root, "V/clip.mkv")
    lib = client.post("/api/libraries", json={"name": "V", "path": str(media_root / "V")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = client.get(f"/api/libraries/{lib}/browse").json()["items"][0]["id"]
    # H.264 + AAC for hls.js: copied into the progressive stream, not HLS.
    assert client.get(f"/api/items/{video}/hls.m3u8", params={"hls_support": "mse"}).status_code == 409


# ---- Failures, file changes, several viewers, the cache (real ffmpeg) ------------------------


def slots_free(m) -> bool:
    return m.streams.active == set() and m.streams._slots._value == m.streams.limits.max_streams


@pytest.fixture
def stuck(tmp_path):
    """A named pipe nobody writes to: ffmpeg opens it and waits forever."""
    import os
    path = tmp_path / "stuck.avi"
    os.mkfifo(path)
    return path


@requires_ffmpeg
def test_encoder_that_makes_nothing_is_reaped_with_a_reason(tmp_path, stuck, monkeypatch):
    monkeypatch.setattr(hls, "STARTUP_TIMEOUT", 1.0)
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(stuck)))
        with pytest.raises(hls.HlsError) as err:
            await m.media_segment(s, V, 0)
        result = (str(err.value), s.viewers[V].encoder.proc.returncode, slots_free(m))
        await m.shutdown()
        return result

    message, returncode, free = run(scenario())
    assert "no progress for 1 seconds" in message
    assert returncode is not None and free


@requires_ffmpeg
def test_viewer_that_gives_up_stops_its_encoder(tmp_path, stuck, monkeypatch):
    monkeypatch.setattr(hls, "SEGMENT_TIMEOUT", 0.5)
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(stuck)))
        with pytest.raises(hls.HlsError, match="Timed out"):
            await m.media_segment(s, V, 0)
        result = (s.viewers[V].encoder, s.viewers[V].last_failure, slots_free(m))
        await m.shutdown()
        return result

    assert run(scenario()) == (None, "a viewer gave up waiting for it", True)


@requires_ffmpeg
def test_encoder_error_is_reported(tmp_path):
    bad = tmp_path / "bad.avi"
    bad.write_bytes(b"not a video at all" * 100)
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(bad)))
        with pytest.raises(hls.HlsError) as err:
            await m.media_segment(s, V, 0)
        result = (str(err.value), slots_free(m))
        await m.shutdown()
        return result

    message, free = run(scenario())
    assert message.startswith("ffmpeg couldn't produce") and "it stopped" not in message and free


@pytest.fixture(scope="module")
def two_minutes(tmp_path_factory):
    """A small 2-minute video (20 segments), for viewers far apart."""
    path = tmp_path_factory.mktemp("hls2") / "long.avi"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=120",
                    "-f", "lavfi", "-i", "sine=duration=120", "-c:v", "mpeg4", "-c:a", "libmp3lame", "-shortest",
                    str(path)], check=True)
    return path


@requires_ffmpeg
def test_two_viewers_far_apart_dont_restart_each_other(tmp_path, two_minutes, monkeypatch):
    monkeypatch.setattr(hls, "AHEAD_LIMIT", 3)
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", Source(two_minutes, CONVERT, 120.0, False, 120, "mp3",
                                             hls.revision(two_minutes))))
        for n in range(3):
            await m.media_segment(s, V, n)          # one tab near the start
            await m.media_segment(s, V2, 12 + n)    # another further on
        result = (s.viewers[V].encoder.start, s.viewers[V2].encoder.start)
        await m.shutdown()
        return result

    near, far = run(scenario())
    # Each tab kept its own encoder: the far one was never moved back to the start.
    assert near < 12 and far == 12


@requires_ffmpeg
def test_viewers_are_capped_per_session(tmp_path, long_clip, monkeypatch):
    monkeypatch.setattr(hls, "MAX_VIEWERS", 2)
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(long_clip)))
        for viewer in ("a" * 16, "b" * 16, "c" * 16):
            await m.media_segment(s, viewer, 0)
        viewers = sorted(s.viewers)
        await m.shutdown()
        return viewers

    assert run(scenario()) == ["b" * 16, "c" * 16]


@requires_ffmpeg
def test_replaced_file_retires_the_old_session(tmp_path, long_clip):
    import os
    video = tmp_path / "v.avi"
    video.write_bytes(long_clip.read_bytes())
    m = manager(tmp_path)

    async def scenario():
        old = m.get(await m.open("vid", source(video)))
        await m.media_segment(old, V, 0)
        proc = old.viewers[V].encoder.proc
        video.write_bytes(long_clip.read_bytes()[:-5000])       # a new version of the file
        os.utime(video, ns=(1, 1))
        new_sid = await m.open("vid", source(video))
        result = (new_sid != old.sid, old.retired, old.folder.exists(), proc.returncode is not None,
                  m.get(old.sid), slots_free(m))
        await m.shutdown()
        return result

    changed, retired, folder, reaped, lookup, free = run(scenario())
    assert changed and retired and not folder and reaped and lookup is None and free


@requires_ffmpeg
def test_file_replaced_during_a_session_is_noticed_when_encoding(tmp_path, long_clip):
    import os
    video = tmp_path / "v.avi"
    video.write_bytes(long_clip.read_bytes())
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(video)))
        await m.media_segment(s, V, 0)
        await forget_from(m, s, 6)
        assert not s.segment(6).exists() and not s.encoders()
        os.utime(video, ns=(1, 1))
        with pytest.raises(hls.HlsGone):
            await m.media_segment(s, V2, 6)          # needs a new encoder: checks the file
        await asyncio.sleep(0.5)                     # retiring runs in the background
        result = (s.retired, s.folder.exists(), slots_free(m))
        await m.shutdown()
        return result

    assert run(scenario()) == (True, False, True)


@requires_ffmpeg
def test_moved_file_keeps_its_session(tmp_path, long_clip):
    import os
    video = tmp_path / "old-name.avi"
    video.write_bytes(long_clip.read_bytes())
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(video)))
        await m.media_segment(s, V, 0)
        moved = tmp_path / "new-name.avi"
        os.rename(video, moved)
        sid = await m.open("vid", source(moved))
        seg = await m.media_segment(s, V, 5)         # encoded from the new path
        result = (sid == s.sid, s.source.path == moved, seg.exists())
        await m.shutdown()
        return result

    assert run(scenario()) == (True, True, True)


@requires_ffmpeg
def test_cache_limit_keeps_what_viewers_need(tmp_path, long_clip, monkeypatch):
    m = manager(tmp_path)
    m.cache_limit = 1                                # far too small: evict all it can

    async def scenario():
        s = m.get(await m.open("vid", source(long_clip)))
        for n in range(7):
            await m.media_segment(s, V, n)
        await m._stop(s.viewers[V])
        monkeypatch.setattr(hls, "KEEP_NEAR", 2)
        freed = await m.enforce_cache_limit()        # the viewer is at 6: keeps 5 and 6
        left = sorted(int(p.stem) for p in s.folder.glob("*.ts"))
        await m.shutdown()
        return freed, left

    freed, left = run(scenario())
    assert freed > 0 and left == [5, 6]


@pytest.fixture
def hls_video(client, media_root, long_clip):
    folder = media_root / "Tapes"
    folder.mkdir()
    (folder / "old.avi").write_bytes(long_clip.read_bytes())
    lib = client.post("/api/libraries", json={"name": "T", "path": str(folder)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = client.get(f"/api/libraries/{lib}/browse").json()["items"][0]["id"]
    plan = client.get(f"/api/items/{video}/plan", params={"hls_support": "mse"}).json()
    uris = segment_uris(client.get(plan["url"]).text)
    return video, folder / "old.avi", [f"/api/items/{video}/{u}" for u in uris]


@requires_ffmpeg
def test_expired_session_is_made_again_from_the_segment_url(client, hls_video):
    """A long pause: the session was dropped, and the player just carries on."""
    _, _, segments = hls_video
    assert client.get(segments[0]).status_code == 200
    manager = client.app.state.hls
    assert client.portal.call(manager.remove_idle, 0) == 1 and not manager.sessions
    assert client.get(segments[1]).status_code == 200
    assert len(manager.sessions) == 1


@requires_ffmpeg
def test_file_replaced_while_playing_answers_gone(client, hls_video, long_clip):
    import os
    _, path, segments = hls_video
    assert client.get(segments[0]).status_code == 200
    manager = client.app.state.hls
    (session,) = manager.sessions.values()
    client.portal.call(forget_from, manager, session, 6)
    path.write_bytes(long_clip.read_bytes()[:-5000])
    os.utime(path, ns=(1, 1))
    res = client.get(segments[6])                    # not encoded yet: the file is checked
    assert res.status_code == 410 and "changed" in res.json()["detail"]
    assert client.portal.call(manager.remove_idle, 0) >= 0
    assert client.get(segments[6]).status_code == 410   # a dropped session of the old file: still gone


@requires_ffmpeg
def test_segment_for_a_bad_viewer_id_is_refused(client, hls_video):
    _, _, segments = hls_video
    parts = segments[0].split("/")
    parts[-2] = "not-a-viewer"
    assert client.get("/".join(parts)).status_code == 404


# ---- Overlapping encoders, refreshed facts, the cache target --------------------------------


def test_publishing_keeps_the_first_copy_and_never_overwrites(tmp_path):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", facts(path=tmp_path / "x.avi")))
        a, b = s.folder / "enc-a-1", s.folder / "enc-b-2"
        a.mkdir(), b.mkdir()
        (a / "3.ts").write_bytes(b"from encoder a")
        first = m.publish(s)
        (b / "3.ts").write_bytes(b"from encoder b")        # the same segment, finished later
        (b / "4.ts.tmp").write_bytes(b"half written")       # not finished: not published
        m.publish(s)
        result = ((s.folder / "3.ts").read_bytes(), first, sorted(p.name for p in s.folder.glob("*.ts")),
                  list(a.iterdir()), sorted(p.name for p in b.iterdir()))
        await m.shutdown()
        return result

    content, count, shared, left_a, left_b = run(scenario())
    assert content == b"from encoder a" and count == 1 and shared == ["3.ts"]
    assert left_a == [] and left_b == ["4.ts.tmp"]           # the losing copy is dropped


@requires_ffmpeg
def test_overlapping_encoders_publish_whole_segments(tmp_path, long_clip, monkeypatch):
    """Two viewers' encoders cover the same segments: every published segment is
    complete and in the right place, and nothing is left half-written."""
    monkeypatch.setattr(hls, "LOOKAHEAD", 0)                 # so the second viewer starts its own
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", source(long_clip)))
        await asyncio.gather(m.media_segment(s, V, 0), m.media_segment(s, V2, 2))
        both_ran = s.starts == 2
        for n in range(1, 7):                                # both keep asking, overlapping
            await asyncio.gather(m.media_segment(s, V, n), m.media_segment(s, V2, min(n + 2, 6)))
        for viewer in list(s.viewers.values()):
            await m._stop(viewer)
        names = sorted(p.name for p in s.folder.iterdir())
        checks = [(codecs(s.segment(n)), first_pts(s.segment(n))) for n in range(7)]
        await m.shutdown()
        return both_ran, names, checks

    both_ran, names, checks = run(scenario())
    assert both_ran
    assert names == [f"{n}.ts" for n in sorted(range(7), key=str)]  # no staging or temp files left
    for n, (names_, start) in enumerate(checks):
        assert names_ == ["h264", "aac"]
        assert start == pytest.approx(n * 6 + 1.4, abs=0.3)


@requires_ffmpeg
def test_reopening_with_changed_facts_starts_afresh(tmp_path, long_clip):
    """A rescan re-probed the file (same size and time) and its facts changed."""
    m = manager(tmp_path)

    async def scenario():
        old = m.get(await m.open("vid", source(long_clip)))
        await m.media_segment(old, V, 0)
        proc = old.viewers[V].encoder.proc
        changed = Source(long_clip, CONVERT, 39.0, True, 240, "mp3", hls.revision(long_clip))
        new_sid = await m.open("vid", changed)
        result = (new_sid != old.sid, old.retired, old.folder.exists(), proc.returncode is not None,
                  m.get(new_sid).source.interlaced, m.get(new_sid).count)
        await m.shutdown()
        return result

    assert run(scenario()) == (True, True, False, True, True, 7)


def test_other_plans_of_the_same_file_version_are_kept(tmp_path):
    """Two browsers (different plans) of the same video share nothing, and neither retires the other."""
    m = manager(tmp_path)

    async def scenario():
        a = await m.open("vid", facts(path=tmp_path / "x.avi"))
        b = await m.open("vid", facts(path=tmp_path / "x.avi", plan=Plan("transcode", "encode", "copy")))
        result = (a != b, sorted(m.sessions) == sorted([a, b]))
        await m.shutdown()
        return result

    assert run(scenario()) == (True, True)


def test_cache_size_counts_unpublished_and_unfinished_files(tmp_path):
    m = manager(tmp_path)

    async def scenario():
        s = m.get(await m.open("vid", facts(path=tmp_path / "x.avi")))
        (s.folder / "0.ts").write_bytes(b"x" * 1000)
        (s.folder / "enc-a-1").mkdir()
        (s.folder / "enc-a-1" / "1.ts").write_bytes(b"x" * 300)       # finished, not yet published
        (s.folder / "enc-a-1" / "2.ts.tmp").write_bytes(b"x" * 200)   # still being written
        size = m.cache_size()
        await m.shutdown()
        return size

    assert run(scenario()) == 1500


def test_cache_target_is_soft_for_what_viewers_need(tmp_path, monkeypatch, caplog):
    """Over the target, only segments a viewer is near remain, and it says so once."""
    import logging
    monkeypatch.setattr(hls, "KEEP_NEAR", 3)
    m = manager(tmp_path)
    m.cache_limit = 1000

    async def scenario():
        s = m.get(await m.open("vid", facts(path=tmp_path / "x.avi")))
        s.viewers[V] = hls.Viewer(V, position=4)
        for n in range(8):
            s.segment(n).write_bytes(b"x" * 1000)
        with caplog.at_level(logging.WARNING, logger="reel.hls"):
            await m.enforce_cache_limit()
            await m.enforce_cache_limit()                             # still over: no second warning
        left = sorted(int(p.stem) for p in s.folder.glob("*.ts"))
        await m.shutdown()
        return left

    assert run(scenario()) == [3, 4, 5]                               # position - 1 .. KEEP_NEAR
    warnings = [r for r in caplog.records if "over REEL_HLS_CACHE_MB" in r.getMessage()]
    assert len(warnings) == 1


def test_cache_file_work_runs_off_the_event_loop_and_status_doesnt_walk(tmp_path, monkeypatch):
    import threading

    m = manager(tmp_path)
    threads = {}
    real_evict = hls._evict

    def watched_evict(*args):
        threads["evict"] = threading.get_ident()
        return real_evict(*args)

    monkeypatch.setattr(hls, "_evict", watched_evict)

    async def scenario():
        threads["loop"] = threading.get_ident()
        s = m.get(await m.open("vid", facts(path=tmp_path / "x.avi")))
        s.segment(0).write_bytes(b"x" * 2048 * 1024)
        before = m.status()["cache_mb"]                     # nothing measured yet
        await m.enforce_cache_limit()
        monkeypatch.setattr(hls, "_folder_size", lambda folder: 1 / 0)   # status must not walk
        after = m.status()["cache_mb"]
        await m.shutdown()
        return before, after

    before, after = run(scenario())
    assert (before, after) == (0.0, 2.0)
    assert threads["evict"] != threads["loop"]


def test_retiring_a_session_deletes_its_files_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    m = manager(tmp_path)
    threads = {}
    real = hls.shutil.rmtree

    def watched(path, **kwargs):
        threads.setdefault("rmtree", threading.get_ident())
        return real(path, **kwargs)

    monkeypatch.setattr(hls.shutil, "rmtree", watched)

    async def scenario():
        threads["loop"] = threading.get_ident()
        s = m.get(await m.open("vid", facts(path=tmp_path / "x.avi")))
        s.segment(0).write_bytes(b"x")
        await m.retire(s)
        return s.folder.exists()

    assert run(scenario()) is False
    assert threads["rmtree"] != threads["loop"]


def test_a_session_reopened_while_the_old_one_is_being_deleted_keeps_its_files(tmp_path, monkeypatch):
    """Retiring deletes files in a worker thread; a request in that moment opens the
    same session id again. The deletion must not take the new session's files."""
    import threading

    m = manager(tmp_path)
    deleting, finish = threading.Event(), threading.Event()
    real = hls.shutil.rmtree

    def slow_rmtree(path, **kwargs):
        deleting.set()
        finish.wait(5)
        return real(path, **kwargs)

    monkeypatch.setattr(hls.shutil, "rmtree", slow_rmtree)

    async def scenario():
        src = facts(path=tmp_path / "x.avi")
        old = m.get(await m.open("vid", src))
        retiring = asyncio.create_task(m.retire(old))
        while not deleting.is_set():
            await asyncio.sleep(0.01)
        new = m.get(await m.open("vid", src))             # same id, opened during the delete
        new.segment(0).write_bytes(b"segment")
        finish.set()
        await retiring
        result = (new.sid == old.sid, new.folder != old.folder, new.segment(0).exists(),
                  m.get(new.sid) is new, old.folder.exists())
        await m.shutdown()
        return result

    assert run(scenario()) == (True, True, True, True, False)
