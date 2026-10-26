"""VidLift's Real-ESRGAN ncnn/Vulkan engine adapter (file-first).

A disciplined port of bluray-to-mp4's validated v0.2.20-v0.2.29 upscale
path (``bluray_to_mp4/upscale.py``, read-only reference; the import is
deliberately NOT taken — its module graph pulls disc-shaped
``makemkv``/``binaries`` vocabulary across the engine seam, which
architecture.md forbids). Where this module deviates from or narrows
the sibling, the deviation is stated in a comment citing the design
section. Binding design: ``docs/designs/real-engine-adapter.md``.

Implements roadmap "Next" item 1 acceptance clauses verbatim
(clause a's clock discipline is SUPERSEDED by the v0.4.0 decode-truth
stamp below; the max(0.5 s, 0.01%) duration gates survive as gate P
and gate M's demoted record):
  a. frame-clock discipline (never a default) + duration sanity
     gates P and M with the max(0.5 s, 0.01%) tolerance and the
     unknown-reference refusal;
  b. explicit tiling derived from the MEASURED frame extent
     (extent+64, ceil to 64, floor 512); ``-t 0`` is unreachable;
  d. ``UpscaleError`` (fail open) and ``EngineCancelled`` (a cancel,
     not a failure) raised at the runner seam;
  g. model artifacts consumed with sha256 verified at first use per
     process, BEFORE any frame is extracted.

Chunk flow (design Section 7): extract frame-index-addressed PNG
frames per chunk, run the engine once per chunk, reassemble each
chunk's output frames into an ffv1 segment stamped with the DERIVED
clock, concat the segments, gate the mezzanine (exact frame count +
clock vs the shipped-audio extent), and hand the runner the
mezzanine path. The workdir is kept on failure/cancel for clause-e
triage (with a per-chunk resume checkpoint) and deleted on success.

v0.4.0 (binding design: docs/designs/v0.4.0-decode-truth-chunking.md,
the port of bluray-to-mp4's v0.2.33 frame-exact chunking and v0.2.34
derived-stamp-rate field lessons, read-only reference):

- The stamp clock is DERIVED from decode-truth — the prepared file's
  decode-counted frame total over the shipped audio's measured extent
  — snapped to a canonical rate or refused pre-GPU. Container
  r/avg_frame_rate fields are diagnostic-only, never stamp inputs
  (design B.2; the .34 lesson: a consistently lying clock family passes
  every metadata gate).
- Chunks are addressed by FRAME INDEX (``select='between(n,a,b)```
  with ``-vsync 0``), never by ``-ss/-t`` wall-time windows; every
  chunk asserts its exact frame count in and out (design B.3; the .33
  lesson: timestamp addressing drifts, index addressing cannot).
- The mezzanine is gated twice: exact frame equality against the
  prepared total, and duration against the SHIPPED-AUDIO extent at
  max(2.0 s, 0.1%) (design B.4). Old gates P/M stay as records; gate M
  is demoted to a diagnostic (its prepared-duration reference cannot
  see a consistent lie).
- A rejected mezzanine is RETAINED (renamed ``.rejected-<ts>``) with a
  diagnostic JSON, never deleted (design B.5).
- Long steps emit step labels + heartbeats through ``on_step`` so the
  runner's state stays observably alive (design B.6).
- A per-chunk checkpoint (``chunk_state.json``) lets an interrupted
  run skip completed GPU chunks on retry — params and total-frames
  proven equal, or the workdir is deleted wholesale (design B.7).
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import _proc
from .plan import PlannedFile
from .probe import parse_probe
from .run import (
    EngineAdapter,
    EngineCancelled,
    EngineResult,
    MissingToolError,
    ToolBox,
    UpscaleError,
)
from .tools_realesrgan import (
    resolve_realesrgan,
    resolve_realesrgan_models,
    verify_model_artifacts,
)

__all__ = [
    "AudioExtent",
    "CANONICAL_RATES",
    "CHUNK_SECONDS",
    "CLOCK_GATE_TOLERANCE_FRACTION",
    "CLOCK_GATE_TOLERANCE_SECONDS",
    "MIN_AUDIO_EXTENT_SECONDS",
    "MIN_CHUNK_SECONDS",
    "SNAP_TOLERANCE_FRACTION",
    "EngineInvocation",
    "MODEL_TABLE",
    "ModelSpec",
    "NcnnVulkanEngine",
    "PNGExtent",
    "derive_stamp_clock",
    "duration_gate",
    "gate_tolerance",
    "probe_audio_extent_seconds",
    "probe_frame_count",
    "gpu_probe_frame",
    "probe_duration_seconds",
    "probe_frame_clock",
    "snap_to_canonical_rate",
    "select_engine",
    "tile_argument",
]

# Runner seam for external commands (same shape as run.Runner /
# probe.Runner): run(binary, args) -> (returncode, stdout, stderr).
# It carries ffmpeg extraction/assembly and ffprobe clock/duration
# probes; the engine process itself needs the live drain/cancel
# discipline and runs through ``_run_process`` (Popen), except in
# tests where the same injected runner fakes the engine call too.
Runner = Callable[[str, list[str]], "tuple[int, str, str]"]

CHUNK_SECONDS = 120  # default; >= 10; env override: VIDLIFT_CHUNK_SECONDS
MIN_CHUNK_SECONDS = 10

# Derived-stamp snap set (v0.4.0 design B.2; port of bluray-to-mp4
# v0.2.34 §1.2, read-only reference). The stamp clock is DERIVED from
# decode-truth (prepared frame count / shipped-audio extent) and snapped
# to one of these canonical tokens; container r/avg fields are
# diagnostic-only, never derivation inputs. 50 and 60000/1001 stay in
# the set because bwdif's field-rate arm produces genuine 50/59.94
# prepared files, and those must snap, not refuse.
CANONICAL_RATES = (
    "24000/1001",
    "24",
    "25",
    "30000/1001",
    "50",
    "60000/1001",
)

# Nearest-neighbor snap tolerance: 0.1% relative, with a 1e-9 ulp guard
# so float renderings of the canonical rationals sit on the correct
# side of the boundary (inclusive; port of v0.2.34 §1.2 verbatim).
SNAP_TOLERANCE_FRACTION = 0.001

# Duration-denominator sanity minimums (v0.4.0 design B.2; port of
# v0.2.34 §1.1(3)): an extent under 10 s or a stream-vs-format skew
# over 25% is a broken probe, not a stamp source.
MIN_AUDIO_EXTENT_SECONDS = 10.0
MAX_AUDIO_PROVENANCE_SKEW_FRACTION = 0.25

# Clock gate (B.4): mezzanine vs the shipped-audio extent at
# max(2.0 s, 0.1%) — kills rate-class desync (25% apart) with two
# orders of magnitude of headroom, passes legit lead-in/out skew
# (seconds) with 5x headroom (port of v0.2.34 §3 constants).
CLOCK_GATE_TOLERANCE_SECONDS = 2.0
CLOCK_GATE_TOLERANCE_FRACTION = 0.001

# Decode-count probe heartbeat cadence (B.6): the step seam re-fires
# at this interval while the minutes-long -count_frames pass runs, so
# last_activity_at advances and the status names the running step.
PROBE_HEARTBEAT_SECONDS = 5.0

# v0.4.5 (progress-honesty §3a, D-count): the LOG-line cadence for the
# count-pass heartbeat — every 30 s the adapter writes one honest log
# line ("<label>: heartbeat t=<n>s (decode-count pass; …)") so the
# log-tail surface disproves "frozen" (the §5 no-silent-window rule:
# HEARTBEAT_LOG_SECONDS is the named ceiling). Module-level so tests
# can shrink it without touching call sites.
PROBE_HEARTBEAT_LOG_SECONDS = 30.0

# Per-chunk resume checkpoint (B.7): written in the engine workdir,
# atomically, after every completed chunk.
CHUNK_STATE_FILENAME = "chunk_state.json"


def _canonical_rate_value(token: str) -> float:
    num, sep, den = token.partition("/")
    if sep:
        return float(num) / float(den)
    return float(num)


def snap_to_canonical_rate(derived: float) -> tuple[str, bool]:
    """Nearest canonical token within 0.1% relative (B.2; .34 §1.2).

    Returns ``(token, snapped)``. Nearest-neighbor FIRST, then the
    inclusive tolerance check — the 23.976-vs-24 windows overlap, so
    "nearest wins" is the disambiguation that keeps boundary rates
    deterministic. Unsnappable -> ``(nearest_token, False)``; callers
    raise the named refusal (never a default, never a guess).
    """
    derived = float(derived)
    nearest = min(
        CANONICAL_RATES,
        key=lambda token: abs(derived - _canonical_rate_value(token)),
    )
    canonical = _canonical_rate_value(nearest)
    tolerance = canonical * (SNAP_TOLERANCE_FRACTION + 1e-9)
    return nearest, abs(derived - canonical) <= tolerance


@dataclass(frozen=True)
class AudioExtent:
    """The measured shipped-audio extent and its provenance (B.2).

    The encode maps ``1:a:0`` from the SOURCE file (run.py
    build_encode_command), so the stamp denominator is the source's
    first audio stream duration when usable, else its format duration.
    BOTH figures ride along when both exist: the two-provenance
    denominator rule derives against each and requires the same snap
    token (the Karen snap-band defense).
    """

    seconds: float | None  # the chosen extent (stream preferred)
    source: str  # "stream" | "format"
    stream_seconds: float | None = None
    format_seconds: float | None = None


def probe_audio_extent_seconds(
    source_path: str, *, ffprobe: str, runner: Runner
) -> AudioExtent:
    """Measure the shipped-audio extent from the SOURCE file (B.2).

    ``a:0`` exactly matches the encode's ``-map 1:a:0`` — if audio
    selection ever becomes configurable, this ordinal MUST follow the
    encode map or the derived clock is silently wrong (pinned at both
    seams; B.10 risk 6). Sanity minimums port v0.2.34 §1.1(3):
    missing, <= 0, or < 10 s refuses; a stream-vs-format skew > 25%
    refuses (a broken probe, not a stamp source). Refusal raises the
    named :class:`UpscaleError` -> fail-open, pre-GPU.
    """
    stream_seconds: float | None = None
    code, stdout, _stderr = runner(
        ffprobe,
        [
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=duration",
            "-of", "csv=p=0",
            source_path,
        ],
    )
    if code == 0:
        for line in stdout.strip().splitlines():
            try:
                value = float(line.strip())
            except ValueError:
                continue
            if value > 0:
                stream_seconds = value
                break
    format_seconds = probe_duration_seconds(
        source_path, ffprobe=ffprobe, runner=runner
    )
    if stream_seconds is not None and stream_seconds <= 0:
        stream_seconds = None  # a non-positive tag is absent, not a source
    if format_seconds is not None and format_seconds <= 0:
        format_seconds = None
    if stream_seconds is None and format_seconds is None:
        raise UpscaleError(
            f"no usable audio extent on source {source_path}: neither the "
            "a:0 stream duration nor the format duration exists; refusing "
            "to derive a stamp clock from nothing"
        )
    if stream_seconds is not None and format_seconds is not None:
        larger = max(stream_seconds, format_seconds)
        skew = abs(stream_seconds - format_seconds) / larger
        if skew > MAX_AUDIO_PROVENANCE_SKEW_FRACTION:
            raise UpscaleError(
                f"audio extent provenance skew on {source_path}: a:0 stream "
                f"reports {stream_seconds:.3f}s, format reports "
                f"{format_seconds:.3f}s ({skew * 100:.1f}% apart, over the "
                f"{MAX_AUDIO_PROVENANCE_SKEW_FRACTION * 100:.0f}% sanity "
                "maximum); a broken probe is not a stamp source"
            )
    chosen = stream_seconds if stream_seconds is not None else format_seconds
    assert chosen is not None  # guarded above
    if chosen < MIN_AUDIO_EXTENT_SECONDS:
        raise UpscaleError(
            f"audio extent on source {source_path} is unusable as a stamp "
            f"denominator: {chosen:.3f}s is under the "
            f"{MIN_AUDIO_EXTENT_SECONDS:g} s sanity minimum"
        )
    return AudioExtent(
        seconds=chosen,
        source="stream" if stream_seconds is not None else "format",
        stream_seconds=stream_seconds,
        format_seconds=format_seconds,
    )


def derive_stamp_clock(
    *,
    source_path: str,
    prepared_total_frames: int,
    audio: AudioExtent,
    prepared_duration: float | None,
    container_clock: dict | None,
) -> dict:
    """Derive the stamp token from decode-truth, never container fields
    (B.2; port of bluray-to-mp4 v0.2.34 §1, adapted to VidLift's shape).

    Numerator: ``prepared_total_frames`` — measured on the PREPARED file
    AFTER any prepare transform (IVTC decimate, bwdif field rate), so
    the stamp matches the frames actually being reassembled. Do not
    "simplify" this back to source-side counting; a preparer that
    changes the frame count is safe only because the numerator is
    post-transform.

    Denominator: the shipped audio's measured extent (the encode maps
    ``1:a:0`` from the source). Provenance checks, in order:
    (1) two-provenance rule — when the source carries BOTH a usable
    a:0 stream extent and a format duration, each derivation must snap
    to the same canonical token (the 23.976-vs-24 band is exactly
    where a few-tenths-percent audio lie lands);
    (2) cross-check against the prepared file's own measured format
    duration (N / prepared_duration) — under-reported audio moves the
    audio-derived snap while the prepared-derived figure stays in the
    true family, and disagreement across provenance is the signature
    (the Karen snap-band finding, ported as a first-class gate);
    (3) all available provenances must agree on one token; container
    r/avg fields stay diagnostic-only, never derivation inputs.

    Unsnappable primary -> a named refusal quoting the derived rate,
    both operands, and the nearest candidate — raised BEFORE any chunk
    extraction (fail-open in seconds, never hours). Returns the
    frame-clock provenance dict for the manifest (schema v2 additive).
    """
    frames = int(prepared_total_frames)
    primary_seconds = audio.seconds
    assert primary_seconds is not None  # probe_audio_extent_seconds guards
    derived_raw = frames / primary_seconds
    token, snapped = snap_to_canonical_rate(derived_raw)
    if not snapped:
        raise UpscaleError(
            f"derived stamp rate {derived_raw:.6f} fps "
            f"({frames} frames / {primary_seconds:.3f}s audio extent on "
            f"{source_path}) does not snap to any canonical rate; nearest "
            f"candidate {token!r} is outside the 0.1% tolerance; refusing "
            "to stamp a mezzanine from an unsnappable clock"
        )

    cross_provenance: list[dict] = []
    checks: list[tuple[str, float]] = []
    if audio.stream_seconds is not None:
        checks.append(("audio_stream_extent", audio.stream_seconds))
    if audio.format_seconds is not None:
        checks.append(("source_format_duration", audio.format_seconds))
    if prepared_duration is not None and float(prepared_duration) > 0:
        checks.append(("prepared_format_duration", float(prepared_duration)))

    for name, seconds in checks:
        derived = frames / seconds
        candidate, candidate_snapped = snap_to_canonical_rate(derived)
        cross_provenance.append(
            {
                "provenance": name,
                "seconds": seconds,
                "derived_fps": derived,
                "snapped": candidate_snapped,
                "token": candidate if candidate_snapped else None,
            }
        )
        # An unsnappable secondary provenance is a diagnostic, not a
        # refusal: the 25% skew sanity gate (probe_audio_extent_seconds)
        # already refuses gross disagreement, and the primary + prepared
        # evidence still anchor the token. A snappable secondary that
        # snaps DIFFERENTLY is the lie signature -> refuse (rules 1-2).
        if candidate_snapped and candidate != token:
            raise UpscaleError(
                f"cross-provenance snap disagreement: the audio-derived "
                f"rate snaps to {token!r} ({frames} frames / "
                f"{primary_seconds:.3f}s {audio.source} audio extent), but "
                f"the {name} derivation ({frames} frames / {seconds:.3f}s = "
                f"{derived:.6f} fps) snaps to {candidate!r}; a consistently "
                "lying clock family cannot be told apart from a broken "
                "probe here, so the mezzanine is refused rather than "
                "stamped from one provenance alone"
            )

    container = container_clock or {}
    return {
        "value": token,  # the canonical token; never a container string
        "source": "derived",
        "derived_raw": derived_raw,
        "snapped": True,
        "audio_extent_s": primary_seconds,
        "audio_source": audio.source,
        "prepared_total_frames": frames,
        # diagnostic-only container fields (B.2; .34 §1.4): the
        # declared clock family, recorded — never derivation inputs
        "container_avg_frame_rate": container.get("avg_frame_rate_raw"),
        "container_r_frame_rate": container.get("r_frame_rate_raw"),
        "cross_provenance": cross_provenance,
    }
# Duration gate tolerance (clause a; port of v0.2.27's measured values):
# frame-scale container rounding plus a tiny proportional allowance,
# rejecting any viewer-visible accumulation. The old max(5 s, 2%) gate
# caught gross mismatch but allowed Mamma Mia's 15.050 s feature-length
# drift (0.22% — under the old 2% gate).
GATE_TOLERANCE_SECONDS = 0.5
GATE_TOLERANCE_FRACTION = 0.0001


@dataclass(frozen=True)
class ModelSpec:
    """One pinned engine profile: the ONLY source of ``-n``/``-s`` (clause b)."""

    name: str
    native_scale: int


# The model registry (design Section 3). x2plus is a TRUE native-2x
# RRDBNet (pixel_unshuffle head, verified in the x2plus
# VERIFICATION-REPORT.md): ``-s 2`` IS its native mode — this is NOT the
# v0.2.25 corruption case (x4plus, a native-4x net, at -s 2). x4plus
# never appears in any VidLift code path; it cannot be constructed
# through this table.
#
# v0.4.8 (design docs/designs/v0.4.8-anime-upscale-profile.md Section 2,
# verified 2026-10-26 against the actual released v0.2.0 binary on this
# host): the animevideov3-x2 net behaves as a TRUE native-2x model at
# ``-s 2`` — a 64x64 probe returned exactly 128x128 and a 65x47 odd-size
# probe returned exactly 130x94 (the x2plus verification
# standard's exact-2x rule). Its fixed internal 4x-then-half Interp
# combination (Resize_37 x4.0 / Resize_40 x0.5) is exposed to the engine
# as a scale-2 output; ``-n realesr-animevideov3-x2 -s 2`` is the
# documented pairing the engine help names as the default combination.
# No Reorg layer appears in the param; the run produced sane image
# content. Hash pinning lives in tools_realesrgan.MODEL_ARTIFACTS,
# verified identically to x2plus (design D1).
MODEL_TABLE: dict[str, ModelSpec] = {
    "live-action": ModelSpec(name="realesrgan-x2plus", native_scale=2),
    "anime": ModelSpec(name="realesr-animevideov3-x2", native_scale=2),
}


def gate_tolerance(reference_s: float) -> float:
    """max(0.5 s, 0.01%) — the gate tolerance for a known reference."""
    return max(GATE_TOLERANCE_SECONDS, GATE_TOLERANCE_FRACTION * float(reference_s))


def duration_gate(
    *,
    label: str,
    reference_s: float | None,
    actual_s: float | None,
    actual_name: str,
    reference_name: str,
) -> dict:
    """One duration sanity gate (clause a; design Section 4.2).

    Returns the manifest record ``{reference_s, actual_s, tolerance_s,
    ok}`` keyed by ``label`` (field names per manifest schema v2), and
    raises :class:`UpscaleError` naming both durations and the
    tolerance when the deviation exceeds it. An unknown reference
    (``None`` or <= 0) FAILS the gate — ported from v0.2.29: a gate
    with a zero reference accepted anything. Callers route the failure
    through fail-open, never a dead job.
    """
    if reference_s is None or float(reference_s) <= 0:
        raise UpscaleError(
            f"{label} duration gate: {reference_name} duration unknown; "
            "refusing to trust a gate with no reference"
        )
    reference = float(reference_s)
    actual = float(actual_s) if actual_s is not None else 0.0
    tolerance = gate_tolerance(reference)
    deviation = abs(actual - reference)
    record = {
        label: {
            f"{actual_name}_s": actual,
            f"{reference_name}_s": reference,
            "tolerance_s": tolerance,
            "ok": True,
        }
    }
    if deviation > tolerance:
        record[label]["ok"] = False
        raise UpscaleError(
            f"{label} duration gate: {actual_name} {actual:.3f}s vs "
            f"{reference_name} {reference:.3f}s (off by {deviation:.3f}s, "
            f"tolerance {tolerance:.3f}s); refusing a desynced output"
        )
    return record


def clock_gate_tolerance(reference_s: float) -> float:
    """max(2.0 s, 0.1%) — the mezzanine-vs-audio clock gate tolerance
    (B.4; port of v0.2.34 §3 constants, kept verbatim: kills rate-class
    desync (25% apart) with two orders of magnitude of headroom, passes
    legit lead-in/out skew (seconds) with 5x headroom)."""
    return max(
        CLOCK_GATE_TOLERANCE_SECONDS,
        CLOCK_GATE_TOLERANCE_FRACTION * float(reference_s),
    )


def _positive_rate(raw: str) -> float | None:
    """Parse an ffprobe rational like '24000/1001' into a positive fps."""
    if not isinstance(raw, str) or not raw:
        return None
    num, sep, den = raw.partition("/")
    try:
        if sep:
            n, d = float(num), float(den)
        else:
            return float(raw)
    except ValueError:
        return None
    if d == 0 or n <= 0:
        return None
    return n / d


def probe_frame_clock(
    prepared_path: str,
    *,
    ffprobe: str,
    runner: Runner,
) -> dict:
    """Resolve the prepared file's frame clock (clause a; design Section 4.1).

    Strict order: ``avg_frame_rate_raw`` (if it parses to a positive
    rate) -> ``r_frame_rate_raw`` (same check) -> a named
    :class:`UpscaleError`. NEVER a default — no 25, no 30000/1001, no
    "assume source": the prepare phase may legitimately change the rate
    (bwdif at field rate today; IVTC later), so the adapter probes the
    file it will actually re-stamp. The clock is carried as the exact
    rational STRING ffprobe reported — float re-rendering is how drift
    gets invented — plus a parsed ``fps`` used only for frame-count
    math (never for re-stamping). v0.4.0 (design B.2): the container
    fields are DEMOTED to diagnostics — the stamp authority is the
    decode-truth derivation (:func:`derive_stamp_clock`); this probe
    stays to record the declared clock family for the diagnostic
    record (both raw fields ride along additively).
    """
    code, stdout, stderr = runner(
        ffprobe,
        ["-v", "error", "-show_streams", "-of", "json", prepared_path],
    )
    if code != 0:
        raise UpscaleError(
            f"no usable frame clock on prepared file {prepared_path}: "
            f"ffprobe failed: {(stderr or 'no diagnostic').strip()[:200]}"
        )
    try:
        probe = parse_probe(prepared_path, stdout)
    except Exception as exc:  # ProbeError; an unparseable clock is unusable
        raise UpscaleError(
            f"no usable frame clock on prepared file {prepared_path}: {exc}"
        ) from exc
    video = probe.video
    if video is None:
        raise UpscaleError(
            f"no usable frame clock on prepared file {prepared_path}: "
            "no video stream"
        )
    raw_fields = {
        "avg_frame_rate": video.avg_frame_rate_raw,
        "r_frame_rate": video.r_frame_rate_raw,
    }
    for source, raw in (
        ("avg_frame_rate", video.avg_frame_rate_raw),
        ("r_frame_rate", video.r_frame_rate_raw),
    ):
        rate = _positive_rate(raw)
        if rate is not None:
            return {
                "value": raw,  # the exact rational string, byte-identical
                "source": source,
                "fps": rate,
                # diagnostic-only raw fields (B.2): the declared clock
                # family, recorded — never a derivation input
                "avg_frame_rate_raw": raw_fields["avg_frame_rate"],
                "r_frame_rate_raw": raw_fields["r_frame_rate"],
            }
    raise UpscaleError(
        f"no usable frame clock on prepared file {prepared_path}: neither "
        "avg_frame_rate nor r_frame_rate parses to a positive rate"
    )


def probe_duration_seconds(
    path: str, *, ffprobe: str, runner: Runner
) -> float | None:
    """The file's format duration via the seam; None when unusable."""
    code, stdout, _stderr = runner(
        ffprobe, ["-v", "error", "-show_format", "-of", "json", path]
    )
    if code != 0:
        return None
    try:
        return parse_probe(path, stdout).duration_seconds
    except Exception:
        return None


