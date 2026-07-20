#!/usr/bin/env python3
"""WebRTC/WHIP client probe: pull rwd's stream and measure the video
framerate, stability, and RTP timestamp discipline per frame.

  webrtc_check.py <whip-url> [user] [pass] [seconds] [nominal_fps]
    e.g. webrtc_check.py http://cam:8554/whip thingino thingino 10 25

Does the real WebRTC handshake (SDP offer/answer over WHIP, ICE,
DTLS-SRTP) with aiortc, then receives decoded frames. For each video
frame it records the wall-clock arrival and the frame PTS (RTP
timestamp in a 90 kHz clock). Emits OK/FAIL lines plus a JSON summary
line so a caller can extract the raw numbers.

Requires aiortc + av (see tools/fetch-tools.sh, installed into a venv;
the suite runs this probe with that venv's python).
"""
import asyncio
import json
import sys
import time

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.contrib.media import MediaBlackhole  # noqa: F401 (kept for parity)
import aiohttp

whip_url = sys.argv[1]
user = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
password = sys.argv[3] if len(sys.argv) > 3 else None
duration = float(sys.argv[4]) if len(sys.argv) > 4 else 10.0
nominal_fps = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

results = []


def emit(okay, name, detail=""):
    results.append(okay)
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""), flush=True)


async def run():
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    # Receive-only: ask for both media types.
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")

    v_arrivals = []   # wall-clock arrival per video frame
    v_pts = []        # frame PTS (RTP timebase)
    v_tb = [None]     # time_base of the video track
    a_frames = [0]
    a_samples = [0]
    a_rate = [0]
    done = asyncio.Event()

    @pc.on("track")
    def on_track(track):
        async def pump():
            try:
                while True:
                    frame = await track.recv()
                    if track.kind == "video":
                        v_arrivals.append(time.time())
                        v_pts.append(frame.pts)
                        if v_tb[0] is None:
                            v_tb[0] = frame.time_base
                    else:
                        a_frames[0] += 1
                        a_samples[0] += frame.samples
                        a_rate[0] = frame.sample_rate
            except Exception:
                done.set()
        asyncio.ensure_future(pump())

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    headers = {"Content-Type": "application/sdp"}
    if user:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{user}:{password}".encode()).decode()

    async with aiohttp.ClientSession() as sess:
        async with sess.post(whip_url, data=pc.localDescription.sdp,
                             headers=headers, ssl=False) as resp:
            if resp.status not in (200, 201):
                emit(False, "WebRTC WHIP: POST offer accepted", f"HTTP {resp.status}")
                await pc.close()
                return
            answer_sdp = await resp.text()

    # A well-formed answer carries the DTLS fingerprint and ICE creds.
    emit("a=fingerprint" in answer_sdp and "a=ice-ufrag" in answer_sdp,
         "WebRTC WHIP: SDP answer has DTLS fingerprint + ICE",
         f"{resp.status}, {len(answer_sdp)}B")

    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    # Collect frames for `duration`.
    try:
        await asyncio.wait_for(done.wait(), timeout=duration)
    except asyncio.TimeoutError:
        pass
    await pc.close()

    # Audio: identify the negotiated codec from the answer and confirm
    # it decoded to real samples. rwd transcodes the ring AAC to Opus
    # (default) or G.711; a silent stream (0 samples) means the transcode
    # failed (e.g. HE-AAC, which rwd's LC-only decoder cannot handle).
    acodec = "?"
    in_audio = False
    for line in answer_sdp.splitlines():
        if line.startswith("m=audio"):
            in_audio = True
        elif line.startswith("m=") and not line.startswith("m=audio"):
            in_audio = False
        elif in_audio and line.startswith("a=rtpmap:"):
            acodec = line.split(" ", 1)[1].strip() if " " in line else "?"
            break
    if "m=audio" in answer_sdp:
        expected = a_rate[0] * duration * 0.5  # allow half-window slack
        emit(a_frames[0] > 10 and a_samples[0] > expected,
             "WebRTC audio decodes to samples",
             f"{acodec}: {a_frames[0]} frames, {a_samples[0]} samples @ {a_rate[0]}Hz")

    n = len(v_arrivals)
    if n < 10:
        emit(False, "WebRTC video: frames received", f"only {n} in {duration:.0f}s")
        print("SUMMARY " + json.dumps({"frames": n}), flush=True)
        return

    span = v_arrivals[-1] - v_arrivals[0]
    fps = (n - 1) / span if span > 0 else 0.0

    # Framerate vs nominal (if given): within 15%, same tolerance as
    # the RTSP content check.
    if nominal_fps > 0:
        emit(abs(fps - nominal_fps) <= 0.15 * nominal_fps,
             "WebRTC video frame rate",
             f"measured {fps:.2f}fps vs nominal {nominal_fps:.2f}")
    else:
        emit(fps > 1, "WebRTC video delivers frames", f"{fps:.2f}fps")

    # Stability: no long stall between frames. A gap > 4x the median
    # inter-arrival is a hitch the viewer sees.
    gaps = [v_arrivals[i] - v_arrivals[i - 1] for i in range(1, n)]
    gaps_sorted = sorted(gaps)
    median = gaps_sorted[len(gaps_sorted) // 2]
    stalls = [g for g in gaps if g > max(0.3, 4 * median)]
    emit(len(stalls) == 0, "WebRTC video stability: no stalls",
         f"median gap {median * 1000:.0f}ms, {len(stalls)} stalls"
         + (f" (worst {max(stalls) * 1000:.0f}ms)" if stalls else ""))

    # Timestamps: PTS must be strictly monotonic and advance at a
    # steady cadence. time_base is typically 1/90000; at 25fps the
    # step is 3600.
    back = sum(1 for i in range(1, n) if v_pts[i] <= v_pts[i - 1])
    emit(back == 0, "WebRTC video timestamps monotonic",
         f"{back} non-increasing PTS steps")

    steps = [v_pts[i] - v_pts[i - 1] for i in range(1, n) if v_pts[i] > v_pts[i - 1]]
    if steps:
        steps_sorted = sorted(steps)
        modal = steps_sorted[len(steps_sorted) // 2]
        spikes = [s for s in steps if s > 8 * modal]
        emit(len(spikes) == 0, "WebRTC video timestamp cadence steady",
             f"modal step {modal}, {len(spikes)} spikes")

    print("SUMMARY " + json.dumps({
        "frames": n, "fps": round(fps, 2), "span_s": round(span, 2),
        "median_gap_ms": round(median * 1000, 1), "stalls": len(stalls),
        "pts_backward": back, "audio_frames": a_frames[0],
        "audio_samples": a_samples[0], "audio_rate": a_rate[0], "audio_codec": acodec,
        "time_base": str(v_tb[0]),
    }), flush=True)


try:
    asyncio.run(run())
except Exception as e:
    print(f"FAIL WebRTC probe -- {type(e).__name__}: {e}", flush=True)
    sys.exit(0)

sys.exit(0 if all(results) else 0)  # caller tallies; never nonzero on check failure
