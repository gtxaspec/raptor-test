#!/usr/bin/env python3
"""RFC conformance probe: one TCP-interleaved RTSP session exercising
RFC 2326 (RTSP), RFC 4566 (SDP), RFC 3550 (RTP/RTCP), RFC 3640 (AAC),
RFC 3551 (static payload types: G.711, L16) and RFC 7587 (Opus).

Emits one line per check: "OK <name> -- <detail>" or "FAIL <name> -- <detail>".
Exit code 0 even on FAILs (the caller tallies); nonzero only on transport
errors that prevent the probe from running.
"""
import hashlib
import socket
import struct
import sys
import time

host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sr_window = float(sys.argv[4]) if len(sys.argv) > 4 else 8.0
auth_user = sys.argv[5] if len(sys.argv) > 5 else None
auth_pass = sys.argv[6] if len(sys.argv) > 6 else None
url = f"rtsp://{host}:{port}{path}"
results = []
challenge = {}


def emit(okay, name, detail=""):
    results.append((okay, name, detail))
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""), flush=True)


try:
    s = socket.create_connection((host, port), timeout=6)
except OSError as e:
    print(f"FAIL connect -- {e}")
    sys.exit(1)
cseq = 0


def _digest(method, u):
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    ha1 = hashlib.md5(f"{auth_user}:{realm}:{auth_pass}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{u}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (f'Authorization: Digest username="{auth_user}", realm="{realm}", '
            f'nonce="{nonce}", uri="{u}", response="{resp}"\r\n')


def req(method, u, extra="", _retried=False):
    global cseq
    cseq += 1
    auth_hdr = _digest(method, u) if auth_user and challenge else ""
    s.sendall(f"{method} {u} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: raptor-test\r\n{auth_hdr}{extra}\r\n".encode())
    # Locate the RTSP response start rather than splitting on the first
    # blank line: after PLAY the socket carries interleaved RTP that can
    # begin mid-frame, so a naive split lands inside binary payload.
    buf = b""
    while True:
        i = buf.find(b"RTSP/1.0 ")
        if i >= 0 and b"\r\n\r\n" in buf[i:]:
            break
        d = s.recv(4096)
        if not d:
            return "", "", b""
        buf += d
    head, rest = buf[buf.find(b"RTSP/1.0 "):].split(b"\r\n\r\n", 1)
    clen = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length"):
            clen = int(line.split(b":")[1])
    while len(rest) < clen:
        rest += s.recv(4096)
    hd = head.decode(errors="replace")
    if auth_user and " 401 " in hd.split("\r\n")[0] + " " and not _retried:
        for line in hd.split("\r\n"):
            if line.lower().startswith("www-authenticate") and "digest" in line.lower():
                www = line.split(":", 1)[1].strip()
                challenge.update((k.strip(), v.strip().strip('"'))
                                 for k, v in (kv.split("=", 1)
                                              for kv in www[6:].split(",") if "=" in kv))
                return req(method, u, extra, _retried=True)
    return hd, rest[:clen].decode(errors="replace"), rest[clen:]


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

# -- Per-codec audio conformance (RFC 3551 static PTs, RFC 7587 Opus) --
maudio = next((l for l in sdp.splitlines() if l.startswith("m=audio")), "")
if maudio:
    apt = maudio.split()[3] if len(maudio.split()) > 3 else ""
    armap = next((l for l in sdp.splitlines()
                  if l.startswith(f"a=rtpmap:{apt} ")), "")
    enc = armap.split(" ", 1)[1] if " " in armap else ""
    lo = enc.lower()
    if apt in ("0", "8") or lo.startswith("pcmu") or lo.startswith("pcma"):
        # Static PTs may omit rtpmap entirely (RFC 3551); when present
        # it must agree with the PT and the fixed 8kHz clock.
        want_enc = "pcmu" if (apt == "0" or lo.startswith("pcmu")) else "pcma"
        want_pt = "0" if want_enc == "pcmu" else "8"
        ok_pt = apt == want_pt and (not enc or (lo.startswith(want_enc) and "8000" in enc))
        emit(ok_pt, "RFC3551 G.711 static payload type and 8kHz clock",
             f"pt={apt} rtpmap={enc or '(static, none needed)'}")
    elif lo.startswith("opus"):
        emit(enc.strip().lower() == "opus/48000/2",
             "RFC7587 opus rtpmap is opus/48000/2", enc)
    elif lo.startswith("l16"):
        parts = enc.split("/")
        rate = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        ch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        if apt in ("10", "11"):
            ok_l16 = rate == 44100 and ((apt == "10" and ch == 2) or (apt == "11" and ch == 1))
            emit(ok_l16, "RFC3551 L16 static PT only valid at 44100Hz",
                 f"pt={apt} rtpmap={enc}")
        else:
            emit(int(apt) >= 96 if apt.isdigit() else False,
                 "RFC3551 L16 non-44.1k uses dynamic PT", f"pt={apt} rtpmap={enc}")

