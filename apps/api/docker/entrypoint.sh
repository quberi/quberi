#!/usr/bin/env sh
# API container entrypoint.
#
# Applies pending Alembic migrations before starting the app process so that
# a rolling deploy always reconciles the schema with the code shipping in the
# same image. The image contains a single revision graph, so the `upgrade head`
# is deterministic and idempotent across replicas.
set -eu

echo "[entrypoint] applying migrations (alembic upgrade head)"
alembic -c /app/alembic.ini upgrade head

echo "[entrypoint] starting uvicorn"
exec uvicorn quberi_api.main:create_app \
  --factory \
  --host 0.0.0.0 \
  --port 8000
