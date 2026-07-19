#!/usr/bin/env python3
"""Basic session probe: RTP-Info anchors, first packets, audio ts deltas."""
import sys

from rtsplib import RtspSession, parse_rtp

host, port, path = sys.argv[1], sys.argv[2], sys.argv[3]
s = RtspSession(host, port, path)
status, sdp = s.describe()
print("== DESCRIBE:", status)
for line in sdp.splitlines():
    if line.startswith(("a=control", "m=")):
        print("  " + line)
s.setup("video", 0)
s.setup("audio", 2)
status, rtpinfo = s.play()
print("== PLAY:", status, rtpinfo)

first = {}
audio_ts = []
for t, ch, pkt in s.packets(6):
    if ch not in (0, 2):
        continue
    rtp = parse_rtp(pkt)
    if not rtp:
        continue
    if ch not in first:
        first[ch] = (rtp[0], rtp[1])
        print(f"  first ch{ch}: seq={rtp[0]} rtptime={rtp[1]}")
    if ch == 2 and len(audio_ts) < 6:
        audio_ts.append(rtp[1])
    if len(audio_ts) >= 6 and 0 in first:
        break
s.close()
print("  audio ts deltas:", [b - a for a, b in zip(audio_ts, audio_ts[1:])])
