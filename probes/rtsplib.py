"""Shared minimal RTSP/1.0 client for raptor-test probes.

TCP-interleaved only: probes exist to inspect exact bytes on a single
connection, which interleaving makes deterministic. Not a general
client; just enough protocol to DESCRIBE/SETUP/PLAY and iterate
interleaved RTP/RTCP frames with a deadline.
"""
import socket
import struct
import time


class RtspSession:
    def __init__(self, host, port, path, timeout=6.0):
        self.host = host
        self.port = int(port)
        self.path = path
        self.url = f"rtsp://{host}:{port}{path}"
        self.sock = socket.create_connection((host, self.port), timeout=timeout)
        self.cseq = 0
        self.session_id = None
        self.leftover = b""

    def request(self, method, url=None, extra="", body_expected=True):
        """Send one request, return (status_line, headers_dict, body)."""
        self.cseq += 1
        u = url or self.url
        msg = f"{method} {u} RTSP/1.0\r\nCSeq: {self.cseq}\r\nUser-Agent: raptor-test\r\n"
        if self.session_id and "Session:" not in extra:
            msg += f"Session: {self.session_id}\r\n"
        msg += extra + "\r\n"
        self.sock.sendall(msg.encode())
        buf = self.leftover
        self.leftover = b""
        # Interleaved $-frames may arrive between request and response;
        # skip them while hunting for the RTSP header block.
        while True:
            while buf[:1] == b"$":
                if len(buf) < 4:
                    buf += self.sock.recv(4096)
                    continue
                ln = struct.unpack(">H", buf[2:4])[0]
                while len(buf) < 4 + ln:
                    buf += self.sock.recv(65536)
                buf = buf[4 + ln:]
            if b"\r\n\r\n" in buf:
                break
            d = self.sock.recv(4096)
            if not d:
                return "", {}, ""
            buf += d
        head, rest = buf.split(b"\r\n\r\n", 1)
        lines = head.decode(errors="replace").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        clen = int(headers.get("content-length", "0") or 0) if body_expected else 0
        while len(rest) < clen:
            rest += self.sock.recv(4096)
        self.leftover = rest[clen:]
        if "session" in headers and not self.session_id:
            self.session_id = headers["session"].split(";")[0].strip()
        return lines[0], headers, rest[:clen].decode(errors="replace")

    def describe(self):
        status, headers, sdp = self.request("DESCRIBE", extra="Accept: application/sdp\r\n")
        return status, sdp

    def setup(self, track, ch):
        return self.request(
            "SETUP", self.url + "/" + track,
            extra=f"Transport: RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1}\r\n")

    def play(self):
        status, headers, _ = self.request("PLAY", extra="Range: npt=0.000-\r\n")
        rtpinfo = {}
        for part in headers.get("rtp-info", "").split(","):
            d = dict(kv.split("=", 1) for kv in part.strip().split(";") if "=" in kv)
            key = "audio" if "audio" in d.get("url", "") else "video"
            try:
                rtpinfo[key] = (int(d["seq"]), int(d["rtptime"]))
            except (KeyError, ValueError):
                pass
        return status, rtpinfo

    def packets(self, duration):
        """Yield (wall_offset, channel, payload) for interleaved frames
        until `duration` seconds elapse."""
        buf = self.leftover
        self.leftover = b""
        t0 = time.time()
        self.sock.settimeout(2)
        while time.time() - t0 < duration:
            try:
                buf += self.sock.recv(65536)
            except socket.timeout:
                continue
            while len(buf) >= 4:
                if buf[:1] != b"$":
                    buf = buf[1:]
                    continue
                ch = buf[1]
                ln = struct.unpack(">H", buf[2:4])[0]
                if len(buf) < 4 + ln:
                    break
                yield time.time() - t0, ch, buf[4:4 + ln]
                buf = buf[4 + ln:]
        self.leftover = buf

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def parse_rtp(payload):
    """(seq, timestamp, marker) from an RTP packet, or None."""
    if len(payload) < 12:
        return None
    seq, ts = struct.unpack(">HI", payload[2:8])
    return seq, ts, bool(payload[1] & 0x80)


def parse_sr(payload):
    """(ntp_float, rtp_ts, has_sdes_compound) from an RTCP SR, or None."""
    if len(payload) < 28 or payload[1] != 200:
        return None
    ntp_hi, ntp_lo, rtpts = struct.unpack(">III", payload[8:20])
    srlen = (struct.unpack(">H", payload[2:4])[0] + 1) * 4
    compound = len(payload) > srlen + 1 and payload[srlen + 1] == 202
    return ntp_hi + ntp_lo / 2**32, rtpts, compound
