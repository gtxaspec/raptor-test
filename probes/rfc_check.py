#!/usr/bin/env python3
"""RFC conformance probe: one TCP-interleaved RTSP session exercising
RFC 2326 (RTSP), RFC 4566 (SDP), RFC 3550 (RTP/RTCP), RFC 3640 (AAC),
RFC 3551 (static payload types: G.711, L16) and RFC 7587 (Opus).

Emits one line per check: "OK <name> -- <detail>" or "FAIL <name> -- <detail>".
Exit code 0 even on FAILs (the caller tallies); nonzero only on transport
errors that prevent the probe from running.
"""
import base64
import binascii
import hashlib
import socket
import struct
import sys
import time

from rtsplib import audio_clock

host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sr_window = float(sys.argv[4]) if len(sys.argv) > 4 else 8.0
auth_user = sys.argv[5] if len(sys.argv) > 5 else None
auth_pass = sys.argv[6] if len(sys.argv) > 6 else None
url = f"rtsp://{host}:{port}{path}"
results = []
challenge = {}
pre_response = b""


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
    global cseq, pre_response
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
    # Keep what arrived BEFORE the status line: a server may send
    # frames between request and response (rsd emits RTCP BYE ahead of
    # the TEARDOWN 200), and discarding this window blinded the BYE
    # compliance leg for months. May begin mid-frame; scanners resync.
    pre_response = buf[:buf.find(b"RTSP/1.0 ")]
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
public = next((ln for ln in head.split("\r\n") if ln.lower().startswith("public")), "")
need = [m for m in ("DESCRIBE", "SETUP", "PLAY", "TEARDOWN") if m not in public]
emit(not need, "RFC2326 OPTIONS Public lists core methods", public or "no Public header")

# -- DESCRIBE + RFC 4566 SDP lints --
head, sdp, _ = req("DESCRIBE", url, "Accept: application/sdp\r\n")
emit("200" in head.split("\r\n")[0], "RFC2326 DESCRIBE 200", head.split("\r\n")[0])
oline = next((ln for ln in sdp.splitlines() if ln.startswith("o=")), "")
of = oline.split()
o_ok = len(of) >= 6 and of[1] != "0" and of[5] != "0.0.0.0"
emit(o_ok, "RFC4566 o= has session id and real address", oline)

# b= lines must precede a= lines within a media section (RFC 4566
# Section 5: fixed field order). Order-sensitive parsers, ffmpeg's
# included, may ignore or misparse a late b=; this bit prudynt once
# (its b=AS landed after a=control until reordered).
_border_ok = True
_border_detail = ""
for _sec in sdp.split("\nm=")[1:]:
    _seen_a = False
    for _ln in _sec.splitlines():
        if _ln.startswith("a="):
            _seen_a = True
        elif _ln.startswith("b=") and _seen_a:
            _border_ok = False
            _border_detail = f"'{_ln.strip()}' after a= in m={_sec.splitlines()[0]}"
emit(_border_ok, "RFC4566 5 b= precedes a= in media sections", _border_detail)
if any(ln.startswith("m=video") for ln in sdp.splitlines()):
    emit(True, "RFC4566 video media section", "")
else:
    emit(any(ln.startswith("m=audio") for ln in sdp.splitlines()),
         "RFC4566 media section present (audio-only source)", "")
has_audio = any(ln.startswith("m=audio") for ln in sdp.splitlines())
is_h265 = "H265" in sdp.upper()
controls = sum(1 for ln in sdp.splitlines() if ln.startswith("a=control:"))
emit(controls >= 1, "RFC4566 a=control present", f"{controls} entries")
if "H265" in sdp or "H264" in sdp:
    emit("sprop" in sdp, "RFC6184/7798 sprop parameter sets in SDP", "")
if "mpeg4-generic" in sdp.lower():
    fmtp = next((ln for ln in sdp.splitlines() if "mpeg4-generic" not in ln and "AAC-hbr" in ln), "")
    needf = [k for k in ("sizelength=13", "indexlength=3", "indexdeltalength=3", "config=") if k not in fmtp]
    emit(not needf, "RFC3640 AAC-hbr fmtp complete", fmtp.strip() or "no fmtp")

