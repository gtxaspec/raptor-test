# Example artifacts

Real outputs from the reference bench, with hostnames sanitized.
Nothing here is fabricated: each file is a genuine run, edited only to
replace lab addresses.

- `t31-example.lightcal` — a light-source calibration table from a
  T31 camera (`raptor-test --light-cal`). Read it bottom-up: AE
  flattens the entire bright span into one luma (a 30x duty change is
  invisible to the camera), and the whole day/night transition lives
  in the last few counts. This is why the ramps and the verification
  rungs are derived from the table instead of hardcoded, and why the
  ladder is raw duty counts rather than percent.
- `daynight-transcript.txt` — a full day/night hardware cycle
  (`--daynight-only`) on a T20 camera with a calibrated ramp: manual
  verbs, a ramped dusk with the single-transition hysteresis
  assertion, IR checks, dark hold, ramped dawn, a step dusk (both
  stimulus classes in one cycle), and dark re-acquisition.

To reproduce the method on your own hardware, see `../BENCH.md`.
