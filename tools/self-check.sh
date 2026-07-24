#!/bin/bash
# self-check: prove raptor-test's probes fire in the right DIRECTION.
#
# Starts probes/evil_server.py in each misbehavior mode and asserts the
# relevant probe PASSes on a correct stream and FAILs on the broken one.
# A conformance suite that never fails is worthless; this is the suite
# testing itself. No camera, no network — pure localhost.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROBES="$DIR/probes"
ES="$DIR/tools/selfcheck-assets/test.h264"
FF="${FFMPEG:-$DIR/tools/ffmpeg}"
PORT=18800
PASS=0
FAIL=0
SRV_PID=""

if [ ! -f "$ES" ]; then
    echo "missing fixture $ES — run: $DIR/tools/gen-selfcheck-assets.sh"
    exit 2
fi

pass() { echo "  PASS  $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL  $1${2:+ — $2}"; FAIL=$((FAIL + 1)); }

start() {  # mode
    PORT=$((PORT + 1))
    python3 "$PROBES/evil_server.py" "$ES" "$PORT" "$1" >/tmp/selfcheck-srv.log 2>&1 &
    SRV_PID=$!
    for _ in $(seq 1 20); do
        (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
        sleep 0.2
    done
    return 1
}
stop() { [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; SRV_PID=""; }
trap stop EXIT

# capture N frames; echoes the decoded frame count
cap_frames() {
    local secs=$1
    timeout -k 3 $((secs + 6)) "$FF" -nostdin -v error -rtsp_transport tcp \
        -i "rtsp://127.0.0.1:$PORT/video" -t "$secs" \
        -progress /tmp/selfcheck-cap.progress -f null - >/dev/null 2>&1
    sed -n 's/^frame=//p' /tmp/selfcheck-cap.progress 2>/dev/null | tail -1
}

echo "=== raptor-test self-check (probe direction) ==="

# ── baseline: a correct stream must satisfy every probe ──
if start good; then
    F=$(cap_frames 4)
    [ "${F:-0}" -gt 30 ] && pass "good: capture decodes video ($F frames)" \
        || fail "good: capture decodes video" "only ${F:-0} frames"
    TS=$(timeout 20 python3 "$PROBES/seq_gap.py" 127.0.0.1 "$PORT" /video 4 2>&1)
    echo "$TS" | grep -q 'seq-lost=0 ' && pass "good: seq continuity" || fail "good: seq continuity" "$TS"
    echo "$TS" | grep -q 'backward=0' && pass "good: no backward ts" || fail "good: no backward ts"
    echo "$TS" | grep -q 'spikes=0' && pass "good: no ts spikes" || fail "good: no ts spikes"
    RFC=$(timeout 25 python3 "$PROBES/rfc_check.py" 127.0.0.1 "$PORT" /video 3 2>&1)
    echo "$RFC" | grep -q 'OK RFC2326 SETUP returns Session' && pass "good: SETUP ok" || fail "good: SETUP ok"
else
    fail "good: server start"
fi
stop

# ── silent: PLAY 200 but no media — the vacuous-pass trap ──
if start silent; then
    F=$(cap_frames 4)
    [ "${F:-0}" -eq 0 ] && pass "silent: capture correctly yields 0 frames (vacuous-pass guard)" \
        || fail "silent: capture" "got $F frames from a silent server"
fi
stop

# ── seqdrop: sequence gaps must be detected ──
if start seqdrop; then
    TS=$(timeout 20 python3 "$PROBES/seq_gap.py" 127.0.0.1 "$PORT" /video 4 2>&1)
    LOST=$(echo "$TS" | grep -oE 'video pkts=[0-9]+ seq-lost=[0-9]+' | grep -oE 'seq-lost=[0-9]+' | cut -d= -f2)
    [ "${LOST:-0}" -gt 0 ] && pass "seqdrop: seq loss detected (seq-lost=$LOST)" \
        || fail "seqdrop: seq loss detected" "seq-lost=$LOST"
fi
stop

# ── tsback: backward timestamp steps must be detected ──
if start tsback; then
    TS=$(timeout 20 python3 "$PROBES/seq_gap.py" 127.0.0.1 "$PORT" /video 4 2>&1)
    BACK=$(echo "$TS" | grep -oE 'backward=[0-9]+' | head -1 | cut -d= -f2)
    [ "${BACK:-0}" -gt 0 ] && pass "tsback: backward ts detected (backward=$BACK)" \
        || fail "tsback: backward ts detected" "backward=$BACK"
fi
stop

# ── tsspike: a lone huge ts jump must be flagged as a spike ──
if start tsspike; then
    TS=$(timeout 20 python3 "$PROBES/seq_gap.py" 127.0.0.1 "$PORT" /video 4 2>&1)
    SP=$(echo "$TS" | grep -oE 'spikes=[0-9]+' | head -1 | cut -d= -f2)
    [ "${SP:-0}" -gt 0 ] && pass "tsspike: ts spike detected (spikes=$SP)" \
        || fail "tsspike: ts spike detected" "spikes=$SP"
fi
stop

# ── garbage200: a server that 200s a bad transport must be caught ──
if start garbage200; then
    RFC=$(timeout 25 python3 "$PROBES/rfc_check.py" 127.0.0.1 "$PORT" /video 3 2>&1)
    echo "$RFC" | grep -q 'FAIL.*garbage transport' && pass "garbage200: lax transport handling caught" \
        || fail "garbage200: lax transport handling caught" "robustness check did not fail"
fi
stop

echo ""
echo "=== self-check: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
