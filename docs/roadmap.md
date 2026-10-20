# VidLift Roadmap

## Shipped

- source probe and classification
- batch pre-flight planner in the CLI and desktop app
- real Real-ESRGAN ncnn/Vulkan upscale engine
- decode-truth frame clock and frame-exact chunking
- 720p HEVC MP4 encode and full output verification
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
