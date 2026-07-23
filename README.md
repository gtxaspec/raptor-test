# raptor-test

Standalone live-stream conformance suite for RTSP servers. Built for
the Raptor Streaming System; works against any RTSP source (checks
self-skip when a feature is absent).

It exercises what unit and integration tests structurally cannot:
real client sessions over TCP **and** UDP, several client
implementations, concurrent viewers with join/leave churn, wire-level
RTP inspection, timestamp/drift math, RFC conformance, NVR recording,
and recorded-clip verification. Every check exists because an ad-hoc
test once missed the bug it now catches.

## Running the whole battery on a target

`raptor-test` checks one endpoint per invocation. To run the *entire*
battery — every backend (rsd, rsd-555, SRT, RTMP, WebRTC, RHD) and
every leg — the same way on every device, use `run-battery` with a
target manifest so coverage can't drift between targets:

```sh
./run-battery t23-cinnado      # one target, all its backends
./run-battery --all            # every targets/*.conf, then a matrix
./run-battery --list
```

Each `targets/<name>.conf` says where a device is and which backends
and legs it exposes; `run-battery` runs raptor-test against each and
prints a pass/fail matrix. Legs a target doesn't expose are marked
`n/a`, never silently dropped. See `targets/README.md`. Bring-up
(build, deploy, start the daemons) is the caller's job — the runner
tests, it doesn't manage the lab.

## Usage (single endpoint)

```sh
./raptor-test rtsp://CAM:9554/ch0 \
    --sub rtsp://CAM:9554/ch1 \          # second stream for the concurrency mix
    --ssh root@CAM \                     # device health checks (raptor bench)
    --go2rtc http://GO2RTC-HOST:1984 \   # restream leg
    --frigate \                          # dockerized Frigate record + clip verify
    --record-check /path/to/recordings \ # verify an external recorder's newest clip
    --user admin --pass secret \        # digest credentials (deployed cameras)
    --rhd https://CAM:8443 \            # RHD HTTP: snapshot, MJPEG, /audio
    --srt srt://CAM:9000 \               # SRT (rsr): MPEG-TS video+audio decode
    --tls rtsps://CAM:9554/ch0 \         # RTSPS/TLS leg (needs [rtsp] tls=true)
    --rtmp 1935 \                        # receive rsp's RTMP push on this port
    --webrtc https://CAM:8554/whip?stream=1  # WebRTC via rwd WHIP (H264 stream)
```

