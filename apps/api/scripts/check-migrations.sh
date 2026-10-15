#!/usr/bin/env sh
# Round-trip smoke test for Alembic migrations.
#
# Rebuilding the schema from an empty DB up to head, back down to base, and
# back up to head again catches three classes of bug at zero test-framework
# cost:
#   1. A migration that doesn't apply cleanly (missing constraint, name too
#      long, invalid SQL).
#   2. A migration whose ``downgrade()`` isn't the inverse of ``upgrade()``.
#   3. Two heads / branch drift.
#
# Requires QUBERI_POSTGRES_DSN in the environment. Run against a database you
# don't mind wiping — typically a compose Postgres or an ephemeral test DB.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -z "${QUBERI_POSTGRES_DSN:-}" ]; then
  echo "error: QUBERI_POSTGRES_DSN must be set" >&2
  exit 1
fi

# Prefer the venv's alembic when running locally; fall back to PATH inside the
# container image.
if [ -x "$API_ROOT/.venv/bin/alembic" ]; then
  ALEMBIC="$API_ROOT/.venv/bin/alembic"
else
  ALEMBIC="alembic"
fi

cd "$API_ROOT"

echo "[check-migrations] upgrade head ..."
"$ALEMBIC" upgrade head

echo "[check-migrations] downgrade base ..."
"$ALEMBIC" downgrade base

echo "[check-migrations] upgrade head (round-trip) ..."
"$ALEMBIC" upgrade head

echo "[check-migrations] OK"
