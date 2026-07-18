// A project's own page (project.html?id=<project URI>).
//
// Three ways to change a project, in increasing weight:
//  1. Quick command (the bar at the bottom, typed or dictated): the request is
//     handed to Ara as a conversation of kind "edit" — linked to this project,
//     hidden from the normal conversation list — and Ara applies it to the
//     project's source file. The page shows her one-line confirmation and
//     reloads the content.
//  2. Direct editing: the pencil switches the page to a raw-Markdown editor
//     over the whole file (frontmatter included). Saving posts the new content
//     with the sha of what was loaded, so a concurrent change by an agent or
//     another device surfaces as a conflict instead of being clobbered.
//  3. A real conversation: "Discuss with Ara" opens the dashboard composer
//     pre-linked to this project (a normal, visible thread).
//
// Data comes from the gateway:
//   GET  /projects/item?id=…   -> {id, title, path, markdown, sha256}
//   POST /projects/item        -> save {id, content, base_sha}
//   POST /conversations        -> quick edit command ({kind:"edit", project})
//   GET  /conversations?project=…&kind=all — the project's recent threads

import { esc } from './base.js';
import { renderMarkdown, renderInline, MD_CSS } from './markdown.js';

const APPLY_POLL_MS = 3000;
// Frontmatter keys that are rendered elsewhere (or meaningless to the user)
// and therefore left out of the meta chips.
const HIDDEN_FM_KEYS = new Set(['id', 'title']);

// Parse the leading --- fenced frontmatter block the way the chambers'
// md2ttl converter does: scalar `key: value` lines plus simple `- item` lists.
// Returns {fields: Map, body: string}.
function splitFrontmatter(markdown) {
  const m = /^---\n([\s\S]*?)\n---\s*\n?/.exec(markdown || '');
  if (!m) return { fields: new Map(), body: markdown || '' };
  const fields = new Map();
  let currentList = null;
  for (const raw of m[1].split('\n')) {
    const item = /^\s*-\s+(.*)$/.exec(raw);
    if (item && currentList !== null) {
      fields.get(currentList).push(stripQuotes(item[1].trim()));
      continue;
    }
    const kv = /^([A-Za-z0-9_]+):\s*(.*)$/.exec(raw);
    if (!kv) continue;
    const value = kv[2].trim();
    if (value === '') {
      fields.set(kv[1], []);
      currentList = kv[1];
    } else {
      fields.set(kv[1], stripQuotes(value));
      currentList = null;
    }
  }
  return { fields, body: (markdown || '').slice(m[0].length) };
}

function stripQuotes(v) {
  return (v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === '"' || v[0] === "'"))
    ? v.slice(1, -1) : v;
}

