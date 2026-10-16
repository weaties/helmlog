"""Drag-to-pan for 360° YouTube videos on the session page (#844).

Two layers, same convention as ``test_moments_markdown.py``:

* **Wiring checks** (always run): ``session.html`` has the pan layer inside
  ``#yt-player-wrap`` and loads ``video_pan.js`` before ``session.js``, and
  ``session.js`` hooks the layer up to the YT player.
* **Behavioural check** (skipped if ``node`` is absent): ``tests/js/
  video_pan_check.mjs`` exercises the pure view math (drag direction, yaw
  wrap, pitch clamp, spherical detection, click-vs-drag).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src" / "helmlog"
_HARNESS = _REPO_ROOT / "tests" / "js" / "video_pan_check.mjs"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text()


def test_template_has_pan_layer_inside_player_wrap() -> None:
    html = _read("templates/session.html")
    wrap = html.index('id="yt-player-wrap"')
    layer = html.index('id="yt-pan-layer"')
    player = html.index('id="yt-player"')
    # The layer sits after #yt-player (so YT's iframe replacement doesn't wipe
    # it) and before the wrap closes; overlay SVGs appended later stack above.
    assert wrap < player < layer
    assert html.index("</div>", layer) < html.index("live-photo-card")


def test_template_loads_video_pan_before_session_js() -> None:
    html = _read("templates/session.html")
    assert "/static/video_pan.js" in html
    assert html.index("/static/video_pan.js") < html.index("/static/session.js")


def test_session_js_wires_pan_layer() -> None:
    js = _read("static/session.js")
    assert "attachVideoPan(" in js
    # The stale claim that embeds can't pan must be gone.
    assert "360 video panning is NOT supported" not in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_video_pan_math_behaviour() -> None:
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"video pan harness failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.startswith("OK "), result.stdout
