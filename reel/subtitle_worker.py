"""Make English subtitles for one video with Whisper, in a process of its own.

    python -m reel.subtitle_worker SRC OUT --model large-v3 --threads 8 --models DIR [--language ja]

Run by subtitles.SubtitleManager. The audio is decoded by ffmpeg (any format
Reel plays), Whisper translates what it hears into English, and the cues go to
OUT as WebVTT. Tells the manager how it's going on stdout, one JSON object per
line: {"stage": "model" | "audio" | "listening"}, {"language": "ja"} (what it
heard), {"progress": 0.42}. Exits non-zero with the reason on stderr.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .subtitles import cues, to_vtt

RATE = 16_000   # Whisper listens at 16 kHz, mono


def say(**news) -> None:
    print(json.dumps(news), flush=True)


def decode(src: str):
    """The whole audio track as 16 kHz mono float samples (a 1-hour video: ~230 MB)."""
    import numpy as np

    proc = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", src, "-map", "0:a:0", "-vn", "-ac", "1",
                           "-ar", str(RATE), "-f", "f32le", "pipe:1"], capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip().splitlines()
        raise SystemExit(f"ffmpeg couldn't read the audio: {detail[-1] if detail else proc.returncode}")
    return np.frombuffer(proc.stdout, np.float32)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("src")
    parser.add_argument("out")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--models", required=True)
    parser.add_argument("--language", default=None)
    args = parser.parse_args(argv)
    # The model is downloaded (once) into the data folder, not a home folder the
    # container's user doesn't have.
    os.environ.setdefault("HF_HOME", str(Path(args.models) / "huggingface"))

    say(stage="model")
    from faster_whisper import WhisperModel

    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads,
                         download_root=args.models)
    say(stage="audio")
    audio = decode(args.src)
    duration = len(audio) / RATE
    if not duration:
        raise SystemExit("The video has no sound to listen to.")
    say(stage="listening")
    segments, heard = model.transcribe(
        audio, language=args.language, task="translate", beam_size=5,
        vad_filter=True,                    # skip silence and music: fewer invented lines
        condition_on_previous_text=False,   # one bad line doesn't repeat down the video
        word_timestamps=True,               # when each line is really said
    )
    say(language=heard.language)
    lines = []
    for segment in segments:
        words = segment.words or []
        start = words[0].start if words else segment.start
        end = words[-1].end if words else segment.end
        lines.append((start, end, segment.text))
        say(progress=round(min(1.0, segment.end / duration), 4))
    Path(args.out).write_text(to_vtt(cues(lines)), encoding="utf-8")
    say(progress=1.0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            sys.exit(1)
        raise
