#!/usr/bin/env python3
"""Count RTP sequence gaps per track over N seconds (TCP-interleaved)."""
import sys

from rtsplib import RtspSession, parse_rtp

host, port, path, dur = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
s = RtspSession(host, port, path)
s.describe()
s.setup("video", 0)
s.setup("audio", 2)
s.play()

last = {}
count = {0: 0, 2: 0}
gaps = {0: 0, 2: 0}
elapsed = 0.0
for t, ch, pkt in s.packets(dur):
    elapsed = t
    if ch not in (0, 2):
        continue
    rtp = parse_rtp(pkt)
    if not rtp:
        continue
    seq = rtp[0]
    if ch in last:
        d = (seq - last[ch]) & 0xFFFF
        if d != 1:
            gaps[ch] += d - 1
    last[ch] = seq
    count[ch] += 1
s.close()
print(f"wall {elapsed:.1f}s: video pkts={count[0]} seq-lost={gaps[0]} | "
      f"audio pkts={count[2]} seq-lost={gaps[2]}")
