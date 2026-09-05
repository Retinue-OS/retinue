// The simulation deck: clock, controls, timeline, the day's feed and the
// system state, polled from GET /simulation — beside the phone, whose iframe
// is the real dashboard served by the same gateway. A click inside the phone
// pauses the story ("you are driving"); Resume plays on from where you are.
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const DAY = 1440;
const MODE_COLORS = { off: '#3a4250', home: '#a86f2c', deep: '#0f4f57', open: '#8a94a0', work: '#2f8a90', social: '#7a4f96' };
const SPHERE_COLORS = { customers: '#6ea8fe', admin: '#c9a0ff', health: '#ff6b6b', friends: '#57c785', family: '#ffb86b', system: '#9aa5b1' };
const WHO = { narrator: 'story', you: 'you', system: 'system', push: 'push', learn: 'profile', ara: 'Ara', reply: 'reply' };
const pad = (n) => String(n).padStart(2, '0');
const hhmm = (m) => { const x = ((Math.round(m) % DAY) + DAY) % DAY; return `${pad(Math.floor(x / 60))}:${pad(x % 60)}`; };
const nice = (v) => (Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1));
const dur = (min) => { min = Math.round(min); if (min < 60) return `${min} min`; if (min < DAY) return `${nice(min / 60)} h`; return `${nice(min / DAY)} d`; };
const whenText = (iso, base) => { if (!iso) return ''; const d = new Date(iso); const m = (d - base) / 60000; return m < DAY ? hhmm(m) : `tomorrow ${hhmm(m)}`; };

