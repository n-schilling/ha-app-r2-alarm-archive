#!/bin/bash
# Passes when CI (.github/workflows/ci.yaml) passed for a commit: the release
# workflow's guard before it tags anything. CI does not run on main; it ran
# when the commit was pushed to dev (or in a pull request), so its check runs
# are on the same sha. Branch protection asks for them as well, but a private
# repository on GitHub Free has none, so this is the check that counts.
#
# Usage: ci-passed.sh <owner/repo> <sha>     (gh with GH_TOKEN; checks: read, actions: read)
#
# Passes when there is at least one check run of CI for the sha and each of
# them ended in success or skipped; of each job name only the newest counts,
# so a job that passed when run again counts as passed. Waits while one is
# queued or running, up to CI_WAIT seconds (default 1200). Check runs of other
# workflows, the release workflow's own among them, do not count.
set -euo pipefail

repo=${1:?owner/repo}
sha=${2:?sha}
deadline=$((SECONDS + ${CI_WAIT:-1200}))

while :; do
    # The check suites of CI's workflow runs for this commit
    suites=$(gh api "repos/${repo}/actions/runs?head_sha=${sha}&per_page=100" \
        --jq '[.workflow_runs[] | select(.path == ".github/workflows/ci.yaml") | .check_suite_id]')
    runs=$(gh api "repos/${repo}/commits/${sha}/check-runs?filter=latest&per_page=100" \
        | jq --argjson suites "${suites}" \
            '[.check_runs[] | select(.check_suite.id as $id | $suites | index($id))]
             | group_by(.name) | map(max_by(.id) | {name, status, conclusion})')
    count=$(jq 'length' <<< "${runs}")
    if [[ ${count} -eq 0 ]]; then
        echo "::error::No CI check runs for ${sha}. CI does not run on main: push the commit to dev, wait for green CI, then push it to main."
        exit 1
    fi
    jq -r '.[] | "  \(.name): \(.conclusion // .status)"' <<< "${runs}"
    waiting=$(jq '[.[] | select(.status != "completed")] | length' <<< "${runs}")
    if [[ ${waiting} -eq 0 ]]; then
        break
    fi
    if (( SECONDS >= deadline )); then
        echo "::error::CI for ${sha} still runs (${waiting} of ${count} check runs), no release"
        exit 1
    fi
    echo "Waiting for ${waiting} of ${count} CI check runs"
    sleep 30
done

failed=$(jq -r '[.[] | select(.conclusion != "success" and .conclusion != "skipped") | .name] | join(", ")' <<< "${runs}")
if [[ -n ${failed} ]]; then
    echo "::error::CI did not pass for ${sha}: ${failed}; no release"
    exit 1
fi
echo "CI passed for ${sha} (${count} check runs)"
