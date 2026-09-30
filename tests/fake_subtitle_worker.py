"""Stands in for reel.subtitle_worker in tests: same arguments and output, no Whisper.

What it does is set by the source file's name: "fail" fails, "slow" takes a few
seconds (reporting progress), anything else finishes at once."""
import json
import sys
import time
from pathlib import Path

src, out = sys.argv[1], sys.argv[2]
language = sys.argv[sys.argv.index("--language") + 1] if "--language" in sys.argv else "ja"


def say(**news):
    print(json.dumps(news), flush=True)


say(stage="model")
say(stage="listening")
say(language=language)
if "fail" in Path(src).name:
    print("the audio couldn't be read", file=sys.stderr)
    sys.exit(1)
steps = 40 if "slow" in Path(src).name else 2
for n in range(steps):
    say(progress=(n + 1) / steps)
    time.sleep(0.1 if steps > 2 else 0)
Path(out).write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nBy the way, was the cake good?\n", encoding="utf-8")