class Deck {
  constructor() {
    this.deck = $('#deck'); this.phone = $('#phone'); this.snap = null; this.feedLen = -1; this.lastView = null; this.iframeDoc = null;
    this.buildDeck();
    document.addEventListener('click', (e) => this.onClick(e));
    document.addEventListener('change', (e) => this.onChange(e));
    this.phone.addEventListener('load', () => this.watchPhone());
    this.poll(); setInterval(() => this.poll(), 600);
  }
  async post(cmd, body) {
    const res = await fetch(`/simulation/${cmd}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    if (res.ok) { this.apply(await res.json()); }
  }
  async poll() {
    try { const res = await fetch('/simulation', { cache: 'no-store' }); if (res.ok) this.apply(await res.json()); } catch (_e) { /* next poll */ }
  }
  apply(snap) {
    const beatsBefore = this.snap ? this.snap.feed.filter((f) => f.beat).length : -1;
    this.snap = snap;
    const beatsNow = snap.feed.filter((f) => f.beat).length;
    this.render();
    // A new beat: refresh the home list at once (the page polls every 5 s),
    // and show the view the beat opened — a chat, a thread, an item's sheet.
    if (beatsNow !== beatsBefore) this.followBeat(beatsBefore === -1);
  }
  followBeat(first) {
    const last = [...this.snap.feed].reverse().find((f) => f.beat);
    const view = last && last.view ? last.view : '/';
    if (first && view === '/') { this.nudgePhone(); return; }
    if (this.snap.driving) return;
    if (view !== this.lastView) { this.lastView = view; this.navigatePhone(view); } else this.nudgePhone();
  }
  navigatePhone(view) {
    try {
      const w = this.phone.contentWindow; const cur = w.location.pathname + w.location.search + w.location.hash;
      if (cur === view) { this.nudgePhone(); return; }
      if (view.startsWith('/#') && w.location.pathname === '/' && !w.location.search) { w.location.hash = view.slice(1); return; }
      w.location.replace(view);
    } catch (_e) { this.phone.src = view; }
  }
  nudgePhone() { try { this.phone.contentWindow.dispatchEvent(new CustomEvent('retinue-attention-change')); } catch (_e) { /* cross-origin never happens */ } }
  watchPhone() {
    try {
      const doc = this.phone.contentDocument; if (!doc || doc === this.iframeDoc) return; this.iframeDoc = doc;
      const acted = () => { if (this.snap && this.snap.playing) this.post('acted'); };
      doc.addEventListener('pointerdown', acted, true); doc.addEventListener('keydown', acted, true);
    } catch (_e) { /* ignore */ }
  }
  buildDeck() {
    this.deck.innerHTML = `
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
            <div class="date" id="date"></div>
            <div class="mode-line" id="mode-line"></div>
          </div>
        </div>
        <div class="controls">
          <button class="btn primary" data-act="toggle-play" id="btn-play">▶ Play the day</button>
          <button class="btn" data-act="step" title="Jump to the next beat of the story">⏭ Next beat</button>
          <button class="btn" data-act="restart" title="Back to midnight">↺ Restart</button>
          <select class="select" data-act="speed" aria-label="Speed" id="speed"></select>
        </div>
        <div class="driving" id="driving" hidden><span>You are driving — the story is paused and will adapt to what you changed.</span><button class="btn primary" data-act="resume">▶ Resume the story</button></div>
        <div class="ended" id="ended" hidden>The day is over. <button class="btn" data-act="restart">Play it again</button></div>
        <p class="hint">Everything in the phone is the real dashboard: the mode chip, the ⓘ sheets, Later and Mark done, the chats and their Ara pane, the threads with their chips. Ara's turns are canned here; in a deployment they are model turns.</p>
      </section>
      <section class="card timeline-card"><div id="timeline"></div><p class="hint">Click anywhere on the day to jump there. Bands: the mode in force; ▲ digest times; ticks: beats of the story (orange: something you do).</p></section>
      <section class="card feed-card"><h3>The day</h3><div class="feed" id="feed"></div></section>
      <section class="card state-card" id="state"></section>`;
    this.feedEl = $('#feed');
  }
  render() {
    const s = this.snap; if (!s) return;
    const now = s.minute; const h = (now / 60) % 12, m = now % 60;
    $('#hand-h').setAttribute('transform', `rotate(${h * 30} 50 50)`);
    $('#hand-m').setAttribute('transform', `rotate(${m * 6} 50 50)`);
    $('#digital').textContent = s.time; $('#mini-time').textContent = s.time;
    $('#date').textContent = new Date(s.date + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'long' });
    const att = s.attention || {}; const mode = att.mode || {};
    const base = new Date(s.date + 'T00:00:00');
    const modeLine = `<span class="dot" style="background:${MODE_COLORS[mode.id] || '#6ea8fe'}"></span>${esc(mode.name || '')}${mode.manual ? ' · set by hand' : (mode.scheduled ? ` · until ${whenText(mode.scheduled.until, base)}` : '')}`;
    $('#mode-line').innerHTML = modeLine; $('#mini-mode').innerHTML = modeLine;
    $('#btn-play').textContent = s.playing ? '⏸ Pause' : '▶ Play the day'; $('#mini-play').textContent = s.playing ? '⏸' : '▶';
    $('#driving').hidden = !s.driving; $('#ended').hidden = !s.ended; $('#minibar').hidden = false;
    const sel = $('#speed'); if (!sel.options.length) sel.innerHTML = s.speeds.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
    if (Number(sel.value) !== s.speed) sel.value = String(s.speed);
    this.renderTimeline(); this.renderFeed(); this.renderState();
  }
  renderTimeline() {
    const s = this.snap; const att = s.attention || {}; const W = 680, X0 = 20; const x = (m) => X0 + (Math.max(0, Math.min(m, DAY)) / DAY) * W;
    const schedule = att.schedule || []; const modes = att.modes || {};
    const bands = schedule.map((e, i) => { const from = e[0], to = i + 1 < schedule.length ? schedule[i + 1][0] : DAY; return `<rect x="${x(from)}" y="10" width="${Math.max(0, x(to) - x(from))}" height="20" fill="${MODE_COLORS[e[1]] || '#666'}" opacity="${from <= s.minute ? 1 : 0.4}"><title>${esc((modes[e[1]] || {}).name || e[1])} ${hhmm(from)}–${hhmm(to)}</title></rect>`; }).join('');
    const labels = schedule.map((e, i) => { const to = i + 1 < schedule.length ? schedule[i + 1][0] : DAY; return to - e[0] >= 120 ? `<text x="${(x(e[0]) + x(to)) / 2}" y="24" class="tl-mode">${esc((modes[e[1]] || {}).name || e[1])}</text>` : ''; }).join('');
    const digests = (att.digest_times || []).map((d) => `<polygon points="${x(d) - 4},8 ${x(d) + 4},8 ${x(d)},3" class="tl-digest"><title>digest ${hhmm(d)}</title></polygon>`).join('');
    const beats = (s.beats || []).map((b) => `<line x1="${x(b.at)}" y1="34" x2="${x(b.at)}" y2="${b.action ? 42 : 39}" class="tl-beat ${b.who}"/>`).join('');
    const hours = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((hh) => `<text x="${x(hh * 60)}" y="60" class="tl-hour">${pad(hh)}</text>`).join('');
    $('#timeline').innerHTML = `<svg viewBox="0 0 720 66" class="timeline" data-act="seek" role="img" aria-label="Timeline of the day">${bands}${labels}${digests}${beats}${hours}<g transform="translate(${x(s.minute)} 0)"><line x1="0" y1="4" x2="0" y2="48" class="tl-head"/><text x="0" y="52" class="tl-head-t">${s.time}</text></g></svg>`;
  }
  renderFeed() {
    const feed = this.snap.feed || []; const sig = feed.length + ':' + (feed.length ? feed[feed.length - 1].t : '');
    if (sig === this.feedSig) return; this.feedSig = sig;
    this.feedEl.innerHTML = feed.length ? feed.map((f, i) => `<div class="entry ${f.who}${f.skipped ? ' skipped' : ''}${f.summary ? ' summary' : ''}${i === feed.length - 1 ? ' latest' : ''}"><span class="t">${hhmm(f.t)}</span><span class="tag">${WHO[f.who] || f.who}</span><span class="txt">${esc(f.text)}</span></div>`).join('') : '<div class="entry"><span class="txt">Press Play, or click on the timeline.</span></div>';
    this.feedEl.scrollTop = this.feedEl.scrollHeight;
  }
  renderState() {
    const s = this.snap; const att = s.attention || {}; const mode = att.mode || {}; const modes = att.modes || {}; const st = s.stats || {};
    const base = new Date(s.date + 'T00:00:00');
    const chips = (arr, color) => (arr && arr.length ? arr.map((x) => `<span class="pill" style="border-color:${color ? color(x) : 'var(--line)'}">${esc(x)}</span>`).join('') : '<span class="muted">none</span>');
    const num = (v, l) => `<div class="num"><b>${v}</b><span>${l}</span></div>`;
    const permits = (att.permits || {})[mode.id] || [];
    $('#state').innerHTML = `
      <h3>System state</h3>
      <div class="mode-card" style="border-left-color:${MODE_COLORS[mode.id] || '#6ea8fe'}"><div class="mode-name">${esc(mode.name || '')} <small>${mode.manual ? 'set by hand' : (mode.scheduled ? `scheduled until ${whenText(mode.scheduled.until, base)}` : '')}</small></div>
        <div class="kv"><span>admits</span><div>${chips(mode.admits, (x) => SPHERE_COLORS[x])}${(mode.admit_tags || []).length ? ` <span class="muted">+ tag</span> ${chips(mode.admit_tags, (x) => SPHERE_COLORS[x])}` : ''}</div></div>
        <div class="kv"><span>permits</span><div>${chips(permits)}</div></div>
        <div class="kv"><span>breaks through</span><div>${esc(mode.threshold || '')} and above</div></div>
        <div class="kv"><span>next breakpoint</span><div>${whenText(att.next_breakpoint, base)}</div></div></div>
      <div class="nums">${num(st.pushes || 0, 'pushes')}${num(st.digests || 0, 'digests')}${num(st.held || 0, 'held')}${num(st.handled || 0, 'handled')}${num(st.corrections || 0, 'corrections')}${num(st.replies || 0, 'Ara replies')}</div>
      <details class="profile" open><summary>Attention profile</summary>
        <div class="kv"><span>importance priors</span><div>${Object.entries(att.priors || {}).map(([k, v]) => `<span class="pill">${esc(k)} <b>${v}</b></span>`).join('')}</div></div>
        <div class="kv"><span>lead times</span><div>${Object.entries(att.leads || {}).filter(([k]) => k !== 'default').map(([k, v]) => `<span class="pill">${esc(k)} <b>${dur(v)}</b></span>`).join('')}</div></div>
        <div class="kv"><span>permits</span><div>${Object.entries(att.permits || {}).filter(([, v]) => v.length).map(([mid, v]) => `<span class="pill">${esc((modes[mid] || {}).name || mid)}: ${v.map(esc).join(', ')}</span>`).join('') || '<span class="muted">none yet</span>'}</div></div>
        <div class="kv"><span>learned today</span><div>${(att.learned || []).length ? att.learned.map((l) => `<div class="learned">${esc(l.text)}</div>`).join('') : '<span class="muted">nothing yet — correct a field on any item (ⓘ in the phone)</span>'}</div></div>
      </details>
      <details class="sources"><summary>What is real here</summary>
        <p class="hint">The gateway (<code>scripts/web-gateway.py</code>) with the attention model, the home screen, the chat page and the threads are the deployment's own code. The life store, the messenger gateways and Ara are stand-ins: a mock ledger with the example messages, mock gateways that accept your sends, and canned dialogues in place of model turns.</p>
      </details>`;
  }
  onClick(ev) {
    const el = ev.target.closest('[data-act]'); if (!el) return;
    const act = el.dataset.act;
    if (act === 'seek') { const pt = el.getBoundingClientRect(); const rel = (ev.clientX - pt.left) / pt.width; const minute = Math.round(((rel * 720 - 20) / 680) * DAY); this.lastView = null; this.post('seek', { minute }).then(() => this.navigatePhone('/')); return; }
    if (act === 'toggle-play') { this.post(this.snap && this.snap.playing ? 'pause' : 'play'); return; }
    if (act === 'step') { this.post('step'); return; }
    if (act === 'restart') { this.lastView = null; this.post('restart').then(() => this.navigatePhone('/')); return; }
    if (act === 'resume') { this.post('resume'); return; }
  }
  onChange(ev) {
    const el = ev.target.closest('[data-act]'); if (!el) return;
    if (el.dataset.act === 'speed') this.post('speed', { speed: Number(el.value) });
  }
}
new Deck();
