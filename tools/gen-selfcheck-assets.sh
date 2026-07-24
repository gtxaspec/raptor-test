#!/bin/bash
# Generate the H.264 fixture the self-check's evil_server replays.
# Committed output is small (~40KB); regenerate if it goes missing.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
FF="${FFMPEG:-$DIR/tools/ffmpeg}"
OUT="$DIR/tools/selfcheck-assets"
mkdir -p "$OUT"
"$FF" -y -v error -f lavfi -i testsrc=size=320x240:rate=30:duration=4 \
    -pix_fmt yuv420p -c:v libx264 -profile:v baseline -g 30 -bf 0 \
    -f h264 "$OUT/test.h264"
echo "wrote $OUT/test.h264 ($(stat -c%s "$OUT/test.h264" 2>/dev/null) bytes)"
