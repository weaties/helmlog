"""Video-derived fleet analysis tooling (#839).

Offline scripts that read the 360° race videos at named instants (gun,
roundings, finish), cache frames and structured observations in a sidecar
SQLite ledger, and derive fleet-relative facts for the season report.
Design: ``docs/video-analysis.md``.

Run as modules from the repo root::

    uv run python -m scripts.analysis.video <command> [options]

None of this runs on the Pi; it needs ``ffmpeg`` and ``yt-dlp`` on the dev Mac.
"""
