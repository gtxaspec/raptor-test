#!/usr/bin/env python3
"""Measure how coherent RTCP Sender Reports are with the RTP timeline
they describe.

For each SR, take the last RTP packet that preceded it on the same
track and extrapolate that packet's timestamp to the SR's arrival
instant at the nominal clock rate. A truthful SR lands where the
packet stream says the clock is; the delta is what a receiver has to
"correct" when it re-anchors on the SR. The spread of those deltas
across a session is the audible-blip budget: a constant delta is a
harmless fixed offset, a wobbling one is a periodic timeline jerk.

Prints one JSON object with per-track deltas so suite legs can assert
on the spread.

Usage: sr_coherence.py <host> <port> <path> <user> <pass> [seconds]
"""

import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from rtsplib import RtspSession, parse_rtp, parse_sr  # noqa: E402


def main():
    host, port, path, user, pw = sys.argv[1:6]
    secs = float(sys.argv[6]) if len(sys.argv) > 6 else 35.0

    s = RtspSession(host, int(port), "/" + path.lstrip("/"), user=user, password=pw)
    _status, sdp = s.describe()

    # Clock rate per media from its own rtpmap; never trust a default.
    # First rtpmap per media only: a backchannel adds a second m=audio
    # (sendonly PCMU/8000) that must not overwrite the live track's rate.
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

    pkts = {0: [], 2: []}
    srs = {1: [], 3: []}
    for at, ch, payload in s.packets(secs):
        if ch in pkts:
            r = parse_rtp(payload)
            if r:
                pkts[ch].append((at, r[1]))
        elif ch in srs:
            r = parse_sr(payload)
            if r:
                srs[ch].append((at, r[0], r[1]))
    s.close()

    # A receiver anchors its wall<->RTP mapping on each SR: offset_k =
    # ntp_k - rtp_k/rate. The step between consecutive offsets is the
    # correction it applies at that SR; the series spread is the total
    # timeline wobble a session suffers. An arrival-clock twin of the
    # series cross-checks without trusting the sender's NTP.
    out = {}
    base = {}
    for rtp_ch, rtcp_ch, name in ((0, 1, "video"), (2, 3, "audio")):
        rate = rates[name]
        ntp_off, arr_off = [], []
        rtp_prev, unwrap = None, 0
        for sr_at, sr_ntp, sr_rtp in srs[rtcp_ch]:
            if rtp_prev is not None and sr_rtp < rtp_prev:
                unwrap += 2**32
            rtp_prev = sr_rtp
            media_s = (sr_rtp + unwrap) / rate
            ntp_off.append(sr_ntp - media_s)
            arr_off.append(sr_at - media_s)
        # Rate views: wire ppm = packet-timeline least squares vs
        # arrival; sr ppm = rtp advance vs ntp advance across SR pairs.
        wire_ppm = None
        if len(pkts[rtp_ch]) > 50:
            xs = [a for a, _ in pkts[rtp_ch]]
            base_ts = pkts[rtp_ch][0][1]
            ys = [((t - base_ts + 2**31) % 2**32 - 2**31) / rate for _, t in pkts[rtp_ch]]
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            den = sum((x - mx) ** 2 for x in xs)
            wire_ppm = round((num / den - 1.0) * 1e6, 1) if den else None
        sr_ppms = []
        for (a1, n1, r1), (a2, n2, r2) in zip(srs[rtcp_ch], srs[rtcp_ch][1:]):
            dn = n2 - n1
            dr = ((r2 - r1 + 2**31) % 2**32 - 2**31) / rate
            if dn > 0.5:
                sr_ppms.append(round((dr / dn - 1.0) * 1e6, 1))
        # Whole-window media-clock rate vs the device wall clock, over
        # the STEADY SRs. Framing-independent and host-clock-
        # independent, which the wire slope is not -- this is the
        # drift number that stays measurable for codecs whose packet
        # spacing rides an availability grid (AAC).
        #
        # A first-to-last SR pair looked like the longest lever, but
        # it inherits whatever its two endpoints carry whole: the
        # first SRs are the session-start anchoring noise
        # steady_spread_ms already skips, and one resync-class
        # excursion landing on an endpoint reads as phantom rate
        # error (a ~20ms anchor over a 35s window is ~550ppm that a
        # 1-2ms steady spread plainly contradicts -- caught as an
        # intermittent battery FAIL). So: least-squares over the
        # steady region, and the leg's excise-one convention applied
        # as residuals -- one resync-class point (>5ms off the fit)
        # is dropped and the fit rerun; a systematic defect survives.
        def rate_fit(pts):
            if len(pts) < 2 or pts[-1][0] - pts[0][0] <= 5.0:
                return None, None
            fmx = sum(x for x, _ in pts) / len(pts)
            fmy = sum(y for _, y in pts) / len(pts)
            fden = sum((x - fmx) ** 2 for x, _ in pts)
            if not fden:
                return None, None
            slope = sum((x - fmx) * (y - fmy) for x, y in pts) / fden
            res = [y - (fmy + slope * (x - fmx)) for x, y in pts]
            return slope, res

        window_ppm = None
        steady = srs[rtcp_ch][2:]
        if len(steady) >= 2:
            n0, r0 = steady[0][1], steady[0][2]
            pts = [(s[1] - n0,
                    ((s[2] - r0 + 2**31) % 2**32 - 2**31) / rate)
                   for s in steady]
            slope, res = rate_fit(pts)
            if res is not None:
                worst = max(range(len(res)), key=lambda i: abs(res[i]))
                if abs(res[worst]) > 0.005:
                    slope2, _ = rate_fit(
                        [p for i, p in enumerate(pts) if i != worst])
                    if slope2 is not None:
                        slope = slope2
            if slope is not None:
                window_ppm = round((slope - 1.0) * 1e6, 1)
        rel = [round((o - ntp_off[0]) * 1000, 3) for o in ntp_off]
        rel_arr = [round((o - arr_off[0]) * 1000, 3) for o in arr_off]
        steps = [round(b - a, 3) for a, b in zip(rel, rel[1:])]
        out[name] = {
            "rate": rate,
            "packets": len(pkts[rtp_ch]),
            "srs": len(srs[rtcp_ch]),
            "mapping_rel_ms": rel,
            "mapping_rel_arrival_ms": rel_arr,
            "corrections_ms": steps,
            "spread_ms": round(max(rel) - min(rel), 3) if len(rel) > 1 else 0.0,
            # Session-start anchoring (audio gate, first-SR arming, slew
            # convergence after a join) is one-time noise; the reporter's
            # complaint and the assertable contract is the steady state.
            "steady_spread_ms": round(max(rel[2:]) - min(rel[2:]), 3) if len(rel) > 3 else None,
            "wire_ppm": wire_ppm,
            "sr_pair_ppm": sr_ppms,
            "window_ppm": window_ppm,
        }
        base[name] = ntp_off
    # Cross-track: A/V alignment shift a muxing receiver sees per SR era.
    n = min(len(base.get("video", [])), len(base.get("audio", [])))
    if n > 1:
        av = [round((base["audio"][i] - base["video"][i] -
                     (base["audio"][0] - base["video"][0])) * 1000, 3) for i in range(n)]
        out["av_skew_rel_ms"] = av
        out["av_skew_spread_ms"] = round(max(av) - min(av), 3)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
