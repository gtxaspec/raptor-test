#!/usr/bin/env python3
"""Count RTP seq gaps per channel over N seconds (TCP-interleaved)."""
import socket, sys, struct, time

host, port, path, dur = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4])
url = f"rtsp://{host}:{port}{path}"
s = socket.create_connection((host, port), timeout=5)
cseq = 0

def req(method, u, extra=""):
    global cseq
    cseq += 1
    s.sendall(f"{method} {u} RTSP/1.0\r\nCSeq: {cseq}\r\n{extra}\r\n".encode())
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
    return head.decode(errors="replace"), rest[clen:]

req("DESCRIBE", url, "Accept: application/sdp\r\n")
h, _ = req("SETUP", url + "/video", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
sess = next(l.split(":")[1].split(";")[0].strip() for l in h.split("\r\n") if l.lower().startswith("session"))
req("SETUP", url + "/audio", f"Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: {sess}\r\n")
_, leftover = req("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")

buf = leftover
last_seq = {}
count = {0: 0, 2: 0}
gaps = {0: 0, 2: 0}
t0 = time.time()
s.settimeout(3)
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
        if ch in (0, 2) and ln >= 12:
            seq = struct.unpack(">H", pkt[2:4])[0]
            if ch in last_seq:
                d = (seq - last_seq[ch]) & 0xFFFF
                if d != 1:
                    gaps[ch] += d - 1
            last_seq[ch] = seq
            count[ch] += 1
w = time.time() - t0
print(f"wall {w:.1f}s: video pkts={count[0]} seq-lost={gaps[0]} | audio pkts={count[2]} seq-lost={gaps[2]}")
print(f"audio frames expected {w/0.064:.0f}, received {count[2]} (AAC 1 pkt/frame)")