// 'actor:jane-doe' / 'urn:retinue:actor:x' -> 'Jane Doe'
function humanizeRef(v) {
  const tail = String(v).split(':').pop() || '';
  if (!/^[a-z0-9_-]+$/i.test(tail)) return v;
  return tail.split(/[-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

class RetinueProjectPage extends HTMLElement {
  constructor() {
    super();
    this._id = new URLSearchParams(location.search).get('id') || '';
    this._item = null;       // {id, title, path, markdown, sha256}
    this._state = 'loading'; // loading | ok | missing | offline
    this._mode = 'view';     // view | edit
    this._draft = '';        // editor content while in edit mode
    this._saving = false;
    this._editError = '';    // save failure / conflict message
    this._cmd = '';          // quick-command draft
    this._sending = false;
    this._apply = null;      // {cid, status: 'working'|'done'|'failed', reply}
    this._applyTimer = null;
    this._recState = 'idle'; // idle | recording | transcribing
    this._recChunks = [];
    this._mediaRecorder = null;
    this._recStream = null;
    this._cmdError = '';
    this._autosend = false;  // send a dictated command right away (shared flag)
    try { this._autosend = localStorage.getItem('retinue-voice-autosend') === '1'; } catch (_e) { /* ignore */ }
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    this.render();
    this.load();
    // Content may be changed elsewhere (Ara, another device) while the page
    // sits in a background tab — refresh the view (never an open editor) when
    // the user comes back to it.
    this._onVisible = () => {
      if (!document.hidden && this._mode === 'view') this.load();
    };
    document.addEventListener('visibilitychange', this._onVisible);
  }

  disconnectedCallback() {
    document.removeEventListener('visibilitychange', this._onVisible);
    if (this._applyTimer) clearTimeout(this._applyTimer);
    this._stopRecording();
    this._stopStream();
  }

  async load() {
    if (!this._id) { this._state = 'missing'; this.render(); return; }
    try {
      const res = await fetch(`/projects/item?id=${encodeURIComponent(this._id)}`,
        { cache: 'no-store' });
      if (res.status === 404) { this._state = 'missing'; this.render(); return; }
      if (!res.ok) throw new Error(String(res.status));
      this._item = await res.json();
      this._state = 'ok';
    } catch (_err) {
      // Keep showing the last loaded state if we have one; otherwise offline.
      if (!this._item) this._state = 'offline';
    }
    this.render();
  }

  // ── Direct editing ─────────────────────────────────────────────────────────

  _startEdit() {
    if (!this._item) return;
    this._mode = 'edit';
    this._draft = this._item.markdown;
    this._editError = '';
    this.render();
  }

  _cancelEdit() {
    this._mode = 'view';
    this._editError = '';
    this.render();
    this.load(); // pick up anything that changed while the editor was open
  }

  async _save() {
    if (this._saving || !this._item) return;
    this._saving = true;
    this._editError = '';
    this.render();
    try {
      const res = await fetch('/projects/item', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: this._item.id, content: this._draft, base_sha: this._item.sha256,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.status === 409) {
        // Adopt the server's sha so a deliberate second Save overwrites.
        this._item.sha256 = body.sha256 || this._item.sha256;
        this._editError = 'This project changed in the background while you were '
          + 'editing. Save again to overwrite that change, or Cancel to see it.';
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      this._item.markdown = this._draft;
      this._item.sha256 = body.sha256 || '';
      this._mode = 'view';
    } catch (_err) {
      this._editError = "Couldn't save — check the connection and try again.";
    } finally {
      this._saving = false;
      this.render();
    }
  }

  // ── Quick commands via Ara (typed or dictated) ─────────────────────────────

  async _sendCommand(text) {
    const cmd = (text || '').trim();
    if (!cmd || this._sending || !this._item) return;
    this._sending = true;
    this._cmdError = '';
    this.render();
    try {
      const res = await fetch('/conversations', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: cmd,
          kind: 'edit',
          project: this._item.id,
          project_title: this._item.title,
          title: `Edit: ${this._item.title}`,
        }),
      });
      if (!res.ok) throw new Error(String(res.status));
      const conv = await res.json();
      this._cmd = '';
      this._apply = { cid: conv.id, status: 'working', reply: '' };
      this._pollApply();
    } catch (_err) {
      this._cmdError = "Couldn't reach Ara — check the connection and try again.";
    } finally {
      this._sending = false;
      this.render();
    }
  }

  async _pollApply() {
    if (this._applyTimer) clearTimeout(this._applyTimer);
    const apply = this._apply;
    if (!apply || apply.status !== 'working') return;
    try {
      const res = await fetch(`/conversations/${apply.cid}`, { cache: 'no-store' });
      if (res.ok) {
        const conv = await res.json();
        const msgs = conv.messages || [];
        const last = msgs[msgs.length - 1] || {};
        if (!conv.pending && last.role === 'assistant') {
          apply.status = 'done';
          apply.reply = last.text || 'Done.';
          this.render();
          this.load(); // the file most likely changed
          return;
        }
      }
    } catch (_err) { /* offline blip — keep polling */ }
    this._applyTimer = setTimeout(() => this._pollApply(), APPLY_POLL_MS);
  }

  _dismissApply() {
    if (this._applyTimer) clearTimeout(this._applyTimer);
    this._apply = null;
    this.render();
  }

  // ── Voice input for the command bar ────────────────────────────────────────

  async _toggleRecord() {
    if (this._recState === 'transcribing') return;
    if (this._recState === 'recording') { this._stopRecording(); return; }
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder)) {
      this._cmdError = 'Voice recording is not supported on this device.';
      this.render();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this._recStream = stream;
      this._recChunks = [];
      const mr = new MediaRecorder(stream);
      this._mediaRecorder = mr;
      mr.addEventListener('dataavailable', (e) => {
        if (e.data && e.data.size) this._recChunks.push(e.data);
      });
      mr.addEventListener('stop', () => this._onRecordingStopped());
      mr.start();
      this._recState = 'recording';
      this._cmdError = '';
      this.render();
    } catch (_err) {
      this._recState = 'idle';
      this._cmdError = 'Microphone access was denied.';
      this._stopStream();
      this.render();
    }
  }

  _stopRecording() {
    try {
      if (this._mediaRecorder && this._mediaRecorder.state !== 'inactive') {
        this._mediaRecorder.stop();
      }
    } catch (_e) { /* ignore */ }
  }

  _stopStream() {
    if (this._recStream) {
      try { this._recStream.getTracks().forEach((tr) => tr.stop()); } catch (_e) { /* ignore */ }
      this._recStream = null;
    }
  }

  async _onRecordingStopped() {
    this._stopStream();
    const chunks = this._recChunks || [];
    this._recChunks = [];
    const type = (this._mediaRecorder && this._mediaRecorder.mimeType)
      || (chunks[0] && chunks[0].type) || 'audio/webm';
    this._mediaRecorder = null;
    if (!chunks.length) { this._recState = 'idle'; this.render(); return; }
    this._recState = 'transcribing';
    this.render();
    let toSend = '';
    try {
      const res = await fetch('/conversations/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': type || 'application/octet-stream' },
        body: new Blob(chunks, { type }),
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const text = ((data && data.text) || '').trim();
      if (text) {
        this._cmd = this._cmd ? `${this._cmd.replace(/\s*$/, '')} ${text}` : text;
        // Same review-or-send-right-away setting as the conversation composer.
        if (this._autosend) toSend = this._cmd;
      } else {
        this._cmdError = 'No speech was detected in the recording.';
      }
    } catch (_err) {
      this._cmdError = "Couldn't transcribe the recording. Please try again.";
    } finally {
      this._recState = 'idle';
      this.render();
    }
    if (toSend) await this._sendCommand(toSend);
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  render() {
    let body;
    if (this._state === 'loading') {
      body = `<div class="bar">${this._backHtml()}<span class="bar-title muted">&#8230;</span></div>`;
    } else if (this._state === 'missing') {
      body = `<div class="bar">${this._backHtml()}<span class="bar-title">Project</span></div>`
        + '<p class="muted">This project could not be found. It may have been renamed or finished.</p>';
    } else if (this._state === 'offline') {
      body = `<div class="bar">${this._backHtml()}<span class="bar-title">Project</span></div>`
        + '<p class="muted">Offline &ndash; no current data.</p>';
    } else if (this._mode === 'edit') {
      body = this._editorHtml();
    } else {
      body = this._viewHtml();
    }
    this.shadowRoot.innerHTML = `<style>${CSS}${MD_CSS}</style>`
      + `<section class="card">${body}</section>`;
    this._wire();
  }

  _backHtml() {
    return '<a class="back" href="/projects.html" aria-label="All projects">&#8249;</a>';
  }

  _micHtml(cls = 'mic') {
    const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
    if (!canRecord) return '';
    const label = this._recState === 'recording' ? '⏹'
      : (this._recState === 'transcribing' ? '…' : '\u{1F3A4}');
    const title = this._recState === 'recording' ? 'Stop recording'
      : (this._recState === 'transcribing' ? 'Transcribing …' : 'Dictate a change');
    const disabled = (this._sending || this._recState === 'transcribing') ? 'disabled' : '';
    return `<button type="button" class="${cls}${this._recState === 'recording' ? ' rec' : ''}" `
      + `data-mic title="${title}" aria-label="${title}" ${disabled}>${label}</button>`;
  }

  _viewHtml() {
    const it = this._item;
    const { fields, body } = splitFrontmatter(it.markdown);
    const title = fields.get('title') || it.title || 'Project';
    // Editing should feel nearby but never shout: one muted pencil in the bar.
    const bar = `<div class="bar">${this._backHtml()}`
      + `<span class="bar-title">${esc(title)}</span>`
      + `<button class="iconbtn" data-edit title="Edit this page" aria-label="Edit this page">&#9998;</button>`
      + `</div>`;
    return bar
      + this._metaHtml(fields)
      + `<div class="body">${body.trim() ? renderMarkdown(body) : '<p class="muted">No notes yet.</p>'}</div>`
      + `<div class="foot">`
      + `<a class="discuss" href="/conversations.html#new?project=${encodeURIComponent(it.id)}&title=${encodeURIComponent(title)}">`
      + `&#x1F4AC; Discuss with Ara</a>`
      + `<span class="path" title="${esc(it.path)}">${esc(it.path)}</span>`
      + `</div>`
      + this._applyHtml()
      + this._commandBarHtml();
  }

  _metaHtml(fields) {
    const chips = [];
    for (const [key, value] of fields) {
      if (HIDDEN_FM_KEYS.has(key)) continue;
      const label = key.replace(/_/g, ' ');
      if (Array.isArray(value)) {
        if (!value.length) continue;
        const items = value.map((v) => renderInline(v)).join(', ');
        chips.push(`<span class="chip"><b>${esc(label)}</b> ${items}</span>`);
        continue;
      }
      const shown = /^(current_actor|actor|waiting_on)$/.test(key)
        ? humanizeRef(value) : value;
      chips.push(`<span class="chip"><b>${esc(label)}</b> ${esc(shown)}</span>`);
    }
    return chips.length ? `<div class="meta">${chips.join('')}</div>` : '';
  }

  _applyHtml() {
    if (this._apply) {
      const a = this._apply;
      if (a.status === 'working') {
        return `<div class="apply working"><span class="spin"></span>`
          + `<span>Ara is applying your change &#8230;</span></div>`;
      }
      return `<div class="apply done"><div class="apply-reply">${renderMarkdown(a.reply)}</div>`
        + `<div class="apply-actions">`
        + `<a href="/conversations.html#conversation-${esc(a.cid)}">Open thread</a>`
        + `<button type="button" data-dismiss aria-label="Dismiss">&times;</button>`
        + `</div></div>`;
    }
    return '';
  }

  _commandBarHtml() {
    const disabled = this._sending ? 'disabled' : '';
    const err = this._cmdError ? `<div class="cmd-err">${esc(this._cmdError)}</div>` : '';
    return `<div class="cmdbar">${err}<form class="row" data-cmd-form>`
      + this._micHtml()
      + `<input type="text" placeholder="Tell Ara what to change &#8230;" `
      + `aria-label="Tell Ara what to change" autocomplete="off" ${disabled} value="${esc(this._cmd)}">`
      + `<button type="submit" title="Send" aria-label="Send" ${disabled}>&#10148;</button>`
      + `</form></div>`;
  }

  _editorHtml() {
    const saving = this._saving ? 'disabled' : '';
    const err = this._editError ? `<div class="cmd-err">${esc(this._editError)}</div>` : '';
    return `<div class="bar">`
      + `<button class="iconbtn" data-cancel title="Cancel" aria-label="Cancel editing">&times;</button>`
      + `<span class="bar-title">Editing</span>`
      + `<button class="save" data-save ${saving}>${this._saving ? 'Saving …' : 'Save'}</button>`
      + `</div>` + err
      + `<textarea class="editor" spellcheck="false" ${saving}>${esc(this._draft)}</textarea>`;
  }

  _wire() {
    const root = this.shadowRoot;
    const on = (sel, fn) => { const el = root.querySelector(sel); if (el) el.addEventListener('click', fn); };
    on('[data-edit]', () => this._startEdit());
    on('[data-cancel]', () => this._cancelEdit());
    on('[data-save]', () => this._save());
    on('[data-dismiss]', () => this._dismissApply());
    on('[data-mic]', () => this._toggleRecord());
    const editor = root.querySelector('.editor');
    if (editor) {
      editor.addEventListener('input', () => { this._draft = editor.value; });
    }
    const form = root.querySelector('[data-cmd-form]');
    if (form) {
      const input = form.querySelector('input');
      input.addEventListener('input', () => { this._cmd = input.value; });
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        this._sendCommand(input.value);
      });
    }
  }
}

