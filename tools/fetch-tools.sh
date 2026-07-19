#!/bin/bash
# Fetch a pinned ffmpeg/ffprobe into tools/ so raptor-test does not
# depend on the system ffmpeg. Client-side RTSP behavior shifts
# between major versions (7.1 streamcopy silently drops AAC audio
# from RTSP; 8.x key-flags it and copies fine), so results are only
# comparable when the client is pinned.
#
# Official static builds from BtbN/FFmpeg-Builds (linked from
# ffmpeg.org). Override the release with FF_TAG (e.g. n7.1).
set -euo pipefail
cd "$(dirname "$0")"

FF_TAG=${FF_TAG:-n8.1}
ASSET="ffmpeg-${FF_TAG}-latest-linux64-gpl-${FF_TAG#n}.tar.xz"
URL="https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/$ASSET"

echo "fetching $ASSET ..."
curl -sL -o /tmp/$ASSET "$URL"
tar xf /tmp/$ASSET -C /tmp
D=$(tar tf /tmp/$ASSET | head -1 | cut -d/ -f1)
cp "/tmp/$D/bin/ffmpeg" "/tmp/$D/bin/ffprobe" .
rm -rf "/tmp/$D" "/tmp/$ASSET"
./ffmpeg -version | head -1
echo "installed into $(pwd)"
