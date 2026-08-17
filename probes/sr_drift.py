#!/usr/bin/env python3
"""Measure long-run RTCP SR truthfulness as rates, not spreads.

The SR coherence leg bounds the wobble of the ntp<->rtp mapping over a
40s window. That metric has a blind spot: a sender whose SR rtp field
is extrapolated from a reference on the wrong clock stays perfectly
SMOOTH while it diverges from the packet timeline, so the mapping
series looks flat and only a receiver notices, hours later, as lipsync
creep. The failure signature lives in two rate fits this probe takes
over minutes:

  lead (per track): (SR.rtp - last packet rtp)/rate minus the arrival
  gap. A truthful SR sits a send-latency above the wire; a reference
  on the wrong clock makes the lead GROW. Its slope is the ppm by
  which the SR timeline outruns the packets it describes.

  skew (cross-track): project the wire timeline of each track to wall
  time through that track's latest SR, exactly as a muxing receiver
  does, and fit video-minus-audio against session time. Slope is the
  receiver's lipsync drift in ppm. Arrival-clock terms cancel between
  tracks, so the host clock does not enter.

Ambient NTP discipline on the camera is tens of ppm; drive the kernel
frequency (adjtimex) a few hundred ppm during the measurement to make
the verdict deterministic -- see tools/drift-leg.sh.

RTP timestamps are unwrapped mod 2^32; a backward jump too large to be
wrap or jitter is a timeline reset (producer restart): it is reported
in "resets" and the fit restarts, never folded into a slope.

Prints one JSON object.

Usage: sr_drift.py <host> <port> <path> <user> <pass> [seconds] [settle]
"""

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from rtsplib import RtspSession, parse_rtp, parse_sr  # noqa: E402


class Unwrap:
    """Track one 32-bit RTP timeline; flag resets instead of absorbing
    them. Packets and SRs of a track share one instance (same clock)."""

    def __init__(self, rate):
        self.rate = rate
        self.base = 0
        self.last = None

    def feed(self, raw):
        if self.last is None:
            self.last = raw
            return self.base + raw, False
        delta = (raw - self.last) & 0xFFFFFFFF
        if delta < 0x80000000:
            # Forward. A leap no live stream produces (>10 min of
            # media) is a rebased timeline, not progress.
            if delta > self.rate * 600:
                self.base = 0
                self.last = raw
                return raw, True
            if raw < self.last:  # crossed 2^32
                self.base += 0x100000000
            self.last = raw
            return self.base + raw, False
        back = 0x100000000 - delta
        if back < self.rate * 10:
            # Small backward = SR-vs-packet jitter or reorder; map into
            # the current era without moving the forward anchor.
            if raw > self.last and self.base >= 0x100000000:
                return self.base - 0x100000000 + raw, False
            return self.base + raw, False
        self.base = 0
        self.last = raw
        return raw, True


def lsq(pts):
    """Least-squares slope + residuals for [(x, y)]; excise-one >5ms
    off the fit (suite convention) and refit once."""
    def fit(p):
        n = len(p)
        mx = sum(x for x, _ in p) / n
        my = sum(y for _, y in p) / n
        den = sum((x - mx) ** 2 for x, _ in p)
        if not den:
            return None, None
        sl = sum((x - mx) * (y - my) for x, y in p) / den
        res = [y - (my + sl * (x - mx)) for x, y in p]
        return sl, res

    if len(pts) < 3:
        return None
    sl, res = fit(pts)
    if sl is None:
        return None
    worst = max(range(len(res)), key=lambda i: abs(res[i]))
    if abs(res[worst]) > 0.005 and len(pts) > 3:
        sl2, _ = fit([p for i, p in enumerate(pts) if i != worst])
        if sl2 is not None:
            sl = sl2
    return sl


def main():
    host, port, path, user, pw = sys.argv[1:6]
    secs = float(sys.argv[6]) if len(sys.argv) > 6 else 180.0
    settle = float(sys.argv[7]) if len(sys.argv) > 7 else 30.0

    s = RtspSession(host, int(port), "/" + path.lstrip("/"), user=user, password=pw)
    _status, sdp = s.describe()

    # First rtpmap per media only: the backchannel adds a second
    # m=audio whose rate must not overwrite the live track's.
    rates = {"video": 90000, "audio": 8000}
    seen, media = set(), None
    for line in sdp.splitlines():
        if line.startswith("m="):
            media = line[2:].split()[0]
        elif line.startswith("a=rtpmap:") and media in rates and media not in seen:
            rates[media] = int(line.split("/")[1])
            seen.add(media)
    s.setup("video", 0)
    s.setup("audio", 2)
    s.play()

    tracks = {
        "video": {"rtp_ch": 0, "rtcp_ch": 1, "rate": rates["video"]},
        "audio": {"rtp_ch": 2, "rtcp_ch": 3, "rate": rates["audio"]},
    }
    for t in tracks.values():
        t.update(unwrap=Unwrap(t["rate"]), pkt=None, off=None,
                 pkts=0, srs=0, lead=[], resets=[])
    by_rtp = {t["rtp_ch"]: t for t in tracks.values()}
    by_rtcp = {t["rtcp_ch"]: t for t in tracks.values()}
    skew = []

    for at, ch, payload in s.packets(secs):
        if ch in by_rtp:
            r = parse_rtp(payload)
            if not r:
                continue
            t = by_rtp[ch]
            ts, reset = t["unwrap"].feed(r[1])
            if reset:
                t["resets"].append(round(at, 3))
                t["pkt"] = t["off"] = None
            t["pkt"] = (at, ts)
            t["pkts"] += 1
        elif ch in by_rtcp:
            r = parse_sr(payload)
            if not r:
                continue
            t = by_rtcp[ch]
            ntp, raw_rtp = r[0], r[1]
            ts, reset = t["unwrap"].feed(raw_rtp)
            if reset:
                t["resets"].append(round(at, 3))
                t["pkt"] = t["off"] = None
                continue
            t["srs"] += 1
            rate = t["rate"]
            if t["pkt"] is not None:
                pat, pts_ = t["pkt"]
                t["lead"].append((at, (ts - pts_) / rate - (at - pat)))
            t["off"] = ntp - ts / rate
            v, a = tracks["video"], tracks["audio"]
            if all(x["off"] is not None and x["pkt"] is not None
                   for x in (v, a)):
                wv = v["pkt"][1] / v["rate"] + (at - v["pkt"][0])
                wa = a["pkt"][1] / a["rate"] + (at - a["pkt"][0])
                skew.append((at, (wv + v["off"]) - (wa + a["off"])))
    s.close()

    def ppm(series):
        pts = [(x, y) for x, y in series if x >= settle]
        sl = lsq(pts)
        return None if sl is None else round(sl * 1e6, 1)

    out = {"seconds": secs, "settle": settle}
    for name, t in tracks.items():
        lead = [(x, y) for x, y in t["lead"] if x >= settle]
        out[name] = {
            "rate": t["rate"],
            "pkts": t["pkts"],
            "srs": t["srs"],
            "lead_slope_ppm": ppm(t["lead"]),
            "lead_first_last_ms": [round(lead[0][1] * 1000, 3),
                                   round(lead[-1][1] * 1000, 3)] if lead else None,
            "resets": t["resets"],
        }
    spts = [(x, y) for x, y in skew if x >= settle]
    out["skew_points"] = len(spts)
    out["skew_slope_ppm"] = ppm(skew)
    out["skew_first_last_ms"] = [round(spts[0][1] * 1000, 3),
                                 round(spts[-1][1] * 1000, 3)] if spts else None
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
