"""How ffprobe output is read and turned into a play mode (no ffmpeg needed)."""
import pytest

from reel.probe import ProbeResult, classify, parse_probe


def test_parse_probe_reads_streams_and_skips_cover_art():
    data = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
             "width": 1920, "height": 1080, "field_order": "progressive", "disposition": {}},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    r = parse_probe(data)
    assert (r.container, r.video_codec, r.audio_codec) == ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac")
    assert (r.width, r.height, r.duration, r.pix_fmt) == (1920, 1080, 12.5, "yuv420p")
    assert r.interlaced is False


@pytest.mark.parametrize("field_order, interlaced", [("tt", True), ("bb", True), ("progressive", False), (None, False)])
def test_parse_probe_interlacing(field_order, interlaced):
    stream = {"codec_type": "video", "codec_name": "mpeg2video"}
    if field_order:
        stream["field_order"] = field_order
    assert parse_probe({"format": {}, "streams": [stream]}).interlaced is interlaced


def test_parse_probe_audio_only():
    r = parse_probe({"format": {"format_name": "mp3"}, "streams": [{"codec_type": "audio", "codec_name": "mp3"}]})
    assert r.video_codec is None and r.audio_codec == "mp3"


MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


@pytest.mark.parametrize(
    "result, ext, mode",
    [
        (ProbeResult(MP4, "h264", "aac", "yuv420p"), ".mp4", "direct"),
        (ProbeResult(MP4, "h264", "aac", "yuv420p"), ".mov", "direct"),
        (ProbeResult(MP4, "h264", None, "yuv420p"), ".mp4", "direct"),
        (ProbeResult(MP4, "av1", "opus", "yuv420p"), ".mp4", "direct"),
        (ProbeResult("matroska,webm", "vp9", "opus", "yuv420p"), ".webm", "direct"),
        (ProbeResult("matroska,webm", "h264", "aac", "yuv420p"), ".mkv", "remux"),
        (ProbeResult("matroska,webm", "h264", "mp3", "yuv420p"), ".mkv", "remux"),
        (ProbeResult("mpegts", "h264", "aac", "yuv420p"), ".ts", "remux"),
        (ProbeResult("matroska,webm", "h264", "ac3", "yuv420p"), ".mkv", "transcode"),
        (ProbeResult("matroska,webm", "vp8", "vorbis", "yuv420p"), ".mkv", "transcode"),
        (ProbeResult(MP4, "h264", "aac", "yuv420p10le"), ".mp4", "transcode"),
        (ProbeResult(MP4, "hevc", "aac", "yuv420p"), ".mp4", "transcode"),
        (ProbeResult("avi", "mpeg4", "mp3", "yuv420p"), ".avi", "transcode"),
        (ProbeResult("asf", "wmv2", "wmav2", "yuv420p"), ".wmv", "transcode"),
        (ProbeResult("mpeg", "mpeg2video", "mp2", "yuv420p"), ".mpg", "transcode"),
        (ProbeResult(MP4, "h264", "aac", "yuv420p", interlaced=True), ".mp4", "transcode"),
        (ProbeResult(error="bad file"), ".mp4", "unsupported"),
        (ProbeResult("mp3", None, "mp3"), ".mp4", "unsupported"),
    ],
)
def test_classify(result, ext, mode):
    assert classify(result, ext) == mode
