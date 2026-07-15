#!/usr/bin/env bash
# Source from ~/.bashrc: source ~/reaper/scripts/reaper-completion.bash

_reaper_complete() {
  local cur reaper_root
  cur="${COMP_WORDS[COMP_CWORD]}"
  reaper_root="$(dirname "$(realpath "${COMP_WORDS[0]}" 2>/dev/null || echo "${COMP_WORDS[0]}")")"

  local subcommands="fetch drift push plan pushids setup"

  # Level 1: subcommand
  if [[ $COMP_CWORD -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "$subcommands" -- "$cur") )
    return
  fi

  local subcmd="${COMP_WORDS[1]}"
  [[ "$subcmd" == "setup" ]] && return

  # Level 2: site
  if [[ $COMP_CWORD -eq 2 ]]; then
    local sites
    sites=$(ls "${reaper_root}/inventories/" 2>/dev/null | grep -v '^\.')
    COMPREPLY=( $(compgen -W "$sites" -- "$cur") )
    return
  fi

  # Level 3: devices (filtered by group; exclude already-typed)
  local site="${COMP_WORDS[2]}"
  local group
  [[ "$subcmd" == "pushids" ]] && group="jumpboxes" || group="all"

  local all_hosts=""
  all_hosts=$(ansible-inventory -i "${reaper_root}/inventories/${site}" --list 2>/dev/null \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
group = sys.argv[1]
if group == 'jumpboxes':
    hosts = d.get('jumpboxes', {}).get('hosts', [])
else:
    hosts = list(d['_meta']['hostvars'].keys())
print('\n'.join(hosts))
" "$group" 2>/dev/null) || true

  # Fallback: grep hosts.yml
  if [[ -z "$all_hosts" ]]; then
    all_hosts=$(grep -E '^\s+\w[\w-]+:' \
      "${reaper_root}/inventories/${site}/hosts.yml" 2>/dev/null \
      | sed 's/[: ]//g') || true
  fi

  # Exclude already-typed hosts
  local typed_hosts="${COMP_WORDS[*]:3}"
  local filtered_hosts=""
  for h in $all_hosts; do
    [[ " $typed_hosts " != *" $h "* ]] && filtered_hosts+=" $h"
  done

  COMPREPLY=( $(compgen -W "$filtered_hosts" -- "$cur") )
}

complete -F _reaper_complete reaper
complete -F _reaper_complete ./reaper
