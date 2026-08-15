#!/usr/bin/env python3
"""Misbehaving RTSP/RTP server for raptor-test's self-check.

Serves a pre-encoded H.264 elementary stream over TCP-interleaved RTP
(RFC 6184) and, on command, breaks exactly one property so the suite's
probes can be checked for firing in the right direction.

    evil_server.py <h264.es> <port> [mode]

Modes:
  good        correct stream (baseline: every probe must PASS)
  silent      PLAY -> 200 OK but never send a packet (the vacuous
              "clean media" trap: capture must FAIL, not pass)
  seqdrop     skip RTP sequence numbers periodically
  tsback      step the RTP timestamp backward periodically
  tsspike     inject a lone huge timestamp jump (no wall-clock gap)
  garbage200  answer a malformed Transport with 200 instead of 4xx

TCP-interleaved only by design — the probes it feeds (seq_gap,
rfc_check, an ffmpeg capture) all speak interleaved. Single video
track, control name "video".
"""
import base64
import socket
import threading
import struct
import sys
import time

ES = sys.argv[1]
PORT = int(sys.argv[2])
MODE = sys.argv[3] if len(sys.argv) > 3 else "good"
CLOCK = 90000
FPS = 30
STEP = CLOCK // FPS  # 3000
MTU = 1400
SEND_LOCK = threading.Lock()


def load_nals(path):
    data = open(path, "rb").read()
    nals, i, n = [], 0, len(data)
    while i < n:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        k = data.find(b"\x00\x00\x01", j + 3)
        if k < 0:
            k = n
        nal = data[j + 3:k]
        while nal.endswith(b"\x00"):
            nal = nal[:-1]
        if nal:
            nals.append(nal)
        i = k
    return nals


NALS = load_nals(ES)
SPS = next((x for x in NALS if (x[0] & 0x1F) == 7), b"")
PPS = next((x for x in NALS if (x[0] & 0x1F) == 8), b"")
# Frames = access units; approximate one NAL of type 1/5 per frame,
# carrying any leading parameter/SEI NALs with it.
FRAMES, cur = [], []
for nal in NALS:
    t = nal[0] & 0x1F
    cur.append(nal)
    if t in (1, 5):
        FRAMES.append(cur)
        cur = []
if cur:
    FRAMES.append(cur)


def sdp(host):
    sp = base64.b64encode(SPS).decode()
    pp = base64.b64encode(PPS).decode()
    profile = SPS[1:4].hex() if len(SPS) >= 4 else "42c01f"
    body = (
        "v=0\r\n"
        f"o=- 0 0 IN IP4 {host}\r\n"
        "s=evil\r\n"
        "t=0 0\r\n"
        "a=control:*\r\n"
        "m=video 0 RTP/AVP 96\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 packetization-mode=1;profile-level-id={profile};"
        f"sprop-parameter-sets={sp},{pp}\r\n"
        "a=control:video\r\n"
    )
    return body


def rtp_packets(seq, ts, nal, marker):
    """Yield RTP payloads for one NAL: single or FU-A fragments."""
    if len(nal) <= MTU:
        yield struct.pack("!BBHII", 0x80, (96 | 0x80) if marker else 96,
                          seq & 0xFFFF, ts, 0xDEADBEEF) + nal
        return
    hdr = nal[0]
    fu_ind = (hdr & 0xE0) | 28
    typ = hdr & 0x1F
    body, first = nal[1:], True
    while body:
        chunk, body = body[:MTU], body[MTU:]
        last = not body
        fu_hdr = (0x80 if first else 0) | (0x40 if last else 0) | typ
        m = (96 | 0x80) if (marker and last) else 96
        yield struct.pack("!BBHII", 0x80, m, seq & 0xFFFF, ts,
                          0xDEADBEEF) + bytes([fu_ind, fu_hdr]) + chunk
        seq += 1
        first = False