Point the RTSP URL at any backend: raptor's compy server (`rsd`) or
its live555 server (`rsd-555`) on its own port. The probes read each
media's `a=control` from the SDP, so track naming differences between
backends are handled transparently. Two known live555 differences,
both upstream library behavior rather than rsd-555 defects: it answers
a malformed `Transport` header with 200 instead of 461
(`parseTransportHeader` defaults to RTP/UDP and ignores tokens it
doesn't recognize), so the garbage-transport robustness check reports
a failure against rsd-555; and its PAUSE check can flake under heavy
device load (one in-flight frame after PAUSE while the pre-window is
starved) — it passes consistently on an idle device.

Requires `ffmpeg`/`ffprobe` and `python3`; uses `mpv` and `docker`
when present. Run `tools/fetch-tools.sh` once — it pins an official
ffmpeg 8 static build in `tools/` (the suite prefers it over the
system ffmpeg: client RTSP behavior shifts between majors — 7.1
streamcopy silently drops RTSP AAC audio, 8.x fixed it), builds
`openRTSP` from live555 source for the live555-client leg, and
provisions `tools/webrtc-venv` (aiortc) for the `--webrtc` leg. Building
openRTSP needs `make` and a C++20-capable `g++` (fetch-tools skips it
with a warning otherwise; the live555-client leg then self-skips). An
explicit `FFMPEG`/`FFPROBE` env override still wins, and every run
logs the client version it used. Logs land in
`./raptor-test-logs/<timestamp>/` and are kept whenever anything
fails (`--keep-logs` keeps them always). Exit codes: 0 = every check
passed (skips allowed), 1 = at least one check failed (failed names
listed, logs kept), 2 = usage error.

## What it checks

| Area | Checks |
|---|---|
| Signaling | SDP structure, AAC `config=` internally consistent with the RTP clock (LC and backward-compatible HE-AAC v1 forms decoded bit-exactly) |
| Transports | Clean media over TCP-interleaved and UDP, main and sub stream |
| Wire level | RTP sequence continuity per track via a raw interleaved probe; RTP timestamp discipline: never backward, single-packet spikes triaged against wall-clock arrival (honest source gap vs timestamp anomaly), audio cadence modal share with nudge-direction split (one-sided = tracking a real source clock, two-sided = oscillating steering) |
| Timestamps | Real capture: per-track monotonicity in native timebases, A/V end alignment; audio cadence vs nominal rate in ppm (catches source-clock drift) |
| Content | Measured video fps vs nominal (within 15%), resolution sanity, decoded audio sample count vs declared rate (catches SBR/rate mislabels), GOP cadence from a raw bitstream census, per-frame SEI presence (raptor ST 0604; skipped when absent) |
| GOP adherence | Keyframes must be strictly periodic (census interval modal +/- 1 frame; drifting or elastic GOPs break recorder segmenting); with `--gop <n>` (`GOP=` in a manifest) the measured interval must equal the configured value |
| Backchannel | Auto-detected: DESCRIBE with the ONVIF Require header; if the server advertises an `a=sendonly` audio section, SETUP it and push real PCMU RTP at the server — the session must survive and, with `--ssh`, the speaker ring must exist on the device mid-session (found a real demand-accounting bug in rsd's jpeg path the day it was written) |
| MJPEG over RTSP | Auto-detected: the `/jpeg` endpoint (rsd default) must deliver decodable RFC 2435 MJPEG, not just answer signaling |
| RTSP behavior | PAUSE halts delivery and PLAY resumes; malformed requests (bogus path, garbage transport, unknown session) answered with 4xx and the server stays healthy |
| Sessions | A silently-vanished UDP client (no teardown, no keepalive, no RTCP) must stop receiving media within the advertised Session timeout and be reaped server-side; mid-session TCP-to-UDP re-SETUP either moves the media or is refused 455 with the session intact |
| Auth | (`--user/--pass`) unauthenticated DESCRIBE draws a Digest challenge, unauthenticated SETUP is refused, wrong password rejected, correct digest accepted; the whole suite then runs authenticated (probes speak digest, client legs carry URL credentials) |
| MTU | Max RTP packet size at most 1472 bytes: anything larger IP-fragments on UDP paths, so one lost fragment costs the whole packet |
| IPv6 | (`--ssh`) clean media over the target's global IPv6 address, the family production NVRs commonly attach over |
| Fault injection | Abruptly killed client (RST mid-stream) followed by clean media for the next client |
| RFC 2326 | OPTIONS Public methods, DESCRIBE/SETUP/PLAY/TEARDOWN status codes, Session header, GET_PARAMETER keepalive, RTP-Info anchors matching the first actual packets |
| RFC 4566 | `o=` origin sanity, media sections, `a=control`, sprop parameter sets for H.26x |
| RFC 3550 | Sender Reports present, compound with SDES CNAME, plausible NTP, sane cadence, cross-track NTP↔RTP mapping consistency (A/V skew), §5.1 nonzero initial seq/timestamp |
| RFC 3640 | AAC-hbr fmtp completeness |
| RFC 3551/7587 | Per-codec audio conformance: G.711 static PT 0/8 at 8 kHz, Opus rtpmap `opus/48000/2`, L16 static PT only at 44.1 kHz (dynamic otherwise) |
| Concurrency | Ladder of 2, 3, then 4 simultaneous clients on the main stream and 2/4 on the sub stream (alternating transports, every client individually verified); sustained UDP client stays clean across three join/leave cycles |
| Deployed cameras | Established sessions are baselined before any suite traffic: clean refusals at `max_clients` with external viewers (an attached NVR) become capacity skips, the leak check compares against the baseline instead of zero, and single-digit UDP loss reports as wireless-grade rather than failing the regression guards |
| Regression | Repeated default-transport ffprobe sessions (UDP dual-SETUP) followed by clean UDP media — guards the re-SETUP fd-leak and cross-wiring bug classes |
| Players | mpv over TCP and UDP: error-free logs and A-V sync < 0.1s |
| live555 client | openRTSP (VLC's RTSP lineage, a different stack than libav) negotiates a session and its dumped elementary stream decodes clean — exercises server paths ffmpeg/mpv cannot |
| RTSPS/TLS | (`--tls rtsps://host:port/path`) TLS handshake, DESCRIBE 200 over rtsps, and clean media through the encrypted transport |
| RTMP push | (`--rtmp <listen-port>`) rsp pushes to a listener on this host; the received FLV carries video and decodes clean (catches HE-AAC ASC mislabels on the push path) |
| WebRTC | (`--webrtc <whip-url>`) a real WHIP client (aiortc: SDP/ICE/DTLS-SRTP) decodes rwd's stream and measures framerate, stall stability, PTS monotonicity/cadence, and that audio decodes to real samples (Opus or G.711). Point at an **H264** stream (`?stream=N`) — rwd's WebRTC video is H264-only; signaling is HTTPS with Basic auth. Needs `tools/webrtc-venv` (fetch-tools.sh) |
| go2rtc | Clean media through a restream; AAC `config=` passed through verbatim |
| Frigate | Dockerized Frigate records via TCP and UDP inputs; recorded clips verified |
| RHD HTTP | (`--rhd`) `/snap` returns a decodable JPEG of sane size (and refuses unauthenticated access when credentials are set); `/mjpeg` is `multipart/x-mixed-replace` carrying multiple JPEG parts that decode frame-by-frame with no errors |
| RHD /audio | (`--rhd`) the HTTP audio stream (ADTS AAC, Ogg/Opus, or WAV/PCM, de-chunked) is framed correctly and decodes to PCM |
| Audio-only | Auto-detected from the SDP (m=audio, no m=video): video checks self-skip, clean-media asserts decoded time instead of frames, single-track probes SETUP the audio track, and the full transport/wire/session/RFC battery runs (validated against a live555 WAV source) |
| Soak | (`--soak <minutes>`) endurance mode replacing the battery: repeated bounded captures plus device RSS/FD trending over the run (fails on >25% RSS growth or >3 new FDs in rvd/rsd) |
| WebRTC talk-back | The WHIP offer carries a sendrecv audio track (aiortc silence); the answer direction must accept client audio and, with `--ssh`, the decoded audio must land in the device speaker ring mid-session |
| SRT | (`--srt`) connects as a caller to the SRT listener (rsr), confirms the MPEG-TS carries video, decodes the video clean, and decodes the audio all the way to PCM — the last step catches an ADTS sample-rate mislabel that a container probe would miss |
| Codec matrix | `tools/codec-matrix.sh` sweeps l16/pcmu/pcma/opus/aac against **both** RTSP backends per codec, asserting the audio RTP timestamp step equals the codec's real frame duration (a server timestamping AAC at its 20ms chunk rate decodes fine but runs the clock 3x fast) |
| Clips | Recorded files: streams present, durations aligned, full decode with zero errors (null-muxer dts nag and its repeat-tails excluded; file-level dts asserted to never move backward instead) |
| Device | (`--ssh`) rsd CPU sane after the suite, connection count back at the pre-suite baseline, daemon log lines emitted during the run free of ERROR-level entries and crashes, framesource "losted buffers" counters flat across the run |

## Design notes

- All log matching is **case-insensitive** (`Error` vs `error` once hid
  a day of HEVC join noise).
- Pipelines are never truncated mid-evidence; failing checks print the
  exact matched lines and keep their logs.
- Background clients are killed with their process tree. Killing only
  a `timeout` wrapper orphans its ffmpeg, which then streams forever
  and masquerades as server load.
- The live-decode error pattern excludes the null muxer's
  "non monotonically increasing dts" nag: `-f null` forces a 1/1000
  timebase where 30 fps frames legally collide on the same
  millisecond. Real monotonicity is asserted from captures in native
  timebases and on the wire.
- ffprobe with no transport flag intentionally probes over UDP with a
  dual-track SETUP: that exact traffic shape found two real server
  bugs the day this suite was written.
- Copy captures use `-copyinkf`: ffmpeg's streamcopy waits for a
  key-flagged packet per stream, but its RTP AAC depacketizer never
  emits one (AAC is not intra-only to ffmpeg), so a plain `-c copy`
  from ANY rtsp server -- go2rtc included -- silently records no
  audio. A dedicated check documents the plain-copy behavior.
- Wire-level RTP timestamp checks (backward steps, single-packet
  spikes, audio cadence stability) exist because a steering loop that
  bang-bangs +-1ms passes every decode check while file muxers
  silently drop packets around the jitter.
- Every external client call is wall-clock bounded. An SRT caller
  blocks forever against a listener that completes the handshake but
  never sends data (one wedged rsr hung a full battery for 40
  minutes), and ffmpeg's `-t` only limits capture *after* connect.
  Bounded captures also require ffmpeg rc=0: a mid-capture stall
  killed by `timeout` leaves an empty error log, which a grep-only
  check would read as a vacuous "decodes clean" pass.
- Target manifests are sourced bash; a quoting error aborts sourcing
  mid-file and silently drops every later field. `run-battery`
  hard-errors on a conf that fails to source instead of running a
  partial battery that still looks green.

## Codec and rate matrices

The suite validates whatever the target is configured for. To sweep a
raptor camera through its audio matrix (l16, pcmu, pcma, opus, aac at
various rates), `tools/codec-matrix.sh user@cam /path/to/conf` edits
the config over ssh, bounces rad+rsd from the NFS build, and runs the
per-codec RFC probe, wire timestamp checks, and a decode against each
combination. HW-validated matrix on T31: all five codecs conformant
with exact wire cadence (G.711 modal 160, opus modal 960 at 100%).

## Not covered (yet)

Multi-day soak (the `--soak` mode trends RSS/FDs over minutes to
hours, not days) and content verification of talk-back audio at the
far end (the suite proves arrival in the speaker ring, not what rad
plays out). The raptor provenance suite separately covers snapshots,
EXIF, signing, and rverify chains.

To self-test the audio-only mode without a camera: build
`live555MediaServer` (same source fetch-tools uses for openRTSP),
serve a **plain** WAV from its working directory (python's `wave`
module output works; ffmpeg's WAV writer adds metadata chunks live555
rejects), and point the suite at `rtsp://host:8554/<file>.wav`.