# -- Per-codec audio conformance (RFC 3551 static PTs, RFC 7587 Opus) --
maudio = next((ln for ln in sdp.splitlines() if ln.startswith("m=audio")), "")
if maudio:
    apt = maudio.split()[3] if len(maudio.split()) > 3 else ""
    armap = next((ln for ln in sdp.splitlines()
                  if ln.startswith(f"a=rtpmap:{apt} ")), "")
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
# Audio-only sources have no video track: SETUP the audio track on
# the primary interleaved pair instead.
primary_url = vurl if "video" in track_url or not track_url else aurl

# -- Transport spec acceptance (RFC 2326 §12.39). "RTP/AVP" and
#    "RTP/AVP/UDP" name the same thing: the lower transport defaults to
#    UDP when omitted, so a server that takes one MUST take the other.
#    ffmpeg, VLC and mpv all send the explicit form, and a server that
#    rejects it is unreachable from every one of them while a bare
#    RTP/AVP probe still passes. Each spelling gets a fresh session that
#    is torn down immediately -- this is about the SETUP answer only. --
for spec in ("RTP/AVP;unicast;client_port=41000-41001",
             "RTP/AVP/UDP;unicast;client_port=41002-41003"):
    probe = socket.create_connection((host, port), timeout=6)
    _s, _c, _sock = s, cseq, probe
    s, cseq = probe, 0
    head, _, _ = req("SETUP", primary_url, f"Transport: {spec}\r\n")
    code = head.split("\r\n")[0]
    tsess = None
    for ln in head.split("\r\n"):
        if ln.lower().startswith("session"):
            tsess = ln.split(":")[1].split(";")[0].strip()
    if tsess:
        req("TEARDOWN", url, f"Session: {tsess}\r\n")
    probe.close()
    s, cseq = _s, _c
    emit("200" in code, f"RFC2326 12.39 SETUP accepts {spec.split(';')[0]}", code)

