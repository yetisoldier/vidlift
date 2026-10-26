"""VidLift: batch AI upscaler for SD video files to 720p HEVC MP4.

Private personal tool. v0.2 adds the batch pre-flight planner and a
PyQt planning UI on top of the probe/classify core. The encode runner
consumes the validated bluray-to-mp4 upscale path (v0.3.0) and the
decode-truth clock/frame-exact chunking port (v0.4.0).
"""

__version__ = "0.4.7"
