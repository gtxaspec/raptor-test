#!/usr/bin/env python3
"""Wire-level RTP continuity over N seconds (TCP-interleaved).

Line 1: sequence-gap summary per track (as before).
Line 2: timestamp summary per track: backward steps, spikes (any step
        beyond 8x the modal step), and for audio the share of steps
        exactly at the modal cadence. A steering loop that bang-bangs
        (alternating modal+1ms/modal-1ms) or a single future-stamped
        packet shows up here and nowhere else: players resync past it
        and file muxers silently drop around it.
"""
import sys
from collections import Counter

from rtsplib import RtspSession, parse_rtp

host, port, path, dur = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
s = RtspSession(host, port, path)
_, sdp = s.describe()
clocks = {0: 90000, 2: 0}
for line in sdp.splitlines():
    if line.startswith("a=rtpmap:"):
        try:
            clk = int(line.split("/")[1])
        except (IndexError, ValueError):
            continue
        if "m=audio" in sdp[:sdp.find(line)] and clocks[2] == 0:
            clocks[2] = clk
        elif clocks[0] == 90000:
            clocks[0] = clk
s.setup("video", 0)
s.setup("audio", 2)
s.play()

last = {}
count = {0: 0, 2: 0}
gaps = {0: 0, 2: 0}
last_ts = {}
last_wall = {}
deltas = {0: [], 2: []}
jumps = {0: [], 2: []}  # (ts_step, wall_gap) for later spike triage
elapsed = 0.0
for t, ch, pkt in s.packets(dur):
    elapsed = t
    if ch not in (0, 2):
        continue
    rtp = parse_rtp(pkt)
    if not rtp:
        continue
    seq, ts, _m = rtp
    if ch in last:
        d = (seq - last[ch]) & 0xFFFF
        if d != 1:
            gaps[ch] += d - 1
    last[ch] = seq
    count[ch] += 1
    if ch in last_ts:
        dt = (ts - last_ts[ch]) & 0xFFFFFFFF
        if dt >= 2**31:
            dt -= 2**32
        if dt != 0:  # same-frame fragments share a timestamp
            deltas[ch].append(dt)
            jumps[ch].append((dt, t - last_wall[ch]))
    last_ts[ch] = ts
    last_wall[ch] = t
s.close()
print(f"wall {elapsed:.1f}s: video pkts={count[0]} seq-lost={gaps[0]} | "
      f"audio pkts={count[2]} seq-lost={gaps[2]}")


def ts_summary(name, dl, clock, jl):
    if len(dl) < 10:
        return f"{name} ts-steps={len(dl)} (too few)"
    back = sum(1 for d in dl if d < 0)
    modal, modal_n = Counter(dl).most_common(1)[0]
    # A big ts step whose wall-clock arrival gap matches is an honest
    # source gap (frames really stopped); one without a matching wall
    # gap is a timestamp anomaly.
    spikes = []
    honest = 0
    for d, wg in jl:
        if abs(d) > 8 * abs(modal):
            if clock and abs(abs(d) / clock - wg) < max(0.2, 0.5 * abs(d) / clock):
                honest += 1
            else:
                spikes.append(d)
    pct = 100.0 * modal_n / len(dl)
    # Direction split of the off-modal steps: a steering loop tracking
    # real source drift nudges one way; a bang-banging loop alternates.
    up = sum(1 for d in dl if modal < d <= 8 * abs(modal))
    down = sum(1 for d in dl if 0 <= d < modal)
    return (f"{name} ts-steps={len(dl)} backward={back} spikes={len(spikes)}"
            f"{' ' + str(spikes[:3]) if spikes else ''} honest-gaps={honest}"
            f" modal={modal} modal-pct={pct:.0f} up={up} down={down}")


print(ts_summary("video", deltas[0], clocks[0], jumps[0]) + " | " +
      ts_summary("audio", deltas[2], clocks[2], jumps[2]))
