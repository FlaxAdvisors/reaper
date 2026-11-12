#!/usr/bin/env bash
# The operator's shell environment for reaper, for the user running it. `reaper setup` runs
# this (after the git hooks and ~/.ssh/config.d/reaper-jumphosts); ./setup gets it from there.
# Idempotent: a line already present is left as it is.
#   scripts/shell-env.sh [clone-dir]     default: the clone this script sits in
#
#   - ~/.ssh/config: Includes for the jump hosts and this clone's ssh/<site> files
#   - `reaper` on PATH: a symlink in ~/.local/bin or ~/bin to the MAIN clone's shim
#   - ~/.bashrc: reaper tab completion, WCOLL + PDSH_RCMD_TYPE for pdsh, and a pip
#     --user PATH line written for python3.11 moved to python3.12 (the one interpreter)
#   - ~/jumphosts.txt (pdsh's WCOLL) from the jump-host ssh config
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"       # helpers beside this script
REAPER_DIR="$(cd "${1:-$HERE/..}" && pwd)"
BASHRC=~/.bashrc
[ -f "$BASHRC" ] || touch "$BASHRC"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
added() { printf '\033[1;33m  +\033[0m %s\n' "$*"; }

# Helper: append a line to ~/.bashrc if not already present.
ensure_bashrc_line() {
    local line="$1"
    local label="${2:-$line}"
    if ! grep -qxF "$line" "$BASHRC"; then
        printf '\n%s\n' "$line" >> "$BASHRC"
        added "$label  →  $BASHRC"
    else
        ok "already in $BASHRC: $label"
    fi
}

# A `$(python3.11 -m site --user-base)/bin` PATH line (pip --user scripts) from the 3.11
# days: same directory, but it names a retired interpreter. Moved to python3.12 in place
# (the original kept as ~/.bashrc.reaper-bak).
python_user_base_to_312() {
    if grep -q 'python3\.11 -m site --user-base' "$BASHRC"; then
        sed -i.reaper-bak 's/python3\.11 -m site --user-base/python3.12 -m site --user-base/g' "$BASHRC"
        added "PATH line: python3.11 -m site --user-base -> python3.12  →  $BASHRC (was saved as $BASHRC.reaper-bak)"
    fi
}

# `reaper` on PATH: a symlink in ~/.local/bin (or ~/bin, whichever is on PATH) to this
# clone's shim, so `reaper ...` works from any directory (the shim resolves its root with
# readlink -f). Only from the MAIN clone: `reaper` on PATH is what deploys, so running
# setup inside a worktree must not repoint it at a branch. An existing link elsewhere,
# or a real file, is reported and left alone.
link_reaper_on_path() {
    local gd cd_
    gd="$(cd "$REAPER_DIR" && git rev-parse --path-format=absolute --git-dir 2>/dev/null || git rev-parse --git-dir)"
    cd_="$(cd "$REAPER_DIR" && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || git rev-parse --git-common-dir)"
    if [[ "$gd" != "$cd_" ]]; then
        ok "reaper on PATH: skipped in a worktree (run reaper setup in the main clone)"
        return 0
    fi
    local dir="" d
    for d in "$HOME/.local/bin" "$HOME/bin"; do
        case ":$PATH:" in *":$d:"*) dir="$d"; break ;; esac
    done
    if [[ -z "$dir" ]]; then
        dir="$HOME/.local/bin"
        ensure_bashrc_line 'export PATH="$HOME/.local/bin:$PATH"' "PATH += ~/.local/bin"
    fi
    mkdir -p "$dir"
    local link="$dir/reaper" want="$REAPER_DIR/reaper"
    if [[ -L "$link" ]]; then
        if [[ "$(readlink -f "$link")" == "$(readlink -f "$want")" ]]; then
            ok "reaper on PATH: $link"
        else
            printf '\033[1;31m  !\033[0m %s -> %s (another clone); left alone. rm it and re-run reaper setup to repoint\n' \
                "$link" "$(readlink -f "$link")"
        fi
    elif [[ -e "$link" ]]; then
        printf '\033[1;31m  !\033[0m %s exists and is not a symlink; left alone\n' "$link"
    else
        ln -s "$want" "$link"
        added "reaper on PATH: $link -> $want"
    fi
}

