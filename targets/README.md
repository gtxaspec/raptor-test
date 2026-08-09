# Test targets

One file per lab device, sourced by `run-battery`. Each defines where
the device is and which backends/legs it exposes, so the **same full
battery** runs on every target — nothing is chosen ad-hoc at run time.

**No on-device config is ever read or edited.** Every bring-up pushes
the repo's stock template (`STOCK_CONF`, default
`../raptor/config/raptor.conf`) to `/tmp` on the device, applies the
pass's section enables and `CONF_SET` entries to a fresh copy, and
launches the daemons with `-c /tmp/raptor-test.conf`. The baseline
therefore always matches the code under test — a device's own config
may predate it — and a battery's config inputs are exactly two
reviewable things: the template in the repo and the manifest. A
target states every deviation from stock in `CONF_SET` (fleet shape
like `ring.refmode`, credentials for the auth legs). Nothing on the
device can skew a run, nothing a run does persists, and there is no
backup/restore step to forget. On-device testing outside the battery
should follow the same rule.

**The baseline enables every feature the suite can exercise** —
backchannel, MJPEG-over-RTSP, and so on — regardless of what any
distribution ships enabled. The battery tests what the code supports,
not what a product turns on; a feature off in `CONF_SET` is a leg
that silently stops running. The only structured exception is the
signing pass: SEI and snapshot signing change what every decoder leg
receives, so they live in their own pass instead of the baseline.

Copy `_template.conf` to `<name>.conf` and fill it in. Site confs
(`targets/*.conf`) are deliberately untracked -- they hold your lab's
IPs and layout and never belong in the repo; only the template is.
Fields left
empty (or `-`) mark a leg as not-applicable for that target; the
runner records it as `n/a` rather than silently skipping it.

The files are **sourced by bash**: quote any value containing spaces
or parentheses (`NOTES` especially). A syntax error aborts sourcing
mid-file — bash silently drops every field after the bad line — so
`run-battery` refuses to run a conf that fails to source and reports
the target as `CONF PARSE ERROR` instead of running a partial battery.

| Field | Meaning |
|-------|---------|
| `SOC` | SoC family (t23, t31, t41, …) — for the record only |
| `HOST` | device IP |
| `SSH` | `user@host` for device-health checks, or empty |
| `RTSP_PORT` / `PATH0` / `PATH1` | rsd (compy) port and stream paths |
| `SRT` | `srt://host:port` for the rsr leg, or empty |
| `R555_PORT` / `R555_PATH0` / `R555_PATH1` | rsd-555 (live555) backend, or empty |
| `RHD` | `https://host:port` base for the RHD snapshot/MJPEG/`/audio` leg, or empty |
| `USER` / `PASS` | RTSP digest credentials, or empty |
| `GO2RTC` | go2rtc API url for the restream leg, or empty |
| `FRIGATE` | `yes` to run the dockerized Frigate NVR leg |
| `RECORD_CHECK` | directory of an external recorder's clips, or empty |
| `TLS` | `rtsps://host:port/path` for the TLS leg, or empty |
| `RTMP` | local listen port the device's rsp pushes to (rsp must run on the device with `[push] url` pointed at this host), or empty |
| `WEBRTC` | rwd WHIP url (`https://host:8554/whip?stream=N`, an **H264** stream) for the WebRTC leg, or empty — needs `tools/webrtc-venv` |
| `NOTES` | free text, **quoted** |

Run one target across every backend it exposes:

```sh
./run-battery t23-cinnado
```

Run all defined targets and print a matrix:

```sh
./run-battery --all
```
