#!/bin/bash
# Guards the 2026-09-25 fw-bins zombie-leak fix (memory note
# fw-bins-procsub-zombie-leak.md): rf()'s `--netrc-file <(printf ...)` forks a
# process-substitution child (the printf feeding the pipe) that bash execs
# under curl's parent. Where PID 1 is a python worker that never reaps, each
# Redfish call leaves one <defunct> process behind; enough calls exhaust a
# small container's pids cgroup (bmc_fw, 2026-09-25: forks failed, `bmc-fw-
# update version` printed "" with rc 0, 12 blades latched target_mismatch).
#
# What this suite gates:
#   a. STATIC: no scripts/*.j2 uses `--netrc-file <(` (a process substitution)
#   b. STATIC: no curl command line carries the credential itself ($SSHPASS,
#      or `$RF_USER:` as in a regression to `-u user:pass`) -- curl must read
#      the credential from stdin/a file, never argv
#   c. BEHAVIOURAL: with a fake curl on PATH, each bin's rf() still sends the
#      exact netrc machine/login/password triple and returns curl's own
#      stdout and exit code unchanged
#   d. BEHAVIOURAL: the credential never appears in curl's own argv
#
# Run: bash scripts/test-no-procsub-creds.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "ok   $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1"; }

BINS="bios-fw-update.sh.j2 bmc-fw-update.sh.j2 bmc-blade-power-cycle.sh.j2"

# ── a/b: static ───────────────────────────────────────────────────────────────
for f in $BINS; do
    if grep -n -- '--netrc-file <(' "$here/$f" >/dev/null 2>&1; then
        bad "$f: --netrc-file still uses a process substitution"
    else
        ok "$f: no --netrc-file <( process substitution"
    fi
    if grep -n 'curl ' "$here/$f" | grep -E '\$SSHPASS|\$RF_USER:' >/dev/null 2>&1; then
        bad "$f: a credential variable appears on the curl command line itself"
    else
        ok "$f: no credential variable on a curl command line"
    fi
done

# ── c/d: behavioural, against a fake curl on PATH ──────────────────────────────
# extract_rf <file> -> just the rf() function definition, so we can eval it
# standalone without sourcing (and running) the whole dispatcher at EOF.
extract_rf() {
    awk '/^rf\(\) \{/{p=1} p{print} p && /^}/{exit}' "$1"
}

fakebin="$work/fakebin"; mkdir -p "$fakebin"
cat > "$fakebin/curl" <<'STUB'
#!/bin/bash
# Records its own argv (to prove no credential lands there) and whatever the
# --netrc-file argument points at (a /dev/fd/N process-substitution pipe, a
# real tempfile, or /dev/stdin -- rf() must not care which fake curl uses).
printf '%s\n' "$*" >> "$FIX_ARGV_LOG"
netrc_arg=""; prev=""
for a in "$@"; do
    [ "$prev" = "--netrc-file" ] && netrc_arg="$a"
    prev="$a"
done
if [ "$netrc_arg" = "/dev/stdin" ]; then
    cat > "$FIX_NETRC_LOG"
elif [ -n "$netrc_arg" ]; then
    cat "$netrc_arg" > "$FIX_NETRC_LOG" 2>/dev/null
else
    : > "$FIX_NETRC_LOG"
fi
printf 'fake-body\nHTTP=200'
exit "${FIX_CURL_RC:-0}"
STUB
chmod +x "$fakebin/curl"

for f in $BINS; do
    fn=$(extract_rf "$here/$f")
    if [ -z "$fn" ]; then
        bad "$f: could not extract rf() to test it standalone"
        continue
    fi
    argv_log="$work/argv.$f"; netrc_log="$work/netrc.$f"
    out_file="$work/out.$f"; rc_file="$work/rc.$f"
    : > "$argv_log"; : > "$netrc_log"
    (
        eval "$fn"
        RF_USER=root; SSHPASS='test-dummy-not-a-real-secret'
        PATH="$fakebin:$PATH"
        export RF_USER SSHPASS PATH
        export FIX_ARGV_LOG="$argv_log" FIX_NETRC_LOG="$netrc_log"
        out=$(rf GET 10.0.0.9 /redfish/v1/Test)
        rc=$?
        printf '%s' "$out" > "$out_file"
        printf '%s' "$rc" > "$rc_file"
    )
    out=$(cat "$out_file" 2>/dev/null)
    rc=$(cat "$rc_file" 2>/dev/null)
    argv=$(cat "$argv_log" 2>/dev/null)
    netrc=$(cat "$netrc_log" 2>/dev/null)

    if [ "$rc" = "0" ] && [ "$out" = "$(printf 'fake-body\nHTTP=200')" ]; then
        ok "$f: rf() returns curl's own stdout and exit code unchanged"
    else
        bad "$f: rf() output/rc mismatch (rc=$rc out=$out)"
    fi

    if printf '%s' "$argv" | grep -q 'test-dummy-not-a-real-secret'; then
        bad "$f: credential appeared in curl's own argv"
    else
        ok "$f: credential absent from curl's own argv"
    fi

    if [ "$netrc" = "machine 10.0.0.9 login root password test-dummy-not-a-real-secret" ]; then
        ok "$f: netrc machine/login/password sent correctly"
    else
        bad "$f: netrc content wrong: [$netrc]"
    fi
done

echo "── $pass ok, $fail failed ──"
[ "$fail" -eq 0 ]
