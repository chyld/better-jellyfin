"""Playing videos: original files, remuxed and transcoded streams."""
import asyncio
import subprocess
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reel import playback
from reel.db import init_db
from reel.main import create_app
from reel.plan import Plan
from reel.playback import StreamManager, direct_content_type, stream_command

REMUX = Plan("remux", "copy", "copy")
CONVERT = Plan("transcode", "encode", "encode")
AUDIO_ONLY = Plan("audio", "copy", "encode")
from reel.probe import probe
from reel.scan_manager import ScanManager
from reel.scanner import scan_library

from conftest import requires_ffmpeg

# ---- ffmpeg commands (no ffmpeg needed) ---------------------------------------


def test_audio_only_conversion_copies_the_video():
    cmd = stream_command(Path("/m/a.mkv"), AUDIO_ONLY, audio_codec="ac3")
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "libx264" not in cmd and "-vf" not in cmd


def test_converted_video_keeps_compatible_audio():
    cmd = stream_command(Path("/m/a.avi"), Plan("transcode", "encode", "copy"), audio_codec="mp3")
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-c:a") + 1] == "copy"


@pytest.mark.parametrize("codec", ["ac3", "eac3"])
def test_copied_ac3_delays_the_header(codec):
    """Without delay_moov ffmpeg refuses to copy (E-)AC-3 into a streamed MP4."""
    cmd = stream_command(Path("/m/a.mkv"), REMUX, audio_codec=codec)
    assert "frag_keyframe+empty_moov+default_base_moof+delay_moov" in cmd
    assert "delay_moov" not in " ".join(stream_command(Path("/m/a.mkv"), REMUX, audio_codec="aac"))


def test_no_audio_track():
    cmd = stream_command(Path("/m/a.mkv"), Plan("remux", "copy", None))
    assert "-c:a" not in cmd


def test_remux_copies_streams():
    cmd = stream_command(Path("/m/a.mkv"), REMUX)
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "copy"
    assert "libx264" not in cmd
    assert "-ss" not in cmd
    assert cmd[-1] == "pipe:1" and "frag_keyframe+empty_moov+default_base_moof" in cmd


def test_seek_goes_before_input():
    cmd = stream_command(Path("/m/a.mkv"), REMUX, start=62.5)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "62.500"


def test_transcode_to_browser_friendly_h264():
    cmd = stream_command(Path("/m/a.avi"), CONVERT)
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert "bwdif" not in cmd[cmd.index("-vf") + 1]


def test_transcode_deinterlaces_and_caps_height():
    vf = (lambda c: c[c.index("-vf") + 1])(stream_command(Path("/m/a.mpg"), CONVERT, interlaced=True, height=2160))
    assert vf.startswith("bwdif")
    assert "scale=-2:1080" in vf
    assert "scale=-2:1080" not in (lambda c: c[c.index("-vf") + 1])(stream_command(Path("/m/a"), CONVERT, height=1080))


def test_audio_is_optional():
    assert "0:a:0?" in stream_command(Path("/m/a.mp4"), REMUX)


def test_cover_art_is_never_the_video():
    cmd = stream_command(Path("/m/a.wmv"), CONVERT)
    assert cmd[cmd.index("-map") + 1] == "0:V:0"


def test_remuxed_aac_is_converted_from_adts():
    assert "aac_adtstoasc" in stream_command(Path("/m/a.mp4"), REMUX, audio_codec="aac")
    # The filter only accepts AAC; other audio is copied untouched.
    assert "aac_adtstoasc" not in stream_command(Path("/m/a.mp4"), REMUX, audio_codec="mp3")
    assert "aac_adtstoasc" not in stream_command(Path("/m/a.mp4"), REMUX, audio_codec=None)


def test_bad_mode():
    with pytest.raises(ValueError):
        stream_command(Path("/m/a.mp4"), Plan("direct"))


@pytest.mark.parametrize(
    "name, content_type",
    [("a.mp4", "video/mp4"), ("A.MOV", "video/mp4"), ("a.m4v", "video/mp4"), ("a.webm", "video/webm"),
     ("a.avi", "application/octet-stream")],
)
def test_direct_content_type(name, content_type):
    assert direct_content_type(name) == content_type


# ---- Real playback -------------------------------------------------------------

@pytest.fixture
def client(settings):
    init_db(settings.db_path)
    manager = ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=probe))
    with TestClient(create_app(settings, manager)) as c:
        c.scans = manager
        yield c


