# VidLift Roadmap

## Shipped

- source probe and classification
- batch pre-flight planner in the CLI and desktop app
- real Real-ESRGAN ncnn/Vulkan upscale engine
- decode-truth frame clock and frame-exact chunking
- 720p HEVC MKV encode and full output verification
- text-subtitle pass-through and bitmap subtitle OCR
- logs, manifests, heartbeat activity, cancellation, and resumable chunks
- benchmark-based ETA support
- MCP bridge with FIFO bulk queue, status, cancel, and manifest tools
- Windows 11 x64 installer with bundled runtime dependencies

## Next

1. Add GUI start, cancel, queue status, and per-file progress controls.
2. Add native bulk-run syntax to the CLI.
3. Validate the installer and full real-engine path on a wider sample matrix:
   Xvid/DivX AVI, MPEG-2 DVD rip, MOV/MP4, WMV, and unusual subtitle layouts.
4. Add Authenticode signing for a warning-free Windows install.
5. Add selectable output presets beyond 720p.
6. Improve OCR language selection and review tools.

## Deferred

- watch folders
- automatic metadata lookup
- media-library management
- PAL-speedup correction
- chapter preservation
- 1080p and 4K targets
- CUDA runtime bundling and reproducibility gate (`GPU-1`)
- transcription watchdog, batch identity, and the CUDA failure latch (`RUN-1`)
- single-instance enforcement (`GUI-2`)
- registry transactions, receipts, and submission identity (`BRIDGE-1`)
- media transaction journal and no-clobber publication (`MEDIA-1`)

## Deferred-scope backlog (v0.4.22.0 trim)

The v0.4.22.0 scope restore cut five workstreams out of the then-current tree
(they shipped in v0.4.20.0/v0.4.21.0, whose tags `v0.4.20.0`/`v0.4.21.0` are
the authoritative source trees). Each carries a deliberate revive trigger —
a field-evidence or owner-decision event, never "someone feels like it".

| ID | Deferred scope (source of truth) | Revive trigger (one line) |
|----|----------------------------------|---------------------------|
| **GPU-1** | CUDA runtime bundling: `packaging/{acquire_cuda_runtime,pyinstaller_cuda,build_identity}.py`, `cuda_runtime_manifest.json`, `gpu_gate.ps1`, `vidlift/cuda_runtime.py`, workflow CUDA steps, plus the NVIDIA legal gate, reproducible-`zlibwapi.dll` supplier, and installer-size baseline decision (~375 MB → ~1.9 GB) | Written NVIDIA redistribution clearance for the exact artifacts is obtained **and** Eric accepts the installer-size step change. |
| **RUN-1** | Transcription watchdog, batch identity, CUDA failure latch: `vidlift/transcription_watchdog.py`, `transcribe.py`/`run.py`/`cli.py` supervision integration, `freeze_support` entrypoints | A reproducible hung/un-cancellable or silently-dead transcription occurs on the .19-style in-process path (or Windows frozen evidence shows real hangs the watchdog would have caught). |
| **GUI-2** | Single-instance enforcement: `vidlift/ui/single_instance.py`, `gui.py` guard wiring, POSIX endpoint lease | Demonstrated harm from two concurrent GUI instances sharing one state dir (registry/settings corruption report), or Eric explicitly requests single-instance behavior. |
| **BRIDGE-1** | Registry transactions/receipts/identity: `vidlift/mcp_bridge/registry_transactions.py`, receipt UX in the GUI, cross-process claim protocol | A real duplicate/lost-job incident (Karen F11 class) is observed in supported usage, or concurrent GUI+CLI/multi-process operation becomes a supported mode. |
| **MEDIA-1** | Media transaction journal + no-clobber publication: `vidlift/media_transaction.py`, `vidlift/safe_files.py`, `verify_mux`, independent remux verification | Eric approves enabling destructive replace commits under journal+backup guarantees (answering handoff §6-Q3), **or** a field data-loss incident on the replace/sidecar paths. |

Cross-reference: GPU-1 subsumes the release-gates machinery (`release_gates.py`,
`size-baseline.json`) and the wheel-artifact identity tests; RUN-1 subsumes
batch identity (`test_gui_batch_identity.py`-class UX).
