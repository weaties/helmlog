"""Minimal server entry point for Playwright E2E tests.

Creates an in-memory Storage and starts the FastAPI app with AUTH_DISABLED=true.
Usage: uv run python tests/e2e/serve.py
"""

import asyncio
import os
import sys

import uvicorn

# Ensure auth is disabled and DB is in-memory for tests
os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("DB_PATH", ":memory:")


async def main() -> None:
    from helmlog.storage import Storage, StorageConfig
    from helmlog.web import create_app

    storage = Storage(StorageConfig(db_path=":memory:"))
    await storage.connect()
    app = create_app(storage)

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
