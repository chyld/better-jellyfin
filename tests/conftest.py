import shutil
import subprocess
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reel.config import Settings
from reel.db import connect, init_db
from reel.main import create_app
from reel.probe import ProbeResult
from reel.scan_manager import ScanManager
from reel.scanner import scan_library


def make_files(root: Path, *paths: str) -> None:
    """Create empty files (and their folders) under root."""
    for rel in paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


class FakeProbe:
    """Stands in for ffprobe: guesses codecs from the extension and records calls."""

    PROFILES = {
        ".mp4": ProbeResult("mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac", "yuv420p", 1920, 1080, 60.0),
        ".mov": ProbeResult("mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac", "yuv420p", 1280, 720, 60.0),
        ".mkv": ProbeResult("matroska,webm", "h264", "aac", "yuv420p", 1920, 1080, 60.0),
        ".avi": ProbeResult("avi", "mpeg4", "mp3", "yuv420p", 640, 480, 60.0),
        ".wmv": ProbeResult("asf", "wmv2", "wmav2", "yuv420p", 320, 240, 60.0),
        ".asf": ProbeResult("asf", "wmv2", "wmav2", "yuv420p", 320, 240, 60.0),
        ".mpg": ProbeResult("mpeg", "mpeg2video", "mp2", "yuv420p", 720, 480, 60.0, interlaced=True),
    }

    def __init__(self):
        self.calls: list[Path] = []
        self.fail: set[str] = set()  # file names that should fail to probe

    def __call__(self, path: Path) -> ProbeResult:
        self.calls.append(path)
        if path.name in self.fail:
            from reel.probe import ProbeError
            raise ProbeError("Invalid data found when processing input")
        return self.PROFILES[path.suffix.lower()]


@pytest.fixture
def media_root(tmp_path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
def settings(tmp_path, media_root) -> Settings:
    return Settings(media_root=media_root.resolve(), data_dir=tmp_path / "data", probe_workers=2)


@pytest.fixture
def conn(settings):
    init_db(settings.db_path)
    c = connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def fake_probe() -> FakeProbe:
    return FakeProbe()


@pytest.fixture
def client(settings, fake_probe):
    """API client whose scans use the fake probe instead of ffprobe."""
    init_db(settings.db_path)
    manager = ScanManager(settings.db_path, workers=2, scan_fn=partial(scan_library, probe_fn=fake_probe))
    app = create_app(settings, manager)
    with TestClient(app) as c:
        c.scans = manager
        yield c


# ---- Real media clips (made with ffmpeg) -------------------------------------

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)

# name -> ffmpeg output arguments. Each clip is one second of test pattern + tone.
CLIPS = {
    "h264_aac.mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "h264_aac.mkv": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "h264_ac3.mkv": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "ac3"],
    "h264_10bit.mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p10le", "-c:a", "aac"],
    "hevc.mp4": ["-c:v", "libx265", "-pix_fmt", "yuv420p", "-c:a", "aac", "-x265-params", "log-level=none"],
    "vp9_opus.webm": ["-c:v", "libvpx-vp9", "-c:a", "libopus"],
    "xvid_mp3.avi": ["-c:v", "mpeg4", "-c:a", "libmp3lame"],
    "wmv.wmv": ["-c:v", "wmv2", "-c:a", "wmav2"],
    "vhs_interlaced.mpg": ["-vf", "setfield=tff", "-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-c:a", "mp2"],
    "silent.mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-an"],
    # Like the real library: an MPEG transport stream with a .mp4 name (ADTS AAC audio).
    "transport_stream.mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts"],
}


@pytest.fixture(scope="session")
def clips(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("clips")
    for name, args in CLIPS.items():
        inputs = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=1"]
        if "-an" not in args:
            inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *inputs, *args, "-shortest", str(folder / name)],
            check=True,
        )
    # Like some old WMV files: cover art stored as the *first* video stream.
    cover = folder / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120",
                    "-frames:v", "1", str(cover)], check=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(cover), "-i", str(folder / "hevc.mp4"),
         "-map", "0", "-map", "1", "-c", "copy", "-disposition:v:0", "attached_pic",
         str(folder / "cover_first.mp4")],
        check=True,
    )
    cover.unlink()
    (folder / "corrupt.mp4").write_bytes(b"this is not a video" * 100)
    return folder