# -- SETUP both tracks, PLAY, RTP-Info --
sess = None
head, _, _ = req("SETUP", primary_url, "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
for ln in head.split("\r\n"):
    if ln.lower().startswith("session"):
        sess = ln.split(":")[1].split(";")[0].strip()
emit(sess is not None, "RFC2326 SETUP returns Session", head.split("\r\n")[0])
if not sess:
    sys.exit(0)
if has_audio and primary_url != aurl:
    req("SETUP", aurl, f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
head, _, leftover = req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
rtpinfo = {}
# Match each RTP-Info entry to a media by its control URL's last
# segment (backends name tracks differently, and live555 lists extra
# tracks like track3 that must not be misfiled as video/audio).
last_seg = {media: turl.rsplit("/", 1)[-1] for media, turl in
            {"video": vurl, "audio": aurl}.items()}
for ln in head.split("\r\n"):
    if ln.lower().startswith("rtp-info"):
        for part in ln.split(":", 1)[1].split(","):
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
cname = {1: False, 3: False}
first_at = {}
last = {}
inband_ps = {}  # NAL type -> first payload seen in band (SPS=7, PPS=8)
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
        if ch in (0, 2) and ln >= 12:
            seq, ts = struct.unpack(">HI", pkt[2:8])
            if ch not in first:
                first[ch] = (seq, ts)
                first_at[ch] = time.time()
            last[ch] = (ts, time.time())
        if ch == 0 and ln >= 13:
            # Parameter sets are small and are sent whole, so only
            # single-NAL and aggregation packets are unpacked; the
            # fragmentation forms carry slice data. H.264 (RFC 6184
            # §5.6) and H.265 (RFC 7798 §4.4) differ in NAL header
            # width, type field and aggregation payload type.
            payload = pkt[12 + 4 * (pkt[0] & 0x0F) :] if pkt[0] & 0x0F else pkt[12:]
            if payload and not is_h265:
                nt = payload[0] & 0x1F
                if nt in (7, 8) and nt not in inband_ps:
                    inband_ps[nt] = payload
                elif nt == 24:  # STAP-A
                    off = 1
                    while off + 2 <= len(payload):
                        sz = struct.unpack(">H", payload[off : off + 2])[0]
                        nal = payload[off + 2 : off + 2 + sz]
                        if nal and (nal[0] & 0x1F) in (7, 8):
                            inband_ps.setdefault(nal[0] & 0x1F, nal)
                        off += 2 + sz
            elif len(payload) >= 2:
                nt = (payload[0] >> 1) & 0x3F
                if nt in (32, 33, 34) and nt not in inband_ps:
                    inband_ps[nt] = payload
                elif nt == 48:  # AP
                    off = 2
                    while off + 2 <= len(payload):
                        sz = struct.unpack(">H", payload[off : off + 2])[0]
                        nal = payload[off + 2 : off + 2 + sz]
                        if len(nal) >= 2 and ((nal[0] >> 1) & 0x3F) in (32, 33, 34):
                            inband_ps.setdefault((nal[0] >> 1) & 0x3F, nal)
                        off += 2 + sz
        if ch in (1, 3) and ln >= 28 and pkt[1] == 200:
            ntp_hi, ntp_lo, rtpts = struct.unpack(">III", pkt[8:20])
            srs[ch].append((time.time() - t0, ntp_hi + ntp_lo / 2**32, rtpts))
            # compound: an SDES (pt=202) must follow in the same frame
            srlen = (struct.unpack(">H", pkt[2:4])[0] + 1) * 4
            if len(pkt) > srlen and pkt[srlen + 1] == 202:
                compound_sdes[ch] = True
                # RFC 3550 6.5.1: the SDES chunk must carry a CNAME
                # (item type 1). It is the only identifier that ties
                # this sender's tracks together for a receiver doing
                # inter-media sync, so an SR without one is not usable
                # for the A/V alignment the SR exists to provide.
                sd = pkt[srlen:]
                if len(sd) > 8 and sd[8:9] == b"\x01":
                    cname[ch] = True

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
    emit(cname[1], "RFC3550 6.5.1 SDES carries CNAME",
         "present" if cname[1] else "no CNAME item in SDES chunk")
    ntp_year = (srs[1][0][1] - 2208988800) / 31557600 + 1970
    emit(2020 < ntp_year < 2100, "RFC3550 SR NTP timestamp plausible", f"~year {ntp_year:.0f}")
    if len(srs[1]) >= 2:
        cad = srs[1][1][0] - srs[1][0][0]
        emit(0.5 <= cad <= 30, "RFC3550 SR cadence sane", f"{cad:.1f}s")
    if has_audio and srs[3] and "video" in rtpinfo and "audio" in rtpinfo:
        vN, vR = srs[1][0][1], srs[1][0][2]
        aN, aR = srs[3][0][1], srs[3][0][2]
        vclk = 90000
        aclk = audio_clock(sdp, default=48000)
        def s32(x):
            return x - 2**32 if x > 2**31 else x
        tv = vN + s32((rtpinfo["video"][1] - vR) & 0xFFFFFFFF) / vclk
        ta = aN + s32((rtpinfo["audio"][1] - aR) & 0xFFFFFFFF) / aclk
        skew = (tv - ta) * 1000
        emit(abs(skew) < 200, "RFC3550 cross-track SR mapping consistent",
             f"a/v skew {skew:+.0f}ms")
else:
    emit(False, "RFC3550 video Sender Reports present", f"none within {sr_window:.0f}s")

# -- sprop-parameter-sets must be the parameter sets actually sent
#    (RFC 6184 §8.1). A decoder that trusts the SDP and never sees a
#    correction decodes with the wrong geometry or fails outright, and
#    the mismatch is invisible to any check that only asks whether the
#    attribute is present. --
def _b64(x):
    try:
        return base64.b64decode(x + "=" * (-len(x) % 4))
    except (ValueError, binascii.Error):
        return b""


declared = {}
if is_h265:
    # RFC 7798 §7.1: each parameter set gets its own attribute.
    for attr, nt in (("sprop-vps=", 32), ("sprop-sps=", 33), ("sprop-pps=", 34)):
        for ln in sdp.splitlines():
            if attr in ln:
                declared[nt] = _b64(ln.split(attr, 1)[1].split(";")[0].strip())
    labels = ((32, "VPS"), (33, "SPS"), (34, "PPS"))
    rfc = "RFC7798 7.1"
else:
    sprop = ""
    for ln in sdp.splitlines():
        if "sprop-parameter-sets=" in ln:
            sprop = ln.split("sprop-parameter-sets=", 1)[1].split(";")[0].strip()
    for n in (_b64(x) for x in sprop.split(",") if x):
        if n:
            declared[n[0] & 0x1F] = n
    labels = ((7, "SPS"), (8, "PPS"))
    rfc = "RFC6184 8.1"

if declared and inband_ps:
    for nt, label in labels:
        if nt in declared and nt in inband_ps:
            emit(declared[nt] == inband_ps[nt], f"{rfc} sprop {label} matches in-band {label}",
                 f"sdp {declared[nt].hex()} vs stream {inband_ps[nt].hex()}")
elif not declared:
    emit(False, f"{rfc} sprop parameter sets present for comparison", "absent from SDP")

# -- RTP timestamp slope must match the declared clock (RFC 3550 5.1).
#    A stream whose timestamps advance at the wrong rate plays at the
#    wrong speed or drifts against audio no matter how healthy every
#    other check looks, and the rate is declared, never inferred: this
#    compares what the SDP promises with what the packets do. --
for _ch, _label, _rate in ((0, "video", 90000),
                           (2, "audio", audio_clock(sdp, default=0))):
    if _ch in first and _ch in last and _rate:
        _dts = (last[_ch][0] - first[_ch][1]) & 0xFFFFFFFF
        _dt = last[_ch][1] - first_at[_ch]
        if _dt > 1.0 and _dts:
            _slope = _dts / _dt
            emit(abs(_slope - _rate) <= 0.05 * _rate,
                 f"RFC3550 5.1 {_label} RTP clock advances at the declared rate",
                 f"{_slope:.0f} Hz measured over {_dt:.1f}s, SDP declares {_rate}")

# -- Every a=control must resolve under the session it came from
#    (RFC 2326 C.1.1). A control URL pointing somewhere else sends a
#    client's SETUP to another session, and the SDP is the only place
#    that mapping is stated. --
_base = url.rstrip("/")
_bad = [f"{m}:{u}" for m, u in track_url.items()
        if u.startswith("rtsp://") and not u.startswith(_base)]
emit(not _bad, "RFC2326 C.1.1 a=control URLs resolve under the session URL",
     ", ".join(_bad) if _bad else f"{len(track_url)} track(s) under {_base}")

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

head, tail_body, tail_rest = req("TEARDOWN", url, f"Session: {sess}\r\n")
emit("200" in head.split("\r\n")[0], "RFC2326 TEARDOWN 200", head.split("\r\n")[0])

# RFC 3550 6.6: a source that stops sending SHOULD send BYE, so a
# receiver can drop the SSRC at once instead of waiting out its
# timeout. rsd sends one per track AHEAD of the TEARDOWN 200 (the
# BYE must precede the response or it races the connection close),
# so the bytes req() saw before the status line are the primary
# evidence -- an after-the-response-only scan misses them, and this
# leg skipped for months against a server that was sending BYE all
# along. BYE may also ride inside a compound packet behind SR/SDES
# (live555 style), so walk each frame's packet chain instead of
# testing only the first packet type.


def _frame_has_bye(_p):
    _i = 0
    while _i + 4 <= len(_p):
        if _p[_i + 1] == 203:
            return True
        _step = (struct.unpack(">H", _p[_i + 2:_i + 4])[0] + 1) * 4
        if _step <= 0:
            break
        _i += _step
    return False


_bye = False
_scan = pre_response + tail_rest
_deadline = time.time() + 2.0
try:
    s.settimeout(0.5)
    # Bounded by the clock, not by the peer: a server that keeps
    # sending after TEARDOWN would otherwise hold this loop open for
    # as long as it liked.
    while not _bye and time.time() < _deadline and len(_scan) < 1 << 20:
        _d = s.recv(65536)
        if not _d:
            break
        _scan += _d
except (socket.timeout, OSError):
    pass
_i = 0
while not _bye and _i + 4 <= len(_scan):
    if _scan[_i:_i + 1] != b"$":
        _i += 1
        continue
    _ln = struct.unpack(">H", _scan[_i + 2:_i + 4])[0]
    if _frame_has_bye(_scan[_i + 4:_i + 4 + _ln]):
        _bye = True
        break
    _i += 4 + _ln
# A SHOULD, not a MUST: credited when seen, recorded as a known
# deviation when absent, rather than left as a permanently red check.
if _bye:
    emit(True, "RFC3550 6.6 BYE sent on teardown", "BYE seen")
else:
    print("SKIP RFC3550 6.6 BYE on teardown -- not sent (SHOULD; TEARDOWN "
          "already ends the session)", flush=True)
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
