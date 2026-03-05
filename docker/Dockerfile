# Insta360 X4 video stitcher — equirectangular 360° MP4 output.
#
# Two build modes controlled by the STITCHER build arg:
#
#   STITCHER=ffmpeg     (default) — stream-copy fallback; outputs dual-fisheye
#                        MP4 without proper stitching. Good for testing the
#                        pipeline end-to-end before MediaSDK is available.
#
#   STITCHER=mediasdk   — full equirectangular stitching with FlowState
#                        stabilization, denoise, and direction lock. Requires
#                        placing libMediaSDK-dev-*.deb in docker/ before build.
#
# Build:
#   docker/build.sh                   # ffmpeg fallback (works today)
#   docker/build.sh --mediasdk        # full stitching (requires .deb)
#
# Run:
#   docker run --rm \
#     -v /path/to/DCIM/Camera01:/input:ro \
#     -v ~/Videos/j105:/output \
#     insta360-cli-utils \
#     --output /output/20260810_140530.mp4 /input/VID_20260810_140530_00_000.insv

FROM ubuntu:22.04

ARG STITCHER=ffmpeg
ARG DEBIAN_FRONTEND=noninteractive

# ── Base packages ─────────────────────────────────────────────────────────────

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libimage-exiftool-perl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── MediaSDK (optional) ──────────────────────────────────────────────────────
# Place the .deb from Insta360 in docker/ before building with --mediasdk.
# The .keep file ensures COPY succeeds even when no .deb files exist.

COPY .keep libMediaSDK-dev*.deb /tmp/
RUN if [ "$STITCHER" = "mediasdk" ]; then \
        DEB=$(ls /tmp/libMediaSDK-dev*.deb 2>/dev/null | head -1); \
        if [ -n "$DEB" ]; then \
            dpkg -i "$DEB" || (apt-get update && apt-get install -yf); \
            rm -f /tmp/libMediaSDK-dev*.deb; \
            echo "MediaSDK installed successfully"; \
        else \
            echo "ERROR: STITCHER=mediasdk but no libMediaSDK-dev*.deb in docker/" >&2; \
            exit 1; \
        fi; \
    fi && rm -f /tmp/.keep

# ── Entrypoint ────────────────────────────────────────────────────────────────

COPY stitch-360.sh /usr/local/bin/stitch-360
RUN chmod +x /usr/local/bin/stitch-360

ENTRYPOINT ["stitch-360"]