def probe_frame_count(path: str, *, ffprobe: str, runner: Runner) -> tuple[int, bool]:
    """The authoritative decode-counted frame total (v0.4.0 design B.1;
    port of bluray-to-mp4 v0.2.33 §2.1, read-only reference).

    Two passes through the seam. The slow ``-count_frames`` decode pass
    is AUTHORITATIVE: it counts what the file actually decodes to, not
    what its container claims. A full FFV1 decode-count costs minutes on
    a feature-length prepared file — accepted and stated honestly: it
    is one-time, pre-GPU, and orders of magnitude cheaper than the GPU
    hours it protects. The fast ``nb_frames`` metadata pass is the
    fallback ONLY when the decode pass is unusable, and its use is
    returned as ``frame_count_from_metadata=True`` so no manifest can
    pose a metadata count as a decoded one. Neither pass usable -> a
    named :class:`UpscaleError` routed to fail-open (never a silent
    metadata degrade — that is the lie vector returning through a
    performance door).
    """
    fast: int | None = None
    code, stdout, _stderr = runner(
        ffprobe,
        [
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames",
            "-of", "json",
            path,
        ],
    )
    if code == 0:
        try:
            streams = json.loads(stdout).get("streams") or [{}]
            raw = streams[0].get("nb_frames")
            if raw is not None and int(raw) > 0:
                fast = int(raw)
        except (ValueError, TypeError, IndexError, json.JSONDecodeError):
            fast = None
    code, stdout, _stderr = runner(
        ffprobe,
        [
            "-v", "error",
            "-select_streams", "v:0",
            "-count_frames",
            "-show_entries", "stream=nb_read_frames",
            "-of", "csv=p=0",
            path,
        ],
    )
    if code == 0:
        for line in reversed(stdout.strip().splitlines()):
            try:
                value = int(line.strip())
            except ValueError:
                continue
            if value > 0:
                return value, False  # decode-truth, authoritative
    if fast is not None:
        return fast, True  # metadata fallback, flagged
    raise UpscaleError(
        f"frame count could not be probed on {path}: neither the "
        "decode-count pass nor the metadata pass reported a usable total"
    )


