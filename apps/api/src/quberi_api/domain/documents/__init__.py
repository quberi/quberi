"""Documents: files uploaded to a space (Scope-A ``file vault`` only).

RAG-adjacent concerns (text extraction, chunking, embedding, retrieval)
belong to a later slice; this package intentionally stays thin.
"""

from quberi_api.domain.documents.models import Document, DocumentSource, FeishuFolderClaim

__all__ = ["Document", "DocumentSource", "FeishuFolderClaim"]
