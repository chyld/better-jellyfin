"""Deciding how to play a video, when Play is pressed.

The scanner stores facts about each file (container, codecs, pixel format,
interlacing). Nothing about *how* to play it is stored: that depends on the
viewer's browser and on these rules, which can change. plan() combines the two.

Video and audio are decided separately, so a file whose video the browser can
play but whose audio it can't (H.264 with AC-3 or Vorbis in an MKV, say) gets
its video copied untouched and only its audio converted, instead of a full
re-encode.

The plan also says how the video is delivered, because that changes what has
to happen to the tracks: HLS segments must start on exact keyframes, so HLS
always encodes the video, and its MPEG-TS segments can only carry some audio.

Modes (what happens to the tracks):
    direct       send the file as it is (range requests, native seeking)
    remux        copy both tracks into a fresh MP4 container
    audio        copy the video, convert the audio
    transcode    convert the video (audio copied if it can be, else converted)
    unsupported  no playable video

Deliveries:
    file         the original file
    progressive  one fragmented MP4 stream, restarted at the new time on a seek
    hls          6-second MPEG-TS segments, encoded on demand (see hls.py)
"""
from dataclasses import dataclass, replace

# What any current browser plays. A browser can report more (see caps.js), e.g.
# HEVC on Safari or with hardware support, or AC-3 on Safari and Edge.
BASELINE_VIDEO = frozenset({"h264", "vp8", "vp9", "av1"})
BASELINE_AUDIO = frozenset({"aac", "mp3", "opus", "vorbis", "flac"})
# Everything a browser may report, so a request can't name arbitrary codecs.
KNOWN_VIDEO = BASELINE_VIDEO | {"hevc"}
KNOWN_AUDIO = BASELINE_AUDIO | {"ac3", "eac3"}

# What ffmpeg can copy into the fragmented MP4 we stream.
MP4_VIDEO = frozenset({"h264", "hevc", "vp9", "av1"})
MP4_AUDIO = frozenset({"aac", "mp3", "opus", "flac", "ac3", "eac3"})
# Browsers only decode 8-bit 4:2:0 H.264 (not 10-bit "Hi10P").
H264_PIX_FMTS = frozenset({"yuv420p", "yuvj420p"})
MP4_FAMILY_EXTENSIONS = frozenset({"mp4", "m4v", "mov"})
# Audio that can be copied into MPEG-TS segments and still play: hls.js handles
# AAC and MP3 there; Safari's own player also AC-3 and E-AC-3. (TS can't carry
# FLAC at all, and Opus or Vorbis in TS don't play in browsers.)
TS_AUDIO = frozenset({"aac", "mp3"})
TS_AUDIO_NATIVE = TS_AUDIO | {"ac3", "eac3"}
HLS_SUPPORT = ("none", "mse", "native")


@dataclass(frozen=True)
class Capabilities:
    """The codecs the viewer's browser says it can decode."""
    video: frozenset[str] = BASELINE_VIDEO
    audio: frozenset[str] = BASELINE_AUDIO

    @classmethod
    def from_query(cls, video: str | None, audio: str | None) -> "Capabilities":
        """From "?video=h264,hevc&audio=aac,ac3": known names only.

        A missing parameter means "a typical browser" (the baseline); an empty one
        means the browser reported it can decode none of them.
        """
        def parse(value, known, baseline):
            if value is None:
                return baseline
            return frozenset(v for v in value.lower().split(",") if v in known)
        return cls(parse(video, KNOWN_VIDEO, BASELINE_VIDEO), parse(audio, KNOWN_AUDIO, BASELINE_AUDIO))


BASELINE = Capabilities()


@dataclass(frozen=True)
class Plan:
    mode: str                    # direct | remux | audio | transcode | unsupported
    video: str | None = None     # copy | encode   (streams only)
    audio: str | None = None     # copy | encode | None (no audio track)
    delivery: str | None = None  # file | progressive | hls | None (unsupported)
    note: str | None = None      # why more work is done than the codecs alone need

    @property
    def streamed(self) -> bool:
        return self.mode in ("remux", "audio", "transcode")


def plan(facts, caps: Capabilities = BASELINE, hls_support: str = "none") -> Plan:
    """How to get this video into this browser, doing as little work as possible.

    `facts` is anything with the probed fields (a database row or a dict):
    container, video_codec, audio_codec, pix_fmt, interlaced, probe_error,
    rel_path and (for HLS) duration. `hls_support` is how the browser plays
    HLS: none, mse (hls.js) or native (Safari).
    """
    tracks = _tracks(facts, caps)
    if tracks.mode == "unsupported":
        return tracks
    if tracks.mode == "direct":
        return replace(tracks, delivery="file")
    # Converted video goes out as HLS (exact segments, cheap seeking) where the
    # browser can play it; Safari can't play the progressive stream at all, so it
    # gets HLS for everything streamed. HLS needs the length to list its segments.
    native = hls_support == "native"
    wants_hls = native or (hls_support == "mse" and tracks.video == "encode")
    if not (wants_hls and _duration(facts)):
        return replace(tracks, delivery="progressive")

    acodec = facts["audio_codec"]
    ts_audio = TS_AUDIO_NATIVE if native else TS_AUDIO
    audio = None if acodec is None else ("copy" if acodec in caps.audio and acodec in ts_audio else "encode")
    notes = []
    if tracks.video == "copy":
        notes.append("This browser plays streams only as HLS, which needs the video re-encoded.")
    if tracks.audio == "copy" and audio == "encode":
        notes.append(f"{acodec.upper()} audio can't be sent in HLS segments, so it's converted.")
    return Plan("transcode", video="encode", audio=audio, delivery="hls", note=" ".join(notes) or None)


def _duration(facts) -> float | None:
    try:
        return facts["duration"]
    except (KeyError, IndexError):
        return None


def _tracks(facts, caps: Capabilities) -> Plan:
    """What each track needs, for the file or the progressive MP4 stream."""
    vcodec, acodec = facts["video_codec"], facts["audio_codec"]
    if facts["probe_error"] or not vcodec:
        return Plan("unsupported")

    ext = facts["rel_path"].rsplit(".", 1)[-1].lower() if "." in facts["rel_path"] else ""
    interlaced = bool(facts["interlaced"])
    # Interlaced video shows combing unless it's deinterlaced, which means converting it.
    video_plays = (
        vcodec in caps.video
        and (vcodec != "h264" or facts["pix_fmt"] in H264_PIX_FMTS)
        and not interlaced
    )
    audio_plays = acodec is None or acodec in caps.audio

    # The file as it is, when the browser can open the container and both tracks.
    containers = set((facts["container"] or "").split(","))
    if video_plays and audio_plays:
        if containers & {"mp4", "mov"} and ext in MP4_FAMILY_EXTENSIONS:
            return Plan("direct")
        if ext == "webm" and vcodec in ("vp8", "vp9", "av1") and acodec in (None, "opus", "vorbis"):
            return Plan("direct")

    audio_copy = acodec is not None and acodec in caps.audio and acodec in MP4_AUDIO
    audio = None if acodec is None else ("copy" if audio_copy else "encode")

    if video_plays and vcodec in MP4_VIDEO:
        return Plan("remux" if audio in (None, "copy") else "audio", video="copy", audio=audio)
    return Plan("transcode", video="encode", audio=audio)
