#!/usr/bin/env python3
"""Minimal RTSP/TCP-interleaved probe: prints DESCRIBE/SETUP/PLAY
responses and the first RTP seq/timestamp seen on each channel."""
import socket, sys, struct

host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
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

h, body, _ = req("DESCRIBE", url, "Accept: application/sdp\r\n")
print("== DESCRIBE:"); print(h.split("\r\n")[0])
for l in body.splitlines():
    if l.startswith(("a=control", "m=")):
        print("  " + l)

sess = None
h, _, _ = req("SETUP", url + "/video", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
for l in h.split("\r\n"):
    if l.lower().startswith("session"):
        sess = l.split(":")[1].split(";")[0].strip()
print("== SETUP video:", h.split("\r\n")[0], "session", sess)
h, _, _ = req("SETUP", url + "/audio", f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
print("== SETUP audio:", h.split("\r\n")[0])

h, _, leftover = req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
print("== PLAY response headers:")
for l in h.split("\r\n"):
    print("  " + l)

# Read interleaved frames; report first RTP ts per channel + a few audio deltas
buf = leftover
first = {}
audio_ts = []
s.settimeout(8)
import time
t0 = time.time()
while time.time() - t0 < 6 and len(audio_ts) < 6:
    try:
        buf += s.recv(65536)
    except socket.timeout:
        break
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
        if ln >= 8 and ch in (1, 3):
            pt = pkt[1]
            if pt == 200 and ('sr'+str(ch)) not in first:
                first['sr'+str(ch)] = True
                import struct as st
                ntp_hi, ntp_lo, rtpts = st.unpack('>III', pkt[8:20])
                print(f"  RTCP SR on ch{ch}: ntp={ntp_hi}.{ntp_lo>>16} rtp_ts={rtpts}")
        if ln >= 12 and ch in (0, 2):
            seq, ts = struct.unpack(">HI", pkt[2:8])
            if ch not in first:
                first[ch] = (seq, ts)
                print(f"  first ch{ch}: seq={seq} rtptime={ts}")
            if ch == 2 and len(audio_ts) < 6:
                audio_ts.append(ts)
print("  audio ts deltas:", [audio_ts[i+1]-audio_ts[i] for i in range(len(audio_ts)-1)])
