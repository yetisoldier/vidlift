"""Sample-based interlace classification (P4; deferred slice of item 1).

The three-way classification from bluray-to-mp4's v0.2.19 design
Section 3.3, adopted verbatim in rule: classify the source BEFORE any
deinterlace filtering, because AI models must see progressive frames
at the right cadence — feeding interlaced frames weaves combing into
the model's texture, while blind deinterlace over a *hard-telecined*
film doubles motion cadence artifacts and throws away the ~20% frame
count savings inverse telecine recovers.

Detection flow (v0.2.19 Section 3.3):
1. ffprobe is the initial triage (the probe's ``field_order`` /
   ``interlaced_flagged`` — already in the runner).
2. THIS module confirms flagged files with frame inspection: one
   ~60-second sample through ffmpeg's ``idet`` filter, whose
   statistics classify the three arms:
   - **progressive** — no deinterlace at all, even if the flag lied;
   - **hard 3:2 telecine** — IVTC: ``fieldmatch=combmatch=full,
     bwdif=mode=send_field:deint=interlaced,decimate`` + output rate
     24000/1001 (fieldmatch recovers progressive frames; bwdif in
     send_field/deint=interlaced only cleans what fieldmatch cannot
     perfectly weave; decimate drops the duplicated field frame);
   - **genuine interlace** — ``bwdif=mode=send_field:parity=auto`` +
     ``fps=<field rate>`` (temporal fidelity at the field rate; for
     NTSC 30000/1001 sources the reference chain's exact
     ``fps=60000/1001``).

P4 changes the PREPARE chain only, never the seam: the runner calls
the classifier for probe-flagged files, and
:func:`vidlift.run.build_prepare_command` consumes the verdict. An
undecidable sample (idet failed, unparsable statistics, or ambiguous
ratios) falls back to today's flag-driven ``bwdif=mode=send_field`` —
the classification refines, it never kills a run.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .probe import FileReport
from .run import Runner

__all__ = [
    "DEFAULT_SAMPLE_SECONDS",
    "MAX_SAMPLE_SECONDS",
    "MIN_SAMPLE_SECONDS",
    "PrepareChain",
    "SampleVerdict",
    "build_idet_command",
    "chain_for",
    "classify_interlace_sample",
    "parse_idet_output",
]

DEFAULT_SAMPLE_SECONDS = 60
MIN_SAMPLE_SECONDS = 10
MAX_SAMPLE_SECONDS = 120

PROGRESSIVE = "progressive"
TELECINE = "telecine"
INTERLACED = "interlaced"

# Decision thresholds (documented, canned-fixture-tested): the
# single-frame idet statistics carry the cadence signature. In hard
# 3:2 telecine each 5-frame cycle has 2 cleanly-woven frames, so the
# cadence reads ~40% single-frame Progressive with a combed majority;
# genuine interlace reads near-zero Progressive. A combed majority with
# MORE clean frames than any 3:2 cadence (>= 50%) is mixed content:
# undecidable, never forced into an arm.
_PROGRESSIVE_RATIO_MIN = 0.85  # clean stream: nearly every frame weaves
_INTERLACED_RATIO_MIN = 0.50  # majority combed: an interlace-shaped source
_TELECINE_PROGRESSIVE_MIN = 0.25  # the 2-of-5 clean-frame cadence floor
_TELECINE_PROGRESSIVE_MAX = 0.50  # exclusive: 3:2 reads ~40%, never 50/50

# The reference chains, adopted verbatim from v0.2.19 Section 3.3.
TELECINE_FILTER = (
    "fieldmatch=combmatch=full,bwdif=mode=send_field:deint=interlaced,decimate"
)
TELECINE_OUTPUT_RATE = "24000/1001"
INTERLACED_FILTER = "bwdif=mode=send_field:parity=auto"

_IDET_LINE = re.compile(
    r"(Single|Multi) frame detection:\s*"
    r"TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
    r"Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)"
)
_REPEATED_LINE = re.compile(
    r"Repeated Fields:\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)"
)


def clamp_sample_seconds(requested: float | None) -> float:
    value = (
        DEFAULT_SAMPLE_SECONDS if requested is None else float(requested)
    )
    return max(float(MIN_SAMPLE_SECONDS), min(float(MAX_SAMPLE_SECONDS), value))


@dataclass(frozen=True)
class SampleVerdict:
    """The classification result for one sampled file.

    ``kind`` is None when the sample was undecidable (idet failed or
    the statistics were ambiguous); the caller then falls back to the
    probe-flag heuristic — never a guess dressed as a verdict.
    """

    kind: str | None  # progressive | telecine | interlaced | None
    sample_seconds: float
    single: dict  # TFF/BFF/Progressive/Undetermined counts
    multi: dict
    repeated: dict
    detail: str
    command: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrepareChain:
    """The filter-chain consequence of one verdict (the classification
    arms; consumed by ``build_prepare_command``)."""

    filters: list[str]  # appended after scale=...,...,setsar=1
    fps_filter: str | None = None  # appended as the LAST -vf filter
    output_rate: str | None = None  # becomes ["-r", value] on the command


def build_idet_command(
    ffmpeg: str, path: str, *, sample_seconds: float = DEFAULT_SAMPLE_SECONDS
) -> list[str]:
    """The sample-inspection command: ~60 s of the file through idet.

    ``-t`` before ``-i`` is input-side wall-time addressing (the same
    semantics the engine adapter's chunk extraction uses), ``-f null``
    discards pixels after the filter runs, and idet's statistics land
    on stderr at the end of the stream.
    """
    seconds = f"{clamp_sample_seconds(sample_seconds):g}"
    return [
        ffmpeg,
        "-hide_banner", "-nostdin",
        "-t", seconds,
        "-i", path,
        "-vf", "idet",
        "-an", "-sn",
        "-f", "null", "-",
    ]


def parse_idet_output(stderr: str) -> tuple[dict, dict, dict]:
    """Parse idet's statistics lines from an ffmpeg stderr transcript.

    Returns ``(single, multi, repeated)`` count dicts. Missing lines
    read as all-zero counts; the classifier treats a zero-total
    single-frame line as undecidable.
    """
    single = {"TFF": 0, "BFF": 0, "Progressive": 0, "Undetermined": 0}
    multi = dict(single)
    repeated = {"Top": 0, "Bottom": 0}
    for line in stderr.splitlines():
        match = _IDET_LINE.search(line)
        if match:
            counts = {
                "TFF": int(match.group(2)),
                "BFF": int(match.group(3)),
                "Progressive": int(match.group(4)),
                "Undetermined": int(match.group(5)),
            }
            if match.group(1) == "Single":
                single = counts
            else:
                multi = counts
            continue
        match = _REPEATED_LINE.search(line)
        if match:
            repeated = {"Top": int(match.group(1)), "Bottom": int(match.group(2))}
    return single, multi, repeated


def _classify_counts(single: dict) -> tuple[str | None, str]:
    """Three-way decision from the single-frame idet counts."""
    determined = single["TFF"] + single["BFF"] + single["Progressive"]
    if determined <= 0:
        return None, "no usable single-frame detections in the sample"
    prog_ratio = single["Progressive"] / determined
    interlaced_ratio = (single["TFF"] + single["BFF"]) / determined
    if prog_ratio >= _PROGRESSIVE_RATIO_MIN:
        return PROGRESSIVE, (
            f"progressive {single['Progressive']}/{determined} clean frames"
        )
    if interlaced_ratio >= _INTERLACED_RATIO_MIN:
        if (
            _TELECINE_PROGRESSIVE_MIN
            <= prog_ratio
            < _TELECINE_PROGRESSIVE_MAX
        ):
            return TELECINE, (
                f"combed {interlaced_ratio:.0%} with a {prog_ratio:.0%} "
                "clean-frame cadence: hard 3:2 telecine"
            )
        if prog_ratio < _TELECINE_PROGRESSIVE_MIN:
            return INTERLACED, (
                f"combed {interlaced_ratio:.0%} with only {prog_ratio:.0%} "
                "clean frames: genuine interlace"
            )
    return None, (
        f"ambiguous sample: progressive {prog_ratio:.0%} vs combed "
        f"{interlaced_ratio:.0%}"
    )


def classify_interlace_sample(
    path: str,
    *,
    ffmpeg: str,
    runner: Runner | None = None,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    result: tuple[int, str, str] | None = None,
) -> SampleVerdict:
    """Run the idet sample pass and classify the three arms.

    ``result`` (a pre-run ``(code, stdout, stderr)``) lets the runner
    execute the command once and hand the transcript in; ``runner`` is
    used otherwise. NEVER raises: an idet failure (nonzero exit,
    missing output, ambiguous statistics) yields ``kind=None`` and the
    caller falls back to the probe-flag heuristic (today's behavior) —
    the classification refines the chain, it must not kill a run.
    """
    command = build_idet_command(ffmpeg, path, sample_seconds=sample_seconds)
    if result is not None:
        code, _stdout, stderr = result
    else:
        if runner is None:
            raise ValueError("classify_interlace_sample needs runner or result")
        code, _stdout, stderr = runner(command[0], command[1:])
    if code != 0:
        return SampleVerdict(
            kind=None,
            sample_seconds=clamp_sample_seconds(sample_seconds),
            single={}, multi={}, repeated={},
            detail=(
                f"idet sample pass failed (exit {code}): "
                f"{(stderr or '').strip()[:200] or 'no diagnostic'}"
            ),
            command=list(command),
        )
    single, multi, repeated = parse_idet_output(stderr or "")
    kind, detail = _classify_counts(single)
    return SampleVerdict(
        kind=kind,
        sample_seconds=clamp_sample_seconds(sample_seconds),
        single=single,
        multi=multi,
        repeated=repeated,
        detail=detail,
        command=list(command),
    )


def chain_for(verdict: SampleVerdict | None, report: FileReport) -> PrepareChain | None:
    """The prepare-chain consequence of a verdict (v0.2.19 reference
    chains, verbatim); None means "fall back to the flag heuristic".

    - progressive: NO deinterlace filter at all (the regression arm).
    - telecine: fieldmatch+decimate chain + output rate 24000/1001.
    - interlaced: bwdif at field rate; the fps filter doubles the
      source's base rate as an exact rational string (30000/1001 ->
      60000/1001, the reference chain's exact value for NTSC; 25/1 ->
      50/1 for PAL). An unparseable source rate falls back to plain
      bwdif at the flagged-heuristic shape rather than inventing one.
    """
    if verdict is None or verdict.kind is None:
        return None
    if verdict.kind == PROGRESSIVE:
        return PrepareChain(filters=[])
    if verdict.kind == TELECINE:
        return PrepareChain(
            filters=[TELECINE_FILTER],
            output_rate=TELECINE_OUTPUT_RATE,
        )
    if verdict.kind == INTERLACED:
        fps_filter = None
        video = report.video
        if video is not None:
            fps_filter = _field_rate_filter(video.r_frame_rate_raw)
        return PrepareChain(
            filters=[INTERLACED_FILTER],
            fps_filter=fps_filter,
        )
    return None


def _field_rate_filter(rate_raw: str) -> str | None:
    """Double the source base rate, exact rational string arithmetic.

    "30000/1001" -> "fps=60000/1001" (the reference chain's value);
    "25/1" -> "fps=50/1". A single-number rate ("30") doubles to "60".
    Anything unparseable returns None — never a guessed field rate.
    """
    if not isinstance(rate_raw, str) or not rate_raw:
        return None
    num, sep, den = rate_raw.partition("/")
    try:
        if sep:
            n, d = int(num), int(den)
            if n <= 0 or d <= 0:
                return None
            return f"fps={2 * n}/{d}"
        n = int(num)
        if n <= 0:
            return None
        return f"fps={2 * n}"
    except ValueError:
        return None