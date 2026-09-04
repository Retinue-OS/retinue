// The prototype's interface: the phone dashboard (interactive at all times), the
// simulation deck (clock, timeline, narration, state) and the wiring between them.
(function (root) {
  'use strict';
  const U = root.AttentionUtil;
  const { hhmm, whenText, dur, DAY, rank, SCHEDULE, DIGEST_TIMES } = U;
  const SPHERE_COLORS = { customers: '#6ea8fe', admin: '#c9a0ff', health: '#ff6b6b', friends: '#57c785', family: '#ffb86b', system: '#9aa5b1' };
  const LEVEL_COLORS = { critical: '#ff5d5d', 'time-sensitive': '#e08a2e', active: '#4fb3b9', passive: '#4a5563' };
  const WHO = { narrator: 'story', you: 'you', system: 'system', push: 'push', learn: 'profile' };
  const LEAD_PRESETS = [[60, '1 h'], [120, '2 h'], [360, '6 h'], [1440, '1 day'], [2880, '2 days'], [4320, '3 days'], [10080, '1 week'], [20160, '2 weeks'], [40320, '4 weeks']];
  const SPEEDS = [[2, '×1 — the day in 12 min'], [4.8, '×2 — 5 min'], [12, '×5 — 2 min'], [24, '×10 — 1 min']];
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  class App {
    constructor(rootEl) {
      this.root = rootEl; this.engine = new root.AttentionEngine();
      this.sim = new root.Simulation(this.engine, { onChange: () => this.renderAll() });
      this.sheet = null; this.menu = false; this.heldOpen = false; this.waitingOpen = false;
      this.banner = null; this.bannerTimer = null; this.seenPushes = 0; this.feedLen = -1;
      this.phoneEl = rootEl.querySelector('#phone'); this.deckEl = rootEl.querySelector('#deck');
      this.buildDeck();
      rootEl.addEventListener('click', (e) => this.onClick(e));
      rootEl.addEventListener('change', (e) => this.onInput(e));
      const params = new URLSearchParams(location.search);
      const at = params.get('at'); if (at) { const [h, m] = at.split(':').map(Number); this.sim.seek(h * 60 + (m || 0)); this.seenPushes = this.engine.pushes.length; }
      const open = params.get('open'); if (open && this.engine.byId.has(open)) this.sheet = open;
      if (params.get('held') === '1') this.heldOpen = true;
      this.renderAll();
      if (params.get('play') === '1') this.sim.play();
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

    row(item, section) {
      const e = this.engine; const lvl = e.level(item);
      const meta = item.actor !== 'you' ? `waiting ${item.waiting_since != null ? dur(e.now - item.waiting_since) : ''}` : `${e.urgencyShort(item)} · ${item.kind === 'chat' ? item.channel : item.kind}`;
      const why = item.actor !== 'you' ? `importance ${item.importance} · parked on ${esc(item.actor)}` : `importance ${item.importance} · ${esc(e.urgencyShort(item))} · ${esc(section === 'now' ? e.admissionReason(item, e.mode()) : e.deliveryShort(item))}`;
      return `<button class="row" data-act="open" data-id="${esc(item.id)}" style="--stripe:${LEVEL_COLORS[lvl]}" title="${esc(lvl)}">
        <div class="row-top"><span class="chip"><i style="background:${SPHERE_COLORS[item.sphere] || '#9aa5b1'}"></i>${esc(item.sphere)}</span>${item.count > 1 ? `<span class="count">${item.count} msgs</span>` : ''}<span class="meta">${esc(meta)}</span></div>
        <div class="row-title">${esc(item.title)}</div>
        <div class="row-why">${why}</div></button>`;
    }
    renderPhone() {
      const e = this.engine, s = e.sections(); const mode = s.mode;
      const until = e.manualMode ? 'by hand' : `until ${hhmm(e.scheduledUntil())}`;
      const nb = e.nextBreakpointText();
      const total = s.now.length + s.next.length + s.held.length + s.waiting.length;
      const banner = this.banner ? `<div class="banner ${this.banner.urgency}" data-act="dismiss-banner"><span class="bell">🔔</span><div><div class="b-title">${esc(this.banner.title)}<span class="b-urg">Urgency: ${esc(this.banner.urgency)}${this.banner.topic ? ' · Topic: ' + esc(this.banner.topic) : ''}</span></div><div class="b-body">${esc(this.banner.body)}</div></div></div>` : '';
      const section = (label, items, key) => items.length ? `<section class="sec ${key}"><h4>${label} · ${items.length}</h4>${items.map((i) => this.row(i, key)).join('')}</section>` : '';
      const collapsible = (label, items, key, open) => items.length ? `<section class="sec ${key}"><button class="sec-toggle" data-act="toggle-${key}"><span>${label} · ${items.length}</span><span class="chev">${open ? '▾' : '▸'}</span></button>${open ? items.map((i) => this.row(i, key)).join('') : ''}</section>` : '';
      this.phoneEl.innerHTML = `<div class="phone"><div class="screen">
        <div class="ph-head"><div class="ph-title">Attention</div><button class="mode-chip" data-act="mode-menu" style="border-color:${mode.color}"><span class="dot" style="background:${mode.color}"></span>${esc(mode.name)} · ${until} ▾</button></div>
        ${banner}
        <div class="ph-body">
          ${section('NOW', s.now, 'now')}
          ${section('NEXT', s.next, 'next')}
          ${collapsible(`HELD UNTIL ${nb.toUpperCase()}`, s.held, 'held', this.heldOpen)}
          ${collapsible('WAITING ON OTHERS', s.waiting, 'waiting', this.waitingOpen)}
          ${total ? '' : '<div class="empty">Nothing wants your attention.</div>'}
        </div>
        ${this.menu ? this.modeMenu() : ''}
        ${this.sheet ? this.sheetHtml() : ''}
      </div></div>`;
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
      const leadSel = `<select class="select small" data-act="lead" data-id="${esc(item.id)}">${LEAD_PRESETS.map(([v, l]) => `<option value="${v}"${v === item.lead ? ' selected' : ''}>${l}</option>`).join('')}${LEAD_PRESETS.some(([v]) => v === item.lead) ? '' : `<option value="${item.lead}" selected>${dur(item.lead)}</option>`}</select>`;
      const actions = item.state !== 'open' ? `<div class="done-note">Done at ${hhmm(item.doneAt)}.</div>` : item.actor !== 'you' ? `<div class="done-note">Parked on ${esc(item.actor)}${item.waiting_since != null ? ` for ${dur(e.now - item.waiting_since)}` : ''}. Nothing to do until they move.</div><div class="actions"><button class="btn" data-act="do" data-id="${esc(item.id)}">Mark resolved</button></div>` : `<div class="actions">
          <button class="btn primary" data-act="do" data-id="${esc(item.id)}">Do it</button>
          <button class="btn" data-act="later" data-when="next" data-id="${esc(item.id)}">Later · ${esc(e.nextBreakpointText())}</button>
          <button class="btn" data-act="later" data-when="tomorrow" data-id="${esc(item.id)}">Later · tomorrow</button>
          ${!item.released ? `<button class="btn" data-act="pull" data-id="${esc(item.id)}">Pull into the list now</button>` : ''}</div>`;
      return `<div class="overlay" data-act="close-sheet"><div class="sheet" data-stop="1">
        <div class="sheet-head"><div><div class="sheet-title">${esc(item.title)}</div><div class="sheet-src">${esc(src)}</div></div><button class="x" data-act="close-sheet" aria-label="Close">✕</button></div>
        ${item.body ? `<div class="sheet-body">${esc(item.body)}</div>` : ''}
        <div class="fields">
          <div class="field"><div class="f-label">Importance</div><div class="f-value">${esc(x.importance)}</div><div class="f-ctl"><button class="btn tiny" data-act="imp" data-delta="-1" data-id="${esc(item.id)}">−</button><button class="btn tiny" data-act="imp" data-delta="1" data-id="${esc(item.id)}">+</button><span class="f-note">corrects the prior for ${esc(item.sender || item.kindLabel)}</span></div></div>
          <div class="field"><div class="f-label">Urgency</div><div class="f-value">${esc(x.urgency)}</div><div class="f-ctl">${item.critical ? '<span class="f-note">critical is declared, not derived</span>' : `lead ${leadSel}<button class="btn tiny" data-act="due" data-delta="-1440" data-id="${esc(item.id)}">−1 day</button><button class="btn tiny" data-act="due" data-delta="1440" data-id="${esc(item.id)}">+1 day</button>${item.due != null ? `<button class="btn tiny" data-act="due" data-delta="none" data-id="${esc(item.id)}">no deadline</button>` : ''}<span class="f-note">lead corrects every “${esc(item.kindLabel)}”</span>`}</div></div>
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
        <div class="nums">${num(st.itemPushes, 'pushes')}${num(st.digests, 'digests')}${num(st.held, 'held')}${num(st.done, 'done')}${num(st.corrections, 'corrections')}</div>
        <details class="profile" open><summary>Attention profile</summary>
          <div class="kv"><span>importance priors</span><div>${Object.entries(p.priors).map(([k, v]) => `<span class="pill">${esc(k)} <b>${v}</b></span>`).join('')}</div></div>
          <div class="kv"><span>lead times</span><div>${Object.entries(p.leads).filter(([k]) => k !== 'default').map(([k, v]) => `<span class="pill">${esc(k)} <b>${dur(v)}</b></span>`).join('')}</div></div>
          <div class="kv"><span>permits</span><div>${Object.entries(p.permits).filter(([, v]) => v.length).map(([m, v]) => `<span class="pill">${esc(e.modes[m].name)}: ${v.map(esc).join(', ')}</span>`).join('') || '<span class="muted">none yet</span>'}</div></div>
          <div class="kv"><span>learned today</span><div>${p.learned.length ? p.learned.map((l) => `<div class="learned">${hhmm(l.at)} — ${esc(l.text)}</div>`).join('') : '<span class="muted">nothing yet — correct a field on any item</span>'}</div></div>
        </details>
        <details class="sources"><summary>Backends (example data)</summary>
          ${root.Backends.all.map((b) => `<div class="kv"><span><code>${esc(b.route)}</code></span><div>${esc(String(b.list(e.now).length))} records visible at ${hhmm(e.now)}</div></div>`).join('')}
          <p class="hint">Each backend mirrors one real gateway route and feeds the same attention model; storage stays separate, the dashboard shows the union.</p>
        </details>`;
    }

    // ---- interaction ---------------------------------------------------------------
    onClick(ev) {
      const el = ev.target.closest('[data-act]'); if (!el) return;
      const act = el.dataset.act; const id = el.dataset.id; const e = this.engine;
      if (act === 'seek') { const svg = el; const pt = svg.getBoundingClientRect(); const rel = (ev.clientX - pt.left) / pt.width; const minute = Math.round(((rel * 720 - 20) / 680) * DAY); this.sim.seek(minute); this.sheet = null; this.menu = false; this.seenPushes = e.pushes.length; this.banner = null; this.renderAll(); return; }
      const stop = ev.target.closest('[data-stop]');
      if ((act === 'close-sheet' || act === 'close-menu') && stop && el !== ev.target.closest('.x')) return; // clicks inside the sheet do not close it
      switch (act) {
        case 'toggle-play': this.sim.toggle(); break;
        case 'step': this.sim.step(); break;
        case 'restart': this.sim.seek(0); this.sim.pause(); this.sheet = null; this.menu = false; this.seenPushes = 0; this.banner = null; break;
        case 'resume': this.sim.resume(); break;
        case 'dismiss-banner': this.banner = null; break;
        case 'mode-menu': this.sim.viewerActed(); this.menu = !this.menu; break;
        case 'close-menu': this.menu = false; break;
        case 'set-mode': this.sim.viewerActed(); e.setManualMode(el.dataset.mode || null); this.menu = false; break;
        case 'open': this.sim.viewerActed(); this.sheet = id; break;
        case 'close-sheet': this.sheet = null; break;
        case 'toggle-held': this.sim.viewerActed(); this.heldOpen = !this.heldOpen; break;
        case 'toggle-waiting': this.sim.viewerActed(); this.waitingOpen = !this.waitingOpen; break;
        case 'do': this.sim.viewerActed(); e.doIt(id); this.sheet = null; break;
        case 'later': this.sim.viewerActed(); e.later(id, el.dataset.when); this.sheet = null; break;
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
      if (el.dataset.act === 'lead') { this.sim.viewerActed(); this.engine.correct(el.dataset.id, { lead: Number(el.value) }); this.renderAll(); }
    }
  }

  root.AttentionApp = { boot: (el) => (root.attentionApp = new App(el)) };
})(globalThis);
