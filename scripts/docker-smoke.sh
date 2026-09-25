#!/usr/bin/env bash
# Build the Reel image and check it works end to end, using a few generated clips.
#
#   sudo scripts/docker-smoke.sh        (or without sudo if you're in the docker group)
#
# Needs ffmpeg and curl on this machine. Leaves nothing behind.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=reel:smoke
NAME=reel-smoke
PORT=18765
# Run the container as the real user even under sudo, like compose does with PUID/PGID.
RUN_UID=${SUDO_UID:-$(id -u)}
RUN_GID=${SUDO_GID:-$(id -g)}
WORK=$(mktemp -d)
BASE=http://127.0.0.1:$PORT

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$*"; docker logs "$NAME" 2>&1 | tail -20 || true; exit 1; }
ok() { printf '\033[32mok\033[0m  %s\n' "$*"; }

step "Making test media"
mkdir -p "$WORK/media/clips" "$WORK/data"
gen() { ffmpeg -v error -y -f lavfi -i testsrc=size=640x360:rate=25:duration=4 -f lavfi -i sine=duration=4 "${@:2}" -shortest "$1"; }
gen "$WORK/media/clips/direct.mp4" -c:v libx264 -pix_fmt yuv420p -c:a aac
gen "$WORK/media/clips/old.avi" -c:v mpeg4 -c:a libmp3lame
ffmpeg -v error -y -f lavfi -i color=c=teal:s=1280x720 -frames:v 1 "$WORK/media/clips/direct.png"
chown -R "$RUN_UID:$RUN_GID" "$WORK"
chmod -R a+rX "$WORK/media"
ok "2 clips and a poster"

step "Building the image"
docker build -t "$IMAGE" .
VERSION=$(docker run --rm "$IMAGE" ffmpeg -hide_banner -version | head -1)
echo "$VERSION"
LATEST=$(curl -fsSL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256 \
  | grep -oE 'ffmpeg-n[0-9]+\.[0-9]+-latest-linux64-gpl-shared' | sed -E 's/ffmpeg-n([0-9.]+)-.*/\1/' | sort -uV | tail -n 1)
case "$VERSION" in
  "ffmpeg version n$LATEST"*) ok "ffmpeg is the newest release series ($LATEST)" ;;
  *) fail "expected ffmpeg $LATEST.x in the image, got: $VERSION" ;;
esac
docker run --rm "$IMAGE" ffprobe -hide_banner -version | head -1 | grep -q "ffprobe version n$LATEST" || fail "ffprobe"
ok "ffprobe matches"

step "Starting the container"
docker run -d --name "$NAME" --user "$RUN_UID:$RUN_GID" -p "$PORT:8000" \
  -v "$WORK/media:/media:ro" -v "$WORK/data:/data" "$IMAGE" >/dev/null
for _ in $(seq 60); do
  curl -fs "$BASE/api/libraries" >/dev/null && break
  sleep 1
done
curl -fs "$BASE/" | grep -q "<title>Reel</title>" || fail "home page not served"
ok "serving on $BASE"

step "Adding a library and scanning"
LIB=$(curl -fs -X POST "$BASE/api/libraries" -H 'content-type: application/json' \
  -d '{"name":"Clips","path":"/media/clips"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -fs -X POST "$BASE/api/libraries/$LIB/scan" >/dev/null
for _ in $(seq 60); do
  STATE=$(curl -fs "$BASE/api/libraries" | python3 -c 'import json,sys; print((json.load(sys.stdin)[0]["scan"] or {}).get("state"))')
  [ "$STATE" = done ] && break
  [ "$STATE" = error ] && fail "scan failed"
  sleep 1
done
[ "$STATE" = done ] || fail "scan didn't finish"
ITEMS=$(curl -fs "$BASE/api/libraries/$LIB/browse")
echo "$ITEMS" | python3 -c '
import json, sys
titles = sorted(i["title"] for i in json.load(sys.stdin)["items"])
assert titles == ["direct", "old"], titles
' || fail "unexpected scan results: $ITEMS"
id_of() { echo "$ITEMS" | python3 -c "import json,sys; print([i['id'] for i in json.load(sys.stdin)['items'] if i['title']=='$1'][0])"; }
DIRECT=$(id_of direct); OLD=$(id_of old)
# How to play is decided per browser at play time (/plan); without codec lists, for a typical one.
field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
DIRECT_PLAN=$(curl -fs "$BASE/api/items/$DIRECT/plan")
OLD_PLAN=$(curl -fs "$BASE/api/items/$OLD/plan")
[ "$(echo "$DIRECT_PLAN" | field mode)/$(echo "$DIRECT_PLAN" | field delivery)" = direct/file ] \
  || fail "direct.mp4 plan: $DIRECT_PLAN"
[ "$(echo "$OLD_PLAN" | field mode)/$(echo "$OLD_PLAN" | field delivery)" = transcode/progressive ] \
  || fail "old.avi plan: $OLD_PLAN"
ok "plans: direct.mp4 plays directly, old.avi is converted (audio: $(echo "$OLD_PLAN" | field audio))"

step "Thumbnails, direct play and live conversion"
curl -fs -o "$WORK/thumb.jpg" "$BASE/api/items/$DIRECT/thumb" || fail "thumbnail"
ok "thumbnail: $(ffprobe -v error -show_entries stream=width,height -of csv=p=0 "$WORK/thumb.jpg")"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -r 0-999 "$BASE/api/items/$DIRECT/file")
[ "$CODE" = 206 ] || fail "range request returned $CODE"
ok "direct play with range requests (206)"
curl -fs -o "$WORK/converted.mp4" "$BASE$(echo "$OLD_PLAN" | field url)"
CODECS=$(ffprobe -v error -show_entries stream=codec_name -of csv=p=0 "$WORK/converted.mp4" | tr '\n' ' ')
# The plan says what happens to the audio: MP3 is copied as is, anything else becomes AAC.
case "$(echo "$OLD_PLAN" | field audio)" in
  copy) EXPECTED="h264 mp3 " ;;
  *) EXPECTED="h264 aac " ;;
esac
[ "$CODECS" = "$EXPECTED" ] || fail "conversion produced: $CODECS (expected $EXPECTED from the plan)"
ok "old.avi converted to $CODECS, as planned"

step "Data folder and health"
[ -s "$WORK/data/reel.db" ] || fail "no database in the data folder"
ok "database written to the data volume, owned by $(stat -c %u:%g "$WORK/data/reel.db")"
for _ in $(seq 40); do
  HEALTH=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
  [ "$HEALTH" = healthy ] && break
  sleep 1
done
[ "$HEALTH" = healthy ] || fail "health check: $HEALTH"
ok "container reports healthy"

printf '\n\033[32mAll good.\033[0m The image works. Start it for real with: docker compose up -d --build\n'
