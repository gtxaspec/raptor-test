#!/usr/bin/env python3
"""RTSP/TCP probe v2: waits long enough to catch RTCP Sender Reports
(raptor first-SR is gated at 30s after PLAY), then computes the
cross-track A/V alignment an SR-honoring NVR (ffmpeg/Frigate) would
derive:  t_ntp(rtp) = sr_ntp + (rtp - sr_rtp)/clock  per track, then
compares the NTP times of the RTP-Info anchors (server's own claim of
"stream position at PLAY").  A nonzero delta = A/V shift the NVR will
apply that a no-SR client would not."""
import socket, sys, struct, time

host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
dur = float(sys.argv[4]) if len(sys.argv) > 4 else 40
url = f"rtsp://{host}:{port}{path}"
s = socket.create_connection((host, port), timeout=5)
cseq = 0

def req(method, u, extra=""):
    global cseq
    cseq += 1
    m = f"{method} {u} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: probe\r\n{extra}\r\n"
    s.sendall(m.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head, rest = buf.split(b"\r\n\r\n", 1)
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length"):
            clen = int(line.split(b":")[1])
    while len(rest) < clen:
        rest += s.recv(4096)
    return head.decode(errors="replace"), rest[:clen].decode(errors="replace"), rest[clen:]

req("DESCRIBE", url, "Accept: application/sdp\r\n")
sess = None
h, _, _ = req("SETUP", url + "/video", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
for l in h.split("\r\n"):
    if l.lower().startswith("session"):
        sess = l.split(":")[1].split(";")[0].strip()
h, _, _ = req("SETUP", url + "/audio", f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
h, _, leftover = req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
rtpinfo = {}
for l in h.split("\r\n"):
    if l.lower().startswith("rtp-info"):
        for part in l.split(":", 1)[1].split(","):
            fields = dict(kv.split("=", 1) for kv in part.strip().split(";") if "=" in kv)
        # parse each track entry properly
        rtpinfo = {}
        for part in l.split(":", 1)[1].split(","):
            d = {}
            for kv in part.strip().split(";"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    d[k] = v
            trk = "video" if "video" in d.get("url", "") else ("audio" if "audio" in d.get("url", "") else d.get("url", "?"))
            rtpinfo[trk] = {"seq": int(d.get("seq", -1)), "rtptime": int(d.get("rtptime", -1))}
print("RTP-Info:", rtpinfo)

buf = leftover
first = {}
srs = {1: [], 3: []}   # ch -> list of (t_arrival, ntp_float, rtp_ts)
t0 = time.time()
s.settimeout(5)
while time.time() - t0 < dur:
    try:
        buf += s.recv(65536)
    except socket.timeout:
        continue
    while len(buf) >= 4:
        if buf[0:1] != b"$":
            buf = buf[1:]
            continue
        ch = buf[1]
        ln = struct.unpack(">H", buf[2:4])[0]
        if len(buf) < 4 + ln:
            break
        pkt = buf[4:4+ln]
        buf = buf[4+ln:]
        if ch in (1, 3) and ln >= 20 and pkt[1] == 200:
            ntp_hi, ntp_lo, rtpts = struct.unpack(">III", pkt[8:20])
            ntp = ntp_hi + ntp_lo / 2**32
            srs[ch].append((time.time() - t0, ntp, rtpts))
            print(f"  t+{time.time()-t0:5.1f}s RTCP SR ch{ch}: ntp={ntp:.6f} rtp_ts={rtpts}")
        elif ch in (0, 2) and ln >= 12 and ch not in first:
            seq, ts = struct.unpack(">HI", pkt[2:8])
            first[ch] = (seq, ts)
            print(f"  first ch{ch}: seq={seq} rtptime={ts}")
    if srs[1] and srs[3] and time.time() - t0 > 33:
        break

print()
if srs[1] and srs[3]:
    vN, vR = srs[1][0][1], srs[1][0][2]
    aN, aR = srs[3][0][1], srs[3][0][2]
    VCLK, ACLK = 90000, 16000
    def s32(x):  # signed diff mod 2^32
        return x - 2**32 if x > 2**31 else x
    # NTP time each track's SR mapping assigns to its RTP-Info anchor
    tv = vN + s32((rtpinfo["video"]["rtptime"] - vR) & 0xFFFFFFFF) / VCLK
    ta = aN + s32((rtpinfo["audio"]["rtptime"] - aR) & 0xFFFFFFFF) / ACLK
    print(f"video SR maps RTP-Info anchor -> ntp {tv:.6f}")
    print(f"audio SR maps RTP-Info anchor -> ntp {ta:.6f}")
    print(f"CROSS-TRACK SKEW an SR-honoring NVR applies: {(tv-ta)*1000:+.1f} ms (video minus audio)")
    # also first-packet alignment
    if 0 in first and 2 in first:
        tv0 = vN + s32((first[0][1] - vR) & 0xFFFFFFFF) / VCLK
        ta0 = aN + s32((first[2][1] - aR) & 0xFFFFFFFF) / ACLK
        print(f"first-packet skew via SRs: video@{tv0:.6f} audio@{ta0:.6f} -> {(tv0-ta0)*1000:+.1f} ms")
else:
    print(f"NO SR pair within {dur:.0f}s: ch1={len(srs[1])} ch3={len(srs[3])}")
