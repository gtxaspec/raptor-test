#!/bin/bash
# Audio codec/rate matrix against a raptor camera running the NFS
# build. For each (codec, rate): rewrite [audio] in the config over
# ssh, bounce rad+rsd, then verify per-codec RFC conformance, wire
# timestamp discipline, and a clean decode.
#
#   tools/codec-matrix.sh root@CAM /tmp/rt.conf [rtsp-port]
#
# Assumes the raptor daemons run from /mnt/nfs/raptor-binary/<soc>/
# (rad may need LD_LIBRARY_PATH for libfaac on old-firmware devices)
# and that the config's last case restores the deployment default.
set -u
TARGET=${1:?usage: codec-matrix.sh user@cam /path/to/conf [port]}
CONF=${2:?usage: codec-matrix.sh user@cam /path/to/conf [port]}
PORT=${3:-554}
CAM=${TARGET#*@}
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROBES="$DIR/probes"
FFMPEG=${FFMPEG:-$DIR/tools/ffmpeg}
FFPROBE=${FFPROBE:-$DIR/tools/ffprobe}
[ -x "$FFMPEG" ] || FFMPEG=ffmpeg
[ -x "$FFPROBE" ] || FFPROBE=ffprobe
BINDIR=${RAPTOR_BINDIR:-/mnt/nfs/raptor-binary/t31}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

run_case() {
    local codec=$1 rate=$2
    echo "=== codec=$codec rate=$rate ==="
    ssh "$TARGET" "
        sed -i '/^\\[audio\\]/,/^\\[/{/^codec = /d; /^sample_rate = /d}' $CONF
        sed -i '/^\\[audio\\]/a codec = $codec\\nsample_rate = $rate' $CONF
        kill \$(pidof rad) 2>/dev/null; sleep 1
        LD_LIBRARY_PATH=$BINDIR/lib start-stop-daemon -S -b -x $BINDIR/rad -- -c $CONF
        sleep 2
        kill \$(pidof rsd) 2>/dev/null; sleep 1
        start-stop-daemon -S -b -x $BINDIR/rsd -- -c $CONF
        sleep 2
        pidof rad >/dev/null && pidof rsd >/dev/null || echo '  DAEMON MISSING'
    " 2>/dev/null
    sleep 3
    timeout 30 python3 "$PROBES/rfc_check.py" "$CAM" "$PORT" /ch0 3 2>/dev/null |
        grep -E "3551|7587|3640" | sed 's/^/  /'
    timeout 18 python3 "$PROBES/seq_gap.py" "$CAM" "$PORT" /ch0 10 2>&1 |
        sed -n 2p | sed 's/^/  wire: /'
    timeout 20 "$FFMPEG" -nostdin -v warning -rtsp_transport tcp -t 8 \
        -i "rtsp://$CAM:$PORT/ch0" -map 0:a -f null - 2>"$TMP/dec.log"
    local e a
    e=$(grep -civE 'Guessed Channel Layout|Last message repeated' "$TMP/dec.log")
    a=$("$FFPROBE" -v error -rtsp_transport tcp -select_streams a -read_intervals %+4 \
        -show_entries stream=codec_name,sample_rate,channels -of csv=p=0 \
        "rtsp://$CAM:$PORT/ch0" 2>/dev/null | head -1)
    echo "  decode: err-lines=$e stream=[$a]"
    [ "$e" -gt 0 ] && sed 's/^/    /' "$TMP/dec.log" | head -3
}

run_case l16 16000
run_case pcmu 8000
run_case pcma 8000
run_case opus 16000
run_case aac 16000

exit 0
