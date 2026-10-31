#!/bin/bash
# Prints the CHANGELOG.md section of a version, without its heading and the
# link definitions at the end; fails when there is none.
# Usage: changelog-section.sh <CHANGELOG.md> <x.y.z>
set -euo pipefail
notes=$(awk -v v="$2" '
    index($0, "## [" v "]") == 1 || $0 == "## " v || index($0, "## " v " ") == 1 { on = 1; next }
    on && /^## / { exit }
    on && /^\[[^]]+\]: / { next }
    on' "$1" | sed -e '/./,$!d')
if [[ -z "${notes//[$'\n' ]/}" ]]; then
    echo "::error::$1 has no section for $2 (## [$2] - YYYY-MM-DD)" >&2
    exit 1
fi
printf '%s\n' "${notes}"
