"""Real ffprobe runs against tiny clips made with ffmpeg."""
import shutil
import pytest

from reel.libraries import create_library
from reel.plan import plan
from reel.probe import ProbeError, probe
from reel.scanner import scan_library

from conftest import requires_ffmpeg

pytestmark = [requires_ffmpeg, pytest.mark.ffmpeg]


@pytest.mark.parametrize(
    "name, video, audio, mode",
    [
        ("h264_aac.mp4", "h264", "aac", "direct"),
        ("silent.mp4", "h264", None, "direct"),
        ("vp9_opus.webm", "vp9", "opus", "direct"),
        ("h264_aac.mkv", "h264", "aac", "remux"),
        ("h264_ac3.mkv", "h264", "ac3", "audio"),       # only the audio needs converting
        ("h264_10bit.mp4", "h264", "aac", "transcode"),
        ("hevc.mp4", "hevc", "aac", "transcode"),
        ("xvid_mp3.avi", "mpeg4", "mp3", "transcode"),
        ("wmv.wmv", "wmv2", "wmav2", "transcode"),
        ("vhs_interlaced.mpg", "mpeg2video", "mp2", "transcode"),
        ("transport_stream.mp4", "h264", "aac", "remux"),
        ("cover_first.mp4", "hevc", "aac", "transcode"),
    ],
)
def test_probe_real_clip(clips, name, video, audio, mode):
    result = probe(clips / name)
    assert (result.video_codec, result.audio_codec) == (video, audio)
    assert (result.width, result.height) == (320, 240)
    assert result.duration == pytest.approx(1.0, abs=0.15)
    facts = {"container": result.container, "video_codec": result.video_codec, "audio_codec": result.audio_codec,
             "pix_fmt": result.pix_fmt, "interlaced": result.interlaced, "probe_error": result.error, "rel_path": name}
    assert plan(facts).mode == mode


def test_probe_detects_interlacing(clips):
    assert probe(clips / "vhs_interlaced.mpg").interlaced is True
    assert probe(clips / "h264_aac.mp4").interlaced is False


def test_probe_corrupt_file_raises(clips):
    with pytest.raises(ProbeError):
        probe(clips / "corrupt.mp4")


def test_full_scan_with_real_ffprobe(conn, media_root, clips):
    for clip in clips.iterdir():
        shutil.copyfile(clip, media_root / clip.name)
    lib = create_library(conn, media_root, "Clips", str(media_root))
    result = scan_library(conn, lib, workers=4)
    assert result["total"] == len(list(clips.iterdir()))
    assert result["failed"] == 1  # corrupt.mp4
    assert result["total"] == 13
    modes = {r["rel_path"]: plan(r).mode for r in conn.execute("SELECT * FROM media_items")}
    assert modes["h264_aac.mp4"] == "direct"
    assert modes["h264_aac.mkv"] == "remux"
    assert modes["vhs_interlaced.mpg"] == "transcode"
    assert modes["corrupt.mp4"] == "unsupported"


def test_a_running_ffprobe_can_be_stopped(tmp_path):
    """A probe stuck on a file that never answers ends when its supervisor stops."""
    import os
    import threading
    import time

    from reel.probe import ProbeError, ProbeSupervisor

    probes = ProbeSupervisor()
    stuck = tmp_path / "stuck.mkv"
    os.mkfifo(stuck)                                   # ffprobe waits on it forever
    outcome = {}

    def run():
        try:
            probes.probe(stuck)
        except ProbeError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(50):
        if probes.stop():
            break
        time.sleep(0.1)
    thread.join(5)
    assert not thread.is_alive() and outcome == {"error": "ffprobe was stopped"}


def test_no_probe_starts_once_stopped(clips, monkeypatch):
    """A probe asked for just after the stop (say, right after a fingerprint) never starts."""
    import subprocess

    from reel import probe as probe_module
    from reel.probe import ProbeError, ProbeSupervisor

    started = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(probe_module.subprocess, "Popen", lambda *a, **k: started.append(a) or real_popen(*a, **k))
    probes = ProbeSupervisor()
    probes.stop()
    with pytest.raises(ProbeError, match="stopped"):
        probes.probe(clips / "h264_aac.mp4")
    assert started == []
    probes.allow()
    assert probes.probe(clips / "h264_aac.mp4").video_codec == "h264" and len(started) == 1


def test_stopping_one_supervisor_leaves_others_alone(clips):
    """Two apps in one process (tests): stopping one's scans doesn't stop the other's probes."""
    from reel.probe import ProbeSupervisor, probe

    stopped, other = ProbeSupervisor(), ProbeSupervisor()
    stopped.stop()
    assert other.probe(clips / "h264_aac.mp4").video_codec == "h264"
    assert probe(clips / "h264_aac.mp4").video_codec == "h264"
