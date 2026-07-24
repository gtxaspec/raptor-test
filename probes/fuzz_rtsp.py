#!/usr/bin/env python3
"""RTSP parser fuzz probe with liveness checking.

Throws malformed RTSP at a server — bad request lines, oversized and
truncated headers, absurd Content-Length/CSeq, garbage methods,
partial interleaved frames — and after every batch sends a well-formed
OPTIONS to confirm the server is still answering. A parser that
crashes, hangs, or wedges the accept loop is caught as a liveness
failure, not a decode error.

    fuzz_rtsp.py <host> <port> [rounds] [user] [pass] [budget_s]

Prints:
  FUZZ sent=<n> alive=<n>/<n> worst=<ms>
  OK|FAIL server survived RTSP fuzzing -- <detail>
Deterministic: mutations are index-derived, no RNG (repeatable runs).
"""
import socket
import sys
import time

HOST = sys.argv[1]
PORT = int(sys.argv[2])
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 200
BUDGET = float(sys.argv[6]) if len(sys.argv) > 6 else 60.0
URL = f"rtsp://{HOST}:{PORT}/stream0"


def mutations(i):
    """Deterministic malformed requests, one family per residue."""
    fam = i % 12
    n = i // 12 + 1
    if fam == 0:
        return f"DESCRIBE {URL} RTSP/1.0\r\nCSeq: {i}\r\nContent-Length: 999999999\r\n\r\n"
    if fam == 1:
        return f"OPTIONS {URL} RTSP/1.0\r\nCSeq: {'9' * min(n, 4000)}\r\n\r\n"
    if fam == 2:
        return "GET / HTTP/1.1\r\nHost: x\r\n\r\n"  # wrong protocol
    if fam == 3:
        return "\x00\x01\x02\x03 garbage not-a-method\r\n\r\n"
    if fam == 4:
        return f"DESCRIBE {URL} RTSP/1.0\r\n" + ("X-Pad: " + "A" * 200 + "\r\n") * min(n, 64) + "\r\n"
    if fam == 5:
        return f"SETUP {URL} RTSP/1.0\r\nCSeq: 1\r\nTransport: " + "A" * (256 * n) + "\r\n\r\n"
    if fam == 6:
        return f"DESCRIBE {URL} RTSP/1.0\r\nCSeq: 1"  # truncated, no terminator
    if fam == 7:
        return f"{'M' * min(n, 2000)} {URL} RTSP/1.0\r\nCSeq: 1\r\n\r\n"  # huge method
    if fam == 8:
        return f"PLAY {URL} RTSP/1.0\r\nCSeq: 1\r\nRange: npt=abc-xyz\r\nSession: \r\n\r\n"
    if fam == 9:
        return "\x24\x00\xff\xff" + "\xde" * 64  # bogus interleaved frame header
    if fam == 10:
        return f"DESCRIBE {URL} RTSP/1.0\r\nCSeq: -{n}\r\nContent-Length: -5\r\n\r\n"
    return f"DESCRIBE {URL}{'/' * min(n, 4000)} RTSP/1.0\r\nCSeq: 1\r\n\r\n"  # huge URI


def send_raw(payload, timeout=0.5):
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(payload.encode("latin-1", "replace"))
        try:
            s.recv(4096)  # drain whatever (or nothing)
        except socket.timeout:
            pass
        s.close()
        return True
    except (OSError, ConnectionError):
        return False


def alive(timeout=4.0):
    """A well-formed OPTIONS must draw an RTSP response. Returns ms or None."""
    t0 = time.time()
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(f"OPTIONS {URL} RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
        data = s.recv(4096)
        s.close()
        if data.startswith(b"RTSP/"):
            return (time.time() - t0) * 1000
    except (OSError, ConnectionError):
        pass
    return None


def main():
    if alive() is None:
        print("FUZZ sent=0 alive=0/0 worst=0")
        print(f"FAIL server survived RTSP fuzzing -- not answering before fuzz ({URL})")
        return
    sent = 0
    checks = 0
    alive_ok = 0
    worst = 0.0
    t_start = time.monotonic()
    for i in range(ROUNDS):
        if time.monotonic() - t_start > BUDGET:
            break  # stop cleanly on the wall-clock budget, still report
        send_raw(mutations(i))
        sent += 1
        if i % 20 == 19:  # liveness probe every 20 mutations
            checks += 1
            ms = alive()
            if ms is not None:
                alive_ok += 1
                worst = max(worst, ms)
    ms = alive()
    checks += 1
    if ms is not None:
        alive_ok += 1
        worst = max(worst, ms)

    print(f"FUZZ sent={sent} alive={alive_ok}/{checks} worst={worst:.0f}ms")
    if alive_ok == checks and worst < 2000:
        print(f"OK server survived RTSP fuzzing -- {sent} malformed reqs, "
              f"liveness {alive_ok}/{checks}, worst {worst:.0f}ms")
    else:
        print(f"FAIL server survived RTSP fuzzing -- liveness {alive_ok}/{checks}, "
              f"worst {worst:.0f}ms (crash/hang under malformed input)")


if __name__ == "__main__":
    main()
