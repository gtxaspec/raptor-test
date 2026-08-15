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
import subprocess
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
    naud = sum(1 for ln in sdp.splitlines() if ln.startswith("m=audio"))
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
    # Every payload type on the sendonly m-line: the client may pick
    # any of them (RFC 8866), so the bursts below cover what is offered
    # and silently skip what is not (rsd-555 offers PCMU alone).
    offer = bc["m"].split("RTP/AVP", 1)[-1].split()
    print(f"BC_OFFER={' '.join(offer)}")

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
        # Drain the media the session streams at us while we send: an
        # unread connection backs live555 into EWOULDBLOCK mid-frame
        # and it splices the interleaved stream (wire corruption a
        # real consuming client never sees).
        for _pkt in s.packets(0.018):
            pass
    print(f"BC_SENT={sent}", flush=True)

    # The rest of the offer, one burst per payload type. Each frame
    # drains the connection like the PCMU loop above.
    def burst(pt, payloads, ts_step):
        nonlocal seq
        bts, n = 0, 0
        for payload in payloads:
            rtp = struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, bts,
                              0x1234ABCD) + payload
            frame = b"\x24" + struct.pack("!BH", 4, len(rtp)) + rtp
            try:
                s.sock.sendall(frame)
                n += 1
            except OSError:
                break
            seq += 1
            bts += ts_step
            for _pkt in s.packets(0.012):
                pass
        return n

    if "8" in offer:
        pcma = bytes([0xD5]) * 160
        print(f"BC_SENT_PCMA={burst(8, [pcma] * 30, 160)}")
    if "114" in offer:
        l16 = struct.pack("!160h", *([1000] * 160))
        print(f"BC_SENT_L16={burst(114, [l16] * 20, 160)}")
    if "112" in offer:
        # A 1-byte packet (TOC only) is a valid 20ms WB mono frame.
        print(f"BC_SENT_OPUS={burst(112, [bytes([0x08])] * 30, 960)}")
    if "113" in offer:
        aus = []
        try:
            adts = subprocess.run(
                ["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=1", "-ar", "16000", "-ac",
                 "1", "-c:a", "aac", "-b:a", "24k", "-f", "adts", "-"],
                capture_output=True, timeout=20).stdout
            i = 0
            while i + 7 <= len(adts) and len(aus) < 12:
                if adts[i] != 0xFF or (adts[i + 1] & 0xF0) != 0xF0:
                    break
                flen = (((adts[i + 3] & 0x03) << 11) | (adts[i + 4] << 3)
                        | (adts[i + 5] >> 5))
                hdr = 7 if (adts[i + 1] & 0x01) else 9
                if (adts[i + 2] >> 2) & 0xF == 8 and flen > hdr:
                    au = adts[i + hdr:i + flen]
                    aus.append(b"\x00\x10"
                               + struct.pack("!H", len(au) << 3) + au)
                i += flen
        except Exception:
            aus = []
        if aus:
            print(f"BC_SENT_AAC={burst(113, aus, 1024)}")
        else:
            print("BC_SENT_AAC=skip")

    # Hold the session so the caller can verify device-side evidence
    # (rsd destroys the speaker ring at teardown by design). Long
    # enough for a loaded single-core unit to process the packets and
    # for the caller to poll over ssh. While holding, watch the
    # backchannel RTCP channel for the receiver report the server
    # SHOULD send about our audio (RFC 3550).
    rr_seen = False
    deadline = time.time() + 8.0
    while time.time() < deadline:
        for _off, ch, payload in s.packets(0.5):
            if ch == 5 and len(payload) >= 2 and payload[1] == 201:
                rr_seen = True
        if rr_seen and time.time() > deadline - 4.0:
            break
    print(f"BC_RR={'yes' if rr_seen else 'no'}")

    st, _, _ = s.request("GET_PARAMETER")
    print(f"BC_ALIVE={'yes' if '200' in st else code(st)}")

    # The leave compound rides ahead of the TEARDOWN 200 on the
    # backchannel RTCP channel; rtsplib keeps frames racing a
    # response in preframes exactly for checks like this.
    s.request("TEARDOWN")
    bye = False
    for ch, payload in getattr(s, "preframes", []):
        if ch != 5:
            continue
        off = 0
        while off + 4 <= len(payload):
            if payload[off + 1] == 203:
                bye = True
            off += (((payload[off + 2] << 8) | payload[off + 3]) + 1) * 4
    print(f"BC_BYE={'yes' if bye else 'no'}")
    s.close()


if __name__ == "__main__":
    main()
