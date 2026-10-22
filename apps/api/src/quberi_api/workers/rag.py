"""arq worker settings for RAG indexing.

Launched from the ``worker`` container with::

    arq quberi_api.workers.rag.WorkerSettings

The worker shares the same Docker image + dependency graph as the API,
so it can import every service module directly. On startup we build one
long-lived infrastructure bundle (postgres session factory, Qdrant
client) and stash it in the ``ctx`` dict every job receives.

Concurrency
-----------
``max_jobs`` is intentionally low (2) because indexing is CPU-bound
(embedding) and IO-bound to Qdrant/Postgres. Two in flight keeps a
single-container box responsive under bulk imports.
"""

from __future__ import annotations

import logging

from arq import cron

from quberi_api.core.config import get_settings
from quberi_api.infrastructure.qdrant import create_qdrant_client
from quberi_api.services.rag.image_text import index_image_text_job
from quberi_api.services.rag.indexer import index_document
from quberi_api.workers.feishu import sync_feishu_source
from quberi_api.workers.settings import build_redis_settings

_log = logging.getLogger(__name__)


async def on_startup(ctx: dict) -> None:
    """Materialise shared resources every job depends on."""

    settings = get_settings()

    # Postgres session factory — same wiring the API uses.
    from quberi_api.db.session import create_engine, create_sessionmaker

    engine = create_engine(settings.postgres_dsn.get_secret_value())
    ctx["engine"] = engine
    ctx["session_factory"] = create_sessionmaker(engine)

    # Qdrant.
    ctx["qdrant"] = create_qdrant_client(settings)

    # Bootstrap platform-default AI bindings here too, so the worker can
    # resolve embeddings even if the API hasn't run yet (e.g. worker-only
    # bootstrap after a schema wipe).
    from quberi_api.services import ai_providers

    async with ctx["session_factory"]() as session:
        try:
            await ai_providers.bootstrap_platform_defaults(session, settings=settings)
        except Exception:  # pragma: no cover — logged; worker still runs
            _log.exception("worker: failed to bootstrap platform AI defaults")

    # Warm up the platform-default embedding model so the first job
    # doesn't have to pay the 2-4 GB Hugging Face download + weights
    # load. First boot on a cold cache takes 5-10 minutes; every
    # subsequent boot is ~15s.
    await _warm_up_embedding_model(settings=settings)

    _log.info("arq worker ready")


async def _warm_up_embedding_model(*, settings) -> None:
    """Load the platform-default embedding model into memory on boot.

    We only touch the *default* model — orgs that BYO a different
    embedding pay the load penalty on their first indexing job (still a
    warm cache, so it's seconds not minutes). Any error here is logged
    and the worker starts anyway; jobs will fail their own way with a
    clearer message than "cold start took 40 minutes".
    """

    model_id = settings.default_embedding_model
    if not model_id:
        _log.info("no default embedding model configured; skipping warmup")
        return
    from quberi_api.services.ai.registry import MODEL_REGISTRY
    spec = MODEL_REGISTRY.get(model_id)
    if spec is None or spec.provider_kind != "local_embedding":
        _log.info(
            "default embedding model %s is not local; skipping warmup",
            model_id,
        )
        return
    _log.info("warming up embedding model %s (may take a while first time)", model_id)
    try:
        from quberi_api.services.ai.base import ProviderConfig
        from quberi_api.services.ai.local_embedding import LocalEmbeddingProvider

        provider = LocalEmbeddingProvider(
            ProviderConfig(kind="local_embedding", display_name="warmup"),
            vector_dim=spec.vector_dim or 1024,
        )
        # Embed a throwaway sentence; forces the sentence-transformers
        # model into memory and validates dim.
        vectors = await provider.embed(["warmup"], model=model_id)
        _log.info(
            "embedding model %s warm; dim=%d",
            model_id, len(vectors[0]) if vectors else 0,
        )
    except Exception:  # pragma: no cover — worker still runs
        _log.exception("embedding model warmup failed; jobs may be slow")


