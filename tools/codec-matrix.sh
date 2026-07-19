#!/bin/bash
# Audio codec/rate matrix against a raptor camera running the NFS
# build. For each (codec, rate): rewrite [audio] in the config over
# ssh, bounce the audio daemon and BOTH RTSP backends, then verify
# per-codec RFC conformance, wire timestamp discipline, and a clean
# decode on each backend that is enabled. Every audio codec gets
# exercised on every server that can serve it — rsd (compy) always,
# rsd-555 (live555) when --r555-port is given.
#
#   tools/codec-matrix.sh user@cam /path/to/conf [options]
#     --port N          rsd RTSP port           (default 554)
#     --path P          stream path, no slash   (default ch0)
#     --r555-port N     also sweep rsd-555 on this port
#     --r555-path P     rsd-555 stream path     (default: same as --path)
#     --bindir DIR      daemon dir on device    (default /mnt/nfs/raptor-binary/t31)
#     --restore C:R[:P] final case = deployment default, e.g. aac:48000:he
#                       (default aac:16000)
#
# rad may need LD_LIBRARY_PATH for libfaac; rsd-555 needs libstdc++ in
# $BINDIR/lib — both are exported for every daemon start.
set -u
TARGET=${1:?usage: codec-matrix.sh user@cam /path/to/conf [options]}
CONF=${2:?usage: codec-matrix.sh user@cam /path/to/conf [options]}
shift 2
PORT=554
RPATH=ch0
R555_PORT=""
R555_PATH=""
BINDIR=/mnt/nfs/raptor-binary/t31
RESTORE=aac:16000
while [ $# -gt 0 ]; do
    case "$1" in
    --port) PORT=$2; shift 2 ;;
    --path) RPATH=$2; shift 2 ;;
    --r555-port) R555_PORT=$2; shift 2 ;;
    --r555-path) R555_PATH=$2; shift 2 ;;
    --bindir) BINDIR=$2; shift 2 ;;
    --restore) RESTORE=$2; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -n "$R555_PATH" ] || R555_PATH=$RPATH
CAM=${TARGET#*@}
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROBES="$DIR/probes"
FFMPEG=${FFMPEG:-$DIR/tools/ffmpeg}
FFPROBE=${FFPROBE:-$DIR/tools/ffprobe}
[ -x "$FFMPEG" ] || FFMPEG=ffmpeg
[ -x "$FFPROBE" ] || FFPROBE=ffprobe
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAILED=0

# check_backend <label> <port> <path> <expected_modal> — conformance +
# wire + decode against one RTSP backend for the current codec. The
# audio RTP timestamp step must equal the codec's real frame duration:
# a server timestamping AAC at its 20ms chunk rate decodes fine but
# runs the clock 3x fast, so decode-only checks miss it.
check_backend() {
    local label=$1 port=$2 path=$3 want_modal=$4
    timeout 30 python3 "$PROBES/rfc_check.py" "$CAM" "$port" "/$path" 3 2>/dev/null |
        grep -E "3551|7587|3640" | sed "s/^/  [$label] /"
    local wire modal
    wire=$(timeout 18 python3 "$PROBES/seq_gap.py" "$CAM" "$port" "/$path" 10 2>&1 | sed -n 2p)
    echo "  [$label] wire: $wire"
    modal=$(echo "$wire" | grep -oE 'audio ts-steps.*modal=[0-9]+' | grep -oE 'modal=[0-9]+' | grep -oE '[0-9]+')
    if [ -n "$want_modal" ] && [ "${modal:-0}" != "$want_modal" ]; then
        echo "  [$label] FAIL: audio ts step $modal != expected $want_modal (frame-duration bug)"
        FAILED=1
    fi
    timeout 20 "$FFMPEG" -nostdin -v warning -rtsp_transport tcp -t 8 \
        -i "rtsp://$CAM:$port/$path" -map 0:a -f null - 2>"$TMP/dec.log"
    local e a
    e=$(grep -civE 'Guessed Channel Layout|Last message repeated' "$TMP/dec.log")
    a=$("$FFPROBE" -v error -rtsp_transport tcp -select_streams a -read_intervals %+4 \
        -show_entries stream=codec_name,sample_rate,channels -of csv=p=0 \
        "rtsp://$CAM:$port/$path" 2>/dev/null | head -1)
    echo "  [$label] decode: err-lines=$e stream=[$a]"
    if [ "$e" -gt 0 ]; then
        sed 's/^/    /' "$TMP/dec.log" | head -3
        FAILED=1
    fi
}

# Expected audio RTP timestamp step for a codec at a rate: AAC frames
# are 1024 samples (2048 for HE, whose RTP clock is the output rate);
# Opus uses a 48k clock with 20ms frames; G.711/L16 are 20ms of clock.
expected_modal() {
    local codec=$1 rate=$2 profile=$3
    case "$codec" in
    aac)  [ "$profile" = he ] && echo 2048 || echo 1024 ;;
    opus) echo 960 ;;
    pcmu|pcma) echo 160 ;;
    l16)  echo $((rate / 50)) ;;
    *)    echo "" ;;
    esac
}

run_case() {
    local codec=$1 rate=$2 profile=${3:-}
    echo "=== codec=$codec rate=$rate${profile:+ profile=$profile} ==="
    ssh "$TARGET" "
        sed -i '/^\\[audio\\]/,/^\\[/{/^codec = /d; /^sample_rate = /d; /^aac_profile = /d}' $CONF
        sed -i '/^\\[audio\\]/a codec = $codec\\nsample_rate = $rate${profile:+\\naac_profile = $profile}' $CONF
        export LD_LIBRARY_PATH=$BINDIR/lib
        kill \$(pidof rad) 2>/dev/null; sleep 1
        start-stop-daemon -S -b -x $BINDIR/rad -- -c $CONF
        sleep 2
        kill \$(pidof rsd) 2>/dev/null; sleep 1
        start-stop-daemon -S -b -x $BINDIR/rsd -- -c $CONF
        if [ -n '$R555_PORT' ]; then
            kill -9 \$(pidof rsd-555) 2>/dev/null; sleep 1
            start-stop-daemon -S -b -x $BINDIR/rsd-555 -- -c $CONF
        fi
        sleep 2
        pidof rad >/dev/null && pidof rsd >/dev/null || echo '  DAEMON MISSING'
    " 2>/dev/null
    sleep 3
    local want
    want=$(expected_modal "$codec" "$rate" "$profile")
    check_backend "rsd" "$PORT" "$RPATH" "$want"
    if [ -n "$R555_PORT" ]; then
        check_backend "rsd-555" "$R555_PORT" "$R555_PATH" "$want"
    fi
}

run_case l16 16000
run_case pcmu 8000
run_case pcma 8000
run_case opus 16000
run_case aac 16000
# Final case restores the deployment default
IFS=: read -r RC RR RP <<<"$RESTORE"
run_case "$RC" "${RR:-16000}" "${RP:-}"

[ "$FAILED" -eq 0 ] && echo "MATRIX CLEAN" || echo "MATRIX HAD DECODE FAILURES"
exit "$FAILED"
