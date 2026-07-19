#!/usr/bin/env python3
"""RFC conformance probe: one TCP-interleaved RTSP session exercising
RFC 2326 (RTSP), RFC 4566 (SDP), RFC 3550 (RTP/RTCP), RFC 3640 (AAC).

Emits one line per check: "OK <name> -- <detail>" or "FAIL <name> -- <detail>".
Exit code 0 even on FAILs (the caller tallies); nonzero only on transport
errors that prevent the probe from running.
"""
import socket
import struct
import sys
import time

host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sr_window = float(sys.argv[4]) if len(sys.argv) > 4 else 8.0
url = f"rtsp://{host}:{port}{path}"
results = []


def emit(okay, name, detail=""):
    results.append((okay, name, detail))
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""), flush=True)


try:
    s = socket.create_connection((host, port), timeout=6)
except OSError as e:
    print(f"FAIL connect -- {e}")
    sys.exit(1)
cseq = 0


def req(method, u, extra=""):
    global cseq
    cseq += 1
    s.sendall(f"{method} {u} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: raptor-test\r\n{extra}\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            return "", "", b""
        buf += d
    head, rest = buf.split(b"\r\n\r\n", 1)
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length"):
            clen = int(line.split(b":")[1])
    while len(rest) < clen:
        rest += s.recv(4096)
    return head.decode(errors="replace"), rest[:clen].decode(errors="replace"), rest[clen:]


# -- RFC 2326: OPTIONS --
head, _, _ = req("OPTIONS", url)
public = next((l for l in head.split("\r\n") if l.lower().startswith("public")), "")
need = [m for m in ("DESCRIBE", "SETUP", "PLAY", "TEARDOWN") if m not in public]
emit(not need, "RFC2326 OPTIONS Public lists core methods", public or "no Public header")

# -- DESCRIBE + RFC 4566 SDP lints --
head, sdp, _ = req("DESCRIBE", url, "Accept: application/sdp\r\n")
emit("200" in head.split("\r\n")[0], "RFC2326 DESCRIBE 200", head.split("\r\n")[0])
oline = next((l for l in sdp.splitlines() if l.startswith("o=")), "")
of = oline.split()
o_ok = len(of) >= 6 and of[1] != "0" and of[5] != "0.0.0.0"
emit(o_ok, "RFC4566 o= has session id and real address", oline)
emit(any(l.startswith("m=video") for l in sdp.splitlines()), "RFC4566 video media section", "")
has_audio = any(l.startswith("m=audio") for l in sdp.splitlines())
controls = sum(1 for l in sdp.splitlines() if l.startswith("a=control:"))
emit(controls >= 1, "RFC4566 a=control present", f"{controls} entries")
if "H265" in sdp or "H264" in sdp:
    emit("sprop" in sdp, "RFC6184/7798 sprop parameter sets in SDP", "")
if "mpeg4-generic" in sdp.lower():
    fmtp = next((l for l in sdp.splitlines() if "mpeg4-generic" not in l and "AAC-hbr" in l), "")
    needf = [k for k in ("sizelength=13", "indexlength=3", "indexdeltalength=3", "config=") if k not in fmtp]
    emit(not needf, "RFC3640 AAC-hbr fmtp complete", fmtp.strip() or "no fmtp")

# -- SETUP both tracks, PLAY, RTP-Info --
sess = None
head, _, _ = req("SETUP", url + "/video", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
for l in head.split("\r\n"):
    if l.lower().startswith("session"):
        sess = l.split(":")[1].split(";")[0].strip()
        timeout_decl = "timeout=" in l
emit(sess is not None, "RFC2326 SETUP returns Session", head.split("\r\n")[0])
if not sess:
    sys.exit(0)
if has_audio:
    req("SETUP", url + "/audio", f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
head, _, leftover = req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
rtpinfo = {}
for l in head.split("\r\n"):
    if l.lower().startswith("rtp-info"):
        for part in l.split(":", 1)[1].split(","):
            d = dict(kv.split("=", 1) for kv in part.strip().split(";") if "=" in kv)
            key = "audio" if "audio" in d.get("url", "") else "video"
            try:
                rtpinfo[key] = (int(d["seq"]), int(d["rtptime"]))
            except (KeyError, ValueError):
                pass
emit(bool(rtpinfo), "RFC2326 PLAY carries RTP-Info", str(rtpinfo))

# -- Collect RTP + RTCP, verify anchors, SR conformance --
buf = leftover
first = {}
srs = {1: [], 3: []}
compound_sdes = {1: False, 3: False}
t0 = time.time()
s.settimeout(2)
while time.time() - t0 < sr_window:
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
        pkt = buf[4:4 + ln]
        buf = buf[4 + ln:]
        if ch in (0, 2) and ln >= 12 and ch not in first:
            seq, ts = struct.unpack(">HI", pkt[2:8])
            first[ch] = (seq, ts)
        if ch in (1, 3) and ln >= 28 and pkt[1] == 200:
            ntp_hi, ntp_lo, rtpts = struct.unpack(">III", pkt[8:20])
            srs[ch].append((time.time() - t0, ntp_hi + ntp_lo / 2**32, rtpts))
            # compound: an SDES (pt=202) must follow in the same frame
            srlen = (struct.unpack(">H", pkt[2:4])[0] + 1) * 4
            if len(pkt) > srlen and pkt[srlen + 1] == 202:
                compound_sdes[ch] = True

if "video" in rtpinfo and 0 in first:
    m = rtpinfo["video"] == first[0]
    emit(m, "RFC2326 RTP-Info video anchors match first packet",
         f"declared {rtpinfo['video']} first {first[0]}")
    emit(first[0][0] != 0 and first[0][1] != 0, "RFC3550 5.1 nonzero initial seq/timestamp",
         f"seq={first[0][0]} ts={first[0][1]}")
if has_audio and "audio" in rtpinfo and 2 in first:
    emit(rtpinfo["audio"] == first[2], "RFC2326 RTP-Info audio anchors match first packet",
         f"declared {rtpinfo['audio']} first {first[2]}")

if srs[1]:
    emit(True, "RFC3550 video Sender Reports present", f"{len(srs[1])} in {sr_window:.0f}s")
    emit(compound_sdes[1], "RFC3550 6.1 SR is compound with SDES", "")
    ntp_year = (srs[1][0][1] - 2208988800) / 31557600 + 1970
    emit(2020 < ntp_year < 2100, "RFC3550 SR NTP timestamp plausible", f"~year {ntp_year:.0f}")
    if len(srs[1]) >= 2:
        cad = srs[1][1][0] - srs[1][0][0]
        emit(0.5 <= cad <= 30, "RFC3550 SR cadence sane", f"{cad:.1f}s")
    if has_audio and srs[3] and "video" in rtpinfo and "audio" in rtpinfo:
        vN, vR = srs[1][0][1], srs[1][0][2]
        aN, aR = srs[3][0][1], srs[3][0][2]
        vclk = 90000
        aclk = 48000
        for l in sdp.splitlines():
            if "mpeg4-generic/" in l.lower():
                aclk = int(l.split("/")[1])
        def s32(x):
            return x - 2**32 if x > 2**31 else x
        tv = vN + s32((rtpinfo["video"][1] - vR) & 0xFFFFFFFF) / vclk
        ta = aN + s32((rtpinfo["audio"][1] - aR) & 0xFFFFFFFF) / aclk
        skew = (tv - ta) * 1000
        emit(abs(skew) < 200, "RFC3550 cross-track SR mapping consistent",
             f"a/v skew {skew:+.0f}ms")
else:
    emit(False, "RFC3550 video Sender Reports present", f"none within {sr_window:.0f}s")

# -- keepalive + teardown --
head, _, _ = req("GET_PARAMETER", url, f"Session: {sess}\r\n")
emit("200" in head.split("\r\n")[0], "RFC2326 GET_PARAMETER keepalive answered", head.split("\r\n")[0])
head, _, _ = req("TEARDOWN", url, f"Session: {sess}\r\n")
emit("200" in head.split("\r\n")[0], "RFC2326 TEARDOWN 200", head.split("\r\n")[0])
