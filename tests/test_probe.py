"""How ffprobe output is read (no ffmpeg needed). Play modes: see test_plan.py."""
import pytest

from reel.probe import parse_probe


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
