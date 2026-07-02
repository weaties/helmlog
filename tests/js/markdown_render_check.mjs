// Behavioural check for the vendored markdown pipeline used by comment bodies
// (#809). This exercises the *library layer* the same way session.js configures
// it — markdown-it with html:false + linkify + the hljs highlight callback +
// the task-lists plugin — and asserts both GFM feature parity and that the
// primary XSS vectors (raw HTML in source, dangerous link schemes) are
// neutralised before the string ever reaches DOMPurify.
//
// Run standalone: `node tests/js/markdown_render_check.mjs`
// It prints "OK <n> checks" and exits 0, or prints the failing check and exits
// 1. The pytest wrapper (tests/test_moments_markdown.py) shells out to this and
// skips if node is unavailable. DOM-dependent behaviour (DOMPurify sanitize,
// @mention injection) is covered by the string-presence tests in that file, as
// the repo has no jsdom dependency.

import { createRequire } from 'module';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const vendor = join(here, '..', '..', 'src', 'helmlog', 'static', 'vendor');

const markdownit = require(join(vendor, 'markdown-it.min.js'));
const taskLists = require(join(vendor, 'markdown-it-task-lists.min.js'));
const hljs = require(join(vendor, 'highlight.min.js'));

// Mirror the renderMarkdown() config in session.js.
const md = markdownit({
  html: false,
  linkify: true,
  breaks: true,
  typographer: false,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return '<pre class="hljs"><code>'
          + hljs.highlight(str, { language: lang, ignoreIllegals: true }).value
          + '</code></pre>';
      } catch (e) { /* fall through */ }
    }
    return '<pre class="hljs"><code>' + md.utils.escapeHtml(str) + '</code></pre>';
  },
}).use(taskLists);

let count = 0;
function check(name, cond) {
  count += 1;
  if (!cond) {
    console.error('FAIL: ' + name);
    process.exit(1);
  }
}

// --- XSS / injection vectors -------------------------------------------------
const scriptOut = md.render('<script>alert(1)</script>');
check('raw <script> in source is escaped, not emitted',
  !/<script/i.test(scriptOut) && scriptOut.includes('&lt;script&gt;'));

const imgOut = md.render('<img src=x onerror=alert(1)>');
// The attribute text survives as escaped plain text; what matters is no live tag.
check('raw <img onerror> in source is escaped', !/<img/i.test(imgOut) && imgOut.includes('&lt;img'));

// markdown-it's default validateLink refuses dangerous schemes: it emits no
// <a> at all, leaving the raw text. The safety property is "no live anchor
// carrying the scheme", not "substring absent" (the scheme survives as text).
const jsLink = md.render('[click](javascript:alert(1))');
check('javascript: link scheme makes no anchor',
  !/href="javascript:/i.test(jsLink) && !/<a\b/i.test(jsLink));

const dataLink = md.render('[click](data:text/html;base64,PHNjcmlwdD4=)');
check('data:text/html link scheme makes no anchor',
  !/href="data:/i.test(dataLink) && !/<a\b/i.test(dataLink));

const vbLink = md.render('[click](vbscript:msgbox(1))');
check('vbscript: link scheme makes no anchor',
  !/href="vbscript:/i.test(vbLink) && !/<a\b/i.test(vbLink));

// --- GFM feature parity ------------------------------------------------------
check('bold -> <strong>', md.render('**bold**').includes('<strong>bold</strong>'));
check('italic -> <em>', md.render('_it_').includes('<em>it</em>'));
check('strikethrough -> <s>', /<s>gone<\/s>/.test(md.render('~~gone~~')));
check('heading -> <h2>', md.render('## Head').includes('<h2>Head</h2>'));
check('blockquote -> <blockquote>', md.render('> quote').includes('<blockquote>'));
check('unordered list -> <ul><li>', /<ul>\s*<li>one<\/li>/.test(md.render('- one')));
check('ordered list -> <ol>', md.render('1. one').includes('<ol>'));

const table = md.render('| a | b |\n|---|---|\n| 1 | 2 |');
check('GFM table -> <table>', table.includes('<table>') && table.includes('<td>1</td>'));

const autolink = md.render('see https://example.com/x now');
check('linkify bare URL -> <a href>', autolink.includes('<a href="https://example.com/x"'));

const fenced = md.render('```js\nconst x = 1;\n```');
check('fenced code -> <pre class="hljs">', fenced.includes('<pre class="hljs">'));
check('fenced code with lang is syntax-highlighted', fenced.includes('hljs-keyword') || fenced.includes('class="hljs-'));

const fencedNoLang = md.render('```\nplain <b>\n```');
check('fenced code without lang escapes contents', fencedNoLang.includes('&lt;b&gt;') && !/<b>/.test(fencedNoLang));

const taskDone = md.render('- [x] done');
check('task list checked -> checkbox checked', /type="checkbox"/.test(taskDone) && /checked/.test(taskDone));
const taskTodo = md.render('- [ ] todo');
check('task list unchecked -> checkbox', /type="checkbox"/.test(taskTodo) && /disabled/.test(taskTodo) && !/checked/.test(taskTodo));

console.log('OK ' + count + ' checks');
