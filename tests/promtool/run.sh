#!/bin/bash
# Run every *.test.yml here against reaper's health rules, in <bang>'s prometheus container.
set -euo pipefail
bang=${1:-bang-gouda}
here=$(cd "$(dirname "$0")" && pwd)
rules=$HOME/git/reaper/roles/apply_health_compose/files/prometheus
gen=$(mktemp -d)
trap 'rm -rf "$gen"' EXIT
~/.venv-reaper-ansible/bin/python "$HOME/git/reaper/scripts/health_board.py" --site eindhoven --out "$gen" --domain bangs.example.net >/dev/null
tar -C "$here" -cf - $(cd "$here" && ls *.test.yml) -C "$rules/rules" . -C "$rules" recording-rules.yml -C "$gen/rules" . \
  | ssh "$bang" 'docker exec -i prod-health-prometheus-1 sh -c "d=\$(mktemp -d) && tar -C \$d -xf - && cd \$d && promtool check rules \$(ls *.yml | grep -v test.yml) 2>&1 | grep -v SUCCESS | grep -v \"^Checking\" ; promtool test rules *.test.yml; rc=\$?; rm -rf \$d; exit \$rc"'
