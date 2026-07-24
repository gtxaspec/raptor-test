#!/bin/bash
# Lint gate: shellcheck (error severity) over the shell entrypoints,
# ruff + py_compile over the probes. Run before committing.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
rc=0

if command -v shellcheck >/dev/null 2>&1; then
    # Error severity only: the suite is deliberate about patterns
    # style rules the suite deliberately breaks (client arg splitting).
    shellcheck -S error "$DIR/raptor-test" "$DIR/run-battery" "$DIR"/tools/*.sh || rc=1
else
    echo "WARNING: shellcheck not installed - shell lint skipped"
fi

if command -v ruff >/dev/null 2>&1; then
    ruff check "$DIR/probes" || rc=1
else
    echo "WARNING: ruff not installed - python lint skipped"
fi

for f in "$DIR"/probes/*.py; do
    python3 -m py_compile "$f" || rc=1
done

[ $rc -eq 0 ] && echo "lint: clean" || echo "lint: FINDINGS (rc=$rc)"
exit $rc
