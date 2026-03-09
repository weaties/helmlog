"""Minimal entry point for running helmlog web server in a Docker container.

Creates an in-memory or file-backed Storage, runs migrations, and starts
uvicorn serving the FastAPI app with federation endpoints.

Usage:
    python -m tests.integration.serve
    # or
    uv run python tests/integration/serve.py
"""

from __future__ import annotations

import asyncio
import os


async def main() -> None:
    import uvicorn

    from helmlog.storage import Storage, StorageConfig
    from helmlog.web import create_app

    db_path = os.environ.get("DB_PATH", ":memory:")
    port = int(os.environ.get("HELMLOG_PORT", "8080"))
    host = os.environ.get("WEB_HOST", "0.0.0.0")

    storage = Storage(StorageConfig(db_path=db_path))
    await storage.connect()

    app = create_app(storage)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
