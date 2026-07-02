# Vendored frontend libraries

These are third-party browser libraries vendored as static assets so the moments
discussion can render GitHub-Flavored Markdown with **no build step** (HelmLog
ships vanilla JS — see `AGENTS.md`). They are loaded as plain `<script>` /
`<link>` tags from `templates/session.html` and consumed by
`renderMarkdown()` in `static/session.js` (#809).

Do not hand-edit the `.min.*` files. To upgrade, re-download the exact
`dist`/`build` artifact for the new version from the same source and update the
version + integrity below.

| File | Package | Version | Source | License |
|---|---|---|---|---|
| `markdown-it.min.js` | markdown-it | 14.1.0 | `cdn.jsdelivr.net/npm/markdown-it@14.1.0/dist/markdown-it.min.js` | MIT |
| `markdown-it-task-lists.min.js` | markdown-it-task-lists | 2.1.1 | `cdn.jsdelivr.net/npm/markdown-it-task-lists@2.1.1/index.js` (wrapped as a browser global) | ISC |
| `purify.min.js` | DOMPurify | 3.2.4 | `cdn.jsdelivr.net/npm/dompurify@3.2.4/dist/purify.min.js` | Apache-2.0 OR MPL-2.0 |
| `highlight.min.js` | highlight.js | 11.10.0 | `cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/highlight.min.js` | BSD-3-Clause |
| `highlight-github-dark.min.css` | highlight.js theme (github-dark) | 11.10.0 | `…/build/styles/github-dark.min.css` | BSD-3-Clause |

HelmLog is dark-first (single `:root` theme in `base.css`), so only the
`github-dark` highlight theme is vendored.

`markdown-it-task-lists.min.js` is the only file that is not byte-for-byte
upstream: the upstream package ships CommonJS only, so it is wrapped in a UMD
shim exposing `window.markdownitTaskLists`. The wrapper is annotated at the top
of the file.

## Security note

Rendered markdown is **always** passed through `DOMPurify.sanitize()` with a
strict allowlist before insertion into the DOM (`renderMarkdown()` in
`session.js`). markdown-it itself is configured with `html: false` so raw HTML
in comment source is escaped, not parsed. See the XSS tests in
`tests/test_moments_markdown.py`.
