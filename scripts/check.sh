#!/bin/bash
# Runs locally what the CI workflow runs, so a push only confirms what already
# passed here: the changelog check, shellcheck, ruff, the tests, and the image
# built and started for this machine's architecture. Keep the steps in step
# with .github/workflows/ci.yaml.
#
# Usage: scripts/check.sh [--no-image]
#
# The tools come from .venv when there is one (python3 -m venv .venv &&
# .venv/bin/pip install --require-hashes -r r2_alarm_archive/requirements.txt &&
# .venv/bin/pip install -r requirements_test.txt), otherwise from PATH. The app configuration linter
# (frenck/action-addon-linter) runs in CI only. The image step needs Docker;
# CI builds both architectures.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

tool() { # tool <name>: the one in .venv, else the one on PATH
    if [[ -x ".venv/bin/$1" ]]; then
        echo ".venv/bin/$1"
    elif command -v "$1"; then
        :
    else
        echo "$1 not found, neither in .venv nor on PATH; see README, section Development." >&2
        return 1
    fi
}

RUFF=$(tool ruff) || exit 2
PYTEST=$(tool pytest) || exit 2

no_image=false
for arg in "$@"; do
    case "$arg" in
        --no-image) no_image=true ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done

failed=()
skipped=()

run() { # run <name> <command...>
    local name=$1
    shift
    echo "==> $name"
    if "$@"; then
        echo "    ok: $name"
    else
        echo "    FAILED: $name"
        failed+=("$name")
    fi
    echo
}

version=$(sed -n 's/^version: "\(.*\)"$/\1/p' r2_alarm_archive/config.yaml)

run "Changelog has the version $version" bash -c \
    '.github/scripts/changelog-section.sh r2_alarm_archive/CHANGELOG.md "$1" > /dev/null' _ "$version"
if command -v shellcheck > /dev/null; then
    run "Shell scripts" shellcheck --shell=bash --severity=warning \
        r2_alarm_archive/rootfs/etc/s6-overlay/s6-rc.d/archive/run \
        r2_alarm_archive/rootfs/etc/s6-overlay/s6-rc.d/archive/finish scripts/check.sh
else
    skipped+=("Shell scripts (no shellcheck)")
fi
run "Ruff" "$RUFF" check r2_alarm_archive/rootfs/usr/lib/r2_archive tests scripts
run "Tests" "$PYTEST" -q

start_without_options() {
    local out
    docker build -q --build-arg "BUILD_VERSION=${version}" -t r2-alarm-archive:check r2_alarm_archive > /dev/null \
        || return 1
    out=$(docker run --rm --network none --user r2archive -e PYTHONPATH=/usr/lib --entrypoint python3 \
        r2-alarm-archive:check -m r2_archive 2>&1 || true)
    echo "    ${out}"
    grep -q '/data/options.json not readable' <<< "${out}"
}

if $no_image; then
    skipped+=("Build and start (--no-image)")
elif ! docker info > /dev/null 2>&1; then
    skipped+=("Build and start (Docker is not running)")
else
    run "Build and start" start_without_options
fi

echo "===================="
# The ${arr[@]+"${arr[@]}"} form keeps an empty array from tripping `set -u` in the bash 3.2 of macOS
for name in ${skipped[@]+"${skipped[@]}"}; do
    echo "skipped: $name"
done
if ((${#failed[@]})); then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All checks passed."
