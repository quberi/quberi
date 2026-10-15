"""Security-shared primitives (encryption, hashing).

The submodules keep runtime-sensitive dependencies (``cryptography`` etc.)
in one place so the rest of the service layer can import a thin surface
without every caller needing to know the exact library used.
"""

from quberi_api.services.security.crypto import (
    CryptoError,
    KeyMissing,
    seal,
    unseal,
)

__all__ = ["CryptoError", "KeyMissing", "seal", "unseal"]
