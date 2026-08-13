"""Shared minimal RTSP/1.0 client for raptor-test probes.

TCP-interleaved only: probes exist to inspect exact bytes on a single
connection, which interleaving makes deterministic. Not a general
client; just enough protocol to DESCRIBE/SETUP/PLAY and iterate
interleaved RTP/RTCP frames with a deadline.
"""
import hashlib
import re
import socket
import ssl
import struct
import time

# RFC 3551 static audio payload types: sections carrying these may
# legally omit the a=rtpmap line entirely (rsd's G.711 sections do).
AUDIO_STATIC_PT_CLOCK = {0: 8000, 8: 8000, 10: 44100, 11: 44100}


def audio_clock(sdp, default=48000):
    """RTP clock of the first receivable audio section in an SDP.

    Sendonly/inactive sections (e.g. the ONVIF backchannel) are camera
    input and never carry the clock a receiver projects with, so they
    are skipped. The clock comes from the section's rtpmap, or from
    the RFC 3551 static payload-type table when there is no rtpmap.
    Assuming a fixed clock here once produced a phantom -0.8s a/v skew
    against G.711 (8kHz projected at the AAC default)."""
    for sec in re.split(r"(?m)^m=", sdp)[1:]:
        if not sec.startswith("audio"):
            continue
        if re.search(r"(?mi)^a=(sendonly|inactive)\s*$", sec):
            continue
        rm = re.search(r"(?mi)^a=rtpmap:\d+\s+[A-Za-z0-9._-]+/(\d+)", sec)
        if rm:
            return int(rm.group(1))
        m0 = re.match(r"audio\s+\d+\s+\S+\s+(\d+)", sec)
        if m0 and int(m0.group(1)) in AUDIO_STATIC_PT_CLOCK:
            return AUDIO_STATIC_PT_CLOCK[int(m0.group(1))]
        break
    return default


