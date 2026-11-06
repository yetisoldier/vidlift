"""Prepared contexts for legacy synthetic helper tests, never production trust."""
from pathlib import Path
from vidlift.media_transaction import prepare_media_operation, record_verified_output


def bind(source, output, *, target=None, srt=None):
    source, output = Path(source), Path(output)
    op = prepare_media_operation(source, target or source, replace_source=True, suppress_srt=False)
    op.fallback = str(output)
    if srt and Path(srt).is_file():
        private = op.directory / Path(srt).name
        private.write_bytes(Path(srt).read_bytes())
    else:
        private = None
    op.sidecar_target = str(Path(target or source).with_suffix('.srt'))
    record_verified_output(op, output, verification=lambda _: {'ok': True, 'kind': 'synthetic-helper-test', 'checks': ['fixture-bytes']}, owned_srt=private)
    return op
