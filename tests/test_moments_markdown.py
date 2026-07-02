"""Markdown rendering + author edit UI for moment comments (#809).

The rendering itself is client-side JS, so these tests split into two layers:

* **Wiring / hook-point checks** (always run): the vendored libs are served,
  ``session.html`` loads them before ``session.js``, and ``session.js`` /
  ``shared.js`` contain the render + edit + creator-id hooks. These fail loudly
  if a refactor strips a piece the feature depends on — the same convention as
  ``test_session_js_has_deeplink_parser``.

* **Behavioural check** (skipped if ``node`` is absent): ``tests/js/
  markdown_render_check.mjs`` exercises the markdown-it + hljs + task-list
  pipeline and asserts GFM parity plus that the primary XSS vectors (raw HTML in
  source, ``javascript:``/``data:``/``vbscript:`` link schemes) are neutralised
  before the string reaches DOMPurify. GitHub-hosted CI runners ship node, so
  this runs there; it degrades to a skip locally when node is missing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from helmlog.storage import Storage, StorageConfig
from helmlog.web import create_app

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDOR = _REPO_ROOT / "src" / "helmlog" / "static" / "vendor"
_HARNESS = _REPO_ROOT / "tests" / "js" / "markdown_render_check.mjs"

_VENDOR_FILES = [
    "markdown-it.min.js",
    "markdown-it-task-lists.min.js",
    "purify.min.js",
    "highlight.min.js",
    "highlight-github-dark.min.css",
]


async def _storage() -> Storage:
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    return s


def _read(name: str) -> str:
    return (_REPO_ROOT / "src" / "helmlog" / "static" / name).read_text()


# ---------------------------------------------------------------------------
# Vendored assets are present and served
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _VENDOR_FILES)
def test_vendor_file_exists(name: str) -> None:
    f = _VENDOR / name
    assert f.is_file(), f"missing vendored asset {name}"
    assert f.stat().st_size > 500, f"vendored asset {name} looks truncated"


@pytest.mark.asyncio
async def test_vendor_assets_served() -> None:
    storage = await _storage()
    try:
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for name in _VENDOR_FILES:
                resp = await client.get(f"/static/vendor/{name}")
                assert resp.status_code == 200, name
    finally:
        await storage.close()


def test_vendor_globals_present() -> None:
    """The vendored bundles export the globals session.js reaches for."""
    assert "markdownit" in (_VENDOR / "markdown-it.min.js").read_text()
    assert "DOMPurify" in (_VENDOR / "purify.min.js").read_text()
    assert "hljs" in (_VENDOR / "highlight.min.js").read_text()
    assert "markdownitTaskLists" in (_VENDOR / "markdown-it-task-lists.min.js").read_text()


# ---------------------------------------------------------------------------
# session.html wiring — libs load before session.js, highlight theme linked
# ---------------------------------------------------------------------------


def test_session_html_loads_vendor_before_session_js() -> None:
    html = (_REPO_ROOT / "src" / "helmlog" / "templates" / "session.html").read_text()
    for asset in (
        "/static/vendor/markdown-it.min.js",
        "/static/vendor/markdown-it-task-lists.min.js",
        "/static/vendor/purify.min.js",
        "/static/vendor/highlight.min.js",
    ):
        assert asset in html, f"session.html does not load {asset}"
        assert html.index(asset) < html.index("/static/session.js"), (
            f"{asset} must load before session.js"
        )
    assert "/static/vendor/highlight-github-dark.min.css" in html


# ---------------------------------------------------------------------------
# session.js — render pipeline hook points
# ---------------------------------------------------------------------------


def test_session_js_has_markdown_pipeline() -> None:
    js = _read("session.js")
    assert "function renderMarkdown" in js
    assert "function stripMarkdown" in js
    assert "DOMPurify.sanitize" in js
    assert "_MD_ALLOWED_TAGS" in js
    assert "_injectMentionsIntoDom" in js
    # markdown-it must be configured to NOT parse raw HTML from source.
    assert "html: false" in js
    # Comment bodies render markdown; subject/counterparty stay plain esc().
    assert "renderMarkdown(c.body)" in js


def test_session_js_preserves_mentions_and_fallback() -> None:
    js = _read("session.js")
    # @mention highlighting is still applied (now via DOM text-node walk).
    assert "_renderMentions" in js
    # Skips code/pre/anchor subtrees so code samples aren't mangled.
    assert "'CODE'" in js and "'PRE'" in js


# ---------------------------------------------------------------------------
# session.js — author/admin edit UI
# ---------------------------------------------------------------------------


def test_session_js_has_edit_ui() -> None:
    js = _read("session.js")
    assert "function editComment" in js
    assert "function _saveCommentEdit" in js
    assert "function _cancelCommentEdit" in js
    # Creator-or-admin gate, matching the backend rule in routes/comments.py.
    assert "_currentUserId" in js
    assert "_userRole === 'admin'" in js
    # Edit posts to the existing endpoint and stashes raw markdown for re-edit.
    assert "/api/comments/" in js
    assert "data-raw=" in js
    assert "btn-edit-comment" in js


def test_shared_js_captures_current_user_id() -> None:
    js = _read("shared.js")
    assert "_currentUserId" in js
    assert "u.id" in js


# ---------------------------------------------------------------------------
# Behavioural: markdown-it + hljs + task-lists pipeline (needs node)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_markdown_pipeline_behaviour() -> None:
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"markdown render harness failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.stdout.startswith("OK "), result.stdout
