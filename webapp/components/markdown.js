// Shared Markdown renderer for the dashboard.
//
// One renderer, used everywhere the dashboard shows Markdown — conversation
// bubbles and project pages — so the same text always renders the same way.
// It is a small, dependency-free subset of Markdown chosen for what Ara and
// the chambers actually produce:
//
//   blocks: paragraphs, headings (#..######), fenced code, blockquotes,
//           nested -/*/1. lists (with [ ]/[x] task items), pipe tables,
//           horizontal rules
//   inline: [label](url), bare http(s)/www URLs, **bold**, *italic*,
//           ~~strikethrough~~, `code`
//
// Safety: the input is HTML-escaped before any markup is generated, and only
// http(s)/mailto/tel URLs ever become links (never javascript:). The output is
// therefore safe to assign to innerHTML.
//
// Rendering hooks let a host specialize without forking the renderer: the
// conversations card passes a `quote` hook that adds its copy-to-clipboard
// button to blockquotes.

import { esc } from './base.js';

// ── Inline pass ───────────────────────────────────────────────────────────────

const SEP = String.fromCharCode(1); // sentinel absent from escaped text

function anchor(url, label) {
  const external = !/^mailto:|^tel:/i.test(url);
  const attrs = external ? ' target="_blank" rel="noopener noreferrer"' : '';
  return `<a href="${url}"${attrs}>${label}</a>`;
}

// Render inline Markdown on one text segment. The text is escaped first, so
// the result is safe. Bare domains without a scheme are deliberately NOT
// auto-linked (too easily confused with filenames) — write them as explicit
// Markdown links.
export function renderInline(text) {
  let s = esc(text);
  const stashed = [];
  // Stash generated <a> HTML behind a placeholder so the later bold/italic
  // passes never mangle a URL, and restore them at the end.
  const stash = (html) => `${SEP}${stashed.push(html) - 1}${SEP}`;
  // ![alt](url) — no remote fetches from rendered content; show it as a link.
  s = s.replace(/!\[([^\]]*)\]\(((?:https?:\/\/)[^\s)]+)\)/gi,
    (_m, alt, url) => stash(anchor(url, alt || url)));
  // [label](url)
  s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:|tel:)[^\s)]+)\)/gi,
    (_m, label, url) => stash(anchor(url, label)));
  // Bare URLs; keep any trailing sentence punctuation outside the link.
  s = s.replace(/\bhttps?:\/\/[^\s<]+/gi, (m) => {
    const t = m.match(/[.,;:!?)\]]+$/);
    const tail = t ? t[0] : '';
    const url = tail ? m.slice(0, -tail.length) : m;
    return stash(anchor(url, url)) + tail;
  });
  s = s.replace(/\bwww\.[^\s<]+/gi, (m) => {
    const t = m.match(/[.,;:!?)\]]+$/);
    const tail = t ? t[0] : '';
    const host = tail ? m.slice(0, -tail.length) : m;
    return stash(anchor('https://' + host, host)) + tail;
  });
  // `code` is stashed too: bold/italic/strike must not fire inside it.
  s = s.replace(/`([^`]+)`/g, (_m, c) => stash(`<code>${c}</code>`));
  s = s.replace(/\*\*([^*]+?)\*\*/g, (_m, c) => `<strong>${c}</strong>`);
  s = s.replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\w)/g, (_m, pre, c) => `${pre}<em>${c}</em>`);
  s = s.replace(/~~([^~\n]+?)~~/g, (_m, c) => `<del>${c}</del>`);
  return s.replace(new RegExp(SEP + '(\\d+)' + SEP, 'g'), (_m, i) => stashed[Number(i)]);
}

// ── Block pass ────────────────────────────────────────────────────────────────

const FENCE_RE = /^(?:```|~~~)\s*([\w.+-]*)\s*$/;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const HR_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE_RE = /^>\s?/;
const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TASK_RE = /^\[([ xX])\]\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/;