async def on_shutdown(ctx: dict) -> None:
    from quberi_api.db.session import dispose_engine
    from quberi_api.services.ai.ocr import close_ocr_provider

    # Stop the OCR child first. It can hold a gigabyte while reading a page, and it should
    # not be left for the operating system to clean up. A failure here must not stop the
    # connections below from closing.
    try:
        await close_ocr_provider()
    except Exception:  # noqa: BLE001
        _log.exception("worker: could not stop the OCR engine cleanly")

    engine = ctx.get("engine")
    qdrant = ctx.get("qdrant")
    if qdrant is not None:
        await qdrant.close()
    if engine is not None:
        await dispose_engine(engine)


async def sweep_guest_sandbox(ctx: dict) -> int:
    """Delete guest-sandbox files that have passed their 24 h life.

    Runs hourly. Each file goes through the verified delete (row, stored file
    and vectors, with the vectors checked), so an expired upload is really gone
    rather than merely hidden. A failure on one file is logged and the sweep
    carries on; the file is retried at the next run.
    """

    from sqlalchemy import select

    from quberi_api.domain.identity.models import Organization, User
    from quberi_api.services import demo_seed, guest_sandbox
    from quberi_api.services.sandbox_cleanup import sweep_expired

    async with ctx["session_factory"]() as session:
        org = (
            await session.execute(
                select(Organization).where(Organization.slug == demo_seed.DEMO_ORG_SLUG)
            )
        ).scalar_one_or_none()
        if org is None:
            return 0
        owner_id = (
            await session.execute(
                select(User.id).where(
                    User.organization_id == org.id,
                    User.email == demo_seed.DEMO_OWNER_EMAIL,
                )
            )
        ).scalar_one_or_none()
        space = await guest_sandbox.get_sandbox_space(session, organization_id=org.id)
        if owner_id is None or space is None:
            return 0
        org_id, space_id = org.id, space.id
        await session.commit()
        return await sweep_expired(
            session, org_id=org_id, space_id=space_id, actor_user_id=owner_id
        )


async def backfill_pictures(ctx: dict) -> int:
    """Queue indexing for pictures that predate picture search. Returns how many."""

    if not get_settings().image_search_enabled:
        return 0
    from quberi_api.services.rag.image_backfill import pictures_needing_index

    async with ctx["session_factory"]() as session:
        ids = await pictures_needing_index(
            session, model_id=get_settings().default_image_embedding_model
        )
        await session.commit()
    queued = 0
    for document_id in ids:
        # A stable job id: asking twice for the same picture queues it once.
        job = await ctx["redis"].enqueue_job(
            "index_document", str(document_id), _job_id=f"index-picture-{document_id}"
        )
        queued += 1 if job is not None else 0
    if queued:
        _log.info("picture backfill: queued %d picture(s) for indexing", queued)
    return queued


async def purge_revoked_sessions(ctx: dict) -> int:
    """Drop signed-out session records whose token has expired anyway."""

    from sqlalchemy import delete, func

    from quberi_api.domain.auth.models import RevokedSession

    async with ctx["session_factory"]() as session:
        result = await session.execute(
            delete(RevokedSession).where(RevokedSession.expires_at < func.now())
        )
        await session.commit()
    return int(result.rowcount or 0)


class WorkerSettings:
    """arq entry point discovered by ``arq quberi_api.workers.rag.WorkerSettings``."""

    functions = [index_document, index_image_text_job, sync_feishu_source]
    # Every hour, on the hour: expire guest-sandbox uploads.
    cron_jobs = [
        cron(sweep_guest_sandbox, minute=0, run_at_startup=True),
        # Pictures uploaded before picture search existed; cheap when there is none.
        cron(backfill_pictures, minute=7, run_at_startup=True),
        cron(purge_revoked_sessions, minute=30),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = build_redis_settings()
    max_jobs = 2
    # 15 min per job. Warmup happens once at boot; steady-state jobs
    # take seconds. The generous ceiling covers pathological PDFs plus
    # network-slow days for connectors we'll add later.
    job_timeout = 900
    keep_result_forever = False
    keep_result = 3600  # keep job outcomes for 1h so debugging is possible
