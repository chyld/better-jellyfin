"""Deciding how to play a video, from its facts and the browser's capabilities."""
import pytest

from dataclasses import replace

from reel.plan import BASELINE, Capabilities, Plan, plan

MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


def facts(container, video, audio, pix_fmt="yuv420p", *, path="v.mp4", interlaced=False, error=None,
          duration=60.0):
    return {"container": container, "video_codec": video, "audio_codec": audio, "pix_fmt": pix_fmt,
            "interlaced": int(interlaced), "probe_error": error, "rel_path": path, "duration": duration}


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
    """What happens to the tracks (for a browser without HLS)."""
    assert replace(plan(f, caps), delivery=None) == expected


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
    assert plan(mp4, Capabilities.from_query("h264", "")) == Plan("audio", video="copy", audio="encode",
                                                                  delivery="progressive")


# ---- Delivery ---------------------------------------------------------------------------

MKV_H264_AAC = facts("matroska,webm", "h264", "aac", path="v.mkv")
AVI_MP3 = facts("avi", "mpeg4", "mp3", path="v.avi")


def test_deliveries_without_hls():
    assert plan(facts(MP4, "h264", "aac")).delivery == "file"
    assert plan(MKV_H264_AAC).delivery == "progressive"
    assert plan(AVI_MP3).delivery == "progressive"
    assert plan(facts(MP4, "h264", "aac", error="bad")).delivery is None


def test_hls_js_gets_hls_only_when_the_video_is_encoded_anyway():
    assert plan(MKV_H264_AAC, hls_support="mse") == Plan("remux", "copy", "copy", delivery="progressive")
    assert plan(AVI_MP3, hls_support="mse") == Plan("transcode", "encode", "copy", delivery="hls")
    assert plan(facts(MP4, "h264", "aac"), hls_support="mse").delivery == "file"


def test_safari_hls_says_the_video_is_encoded_and_why():
    p = plan(MKV_H264_AAC, hls_support="native")
    assert (p.mode, p.video, p.audio, p.delivery) == ("transcode", "encode", "copy", "hls")
    assert "re-encoded" in p.note
    assert plan(facts(MP4, "h264", "aac"), hls_support="native").delivery == "file"


@pytest.mark.parametrize("acodec", ["flac", "opus", "vorbis"])
def test_audio_hls_segments_cant_carry_is_converted(acodec):
    f = facts("matroska,webm", "mpeg4", acodec, path="v.mkv")
    progressive = plan(f)
    hls = plan(f, hls_support="mse")
    assert hls.audio == "encode" and hls.delivery == "hls"
    if progressive.audio == "copy":                      # MP4 could carry it, TS can't
        assert acodec.upper() in hls.note


def test_ac3_is_copied_into_hls_only_for_safari():
    f = facts("matroska,webm", "mpeg4", "ac3", path="v.mkv")
    assert plan(f, AC3, hls_support="native").audio == "copy"
    assert plan(f, AC3, hls_support="mse").audio == "encode"


def test_no_audio_track_in_hls():
    assert plan(facts("avi", "mpeg4", None, path="v.avi"), hls_support="mse").audio is None


def test_hls_needs_a_known_length():
    assert plan(facts("avi", "mpeg4", "mp3", path="v.avi", duration=None), hls_support="mse").delivery == "progressive"
    assert plan(facts("avi", "mpeg4", "mp3", path="v.avi", duration=None), hls_support="native").delivery == "progressive"
