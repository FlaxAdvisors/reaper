FROM python:3.12-slim AS base

# libpq5: psycopg's C library. iproute2: flax_switch_sense's --check-vip-holder
# flag (parses `ip -o -4 addr`). openssh-client: the ssh binary sshpass execs --
# WITHOUT it sshpass fails "No such file or directory" and EVERY ssh path breaks
# (flax_switch_sense cumulus driver, flax_reconcile host-ssh/bmc-ll kick rungs +
# ios write, flax_observe openbmc-ssh/host-uptime probes). sshpass + ipmitool:
# flax_observe's BMC and host probes (openbmc-ssh, ipmitool fru reads, host
# uptime checks). iputils-ping: state machine pings BMCs + hosts to set
# bmcping / nodeping vars. nmap/jq/curl: post viewer's PWR/IDNT/INV/POP actions
# shell ghost helpers (powertriage/identtriage detect AMI-vs-OpenBMC via nmap UDP
# 623/664, parse credentials-bmc.json via jq, identtriage patches Redfish
# LocationIndicatorActive via curl on OpenBMC).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    iproute2 \
    openssh-client \
    sshpass \
    ipmitool \
    iputils-ping \
    nmap \
    jq \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY flax_control /app/flax_control

# Bake schema migrations into the image so deploys don't need a host bind-mount.
# alembic runs from /app/schema; see roles/apply_flax_stack/tasks/systemd.yml.
COPY schema /app/schema

# Plan 2: bundle flax_switch_sense in the same image. flax-control's CMD
# is uvicorn (web UI); flax-switch-sense's container CMD overrides to
# python -m flax_switch_sense.
COPY flax_switch_sense /app/flax_switch_sense

# Plan 3: bundle flax_observe in the same image. flax-control's CMD is
# uvicorn (web UI); flax-switch-sense's container CMD is `python -m
# flax_switch_sense`; flax-observe's is `python -m flax_observe`.
COPY flax_observe /app/flax_observe

# Plan 4: bundle flax_classify. flax-classify's container CMD overrides
# uvicorn -> `python -m flax_classify`.
COPY flax_classify /app/flax_classify

# flax-discover: identity service (family + vm_n -> devices). Container CMD
# overrides uvicorn -> `python -m flax_discover`.
COPY flax_discover /app/flax_discover

# Plan flax-reconcile: bundle flax_reconcile. flax-reconcile's container CMD
# overrides uvicorn -> `python -m flax_reconcile`.
COPY flax_reconcile /app/flax_reconcile

# flax-post: Eindhoven Post Servers viewer. Container CMD overrides uvicorn ->
# `uvicorn flax_post.app:app --port 8446`.
COPY flax_post /app/flax_post

EXPOSE 8000

# Single uvicorn worker — flax-control is mostly IO-bound on Postgres + low concurrency.
# If concurrency becomes the bottleneck, swap to gunicorn -k uvicorn.workers.UvicornWorker.
CMD ["uvicorn", "flax_control.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
