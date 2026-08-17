#!/bin/bash
#
# drift-leg.sh -- amplified SR clock-truth leg.
#
# Injects a known kernel frequency offset on the camera (the same
# mechanism ntpd uses, +-500ppm max) and fits the receiver-projected
# a/v skew slope from SR content over a few minutes. A sender whose
# SR references ride an undisciplined clock through a frozen offset
# fails in minutes at +300ppm; ambient discipline (tens of ppm) would
# need an hour+ and is temperature/ntpd-state dependent, so the
# injection is what makes this gateable. ppm=0 runs a pure ambient
# measurement (no clock manipulation, no ssh needed beyond none).
#
# The camera's wall clock gains ppm*secs of offset during the leg
# (~70ms at defaults); ntpd is restarted afterwards and corrects it.
# Do not run while wall-aligned recordings matter.
#
# usage: drift-leg.sh <host> <rtsp_port> <path> <user> <pass> <ssh> [ppm] [secs] [settle]
# env:   PASS_PPM (default 100) -- |skew slope| at/above this fails
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST=$1 PORT=$2 RPATH=$3 USER_=$4 PASS_=$5 SSH_=$6
PPM=${7:-300}
SECS=${8:-180}
SETTLE=${9:-30}
PASS_PPM=${PASS_PPM:-100}
SSHO="-o BatchMode=yes -o ConnectTimeout=8"

OLD_RAW=""
restore() {
    [ -n "$OLD_RAW" ] || return 0
    ssh $SSHO "$SSH_" "/tmp/freqadj setraw $OLD_RAW; /etc/init.d/S49ntpd start 2>/dev/null || ntpd -n -S /etc/ntpd_callback & true" >/dev/null 2>&1
    OLD_RAW=""
}
trap restore EXIT INT TERM

if [ "$PPM" != "0" ]; then
    # ssh pipe, not scp: camera sshd is dropbear with no sftp-server
    ssh $SSHO "$SSH_" "cat > /tmp/freqadj" < "$HERE/freqadj.mips" || {
        echo "SKIP drift-leg: cannot stage freqadj on $SSH_"
        exit 2
    }
    ssh $SSHO "$SSH_" "chmod +x /tmp/freqadj; /etc/init.d/S49ntpd stop 2>/dev/null; killall ntpd 2>/dev/null; true" >/dev/null 2>&1
    ADD=$(ssh $SSHO "$SSH_" "/tmp/freqadj add $PPM") || {
        echo "SKIP drift-leg: freqadj failed on $SSH_"
        exit 2
    }
    OLD_RAW=$(echo "$ADD" | sed -n 's/^old \(-\{0,1\}[0-9]*\) new.*/\1/p')
    if [ -z "$OLD_RAW" ]; then
        echo "SKIP drift-leg: unparseable freqadj output: $ADD"
        exit 2
    fi
    echo "drift-leg: injected ${PPM}ppm on $SSH_ (restore raw $OLD_RAW)"
    # Old kernels (3.10 class) converge the timekeeper mult toward a
    # frequency step over minutes, not instantly; measured ~200/264ppm
    # effective at t=60s on T31. Let the ramp mostly finish so the
    # fit window sees the intended rate.
    sleep "${INJECT_WAIT:-90}"
fi

JSON=$(python3 "$HERE/../probes/sr_drift.py" "$HOST" "$PORT" "$RPATH" "$USER_" "$PASS_" "$SECS" "$SETTLE")
RC=$?
restore
trap - EXIT INT TERM

echo "$JSON"
[ $RC -eq 0 ] || { echo "FAIL drift-leg: probe rc=$RC"; exit 1; }

python3 - "$JSON" "$PPM" "$PASS_PPM" <<'EOF'
import json, sys
d = json.loads(sys.argv[1])
ppm, bound = float(sys.argv[2]), float(sys.argv[3])
skew = d.get("skew_slope_ppm")
pts = d.get("skew_points", 0)
vl = d.get("video", {}).get("lead_slope_ppm")
al = d.get("audio", {}).get("lead_slope_ppm")
resets = sum(len(d.get(t, {}).get("resets", [])) for t in ("video", "audio"))
print(f"drift-leg: inject={ppm:+.0f}ppm skew_slope={skew}ppm "
      f"lead v={vl} a={al} points={pts} resets={resets}")
if skew is None or pts < 15:
    print("SKIP drift-leg: not enough SR pairs for a fit")
    sys.exit(2)
if abs(skew) >= bound:
    which = "video" if vl is not None and abs(vl) > abs(al or 0) else "audio"
    print(f"FAIL drift-leg: receiver a/v drifts {skew}ppm "
          f"({which} SR timeline diverges from its wire)")
    sys.exit(1)
print(f"OK drift-leg: skew {skew}ppm within {bound}ppm under "
      f"{ppm:+.0f}ppm discipline")
EOF
exit $?
