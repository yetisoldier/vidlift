"""Input validation for the eight MCP tools (mcp-bridge.md Sections 4-5.2).

Every incoming ``tools/call`` is validated against its pinned schema
BEFORE dispatch: unknown fields are rejected, types are checked
(including the options sub-object), and required fields must be
present. The schemas double as the ``inputSchema`` surface
``tools/list`` reports, so the registered shapes ARE the validated
shapes — one source of truth (the v0.2.32 lesson).

v0.4.0 note (design B.6): ``status``'s payload already carries the
runner's whole ``progress`` dict (it flows through
``RunState.to_dict()``); the runner's step labels and heartbeat make
that field MEAN something during the previously-blind windows
(concat, gate probes, encode, verify). No schema change was needed —
this module documents that fact instead of inventing a new field.
"""

from __future__ import annotations

__all__ = [
    "InvalidInput",
    "TOOL_SCHEMAS",
    "TOOL_DESCRIPTIONS",
    "validate_tool_args",
]


class InvalidInput(Exception):
    """Schema validation failed; ``field`` names the offending field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


_OPTIONS_PLAN = {
    "target": {"type": "string"},
    "gb_per_hour": {"type": "number"},
    "output_dir": {"type": ["string", "null"]},
}

_OPTIONS_RUN = {
    "target": {"type": "string"},
    "gb_per_hour": {"type": "number"},
    "output_dir": {"type": ["string", "null"]},
    "ffmpeg": {"type": ["string", "null"]},
    "ffprobe": {"type": ["string", "null"]},
    "log_dir": {"type": ["string", "null"]},
    # v0.4.7: opt-in delete-and-replace (design guardrail a: default OFF;
    # the runner only ever replaces AFTER verify_output passes).
    "replace_source": {"type": "boolean"},
    # v0.4.8 (design D5): the upscale model profile ("live-action" |
    # "anime"), string not enum matching "target"'s permissive shape —
    # value validation happens in run_file's select_engine path, where
    # Unknown profile: run_file's selection-time fail-open turns the
    # UpscaleError into an honest no-AI success naming the profile
    # (NOT a job failure — the bridge's job-failure path is never
    # reached; CLI/GUI refuse an unknown value outright instead).
    # Absent -> "live-action" (v0.4.7 behavior).
    "upscale_profile": {"type": "string"},
}

# The binding tool surface (mcp-bridge.md Section 4, verbatim shapes).
TOOL_SCHEMAS: dict[str, dict] = {
    "probe": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
        "required": ["paths"],
    },
    "plan": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "options": {"type": "object", "properties": _OPTIONS_PLAN},
        },
        "required": ["paths"],
    },
    "benchmark": {
        "type": "object",
        "properties": {
            "sample_path": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
        "required": ["sample_path"],
    },
    "run": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "options": {"type": "object", "properties": _OPTIONS_RUN},
        },
        "required": ["paths"],
    },
    "status": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    "cancel": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    "list": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer"},
            "include_history": {"type": "boolean"},
        },
    },
    "manifest": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "probe": (
        "Probe and classify video files: container, streams, source class, "
        "displayed geometry, upscale verdict. Per-file isolation: one "
        "unreadable file never fails the batch."
    ),
    "plan": (
        "Pre-flight plan: verdict, output path and geometry, bitrate plan, "
        "estimated size, honest expectations, and what the plan cannot know."
    ),
    "benchmark": (
        "Measure one short slice through the active engine to pin an ETA "
        "constant for this machine. Runs as a tracked job; results cached "
        "per (engine, model, geometry bucket)."
    ),
    "run": (
        "Enqueue files through the full runner pipeline (probe, plan, "
        "prepare, upscale, encode, verify). Returns immediately with job "
        "ids; poll status/manifest. Nothing requires this session to stay "
        "open."
    ),
    "status": (
        "The full runner observability contract for one job: state, phase, "
        "log tail, commands, phase timings, engine honesty fields, "
        "verification, measured ETA (null unless measured). During long "
        "steps the payload's runner progress carries a 'step' label and "
        "last_activity_at advances (v0.4.0 heartbeat)."
    ),
    "cancel": (
        "Cooperative cancel. Queued jobs are removed without starting; "
        "running jobs observe the cancel flag; finished or unknown jobs are "
        "reported non-events."
    ),
    "list": (
        "Queue depth, running jobs, and terminal history from the "
        "persisted registry."
    ),
    "manifest": (
        "The run's artifact manifest exactly as the runner wrote it, "
        "including engine_simulated, engine_note, and any fail-open notes. "
        "Never filtered, renamed, or redacted."
    ),
}


def _type_ok(value, spec) -> bool:
    types = spec.get("type")
    if isinstance(types, str):
        types = [types]
    for name in types or []:
        if name == "string" and isinstance(value, str):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "null" and value is None:
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
    return False


def _validate_object(props: dict, value: dict, where: str) -> None:
    for key in value:
        if key not in props:
            raise InvalidInput(key, f"unknown field '{where}{key}'")
    for key, spec in props.items():
        if key not in value:
            continue
        item = value[key]
        if not _type_ok(item, spec):
            expected = spec.get("type")
            if isinstance(expected, list):
                expected = "|".join(expected)
            raise InvalidInput(
                key,
                f"field '{where}{key}' must be of type {expected}; "
                f"got {type(item).__name__}",
            )
        if isinstance(item, list) and "items" in spec:
            item_spec = spec["items"]
            for i, element in enumerate(item):
                if not _type_ok(element, item_spec):
                    raise InvalidInput(
                        key,
                        f"field '{where}{key}[{i}]' must be of type "
                        f"{item_spec.get('type')}",
                    )
        minimum = spec.get("minItems")
        if (
            isinstance(item, list)
            and minimum is not None
            and len(item) < minimum
        ):
            raise InvalidInput(
                key, f"field '{where}{key}' needs at least {minimum} item(s)"
            )
        if isinstance(item, dict) and "properties" in spec:
            _validate_object(spec["properties"], item, f"{where}{key}.")


def validate_tool_args(name: str, args: object) -> dict:
    """Validate one tool call's arguments against the pinned schema.

    Raises :class:`InvalidInput` naming the field; returns the (valid)
    arguments dict unchanged — defaults are applied by the tool
    adapters, not here (mapping stays in tools.py).
    """
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        raise InvalidInput("name", f"unknown tool: {name}")
    if not isinstance(args, dict):
        raise InvalidInput("arguments", "tool arguments must be a JSON object")
    _validate_object(schema["properties"], args, "")
    for required in schema.get("required", []):
        if required not in args:
            raise InvalidInput(required, f"missing required field '{required}'")
    return args