# ~/.ssh/config Includes: the rendered jump hosts (~/.ssh/config.d/reaper-jumphosts: names
# -> tailscale IPs from the site vaults, so bare bang names work from anywhere, a bang
# included, without any FQDN in the repo) and this clone's ssh/<site> device files.
ssh_includes() {
    local SSH_CONFIG=~/.ssh/config TOOLPY="${TOOLPY:-python3.12}"
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    if ! [ -f "$SSH_CONFIG" ]; then
        touch "$SSH_CONFIG"
        chmod 600 "$SSH_CONFIG"    
    fi

    # reaper's Includes go at the TOP of ~/.ssh/config: OpenSSH treats an Include after
    # a Host/Match line as part of that block, so appending at the end only applied
    # them to the last host. scripts/ssh-includes.py moves/adds them above every
    # Host/Match block, treats ~/x and /home/<u>/x as one file, keeps a .reaper-bak.
    ssh_includes=("~/.ssh/config.d/reaper-jumphosts")
    for inv_dir in "$REAPER_DIR/inventories"/*/; do
        site=$(basename "$inv_dir")
        if [[ -f "$REAPER_DIR/ssh/$site" ]]; then
            ssh_includes+=("$REAPER_DIR/ssh/$site")
        fi
    done
    # A bang clone is made under umask 0002, and OpenSSH refuses a group-writable Include
    # ("Bad owner or permissions": then EVERY ssh fails). Fix the modes now, and after each
    # pull/checkout (git rewrites a changed file under the umask) with two small hooks.
    # A hook that isn't ours is left alone.
    chmod go-w "$REAPER_DIR"/ssh/* 2>/dev/null || true
    hooks="$(cd "$REAPER_DIR" && git rev-parse --git-path hooks)"   # worktree-safe; git >= 2.5
    [[ "$hooks" == /* ]] || hooks="$REAPER_DIR/$hooks"
    mkdir -p "$hooks"
    for h in post-merge post-checkout; do
        if [[ ! -e "$hooks/$h" ]] || grep -q 'reaper-ssh-modes' "$hooks/$h"; then
            printf '%s\n' '#!/bin/sh' \
                '# reaper-ssh-modes (written by reaper setup): OpenSSH refuses a group-writable Include' \
                'chmod go-w "$(git rev-parse --show-toplevel)"/ssh/* 2>/dev/null' 'exit 0' > "$hooks/$h"
            chmod 0755 "$hooks/$h"
        fi
    done
    "$TOOLPY" "$HERE/ssh-includes.py" "$SSH_CONFIG" "${ssh_includes[@]}"
}

jumphosts_txt() {
    local cfg=~/.ssh/config.d/reaper-jumphosts
    if [[ -f "$cfg" ]]; then
        grep '^Host ' "$cfg" | cut -d' ' -f2 > ~/jumphosts.txt
        ok "wrote ~/jumphosts.txt ($(wc -l < ~/jumphosts.txt | tr -d ' ') entries)"
    fi
}

info "Shell environment ($(id -un)): ~/.ssh/config, reaper on PATH, ~/.bashrc"
ssh_includes
link_reaper_on_path
python_user_base_to_312
ensure_bashrc_line ". $REAPER_DIR/scripts/reaper-completion.bash" \
    "source reaper-completion.bash"
ensure_bashrc_line "export WCOLL=~/jumphosts.txt" \
    "export WCOLL"
ensure_bashrc_line "export PDSH_RCMD_TYPE=ssh" \
    "export PDSH_RCMD_TYPE"
jumphosts_txt
