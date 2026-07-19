#!/usr/bin/env python3
"""Session lifecycle probes that need UDP transport and long waits.

Modes:
  reap   <host> <port> <path> [user] [pass]
      SETUP video over UDP, PLAY, then go completely silent (no
      keepalive, no RTCP, control connection left open). A server
      that honors its advertised Session timeout must stop the media
      flow and close the session; one that does not leaks a client
      slot for every silently-vanished viewer (WiFi dropouts do this
      constantly). Emits OK/FAIL lines like rfc_check.

  switch <host> <port> <path> [user] [pass]
      SETUP video TCP-interleaved, PLAY, verify frames, then
      re-SETUP the same track over UDP mid-session and verify the
      media follows the transport switch. This is the traffic shape
      that exposed the fd cross-wiring regression.
"""
import socket
import sys
import time

from rtsplib import RtspSession, parse_rtp

mode, host, port, path = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
user = sys.argv[5] if len(sys.argv) > 5 else None
password = sys.argv[6] if len(sys.argv) > 6 else None


def emit(okay, name, detail=""):
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""),
          flush=True)


def udp_pair():
    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("0.0.0.0", 0))
    p = rtp.getsockname()[1]
    if p % 2:
        rtp.close()
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rtp.bind(("0.0.0.0", p + 1))
        except OSError:
            rtp.bind(("0.0.0.0", 0))
        p = rtp.getsockname()[1]
    rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rtcp.bind(("0.0.0.0", p + 1))
    except OSError:
        rtcp.bind(("0.0.0.0", 0))
    return rtp, rtcp, p, rtcp.getsockname()[1]


def udp_count(sock, secs, drain_first=False):
    if drain_first:
        # Packets that arrived while we slept sit in the kernel buffer;
        # drop them so we only measure fresh arrivals.
        sock.settimeout(0.05)
        while True:
            try:
                sock.recv(65536)
            except socket.timeout:
                break
    n = 0
    sock.settimeout(0.5)
    end = time.time() + secs
    while time.time() < end:
        try:
            sock.recv(65536)
            n += 1
        except socket.timeout:
            continue
    return n


if mode == "reap":
    s = RtspSession(host, port, path, user=user, password=password)
    s.describe()
    rtp, rtcp, p0, p1 = udp_pair()
    st, hdrs, _ = s.request("SETUP", s.track_url("video"),
                            extra=f"Transport: RTP/AVP;unicast;client_port={p0}-{p1}\r\n")
    if "200" not in st:
        emit(False, "reap: UDP SETUP", st)
        sys.exit(0)
    adv = 60
    for part in hdrs.get("session", "").split(";"):
        if part.strip().startswith("timeout="):
            adv = int(part.strip()[8:])
    s.request("PLAY", extra="Range: npt=0.000-\r\n")
    n0 = udp_count(rtp, 3)
    emit(n0 > 10, "reap: media flows before going silent", f"{n0} pkts/3s, timeout={adv}s")
    if adv > 120:
        print(f"SKIP reap: advertised timeout {adv}s too long to wait", flush=True)
        sys.exit(0)
    # Total silence: no keepalive, no RTCP. Wait out the timeout.
    time.sleep(adv + 8)
    n1 = udp_count(rtp, 4, drain_first=True)
    emit(n1 == 0, "reap: silent session stops receiving media after timeout",
         f"{n1} pkts in 4s probe window")
    # The server should have torn the session down; the control socket
    # is dead (closed) or the session id is now invalid.
    dead = False
    try:
        s.sock.settimeout(3)
        s.sock.sendall(b"")
        st2, _, _ = s.request("GET_PARAMETER")
        dead = any(c in st2 for c in ("454", "455", "400")) or st2 == ""
    except OSError:
        dead = True
    emit(dead, "reap: session torn down server-side", st2 if not dead else "connection closed/invalid")
    s.close()

elif mode == "switch":
    s = RtspSession(host, port, path, user=user, password=password)
    s.describe()
    st, _, _ = s.setup("video", 0)
    if "200" not in st:
        emit(False, "switch: TCP SETUP", st)
        sys.exit(0)
    s.play()
    frames = 0
    for _t, ch, pkt in s.packets(3):
        if ch == 0 and parse_rtp(pkt):
            frames += 1
    emit(frames > 10, "switch: interleaved media before switch", f"{frames} pkts/3s")
    rtp, rtcp, p0, p1 = udp_pair()
    st, _, _ = s.request("SETUP", s.track_url("video"),
                         extra=f"Transport: RTP/AVP;unicast;client_port={p0}-{p1}\r\n")
    if "200" not in st:
        # Mid-session transport change may be refused (455): that is
        # legal, but the session must stay usable afterwards.
        okpost, _, _ = s.request("GET_PARAMETER")
        emit("455" in st and "200" in okpost,
             "switch: mid-session re-SETUP refused cleanly (455), session intact",
             f"setup={st}")
        s.request("TEARDOWN")
        s.close()
        sys.exit(0)
    s.request("PLAY", extra="Range: npt=0.000-\r\n")
    n = udp_count(rtp, 4)
    emit(n > 10, "switch: media follows TCP->UDP re-SETUP", f"{n} UDP pkts/4s")
    leftover_tcp = 0
    for _t, ch, _pkt in s.packets(2):
        if ch == 0:
            leftover_tcp += 1
    emit(leftover_tcp < 10, "switch: interleaved path quiesced after switch",
         f"{leftover_tcp} stray interleaved pkts/2s")
    s.request("TEARDOWN")
    s.close()

else:
    print(f"FAIL unknown mode {mode}")
    sys.exit(1)
