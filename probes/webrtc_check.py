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
talkback = len(sys.argv) > 6 and sys.argv[6] == "1"

results = []


def emit(okay, name, detail=""):
    results.append(okay)
    print(("OK " if okay else "FAIL ") + name + (" -- " + detail if detail else ""), flush=True)


async def run():
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTransceiver("video", direction="recvonly")
    if talkback:
        # Talk-back: offer to SEND audio too. aiortc's stock
        # AudioStreamTrack generates 20ms silence frames, which is
        # enough to exercise rwd's receive path (Compy_AudioReceiver).
        from aiortc.mediastreams import AudioStreamTrack
        pc.addTransceiver(AudioStreamTrack(), direction="sendrecv")
    else:
        pc.addTransceiver("audio", direction="recvonly")

    v_arrivals = []   # wall-clock arrival per video frame
    v_pts = []        # frame PTS (RTP timebase)
    v_tb = [None]     # time_base of the video track
    a_frames = [0]
    a_t0 = [None]
    a_t1 = [None]
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
                        if a_t0[0] is None:
                            a_t0[0] = time.time()
                        a_t1[0] = time.time()
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
            # WHIP resource URL: the session handle a client uses to
            # release the PeerConnection (draft-ietf-wish-whip 4.2).
            resource = resp.headers.get("Location", "")

    # A well-formed answer carries the DTLS fingerprint and ICE creds.
    emit("a=fingerprint" in answer_sdp and "a=ice-ufrag" in answer_sdp,
         "WebRTC WHIP: SDP answer has DTLS fingerprint + ICE",
         f"{resp.status}, {len(answer_sdp)}B")

    if talkback:
        # The answer's audio direction tells us whether the server
        # actually accepted client audio.
        a_dir = "none"
        in_a = False
        for line in answer_sdp.splitlines():
            if line.startswith("m=audio"):
                in_a = True
            elif line.startswith("m=") :
                in_a = False
            elif in_a and line.startswith("a=") and line.strip() in (
                    "a=sendrecv", "a=recvonly", "a=sendonly", "a=inactive"):
                a_dir = line.strip()[2:]
                break
        emit(a_dir in ("sendrecv", "recvonly"),
             "WebRTC talk-back negotiated (server accepts client audio)",
             f"answer audio direction: {a_dir}")

    emit(bool(resource), "WHIP answer carries a Location resource URL", resource or "no Location header")

    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    # Collect frames for `duration`.
    try:
        await asyncio.wait_for(done.wait(), timeout=duration)
    except asyncio.TimeoutError:
        pass
    await pc.close()

    # DELETE on the resource is how a client says it is done
    # (draft-ietf-wish-whip 4.2). A server that does not honour it
    # leaks the PeerConnection until some other timeout fires, which
    # on a 4-client camera is a denied slot for the next viewer.
    if resource:
        durl = resource if "://" in resource else \
            whip_url.split("/", 3)[0] + "//" + whip_url.split("/", 3)[2] + resource
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.delete(durl, headers={k: v for k, v in headers.items()
                                                   if k != "Content-Type"}, ssl=False) as dr:
                    emit(dr.status in (200, 204, 404),
                         "WHIP DELETE releases the session", f"HTTP {dr.status}")
        except Exception as e:
            emit(False, "WHIP DELETE releases the session", f"{type(e).__name__}: {e}")

    # Audio: identify the negotiated codec from the answer and confirm
    # it decoded to real samples. rwd transcodes the ring AAC to Opus
    # (default) or G.711; a silent stream (0 samples) means the transcode
    # failed. Judged over the span audio actually flowed, not the whole
    # capture window: ICE+DTLS setup time varies by link and must not
    # read as missing samples. Sustained full-rate over >=2s proves the
    # transcode; a stalled or silent stream still fails.
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
        a_span = (a_t1[0] - a_t0[0]) if a_t0[0] is not None else 0.0
        min_span = min(2.0, duration * 0.5)
        expected = a_rate[0] * a_span * 0.8
        emit(a_frames[0] > 10 and a_span >= min_span and a_samples[0] > expected,
             "WebRTC audio decodes to samples",
             f"{acodec}: {a_frames[0]} frames, {a_samples[0]} samples @ {a_rate[0]}Hz "
             f"over {a_span:.1f}s")

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
        "audio_span_s": round((a_t1[0] - a_t0[0]), 2) if a_t0[0] is not None else 0,
        "time_base": str(v_tb[0]),
    }), flush=True)


try:
    asyncio.run(run())
except Exception as e:
    print(f"FAIL WebRTC probe -- {type(e).__name__}: {e}", flush=True)
    sys.exit(0)

sys.exit(0 if all(results) else 0)  # caller tallies; never nonzero on check failure
