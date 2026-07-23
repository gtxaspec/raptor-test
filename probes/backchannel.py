#!/usr/bin/env python3
"""ONVIF RTSP backchannel probe (client -> server audio).

DESCRIBEs with the ONVIF backchannel Require header, finds the
a=sendonly audio section, SETUPs video + backchannel (TCP
interleaved), PLAYs, pushes ~1.2s of PCMU silence as RTP on the
backchannel channel, then confirms the session is still healthy.

Prints parse-friendly tokens (one per line):
  BC_DESCRIBE=<code>  BC_SENDONLY=yes|no  BC_AUDIO_SECTIONS=<n>
  BC_CODEC=<rtpmap>   BC_SETUP=<code>     BC_PLAY=<code>
  BC_SENT=<n>         BC_ALIVE=yes|<status>
Early exit after a token means the rest was not attempted.

Usage: backchannel.py <host> <port> <path> [user] [pass]
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rtsplib import RtspSession

REQUIRE = "Require: www.onvif.org/ver20/backchannel\r\n"


def code(status_line):
    parts = status_line.split(" ")
    return parts[1] if len(parts) > 1 else "none"


def main():
    host, port, path = sys.argv[1:4]
    user = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
    password = sys.argv[5] if len(sys.argv) > 5 else None
    s = RtspSession(host, port, path, user=user, password=password)

    status, headers, sdp = s.request(
        "DESCRIBE", extra="Accept: application/sdp\r\n" + REQUIRE)
    print(f"BC_DESCRIBE={code(status)}")
    if "200" not in status:
        return
    print(f"BC_SENDONLY={'yes' if 'a=sendonly' in sdp else 'no'}")
    naud = sum(1 for l in sdp.splitlines() if l.startswith("m=audio"))
    print(f"BC_AUDIO_SECTIONS={naud}")
    if "a=sendonly" not in sdp:
        return

    # Per-section parse: control URL, direction, first rtpmap
    base = headers.get("content-base", "").strip() or s.url
    sections = []
    cur = None
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            cur = {"m": line, "ctl": None, "sendonly": False, "rtpmap": None}
            sections.append(cur)
        elif cur is not None:
            if line.startswith("a=control:"):
                cur["ctl"] = line.split(":", 1)[1].strip()
            elif line == "a=sendonly":
                cur["sendonly"] = True
            elif line.startswith("a=rtpmap:") and cur["rtpmap"] is None:
                cur["rtpmap"] = line.split(":", 1)[1].strip()

    def absu(ctl):
        if not ctl or ctl == "*":
            return base.rstrip("/")
        if ctl.startswith("rtsp://"):
            return ctl
        return base.rstrip("/") + "/" + ctl

    bc = next((x for x in sections
               if x["m"].startswith("m=audio") and x["sendonly"]), None)
    vid = next((x for x in sections if x["m"].startswith("m=video")), None)
    aud = next((x for x in sections
                if x["m"].startswith("m=audio") and not x["sendonly"]), None)
    print(f"BC_CODEC={bc['rtpmap'] or 'unknown'}")

    st, _, _ = s.request(
        "SETUP", absu(vid["ctl"]) if vid else s.url + "/video",
        extra="Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n")
    if "200" not in st:
        print(f"BC_SETUP={code(st)} (video)")
        return
    if aud:
        s.request("SETUP", absu(aud["ctl"]),
                  extra="Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\n"
                  + REQUIRE)
    st, _, _ = s.request(
        "SETUP", absu(bc["ctl"]),
        extra="Transport: RTP/AVP/TCP;unicast;interleaved=4-5\r\n" + REQUIRE)
    print(f"BC_SETUP={code(st)}")
    if "200" not in st:
        return
    st, _ = s.play()
    print(f"BC_PLAY={code(st)}")
    if "200" not in st:
        return

    # ~1.2s of PCMU silence, 20ms frames, RTP over interleaved ch 4
    seq, ts, sent = 0, 0, 0
    for _ in range(60):
        rtp = struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF, ts,
                          0x1234ABCD) + b"\xff" * 160
        frame = b"\x24" + struct.pack("!BH", 4, len(rtp)) + rtp
        try:
            s.sock.sendall(frame)
            sent += 1
        except OSError:
            break
        seq += 1
        ts += 160
        time.sleep(0.018)
    print(f"BC_SENT={sent}")

    st, _, _ = s.request("GET_PARAMETER")
    print(f"BC_ALIVE={'yes' if '200' in st else code(st)}")
    s.request("TEARDOWN")
    s.close()


if __name__ == "__main__":
    main()
