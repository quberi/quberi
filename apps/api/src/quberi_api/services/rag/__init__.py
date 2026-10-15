"""RAG pipeline — extraction, chunking, embedding, vector storage.

Public surface used by the API + worker:

* :func:`extract_text` (from :mod:`extract`) — turn a stored document
  blob into plain text.
* :func:`chunk_text` (from :mod:`chunk`) — split extracted text into
  overlapping windows sized for the embedding model.
* :class:`VectorStore` (from :mod:`vector_store`) — the Qdrant façade.
* :func:`index_document` (from :mod:`indexer`) — the arq job entry
  point; end-to-end pipeline for one document.
"""

from quberi_api.services.rag.chunk import Chunk, chunk_text
from quberi_api.services.rag.extract import ExtractError, extract_text
from quberi_api.services.rag.indexer import (
    IndexingResult,
    index_document,
    remove_document_from_index,
    reindex_document,
)
from quberi_api.services.rag.generate import (
    Citation,
    GenerationResult,
    generate_answer,
)
from quberi_api.services.rag.retrieve import (
    ScopedRetrieval,
    embed_query,
    retrieve_for_conversation,
)
from quberi_api.services.rag.vector_store import (
    RetrievedChunk,
    VectorStore,
    collection_name_for_model,
)

__all__ = [
    "Chunk",
    "Citation",
    "ExtractError",
    "GenerationResult",
    "IndexingResult",
    "RetrievedChunk",
    "ScopedRetrieval",
    "VectorStore",
    "chunk_text",
    "collection_name_for_model",
    "embed_query",
    "extract_text",
    "generate_answer",
    "index_document",
    "reindex_document",
    "remove_document_from_index",
    "retrieve_for_conversation",
]
