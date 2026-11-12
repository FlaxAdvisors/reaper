# reaper bash completion, generated from the CLI's own command table so it can
# never drift from it. Source from ~/.bashrc:
#   source ~/git/reaper/scripts/reaper-completion.bash
source <("$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../reaper" completion)
