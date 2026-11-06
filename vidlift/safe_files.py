"""Atomic no-clobber publication on a single local filesystem.

Hard-link publication is atomic against late files and dangling symlinks.
Unsupported filesystems fail closed; do not emulate it with check/replace.
"""
from __future__ import annotations
import os
from pathlib import Path
import tempfile


def publish_new(source, destination):
    """Publish a completed regular temp without replacing any existing name."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise OSError("publication requires a regular completed file")
    os.link(source, destination, follow_symlinks=False)
    # Publication succeeded. Failure to remove the private temp must not turn
    # the accepted output into an apparent failure inviting destructive retry.
    try:
        source.unlink()
    except OSError:
        pass


def write_new_bytes(destination, data):
    destination = Path(destination)
    fd, temp = tempfile.mkstemp(dir=destination.parent, prefix=".vidlift-write-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        publish_new(temp, destination)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
