"""Shared pytest helpers for VidLift's test suite (internal)."""

from __future__ import annotations


def make_wrapper(*args, **kwargs):
    """The v0.4.15.2 transcribe wrapper under test (import inside the
    helper so a missing implementation fails these tests, not the whole
    suite's collection)."""
    from vidlift.transcribe import make_whisper_engine_with_fallback

    return make_whisper_engine_with_fallback(*args, **kwargs)
