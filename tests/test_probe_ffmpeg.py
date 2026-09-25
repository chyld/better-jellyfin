"""Real ffprobe runs against tiny clips made with ffmpeg."""
import pytest

from reel.libraries import create_library
from reel.probe import ProbeError, classify, probe
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
        ("h264_ac3.mkv", "h264", "ac3", "transcode"),
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
    assert classify(result, (clips / name).suffix) == mode


def test_probe_detects_interlacing(clips):
    assert probe(clips / "vhs_interlaced.mpg").interlaced is True
    assert probe(clips / "h264_aac.mp4").interlaced is False


def test_probe_corrupt_file_raises(clips):
    with pytest.raises(ProbeError):
        probe(clips / "corrupt.mp4")


def test_full_scan_with_real_ffprobe(conn, media_root, clips):
    for clip in clips.iterdir():
        (media_root / clip.name).symlink_to(clip)
    lib = create_library(conn, media_root, "Clips", str(media_root))
    result = scan_library(conn, lib, workers=4)
    assert result["total"] == len(list(clips.iterdir()))
    assert result["failed"] == 1  # corrupt.mp4
    assert result["total"] == 13
    modes = dict(conn.execute("SELECT rel_path, play_mode FROM media_items").fetchall())
    assert modes["h264_aac.mp4"] == "direct"
    assert modes["h264_aac.mkv"] == "remux"
    assert modes["vhs_interlaced.mpg"] == "transcode"
    assert modes["corrupt.mp4"] == "unsupported"