@pytest.fixture
def items(client, media_root, clips):
    """Scan the test clips (copied, so tests can change them) and map name -> item id."""
    folder = media_root / "clips"
    folder.mkdir()
    for clip in clips.iterdir():
        (folder / clip.name).write_bytes(clip.read_bytes())
    lib = client.post("/api/libraries", json={"name": "Clips", "path": str(folder)}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    page = client.get(f"/api/libraries/{lib['id']}/browse").json()
    ids = {}
    for item in page["items"]:
        detail = client.get(f"/api/items/{item['id']}").json()
        ids[detail["rel_path"]] = item["id"]
    return ids


def ffprobe_bytes(data: bytes, tmp_path) -> dict:
    f = tmp_path / "out.mp4"
    f.write_bytes(data)
    r = probe(f)
    return {"container": r.container, "video": r.video_codec, "audio": r.audio_codec,
            "pix_fmt": r.pix_fmt, "duration": r.duration, "interlaced": r.interlaced}


@requires_ffmpeg
def test_direct_file_supports_ranges(client, items, media_root):
    item = items["h264_aac.mp4"]
    whole = (media_root / "clips/h264_aac.mp4").read_bytes()

    res = client.get(f"/api/items/{item}/file")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert res.headers["accept-ranges"] == "bytes"
    assert res.content == whole

    res = client.get(f"/api/items/{item}/file", headers={"Range": "bytes=100-199"})
    assert res.status_code == 206
    assert res.content == whole[100:200]
    assert res.headers["content-range"] == f"bytes 100-199/{len(whole)}"


@requires_ffmpeg
@pytest.mark.parametrize(
    "name, video, audio",
    [
        ("h264_aac.mkv", "h264", "aac"),         # remux: codecs copied
        ("transport_stream.mp4", "h264", "aac"), # remux of MPEG-TS with ADTS audio
        ("cover_first.mp4", "h264", "aac"),      # the real video, not the cover image
        ("xvid_mp3.avi", "h264", "mp3"),         # video converted; MP3 audio copied as is
        ("wmv.wmv", "h264", "aac"),
        ("h264_ac3.mkv", "h264", "aac"),
        ("hevc.mp4", "h264", "aac"),
        ("vhs_interlaced.mpg", "h264", "aac"),
        ("silent.mp4", "h264", None),            # no audio track is fine
    ],
)
def test_stream_is_playable_mp4(client, items, tmp_path, name, video, audio):
    res = client.get(f"/api/items/{items[name]}/stream")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert res.headers["cache-control"] == "no-store"
    out = ffprobe_bytes(res.content, tmp_path)
    assert "mp4" in out["container"]
    assert (out["video"], out["audio"]) == (video, audio)
    assert out["pix_fmt"] == "yuv420p"
    assert out["duration"] == pytest.approx(1.0, abs=0.2)


@requires_ffmpeg
def test_interlaced_video_comes_out_progressive(client, items, tmp_path):
    out = ffprobe_bytes(client.get(f"/api/items/{items['vhs_interlaced.mpg']}/stream").content, tmp_path)
    assert out["interlaced"] is False


@requires_ffmpeg
def test_remux_does_not_reencode(client, items, media_root, tmp_path):
    """Copied streams keep the exact same video packets as the source."""
    res = client.get(f"/api/items/{items['h264_aac.mkv']}/stream")
    (tmp_path / "out.mp4").write_bytes(res.content)

    def video_packets(path):
        return subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True,
        ).stdout.split()

    assert video_packets(tmp_path / "out.mp4") == video_packets(media_root / "clips/h264_aac.mkv")


@requires_ffmpeg
def test_stream_from_a_start_time(client, items, tmp_path):
    res = client.get(f"/api/items/{items['xvid_mp3.avi']}/stream", params={"start": 0.5})
    assert res.status_code == 200
    assert ffprobe_bytes(res.content, tmp_path)["duration"] == pytest.approx(0.5, abs=0.2)


@requires_ffmpeg
@pytest.mark.parametrize("start", [-1, 1.5, 99])
def test_start_outside_video(client, items, start):
    assert client.get(f"/api/items/{items['xvid_mp3.avi']}/stream", params={"start": start}).status_code == 416


@requires_ffmpeg
def test_unplayable_video(client, items):
    assert client.get(f"/api/items/{items['corrupt.mp4']}/stream").status_code == 409


@requires_ffmpeg
def test_file_ffmpeg_cannot_read_gives_clear_error(client, items, media_root):
    # The file changed after the scan and is now garbage.
    (media_root / "clips/h264_aac.mkv").write_bytes(b"garbage" * 1000)
    res = client.get(f"/api/items/{items['h264_aac.mkv']}/stream")
    assert res.status_code == 502
    assert "ffmpeg" in res.json()["detail"]


@requires_ffmpeg
def test_missing_file(client, items, media_root):
    (media_root / "clips/h264_aac.mp4").unlink()
    (media_root / "clips/h264_aac.mkv").unlink()
    assert client.get(f"/api/items/{items['h264_aac.mp4']}/file").status_code == 404
    res = client.get(f"/api/items/{items['h264_aac.mkv']}/stream")
    assert res.status_code == 404
    assert "missing" in res.json()["detail"]


