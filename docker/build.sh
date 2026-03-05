#!/usr/bin/env bash
# build.sh — Build the Insta360 stitcher Docker image.
#
# Usage:
#   ./docker/build.sh              # ffmpeg fallback (works today)
#   ./docker/build.sh --mediasdk   # full stitching (requires .deb in docker/)
#
# The image is tagged as 'insta360-cli-utils' so process-videos.sh works
# without any configuration changes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="insta360-cli-utils"
STITCHER="ffmpeg"

while [ $# -gt 0 ]; do
  case "$1" in
    --mediasdk)
      STITCHER="mediasdk"
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--mediasdk]"
      echo ""
      echo "  (default)     Build with ffmpeg fallback (stream-copy, no stitching)"
      echo "  --mediasdk    Build with Insta360 MediaSDK (requires .deb in docker/)"
      echo ""
      echo "Both tag the image as '$IMAGE_NAME'."
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# Check for MediaSDK .deb if requested
if [ "$STITCHER" = "mediasdk" ]; then
  DEB_COUNT=$(find "$SCRIPT_DIR" -maxdepth 1 -name 'libMediaSDK-dev*.deb' | wc -l | tr -d ' ')
  if [ "$DEB_COUNT" -eq 0 ]; then
    echo "ERROR: --mediasdk requires libMediaSDK-dev*.deb in docker/" >&2
    echo "" >&2
    echo "Steps to get the MediaSDK:" >&2
    echo "  1. Apply at https://www.insta360.com/sdk/apply" >&2
    echo "  2. Download the Linux SDK (Ubuntu 22.04 .deb)" >&2
    echo "  3. Place the .deb file in: $SCRIPT_DIR/" >&2
    echo "  4. Re-run: $0 --mediasdk" >&2
    exit 1
  fi
  echo "==> Building with MediaSDK (full equirectangular stitching)"
else
  echo "==> Building with ffmpeg fallback (stream-copy, dual-fisheye)"
  echo "    For proper 360° stitching, rebuild with: $0 --mediasdk"
fi

echo "==> Image: $IMAGE_NAME"
echo ""

docker build \
  --build-arg "STITCHER=$STITCHER" \
  -t "$IMAGE_NAME" \
  "$SCRIPT_DIR"

echo ""
echo "==> Build complete: $IMAGE_NAME"
echo ""
echo "Test with:"
echo "  docker run --rm $IMAGE_NAME --help"