class RtspSession:
    def __init__(self, host, port, path, timeout=6.0, user=None, password=None, tls=False):
        self.host = host
        self.port = int(port)
        self.path = path
        scheme = "rtsps" if tls else "rtsp"
        # An IPv6 literal must be bracketed in the request-URI, and only
        # there -- the socket call below wants it bare.
        hostpart = f"[{host}]" if ":" in host else host
        self.url = f"{scheme}://{hostpart}:{port}{path}"
        self.sock = socket.create_connection((host, self.port), timeout=timeout)
        if tls:
            # Cameras ship self-signed certs; the probe verifies the
            # protocol, not the CA chain.
            cx = ssl.create_default_context()
            cx.check_hostname = False
            cx.verify_mode = ssl.CERT_NONE
            self.sock = cx.wrap_socket(self.sock, server_hostname=host)
        self.cseq = 0
        self.session_id = None
        self.preframes = []
        self.leftover = b""
        self.user = user
        self.password = password
        self.last_challenge = None

    def _digest_auth(self, method, uri):
        """Authorization header from the stored Digest challenge."""
        ch = self.last_challenge or {}
        realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
        ha1 = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (f'Authorization: Digest username="{self.user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp}"\r\n')

    def request(self, method, url=None, extra="", body_expected=True, _retried=False):
        """Send one request, return (status_line, headers_dict, body).
        With credentials set, a 401 Digest challenge is answered once."""
        self.cseq += 1
        u = url or self.url
        msg = f"{method} {u} RTSP/1.0\r\nCSeq: {self.cseq}\r\nUser-Agent: raptor-test\r\n"
        if self.session_id and "Session:" not in extra:
            msg += f"Session: {self.session_id}\r\n"
        if self.user and self.last_challenge:
            msg += self._digest_auth(method, u)
        msg += extra + "\r\n"
        self.sock.sendall(msg.encode())
        buf = self.leftover
        self.leftover = b""
        # Frames deframed while reading THIS response, in arrival order.
        # A server may legitimately send binary frames between request
        # and response (rsd emits RTCP BYE ahead of the TEARDOWN 200);
        # discarding them silently blinded the BYE compliance leg for
        # months. (ch, payload) tuples; reset on every request.
        self.preframes = []

        # Read the response as an ordered stream: interleaved $-frames
        # can arrive before OR inside a short-written response, so
        # deframe strictly from the front and accumulate head text
        # around them. The terminator is only ever searched in the
        # accumulated text, never in frame payloads (RTCP binary can
        # contain CRLFCRLF bytes, and a whole-buffer scan then splits
        # the head mid-frame -- seen against rsd-555 under load).
        head_txt = b""
        rest = b""
        while True:
            if len(buf) < 2:
                d = self.sock.recv(4096)
                if not d:
                    return "", {}, ""
                buf += d
                continue
            if buf[:1] == b"$" and buf[1] <= 30:
                while len(buf) < 4:
                    d = self.sock.recv(4096)
                    if not d:
                        return "", {}, ""
                    buf += d
                ln = struct.unpack(">H", buf[2:4])[0]
                while len(buf) < 4 + ln:
                    d = self.sock.recv(65536)
                    if not d:
                        return "", {}, ""
                    buf += d
                self.preframes.append((buf[1], buf[4:4 + ln]))
                buf = buf[4 + ln:]
                continue
            # Consume text up to the next possible frame start; keep a
            # trailing "$" in buf until its follow-up byte arrives.
            m = buf.find(b"$", 1)
            if m < 0:
                head_txt += buf
                buf = b""
            else:
                head_txt += buf[:m]
                buf = buf[m:]
            if b"\r\n\r\n" in head_txt:
                head_txt, _, extra_txt = head_txt.partition(b"\r\n\r\n")
                # Bytes past the terminator are body/stream bytes;
                # frames inside the head were already removed.
                rest = extra_txt + buf
                break
        lines = head_txt.decode(errors="replace").split("\r\n")
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
        if "401" in lines[0] and "www-authenticate" in headers:
            www = headers["www-authenticate"]
            if www.lower().startswith("digest"):
                self.last_challenge = dict(
                    (k.strip(), v.strip().strip('"'))
                    for k, v in (kv.split("=", 1)
                                 for kv in www[6:].split(",") if "=" in kv))
                if self.user and not _retried:
                    return self.request(method, url, extra, body_expected, _retried=True)
        return lines[0], headers, rest[:clen].decode(errors="replace")

    def describe(self):
        status, headers, sdp = self.request("DESCRIBE", extra="Accept: application/sdp\r\n")
        # Map each media type to its SETUP target from a=control
        # (RFC 2326 §C.1.1): backends differ (compy: video/audio,
        # live555: track1/track2), so never assume the suffix.
        base = headers.get("content-base", "").strip() or self.url
        self.tracks = {}
        media = None
        for line in sdp.splitlines():
            if line.startswith("m="):
                media = "video" if line[2:].startswith("video") else \
                        "audio" if line[2:].startswith("audio") else None
            elif line.startswith("a=control:") and media:
                ctl = line.split(":", 1)[1].strip()
                if ctl == "*":
                    self.tracks[media] = base.rstrip("/")
                elif ctl.startswith("rtsp://"):
                    self.tracks[media] = ctl
                else:
                    self.tracks[media] = base.rstrip("/") + "/" + ctl
        return status, sdp

    def track_url(self, media):
        """SETUP target for a media type, from the parsed a=control."""
        return getattr(self, "tracks", {}).get(media, self.url + "/" + media)

    def first_media(self):
        """Preferred media for single-track probes: video when the
        source has it, else audio (audio-only sources)."""
        t = getattr(self, "tracks", {})
        return "video" if "video" in t else ("audio" if "audio" in t else "video")

    def setup(self, media, ch):
        return self.request(
            "SETUP", self.track_url(media),
            extra=f"Transport: RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1}\r\n")

    def play(self):
        status, headers, _ = self.request("PLAY", extra="Range: npt=0.000-\r\n")
        rtpinfo = {}
        for part in headers.get("rtp-info", "").split(","):
            d = dict(kv.split("=", 1) for kv in part.strip().split(";") if "=" in kv)
            url = d.get("url", "").strip()
            # Resolve against the a=control URLs from DESCRIBE. The
            # suffix carries no meaning of its own (compy names tracks
            # video/audio, live555 names them trackN), and a server may
            # list tracks that are not in the SDP it just served: this
            # one advertises a third track anchored at seq=0 rtptime=0.
            # Guessing from the URL text lands that on video and
            # silently replaces the real anchor with zeros, so an
            # unrecognized track is skipped instead.
            key = None
            for media, ctl in (getattr(self, "tracks", None) or {}).items():
                if url and (url == ctl or url.rstrip("/").endswith(ctl.rsplit("/", 1)[-1])):
                    key = media
                    break
            if key is None:
                if getattr(self, "tracks", None):
                    continue
                key = "audio" if "audio" in url else "video"
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
