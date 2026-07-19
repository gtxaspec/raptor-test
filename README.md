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

## Usage

```sh
./raptor-test rtsp://CAM:9554/ch0 \
    --sub rtsp://CAM:9554/ch1 \          # second stream for the concurrency mix
    --ssh root@CAM \                     # device health checks (raptor bench)
    --go2rtc http://GO2RTC-HOST:1984 \   # restream leg
    --frigate \                          # dockerized Frigate record + clip verify
    --record-check /path/to/recordings   # verify an external recorder's newest clip
```

Requires `ffmpeg`/`ffprobe` and `python3`; uses `mpv` and `docker`
when present. Run `tools/fetch-tools.sh` once to pin an official
ffmpeg 8 static build in `tools/` -- the suite prefers it over the
system ffmpeg (client RTSP behavior shifts between majors: 7.1
streamcopy silently drops RTSP AAC audio, 8.x fixed it). An explicit
`FFMPEG`/`FFPROBE` env override still wins, and every run logs the
client version it used. Logs land in
`./raptor-test-logs/<timestamp>/` and are kept whenever anything
fails (`--keep-logs` keeps them always).

## What it checks

| Area | Checks |
|---|---|
| Signaling | SDP structure, AAC `config=` internally consistent with the RTP clock (LC and backward-compatible HE-AAC v1 forms decoded bit-exactly) |
| Transports | Clean media over TCP-interleaved and UDP, main and sub stream |
| Wire level | RTP sequence continuity per track via a raw interleaved probe |
| Timestamps | Real capture: per-track monotonicity in native timebases, A/V end alignment; audio cadence vs nominal rate in ppm (catches source-clock drift) |
| Content | Measured video fps vs nominal (within 15%), resolution sanity, decoded audio sample count vs declared rate (catches SBR/rate mislabels), GOP cadence from a raw bitstream census, per-frame SEI presence (raptor ST 0604; skipped when absent) |
| RTSP behavior | PAUSE halts delivery and PLAY resumes; malformed requests (bogus path, garbage transport, unknown session) answered with 4xx and the server stays healthy |
| Fault injection | Abruptly killed client (RST mid-stream) followed by clean media for the next client |
| RFC 2326 | OPTIONS Public methods, DESCRIBE/SETUP/PLAY/TEARDOWN status codes, Session header, GET_PARAMETER keepalive, RTP-Info anchors matching the first actual packets |
| RFC 4566 | `o=` origin sanity, media sections, `a=control`, sprop parameter sets for H.26x |
| RFC 3550 | Sender Reports present, compound with SDES CNAME, plausible NTP, sane cadence, cross-track NTP↔RTP mapping consistency (A/V skew), §5.1 nonzero initial seq/timestamp |
| RFC 3640 | AAC-hbr fmtp completeness |
| Concurrency | Ladder of 2, 3, then 4 simultaneous clients on the main stream and 2/4 on the sub stream (alternating transports, every client individually verified); sustained UDP client stays clean across three join/leave cycles |
| Regression | Repeated default-transport ffprobe sessions (UDP dual-SETUP) followed by clean UDP media — guards the re-SETUP fd-leak and cross-wiring bug classes |
| Players | mpv over TCP and UDP: error-free logs and A-V sync < 0.1s |
| go2rtc | Clean media through a restream; AAC `config=` passed through verbatim |
| Frigate | Dockerized Frigate records via TCP and UDP inputs; recorded clips verified |
| Clips | Recorded files: streams present, durations aligned, full decode with zero errors |
| Device | (`--ssh`) rsd CPU sane after the suite, no leaked connections |

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

## Not covered (yet)

Session-timeout reaping (needs a 60s idle wait), RTSPS/TLS and
authenticated sessions, backchannel audio, and multi-hour soak runs.
Candidates for a `--soak` mode. The raptor provenance suite separately
covers snapshots, EXIF, signing, and rverify chains.