@dataclass(frozen=True)
class PNGExtent:
    """Measured PNG geometry from the 24-byte IHDR header (no decoder)."""

    width: int
    height: int

    @property
    def extent(self) -> int:
        return max(self.width, self.height)


def _png_size(path: Path) -> PNGExtent | None:
    """PNG header read: signature check plus IHDR width/height at bytes
    16..24. Port of the sibling's ``_png_size`` verbatim in rule."""
    try:
        with path.open("rb") as fh:
            header = fh.read(24)
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width, height = struct.unpack(">II", header[16:24])
    return PNGExtent(width, height)


def tile_argument(frame_extent: int, *, floor: int | None = None) -> int:
    """Explicit tile size from the MEASURED extent (clause b; design 6).

    ``requested = max(512, extent + 64)`` (one tile, frame plus margin),
    ``tile = ceil(requested / 64) * 64`` (driver-friendly multiple). An
    operator floor (env ``VIDLIFT_ENGINE_TILE``) is honored exactly as
    the sibling does — as a FLOOR only. ``-t 0`` is unreachable by
    construction, and the result is never smaller than the extent.
    """
    requested = max(512, int(frame_extent) + 64)
    if floor is not None and floor > 0:
        requested = max(requested, int(floor))
    tile = int(math.ceil(requested / 64) * 64)
    return max(tile, int(frame_extent))


@dataclass(frozen=True)
class EngineInvocation:
    """One recorded engine process command line (for tests and logs)."""

    argv: tuple[str, ...]
    chunk_index: int
    input_frames: int
    tile: int


# Lazy "can this machine even run it" probe cache (design Section 5):
# one probe frame per (binary, models_dir, model name) per process —
# auto selection must not pay the probe cost for every run in a
# long-lived bridge. v0.4.8 (D4): the key gained the probed model name —
# a probe pass on one profile's model must not silently bless another;
# with the two-row registry the (binary, models_dir) key alone would
# alias the anime and live-action probes.
_PROBE_CACHE: dict[tuple[str, str, str], tuple[bool, str]] = {}

