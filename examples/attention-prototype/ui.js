// The prototype's interface: the phone dashboard (interactive at all times) with
// its list, the chat and thread views and the details sheet; the simulation deck
// (clock, timeline, narration, state); and the wiring between them.
(function (root) {
  'use strict';
  const U = root.AttentionUtil;
  const { hhmm, whenText, dur, DAY, SCHEDULE, DIGEST_TIMES } = U;
  const SPHERE_COLORS = { customers: '#6ea8fe', admin: '#c9a0ff', health: '#ff6b6b', friends: '#57c785', family: '#ffb86b', system: '#9aa5b1' };
  const LEVEL_COLORS = { critical: '#ff5d5d', 'time-sensitive': '#e08a2e', active: '#4fb3b9', passive: '#4a5563' };
  const WHO = { narrator: 'story', you: 'you', system: 'system', push: 'push', learn: 'profile' };
  const LEAD_PRESETS = [[60, '1 h'], [120, '2 h'], [360, '6 h'], [1440, '1 day'], [2880, '2 days'], [4320, '3 days'], [10080, '1 week'], [20160, '2 weeks'], [40320, '4 weeks']];
  const SPEEDS = [[2, '×1 — the day in 12 min'], [4.8, '×2 — 5 min'], [12, '×5 — 2 min'], [24, '×10 — 1 min']];
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024; // in step with the gateway's cap
  const fmtSize = (n) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);
  const attachmentsHtml = (atts) => (atts && atts.length ? `<div class="attachments">${atts.map((a) => `<span class="att">📎 ${esc(a.name)}<small>${esc(fmtSize(a.size))}</small></span>`).join('')}</div>` : '');

  // Draft rewrites, rule-based: what a model turn does in a deployment.
  const CONTRACTIONS = [[/\bI’ll\b|\bI'll\b/g, 'I will'], [/\bI’m\b|\bI'm\b/g, 'I am'], [/\bdon’t\b|\bdon't\b/gi, 'do not'], [/\bcan’t\b|\bcan't\b/gi, 'cannot'], [/\bwon’t\b|\bwon't\b/gi, 'will not'], [/\bit’s\b|\bit's\b/gi, 'it is'], [/\bwe’re\b|\bwe're\b/gi, 'we are'], [/\bthat’s\b|\bthat's\b/gi, 'that is'], [/\bdoesn’t\b|\bdoesn't\b/gi, 'does not']];
  const EMOJI = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]\uFE0F?/gu;
  function rewrite(text, style, first) {
    let t = text.trim();
    if (style === 'Shorter') { const parts = t.split(/(?<=[.!?])\s+/); t = parts[0]; if (parts.length === 1 && t.includes(' — ')) t = t.split(' — ')[0]; if (!/[.!?]$/.test(t)) t += '.'; return t; }
    if (style === 'More formal') { for (const [re, to] of CONTRACTIONS) t = t.replace(re, to); t = t.replace(EMOJI, '').replace(/\s+/g, ' ').trim(); if (!/[.!?]$/.test(t)) t += '.'; if (!/^(dear|hello|good)/i.test(t)) t = `${first ? `Dear ${first}, ` : ''}${t.charAt(0).toLowerCase()}${t.slice(1)}`; return `${t} Kind regards.`; }
    if (style === 'Warmer') { t = t.replace(/^(dear|hello|hi)\s+\w+,?\s*/i, ''); t = t.replace(/\s*kind regards\.?$/i, ''); if (!/[.!?]$/.test(t)) t += '.'; return `${first ? `Hi ${first}! ` : 'Hi! '}${t} Looking forward to it 🙂`; }
    return t;
  }

  // Live waveform on the recording row's canvas, driven by a Web Audio analyser over
  // the mic stream — with a simulated wave where that is unavailable (after voice.js).
  class Waveform {
    constructor(hostFn) { this.hostFn = hostFn; this.ctx = null; this.analyser = null; this.raf = null; this.phase = 0; this.startMs = 0; }
    start(stream, startMs) {
      this.stop(); this.startMs = startMs || Date.now();
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC && stream) { try { this.ctx = new AC(); const src = this.ctx.createMediaStreamSource(stream); const an = this.ctx.createAnalyser(); an.fftSize = 128; an.smoothingTimeConstant = 0.7; src.connect(an); this.analyser = an; } catch (_e) { this.analyser = null; } }
      const tick = () => { this.draw(); this.raf = requestAnimationFrame(tick); }; tick();
    }
    stop() { if (this.raf) cancelAnimationFrame(this.raf); this.raf = null; this.analyser = null; if (this.ctx) { try { this.ctx.close(); } catch (_e) { /* ignore */ } this.ctx = null; } }
    draw() {
      const host = this.hostFn(); if (!host) return;
      const timeEl = host.querySelector('[data-rectime]');
      if (timeEl) { const s = Math.max(0, Math.floor((Date.now() - this.startMs) / 1000)); const txt = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; if (timeEl.textContent !== txt) timeEl.textContent = txt; }
      const canvas = host.querySelector('[data-wave]'); if (!canvas) return;
      const dpr = window.devicePixelRatio || 1; const w = canvas.clientWidth, h = canvas.clientHeight; if (!w || !h) return;
      if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
      if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
      const ctx = canvas.getContext('2d'); if (!ctx) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.fillStyle = '#6ea8fe';
      const bars = Math.max(12, Math.min(48, Math.floor(w / 7))); this.phase += 0.22;
      let levels;
      if (this.analyser) { const data = new Uint8Array(this.analyser.frequencyBinCount); this.analyser.getByteFrequencyData(data); const usable = Math.max(1, Math.floor(data.length * 0.75)); levels = Array.from({ length: bars }, (_, i) => data[Math.floor((i / bars) * usable)] / 255); }
      else levels = Array.from({ length: bars }, (_, i) => 0.35 + 0.25 * Math.sin(i * 0.7 + this.phase) + 0.18 * Math.sin(i * 1.3 - this.phase * 1.6) + 0.08 * Math.random());
      const bw = canvas.width / bars, mid = canvas.height / 2;
      for (let i = 0; i < bars; i++) { const level = Math.min(1, Math.max(0.06, levels[i])); const bh = Math.max(2 * dpr, level * canvas.height); const x = i * bw + bw * 0.2, bwid = bw * 0.6, r = Math.min(bwid / 2, 2 * dpr), y = mid - bh / 2; if (typeof ctx.roundRect === 'function') { ctx.beginPath(); ctx.roundRect(x, y, bwid, bh, r); ctx.fill(); } else ctx.fillRect(x, y, bwid, bh); }
    }
  }

  class App {
    constructor(rootEl) {
      this.root = rootEl; this.engine = new root.AttentionEngine();
      this.sim = new root.Simulation(this.engine, { onChange: () => this.renderAll() });
      this.sheet = null; this.view = null; this.draft = null; this.toast = null; this.menu = false; this.heldOpen = false; this.waitingOpen = false;
      this.files = {}; this.rec = null; this.job = null; this.attachError = ''; this.wave = new Waveform(() => this.phoneEl);
      this.sttUrl = null; this.cloudOk = false; try { this.cloudOk = localStorage.getItem('attention-voice-cloud') === '1'; } catch (_e) { /* no storage */ }
      this.localRecognition = null; // null = unknown, then true/false
      this.banner = null; this.bannerTimer = null; this.toastTimer = null; this.seenPushes = 0; this.feedLen = -1;
      this.phoneEl = rootEl.querySelector('#phone'); this.deckEl = rootEl.querySelector('#deck');
      this.buildDeck(); this.probeVoice();
      rootEl.addEventListener('click', (e) => this.onClick(e));
      rootEl.addEventListener('change', (e) => { if (e.target.matches('[data-file]')) { this.addFiles(e.target.dataset.id, e.target.files); return; } this.onInput(e); });
      rootEl.addEventListener('input', (e) => { if (e.target.id === 'composer') { const src = this.draft && this.draft.id === e.target.dataset.id && this.draft.tab === e.target.dataset.tab ? this.draft.source : 'typed'; this.draft = { id: e.target.dataset.id, tab: e.target.dataset.tab, text: e.target.value, source: src === 'typed' ? 'typed' : src }; } });
      rootEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.id === 'composer') { e.preventDefault(); this.send(e.target.dataset.id, e.target.dataset.tab); } });
      const params = new URLSearchParams(location.search);
      const at = params.get('at'); if (at) { const [h, m] = at.split(':').map(Number); this.sim.seek(h * 60 + (m || 0)); this.seenPushes = this.engine.pushes.length; }
      if (params.get('held') === '1') this.heldOpen = true;
      const open = params.get('open'); if (open && this.engine.byId.has(open)) { this.openView(open, params.get('tab')); const chip = params.get('chip'); if (chip) { this.converse(open, chip); const rw = params.get('rewrite'); if (rw) this.rewriteDraft(open, rw); if (params.get('tab')) this.view.tab = params.get('tab'); } if (params.get('details') === '1') this.sheet = open; }
      this.renderAll();
      if (params.get('play') === '1') this.sim.play();
      if (params.get('rec') === '1' && this.view) this.startRecording(this.view.id, this.view.tab);
      this.clockLoop();
    }

    // ---- deck skeleton ---------------------------------------------------------
    buildDeck() {
      this.deckEl.innerHTML = `
        <section class="card clock-card">
          <div class="clock-row">
            <svg class="clock" viewBox="0 0 100 100" role="img" aria-label="Clock">
              <circle cx="50" cy="50" r="46" class="clock-face"/>
              ${Array.from({ length: 12 }, (_, i) => { const a = (i / 12) * Math.PI * 2; const r1 = i % 3 === 0 ? 38 : 41, r2 = 45; return `<line x1="${50 + r1 * Math.sin(a)}" y1="${50 - r1 * Math.cos(a)}" x2="${50 + r2 * Math.sin(a)}" y2="${50 - r2 * Math.cos(a)}" class="tick${i % 3 === 0 ? ' major' : ''}"/>`; }).join('')}
              <line id="hand-h" x1="50" y1="50" x2="50" y2="24" class="hand hour"/>
              <line id="hand-m" x1="50" y1="50" x2="50" y2="12" class="hand minute"/>
              <circle cx="50" cy="50" r="2.4" class="pin"/>
            </svg>
            <div class="clock-text">
              <div class="digital" id="digital">00:00</div>
              <div class="date" id="date">Thu 3 Sept 2026</div>
              <div class="mode-line" id="mode-line"></div>
            </div>
          </div>
          <div class="controls">
            <button class="btn primary" data-act="toggle-play" id="btn-play">▶ Play the day</button>
            <button class="btn" data-act="step" title="Jump to the next beat of the story">⏭ Next beat</button>
            <button class="btn" data-act="restart" title="Back to midnight">↺ Restart</button>
            <select class="select" data-act="speed" aria-label="Speed">${SPEEDS.map(([v, l]) => `<option value="${v}"${v === this.sim.speed ? ' selected' : ''}>${l}</option>`).join('')}</select>
          </div>
          <div class="voice-line" id="voice-line"></div>
          <div class="driving" id="driving" hidden><span>You are driving — the story is paused and will adapt to what you changed.</span><button class="btn primary" data-act="resume">▶ Resume the story</button></div>
          <div class="ended" id="ended" hidden>The day is over. <button class="btn" data-act="restart">Play it again</button></div>
        </section>
        <section class="card timeline-card"><div id="timeline"></div><p class="hint">Click anywhere on the day to jump there. Bands: the mode in force; ▲ digest times; ticks: beats of the story.</p></section>
        <section class="card feed-card"><h3>The day</h3><div class="feed" id="feed"></div></section>
        <section class="card state-card" id="state"></section>`;
      this.feedEl = this.deckEl.querySelector('#feed');
    }

    // ---- rendering ---------------------------------------------------------------
    renderAll() { this.renderPhone(); this.renderFeed(); this.renderState(); this.renderTimeline(); this.renderClock(); this.renderControls(); this.noticePushes(); }
    clockLoop() { const step = () => { this.renderClock(); if (this.engine.version !== this.renderedVersion) this.renderAll(); requestAnimationFrame(step); }; requestAnimationFrame(step); }

    renderClock() {
      const now = this.engine.now; const h = (now / 60) % 12, m = now % 60;
      const hh = this.deckEl.querySelector('#hand-h'), mh = this.deckEl.querySelector('#hand-m');
      if (hh) hh.setAttribute('transform', `rotate(${h * 30} 50 50)`);
      if (mh) mh.setAttribute('transform', `rotate(${m * 6} 50 50)`);
      this.deckEl.querySelector('#digital').textContent = hhmm(now);
      this.deckEl.querySelector('#date').textContent = `${U.dateText(now)} 2026`;
      const mode = this.engine.mode();
      this.deckEl.querySelector('#mode-line').innerHTML = `<span class="dot" style="background:${mode.color}"></span>${esc(mode.name)}${this.engine.manualMode ? ' · set by hand' : ` · until ${hhmm(this.engine.scheduledUntil())}`}`;
      const mt = this.root.querySelector('#mini-time'); if (mt) mt.textContent = hhmm(now);
      const mm = this.root.querySelector('#mini-mode'); if (mm) mm.innerHTML = `<span class="dot" style="background:${mode.color}"></span>${esc(mode.name)}`;
      const head = this.deckEl.querySelector('#playhead'); if (head) head.setAttribute('transform', `translate(${20 + (now / DAY) * 680} 0)`);
      const headT = this.deckEl.querySelector('#playhead-t'); if (headT) headT.textContent = hhmm(now);
    }
    voiceRoute() { if (this.sttUrl) return ['stt', `your stt service at ${this.sttUrl}`]; if (this.localRecognition) return ['local', 'on-device, in the browser']; if (this.cloudOk && (window.SpeechRecognition || window.webkitSpeechRecognition)) return ['cloud', 'the browser’s cloud service (audio leaves the device)']; return [null, 'none — recordings are kept but not transcribed']; }
    async probeVoice() {
      const params = new URLSearchParams(location.search); const given = params.get('stt');
      if (given) this.sttUrl = given;
      else if (/^https?:$/.test(location.protocol)) { try { const r = await fetch('/transcribe', { method: 'HEAD' }); if (r.status !== 404) this.sttUrl = '/transcribe'; } catch (_e) { /* no endpoint */ } }
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      // The on-device availability check is skipped under automation (headless runs), where it can stall the renderer.
      if (SR && typeof SR.available === 'function' && !navigator.webdriver) { try { this.localRecognition = (await SR.available({ langs: [navigator.language || 'en'], processLocally: true })) === 'available'; } catch (_e) { this.localRecognition = false; } } else this.localRecognition = false;
      this.renderVoiceLine();
    }
    renderVoiceLine() {
      const el = this.deckEl.querySelector('#voice-line'); if (!el) return; const [route, label] = this.voiceRoute();
      el.innerHTML = `<span class="vl-label">Voice transcription:</span> <span class="vl-route ${route || 'none'}">${esc(label)}</span>` + (route === 'stt' || route === 'local' ? '' : `<label class="vl-opt"><input type="checkbox" data-act="cloud-ok"${this.cloudOk ? ' checked' : ''}> allow the browser’s cloud speech recognition</label>`) + `<span class="hint">Private routes first: the stt service behind <code>serve.ts</code>, then the browser’s on-device recognition; the cloud only if you allow it.</span>`;
    }
    renderControls() {
      const b = this.deckEl.querySelector('#btn-play'); b.textContent = this.sim.playing ? '⏸ Pause' : (this.engine.now === 0 ? '▶ Play the day' : '▶ Play');
      const mb = this.root.querySelector('#mini-play'); if (mb) mb.textContent = this.sim.playing ? '⏸ Pause' : (this.sim.viewerDriving ? '▶ Resume' : '▶ Play');
      this.deckEl.querySelector('#driving').hidden = !this.sim.viewerDriving;
      this.deckEl.querySelector('#ended').hidden = !this.sim.ended;
      this.renderedVersion = this.engine.version;
    }
    noticePushes() {
      const n = this.engine.pushes.length;
      if (n > this.seenPushes) { this.banner = this.engine.pushes[n - 1]; this.seenPushes = n; clearTimeout(this.bannerTimer); this.bannerTimer = setTimeout(() => { this.banner = null; this.renderPhone(); }, 7000); this.renderPhone(); }
      else if (n < this.seenPushes) { this.seenPushes = n; this.banner = null; }
    }
    showToast(text) { this.toast = text; clearTimeout(this.toastTimer); this.toastTimer = setTimeout(() => { this.toast = null; this.renderPhone(); }, 4500); }

    row(item, section) {
      const e = this.engine; const lvl = e.level(item);
      const meta = item.actor !== 'you' ? `waiting ${item.waiting_since != null ? dur(e.now - item.waiting_since) : ''}` : `${e.urgencyShort(item)} · ${item.kind === 'chat' ? item.channel : item.kind}`;
      const why = item.actor !== 'you' ? `importance ${item.importance} · parked on ${esc(item.actor)}` : `importance ${item.importance} · ${esc(e.urgencyShort(item))} · ${esc(section === 'now' ? e.admissionReason(item, e.mode()) : e.deliveryShort(item))}`;
      return `<button class="row" data-act="view" data-id="${esc(item.id)}" style="--stripe:${LEVEL_COLORS[lvl]}" title="${esc(lvl)}">
        <div class="row-top"><span class="chip"><i style="background:${SPHERE_COLORS[item.sphere] || '#9aa5b1'}"></i>${esc(item.sphere)}</span>${item.count > 1 ? `<span class="count">${item.count} msgs</span>` : ''}<span class="meta">${esc(meta)}</span></div>
        <div class="row-title">${esc(item.title)}</div>
        ${e.preview(item) ? `<div class="row-preview">${esc(e.preview(item))}</div>` : ''}
        <div class="row-why">${why}</div></button>`;
    }
    renderPhone() {
      const e = this.engine, s = e.sections(); const mode = s.mode;
      const until = e.manualMode ? 'by hand' : `until ${hhmm(e.scheduledUntil())}`;
      const nb = e.nextBreakpointText();
      const total = s.now.length + s.next.length + s.held.length + s.waiting.length;
      const banner = this.banner ? `<div class="banner ${this.banner.urgency}" data-act="dismiss-banner"><span class="bell">🔔</span><div><div class="b-title">${esc(this.banner.title)}<span class="b-urg">Urgency: ${esc(this.banner.urgency)}${this.banner.topic ? ' · Topic: ' + esc(this.banner.topic) : ''}</span></div><div class="b-body">${esc(this.banner.body)}</div></div></div>` : '';
      const toast = this.toast ? `<div class="toast">${esc(this.toast)}</div>` : '';
      const section = (label, items, key) => items.length ? `<section class="sec ${key}"><h4>${label} · ${items.length}</h4>${items.map((i) => this.row(i, key)).join('')}</section>` : '';
      const collapsible = (label, items, key, open) => items.length ? `<section class="sec ${key}"><button class="sec-toggle" data-act="toggle-${key}"><span>${label} · ${items.length}</span><span class="chev">${open ? '▾' : '▸'}</span></button>${open ? items.map((i) => this.row(i, key)).join('') : ''}</section>` : '';
      const list = `<div class="ph-head"><div class="ph-title">Attention</div><button class="mode-chip" data-act="mode-menu" style="border-color:${mode.color}"><span class="dot" style="background:${mode.color}"></span>${esc(mode.name)} · ${until} ▾</button></div>
        ${banner}
        <div class="ph-body">
          ${section('NOW', s.now, 'now')}
          ${section('NEXT', s.next, 'next')}
          ${collapsible(`HELD UNTIL ${nb.toUpperCase()}`, s.held, 'held', this.heldOpen)}
          ${collapsible('WAITING ON OTHERS', s.waiting, 'waiting', this.waitingOpen)}
          ${total ? '' : '<div class="empty">Nothing wants your attention.</div>'}
        </div>`;
      this.phoneEl.innerHTML = `<div class="phone"><div class="screen">
        ${this.view ? banner + this.viewHtml() : list}
        ${toast}
        ${this.menu ? this.modeMenu() : ''}
        ${this.sheet ? this.sheetHtml() : ''}
      </div></div>`;
      const bub = this.phoneEl.querySelector('#bubbles'); if (bub) bub.scrollTop = bub.scrollHeight;
    }
    openView(id, tab) { const item = this.engine.byId.get(id); if (!item) return; this.engine.ensureThread(id); this.view = { id, tab: item.kind === 'chat' ? (tab === 'chat' ? 'chat' : 'ara') : 'ara' }; this.sheet = null; this.menu = false; if (!this.draft || this.draft.id !== id) this.draft = null; }
    viewHtml() {
      const e = this.engine; const item = e.byId.get(this.view.id); if (!item) { this.view = null; return ''; }
      e.ensureThread(item.id);
      const isChat = item.kind === 'chat'; const tab = isChat ? this.view.tab : 'ara'; const lvl = e.level(item);
      const sub = isChat ? `${item.channel} · ${item.count > 1 ? `${item.count} messages` : item.sender}` : item.kind === 'thread' ? `Thread opened by ${item.agent}` : `Project · ${item.actor === 'you' ? 'your move' : `parked on ${item.actor}`}`;
      const status = item.state === 'done' ? `${item.doneHow === 'replied' ? 'replied' : 'done'} ${hhmm(item.doneAt)}` : item.actor !== 'you' ? `waiting on ${item.actor}` : `${lvl} · ${e.urgencyShort(item)}`;
      const bubbles = tab === 'chat'
        ? item.chat.map((m) => `<div class="bub ${m.dir}">${m.dir === 'in' && m.from ? `<div class="bub-from">${esc(m.from)}</div>` : ''}${m.text ? `<div class="bub-text">${esc(m.text)}</div>` : ''}${attachmentsHtml(m.attachments)}<div class="bub-t">${m.dir === 'in' ? esc(m.from || item.sender) : 'you'} · ${esc(whenText(m.t, e.now))}</div></div>`).join('')
        : item.thread.map((m) => `<div class="bub ${m.who === 'ara' ? 'in ara' : 'out'}">${m.text ? `<div class="bub-text">${esc(m.text)}</div>` : ''}${attachmentsHtml(m.attachments)}<div class="bub-t">${m.who === 'ara' ? 'Ara' : 'you'} · ${esc(whenText(m.t, e.now))}</div></div>`).join('');
      const chatDraft = isChat && this.draft && this.draft.id === item.id && this.draft.tab === 'chat' && this.draft.text ? this.draft : null;
      const draftCard = tab === 'ara' && chatDraft ? `<div class="draft-card"><div class="dc-label">Draft in the chat composer</div><div class="dc-text">“${esc(chatDraft.text)}”</div><div class="chips">${['Shorter', 'Warmer', 'More formal'].map((s) => `<button class="chipbtn" data-act="rewrite" data-id="${esc(item.id)}" data-style="${s}">${s}</button>`).join('')}<button class="chipbtn primary" data-act="send-now" data-id="${esc(item.id)}" data-tab="chat">Send now</button><button class="chipbtn" data-act="discard-draft" data-id="${esc(item.id)}" data-tab="chat">Discard</button></div></div>` : '';
      const chips = tab === 'ara' && item.chips && item.chips.length && item.state === 'open' ? `<div class="chips">${item.chips.map((c) => `<button class="chipbtn" data-act="chip" data-id="${esc(item.id)}" data-label="${esc(c)}">${esc(c)}</button>`).join('')}</div>` : '';
      const composer = this.composerHtml(item, tab);
      const foot = item.state === 'open' && item.actor === 'you' ? `<div class="v-foot"><button class="btn" data-act="later" data-when="next" data-id="${esc(item.id)}">Later · ${esc(e.nextBreakpointText())}</button><button class="btn" data-act="do" data-id="${esc(item.id)}">${isChat ? 'Mark handled' : 'Mark done'}</button></div>` : '';
      return `<div class="view">
        <div class="v-head"><button class="back" data-act="back" aria-label="Back">‹</button><div class="v-titles"><div class="v-title">${esc(item.title)}</div><div class="v-sub">${esc(sub)} · <span style="color:${LEVEL_COLORS[lvl]}">${esc(status)}</span></div></div><button class="info" data-act="open" data-id="${esc(item.id)}" title="Importance, urgency, delivery — and their corrections">ⓘ</button></div>
        ${isChat ? `<div class="segs"><button class="seg${tab === 'chat' ? ' on' : ''}" data-act="tab" data-tab="chat">Chat</button><button class="seg${tab === 'ara' ? ' on' : ''}" data-act="tab" data-tab="ara">Ara</button></div>` : ''}
        <div class="bubbles" id="bubbles">${bubbles || '<div class="empty">Nothing yet.</div>'}</div>
        ${draftCard}${chips}${composer}${foot}</div>`;
    }
    // Ara rewrites the chat draft: rule-based here, a model turn in a deployment.
    rewriteDraft(id, style, asked) {
      const d = this.draft && this.draft.id === id && this.draft.tab === 'chat' ? this.draft : null; if (!d || !d.text) return false;
      const item = this.engine.byId.get(id); const first = item && item.sender && !item.sender.includes(' group') ? item.sender.split(' ')[0] : '';
      const text = rewrite(d.text, style, first);
      this.draft = { id, tab: 'chat', text, source: 'ara' };
      this.engine.exchange(id, asked || style, `Rewritten (${style.toLowerCase()}): “${text}” — it is in the composer; send it, edit it, or ask for another take.`);
      return true;
    }
    composerHtml(item, tab) {
      const id = item.id; const draft = this.draft && this.draft.id === id && this.draft.tab === tab ? this.draft : null;
      const files = this.files[id] || [];
      const chips = files.length ? `<div class="file-chips">${files.map((f, i) => `<span class="fchip"><span class="c-name">${esc(f.name)}</span><span class="c-size">${esc(fmtSize(f.size))}</span><button type="button" class="c-x" data-act="rmfile" data-id="${esc(id)}" data-index="${i}" aria-label="Remove attachment">×</button></span>`).join('')}</div>` : '';
      const err = this.attachError ? `<div class="attach-err">${esc(this.attachError)}</div>` : '';
      let row;
      if (this.rec && this.rec.id === id && this.rec.tab === tab) {
        row = `<div class="composer-row rec-row"><button type="button" class="rec-btn rec-abort" data-act="rec-abort" title="Discard recording" aria-label="Discard recording">✕</button><div class="wave-wrap"><span class="rec-dot" aria-hidden="true"></span><canvas class="wave" data-wave aria-hidden="true"></canvas><span class="rec-time" data-rectime>0:00</span></div><button type="button" class="rec-btn rec-ok" data-act="rec-check" title="Stop and transcribe for review" aria-label="Stop and transcribe for review">✓</button><button type="button" class="rec-btn rec-send" data-act="rec-send" title="Stop, transcribe and send" aria-label="Stop, transcribe and send">➤</button></div>`;
      } else if (this.job && this.job.id === id && this.job.tab === tab) {
        row = `<div class="composer-row rec-row"><div class="wave-wrap rec-status" role="status"><span>${esc(this.job.label)}</span></div></div>`;
      } else {
        const placeholder = tab === 'chat' ? `Reply to ${item.sender}…` : 'Ask Ara…';
        row = `<div class="composer-row"><button type="button" class="mic" data-act="mic" data-id="${esc(id)}" data-tab="${tab}" title="Record a voice message" aria-label="Record a voice message">🎤</button><div class="field"><input id="composer" type="text" autocomplete="off" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}" value="${esc(draft ? draft.text : '')}" data-tab="${tab}" data-id="${esc(id)}"><label class="clip" title="Attach a file" aria-label="Attach a file"><input type="file" multiple hidden data-file data-id="${esc(id)}"><span aria-hidden="true">📎</span></label></div><button type="button" class="send" data-act="send" data-id="${esc(id)}" data-tab="${tab}" title="${tab === 'chat' ? 'Send' : 'Ask Ara'}" aria-label="${tab === 'chat' ? 'Send' : 'Ask Ara'}">➤</button></div>`;
      }
      const bar = draft && draft.text && draft.source !== 'typed' && !this.rec && !this.job ? `<div class="draft-bar"><span class="draft-label">${draft.source === 'ara' ? 'Ara’s draft' : 'Transcribed'} — edit it here${tab === 'chat' ? ' or in the Ara pane' : ''}</span><button type="button" class="btn tiny primary" data-act="send-now" data-id="${esc(id)}" data-tab="${tab}">${tab === 'chat' ? 'Send now' : 'Ask now'}</button><button type="button" class="btn tiny" data-act="discard-draft" data-id="${esc(id)}" data-tab="${tab}">Discard</button></div>` : '';
      return `<div class="composer">${bar}${chips}${err}${row}</div>`;
    }
    // ---- attachments ------------------------------------------------------------------
    addFiles(id, list) {
      this.sim.viewerActed(); this.attachError = '';
      const files = this.files[id] || (this.files[id] = []);
      for (const f of Array.from(list || [])) { if (f.size > MAX_ATTACHMENT_BYTES) { this.attachError = `${f.name} is larger than 25 MB.`; continue; } files.push({ name: f.name, size: f.size }); }
      this.renderAll();
    }
    // ---- voice -----------------------------------------------------------------------------
    async startRecording(id, tab) {
      if (this.rec) return; this.sim.viewerActed(); this.attachError = '';
      const rec = { id, tab, startMs: Date.now(), stream: null, recognition: null, recorder: null, chunks: [], transcript: '', interim: '', real: false, route: null };
      this.rec = rec; this.renderAll();
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        try { rec.stream = await navigator.mediaDevices.getUserMedia({ audio: true }); rec.real = true; } catch (_e) { rec.stream = null; rec.error = 'not-allowed'; }
      }
      if (this.rec !== rec) { if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop()); return; }
      const [route] = this.voiceRoute(); rec.route = route;
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (route === 'stt') {
        if (rec.stream && window.MediaRecorder) { try { const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'].find((m) => MediaRecorder.isTypeSupported(m)); rec.recorder = mime ? new MediaRecorder(rec.stream, { mimeType: mime }) : new MediaRecorder(rec.stream); rec.recorder.ondataavailable = (ev) => { if (ev.data && ev.data.size) rec.chunks.push(ev.data); }; rec.recorder.start(250); } catch (_e) { rec.recorder = null; rec.error = 'recorder'; } }
        else rec.error = rec.error || 'no-mic';
      } else if ((route === 'local' || route === 'cloud') && SR) {
        try {
          const r = new SR(); r.continuous = true; r.interimResults = true; r.lang = navigator.language || 'en'; if (route === 'local') r.processLocally = true;
          r.onresult = (ev) => { let fin = '', interim = ''; for (let i = 0; i < ev.results.length; i++) { const t = ev.results[i][0].transcript; if (ev.results[i].isFinal) fin += t + ' '; else interim += t; } rec.transcript = fin.trim(); rec.interim = interim.trim(); };
          r.onerror = (ev) => { rec.error = ev.error || 'error'; };
          r.onend = () => { rec.ended = true; if (rec.onended) rec.onended(); };
          r.start(); rec.recognition = r;
        } catch (_e) { rec.recognition = null; rec.error = 'unavailable'; }
      } else if (!route) rec.error = rec.error || 'no-route';
      if (!rec.stream && !rec.recognition) this.showToast('No microphone here — the waveform is simulated.');
      this.wave.start(rec.stream, rec.startMs);
    }
    stopRecording(mode) {
      const rec = this.rec; if (!rec) return; this.rec = null; this.wave.stop();
      const seconds = Math.max(1, Math.round((Date.now() - rec.startMs) / 1000));
      if (rec.recognition) { try { rec.recognition.stop(); } catch (_e) { /* ignore */ } }
      if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop());
      if (mode === 'abort') { this.renderAll(); return; }
      const { id, tab } = rec; const routeName = rec.route === 'stt' ? 'stt service' : rec.route === 'local' ? 'on-device' : rec.route === 'cloud' ? 'browser cloud' : null;
      this.job = { id, tab, label: `${mode === 'send' ? 'Transcribing & sending' : 'Transcribing'}${routeName ? ` (${routeName})` : ''} …` }; this.renderAll();
      // The stt route posts the recording; the recogniser routes wait for final results, briefly.
      const settled = rec.route === 'stt' ? this.transcribeViaStt(rec) : new Promise((resolve) => { if (!rec.recognition || rec.ended) return resolve(); rec.onended = resolve; setTimeout(resolve, 1500); });
      settled.then(() => {
        this.job = null;
        const spoken = [rec.transcript, rec.interim].filter(Boolean).join(' ').trim();
        if (spoken) this.showToast(`Transcribed by ${rec.route === 'stt' ? 'your stt service' : rec.route === 'local' ? 'the browser, on-device' : 'the browser’s cloud service'}.`);
        else {
          const why = rec.error === 'no-route' ? 'No private transcription is available here: serve the prototype with serve.ts next to the stt service, or allow the browser’s cloud recognition under Voice transcription.' : rec.error === 'stt' ? `The stt service failed: ${rec.errorDetail || 'no answer'}.` : rec.error === 'no-mic' ? 'No microphone, so nothing was recorded.' : rec.error === 'not-allowed' || rec.error === 'service-not-allowed' ? 'Microphone access was refused for this page.' : rec.error === 'audio-capture' ? 'No microphone was found.' : rec.error === 'network' ? 'The browser’s speech service is unreachable.' : 'Nothing was recognised.';
          this.showToast(`${why} A placeholder stands in for the transcript.`);
        }
        const text = spoken || `🎙 voice note, ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
        if (mode === 'send') { this.send(id, tab, text); return; }
        const prev = this.draft && this.draft.id === id && this.draft.tab === tab ? this.draft.text : '';
        this.draft = { id, tab, text: prev ? `${prev} ${text}` : text, source: 'voice' }; this.renderAll();
      });
    }
    // Post the recording to the stt endpoint (raw bytes, container type, optional language), as the gateway does.
    async transcribeViaStt(rec) {
      if (!rec.recorder) { rec.error = rec.error || 'no-mic'; return; }
      await new Promise((resolve) => { if (rec.recorder.state === 'inactive') return resolve(); rec.recorder.onstop = resolve; try { rec.recorder.stop(); } catch (_e) { resolve(); } });
      const blob = new Blob(rec.chunks, { type: rec.recorder.mimeType || 'application/octet-stream' });
      if (!blob.size) { rec.error = 'no-mic'; return; }
      try {
        const lang = (navigator.language || 'en').split('-')[0];
        const res = await fetch(`${this.sttUrl}${this.sttUrl.includes('?') ? '&' : '?'}lang=${encodeURIComponent(lang)}`, { method: 'POST', headers: { 'Content-Type': blob.type }, body: blob });
        const body = await res.json().catch(() => ({}));
        if (!res.ok || typeof body.text !== 'string') { rec.error = 'stt'; rec.errorDetail = body.error || `HTTP ${res.status}`; return; }
        rec.transcript = body.text.trim();
      } catch (e) { rec.error = 'stt'; rec.errorDetail = String(e && e.message || e); }
    }
    modeMenu() {
      const e = this.engine; const cur = e.mode(); const sched = e.scheduledMode();
      const rows = Object.values(e.modes).map((m) => `<button class="menu-row${cur.id === m.id ? ' on' : ''}" data-act="set-mode" data-mode="${m.id}"><span class="dot" style="background:${m.color}"></span><span><b>${esc(m.name)}</b><small>${esc(m.blurb)}</small></span></button>`).join('');
      return `<div class="overlay" data-act="close-menu"><div class="menu" data-stop="1"><div class="menu-head">Focus mode</div>${rows}<button class="menu-row follow${e.manualMode ? '' : ' on'}" data-act="set-mode" data-mode=""><span class="dot" style="background:${sched.color}"></span><span><b>Follow the schedule</b><small>${esc(sched.name)} until ${hhmm(e.scheduledUntil())}</small></span></button></div></div>`;
    }
    sheetHtml() {
      const e = this.engine; const item = e.byId.get(this.sheet); if (!item) return '';
      const mode = e.mode(); const lvl = e.level(item); const x = e.explain(item);
      const src = item.kind === 'chat' ? `${item.channel} · ${item.sender}${item.count > 1 ? ` · ${item.count} messages` : ''}` : item.kind === 'thread' ? `Thread opened by ${item.agent}` : `Project · ${item.actor === 'you' ? 'your move' : `parked on ${item.actor}`}`;
      const hasPermit = item.sender && e.profile.permits[mode.id].includes(item.sender);
      const admitsSphere = mode.admits.includes(item.sphere);
      const day = Math.floor(e.now / DAY) * DAY; const at = (d, h) => day + d * DAY + h * 60;
      const dueOpts = [[null, 'no deadline'], [at(0, 17), 'today 17:00'], [at(1, 12), 'tomorrow 12:00'], [at(1, 17), 'tomorrow 17:00'], [at(3, 17), 'in 3 days'], [at(7, 17), 'in a week'], [at(14, 17), 'in two weeks'], [at(28, 17), 'in four weeks']].filter(([v]) => v == null || v > e.now);
      if (item.due != null && !dueOpts.some(([v]) => v === item.due)) dueOpts.splice(1, 0, [item.due, whenText(item.due, e.now)]);
      const dueSel = `<select class="select small" data-act="due-set" data-id="${esc(item.id)}">${dueOpts.map(([v, l]) => `<option value="${v == null ? 'none' : v}"${(v == null ? item.due == null : v === item.due) ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
      const leadSel = `<select class="select small" data-act="lead" data-id="${esc(item.id)}">${LEAD_PRESETS.map(([v, l]) => `<option value="${v}"${v === item.lead ? ' selected' : ''}>${l}</option>`).join('')}${LEAD_PRESETS.some(([v]) => v === item.lead) ? '' : `<option value="${item.lead}" selected>${dur(item.lead)}</option>`}</select>`;
      const actions = item.state !== 'open' ? `<div class="done-note">${item.doneHow === 'replied' ? 'Replied' : 'Done'} at ${hhmm(item.doneAt)}.</div><div class="actions"><button class="btn" data-act="view" data-id="${esc(item.id)}">${item.kind === 'chat' ? 'Open the chat' : 'Open the thread'}</button></div>` : item.actor !== 'you' ? `<div class="done-note">Parked on ${esc(item.actor)}${item.waiting_since != null ? ` for ${dur(e.now - item.waiting_since)}` : ''}.</div><div class="actions"><button class="btn primary" data-act="view" data-id="${esc(item.id)}">Discuss with Ara</button><button class="btn" data-act="do" data-id="${esc(item.id)}">Mark resolved</button></div>` : `<div class="actions">
          <button class="btn primary" data-act="view" data-id="${esc(item.id)}">${item.kind === 'chat' ? 'Open the chat' : 'Work on it with Ara'}</button>
          <button class="btn" data-act="later" data-when="next" data-id="${esc(item.id)}">Later · ${esc(e.nextBreakpointText())}</button>
          <button class="btn" data-act="later" data-when="tomorrow" data-id="${esc(item.id)}">Later · tomorrow</button>
          ${!item.released ? `<button class="btn" data-act="pull" data-id="${esc(item.id)}">Pull into the list now</button>` : ''}</div>`;
      return `<div class="overlay" data-act="close-sheet"><div class="sheet" data-stop="1">
        <div class="sheet-head"><div><div class="sheet-title">${esc(item.title)}</div><div class="sheet-src">${esc(src)}</div></div><button class="x" data-act="close-sheet" aria-label="Close">✕</button></div>
        ${item.body ? `<div class="sheet-body">${esc(item.body)}</div>` : ''}
        <div class="fields">
          <div class="field"><div class="f-label">Importance</div><div class="f-value">${esc(x.importance)}</div><div class="f-ctl"><button class="btn tiny" data-act="imp" data-delta="-1" data-id="${esc(item.id)}">−</button><button class="btn tiny" data-act="imp" data-delta="1" data-id="${esc(item.id)}">+</button><span class="f-note">corrects the prior for ${esc(item.sender || item.kindLabel)}</span></div></div>
          <div class="field"><div class="f-label">Urgency</div><div class="f-value">${esc(x.urgency)}</div><div class="f-ctl">${item.critical ? '<span class="f-note">critical is declared, not derived</span>' : `<span class="f-k">deadline</span> ${dueSel}${item.due != null ? `<button class="btn tiny" data-act="due" data-delta="-1440" data-id="${esc(item.id)}">−1 day</button><button class="btn tiny" data-act="due" data-delta="1440" data-id="${esc(item.id)}">+1 day</button>` : ''}<span class="f-break"></span><span class="f-k">lead</span> ${leadSel}<span class="f-note">the lead corrects every “${esc(item.kindLabel)}”</span>`}</div></div>
          <div class="field"><div class="f-label">Delivery</div><div class="f-value">level <b style="color:${LEVEL_COLORS[lvl]}">${esc(lvl)}</b> · ${esc(x.delivery)}</div><div class="f-ctl">${item.sender ? `<button class="btn tiny${hasPermit ? ' on' : ''}" data-act="permit" data-id="${esc(item.id)}">${hasPermit ? `Revoke ${esc(item.sender)}’s ${esc(mode.name)} permit` : `Let ${esc(item.sender)} interrupt in ${esc(mode.name)}`}</button>` : ''}<button class="btn tiny${admitsSphere ? ' on' : ''}" data-act="admit" data-id="${esc(item.id)}">${admitsSphere ? `Stop admitting ${esc(item.sphere)} in ${esc(mode.name)}` : `Admit ${esc(item.sphere)} in ${esc(mode.name)}`}</button><span class="f-note">a Focus rule, importance untouched</span></div></div>
        </div>
        ${actions}
      </div></div>`;
    }
    renderFeed() {
      const feed = this.sim.feed; if (feed.length === this.feedLen) return; this.feedLen = feed.length;
      this.feedEl.innerHTML = feed.length ? feed.map((f, i) => `<div class="entry ${f.who}${f.skipped ? ' skipped' : ''}${f.summary ? ' summary' : ''}${i === feed.length - 1 ? ' latest' : ''}"><span class="t">${hhmm(f.t)}</span><span class="tag">${WHO[f.who] || f.who}</span><span class="txt">${esc(f.text)}</span></div>`).join('') : '<div class="entry"><span class="txt">Press Play, or click on the timeline.</span></div>';
      this.feedEl.scrollTop = this.feedEl.scrollHeight;
    }
    renderTimeline() {
      const e = this.engine; const W = 680, X0 = 20; const x = (m) => X0 + (Math.max(0, Math.min(m, DAY)) / DAY) * W;
      const bands = [];
      const hist = e.modeHistory; for (let i = 0; i < hist.length; i++) { const from = hist[i].from, to = i + 1 < hist.length ? hist[i + 1].from : e.now; bands.push({ from, to, id: hist[i].id, actual: true }); }
      for (let i = 0; i < SCHEDULE.length; i++) { const from = Math.max(SCHEDULE[i][0], e.now), to = i + 1 < SCHEDULE.length ? SCHEDULE[i + 1][0] : DAY; if (to > from) bands.push({ from, to, id: SCHEDULE[i][1], actual: false }); }
      const bandSvg = bands.map((b) => `<rect x="${x(b.from)}" y="10" width="${Math.max(0, x(b.to) - x(b.from))}" height="20" fill="${e.modes[b.id].color}" opacity="${b.actual ? 1 : 0.35}"><title>${esc(e.modes[b.id].name)} ${hhmm(b.from)}–${hhmm(b.to)}${b.actual ? '' : ' (scheduled)'}</title></rect>`).join('');
      const labels = SCHEDULE.map((s, i) => { const to = i + 1 < SCHEDULE.length ? SCHEDULE[i + 1][0] : DAY; return to - s[0] >= 120 ? `<text x="${(x(s[0]) + x(to)) / 2}" y="24" class="tl-mode">${esc(e.modes[s[1]].name)}</text>` : ''; }).join('');
      const digests = DIGEST_TIMES.map((d) => `<polygon points="${x(d) - 4},8 ${x(d) + 4},8 ${x(d)},3" class="tl-digest"><title>digest ${hhmm(d)}</title></polygon>`).join('');
      const beats = this.sim.script.map((b) => `<line x1="${x(b.at)}" y1="34" x2="${x(b.at)}" y2="${b.action ? 42 : 39}" class="tl-beat ${b.who}"/>`).join('');
      const hours = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) => `<text x="${x(h * 60)}" y="60" class="tl-hour">${String(h).padStart(2, '0')}</text>`).join('');
      this.deckEl.querySelector('#timeline').innerHTML = `<svg viewBox="0 0 720 66" class="timeline" data-act="seek" role="img" aria-label="Timeline of the day">${bandSvg}${labels}${digests}${beats}${hours}<g id="playhead" transform="translate(${x(e.now)} 0)"><line x1="0" y1="4" x2="0" y2="48" class="tl-head"/><text id="playhead-t" x="0" y="52" class="tl-head-t"></text></g></svg>`;
    }
    renderState() {
      const e = this.engine; const mode = e.mode(); const st = e.stats(); const p = e.profile;
      const chips = (arr, color) => arr.length ? arr.map((s) => `<span class="pill" style="border-color:${color ? color(s) : 'var(--line)'}">${esc(s)}</span>`).join('') : '<span class="muted">none</span>';
      const num = (v, l) => `<div class="num"><b>${v}</b><span>${l}</span></div>`;
      this.deckEl.querySelector('#state').innerHTML = `
        <h3>System state</h3>
        <div class="mode-card" style="border-left-color:${mode.color}"><div class="mode-name">${esc(mode.name)} <small>${e.manualMode ? 'set by hand' : `scheduled until ${hhmm(e.scheduledUntil())}`}</small></div>
          <div class="kv"><span>admits</span><div>${chips(mode.admits, (s) => SPHERE_COLORS[s])}${mode.admitTags.length ? ` <span class="muted">+ tag${mode.admitTags.length > 1 ? 's' : ''}</span> ${chips(mode.admitTags, (s) => SPHERE_COLORS[s])}` : ''}</div></div>
          <div class="kv"><span>permits</span><div>${chips(p.permits[mode.id])}</div></div>
          <div class="kv"><span>breaks through</span><div>${esc(mode.threshold)} and above</div></div>
          <div class="kv"><span>next breakpoint</span><div>${esc(e.nextBreakpointText())}</div></div></div>
        <div class="nums">${num(st.itemPushes, 'pushes')}${num(st.digests, 'digests')}${num(st.held, 'held')}${num(st.done, 'handled')}${num(st.corrections, 'corrections')}</div>
        <details class="profile" open><summary>Attention profile</summary>
          <div class="kv"><span>importance priors</span><div>${Object.entries(p.priors).map(([k, v]) => `<span class="pill">${esc(k)} <b>${v}</b></span>`).join('')}</div></div>
          <div class="kv"><span>lead times</span><div>${Object.entries(p.leads).filter(([k]) => k !== 'default').map(([k, v]) => `<span class="pill">${esc(k)} <b>${dur(v)}</b></span>`).join('')}</div></div>
          <div class="kv"><span>permits</span><div>${Object.entries(p.permits).filter(([, v]) => v.length).map(([m, v]) => `<span class="pill">${esc(e.modes[m].name)}: ${v.map(esc).join(', ')}</span>`).join('') || '<span class="muted">none yet</span>'}</div></div>
          <div class="kv"><span>learned today</span><div>${p.learned.length ? p.learned.map((l) => `<div class="learned">${hhmm(l.at)} — ${esc(l.text)}</div>`).join('') : '<span class="muted">nothing yet — correct a field on any item (ⓘ in its view)</span>'}</div></div>
        </details>
        <details class="sources"><summary>Backends (example data)</summary>
          ${root.Backends.all.map((b) => `<div class="kv"><span><code>${esc(b.route)}</code></span><div>${esc(String(b.list(e.now).length))} records visible at ${hhmm(e.now)}</div></div>`).join('')}
          <p class="hint">Each backend mirrors one real gateway route and feeds the same attention model; storage stays separate, the dashboard shows the union. Ara’s turns in the threads are canned here; in a deployment they are model turns.</p>
        </details>`;
    }

    // ---- interaction ---------------------------------------------------------------
    send(id, tab, explicit) {
      const input = this.phoneEl.querySelector('#composer'); const fromDom = input && input.dataset.id === id && input.dataset.tab === tab ? input.value : null;
      const text = (explicit != null ? explicit : fromDom != null ? fromDom : (this.draft && this.draft.id === id && this.draft.tab === tab ? this.draft.text : '')).trim();
      const files = (this.files[id] || []).slice(); if (!text && !files.length) return;
      this.sim.viewerActed(); this.draft = null; this.files[id] = []; this.attachError = '';
      if (tab === 'chat') this.engine.sendChat(id, text, files);
      else this.converse(id, text, files);
      this.renderAll();
    }
    converse(id, text, files = []) {
      const intent = /short|brief|concise/i.test(text) ? 'Shorter' : /formal|polite|business/i.test(text) ? 'More formal' : /warm|friendl|nicer|kinder/i.test(text) ? 'Warmer' : null;
      const dlg = this.engine._dialogue(this.engine.byId.get(id));
      if (intent && !files.length && this.draft && this.draft.id === id && this.draft.tab === 'chat' && this.draft.text && !(dlg && dlg.replies && dlg.replies[text])) { this.rewriteDraft(id, intent, text); return; }
      const r = this.engine.say(id, text, files); if (!r) return;
      if (r.draft) { this.draft = { id, tab: 'chat', text: r.draft, source: 'ara' }; if (this.view && this.view.id === id) this.view.tab = 'chat'; }
      const d = this.engine._dialogue(this.engine.byId.get(id)); const rec = d && d.replies && d.replies[text]; if (rec && rec.note) this.showToast(rec.note);
    }
    onClick(ev) {
      const el = ev.target.closest('[data-act]'); if (!el) return;
      const act = el.dataset.act; const id = el.dataset.id; const e = this.engine;
      if (act === 'seek') { const pt = el.getBoundingClientRect(); const rel = (ev.clientX - pt.left) / pt.width; const minute = Math.round(((rel * 720 - 20) / 680) * DAY); this.sim.seek(minute); this.sheet = null; this.menu = false; this.view = null; this.draft = null; this.seenPushes = e.pushes.length; this.banner = null; this.renderAll(); return; }
      const stop = ev.target.closest('[data-stop]');
      if ((act === 'close-sheet' || act === 'close-menu') && stop && el !== ev.target.closest('.x')) return; // clicks inside the sheet do not close it
      switch (act) {
        case 'toggle-play': this.sim.toggle(); break;
        case 'step': this.sim.step(); break;
        case 'restart': this.sim.seek(0); this.sim.pause(); this.sheet = null; this.menu = false; this.view = null; this.draft = null; this.seenPushes = 0; this.banner = null; break;
        case 'resume': this.sim.resume(); break;
        case 'dismiss-banner': this.banner = null; break;
        case 'mode-menu': this.sim.viewerActed(); this.menu = !this.menu; break;
        case 'close-menu': this.menu = false; break;
        case 'set-mode': this.sim.viewerActed(); e.setManualMode(el.dataset.mode || null); this.menu = false; break;
        case 'view': this.sim.viewerActed(); this.openView(id); break;
        case 'back': this.view = null; this.draft = null; if (this.rec) this.stopRecording('abort'); break;
        case 'tab': this.sim.viewerActed(); if (this.view) this.view.tab = el.dataset.tab; break;
        case 'chip': this.sim.viewerActed(); this.converse(id, el.dataset.label); break;
        case 'send': this.send(id, el.dataset.tab); return;
        case 'mic': this.startRecording(id, el.dataset.tab); return;
        case 'rec-abort': this.stopRecording('abort'); return;
        case 'rec-check': this.stopRecording('review'); return;
        case 'rec-send': this.stopRecording('send'); return;
        case 'rmfile': this.sim.viewerActed(); (this.files[id] || []).splice(Number(el.dataset.index), 1); break;
        case 'send-now': this.send(id, el.dataset.tab, this.draft && this.draft.id === id && this.draft.tab === el.dataset.tab && this.view && this.view.tab !== el.dataset.tab ? this.draft.text : undefined); return;
        case 'discard-draft': this.sim.viewerActed(); if (this.draft && this.draft.id === id) this.draft = null; break;
        case 'rewrite': this.sim.viewerActed(); this.rewriteDraft(id, el.dataset.style); break;
        case 'open': this.sim.viewerActed(); this.sheet = id; break;
        case 'close-sheet': this.sheet = null; break;
        case 'toggle-held': this.sim.viewerActed(); this.heldOpen = !this.heldOpen; break;
        case 'toggle-waiting': this.sim.viewerActed(); this.waitingOpen = !this.waitingOpen; break;
        case 'do': this.sim.viewerActed(); e.doIt(id); this.sheet = null; break;
        case 'later': this.sim.viewerActed(); e.later(id, el.dataset.when); this.sheet = null; if (this.view && this.view.id === id) { this.view = null; this.draft = null; } break;
        case 'pull': this.sim.viewerActed(); e.pull(id); break;
        case 'imp': { this.sim.viewerActed(); const it = e.byId.get(id); const v = Math.max(0, Math.min(5, it.importance + Number(el.dataset.delta))); if (v !== it.importance) e.correct(id, { importance: v }); break; }
        case 'due': { this.sim.viewerActed(); const it = e.byId.get(id); const d = el.dataset.delta; e.correct(id, { due: d === 'none' ? null : (it.due != null ? it.due : e.now) + Number(d) }); break; }
        case 'permit': { this.sim.viewerActed(); const it = e.byId.get(id); const m = e.mode(); e.permit(it.sender, m.id, !e.profile.permits[m.id].includes(it.sender)); break; }
        case 'admit': { this.sim.viewerActed(); const it = e.byId.get(id); const m = e.mode(); e.admitSphere(it.sphere, m.id, !m.admits.includes(it.sphere)); break; }
        default: return;
      }
      this.renderAll();
    }
    onInput(ev) {
      const el = ev.target.closest('[data-act]'); if (!el) return;
      if (el.dataset.act === 'speed') { this.sim.speed = Number(el.value); return; }
      if (el.dataset.act === 'cloud-ok') { this.cloudOk = !!el.checked; try { localStorage.setItem('attention-voice-cloud', this.cloudOk ? '1' : '0'); } catch (_e) { /* no storage */ } this.renderVoiceLine(); return; }
      if (el.dataset.act === 'lead') { this.sim.viewerActed(); this.engine.correct(el.dataset.id, { lead: Number(el.value) }); this.renderAll(); }
      if (el.dataset.act === 'due-set') { this.sim.viewerActed(); this.engine.correct(el.dataset.id, { due: el.value === 'none' ? null : Number(el.value) }); this.renderAll(); }
    }
  }

  root.AttentionApp = { boot: (el) => (root.attentionApp = new App(el)) };
})(globalThis);