@requires_ffmpeg
def test_cover_first_file_streams_the_real_video(client, items, tmp_path):
    out = client.get(f"/api/items/{items['cover_first.mp4']}/stream").content
    (tmp_path / "cover.mp4").write_bytes(out)
    size = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(tmp_path / "cover.mp4")], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert size == "320,240"  # the video, not the 160x120 cover


@requires_ffmpeg
def test_head_request_for_direct_file(client, items):
    res = client.head(f"/api/items/{items['h264_aac.mp4']}/file")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert int(res.headers["content-length"]) > 0


def test_unknown_item(client):
    assert client.get("/api/items/999/file").status_code == 404
    assert client.get("/api/items/999/stream").status_code == 404


@requires_ffmpeg
def test_ffmpeg_is_killed_when_the_viewer_leaves():
    """Closing the stream early (tab closed, or a seek) must not leave ffmpeg running."""
    # An endless test pattern: ffmpeg would run forever if nobody stopped it.
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
           "-c:v", "libx264", "-preset", "ultrafast", *playback.FMP4_OUTPUT]
    manager = StreamManager()

    async def watch_briefly():
        stream = manager.stream(cmd)
        assert len(await anext(stream)) > 0
        (proc,) = manager.active
        await stream.aclose()
        return proc

    proc = asyncio.run(watch_briefly())
    assert proc.returncode is not None
    assert manager.active == set()


@requires_ffmpeg
def test_ffmpeg_stream_finishes_normally(clips):
    cmd = stream_command(clips / "h264_aac.mkv", REMUX)
    manager = StreamManager()

    async def read_all():
        return b"".join([chunk async for chunk in manager.stream(cmd)])

    assert len(asyncio.run(read_all())) > 1000
    assert manager.active == set()


# ---- Plans through the API ---------------------------------------------------------------


def video_packets(path):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True, check=True,
    ).stdout.split()


@requires_ffmpeg
def test_audio_only_conversion_leaves_the_video_untouched(client, items, media_root, tmp_path):
    """H.264 + AC-3 in MKV: the video is copied packet for packet, the audio becomes AAC."""
    plan = client.get(f"/api/items/{items['h264_ac3.mkv']}/plan").json()
    assert (plan["mode"], plan["video"], plan["audio"]) == ("audio", "copy", "encode")
    res = client.get(plan["url"] + "&start=0")
    out = tmp_path / "out.mp4"
    out.write_bytes(res.content)
    assert ffprobe_bytes(res.content, tmp_path)["audio"] == "aac"
    assert video_packets(out) == video_packets(media_root / "clips/h264_ac3.mkv")


@requires_ffmpeg
def test_browser_that_plays_ac3_gets_it_copied(client, items, tmp_path):
    plan = client.get(f"/api/items/{items['h264_ac3.mkv']}/plan", params={"audio": "aac,ac3"}).json()
    assert (plan["mode"], plan["audio"]) == ("remux", "copy")
    assert "ac3" in plan["url"]  # the stream URL carries the capabilities
    out = ffprobe_bytes(client.get(plan["url"]).content, tmp_path)
    assert (out["video"], out["audio"]) == ("h264", "ac3")


@requires_ffmpeg
def test_hevc_depends_on_the_browser(client, items):
    typical = client.get(f"/api/items/{items['hevc.mp4']}/plan").json()
    assert typical["mode"] == "transcode" and typical["url"].startswith(f"/api/items/{items['hevc.mp4']}/stream?")
    capable = client.get(f"/api/items/{items['hevc.mp4']}/plan", params={"video": "h264,hevc"}).json()
    assert capable == {"mode": "direct", "video": None, "audio": None, "streamed": False,
                       "url": f"/api/items/{items['hevc.mp4']}/file"}


@requires_ffmpeg
def test_plan_for_unplayable_and_unknown(client, items):
    assert client.get(f"/api/items/{items['corrupt.mp4']}/plan").json()["url"] is None
    assert client.get("/api/items/nope/plan").status_code == 404


@requires_ffmpeg
def test_listing_shows_the_mode_for_a_typical_browser(client, items):
    detail = client.get(f"/api/items/{items['h264_ac3.mkv']}").json()
    assert detail["play_mode"] == "audio"


@requires_ffmpeg
def test_direct_file_can_still_be_streamed_as_a_copy(client, items, tmp_path):
    out = ffprobe_bytes(client.get(f"/api/items/{items['h264_aac.mp4']}/stream").content, tmp_path)
    assert (out["video"], out["audio"]) == ("h264", "aac")
