#!/bin/bash
# impair-check: prove raptor-test's wire probes behave correctly under
# CONTROLLED network impairment, instead of hand-waving past WiFi noise.
#
# Runs live555MediaServer (real UDP RTP) inside a network namespace,
# applies tc netem to that namespace loopback, and asserts:
#   - clean link: a UDP capture decodes cleanly (no error lines)
#   - 5% loss:    the capture's degradation detection FIRES (ffmpeg
#                 reports missed/concealing) — the suite notices loss
#                 rather than tolerating it blindly
#   - recovery:   with impairment removed, the capture is clean again
#
# UDP is required: TCP-interleaved RTP retransmits lost packets, so
# loss is invisible to the application. live555MediaServer serves real
# UDP RTP; evil_server is TCP-only and cannot show loss.
#
# A fresh netns has its own loopback that accepts a netem qdisc (the
# host's shared lo is special-cased and does not). Needs root; no
# camera, fully self-contained.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
ES="$DIR/tools/selfcheck-assets/test.h264"
SRV_BIN="$DIR/tools/live555MediaServer"
FF="${FFMPEG:-$DIR/tools/ffmpeg}"
NS=rtimpair
SRV_PID=""
PASS=0
FAIL=0

[ "$(id -u)" -eq 0 ] || { echo "impair-check needs root (netns + tc netem) — re-run with sudo"; exit 2; }
[ -f "$ES" ] || { echo "missing fixture — run tools/gen-selfcheck-assets.sh"; exit 2; }
[ -x "$SRV_BIN" ] || { echo "missing live555MediaServer — run tools/fetch-tools.sh"; exit 2; }

pass() { echo "  PASS  $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $1${2:+ — $2}"; FAIL=$((FAIL + 1)); }

teardown() {
    [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null
    ip netns pids "$NS" 2>/dev/null | xargs -r kill 2>/dev/null
    ip netns del "$NS" 2>/dev/null
    rm -rf "${WORK:-}" 2>/dev/null
}
trap teardown EXIT
ip netns del "$NS" 2>/dev/null

ip netns add "$NS"
ip netns exec "$NS" ip link set lo up

nsx() { ip netns exec "$NS" "$@"; }
netem()       { nsx tc qdisc replace dev lo root netem "$@" 2>/dev/null; }
netem_clear() { nsx tc qdisc del dev lo root 2>/dev/null; }
ERRPAT='missed|dropping|discontinuity|corrupt|concealing|invalid data|error while decoding'

# UDP capture inside the netns; echoes "<frames> <errlines>" so a
# caller can tell clean media (frames>0, 0 err) from no media at all
# (frames==0) — the vacuous-pass trap this whole suite guards against.
cap() {
    nsx timeout -k 3 14 "$FF" -nostdin -v warning -rtsp_transport udp \
        -i "rtsp://127.0.0.1:$PORT/stream.264" -t 6 \
        -progress /tmp/impair-cap.prog -f null - >/tmp/impair-cap.log 2>&1
    local fr err
    fr=$(sed -n 's/^frame=//p' /tmp/impair-cap.prog 2>/dev/null | tail -1)
    err=$(grep -ciE "$ERRPAT" /tmp/impair-cap.log)
    echo "${fr:-0} ${err:-0}"
}

WORK=$(mktemp -d); cp "$ES" "$WORK/stream.264"
nsx bash -c "cd '$WORK' && exec '$SRV_BIN'" >/tmp/impair-srv.log 2>&1 &
SRV_PID=$!
# live555MediaServer binds 554 when it can (root here), else 8554 —
# discover which from the listen set instead of assuming.
PORT=""
for _ in $(seq 1 25); do
    P=$(nsx ss -tln 2>/dev/null | grep -oE ':(554|8554) ' | tr -d ': ' | head -1)
    [ -n "$P" ] && { PORT=$P; break; }
    sleep 0.2
done
[ -n "$PORT" ] || { fail "live555 did not start in netns"; echo "=== $PASS passed, $((FAIL+1)) failed ==="; exit 1; }
echo "    (live555 on port $PORT)"

echo "=== raptor-test impairment check (tc netem, netns loopback, UDP) ==="

# clean link — media must actually flow AND decode without errors
netem_clear
read -r FR ERR <<<"$(cap)"
{ [ "${FR:-0}" -gt 30 ] && [ "${ERR:-9}" -eq 0 ]; } \
    && pass "clean link: media flows and decodes clean (frames=$FR err=$ERR)" \
    || fail "clean link" "frames=${FR:-0} err=${ERR:-?} (want frames>30, err=0)"

# 15% loss — media must still connect, and degradation MUST be visible
# (decode errors, or a materially reduced frame count vs clean).
netem loss 15%
read -r FR2 ERR2 <<<"$(cap)"
if [ "${FR2:-0}" -eq 0 ]; then
    fail "15% loss: session survives" "no frames at all (connection lost, not degraded)"
elif [ "${ERR2:-0}" -gt 0 ] || [ "${FR2:-0}" -lt $(( FR * 3 / 4 )) ]; then
    pass "15% loss: degradation detected (frames=$FR2 vs $FR clean, err=$ERR2)"
else
    fail "15% loss detection" "frames=$FR2 err=$ERR2 — suite blind to 15% loss"
fi

# recovery — clean media again once impairment is removed
netem_clear
read -r FR3 ERR3 <<<"$(cap)"
{ [ "${FR3:-0}" -gt 30 ] && [ "${ERR3:-9}" -eq 0 ]; } \
    && pass "recovery: clean media after impairment removed (frames=$FR3 err=$ERR3)" \
    || fail "recovery" "frames=${FR3:-0} err=${ERR3:-?}"
rm -rf "$WORK"

echo ""
echo "=== impair-check: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
