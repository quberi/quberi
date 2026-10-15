#!/usr/bin/env sh
# Drift check: are the ORM models in sync with the latest Alembic head?
#
# ``alembic check`` compares Base.metadata against the current DB (which must
# be at head). It exits non-zero if the models describe a schema that would
# require a new migration to produce. Run this before every commit that
# touches ORM models.
#
# Requires QUBERI_POSTGRES_DSN pointing at a DB already migrated to head.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -z "${QUBERI_POSTGRES_DSN:-}" ]; then
  echo "error: QUBERI_POSTGRES_DSN must be set" >&2
  exit 1
fi

if [ -x "$API_ROOT/.venv/bin/alembic" ]; then
  ALEMBIC="$API_ROOT/.venv/bin/alembic"
else
  ALEMBIC="alembic"
fi

cd "$API_ROOT"

if "$ALEMBIC" check; then
  echo "[check-drift] models match head. no migration needed."
  exit 0
fi

cat >&2 <<'MSG'

[check-drift] FAILED
The ORM models describe changes that are not captured by any migration.
Generate one with:

    alembic revision --autogenerate -m "<short slug>"

Then review the file under apps/api/alembic/versions/ before committing.
MSG
exit 1
