# Test targets

One file per lab device, sourced by `run-battery`. Each defines where
the device is and which backends/legs it exposes, so the **same full
battery** runs on every target — nothing is chosen ad-hoc at run time.

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
