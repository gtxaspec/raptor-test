#!/usr/bin/env python3
"""Wall-clock step probe: the camera's clock jumps mid-stream.

  clock_step.py <host> <port> <path> [user] [pass] [seconds]

Every camera does this at least once per boot, when NTP first syncs
and drags the clock forward from whatever the RTC held. RTP timestamps
are a media clock (RFC 3550 5.1) and must keep advancing at the
payload rate through it: a sender that derives them from
CLOCK_REALTIME emits a discontinuity that every receiver reads as a
massive gap, and players stall, resync or drop the session. RTCP SR
NTP, by contrast, *is* wall clock and is expected to follow the step.

The caller steps the clock while this runs. Emits the observed RTP
timestamp gaps and SR NTP movement; the caller asserts on them.
"""
import json
import sys

from rtsplib import RtspSession, parse_rtp, parse_sr

host, port, path = sys.argv[1], sys.argv[2], sys.argv[3]
user = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
password = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None
dur = float(sys.argv[6]) if len(sys.argv) > 6 else 20.0

s = RtspSession(host, port, path, user=user, password=password)
_, sdp = s.describe()
s.setup("video", 0)
s.play()

prev_ts = None
prev_wall = None
gaps = []          # (rtp_delta_seconds, wall_delta_seconds)
srs = []           # (offset, ntp)
frames = 0
for t, ch, pkt in s.packets(dur):
    if ch == 0:
        rtp = parse_rtp(pkt)
        if not rtp:
            continue
        frames += 1
        ts = rtp[1]
        if prev_ts is not None and ts != prev_ts:
            d = (ts - prev_ts) & 0xFFFFFFFF
            if d > 0x80000000:      # timestamp went backwards
                d -= 1 << 32
            gaps.append((d / 90000.0, t - prev_wall))
            prev_wall = t
        if prev_ts is None:
            prev_wall = t
        prev_ts = ts
    elif ch == 1:
        sr = parse_sr(pkt)
        if sr:
            srs.append((t, sr[0]))
s.close()

worst = max((abs(g[0]) for g in gaps), default=0.0)
sr_jump = 0.0
if len(srs) >= 2:
    # Biggest movement of SR NTP beyond the wall time that elapsed
    # between the two reports: that residue is the step itself.
    sr_jump = max(abs((b[1] - a[1]) - (b[0] - a[0])) for a, b in zip(srs, srs[1:]))

print("SUMMARY " + json.dumps({
    "frames": frames,
    "max_rtp_gap_s": round(worst, 3),
    "sr_count": len(srs),
    "sr_ntp_step_s": round(sr_jump, 1),
}), flush=True)
