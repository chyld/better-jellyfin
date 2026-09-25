"""Deciding how to play a video, from its facts and the browser's capabilities."""
import pytest

from reel.plan import BASELINE, Capabilities, Plan, plan

MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


def facts(container, video, audio, pix_fmt="yuv420p", *, path="v.mp4", interlaced=False, error=None):
    return {"container": container, "video_codec": video, "audio_codec": audio, "pix_fmt": pix_fmt,
            "interlaced": int(interlaced), "probe_error": error, "rel_path": path}


HEVC = Capabilities(video=frozenset({"h264", "hevc", "vp9", "av1"}), audio=BASELINE.audio)
AC3 = Capabilities(video=BASELINE.video, audio=BASELINE.audio | {"ac3", "eac3"})


@pytest.mark.parametrize(
    "f, caps, expected",
    [
        # Straight from the file.
        (facts(MP4, "h264", "aac"), BASELINE, Plan("direct")),
        (facts(MP4, "h264", "aac", path="v.mov"), BASELINE, Plan("direct")),
        (facts(MP4, "h264", None), BASELINE, Plan("direct")),
        (facts(MP4, "av1", "opus"), BASELINE, Plan("direct")),
        (facts("matroska,webm", "vp9", "opus", path="v.webm"), BASELINE, Plan("direct")),
        # Right tracks, wrong container: copy both.
        (facts("matroska,webm", "h264", "aac", path="v.mkv"), BASELINE, Plan("remux", "copy", "copy")),
        (facts("mpegts", "h264", "aac", path="v.mp4"), BASELINE, Plan("remux", "copy", "copy")),
        (facts("matroska,webm", "h264", None, path="v.mkv"), BASELINE, Plan("remux", "copy", None)),
        # Video fine, audio not: copy the video, convert only the audio.
        (facts("matroska,webm", "h264", "ac3", path="v.mkv"), BASELINE, Plan("audio", "copy", "encode")),
        (facts("matroska,webm", "h264", "vorbis", path="v.mkv"), BASELINE, Plan("audio", "copy", "encode")),
        (facts(MP4, "h264", "ac3"), BASELINE, Plan("audio", "copy", "encode")),
        (facts("matroska,webm", "h264", "dts", path="v.mkv"), BASELINE, Plan("audio", "copy", "encode")),
        # ...unless the browser plays that audio too.
        (facts(MP4, "h264", "ac3"), AC3, Plan("direct")),
        (facts("matroska,webm", "h264", "ac3", path="v.mkv"), AC3, Plan("remux", "copy", "copy")),
        # Video the browser can't play: convert it (copy the audio when possible).
        (facts("avi", "mpeg4", "mp3", path="v.avi"), BASELINE, Plan("transcode", "encode", "copy")),
        (facts("asf", "wmv2", "wmav2", path="v.wmv"), BASELINE, Plan("transcode", "encode", "encode")),
        (facts("mpeg", "mpeg2video", "mp2", path="v.mpg"), BASELINE, Plan("transcode", "encode", "encode")),
        (facts(MP4, "h264", "aac", "yuv420p10le"), BASELINE, Plan("transcode", "encode", "copy")),
        (facts(MP4, "h264", "aac", interlaced=True), BASELINE, Plan("transcode", "encode", "copy")),
        (facts("matroska,webm", "vp8", "vorbis", path="v.mkv"), BASELINE, Plan("transcode", "encode", "encode")),
        # HEVC: converted unless the browser says it can play it.
        (facts(MP4, "hevc", "aac"), BASELINE, Plan("transcode", "encode", "copy")),
        (facts(MP4, "hevc", "aac"), HEVC, Plan("direct")),
        (facts("matroska,webm", "hevc", "aac", path="v.mkv"), HEVC, Plan("remux", "copy", "copy")),
        # Nothing to play.
        (facts(None, None, None, error="Invalid data"), BASELINE, Plan("unsupported")),
        (facts("mp3", None, "mp3"), BASELINE, Plan("unsupported")),
    ],
)
def test_plan(f, caps, expected):
    assert plan(f, caps) == expected


def test_streamed():
    assert not Plan("direct").streamed and not Plan("unsupported").streamed
    assert all(Plan(m).streamed for m in ("remux", "audio", "transcode"))


def test_capabilities_from_the_query():
    caps = Capabilities.from_query("h264,HEVC,made-up", "aac,ac3")
    assert caps.video == {"h264", "hevc"}            # unknown names are ignored
    assert caps.audio == {"aac", "ac3"}
    assert Capabilities.from_query(None, None) == BASELINE
    assert Capabilities.from_query(None, "aac").video == BASELINE.video


def test_empty_capability_lists_mean_none():
    caps = Capabilities.from_query("", "")
    assert caps.video == frozenset() and caps.audio == frozenset()
    assert Capabilities.from_query("made-up", "").audio == frozenset()


def test_browser_without_the_audio_codec_gets_it_converted():
    mp4 = {"container": "mov,mp4,m4a,3gp,3g2,mj2", "video_codec": "h264", "audio_codec": "aac",
           "pix_fmt": "yuv420p", "interlaced": 0, "probe_error": None, "rel_path": "clip.mp4"}
    assert plan(mp4, Capabilities.from_query("h264", "")) == Plan("audio", video="copy", audio="encode")
