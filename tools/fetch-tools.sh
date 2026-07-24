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

# openRTSP — the live555 test client, a different RTSP stack than
# libav (ffmpeg/mpv). Built from live555 source: LIVE555_SRC points at
# a checkout (default: the raptor build's .deps/live), else downloaded.
LIVE555_SRC=${LIVE555_SRC:-../../raptor/.deps/live}
if [ ! -x openRTSP ] && ! command -v g++ >/dev/null 2>&1; then
    echo "WARNING: g++ not found - skipping the openRTSP build (the live555-client leg will self-skip)"
elif [ ! -x openRTSP ]; then
    SRC=$(mktemp -d)
    if [ -d "$LIVE555_SRC/testProgs" ]; then
        cp -r "$LIVE555_SRC" "$SRC/live"
    else
        echo "fetching live555 source ..."
        curl -sL http://www.live555.com/liveMedia/public/live555-latest.tar.gz |
            tar xz -C "$SRC"
    fi
    (cd "$SRC/live" && sed -i 's/-DBSD=1/-DBSD=1 -std=c++20/' config.linux-64bit &&
        ./genMakefiles linux-64bit && make -j"$(nproc)") >/dev/null 2>&1
    cp "$SRC/live/testProgs/openRTSP" . && echo "built openRTSP"
    # live555MediaServer: a real RFC-conformant RTSP server for the
    # host-battery self-test (tools/host-battery.sh serves the test
    # fixture through it so a green run means the suite passed against
    # good media, no camera).
    [ -x "$SRC/live/mediaServer/live555MediaServer" ] && \
        cp "$SRC/live/mediaServer/live555MediaServer" . && echo "built live555MediaServer"
    rm -rf "$SRC"
fi
./openRTSP 2>&1 | head -1 || true
echo "installed into $(pwd)"

# WebRTC client (aiortc) — a real WHIP/ICE/DTLS-SRTP client for the
# --webrtc leg. Lives in a venv because aiortc needs PyAV.
if [ ! -x webrtc-venv/bin/python ]; then
    echo "creating webrtc venv (aiortc) ..."
    python3 -m venv webrtc-venv
    ./webrtc-venv/bin/pip install --quiet 'aiortc==1.15.*' 'av==17.*' 'aiohttp==3.*'
fi
./webrtc-venv/bin/python -c "import aiortc; print('aiortc', aiortc.__version__)"
