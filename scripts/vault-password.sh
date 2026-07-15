#!/usr/bin/env bash
set -euo pipefail
site="${REAPER_SITE:-braintree}"
cat "${HOME}/.vault_pass_${site}"
