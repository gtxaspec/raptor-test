#!/usr/bin/env python3
"""RHD HTTP endpoint probe: JPEG snapshot and MJPEG stream.

  rhd_check.py <base-url> [user] [pass]
    e.g. rhd_check.py https://cam:8443 thingino thingino

Checks:
  - GET /snap returns 200 with a JPEG body (SOI marker, sane size)
  - GET /mjpeg returns multipart/x-mixed-replace with per-part
    Content-Type image/jpeg and at least two complete JPEG frames
  - with credentials, an unauthenticated /snap is refused 401

Writes the snapshot to <out>/snap.jpg and an MJPEG sample to
<out>/mjpeg.bin (path from RHD_OUT env, default cwd) so the caller
can decode them with ffprobe/ffmpeg. Emits OK/FAIL lines.
"""
import base64
import os
import socket
import ssl
import sys
import time

base = sys.argv[1].rstrip("/")
user = sys.argv[2] if len(sys.argv) > 2 else None
password = sys.argv[3] if len(sys.argv) > 3 else None
out = os.environ.get("RHD_OUT", ".")

scheme, rest = base.split("://", 1)
hostport = rest.split("/", 1)[0]
host = hostport.split(":")[0]
port = int(hostport.split(":")[1]) if ":" in hostport else (443 if scheme == "https" else 80)


def emit(okay, name, detail=""):
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""), flush=True)


def http_get(path, with_auth=True, read_secs=0):
    """Return (status_int, headers_dict, body_bytes). If read_secs > 0,
    keep reading the body for that long (streaming endpoints)."""
    sock = socket.create_connection((host, port), timeout=8)
    if scheme == "https":
        cx = ssl.create_default_context()
        cx.check_hostname = False
        cx.verify_mode = ssl.CERT_NONE
        sock = cx.wrap_socket(sock, server_hostname=host)
    req = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
    if with_auth and user:
        tok = base64.b64encode(f"{user}:{password}".encode()).decode()
        req += f"Authorization: Basic {tok}\r\n"
    req += "\r\n"
    sock.sendall(req.encode())
    buf = b""
    sock.settimeout(2)
    deadline = time.time() + (read_secs if read_secs else 8)
    while time.time() < deadline:
        try:
            d = sock.recv(65536)
        except socket.timeout:
            if read_secs:
                continue
            break
        if not d:
            break
        buf += d
        if not read_secs and b"\r\n\r\n" in buf:
            head = buf.split(b"\r\n\r\n", 1)[0]
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length"):
                    clen = int(line.split(b":")[1])
            if clen and len(buf) - len(head) - 4 >= clen:
                break
    sock.close()
    if b"\r\n\r\n" not in buf:
        return 0, {}, b""
    head, body = buf.split(b"\r\n\r\n", 1)
    lines = head.decode(errors="replace").split("\r\n")
    status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 else 0
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


# -- /snap --
st, hdrs, body = http_get("/snap")
ctype = hdrs.get("content-type", "")
jpeg_ok = st == 200 and body[:3] == b"\xff\xd8\xff" and len(body) > 1000
emit(jpeg_ok, "RHD /snap returns a JPEG",
     f"HTTP {st}, {len(body)} bytes, {ctype}")
if jpeg_ok:
    with open(os.path.join(out, "snap.jpg"), "wb") as f:
        f.write(body)

# -- auth enforcement (only meaningful when creds are configured) --
if user:
    st_na, _, _ = http_get("/snap", with_auth=False)
    emit(st_na == 401, "RHD /snap refuses unauthenticated access", f"HTTP {st_na}")

# -- /mjpeg --
st, hdrs, body = http_get("/mjpeg", read_secs=6)
ctype = hdrs.get("content-type", "")
multipart = "multipart/x-mixed-replace" in ctype and "boundary=" in ctype
frames = body.count(b"\xff\xd8\xff")
part_ct = body.lower().count(b"content-type: image/jpeg")
emit(multipart, "RHD /mjpeg is multipart/x-mixed-replace", ctype or "(no content-type)")
emit(frames >= 2 and part_ct >= 2,
     "RHD /mjpeg carries multiple JPEG parts",
     f"{frames} SOI markers, {part_ct} image/jpeg parts in 6s")
if frames >= 1:
    with open(os.path.join(out, "mjpeg.bin"), "wb") as f:
        f.write(body)
