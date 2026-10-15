FROM public.ecr.aws/docker/library/python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Indexes are overridable at build time (``docker build --build-arg
# PIP_INDEX_URL=...``) so a slow/unreachable upstream never forces a
# Dockerfile edit. Defaults are the official indexes. Some third-party
# mirrors (e.g. Aliyun's pytorch-wheels) serve a JS-rendered browse page
# instead of a plain PEP 503 index, which pip can't parse, and disambiguate
# accelerator variants via a PEP 440 local version (``2.6.0+cpu``) instead
# of URL path — if you swap in a mirror, verify it serves a plain index
# page and set TORCH_VERSION to match its version string.
ARG PIP_INDEX_URL=https://pypi.org/simple/
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_VERSION=2.6.0
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# --- Layer 1: CPU-only PyTorch (own layer, never invalidated by src) ----
# Pinned ≥2.6 because ``transformers`` refuses earlier versions when
# loading ``.bin`` weights (CVE-2025-32434). Because this RUN has no
# dependency on the app source, a code change never re-downloads the
# ~200 MB wheel. The cache mount persists pip's download cache across
# builds (including after ``FROM`` layer invalidation), so even a full
# rebuild only re-downloads what actually changed.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url ${PYTORCH_INDEX_URL} \
      "torch==${TORCH_VERSION}"

# --- Layer 2: app + remaining deps --------------------------------------
# ``pip install .`` sees torch already satisfied and skips it; only the
# lighter deps (fastapi, sqlalchemy, …) resolve. Reruns when src changes
# but stays off the torch download path.
COPY pyproject.toml ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

# Migration assets live alongside the app so ``alembic upgrade head`` runs
# against the exact schema the code expects.
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

COPY docker/entrypoint.sh /usr/local/bin/quberi-entrypoint
RUN chmod +x /usr/local/bin/quberi-entrypoint

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/quberi-entrypoint"]
