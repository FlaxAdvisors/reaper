#!/bin/bash
# Apply per-service role passwords. Runs after Postgres is healthy.
# Idempotent: ALTER ROLE is safe to repeat.
set -euo pipefail

if [ -z "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
  echo "POSTGRES_SUPERUSER_PASSWORD must be set" >&2
  exit 1
fi

# Roles created by migration 001; we just set passwords here.
ROLES=(
  "flax_control:${FLAX_CONTROL_DB_PASSWORD:-}"
  "flax_switch_sense:${FLAX_SWITCH_SENSE_DB_PASSWORD:-}"
  "flax_discover:${FLAX_DISCOVER_DB_PASSWORD:-}"
  "flax_classify:${FLAX_CLASSIFY_DB_PASSWORD:-}"
  "flax_reconcile:${FLAX_RECONCILE_DB_PASSWORD:-}"
  "flax_observe:${FLAX_OBSERVE_DB_PASSWORD:-}"
  "flax_post:${FLAX_POST_DB_PASSWORD:-}"
  "flax_kea_hook:${FLAX_KEA_HOOK_DB_PASSWORD:-}"
  "kea:${KEA_DB_PASSWORD:-}"
)

for entry in "${ROLES[@]}"; do
  role="${entry%%:*}"
  pw="${entry#*:}"
  if [ -z "$pw" ]; then
    echo "(skip: no password supplied for $role)" >&2
    continue
  fi
  # Use psql via the postgres container's local socket
  docker compose -f /etc/flax/flax-stack/docker-compose.yml exec -T -e PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" \
    postgres psql -U postgres -d flax -v ON_ERROR_STOP=1 \
    -c "ALTER ROLE ${role} WITH ENCRYPTED PASSWORD '${pw}';"
done

# Replication role for streaming HA (NOT created by migration 001 — it is a
# cluster-level role with the REPLICATION attribute, not a flax schema role).
# Only provisioned when REPLICATOR_PASSWORD is supplied (HA sites). Created if
# missing, then password set; both idempotent. On a standby this whole script
# is skipped by the caller (read-only — roles arrive via pg_authid replication).
if [ -n "${REPLICATOR_PASSWORD:-}" ]; then
  docker compose -f /etc/flax/flax-stack/docker-compose.yml exec -T -e PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" \
    postgres psql -U postgres -d flax -v ON_ERROR_STOP=1 \
    -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='replicator') THEN CREATE ROLE replicator WITH REPLICATION LOGIN; END IF; END \$\$;"
  docker compose -f /etc/flax/flax-stack/docker-compose.yml exec -T -e PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" \
    postgres psql -U postgres -d flax -v ON_ERROR_STOP=1 \
    -c "ALTER ROLE replicator WITH ENCRYPTED PASSWORD '${REPLICATOR_PASSWORD}';"
  # Let replicator drive pg_rewind during failback (so the operator runbook needs
  # no superuser conninfo over the peer's mgmt IP — pg_hba only permits postgres
  # on loopback). These are exactly the functions pg_rewind invokes. Grants are
  # cluster-global + replicate, so every node's replicator can rewind. Idempotent.
  docker compose -f /etc/flax/flax-stack/docker-compose.yml exec -T -e PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" \
    postgres psql -U postgres -d flax -v ON_ERROR_STOP=1 \
    -c "GRANT EXECUTE ON function pg_ls_dir(text, boolean, boolean) TO replicator;
        GRANT EXECUTE ON function pg_stat_file(text, boolean) TO replicator;
        GRANT EXECUTE ON function pg_read_binary_file(text) TO replicator;
        GRANT EXECUTE ON function pg_read_binary_file(text, bigint, bigint, boolean) TO replicator;"
else
  echo "(skip: no REPLICATOR_PASSWORD — standalone, no HA replication)" >&2
fi

echo "ok: role passwords provisioned" >&2