function renderParagraph(lines) {
  return `<p>${lines.map(renderInline).join('<br>')}</p>`;
}

function splitTableRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  // Split on unescaped pipes; \| stays a literal pipe inside a cell.
  return s.split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, '|').trim());
}

function renderTable(headerLine, rowLines) {
  const head = splitTableRow(headerLine);
  const rows = rowLines.map(splitTableRow);
  const th = head.map((c) => `<th>${renderInline(c)}</th>`).join('');
  const trs = rows.map((r) =>
    `<tr>${head.map((_h, i) => `<td>${renderInline(r[i] || '')}</td>`).join('')}</tr>`
  ).join('');
  // The wrapper scrolls horizontally so a wide table never widens the page.
  return `<div class="md-tablewrap"><table><thead><tr>${th}</tr></thead>` +
    `<tbody>${trs}</tbody></table></div>`;
}

function renderListItems(items) {
  // items: [{indent, marker, text}]. Nesting is by indent: a deeper indent
  // opens a sublist inside the previous (still-open) item.
  let html = '';
  const stack = []; // open list tags: 'ul' | 'ol'
  let prevIndent = 0;
  const open = (ordered) => { const tag = ordered ? 'ol' : 'ul'; stack.push(tag); html += `<${tag}>`; };
  for (const it of items) {
    const ordered = /^\d/.test(it.marker);
    if (!stack.length) {
      open(ordered);
    } else if (it.indent > prevIndent) {
      open(ordered); // nest inside the still-open <li>
    } else {
      html += '</li>';
      while (stack.length > 1 && it.indent < prevIndent) {
        html += `</${stack.pop()}></li>`; // close the sublist, then its parent item
        prevIndent -= 2;
      }
    }
    let body = it.text;
    const task = TASK_RE.exec(body);
    if (task) {
      const done = task[1] !== ' ';
      body = `<span class="md-task${done ? ' done' : ''}">` +
        `<input type="checkbox" disabled${done ? ' checked' : ''}></span>` +
        renderInline(task[2]);
    } else {
      body = renderInline(body);
    }
    html += `<li>${body}`;
    prevIndent = it.indent;
  }
  html += '</li>';
  while (stack.length) {
    html += `</${stack.pop()}>`;
    if (stack.length) html += '</li>'; // the parent item the sublist lived in
  }
  return html;
}

/**
 * Render a Markdown string to safe HTML wrapped in <div class="md">.
 *
 * opts.quote: optional (rawText, innerHtml) => html hook for blockquotes —
 * rawText is the un-prefixed quoted text (for copy-to-clipboard), innerHtml
 * its rendered inline content.
 */
