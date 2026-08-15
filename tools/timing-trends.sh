#!/bin/bash
# timing-trends: turn the battery-log archive into statistics.
#
#
# Requires gawk (3-arg match, asort).
# The suite's PASS lines carry timings ("dusk reaches night (5s)"),
# but any single battery is N=1 -- quoting one run's number as THE
# number is lab-notebook talk. Every battery keeps a full transcript,
# so the repetitions already exist on disk; this walks them and prints
# count/min/median/max per timed check, which is what a quoted number
# should come from.
#
# Usage: tools/timing-trends.sh [battery-logs-dir] [name-filter]
#   tools/timing-trends.sh                      # all logs, all checks
#   tools/timing-trends.sh battery-logs dusk    # only dusk checks
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOGS=${1:-"$DIR/battery-logs"}
FILTER=${2:-}

[ -d "$LOGS" ] || { echo "no such log dir: $LOGS" >&2; exit 2; }

# Every "  PASS  <name> (<N>s...)" line across every transcript. The
# check name is the key; the first "<number>s" in the parens is the
# sample. FAIL lines are counted separately so a check that mostly
# times out cannot masquerade as fast-but-rarely-run.
grep -rhE '^  (PASS|FAIL)  .*\([0-9]+(\.[0-9]+)?s' "$LOGS" 2>/dev/null |
awk -v filter="$FILTER" '
{
    verdict = $1
    line = $0
    sub(/^  (PASS|FAIL)  /, "", line)
    name = line
    sub(/ \(.*/, "", name)
    if (filter != "" && index(name, filter) == 0) next
    if (match(line, /\(([0-9]+(\.[0-9]+)?)s/, m)) {
        if (verdict == "PASS") { v[name] = v[name] " " m[1]; n[name]++ }
        else f[name]++
    }
}
END {
    if (!length(n) && !length(f)) { print "no timed checks matched"; exit 0 }
    printf "%-55s %5s %5s %7s %7s %7s\n", "check", "runs", "fails", "min", "median", "max"
    for (name in n) {
        cnt = split(substr(v[name], 2), a, " ")
        asort(a, s, "@val_num_asc")
        med = (cnt % 2) ? s[(cnt+1)/2] : (s[cnt/2] + s[cnt/2+1]) / 2
        printf "%-55s %5d %5d %6ss %6ss %6ss\n", name, cnt, f[name]+0, s[1], med, s[cnt]
    }
    for (name in f) if (!(name in n))
        printf "%-55s %5d %5d %7s %7s %7s\n", name, 0, f[name], "-", "-", "-"
}'
