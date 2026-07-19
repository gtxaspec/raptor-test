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
    return rest[clen:]
req("DESCRIBE", url, "Accept: application/sdp\r\n")
r = req("SETUP", url + "/video", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
# session id from prior response header — redo properly
s.close()
s = socket.create_connection((host, port), timeout=5)
cseq = 0
def req2(method, u, extra=""):
    global cseq
    cseq += 1
    s.sendall(f"{method} {u} RTSP/1.0\r\nCSeq: {cseq}\r\n{extra}\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head, rest = buf.split(b"\r\n\r\n", 1)
    clen = 0
    sess = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length"):
            clen = int(line.split(b":")[1])
        if line.lower().startswith(b"session"):
            sess = line.split(b":")[1].split(b";")[0].strip().decode()
    while len(rest) < clen:
        rest += s.recv(4096)
    return sess, rest[clen:]
req2("DESCRIBE", url, "Accept: application/sdp\r\n")
sess, _ = req2("SETUP", url + "/video", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
_, leftover = req2("PLAY", url, f"Session: {sess}\r\nRange: npt=0.000-\r\n")
buf = leftover
t0 = time.time()
frames = []  # (wall, ) per marker packet
s.settimeout(2)
while time.time() - t0 < dur:
    try: buf += s.recv(65536)
    except socket.timeout: continue
    while len(buf) >= 4:
        if buf[0:1] != b"$":
            buf = buf[1:]; continue
        ch = buf[1]; ln = struct.unpack(">H", buf[2:4])[0]
        if len(buf) < 4 + ln: break
        pkt = buf[4:4+ln]; buf = buf[4+ln:]
        if ch == 0 and ln >= 12 and (pkt[1] & 0x80):  # marker = frame end
            frames.append(time.time() - t0)
gaps = [(round((b-a)*1000,1), round(a,1)) for a,b in zip(frames, frames[1:]) if (b-a) > 0.08]
print(f"{len(frames)} frames, arrival gaps >80ms (ms, at s): {gaps[:10]}")
