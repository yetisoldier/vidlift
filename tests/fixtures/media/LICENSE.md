# tests/fixtures/media — fixtures license note

## fixture-cartoon.vob

A 4-second, 640x480 4:3 (square-pixel) DVD-class MPEG-2 VOB cut from a
public-domain cel-animation short:

- **Source short:** *Superman: The Mechanical Monsters* (Fleischer
  Studios, 1941; dir. Dave Fleischer).
- **Public-domain basis:** copyright not renewed; the work is in the
  public domain in the United States. The Internet Archive copy of the
  restored short is marked with a CC0 dedication at
  <https://archive.org/details/superman_mechanical_monsters>
  (metadata `licenseurl`: http://creativecommons.org/publicdomain/zero/1.0/).
- **Downloaded from:** <https://archive.org/download/superman_mechanical_monsters/superman_mechanical_monsters.mp4>
  (MP4 v2, h264 640x480, 609.9 s; downloaded 2026-10-26 and
  sha256-checked before the extract below).
- **Derivation (the fixture bytes):**

  ```
  ffmpeg -ss 145 -t 4 -i superman_mechanical_monsters.mp4 \
      -c:v mpeg2video -q:v 6 -c:a mp3 -b:a 128k -ar 44100 \
      fixture-cartoon.vob
  ```

  i.e. a real ffmpeg cut/re-encode at the brief's exact spec: ~2-5 s,
  640x480 (SD, 4:3 — the DVD-MPEG2-rip source class), MPEG-2 quality
  `q:v 6` in a VOB so the fixture exercises VidLift's
  `SOURCE_DVD_MPEG2_RIP` path end-to-end.

- **Content:** cel-shaded flat-fill regions with dark line-art
  mechanical walkers (the exact failure mode the general x2plus net
  invents false texture into) — the gate fixture for the v0.4.8 anime
  upscale profile.

The source is public-domain content; no license restricts the
redistribution of these derived bytes in this repository.