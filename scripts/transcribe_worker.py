"""Stateless transcription worker — runs on a Mac over Tailscale.

Accepts WAV file uploads via HTTP, runs faster-whisper (+ optional pyannote
speaker diarisation), and returns JSON results.  The Pi POSTs audio here when
``TRANSCRIBE_URL`` is configured.

Usage::

    # From the project root on the Mac:
    uv run uvicorn scripts.transcribe_worker:app --host 0.0.0.0 --port 8321

    # Or bind to Tailscale interface only:
    uv run uvicorn scripts.transcribe_worker:app --host 100.x.x.x --port 8321
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import uvicorn
from fastapi import FastAPI, Query, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

app = FastAPI(title="J105 Transcription Worker", docs_url=None, redoc_url=None)


@app.post("/transcribe")
async def transcribe(
    file: UploadFile,
    model_size: str = Query(default="base"),
    diarize: str = Query(default="true"),
) -> JSONResponse:
    """Accept a WAV upload, run whisper + optional diarisation, return JSON."""
    want_diarize = diarize.lower() in {"true", "1", "yes"}

    # Save the uploaded WAV to a temp file (whisper needs a file path)
    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
        content = await file.read()
        tmp.write(content)

    try:
        logger.info(
            "Transcribing: {} ({:.1f} MB) model={} diarize={}",
            file.filename,
            len(content) / 1_048_576,
            model_size,
            want_diarize,
        )

        from logger.transcribe import (
            _pyannote_available,
            _run_whisper,
            _run_with_diarization,
        )

        use_diarize = want_diarize and bool(os.environ.get("HF_TOKEN")) and _pyannote_available()

        if use_diarize:
            text, segments_json_str = _run_with_diarization(
                file_path=tmp_path, model_size=model_size
            )
            segments: list[dict[str, Any]] = json.loads(segments_json_str)
        else:
            text = _run_whisper(file_path=tmp_path, model_size=model_size)
            segments = [{"start": 0.0, "end": 0.0, "text": text}]

        logger.info("Done: {} chars, {} segments", len(text), len(segments))
        return JSONResponse({"text": text, "segments": segments})
    finally:
        os.unlink(tmp_path)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8321)
