# Building a bench that can run the full battery

`raptor-test` needs nothing but a camera URL for its core checks, and
`tools/self-check.sh` needs no hardware at all — it proves the probes
fail in the right direction against a local misbehaving server, and
you should run it first on any new machine. This document is about
the last tier: the physical bench that unlocks the day/night
hardware cycle, ramped dusk/dawn, and light-source calibration.

## Minimum hardware

1. **A camera running raptor**, reachable over ssh (the day/night
   checks drive `raptorctl` remotely).
2. **A scriptable room light.** Anything you can switch and set from
   a shell command works: a PWM LED strip behind an HTTP bridge, a
   smart plug plus a dimmer, zigbee2mqtt — the suite only ever runs
   the command templates you give it. Dimmable is strongly
   preferred: on/off unlocks the transitions, dimming unlocks the
   ramps and calibration, which is where the interesting hysteresis
   coverage lives.
3. Darkness control: the camera's room must actually get dark when
   the light is off. A window defeats every dusk leg during daytime.

The reference bench uses a 12 V white LED strip on a PCA9685 PWM
chip behind a small HTTP relay-panel service; none of that is
required — only the command templates below.

## Wiring it up: the target manifest

Copy `targets/_template.conf` and fill in (see `targets/README.md`
for the full schema):

    LIGHTS_ON_CMD="curl -s -X POST ... full brightness ..."
    LIGHTS_OFF_CMD="curl -s -X POST ... dark ..."
    # printf template, %d = RAW duty level (0..LIGHTS_MAX, default
    # 4095). Raw counts, not percent: the transition region sits in
    # the last few percent, where 1% is already 41 counts.
    LIGHTS_SET_CMD="curl -s ... '{\"level\":%d}' ..."
    LIGHT_CAL_FILE="$DIR/targets/<name>.lightcal"

Multi-camera benches need the interference hooks: a second camera in
auto goes night when the room darkens and its IR floods the scene
(this produced a measured false "lit" reading of gain 441 on the
reference bench before the hooks existed):

    BENCH_QUIESCE_CMD="ssh othercam 'raptorctl ric mode day' || true"
    BENCH_RESTORE_CMD="ssh othercam 'raptorctl ric mode auto' || true"

## The methodology, in the order you should run it

1. **Self-check** (no hardware): `tools/self-check.sh` — a suite
   that cannot demonstrate its own failure direction is decorative.
2. **Calibrate** (`--light-cal --light-cal-file <f>`): forces day so
   the filter and IR stay parked, walks a half-EV raw-level ladder,
   records settled AE readings per rung, reports where the camera's
   own thresholds cross, and stores the table. Expect surprises:
   on the reference bench AE flattens a 30x duty change into the
   same luma, and one camera's entire transition lives in the bottom
   four counts of 4095. Your curve will be different — that is the
   point. Recalibrate only when the bench knowingly changes (moved
   camera, new lamp); the table is a pinned reference, not a moving
   average.
3. **Verify, every session** (`--light-verify`, and automatically
   before the daynight light legs): three rungs against the pinned
   table — full scale, mid-transition, deepest dark. Drift means
   the BENCH changed; the daynight light legs then skip with a named
   reason instead of failing as camera bugs. Every cal and verify
   appends to `<f>.history`, so slow creep shows up as a trend.
4. **Run the cycle** (`--daynight-only`, or `--daynight` inside a
   battery): with `LIGHTS_SET_CMD` present the first dusk and the
   dawn are log-spaced ramps — derived from your calibration table
   at equal measured-luma spacing when it exists — with an assertion
   that the threshold region is crossed exactly once. Flapping under
   a monotonic dim is precisely what the hysteresis machinery exists
   to prevent, and a step change can never test it. The second dusk
   stays a step so both stimulus classes run every cycle.
5. **Budgets are rig-dependent.** `--dusk-budget` / `--dawn-budget`
   (defaults 45/90 s) assume the reference lamp and sensors; a dim
   lamp or a probe-cadence-bound sensor legitimately needs more.
   A budget failure on a new rig is a tuning question before it is
   a camera bug.

## Provenance, or: which code did you just test?

`run-battery` stamps every transcript with the suite's git revision
and the checksums of both config inputs, and after each bring-up it
records the daemon's own compiled-in build hash (read from the
banner the running process printed) plus a checksum of the binary on
disk. If you deploy by hand instead, do the equivalent — a result
that cannot name the code that produced it is an anecdote with a
timestamp. The reference workflow deploys freshly built daemons over
NFS and treats the flashed image's daemons as stale by default.

## Numbers you quote

Single runs are N=1. The battery archive keeps full transcripts, so
repetitions accumulate on disk; `tools/timing-trends.sh` turns them
into count/min/median/max per timed check. Quote those, not one
run's lucky draw. Pass/fail gates are thresholded and safe at N=1;
timings are not.

## What real artifacts look like

See `examples/` — a genuine calibration table and a genuine cycle
transcript from the reference bench, sanitized only of addresses.
