#!/usr/bin/env bash
# stitch-360 — Entrypoint for the Insta360 stitcher Docker image.
#
# Converts .insv files into equirectangular 360° MP4 and injects spatial
# metadata so YouTube recognises them as 360° video.
#
# Usage:
#   stitch-360 --output /output/FILE.mp4 /input/VID_*.insv [...]
#   stitch-360 --help
#
# The script auto-detects which stitcher is available:
#   1. MediaSDKTest  → full equirectangular + FlowState (best quality)
#   2. ffmpeg        → stream-copy fallback (dual-fisheye, not stitched)

set -euo pipefail

# ── Parse arguments ───────────────────────────────────────────────────────────

OUTPUT=""
INPUTS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --output|-o)
      OUTPUT="$2"
      shift 2
      ;;
    --help|-h)
      echo "Usage: stitch-360 --output /output/FILE.mp4 /input/*.insv [...]"
      echo ""
      echo "Stitches Insta360 X4 .insv files into equirectangular 360° MP4."
      echo "Auto-detects stitcher: MediaSDKTest (best) or ffmpeg (fallback)."
      exit 0
      ;;
    *)
      INPUTS+=("$1")
      shift
      ;;
  esac
done

if [ -z "$OUTPUT" ]; then
  echo "ERROR: --output is required" >&2
  echo "Usage: stitch-360 --output /output/FILE.mp4 /input/*.insv [...]" >&2
  exit 1
fi

if [ ${#INPUTS[@]} -eq 0 ]; then
  echo "ERROR: no input .insv files specified" >&2
  exit 1
fi

echo "==> Input: ${INPUTS[*]}"
echo "==> Output: $OUTPUT"

# ── Select stitcher ───────────────────────────────────────────────────────────

STITCHER=""
if command -v MediaSDKTest &>/dev/null; then
  STITCHER="mediasdk"
  echo "==> Stitcher: MediaSDK (equirectangular + FlowState)"
elif command -v ffmpeg &>/dev/null; then
  STITCHER="ffmpeg"
  echo "==> Stitcher: ffmpeg (stream-copy fallback — NOT properly stitched)"
  echo "    Install MediaSDK for proper 360° stitching."
  echo "    See: docker/build.sh --mediasdk"
else
  echo "ERROR: no stitcher available (need MediaSDKTest or ffmpeg)" >&2
  exit 1
fi

# ── Stitch ────────────────────────────────────────────────────────────────────

TEMP_OUTPUT="${OUTPUT}.tmp.mp4"

case "$STITCHER" in
  mediasdk)
    # MediaSDKTest takes a single input file; for multi-segment recordings
    # we use the first segment (MediaSDK reads subsequent segments automatically
    # when they're in the same directory with sequential naming).
    echo "==> Stitching with MediaSDK..."
    MediaSDKTest \
      -inputs "${INPUTS[0]}" \
      -output "$TEMP_OUTPUT" \
      -enable_flowstate \
      -enable_directionlock \
      -enable_denoise
    ;;
  ffmpeg)
    # Fallback: .insv is essentially an MP4 container. Stream-copy the video
    # and audio tracks without re-encoding. The result is a dual-fisheye
    # video — YouTube won't render it as 360° but the pipeline is testable.
    echo "==> Stream-copying with ffmpeg (dual-fisheye fallback)..."
    if [ ${#INPUTS[@]} -eq 1 ]; then
      ffmpeg -y -i "${INPUTS[0]}" \
        -map 0:v -map 0:a -c copy -movflags +faststart "$TEMP_OUTPUT"
    else
      # Multiple segments: concatenate then stream-copy
      CONCAT_FILE=$(mktemp /tmp/concat_XXXXXX.txt)
      for inp in "${INPUTS[@]}"; do
        echo "file '$inp'" >> "$CONCAT_FILE"
      done
      ffmpeg -y -f concat -safe 0 -i "$CONCAT_FILE" \
        -map 0:v -map 0:a -c copy -movflags +faststart "$TEMP_OUTPUT"
      rm -f "$CONCAT_FILE"
    fi
    ;;
esac

# Verify output
if [ ! -s "$TEMP_OUTPUT" ]; then
  echo "ERROR: stitcher produced no output or empty file" >&2
  rm -f "$TEMP_OUTPUT"
  exit 1
fi

# ── Inject 360° spatial metadata ──────────────────────────────────────────────

echo "==> Injecting 360° spatial metadata..."
exiftool -overwrite_original \
  -ProjectionType="equirectangular" \
  -XMP-GSpherical:Spherical="true" \
  -XMP-GSpherical:Stitched="true" \
  -XMP-GSpherical:ProjectionType="equirectangular" \
  "$TEMP_OUTPUT" \
|| echo "    WARNING: exiftool metadata injection failed (video still usable)"

# Move to final output path (atomic on same filesystem)
mv "$TEMP_OUTPUT" "$OUTPUT"

SIZE_MB=$(du -m "$OUTPUT" | cut -f1)
echo "==> Done: $OUTPUT (${SIZE_MB} MB)"
