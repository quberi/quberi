"""Background workers driven by arq (Redis-backed job queue).

The API process enqueues jobs via
:mod:`quberi_api.workers.enqueue`; a separate ``worker`` container runs
``arq quberi_api.workers.rag.WorkerSettings`` to consume them.
"""