const CSS = `
  :host { display: flex; flex-direction: column; min-height: 0; flex: 1; }
  * { box-sizing: border-box; }
  button, input, textarea { font: inherit; }
  button:focus-visible, a:focus-visible, input:focus-visible, textarea:focus-visible {
    outline: 2px solid var(--accent, #6ea8fe); outline-offset: 1px; }

  /* Chrome-less on phones, a framed card on wide screens — like the other
     dashboard regions. */
  .card { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 2px; }
  @media (min-width: 700px) {
    .card { background: var(--card, #151922); border: 1px solid var(--line, rgba(231, 235, 242, .08));
            border-radius: var(--radius, 16px); padding: 14px 16px; }
  }
  .muted { color: var(--muted, #8b93a3); margin: 4px 0; }

  .bar { flex: none; display: flex; align-items: center; gap: 10px; padding: 2px 0 10px;
         border-bottom: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .back { flex: none; width: 34px; height: 34px; border-radius: 50%;
          background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); text-decoration: none;
          font-size: 1.35rem; line-height: 1; display: inline-flex; align-items: center;
          justify-content: center; padding: 0 2px 2px 0; -webkit-tap-highlight-color: transparent; }
  .bar-title { flex: 1; min-width: 0; font-weight: 650; font-size: 1.05rem;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .iconbtn { flex: none; width: 34px; height: 34px; border-radius: 50%; background: transparent;
             border: 1px solid var(--line, rgba(231, 235, 242, .08)); color: var(--muted, #8b93a3);
             cursor: pointer; font-size: .95rem; display: inline-flex; align-items: center;
             justify-content: center; padding: 0; -webkit-tap-highlight-color: transparent; }
  .iconbtn:hover { border-color: var(--accent, #6ea8fe); color: var(--accent, #6ea8fe); }

  .meta { flex: none; display: flex; flex-wrap: wrap; gap: 6px; padding: 12px 0 2px; }
  .chip { background: var(--card-2, #1c2230); border-radius: 999px; padding: 4px 11px;
          font-size: .78rem; color: var(--fg2, #c3cad6); }
  .chip b { font-weight: 600; color: var(--muted, #8b93a3); text-transform: capitalize;
            margin-right: 4px; }
  .chip a { color: var(--accent, #6ea8fe); }

  .body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
          padding: 10px 2px 14px; }

  .foot { flex: none; display: flex; align-items: center; justify-content: space-between;
          gap: 10px; padding: 8px 0; border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .discuss { color: var(--accent, #6ea8fe); text-decoration: none; font-weight: 600;
             font-size: .9rem; }
  .discuss:hover { text-decoration: underline; }
  .path { color: var(--muted, #8b93a3); font-size: .68rem; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; }

  .apply { flex: none; display: flex; align-items: center; gap: 10px; margin: 8px 0 0;
           padding: 9px 12px; border-radius: 12px; background: var(--card-2, #1c2230);
           border: 1px solid var(--accent, #6ea8fe); font-size: .88rem; }
  .apply.working { color: var(--muted, #8b93a3); font-style: italic; }
  .apply.done { align-items: flex-start; }
  .apply-reply { flex: 1; min-width: 0; }
  .apply-actions { flex: none; display: flex; align-items: center; gap: 8px; }
  .apply-actions a { color: var(--accent, #6ea8fe); font-size: .78rem; }
  .apply-actions button { background: none; border: 0; color: var(--muted, #8b93a3);
                          font-size: 1.1rem; line-height: 1; cursor: pointer; padding: 0 2px; }
  .spin { flex: none; width: 14px; height: 14px; border-radius: 50%;
          border: 2px solid var(--line, rgba(231, 235, 242, .2));
          border-top-color: var(--accent, #6ea8fe); animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .cmdbar { flex: none; margin-top: 8px; padding-top: 10px;
            border-top: 1px solid var(--line, rgba(231, 235, 242, .08)); }
  .cmd-err { color: var(--high, #ff6b6b); font-size: .76rem; margin-bottom: 8px; }
  .row { display: flex; gap: 6px; align-items: center; }
  .row input { flex: 1; min-width: 0; height: 40px; background: var(--card-2, #1c2230);
               border: 0; border-radius: 20px; padding: 0 14px; color: var(--fg, #e7ebf2); }
  .row input::placeholder { color: var(--muted, #8b93a3); }
  .row button[type="submit"] { flex: none; display: inline-flex; align-items: center;
      justify-content: center; width: 40px; height: 40px; border-radius: 50%;
      background: var(--accent, #6ea8fe); color: #0b0d12; border: 0; font-size: 1.05rem;
      cursor: pointer; padding: 0 0 0 2px; -webkit-tap-highlight-color: transparent; }
  .mic { display: inline-flex; align-items: center; justify-content: center; height: 40px;
         width: 40px; flex: none; border-radius: 50%; background: var(--card-2, #1c2230);
         border: 0; cursor: pointer; color: var(--fg, #e7ebf2); font-size: 1.05rem;
         -webkit-tap-highlight-color: transparent; }
  .mic.rec { background: var(--high, #ff6b6b); color: #fff;
             animation: mic-pulse 1.2s ease-in-out infinite; }
  .mic[disabled] { opacity: .6; cursor: default; }
  @keyframes mic-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .55; } }
  button[disabled], input[disabled], textarea[disabled] { opacity: .6; cursor: default; }

  .save { flex: none; background: var(--accent, #6ea8fe); color: #0b0d12; border: 0;
          border-radius: 999px; padding: 7px 18px; font-weight: 650; cursor: pointer; }
  .editor { flex: 1; min-height: 55vh; width: 100%; margin-top: 10px; resize: none;
            background: var(--card-2, #1c2230); color: var(--fg, #e7ebf2); border: 0;
            border-radius: 12px; padding: 12px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: .86rem; line-height: 1.5; }
`;

customElements.define('retinue-project-page', RetinueProjectPage);