# v0.4.5 slice 2b (the 2026-10-20 live-run verdict): the probe frame's
# subprocess bound is 120 s — the engine decode of one tiny PNG is
# sub-second when healthy, so three minutes is 100x honesty headroom,
# far under the "minutes" the hang cost. The bound lives in
# _probe_run's self-managed reaping, NOT bare subprocess.run: Python's
# run(timeout=) does the kill but then calls an UNTIMED communicate()
# on the pipes — a killed ncnn child whose driver-spawned grandchildren
# hold the pipes open (the Windows hang: GPU resident-but-idle for
# three hours) blocks forever. _probe_run keeps the run() call shape
# so the injected-fake seam and the real path agree.
GPU_PROBE_TIMEOUT_SECONDS = 120.0
_PROBE_DRAIN_MAX_LINES = 256


def _probe_run(
    cmd: list[str],
    *,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """subprocess.run-shaped, subprocess-level bounded reaping (2b).

    The DEFAULT ``run_fn`` for :func:`gpu_probe_frame`. Pipe discipline:
    both streams are drained by daemon threads into bounded tails (no
    pipe-buffer deadlock, no unbounded memory), the child is waited on
    against a deadline, and a timeout escalates terminate -> wait 10 s
    -> kill -> wait 5 s (the same ladder the engine's ``_run_process``
   validated for stubborn children) — then raises the SAME
    :class:`subprocess.TimeoutExpired` ``run`` would, so callers and
    injected fakes share one timeout contract. The bounded-reap
    difference from ``subprocess.run``: every wait in the escalation is
    timed, and no untimed communicate() ever follows the kill — the
    exact trap that hung Eric's Windows probe for three hours.
    Non-piped calls (unused by the probe) delegate to subprocess.run.
    """
    deadline = time.monotonic() + float(timeout) if timeout else None
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    out: deque[str] = deque(maxlen=_PROBE_DRAIN_MAX_LINES)
    err: deque[str] = deque(maxlen=_PROBE_DRAIN_MAX_LINES)

    def _drain(pipe, sink: deque) -> None:
        try:
            for line in pipe:
                sink.append(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=_drain, args=(pipe, sink), daemon=True)
        for pipe, sink in ((proc.stdout, out), (proc.stderr, err))
    ]
    for thread in threads:
        thread.start()
    try:
        if deadline is None:
            proc.wait()
        else:
            try:
                proc.wait(timeout=max(0.05, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass  # bounded give-up; the raise is what matters
                for thread in threads:
                    thread.join(timeout=1)
                raise subprocess.TimeoutExpired(cmd, timeout) from None
    finally:
        for thread in threads:
            thread.join(timeout=1)
    return subprocess.CompletedProcess(
        cmd, proc.returncode, "".join(out), "".join(err)
    )


def _tiny_png(size: int = 64) -> bytes:
    """A minimal valid solid-gray RGB PNG the real engine can decode.

    Written byte-by-byte (signature, IHDR, one zlib IDAT of filter-0
    scanlines, IEND) because PIL is deliberately not a dependency and
    the probe must run inside any packaged environment.
    """
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + tag
            + data
            + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([120]) * (size * 3) for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def gpu_probe_frame(
    binary: str,
    models_dir: str,
    *,
    spec: ModelSpec | None = None,
    timeout_s: float = GPU_PROBE_TIMEOUT_SECONDS,
    run_fn=_probe_run,
    workspace: Path | None = None,
) -> tuple[bool, str]:
    """Run one REAL tiny frame through the engine and verify its output.

    The auto-selection gate (design Section 5, the sibling's "can this
    machine even run it" pattern): the binary must decode the probe
    PNG, run the model, and emit the EXACT native-scale output geometry
    (64x64 -> 128x128 at scale 2). Cached per process per
    (binary, models_dir). Never raises — every failure is
    ``(False, reason)`` so auto selection can fall to the stub with
    the reason recorded (design r1: a bad binary fails here, not
    mid-run).

    v0.4.5 slice 2b (the 2026-10-20 live-run verdict): the probe is
    subprocess-level BOUNDED at ``GPU_PROBE_TIMEOUT_SECONDS`` (120 s).
    A timeout is honest engineering data, not a hang: the reason reads
    ``probe frame timed out after <n>s``, a stderr line says
    ``engine probe timed out after <n>s``, and ``select_engine``'s
    existing auto path lands on the stub — the run ships fail-open
    instead of sitting GPU-resident-but-idle for hours (the default
    ``run_fn`` is :func:`_probe_run`, whose bounded reaping survives
    the killed-children-holding-pipes trap that hung the live run).
    """
    spec = spec or MODEL_TABLE["live-action"]
    key = (str(binary), str(models_dir), spec.name)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    tmp: Path | None = None
    ok, reason = False, ""
    try:
        if workspace is None:
            import tempfile

            tmp = Path(tempfile.mkdtemp(prefix="vidlift-engine-probe-"))
        else:
            tmp = workspace
        # The engine requires input/output of the SAME type (file+file or
        # dir+dir); the real chunk flow is dir+dir, so the probe mirrors it.
        in_dir = tmp / "in"
        out_dir = tmp / "out"
        in_dir.mkdir(exist_ok=True)
        out_dir.mkdir(exist_ok=True)
        (in_dir / "probe.png").write_bytes(_tiny_png())
        cmd = [
            binary,
            "-i", str(in_dir),
            "-o", str(out_dir),
            "-s", str(spec.native_scale),
            "-t", str(tile_argument(64)),
            "-m", str(models_dir),
            "-n", spec.name,
            "-f", "png",
        ]
        try:
            proc = run_fn(
                cmd, capture_output=True, text=True, timeout=timeout_s,
                **_proc.popen_kwargs(),
            )
        except subprocess.TimeoutExpired:
            # 2b: a probe that outlives the bound is DATA — name the
            # timeout as the reason (never a bare "could not run"),
            # say it on stderr (stdout is MCP protocol), and let the
            # caller's auto path fall to the stub.
            message = f"probe frame timed out after {int(timeout_s)}s"
            print(
                f"engine probe timed out after {int(timeout_s)}s",
                file=sys.stderr,
                flush=True,
            )
            ok, reason = False, message
        except Exception as exc:  # OSError, driver hangs
            ok, reason = False, f"probe frame could not run: {type(exc).__name__}: {exc}"
        else:
            if proc.returncode != 0:
                tail = (getattr(proc, "stderr", "") or "").strip()[-200:]
                ok, reason = (
                    False,
                    f"probe frame exited {proc.returncode}: {tail or 'no diagnostic'}",
                )
            else:
                out_png = out_dir / "probe.png"
                size = _png_size(out_png) if out_png.is_file() else None
                expected = 64 * spec.native_scale
                if size is None:
                    ok, reason = False, "probe frame produced no output PNG"
                elif (size.width, size.height) != (expected, expected):
                    ok, reason = (
                        False,
                        f"probe frame geometry {size.width}x{size.height} "
                        f"is not the exact {expected}x{expected} native-scale result",
                    )
                else:
                    ok, reason = True, (
                        f"probe frame ok (64x64 -> {expected}x{expected})"
                    )
    finally:
        if tmp is not None and workspace is None:
            shutil.rmtree(tmp, ignore_errors=True)
    _PROBE_CACHE[key] = (ok, reason)
    return _PROBE_CACHE[key]


def select_engine(
    request: str = "auto",
    *,
    profile: str = "live-action",
    binary: str | None = None,
    models_dir: str | None = None,
    tools: ToolBox | None = None,
) -> tuple[EngineAdapter, str | None]:
    """Resolve the ``--engine {auto,stub,ncnn}`` request (design Section 5).

    - ``stub``: the passthrough stub, forced.
    - ``ncnn``: forces the real engine; a resolution failure raises the
      named :class:`MissingToolError` (a setup problem), and a hash
      mismatch raises :class:`UpscaleError` (the run then fails open
      honestly per Section 9).
    - ``auto`` (the default): ncnn when the binary + models resolve AND
      the lazy GPU probe frame succeeds; anything else falls to the
      stub with a note saying exactly why (design r1: a bad binary
      fails the probe, never the run).

    v0.4.8 (D4): ``profile`` names the MODEL_TABLE row ("live-action"
    is the unchanged default; "anime" selects the animevideov3-x2 net,
    its pinned artifacts, and its own probe). An unknown profile
    raises :class:`UpscaleError` naming it — the same failure family
    as the final ``unknown engine request`` raise.

    Returns ``(engine, note)`` — ``note`` is the auto-fallback reason
    for the manifest's ``engine_note`` when auto lands on the stub.
    """
    from .run import StubEngine

    spec = MODEL_TABLE.get(profile)
    if spec is None:
        raise UpscaleError(f"unknown engine profile: {profile!r}")
    if request == "stub":
        return StubEngine(), None
    if request == "ncnn":
        binary_path = resolve_realesrgan(binary)
        models = resolve_realesrgan_models(binary_path, models_dir)
        verify_model_artifacts(models, spec.name)
        return (
            NcnnVulkanEngine(
                binary=binary_path, models_dir=models, profile=profile
            ),
            None,
        )
    if request == "auto":
        try:
            binary_path = resolve_realesrgan(binary)
            models = resolve_realesrgan_models(binary_path, models_dir)
            verify_model_artifacts(models, spec.name)
            ok, reason = gpu_probe_frame(binary_path, models, spec=spec)
            if not ok:
                note = f"auto: stub engine ({reason})"
                return StubEngine(note=note), note
            return (
                NcnnVulkanEngine(
                    binary=binary_path, models_dir=models, profile=profile
                ),
                None,
            )
        except (MissingToolError, UpscaleError) as exc:
            note = f"auto: stub engine ({exc})"
            return StubEngine(note=note), note
    raise UpscaleError(f"unknown engine request: {request!r}")


class NcnnVulkanEngine(EngineAdapter):
    """Real Real-ESRGAN pass through chunked PNG frame directories.

    File-first contract (``EngineAdapter.run``): prepared progressive
    square-pixel file path + plan in; mezzanine path + frame count out.
    No disc/rip/drive/title vocabulary crosses this seam.
    """

    def __init__(
        self,
        *,
        profile: str = "live-action",
        binary: str | None = None,
        models_dir: str | None = None,
        artifacts: dict | None = None,
        chunk_seconds: int | None = None,
        tile_floor: int | None = None,
        runner: Runner | None = None,
        engine_runner: Callable[[str, list[str]], int] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[str], None] | None = None,
        popen_factory=subprocess.Popen,
    ) -> None:
        spec = MODEL_TABLE.get(profile)
        if spec is None:
            raise UpscaleError(f"unknown engine profile: {profile}")
        self.profile = profile
        self.spec = spec
        self._binary = binary
        self._models_dir = models_dir
        self._artifacts = artifacts  # fixture table for tests; None = pinned
        self._chunk_seconds = int(
            chunk_seconds
            if chunk_seconds is not None
            else (_env_int("VIDLIFT_CHUNK_SECONDS") or CHUNK_SECONDS)
        )
        self.tile_floor = (
            tile_floor
            if tile_floor is not None
            else _env_int("VIDLIFT_ENGINE_TILE")
        )
        # The runner seam carries ffmpeg/ffprobe (canned JSON in tests);
        # engine_runner, when injected, stands in for the engine process
        # (returning its exit code) so tests can observe command shape.
        # With neither, the engine runs live through popen_factory.
        self.runner = runner
        self.engine_runner = engine_runner
        self.on_progress = on_progress
        self.on_step = on_step
        self.popen_factory = popen_factory
        self.invocations: list[EngineInvocation] = []
        self.log_lines: list[str] = []

    # -- public seam -----------------------------------------------------

    def run(
        self,
        prepared_path: str,
        plan: PlannedFile,
        tools: ToolBox,
        *,
        cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[str], None] | None = None,
    ) -> EngineResult:
        """Run the real engine over the prepared file (design Sections 4-7).

        ``on_progress`` is the runner's progress seam (``record_progress``);
        it chains with any constructor-level callback so both the
        runner's RunState and direct constructors (the bridge, tests)
        observe frame progress. ``on_step`` (v0.4.0 design B.6) is the
        step-label seam (``record_step``): the adapter fires a label at
        the start of each long step and as the decode-count probes'
        heartbeat, so ``last_activity_at`` stays true during the
        previously-blind windows; it chains the same way.
        """
        original_progress = self.on_progress
        if on_progress is not None:
            if original_progress is None:
                self.on_progress = on_progress
            else:
                def _chained(frames_done: int, frames_total: int) -> None:
                    original_progress(frames_done, frames_total)
                    on_progress(frames_done, frames_total)

                self.on_progress = _chained
        original_step = self.on_step
        if on_step is not None:
            if original_step is None:
                self.on_step = on_step
            else:
                def _chained_step(label: str) -> None:
                    original_step(label)
                    on_step(label)

                self.on_step = _chained_step
        try:
            return self._run_and_report(
                prepared_path, plan, tools, cancel=cancel
            )
        finally:
            self.on_progress = original_progress
            self.on_step = original_step

    def _run_and_report(
        self,
        prepared_path: str,
        plan: PlannedFile,
        tools: ToolBox,
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> EngineResult:
        binary = resolve_realesrgan(self._binary)
        models_dir = resolve_realesrgan_models(binary, self._models_dir)
        verify_model_artifacts(models_dir, self.spec.name, artifacts=self._artifacts)

        runner = self._seam_runner()
        prepared_duration = probe_duration_seconds(
            prepared_path, ffprobe=tools.ffprobe, runner=runner
        )
        if prepared_duration is None:
            raise UpscaleError(
                f"prepared file duration could not be probed "
                f"({prepared_path}); refusing to upscale blind"
            )
        # Gate P: prepared must match the SOURCE duration within
        # tolerance — catches a prepare-phase bug (a future IVTC that
        # forgot decimate, a bwdif/fps mismatch) before hours are spent.
        # An unknown source duration refuses (v0.2.29 rule). v0.4.0
        # (design B.4): gate P KEEPS its refusal behavior (a prepare-
        # phase clock bug must still abort pre-GPU); the record now
        # labels its reference what it is — a FORMAT duration, a
        # sanity reference, never a stamp authority.
        gate_p = duration_gate(
            label="prepare_vs_source",
            reference_s=plan.report.duration_seconds,
            actual_s=prepared_duration,
            actual_name="prepared",
            reference_name="source",
        )
        gate_p["prepare_vs_source"]["reference_kind"] = "format_duration"

        # B.1: the authoritative decode-counted total, BEFORE any GPU
        # work. The step label keeps the state observably alive through
        # a minutes-long FFV1 decode-count (B.6).
        self._step("prepared-count-probe")
        prepared_total_frames, frames_from_metadata = (
            self._probe_frame_count_alive(
                str(prepared_path), tools=tools, label="prepared-count-probe"
            )
        )

        # B.2 denominator: the shipped audio's measured extent, probed
        # on the SOURCE file's a:0 — the exact stream the encode maps
        # (-map 1:a:0). If audio selection ever becomes configurable,
        # this ordinal MUST follow the encode map or the derived clock
        # is silently wrong (B.10 risk 6, pinned at both seams).
        audio = probe_audio_extent_seconds(
            plan.report.path, ffprobe=tools.ffprobe, runner=runner
        )

        # The prepared file's declared clock: DIAGNOSTIC-ONLY (B.2;
        # .34 §1.4). The v0.3.0 adapter treated these container fields
        # as the stamp authority — the lie vector this port retires.
        # An unusable declared clock is recorded as None, never a
        # refusal: decode-truth does not need the container's opinion.
        container_clock = self._container_clock_diagnostic(
            str(prepared_path), tools=tools
        )

        # B.2: derive the stamp from decode-truth, snap, cross-check
        # provenance — every refusal on this path is pre-GPU (fail-open
        # in seconds, never hours).
        stamp = derive_stamp_clock(
            source_path=plan.report.path,
            prepared_total_frames=prepared_total_frames,
            audio=audio,
            prepared_duration=prepared_duration,
            container_clock=container_clock,
        )
        self._log(
            f"stamp derived: {stamp['value']} from {prepared_total_frames} "
            f"decoded frames / {stamp['audio_extent_s']:.3f}s "
            f"{stamp['audio_source']} audio extent (container declared "
            f"{container_clock['value'] if container_clock else 'nothing usable'})"
        )

        output = Path(prepared_path).with_suffix(".upscaled.mkv")
        workdir = _workdir_for(prepared_path)
        diagnostic = {
            "source_path": plan.report.path,
            "prepared_path": str(prepared_path),
            "prepared_total_frames": prepared_total_frames,
            "frame_count_from_metadata": frames_from_metadata,
            "audio_extent_s": stamp["audio_extent_s"],
            "audio_source": stamp["audio_source"],
            "derived_rate_raw": stamp["derived_raw"],
            "derived_rate_snapped": stamp["value"],
            "stamp_fps": stamp["value"],
            "container_r_frame_rate": stamp["container_r_frame_rate"],
            "container_avg_frame_rate": stamp["container_avg_frame_rate"],
            "cross_provenance": stamp["cross_provenance"],
            "clock_tolerance_s": clock_gate_tolerance(
                float(stamp["audio_extent_s"])
            ),
        }

        gates = dict(gate_p)
        frame_gates: dict | None = None
        resume_info: dict = {"checkpoint_used": False, "chunks_reused": 0}
        frames_done = 0
        try:
            frames_done, chunk_context, resume_info = self._run_chunks(
                prepared_path=str(prepared_path),
                prepared_total_frames=prepared_total_frames,
                stamp=stamp,
                tools=tools,
                binary=binary,
                models_dir=models_dir,
                workdir=workdir,
                output=output,
                cancel=cancel,
            )
            diagnostic.update(chunk_context)

            # B.4 gate 1 — exact frame equality: decode-count the
            # mezzanine (same helper, same honest cost) and require
            # integer equality with the prepared total. Non-emptiness
            # (the v0.3.0 assertion) cannot see a chunk that silently
            # dropped or duplicated frames; exact counts can.
            self._step("gate-frames-probe")
            mezzanine_frames, _ = self._probe_frame_count_alive(
                str(output), tools=tools, label="gate-frames-probe"
            )
            frame_gates = {
                "mezzanine_vs_prepared": {
                    "expected": prepared_total_frames,
                    "actual": mezzanine_frames,
                    "ok": mezzanine_frames == prepared_total_frames,
                }
            }
            diagnostic["mezzanine_frame_count"] = mezzanine_frames
            if mezzanine_frames != prepared_total_frames:
                raise UpscaleError(
                    "mezzanine_vs_prepared frame gate: mezzanine decodes "
                    f"to {mezzanine_frames} frames, the prepared file has "
                    f"{prepared_total_frames}; exact equality required, "
                    "refusing a miscounted mezzanine"
                )

            # B.4 gate 2 — clock vs the SHIPPED-AUDIO extent (the same
            # provenance that derived the stamp), max(2.0 s, 0.1%).
            # The old gate M reference (prepared format duration)
            # cannot see a consistently lying clock family; the audio
            # that reaches the MP4 is the axis viewers experience.
            self._step("gate-clock-probe")
            mezzanine_duration = probe_duration_seconds(
                str(output), ffprobe=tools.ffprobe, runner=runner
            )
            if mezzanine_duration is None:
                raise UpscaleError(
                    f"upscale mezzanine duration could not be probed "
                    f"({output}); refusing to encode a possibly desynced "
                    "upscale"
                )
            diagnostic["mezzanine_duration_s"] = mezzanine_duration
            audio_extent_s = float(stamp["audio_extent_s"])
            tolerance = clock_gate_tolerance(audio_extent_s)
            deviation = abs(mezzanine_duration - audio_extent_s)
            clock_ok = deviation <= tolerance
            gates["mezzanine_vs_audio"] = {
                "mezzanine_s": mezzanine_duration,
                "audio_s": audio_extent_s,
                "audio_source": stamp["audio_source"],
                "tolerance_s": tolerance,
                "ok": clock_ok,
            }
            if not clock_ok:
                raise UpscaleError(
                    "mezzanine_vs_audio clock gate: mezzanine "
                    f"{mezzanine_duration:.3f}s vs shipped-audio "
                    f"{audio_extent_s:.3f}s ({stamp['audio_source']} "
                    f"extent; off by {deviation:.3f}s, tolerance "
                    f"{tolerance:.3f}s); refusing a desynced upscale"
                )

            # Gate M, demoted to a diagnostic (B.4): still recorded with
            # its measured values and ok flag (manifest compat), never
            # a refusal — the audio-extent clock gate above is the
            # authority this port installs.
            gate_m_tolerance = gate_tolerance(prepared_duration)
            gates["mezzanine_vs_prepared"] = {
                "mezzanine_s": mezzanine_duration,
                "prepared_s": prepared_duration,
                "tolerance_s": gate_m_tolerance,
                "ok": abs(mezzanine_duration - prepared_duration)
                <= gate_m_tolerance,
                "diagnostic_only": True,
            }
        except EngineCancelled:
            raise
        except UpscaleError as exc:
            # B.5: a mezzanine that exists at failure time is RETAINED
            # (renamed, never deleted) with a diagnostic JSON naming
            # every operand of the derivation; the amended message
            # carries both paths verbatim into the runner's fail-open
            # log and manifest — no runner signature change.
            retained = self._retain_rejected_mezzanine(
                output, diagnostic, str(exc)
            )
            if retained is not None:
                retained_path, diagnostic_path = retained
                amended = UpscaleError(
                    f"{exc}; rejected mezzanine retained at "
                    f"{retained_path}; diagnostic at {diagnostic_path}"
                )
                amended.rejected_mezzanine = (  # type: ignore[attr-defined]
                    {"path": retained_path, "diagnostic_path": diagnostic_path}
                )
                raise amended from exc
            raise

        # Success: workdir deleted (retention contract); the sibling's
        # frame-dir peak stays ~1-2 GB because in/out died per chunk.
        shutil.rmtree(workdir, ignore_errors=True)
        return EngineResult(
            mezzanine_path=str(output),
            frames_done=frames_done,
            note=None,
            simulated=False,
            engine_model=self.spec.name,
            engine_native_scale=self.spec.native_scale,
            frame_clock=stamp,
            duration_gates=gates,
            frame_gates=frame_gates,
            resume=resume_info,
            model_hashes_verified=True,
        )

    # -- internals ---------------------------------------------------------

    def _seam_runner(self) -> Runner:
        """ffmpeg/ffprobe through the injected runner when present
        (tests replay canned JSON), else real subprocesses."""
        if self.runner is not None:
            return self.runner
        from .run import _subprocess_runner

        return _subprocess_runner

    @staticmethod
    def _expected_frames(duration: float | None, clock: dict) -> int:
        """Deprecated v0.3.0 estimate (ceil(duration x container fps))
        — RETIRED by v0.4.0 B.3/E15: the progress denominator is the
        decode-counted ``prepared_total_frames``. Kept one release as a
        thin compatibility shim for external callers; never used by the
        chunk flow."""
        if duration is None:
            return 0
        return max(1, math.ceil(float(duration) * float(clock["fps"])))

    def _log(self, text: str) -> None:
        self.log_lines.append(text)

    def _step(self, label: str) -> None:
        """One labeled long-step boundary (B.6): logged through the
        adapter's own log and fired through the ``on_step`` seam
        (``record_step`` sets state.progress["step"] and touches the
        state), so the bridge's status names WHAT is running during the
        previously-blind windows."""
        self._log(f"step: {label}")
        if self.on_step is not None:
            self.on_step(label)

    def _probe_frame_count_alive(
        self, path: str, *, tools: ToolBox, label: str
    ) -> tuple[int, bool]:
        """B.1's decode-count probe with B.6's heartbeat: a full FFV1
        decode-count runs minutes on a feature-length file, so it runs
        on a worker thread while the step label re-fires every
        ``PROBE_HEARTBEAT_SECONDS`` — the state's last_activity stays
        true, the window never reads as frozen.

        v0.4.5 (progress-honesty design §3a, D-count): the pump also
        writes a throttled liveness LOG line every
        ``PROBE_HEARTBEAT_LOG_SECONDS`` — the B.6 step touch mutated
        state but left the log tail silent, so a 10-40 min count pass
        looked byte-identical to a hang. The line names the phase,
        elapsed wall time, and WHY there is no frame counter yet; no
        fabricated percent.
        """
        runner = self._seam_runner()
        started = time.time()
        box: dict = {}

        def _work() -> None:
            try:
                box["value"] = probe_frame_count(
                    path, ffprobe=tools.ffprobe, runner=runner
                )
            except BaseException as exc:  # surfaced after the join
                box["error"] = exc

        thread = threading.Thread(
            target=_work, daemon=True, name=f"vidlift-{label}"
        )
        thread.start()
        next_log = started + PROBE_HEARTBEAT_LOG_SECONDS
        while thread.is_alive():
            thread.join(timeout=PROBE_HEARTBEAT_SECONDS)
            if thread.is_alive():
                self._step(label)  # heartbeat touch
                if time.time() >= next_log:
                    # §3a: the honest liveness line — names the phase,
                    # names elapsed wall time, and says WHY there is no
                    # frame counter yet (the count is the denominator
                    # being measured; it cannot be its own numerator).
                    self._log(
                        f"{label}: heartbeat t={time.time() - started:.0f}s "
                        "(decode-count pass; no per-frame signal until "
                        "ffprobe summary)"
                    )
                    next_log = time.time() + PROBE_HEARTBEAT_LOG_SECONDS
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _container_clock_diagnostic(
        self, prepared_path: str, *, tools: ToolBox
    ) -> dict | None:
        """The prepared file's DECLARED container clock — diagnostic
        only (B.2; .34 §1.4). Never raises: a container that cannot
        even declare a clock is recorded as None in the provenance
        record; decode-truth does not need the container's opinion."""
        try:
            return probe_frame_clock(
                prepared_path,
                ffprobe=tools.ffprobe,
                runner=self._seam_runner(),
            )
        except UpscaleError:
            return None

    def _retain_rejected_mezzanine(
        self, output: Path, diagnostic: dict, error: str
    ) -> tuple[str, str] | None:
        """B.5: retain a rejected mezzanine — RENAME, never delete —
        plus its diagnostic JSON. Returns (retained_path,
        diagnostic_path), or None when no mezzanine file exists or the
        filesystem refuses the retention (the original error still
        propagates either way; retention is best-effort, deletion is
        forbidden)."""
        if not output.is_file():
            return None
        ts = time.strftime("%Y%m%dT%H%M%S")
        name = output.name
        if name.endswith(".upscaled.mkv"):
            retained = output.with_name(
                name[: -len(".upscaled.mkv")] + f".rejected-{ts}.mkv"
            )
        else:
            retained = output.with_name(f"{name}.rejected-{ts}.mkv")
        diagnostic_path = retained.with_name(
            retained.name[: -len(".mkv")] + ".diagnostic.json"
        )
        try:
            output.rename(retained)
            payload = dict(diagnostic)
            payload["error"] = error
            diagnostic_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            self._log(f"rejected-mezzanine retention failed: {exc}")
            return None
        self._log(f"rejected mezzanine retained at {retained}")
        return str(retained), str(diagnostic_path)

    @staticmethod
    def _cancelled(cancel) -> bool:
        return bool(cancel is not None and cancel())

    def _run_process(
        self, cmd: list[str], *, label: str, cancel=None
    ) -> None:
        """Process discipline, port of the sibling's ``_run_process``:
        drain threads on stdout/stderr (no pipe deadlock), 0.2 s cancel
        poll, terminate -> wait 10 s -> kill -> wait 5 s. When an
        ``engine_runner`` seam is injected it replaces the live process
        while the cancel contract (raise :class:`EngineCancelled`) still
        holds; ``popen_factory`` lets tests observe the terminate/kill
        escalation without a real stubborn child.
        """
        if self._cancelled(cancel):
            raise EngineCancelled(f"cancelled before {label}")
        if self.engine_runner is not None:
            code = self.engine_runner(cmd[0], cmd[1:])
            if self._cancelled(cancel):
                raise EngineCancelled(f"cancelled during {label}")
            if code != 0:
                raise UpscaleError(f"{label} failed with code {code}")
            return
        try:
            proc = self.popen_factory(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                # v0.4.4 (design §6): suppress child console windows on
                # Windows; an empty dict on every other platform, so the
                # popen_factory test seam sees no kwargs on Linux.
                **_proc.popen_kwargs(),
            )
        except OSError as exc:
            raise UpscaleError(f"{label} could not start: {exc}") from exc

        output_tail: list[str] = []

        def drain(pipe) -> None:
            try:
                for line in pipe:
                    output_tail.append(line.rstrip())
                    self._parse_engine_progress(line)
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass

        threads = []
        for pipe in (proc.stdout, proc.stderr):
            if pipe is None:
                continue
            thread = threading.Thread(target=drain, args=(pipe,), daemon=True)
            thread.start()
            threads.append(thread)

        while proc.poll() is None:
            if self._cancelled(cancel):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                raise EngineCancelled(f"cancelled during {label}")
            time.sleep(0.2)

        proc.wait()
        for thread in threads:
            thread.join(timeout=1)
        if proc.returncode != 0:
            detail = " | ".join(output_tail[-6:])[:600]
            raise UpscaleError(
                f"{label} failed with code {proc.returncode}: {detail}"
            )

    def _parse_engine_progress(self, line: str) -> None:
        """Cheap best-effort frame counter from engine stdout lines like
        '16.67%' or '1234/7392' — a progress hint through
        ``on_progress`` only, never a promise."""
        if self.on_progress is None:
            return
        text = line.strip()
        for token in text.replace(",", " ").split():
            if "/" in token and token.replace("/", "").isdigit():
                done, _, total = token.partition("/")
                if total.isdigit() and int(total) > 0:
                    self.on_progress(int(done), int(total))
                return

    def _ffmpeg(
        self,
        cmd: list[str],
        *,
        label: str,
        tools: ToolBox,
        cancel=None,
    ) -> None:
        """ffmpeg through the runner seam (subprocess when live);
        a nonzero exit is a named UpscaleError (fail-open destination)."""
        if self._cancelled(cancel):
            raise EngineCancelled(f"cancelled before {label}")
        code, _out, err = self._seam_runner()(cmd[0], cmd[1:])
        if self._cancelled(cancel):
            raise EngineCancelled(f"cancelled during {label}")
        if code != 0:
            raise UpscaleError(
                f"{label} failed (exit {code}): {(err or '').strip()[:300]}"
            )

    def _run_chunks(
        self,
        *,
        prepared_path: str,
        prepared_total_frames: int,
        stamp: dict,
        tools: ToolBox,
        binary: str,
        models_dir: str,
        workdir: Path,
        output: Path,
        cancel=None,
    ) -> tuple[int, dict, dict]:
        """Extract -> engine -> reassemble per chunk; concat; returns
        (total output frames, chunk diagnostic context, resume info)
        (design Section 7 + v0.4.0 B.3/B.7).

        B.3: chunks are addressed by FRAME INDEX over the
        decode-counted total — ``F = max(1, round(chunk_seconds x
        stamp_fps))`` frames per full chunk, ``ceil(N / F)`` chunks,
        each chunk's ``[first, first+count)`` extracted with
        ``select='between(n,a,b)'`` + ``-vsync 0`` (no muxer dup/drop
        toward an implied clock; PNG naming starts at 1 so the extent
        probe stays untouched). ``-ss/-t`` wall-time addressing is
        RETIRED: container-timestamp windows do not land on frame
        boundaries and per-window jitter accumulates into
        feature-length drift (.33 §1). Every chunk asserts its EXACT
        frame count in and out — a chunk that silently drops or
        duplicates a frame cannot assemble a partial mezzanine.
        """
        stamp_fps = _canonical_rate_value(str(stamp["value"]))
        chunk_seconds = max(MIN_CHUNK_SECONDS, int(self._chunk_seconds))
        frames_per_chunk = max(1, round(chunk_seconds * stamp_fps))
        total_frames = int(prepared_total_frames)
        chunks = max(1, math.ceil(total_frames / frames_per_chunk))
        expected_per_chunk = [
            min(frames_per_chunk, total_frames - idx * frames_per_chunk)
            for idx in range(chunks)
        ]
        workdir.mkdir(parents=True, exist_ok=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()

        # B.7: per-chunk resume. The checkpoint is proven against THIS
        # invocation — params (model/scale/chunk_seconds), prepared
        # path, total frames, and stamp token, all re-derived fresh —
        # before ANY segment is trusted. Any mismatch, missing file,
        # wrong size, or truncated duration (probed cheaply; must sit
        # within one frame-time of count/stamp) deletes the workdir
        # WHOLESALE and starts clean. No partial trust, ever.
        checkpoint_params = {
            "model": self.spec.name,
            "scale": self.spec.native_scale,
            "chunk_seconds": chunk_seconds,
        }
        reused = self._load_checkpoint(
            workdir=workdir,
            prepared_path=str(prepared_path),
            prepared_total_frames=total_frames,
            stamp_token=str(stamp["value"]),
            stamp_fps=stamp_fps,
            params=checkpoint_params,
            tools=tools,
        )
        resume_info = {
            "checkpoint_used": bool(reused),
            "chunks_reused": len(reused) if reused else 0,
        }
        completed: dict[int, dict] = {
            int(entry["idx"]): entry for entry in (reused or [])
        }

        segments: list[Path] = []
        frames_done = 0
        completed_records: list[dict] = []
        if reused:
            for entry in sorted(completed.values(), key=lambda e: int(e["idx"])):
                segments.append(Path(str(entry["segment"])))
                frames_done += int(entry["count"])
            # seed the checkpoint's completed list with the reused
            # entries so a LATER failure still records every completed
            # chunk (resumed chunks must not fall out of the checkpoint)
            completed_records = [dict(entry) for entry in sorted(
                completed.values(), key=lambda e: int(e["idx"])
            )]
            self._log(
                f"resume: reusing {len(reused)} completed chunk(s) from "
                f"{CHUNK_STATE_FILENAME}"
            )
        # v0.4.5 (progress-honesty §3b, D-eta): the chunk count is the
        # honest denominator the moment the decode-count lands; the
        # frame counters ride the first on_progress below.
        if self.on_step is not None:
            self.on_step("chunk")
        try:
            for idx in range(chunks):
                if idx in completed:
                    continue  # B.7: checkpoint-proven completed chunk
                first = idx * frames_per_chunk
                count = expected_per_chunk[idx]
                last = first + count - 1
                chunk = workdir / f"chunk_{idx:04d}"
                in_dir = chunk / "in"
                out_dir = chunk / "out"
                shutil.rmtree(in_dir, ignore_errors=True)
                shutil.rmtree(out_dir, ignore_errors=True)
                in_dir.mkdir(parents=True, exist_ok=True)
                out_dir.mkdir(parents=True, exist_ok=True)
                segment = chunk / "segment.mkv"
                if segment.exists():
                    segment.unlink()
                # v0.4.5 (§3b): the sub-stage's human note + chunk
                # position, promoted through the on_step seam (the same
                # record_step merge; additive keys only).
                if self.on_step is not None:
                    self.on_step(
                        f"chunk {idx + 1}/{chunks} — "
                        "upscaling chunk "
                        f"{idx + 1}/{chunks} (no per-frame ETA during "
                        "extraction; frames_total arrives with the "
                        "first chunk's counters)"
                    )

                try:
                    # Extraction (B.3): FRAME-INDEX addressing on the
                    # prepared file. ``select='between(n,first,last)'``
                    # is the only addressing that cannot drift;
                    # ``-vsync 0`` forbids the muxer from dup/dropping
                    # toward an implied clock; ``-start_number 1``
                    # keeps the existing PNG naming and the
                    # frame_00000001.png extent probe untouched. No
                    # ``-ss``/``-t``: those address container
                    # timestamps — the drift generator .33 retired.
                    # (Prefix-decode cost grows with chunk index;
                    # accepted and stated; the hybrid-seek mitigation
                    # is explicitly deferred out of this milestone.)
                    self._ffmpeg(
                        [
                            tools.ffmpeg,
                            "-y", "-hide_banner", "-nostdin",
                            "-i", prepared_path,
                            "-an", "-sn",
                            "-vf", f"select='between(n,{first},{last})'",
                            "-vsync", "0",
                            "-start_number", "1",
                            str(in_dir / "frame_%08d.png"),
                        ],
                        label=f"frame extraction chunk {idx + 1}/{chunks}",
                        tools=tools,
                        cancel=cancel,
                    )
                    input_frames = _count_pngs(in_dir)
                    # B.3 assertion, port of .33 §2.4 verbatim in rule:
                    # exact count in, exact count out.
                    if input_frames != count:
                        raise UpscaleError(
                            f"chunk {idx + 1}/{chunks} extraction count "
                            f"mismatch: expected {count} frames "
                            f"[{first}, {last}], extracted {input_frames}"
                        )
                    extent_png = _png_size(in_dir / "frame_00000001.png")
                    frame_extent = extent_png.extent if extent_png else 0
                    tile = tile_argument(
                        frame_extent, floor=self.tile_floor
                    )

                    # The engine command (clause b): -n/-s pairing comes
                    # from MODEL_TABLE alone; the tile from the measured
                    # extent. -t 0 is unreachable by construction.
                    esrgan_cmd = [
                        binary,
                        "-i", str(in_dir),
                        "-o", str(out_dir),
                        "-s", str(self.spec.native_scale),
                        "-t", str(tile),
                        "-m", models_dir,
                        "-n", self.spec.name,
                        "-f", "png",
                    ]
                    self._log(
                        f"chunk {idx + 1}/{chunks}: frames={input_frames} "
                        f"extent={frame_extent} tile={tile} "
                        f"cmd={shlex.join(esrgan_cmd)}"
                    )
                    # v0.4.5 (§3b): the chunk counters + honest note ride
                    # the SAME on_step seam so the GUI's stage label can
                    # render "upscaling chunk 3/12" and the status names
                    # the AI pass exactly while it runs.
                    if self.on_step is not None:
                        self.on_step(
                            f"chunk {idx + 1}/{chunks} — "
                            "upscaling chunk "
                            f"{idx + 1}/{chunks} (no per-frame ETA during "
                            "the AI pass; frames_total arrives with the "
                            "first chunk's counters)"
                        )
                    self._run_process(
                        esrgan_cmd,
                        label=f"Real-ESRGAN chunk {idx + 1}/{chunks}",
                        cancel=cancel,
                    )
                    self.invocations.append(
                        EngineInvocation(
                            argv=tuple(esrgan_cmd),
                            chunk_index=idx,
                            input_frames=input_frames,
                            tile=tile,
                        )
                    )
                    output_frames = _count_pngs(out_dir)
                    if output_frames != count:
                        raise UpscaleError(
                            f"chunk {idx + 1}/{chunks} engine output "
                            f"count mismatch: expected {count} frames, "
                            f"engine produced {output_frames}"
                        )

                    # Reassembly: the DERIVED stamp token (B.2) stamps
                    # the segment — the canonical token, never a
                    # container string, never a default. The segment
                    # filter degenerates to setsar=1 at scale == 2
                    # (design Section 3 geometry note).
                    self._ffmpeg(
                        [
                            tools.ffmpeg,
                            "-y", "-hide_banner", "-nostdin",
                            "-framerate", str(stamp["value"]),
                            "-i", str(out_dir / "frame_%08d.png"),
                            "-an",
                            "-vf", "setsar=1",
                            "-c:v", "ffv1",
                            "-level", "3",
                            str(segment),
                        ],
                        label=f"mezzanine segment {idx + 1}/{chunks}",
                        tools=tools,
                        cancel=cancel,
                    )
                    segments.append(segment)
                    frames_done += count
                    completed_records.append(
                        {
                            "idx": idx,
                            "count": count,
                            "segment": str(segment),
                            "segment_size": segment.stat().st_size,
                        }
                    )
                    # v0.4.5 (§3b): each reassembled segment's position
                    # rides the step seam ("reassembly chunk 5/12" +
                    # the cumulative counters) so the status names the
                    # assembly work between the GPU passes.
                    if self.on_step is not None:
                        self.on_step(
                            f"chunk {idx + 1}/{chunks} — "
                            f"reassembly chunk {idx + 1}/{chunks} "
                            f"({frames_done}/{total_frames} frames "
                            "upscaled so far)"
                        )
                    self._write_checkpoint(
                        workdir=workdir,
                        prepared_path=str(prepared_path),
                        prepared_total_frames=total_frames,
                        stamp_token=str(stamp["value"]),
                        stamp_fps=stamp_fps,
                        chunk_frames=frames_per_chunk,
                        params=checkpoint_params,
                        completed=sorted(
                            completed_records, key=lambda r: r["idx"]
                        ),
                    )
                    # v0.4.5 reassembly step rides the seam right ABOVE;
                    # this on_progress call is unchanged B.3 discipline.
                    if self.on_progress is not None:
                        self.on_progress(
                            min(frames_done, total_frames), total_frames
                        )
                except EngineCancelled:
                    # Partial chunk dirs are cleaned on cancel; completed
                    # segments stay in the kept workdir (design Section 7).
                    shutil.rmtree(in_dir, ignore_errors=True)
                    shutil.rmtree(out_dir, ignore_errors=True)
                    raise
                finally:
                    shutil.rmtree(in_dir, ignore_errors=True)
                    shutil.rmtree(out_dir, ignore_errors=True)

            # B.6: concat is one long external command over every
            # segment; the step label keeps the window observable.
            self._step("concat")
            if len(segments) == 1:
                shutil.move(str(segments[0]), str(output))
            else:
                concat = _write_concat_list(segments)
                try:
                    self._ffmpeg(
                        [
                            tools.ffmpeg,
                            "-y", "-hide_banner", "-nostdin",
                            "-f", "concat", "-safe", "0",
                            "-i", concat,
                            "-c", "copy",
                            str(output),
                        ],
                        label="mezzanine concat",
                        tools=tools,
                        cancel=cancel,
                    )
                finally:
                    try:
                        Path(concat).unlink()
                    except OSError:
                        pass
            if not output.is_file() or output.stat().st_size == 0:
                raise UpscaleError("upscale mezzanine was not created")
        except EngineCancelled:
            try:
                if output.exists():
                    output.unlink()
            except OSError:
                pass
            raise
        except Exception as exc:
            # B.5: a mezzanine that exists at failure time is RETAINED
            # by the caller (_run_and_report) — never deleted here.
            # Pre-mezzanine failures leave no output file; the kept
            # workdir (design Section 7) and the B.7 checkpoint cover
            # triage and resume.
            if isinstance(exc, UpscaleError):
                raise
            raise UpscaleError(str(exc)) from exc
        chunk_context = {
            "chunk_frames": frames_per_chunk,
            "chunks": chunks,
            "expected_per_chunk": expected_per_chunk,
        }
        return frames_done, chunk_context, resume_info

    def _write_checkpoint(
        self,
        *,
        workdir: Path,
        prepared_path: str,
        prepared_total_frames: int,
        stamp_token: str,
        stamp_fps: float,
        chunk_frames: int,
        params: dict,
        completed: list[dict],
    ) -> None:
        """B.7: write ``chunk_state.json`` atomically (temp + replace,
        fsynced) after each completed chunk. The GPU hours live in the
        chunk engine processes; this file is what a retried run
        inherits. Best-effort: a checkpoint that cannot be written
        costs resume, never the run."""
        state = {
            "stamp_fps": stamp_token,
            "prepared_path": prepared_path,
            "prepared_total_frames": prepared_total_frames,
            "chunk_frames": chunk_frames,
            "completed": completed,
            "params": params,
        }
        path = workdir / CHUNK_STATE_FILENAME
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(workdir), prefix=".chunk-state-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(state, indent=2) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, path)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._log(f"checkpoint write failed (resume lost): {exc}")

    def _load_checkpoint(
        self,
        *,
        workdir: Path,
        prepared_path: str,
        prepared_total_frames: int,
        stamp_token: str,
        stamp_fps: float,
        params: dict,
        tools: ToolBox,
    ) -> list[dict] | None:
        """B.7: validate ``chunk_state.json`` against THIS invocation.

        Cheap parts only (the design's trust model): params, prepared
        path, decode-counted total, and stamp token must match the
        fresh invocation EXACTLY; then each recorded segment must
        exist with its recorded size and probe a duration within ONE
        FRAME-TIME of ``count / stamp_fps`` (a kill-mid-segment write
        leaves a truncated file; the B.4 gates remain the real
        tripwire, so this integrity check is deliberately cheap, not
        cryptographic). ANY failure — mismatch, missing file, wrong
        size, truncated duration — deletes the workdir WHOLESALE and
        returns None (clean restart). No partial trust.
        """
        path = workdir / CHUNK_STATE_FILENAME
        if not path.is_file():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            shutil.rmtree(workdir, ignore_errors=True)
            return None
        mismatch = (
            state.get("prepared_path") != prepared_path
            or int(state.get("prepared_total_frames") or -1)
            != prepared_total_frames
            or state.get("stamp_fps") != stamp_token
            or state.get("params") != params
        )
        if mismatch:
            # stale cache by construction: delete wholesale, start clean
            shutil.rmtree(workdir, ignore_errors=True)
            workdir.mkdir(parents=True, exist_ok=True)
            return None
        completed = state.get("completed")
        if not isinstance(completed, list) or not completed:
            shutil.rmtree(workdir, ignore_errors=True)
            workdir.mkdir(parents=True, exist_ok=True)
            return None
        frame_time = 1.0 / max(stamp_fps, 1e-9)
        for entry in completed:
            try:
                segment = Path(str(entry["segment"]))
                count = int(entry["count"])
                size = int(entry["segment_size"])
            except (KeyError, TypeError, ValueError):
                shutil.rmtree(workdir, ignore_errors=True)
                workdir.mkdir(parents=True, exist_ok=True)
                return None
            if not segment.is_file() or segment.stat().st_size != size:
                shutil.rmtree(workdir, ignore_errors=True)
                workdir.mkdir(parents=True, exist_ok=True)
                return None
            duration = probe_duration_seconds(
                str(segment), ffprobe=tools.ffprobe, runner=self._seam_runner()
            )
            expected_duration = count / max(stamp_fps, 1e-9)
            if (
                duration is None
                or abs(duration - expected_duration) > frame_time
            ):
                shutil.rmtree(workdir, ignore_errors=True)
                workdir.mkdir(parents=True, exist_ok=True)
                return None
        return completed


def _workdir_for(prepared_path: str) -> Path:
    """``<log_root>/<run_id>.engine-work`` (design Section 7): the
    prepared file lives at ``<log_root>/<run_id>.prepared.mkv``, so the
    workdir sits beside it under the same log root."""
    prepared = Path(prepared_path)
    name = prepared.name
    if name.endswith(".prepared.mkv"):
        name = name[: -len(".prepared.mkv")]
    return prepared.parent / f"{name}.engine-work"


def _count_pngs(directory: Path) -> int:
    return sum(1 for _ in directory.glob("*.png"))


def _write_concat_list(segments: list[Path]) -> str:
    """concat demuxer list with single-quote escaping (sibling port)."""
    fh = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, suffix=".txt"
    )
    with fh:
        for segment in segments:
            path = str(segment).replace("'", "'\\''")
            fh.write(f"file '{path}'\n")
    return fh.name


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None