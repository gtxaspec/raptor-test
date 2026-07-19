#!/usr/bin/env python3
"""Wall-clock video frame-arrival gaps over N seconds. Reports gaps
above 80ms: delivery stalls that stream timestamps cannot show."""
import sys

from rtsplib import RtspSession, parse_rtp

host, port, path, dur = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
s = RtspSession(host, port, path)
s.describe()
s.setup("video", 0)
s.play()

frames = []
for t, ch, pkt in s.packets(dur):
    if ch != 0:
        continue
    rtp = parse_rtp(pkt)
    if rtp and rtp[2]:  # marker = end of frame
        frames.append(t)
s.close()
gaps = [(round((b - a) * 1000, 1), round(a, 1))
        for a, b in zip(frames, frames[1:]) if (b - a) > 0.08]
print(f"{len(frames)} frames, arrival gaps >80ms (ms, at s): {gaps[:10]}")
