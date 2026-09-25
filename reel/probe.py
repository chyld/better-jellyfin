"""Read a video's technical details with ffprobe (the facts the catalog stores).

How a video is played is decided later, per browser, by plan.py. Only the first
real video stream and the first audio stream are recorded (the player has no
track picker); recording more is a PROBE_VERSION bump.
"""
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

PROBE_TIMEOUT = 120  # seconds; files live on a NAS, so be generous
# Bump when what's probed or how it's classified changes: rows made by an older
# version are re-probed on the next scan, even if the file itself hasn't changed.
PROBE_VERSION = 1


@dataclass
class ProbeResult:
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    pix_fmt: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    interlaced: bool = False
    error: str | None = None


class ProbeError(Exception):
    pass


class ProbeSupervisor:
    """Runs ffprobe and can stop every probe it started (a scan being stopped).

    Starting and registering a probe happens under the same lock as stopping,
    and once stopped no new probe starts, so none can slip past a shutdown.
    Each ScanManager has its own; `probe()` uses a shared default one.
    """

    def __init__(self):
        self._running: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._stopped = False

    def stop(self) -> int:
        """Kill every probe running now and refuse new ones until allow(). Returns how many."""
        with self._lock:
            self._stopped = True
            procs = list(self._running)
            for proc in procs:
                proc.kill()
        return len(procs)

    def allow(self) -> None:
        with self._lock:
            self._stopped = False

    def probe(self, path: Path) -> ProbeResult:
        """Run ffprobe on a file. Raises ProbeError if it can't be read."""
        cmd = [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        with self._lock:
            if self._stopped:
                raise ProbeError("ffprobe was stopped")
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            self._running.add(proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=PROBE_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise ProbeError(f"ffprobe timed out after {PROBE_TIMEOUT}s")
        finally:
            with self._lock:
                self._running.discard(proc)
        if proc.returncode != 0:
            if proc.returncode < 0:
                raise ProbeError("ffprobe was stopped")
            raise ProbeError(stderr.strip().splitlines()[-1] if stderr.strip() else "ffprobe failed")
        return parse_probe(json.loads(stdout))


_default = ProbeSupervisor()


def probe(path: Path) -> ProbeResult:
    """Run ffprobe on a file (outside any scan manager). Raises ProbeError if it can't be read."""
    return _default.probe(path)


def parse_probe(data: dict) -> ProbeResult:
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    # Skip embedded cover art, which ffprobe reports as a video stream.
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = fmt.get("duration") or (video or {}).get("duration")
    return ProbeResult(
        container=fmt.get("format_name"),
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        pix_fmt=video.get("pix_fmt") if video else None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        duration=float(duration) if duration else None,
        interlaced=bool(video) and video.get("field_order") in ("tt", "bb", "tb", "bt"),
    )