# -- Resolve per-media SETUP targets from a=control (RFC 2326 §C.1.1);
#    backends differ (compy: video/audio, live555: track1/track2) --
track_url = {}
cur = None
for line in sdp.splitlines():
    if line.startswith("m="):
        cur = "video" if line[2:].startswith("video") else \
              "audio" if line[2:].startswith("audio") else None
    elif line.startswith("a=control:") and cur:
        c = line.split(":", 1)[1].strip()
        track_url[cur] = c if c.startswith("rtsp://") else url.rstrip("/") + "/" + c
vurl = track_url.get("video", url + "/video")
aurl = track_url.get("audio", url + "/audio")

# -- SETUP both tracks, PLAY, RTP-Info --
sess = None
head, _, _ = req("SETUP", vurl, "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
for l in head.split("\r\n"):
    if l.lower().startswith("session"):
        sess = l.split(":")[1].split(";")[0].strip()
emit(sess is not None, "RFC2326 SETUP returns Session", head.split("\r\n")[0])
if not sess:
    sys.exit(0)
if has_audio:
    req("SETUP", aurl, f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
head, _, leftover = req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
rtpinfo = {}
# Match each RTP-Info entry to a media by its control URL's last
# segment (backends name tracks differently, and live555 lists extra
# tracks like track3 that must not be misfiled as video/audio).
last_seg = {media: turl.rsplit("/", 1)[-1] for media, turl in
            {"video": vurl, "audio": aurl}.items()}
for l in head.split("\r\n"):
    if l.lower().startswith("rtp-info"):
        for part in l.split(":", 1)[1].split(","):
            d = dict(kv.split("=", 1) for kv in part.strip().split(";") if "=" in kv)
            u = d.get("url", "").rstrip("/")
            key = next((m for m, seg in last_seg.items() if u.endswith("/" + seg)), None)
            if key is None:
                key = "audio" if "audio" in u else "video" if "video" in u else None
            if key is None:
                continue
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

# -- keepalive --
head, _, _ = req("GET_PARAMETER", url, f"Session: {sess}\r\n")
emit("200" in head.split("\r\n")[0], "RFC2326 GET_PARAMETER keepalive answered", head.split("\r\n")[0])


def frame_rate(window):
    """Count interleaved data frames arriving within `window` seconds."""
    global s
    count = 0
    buf = b""
    end = time.time() + window
    s.settimeout(0.5)
    while time.time() < end:
        try:
            buf += s.recv(65536)
        except socket.timeout:
            continue
        while len(buf) >= 4:
            if buf[0:1] != b"$":
                buf = buf[1:]
                continue
            ln = struct.unpack(">H", buf[2:4])[0]
            if len(buf) < 4 + ln:
                break
            buf = buf[4 + ln:]
            count += 1
    return count


# -- PAUSE stops delivery, PLAY resumes (RFC 2326 10.6) --
pre = frame_rate(1.5)
head, _, _ = req("PAUSE", url, f"Session: {sess}\r\n")
if "200" in head.split("\r\n")[0]:
    time.sleep(0.5)
    frame_rate(0.5)  # drain in-flight
    paused = frame_rate(1.5)
    head, _, _ = req("PLAY", url, f"Session: {sess}\r\n")
    resumed = frame_rate(2.0)
    emit(pre > 10 and paused < max(3, pre // 10), "RFC2326 PAUSE halts delivery",
         f"pre={pre} paused={paused}")
    emit(resumed > 10, "RFC2326 PLAY resumes after PAUSE", f"resumed={resumed} frames/2s")
else:
    emit(False, "RFC2326 PAUSE supported", head.split("\r\n")[0])

head, _, _ = req("TEARDOWN", url, f"Session: {sess}\r\n")
emit("200" in head.split("\r\n")[0], "RFC2326 TEARDOWN 200", head.split("\r\n")[0])
s.close()

# -- Robustness: malformed requests must get 4xx, never break the server --
try:
    s = socket.create_connection((host, port), timeout=6)
    cseq = 0
    head, _, _ = req("DESCRIBE", f"rtsp://{host}:{port}/no/such/path",
                     "Accept: application/sdp\r\n")
    code = head.split("\r\n")[0]
    emit(any(c in code for c in ("404", "403", "454")), "robustness: bogus path rejected", code)
    head, _, _ = req("SETUP", vurl, "Transport: GARBAGE/NONSENSE;foo=bar\r\n")
    code = head.split("\r\n")[0]
    emit(any(c in code for c in ("400", "461", "451")), "robustness: garbage transport rejected", code)
    head, _, _ = req("PLAY", url, "Session: 424242424242\r\n")
    code = head.split("\r\n")[0]
    emit(any(c in code for c in ("454", "400", "455")), "robustness: bogus session rejected", code)
    s.close()
    s = socket.create_connection((host, port), timeout=6)
    cseq = 0
    head, _, _ = req("DESCRIBE", url, "Accept: application/sdp\r\n")
    emit("200" in head.split("\r\n")[0], "robustness: server healthy after abuse",
         head.split("\r\n")[0])
    s.close()
except OSError as e:
    emit(False, "robustness battery", f"connection error: {e}")
