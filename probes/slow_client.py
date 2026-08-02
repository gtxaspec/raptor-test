#!/usr/bin/env python3
"""Slow-client probe: a reader that never keeps up.

  slow_client.py <host> <rtsp-port> <rtsp-path> [user] [pass] [rhd-url] [secs]

Both daemons queue a response and drain it as the socket allows, so a
client that asks for data and then reads it a byte at a time parks a
send buffer for as long as it likes. Nothing in the suite exercises
that: every other client reads as fast as it can. The failure modes
are a blocked event loop (one slow reader stalls everyone) and an
unbounded queue (memory grows until the daemon dies), and the same
socket shape is what a congested WiFi viewer looks like from the
inside.

Asserts that while a deliberately slow client holds a session open, a
normal client is still served promptly, and that the daemon is healthy
afterwards. Exercises RTSP (rsd/rsd-555) and, when given, RHD.
"""
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

host = sys.argv[1]
port = int(sys.argv[2])
path = sys.argv[3]
user = sys.argv[4] if len(sys.argv) > 4 else None
password = sys.argv[5] if len(sys.argv) > 5 else None
rhd = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] != "-" else None
secs = float(sys.argv[7]) if len(sys.argv) > 7 else 12.0

sys.path.insert(0, __import__("os").path.dirname(__file__))
from rtsplib import RtspSession  # noqa: E402

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def emit(okay, name, detail=""):
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""), flush=True)


stop = threading.Event()


def slow_rtsp():
    """PLAY, then read one byte at a time with long pauses."""
    try:
        s = RtspSession(host, port, path, user=user, password=password)
        s.describe()
        s.setup("video", 0)
        s.play()
        s.sock.settimeout(1.0)
        while not stop.is_set():
            try:
                if not s.sock.recv(1):
                    break
            except (socket.timeout, OSError):
                pass
            time.sleep(0.25)
        s.close()
    except Exception:
        pass


def slow_rhd():
    """Request MJPEG, then read a byte at a time."""
    try:
        scheme, rest = rhd.split("://", 1)
        hp = rest.split("/", 1)[0]
        h = hp.split(":")[0]
        p = int(hp.split(":")[1]) if ":" in hp else (443 if scheme == "https" else 80)
        raw = socket.create_connection((h, p), timeout=6)
        sock = CTX.wrap_socket(raw, server_hostname=h) if scheme == "https" else raw
        auth = ""
        if user:
            import base64
            auth = "Authorization: Basic " + base64.b64encode(
                f"{user}:{password}".encode()).decode() + "\r\n"
        sock.sendall(f"GET /mjpeg?stream=0 HTTP/1.1\r\nHost: {h}\r\n{auth}\r\n".encode())
        sock.settimeout(1.0)
        while not stop.is_set():
            try:
                if not sock.recv(1):
                    break
            except (socket.timeout, OSError):
                pass
            time.sleep(0.25)
        sock.close()
    except Exception:
        pass


def normal_rtsp_ok():
    """A well-behaved client: DESCRIBE + PLAY + first frame, timed."""
    t0 = time.time()
    try:
        s = RtspSession(host, port, path, user=user, password=password)
        s.describe()
        s.setup("video", 0)
        s.play()
        got = 0
        for _, ch, pkt in s.packets(6):
            if ch == 0 and len(pkt) > 12:
                got += 1
                if got >= 5:
                    break
        s.close()
        return time.time() - t0, got
    except Exception as e:
        return time.time() - t0, f"error: {type(e).__name__}"


base, n0 = normal_rtsp_ok()
emit(isinstance(n0, int) and n0 >= 5, "baseline client served before the slow reader",
     f"{n0} frames in {base:.1f}s")

workers = [threading.Thread(target=slow_rtsp, daemon=True) for _ in range(2)]
if rhd:
    workers.append(threading.Thread(target=slow_rhd, daemon=True))
for w in workers:
    w.start()
time.sleep(3)  # let the slow readers settle into their PLAY

el, n = normal_rtsp_ok()
emit(isinstance(n, int) and n >= 5, "normal client still served while slow readers hold sessions",
     f"{n} frames in {el:.1f}s")
# A stalled event loop shows up as latency, not just failure.
emit(el < max(base * 3, 8.0), "slow readers do not stall a normal client",
     f"{el:.1f}s vs {base:.1f}s baseline")

if rhd:
    t0 = time.time()
    try:
        req = urllib.request.Request(rhd.rstrip("/") + "/snap?stream=0")
        if user:
            import base64
            req.add_header("Authorization", "Basic " + base64.b64encode(
                f"{user}:{password}".encode()).decode())
        with urllib.request.urlopen(req, timeout=12, context=CTX) as r:
            body = r.read()
        emit(body[:2] == b"\xff\xd8", "RHD still serves a snapshot while a slow reader holds /mjpeg",
             f"{len(body)} bytes in {time.time() - t0:.1f}s")
    except Exception as e:
        emit(False, "RHD still serves a snapshot while a slow reader holds /mjpeg",
             f"{type(e).__name__}: {e}")

time.sleep(max(0.0, secs - 3))
stop.set()
for w in workers:
    w.join(timeout=5)
time.sleep(2)  # let the server reap the abandoned sessions

el, n = normal_rtsp_ok()
emit(isinstance(n, int) and n >= 5, "server healthy after the slow readers leave",
     f"{n} frames in {el:.1f}s")
