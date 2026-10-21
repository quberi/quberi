"""Which OCR model files are used, pinned so a run today and a run next year read the same.

Both models are PaddlePaddle's PP-OCRv5 *mobile* pair, Apache-2.0, republished by the
PaddlePaddle team as plain safetensors that ``transformers`` loads directly. No code
from the model repositories is ever run (``trust_remote_code`` stays off), and the
weights are checked against the hashes below before they are used.

Changing a revision means re-running the checks that were done for it (the OCR
evaluation set) and updating the hashes.
"""

from __future__ import annotations

from dataclasses import dataclass

ENGINE_NAME = "pp-ocrv5-transformers"
MODEL_NAME = "PP-OCRv5_mobile"

DET_REPO = "PaddlePaddle/PP-OCRv5_mobile_det_safetensors"
DET_REVISION = "c5041d225cf951ff06900ab81a3c7d543c45e2ad"
DET_SHA256 = "c49eea87d579f931c79bf29ac1ffb1b3166f86f6811b8e46b982bb66e3f0f287"

REC_REPO = "PaddlePaddle/PP-OCRv5_mobile_rec_safetensors"
REC_REVISION = "485a9a9781535ad477de3b1eea5f96a2e7300d8d"
REC_SHA256 = "0bce74901be5dc8aef6d62a09eef40e46d9687fe7b087b37639e80f5d80219a3"

# What the cache key for "this picture is already read" should include.
ENGINE_VERSION = f"det@{DET_REVISION[:8]}+rec@{REC_REVISION[:8]}"

# Longest side the detector sees. The model's own default (960) loses small print on a
# full page; 1600 found every line in the evaluation page where 960 missed some.
DEFAULT_DET_SIDE = 1600
# Memory climbs steeply past this (a 2 GB process was killed at 3600), so it is a wall,
# not a suggestion: anything larger is shrunk first.
HARD_MAX_DET_SIDE = 2000


@dataclass(frozen=True)
class EngineConfig:
    """Everything the child process needs. Plain values only: it is sent across a pipe."""

    det_side: int = DEFAULT_DET_SIDE
    max_pixels: int = 40_000_000
    min_confidence: float = 0.5
    max_lines: int = 500
    threads: int = 2
    rec_batch: int = 8
    hf_home: str | None = None

    def clamped(self) -> EngineConfig:
        side = max(320, min(self.det_side, HARD_MAX_DET_SIDE))
        return EngineConfig(
            det_side=side,
            max_pixels=self.max_pixels,
            min_confidence=self.min_confidence,
            max_lines=self.max_lines,
            threads=max(1, self.threads),
            rec_batch=max(1, self.rec_batch),
            hf_home=self.hf_home,
        )
