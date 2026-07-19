#!/usr/bin/env python3
"""RTCP SR observer: waits up to N seconds, prints SR arrivals and the
cross-track NTP<->RTP mapping skew an SR-honoring NVR would apply."""
import sys

from rtsplib import RtspSession, parse_sr, parse_rtp

host, port, path = sys.argv[1], sys.argv[2], sys.argv[3]
dur = float(sys.argv[4]) if len(sys.argv) > 4 else 40
s = RtspSession(host, port, path)
_, sdp = s.describe()
aclk = 16000
for line in sdp.splitlines():
    if "mpeg4-generic/" in line.lower():
        aclk = int(line.split("/")[1])
s.setup("video", 0)
s.setup("audio", 2)
_, rtpinfo = s.play()
print("RTP-Info:", rtpinfo)

first = {}
srs = {1: [], 3: []}
for t, ch, pkt in s.packets(dur):
    if ch in (0, 2):
        rtp = parse_rtp(pkt)
        if rtp and ch not in first:
            first[ch] = (rtp[0], rtp[1])
            print(f"  first ch{ch}: seq={rtp[0]} rtptime={rtp[1]}")
    elif ch in (1, 3):
        sr = parse_sr(pkt)
        if sr:
            srs[ch].append((t, sr[0], sr[1]))
            print(f"  t+{t:5.1f}s RTCP SR ch{ch}: ntp={sr[0]:.6f} rtp_ts={sr[1]}")
    if srs[1] and srs[3] and t > 6:
        break
s.close()

print()
if srs[1] and srs[3] and "video" in rtpinfo and "audio" in rtpinfo:
    vN, vR = srs[1][0][1], srs[1][0][2]
    aN, aR = srs[3][0][1], srs[3][0][2]

    def s32(x):
        return x - 2**32 if x > 2**31 else x

    tv = vN + s32((rtpinfo["video"][1] - vR) & 0xFFFFFFFF) / 90000
    ta = aN + s32((rtpinfo["audio"][1] - aR) & 0xFFFFFFFF) / aclk
    print(f"CROSS-TRACK SKEW an SR-honoring NVR applies: {(tv - ta) * 1000:+.1f} ms")
else:
    print(f"NO SR pair within {dur:.0f}s: ch1={len(srs[1])} ch3={len(srs[3])}")
