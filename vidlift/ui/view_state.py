"""Pure presentation snapshots. Execution truth remains in the job registry."""
from __future__ import annotations

from dataclasses import dataclass
import math


def positive_gbph(text: str) -> float:
    """Reject non-finite values before planning or recording work."""
    try:
        value = float(text)
    except (ValueError, TypeError):
        raise ValueError("GB/hour must be a finite number greater than zero.") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("GB/hour must be a finite number greater than zero.")
    return value


@dataclass(frozen=True)
class PlanInputs:
    paths: tuple[str, ...]
    gbph: float
    output_dir: str
    subtitle_ocr: bool
    transcribe: bool


@dataclass(frozen=True)
class SubtitleOptions:
    replace_source: bool = False
    suppress_srt: bool = False
    device: str = "auto"

    def __post_init__(self):
        if self.device not in {"auto", "cuda", "cpu"}:
            raise ValueError("Unsupported transcription device")

    @property
    def sidecar_note(self) -> str:
        if self.suppress_srt:
            return "SRT removed only after successful embed; kept if embedding fails."
        return "SRT sidecar kept beside the output."

    @property
    def device_note(self) -> str:
        return {"auto": "Auto", "cuda": "GPU (CUDA requested; CPU fallback possible)",
                "cpu": "CPU"}[self.device]
