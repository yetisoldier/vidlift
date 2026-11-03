# Release Notes — VidLift v0.4.16.0

## Summary
This release focuses on transcription model observability, remote monitoring discovery, and critical fixes for job cancellation and run-honesty.

## 🚀 New Features

### Transcription Model Management
- **One-Click Install:** The GUI now detects if the `faster-whisper` medium model is missing and provides an explicit **Install** button.
- **Background Installation:** Model downloads run on a background thread to prevent GUI freezes and avoid blocking the serial job queue.
- **Real-time Progress:** Download progress is now published to the GUI and the `/status` API, eliminating "silent stalls" during first-use model fetches.
- **Integrity Checks:** All model downloads are verified via SHA-256 pins before being marked as complete.

### Remote Monitoring Enhancements
- **Discovery Card:** A new `monitor.json` discovery card allows remote agents to automatically resolve the current effective monitor token and port.
- **Machine-Wide Visibility:** The `/status` API envelope now includes a `transcription_model` block, allowing watchers to see the model's state (installed/downloading) even when no jobs are active.

## 🔧 Fixes & Hardening

### Job Cancellation
- **Reliable Cancellation:** Fixed a critical issue where "Cancel Current" or "Cancel Batch" could be ignored if the job was outside the engine's upscale phase. Jobs now observe the cancel flag at every phase boundary.
- **Subprocess Termination:** The `_subprocess_runner` is now cancel-aware, ensuring that underlying FFmpeg/ffprobe children are actually terminated when a job is cancelled.

### Run Honesty & Validation
- **Mode A Heartbeat Parity:** Model downloads during a run now touch the `JobRecord`, preventing the GUI's stale-activity warning from triggering during long fetches.
- **Correct Device Echo:** Fixed a manifest path error that caused finished transcription jobs to report `device: null` to remote watchers.
- **Lock Hardening:** The per-tag download lock now correctly degrades to OPEN if the cache directory becomes read-only, preventing infinite retry loops.
- **Discovery Card Security:** The discovery card is now created with restricted permissions (0o600) to prevent token leakage on shared machines.

## ⚠️ Known Issues
- **Lock Stale-Reclaim Spin:** In specific edge cases where a read-only directory contains a stale lock, the reclaim logic may spin. This is a non-gating issue and does not affect general operation.
