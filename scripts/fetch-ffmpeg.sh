#!/bin/sh
# Download the newest ffmpeg + ffprobe release build (GPL, shared libraries)
# from BtbN/FFmpeg-Builds, verify its checksum, and install it into $1:
#
#   OUT_DIR/bin/ffmpeg, OUT_DIR/bin/ffprobe, OUT_DIR/lib/*.so*
#
# The binaries find their libraries through a relative rpath ($ORIGIN/../lib),
# so OUT_DIR can live anywhere; put OUT_DIR/bin on PATH. The shared build is
# ~130 MB smaller than two static binaries.
#
#   fetch-ffmpeg.sh OUT_DIR [ARCH] [SERIES]
#     ARCH    amd64 (default) or arm64
#     SERIES  release series such as 9.0, or "auto" (default): the newest release
#
# The "n<series>" builds follow that release branch, so they include the latest
# point release and fixes. Used by the Dockerfile; also runs on a Linux host.
set -eu

out=$1
arch=${2:-amd64}
series=${3:-auto}
base=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest

case "$arch" in
  amd64) target=linux64 ;;
  arm64) target=linuxarm64 ;;
  *) echo "fetch-ffmpeg: unsupported architecture: $arch" >&2; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"

curl -fsSL -o checksums.sha256 "$base/checksums.sha256"
if [ "$series" = auto ]; then
  series=$(grep -oE "ffmpeg-n[0-9]+\.[0-9]+-latest-$target-gpl-shared-[0-9.]+\.tar\.xz" checksums.sha256 \
    | sed -E 's/.*-gpl-shared-([0-9.]+)\.tar\.xz/\1/' | sort -uV | tail -n 1)
  [ -n "$series" ] || { echo "fetch-ffmpeg: no release builds listed" >&2; exit 1; }
fi
name="ffmpeg-n$series-latest-$target-gpl-shared-$series"
echo "fetch-ffmpeg: $name"

curl -fsSL -o "$name.tar.xz" "$base/$name.tar.xz"
grep "  $name.tar.xz\$" checksums.sha256 | sha256sum -c -

tar -xJf "$name.tar.xz"
mkdir -p "$out/bin" "$out/lib"
mv "$name/bin/ffmpeg" "$name/bin/ffprobe" "$out/bin/"
# Runtime libraries only (no headers, static archives or pkg-config files).
find "$name/lib" -maxdepth 1 -name '*.so*' -exec mv {} "$out/lib/" \;
"$out/bin/ffmpeg" -hide_banner -version | head -n 1
