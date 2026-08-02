#!/usr/bin/env python3
"""RHD cold-start snapshot probe.

  rhd_cold.py <base-url> [user] [pass] [idle_secs]
    e.g. rhd_cold.py https://cam:8443 thingino thingino 25

With [jpeg] idle on (the default), an unwatched JPEG channel's encoder
is stopped, so the first /snap after a quiet period pays an encoder
start plus a frame period before there is anything to return. Two
distinct faults live here, and both are invisible to a warm probe:

  - the request must still be answered, not time out
  - waiting for it must not stall the daemon, which serves every
    client from one thread

The second is measured by firing two cold requests at different
streams at once: served concurrently the wall clock is the slower of
the two, served inline it is their sum. Run this before any other
JPEG traffic -- an MJPEG viewer holds the encoder up and there is no
cold path left to test.
"""
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
user = sys.argv[2] if len(sys.argv) > 2 else None
password = sys.argv[3] if len(sys.argv) > 3 else None
idle = float(sys.argv[4]) if len(sys.argv) > 4 else 25.0

# The daemon's own budget is 5s; allow for TLS setup and a slow link.
BUDGET_S = 8.0
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def emit(okay, name, detail=""):
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""), flush=True)


def snap(stream):
    """(elapsed, status, body) for one /snap request."""
    req = urllib.request.Request(f"{base}/snap?stream={stream}")
    if user:
        import base64 as b64
        tok = b64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=BUDGET_S + 4, context=CTX) as r:
            return time.time() - t0, r.status, r.read()
    except urllib.error.HTTPError as e:
        return time.time() - t0, e.code, b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return time.time() - t0, 0, str(e).encode()


print(f"idling {idle:.0f}s so the JPEG encoder stops...", flush=True)
time.sleep(idle)

el, st, body = snap(0)
jpeg = body[:2] == b"\xff\xd8"
emit(st == 200 and jpeg and el < BUDGET_S, "RHD cold /snap answered after idle",
     f"HTTP {st}, {len(body)} bytes, {el:.2f}s (budget {BUDGET_S:.0f}s)")

# Second request while the encoder is still warm: this separates "cold
# start is slow" from "cold start is broken".
el2, st2, body2 = snap(0)
emit(st2 == 200 and body2[:2] == b"\xff\xd8", "RHD warm /snap still served",
     f"HTTP {st2}, {el2:.2f}s")

print(f"idling {idle:.0f}s again for the concurrency leg...", flush=True)
time.sleep(idle)

res = {}


def worker(stream):
    res[stream] = snap(stream)


t0 = time.time()
threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
for t in threads:
    t.start()
for t in threads:
    t.join()
wall = time.time() - t0

both_ok = all(res[i][1] == 200 and res[i][2][:2] == b"\xff\xd8" for i in res)
emit(both_ok, "RHD two cold snapshots both served",
     ", ".join(f"stream{i}: HTTP {res[i][1]} {res[i][0]:.2f}s" for i in sorted(res)))

if both_ok:
    slowest = max(res[i][0] for i in res)
    total = sum(res[i][0] for i in res)
    # Serialised, the wall clock is the sum; concurrent, it is the
    # slower request plus scheduling noise. Half way between the two
    # separates them without being brittle about either.
    emit(wall < (slowest + total) / 2,
         "RHD cold snapshots served concurrently, not head-of-line",
         f"wall {wall:.2f}s vs slowest {slowest:.2f}s, serial would be {total:.2f}s")
else:
    emit(False, "RHD cold snapshots served concurrently, not head-of-line",
         "skipped: a request failed")
