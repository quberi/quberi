"""AI provider bindings and per-org model selections (Slice H).

Domain: **who provides the LLM/embedding call**, decoupled from
*what model* to use for what purpose. A binding is an authenticated
handle to a provider (DeepSeek key, OpenAI key, local BGE-M3 loader…);
a selection picks a binding + model for a specific purpose (chat /
embedding / reranker).
"""

from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding  # noqa: F401