export function renderMarkdown(text, opts = {}) {
  const quoteHook = opts.quote
    || ((_raw, inner) => `<blockquote class="md-quote">${inner}</blockquote>`);
  const lines = String(text == null ? '' : text).split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    const fence = FENCE_RE.exec(line);
    if (fence) {
      const code = [];
      i++;
      while (i < lines.length && !FENCE_RE.test(lines[i])) { code.push(lines[i]); i++; }
      i++; // skip the closing fence (or run off the end of an unclosed block)
      const cls = fence[1] ? ` class="lang-${esc(fence[1])}"` : '';
      out.push(`<pre class="md-pre"><code${cls}>${esc(code.join('\n'))}</code></pre>`);
      continue;
    }

    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2].trim())}</h${level}>`);
      i++;
      continue;
    }

    if (QUOTE_RE.test(line)) {
      const quoted = [];
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        quoted.push(lines[i].replace(QUOTE_RE, ''));
        i++;
      }
      const raw = quoted.join('\n');
      out.push(quoteHook(raw, quoted.map(renderInline).join('<br>')));
      continue;
    }

    const list = LIST_RE.exec(line);
    if (list) {
      const items = [];
      while (i < lines.length) {
        const m = LIST_RE.exec(lines[i]);
        if (!m) break;
        items.push({ indent: m[1].length, marker: m[2], text: m[3] });
        i++;
      }
      out.push(renderListItems(items));
      continue;
    }

    // HR is checked after lists so "- - -"-ish list items are not eaten, and
    // after the heading/quote checks which can't collide with it.
    if (HR_RE.test(line)) { out.push('<hr>'); i++; continue; }

    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      const header = line;
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(lines[i]);
        i++;
      }
      out.push(renderTable(header, rows));
      continue;
    }

    // Paragraph: consecutive plain lines, single newlines become <br>.
    const plain = [];
    while (i < lines.length && lines[i].trim()
        && !FENCE_RE.test(lines[i]) && !HEADING_RE.test(lines[i])
        && !QUOTE_RE.test(lines[i]) && !LIST_RE.exec(lines[i])
        && !HR_RE.test(lines[i])
        && !(lines[i].includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1]))) {
      plain.push(lines[i]);
      i++;
    }
    if (plain.length) out.push(renderParagraph(plain));
  }
  return `<div class="md">${out.join('')}</div>`;
}

// Styles for rendered Markdown, shared by every shadow root that shows it.
// Scoped under .md; colors fall back to the dashboard palette variables.
export const MD_CSS = `
  .md { line-height: 1.45; overflow-wrap: anywhere; }
  .md > :first-child { margin-top: 0; }
  .md > :last-child { margin-bottom: 0; }
  .md p { margin: 0 0 8px; }
  .md h1, .md h2, .md h3, .md h4, .md h5, .md h6 {
    margin: 14px 0 6px; line-height: 1.25; font-weight: 650; color: var(--fg, #e7ebf2);
    /* Hosts style bare h2 for their card headers (uppercase, muted); rendered
       content must not inherit that look. */
    text-transform: none; letter-spacing: normal; }
  .md h1 { font-size: 1.15rem; }
  .md h2 { font-size: 1.05rem; }
  .md h3 { font-size: .98rem; }
  .md h4, .md h5, .md h6 { font-size: .92rem; }
  .md a { color: var(--accent, #6ea8fe); text-decoration: underline; overflow-wrap: anywhere; }
  .md code { background: rgba(110, 168, 254, .15); border-radius: 4px; padding: 1px 4px;
             font-size: .88em; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .md .md-pre { background: rgba(0, 0, 0, .25); border: 1px solid var(--line, rgba(231, 235, 242, .08));
                border-radius: 10px; padding: 10px 12px; margin: 8px 0; overflow-x: auto; }
  .md .md-pre code { background: none; padding: 0; font-size: .84em; white-space: pre; }
  .md ul, .md ol { margin: 4px 0 8px; padding-left: 22px; }
  .md li { margin: 2px 0; }
  .md li > ul, .md li > ol { margin: 2px 0; }
  .md .md-task input { margin: 0 6px 0 0; vertical-align: -1px; accent-color: var(--accent, #6ea8fe); }
  .md .md-task { margin-left: -4px; }
  .md li:has(> .md-task.done) { color: var(--muted, #8b93a3); text-decoration: line-through; }
  .md li:has(> .md-task) { list-style: none; margin-left: -14px; }
  .md hr { border: 0; border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); margin: 12px 0; }
  .md .md-quote { margin: 6px 0 8px; padding: 6px 10px; border-left: 3px solid var(--accent, #6ea8fe);
                  background: rgba(110, 168, 254, .1); border-radius: 8px; }
  .md .md-tablewrap { overflow-x: auto; margin: 8px 0; }
  .md table { border-collapse: collapse; font-size: .88em; min-width: 60%; }
  .md th, .md td { border: 1px solid var(--line, rgba(231, 235, 242, .12)); padding: 5px 9px;
                   text-align: left; vertical-align: top; }
  .md th { background: rgba(110, 168, 254, .08); font-weight: 650; }
`;