def rtcp_sr(conn, ts, pkt_count, oct_count):
    """Minimal compound SR + SDES(CNAME) on interleaved channel 1."""
    ntp_hi = 0x83AA7E80  # a plausible fixed epoch; probes check sanity not exactness
    sr = struct.pack("!BBHIIIIII", 0x80, 200, 6, 0xDEADBEEF,
                     ntp_hi, 0, ts, pkt_count, oct_count)
    cname = b"evil@self"
    sdes_body = struct.pack("!IBB", 0xDEADBEEF, 1, len(cname)) + cname
    pad = (4 - (len(sdes_body) + 1) % 4) % 4
    sdes_body += b"\x00" * (pad + 1)
    sdes = struct.pack("!BBH", 0x81, 202, len(sdes_body) // 4) + sdes_body
    comp = sr + sdes
    try:
        with SEND_LOCK:
            conn.sendall(b"\x24\x01" + struct.pack("!H", len(comp)) + comp)
    except (OSError, struct.error):
        pass


def stream(conn, stop):
    seq, ts, fno = 1, 10000, 0
    pkt_count = oct_count = 0
    last_sr = time.time()
    period = 1.0 / FPS
    loops = max(1, (25 * FPS) // max(1, len(FRAMES)))
    for au in (FRAMES * loops):  # ~25s of frames, enough for any probe
        if stop.is_set():
            return
        # Prepend SPS/PPS every 30 frames so late-joining ffmpeg decodes.
        send_nals = list(au)
        if fno % 30 == 0 and SPS and PPS:
            send_nals = [SPS, PPS] + send_nals

        this_ts = ts
        if MODE == "tsback" and fno % 30 == 15:
            this_ts = (ts - 20 * STEP) & 0xFFFFFFFF
        elif MODE == "tsspike" and fno % 30 == 15:
            this_ts = (ts + 4000 * STEP) & 0xFFFFFFFF

        for idx, nal in enumerate(send_nals):
            marker = idx == len(send_nals) - 1
            for pkt in rtp_packets(seq, this_ts, nal, marker):
                try:
                    with SEND_LOCK:
                        conn.sendall(b"\x24\x00" + struct.pack("!H", len(pkt)) + pkt)
                except OSError:
                    return
                seq += 1
                if MODE == "seqdrop" and seq % 40 == 0:
                    seq += 5  # gap the sequence space
        pkt_count += 1
        oct_count += sum(len(x) for x in send_nals)
        if time.time() - last_sr >= 1.0:
            rtcp_sr(conn, this_ts, pkt_count, oct_count)
            last_sr = time.time()
        ts = (ts + STEP) & 0xFFFFFFFF
        fno += 1
        time.sleep(period)


def handle(conn):
    conn.settimeout(30)
    host = conn.getsockname()[0]
    buf = b""
    stop_ev = threading.Event()
    stream_thread = [None]
    while True:
        try:
            d = conn.recv(4096)
        except OSError:
            return
        if not d:
            return
        buf += d
        while b"\r\n\r\n" in buf:
            req, buf = buf.split(b"\r\n\r\n", 1)
            lines = req.decode(errors="replace").split("\r\n")
            method = lines[0].split(" ")[0]
            cseq = next((ln.split(":", 1)[1].strip()
                         for ln in lines if ln.lower().startswith("cseq")), "0")
            transport = next((ln.split(":", 1)[1].strip()
                              for ln in lines if ln.lower().startswith("transport")), "")

            def reply(extra="", code="200 OK"):
                with SEND_LOCK:
                    conn.sendall(
                        f"RTSP/1.0 {code}\r\nCSeq: {cseq}\r\n{extra}\r\n".encode())

            if method == "OPTIONS":
                reply("Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n")
            elif method == "DESCRIBE":
                body = sdp(host).encode()
                with SEND_LOCK:
                    conn.sendall(
                        f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
                        f"Content-Type: application/sdp\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            elif method == "SETUP":
                # Reject a malformed transport with 4xx — unless the
                # garbage200 mode is deliberately lax.
                bad = "RTP/AVP" not in transport
                if bad and MODE != "garbage200":
                    reply(code="461 Unsupported Transport")
                else:
                    reply("Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
                          "Session: 12345678\r\n")
            elif method == "PLAY":
                reply("Session: 12345678\r\nRTP-Info: url=video;seq=1;rtptime=10000\r\n")
                if MODE != "silent" and stream_thread[0] is None:
                    stream_thread[0] = threading.Thread(
                        target=stream, args=(conn, stop_ev), daemon=True)
                    stream_thread[0].start()
                # keep reading control (GET_PARAMETER, PAUSE, TEARDOWN)
            elif method == "GET_PARAMETER":
                reply("Session: 12345678\r\n")
            elif method == "PAUSE":
                reply("Session: 12345678\r\n")
            elif method == "TEARDOWN":
                stop_ev.set()
                reply()
                return
            else:
                reply(code="405 Method Not Allowed")


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(4)
    print(f"evil_server mode={MODE} port={PORT} frames={len(FRAMES)}", flush=True)
    # One thread per connection: real servers accept concurrently, and
    # rfc_check's transport-spec probes open a second connection while
    # the main one is held. Serial accept left that probe in the
    # backlog until it timed out and (before it was hardened) killed
    # the whole probe -- the fixture must not model a misbehavior no
    # mode asked for.
    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            return
        threading.Thread(target=serve_one, args=(conn,), daemon=True).start()


def serve_one(conn):
    try:
        handle(conn)
    except Exception:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
