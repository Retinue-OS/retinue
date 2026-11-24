// The script of the pending-send pages the gateway renders itself
// (scripts/web-gateway.py: /sends, /sends/next and /sends/<account>/<id>).
//
// It used to be written into each page. It is a file of the dashboard's own
// because the pages' Content-Security-Policy runs scripts only from such
// files (docs/dashboard.md, "The Content-Security-Policy"), and what a page
// needs to know is in its markup, written there by the gateway, so one file
// serves all of them:
//
//   <body data-sends-leave>       the empty list a decision lands on when
//                                 nothing is left to decide: go back at once
//   #form-approve, #form-reject   the approval card: lock both buttons once
//                                 one is pressed, so a slow answer is not
//                                 met with a second decision
//   #back-thread[data-thread]     the way back to the thread that asked:
//                                 Back when it is where the tab came from
//   #st-row[data-status]          the status page of a decided send: poll
//                                 while it is sending, then say how it ended
//                                 and move on — or, where nobody knows how
//                                 it ended, that it is not confirmed (the
//                                 line from data-note-unknown, else the
//                                 script's own)
//
// A classic script at the end of the body, where the inline scripts stood,
// so everything it looks for is already there. Written for old WebViews
// too, like the inline scripts were: no modules, no arrow functions.
(function () {
  // The place in the thread the approval link was tapped at. The thread
  // writes it as the link is tapped (conversation.js, _noteSendsReturn: the
  // thread id, its shelfLocation() and the page it was open on); a way back
  // hands it to the thread page in the shelf's restore format (shelf-store.js
  // setRestore), so the thread opens at that exact message whether the
  // browser brings the page back from its cache or loads it afresh.
  //
  // The note is for the visit that tap began: a way back uses it up, and one
  // left behind (the reader went back by other means) lapses, so an approval
  // opened later in this tab does not land at a place long left.
  var SENDS_RETURN_KEY = 'retinue.sends.return';
  var SENDS_RETURN_TTL_MS = 30 * 60 * 1000;
  var SHELF_RESTORE_KEY = 'retinue.shelf.restore';

  function sendsReturn(id) {
    try {
      var r = JSON.parse(sessionStorage.getItem(SENDS_RETURN_KEY) || 'null');
      if (!r || r.id !== id) return null;
      if (Date.now() - (r.at || 0) < SENDS_RETURN_TTL_MS) return r;
      sessionStorage.removeItem(SENDS_RETURN_KEY);
      return null;
    } catch (e) { return null; }
  }
  function primeReturn(id) {
    var r = id && sendsReturn(id);
    if (!r) return;
    try { sessionStorage.removeItem(SENDS_RETURN_KEY); } catch (e) { /* lapses */ }
    if (!r.location) return;
    try {
      sessionStorage.setItem(SHELF_RESTORE_KEY, JSON.stringify(
        { key: 'thread:' + id, location: r.location, at: Date.now() }));
    } catch (e) { /* the thread opens at its newest message */ }
  }
  function threadOf(u) {
    var m = /^#conversation-([0-9a-f]{32})$/.exec(u.hash || '');
    return m ? m[1] : null;
  }

  // The way out of the /sends pages once nothing is left to decide: back to
  // the page the approval link was tapped on. The dashboard opens it in place
  // (#359), so that is the latest same-origin history entry that is not a
  // /sends page (Navigation API), however many approval and status pages
  // followed it. A page opened on its own (a link from another app, another
  // site before it in the tab) has none, so it closes; where the browser
  // refuses that, the dashboard, never the empty /sends list.
  function closeOrHome() {
    window.close();
    setTimeout(function () { location.replace('/'); }, 400);
  }

  // The latest same-origin history entry that is not a /sends page, or null.
  function originEntry() {
    var nav = window.navigation;
    if (!nav || !nav.currentEntry || !nav.entries) return null;
    var entries = nav.entries();
    for (var i = nav.currentEntry.index - 1; i >= 0; i--) {
      var u;
      try { u = new URL(entries[i].url); } catch (e) { continue; }
      if (u.origin !== location.origin || /^\/sends(\/|$)/.test(u.pathname)) continue;
      return { entry: entries[i], url: u };
    }
    return null;
  }

  function leave() {
    var o = originEntry();
    if (o) {
      primeReturn(threadOf(o.url));
      var t = window.navigation.traverseTo(o.entry.key);
      if (t && t.committed) { t.committed.catch(closeOrHome); }
      return;
    }
    closeOrHome();
  }

  if (document.body.hasAttribute('data-sends-leave')) {
    leave();
    return;
  }

  // "Back to the thread" (#back-thread): the way to the dashboard thread that
  // asked for the approval. When that thread is where this tab came from, the
  // tap is the browser's own Back (traverseTo, stepping over any approval and
  // status pages in between), so the thread is exactly as it was left.
  // Otherwise (no Navigation API, the approval opened from a push or another
  // app) it stays a plain link to the thread, on the page it was open on when
  // the approval link was tapped. Either way the place in it is handed over.
  var back = document.getElementById('back-thread');
  if (back) {
    var backId = back.getAttribute('data-thread');
    var noted = sendsReturn(backId);
    if (noted && typeof noted.href === 'string' && noted.href.charAt(0) === '/' &&
        noted.href.charAt(1) !== '/') back.href = noted.href;
    back.addEventListener('click', function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      primeReturn(backId);
      var o = originEntry();
      if (!o || threadOf(o.url) !== backId) return;
      e.preventDefault();
      var t = window.navigation.traverseTo(o.entry.key);
      if (t && t.committed) { t.committed.catch(function () { location.href = back.href; }); }
    });
  }

  // The approval card.
  function lockButtons(activeLabel) {
    ['btn-approve', 'btn-reject'].forEach(function (id) {
      var btn = document.getElementById(id);
      if (!btn) return;
      btn.disabled = true;
      btn.style.opacity = '0.5';
      btn.style.cursor = 'not-allowed';
    });
    var skip = document.getElementById('btn-skip');
    if (skip) { skip.style.pointerEvents = 'none'; skip.style.opacity = '0.5'; }
    var active = document.getElementById(activeLabel);
    if (active) { active.textContent = 'Processing...'; }
  }
  var fa = document.getElementById('form-approve');
  if (fa) { fa.addEventListener('submit', function () { lockButtons('btn-approve'); }); }
  var fr = document.getElementById('form-reject');
  if (fr) { fr.addEventListener('submit', function () { lockButtons('btn-reject'); }); }

  // The status page. A "sending" entry polls the JSON status endpoint rather
  // than reloading the page; success shows the check and moves on a moment
  // later (to the next pending request, else the way out above); a failure
  // stays put, so its error can be read.
  //
  // The status document is {status, error?, outcome_unknown?, note?, next?}.
  // An "error" whose `outcome_unknown` is true is no failure: the mail
  // server was handed the mail and its answer was lost, so nobody knows
  // whether it went out. The page says exactly that — "not confirmed, it may
  // have gone out" — with a question mark, never the cross and never the
  // "could not deliver" line, which would have the reader send it again.
  // `note`, where the gateway sends one, is its own sentence for the outcome
  // and is shown in place of the page's (a mail sent, but not to everyone).
  var row = document.getElementById('st-row');
  if (!row || !row.hasAttribute('data-status')) return;
  var status = row.getAttribute('data-status');
  var nextUrl = row.getAttribute('data-next') || null;
  var pollUrl = row.getAttribute('data-poll') || null;
  var noteDone = row.getAttribute('data-note-done') || '';
  var noteRejected = row.getAttribute('data-note-rejected') || '';
  var noteRetracted = row.getAttribute('data-note-retracted') || '';
  var noteError = row.getAttribute('data-note-error') || '';
  var noteUnknown = row.getAttribute('data-note-unknown') ||
    'Not confirmed — it may have gone out. Check with the recipient or the Sent folder before it is sent again.';
  var icon = document.getElementById('st-icon');
  var note = document.getElementById('st-note');
  var nextBtn = document.getElementById('st-next');

  function showNext() { if (nextUrl && nextBtn) { nextBtn.href = nextUrl; nextBtn.style.display = ''; } }
  function advance() {
    if (nextUrl) { location = nextUrl; return; }
    leave();
  }
  // A sentence from the status document, as text of a line or two; '' for
  // anything that is not one.
  function sentence(v) {
    return typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, 500) : '';
  }
  // The mark of an outcome nobody knows: a question mark in a neutral disc,
  // shaped like the check and the cross. Built by DOM calls and styled
  // through its style object — the page's stylesheet knows only those two,
  // and its Content-Security-Policy takes no style from markup.
  function unsureMark() {
    var d = document.createElement('div');
    d.className = 'unsure';
    d.setAttribute('role', 'img');
    d.setAttribute('aria-label', 'not confirmed');
    d.textContent = '?';
    var s = d.style;
    s.width = '38px'; s.height = '38px'; s.borderRadius = '50%'; s.display = 'flex';
    s.alignItems = 'center'; s.justifyContent = 'center'; s.fontSize = '1.35rem'; s.fontWeight = '700';
    s.border = '2px solid var(--muted)'; s.boxSizing = 'border-box';
    return d;
  }
  function terminal(st, err, doc) {
    var own = sentence(doc && doc.note);
    if (st === 'approved') {
      icon.innerHTML = '<div class="check">✓</div>';
      note.textContent = own || noteDone;
      setTimeout(advance, 1500);
    } else if (st === 'rejected') {
      icon.innerHTML = '<div class="cross">✕</div>';
      note.textContent = noteRejected;
      showNext();
    } else if (st === 'retracted') {
      icon.innerHTML = '<div class="cross">✕</div>';
      note.textContent = noteRetracted;
      showNext();
    } else if (st === 'error' && doc && doc.outcome_unknown === true) {
      icon.textContent = '';
      icon.appendChild(unsureMark());
      note.textContent = own || (noteUnknown + (sentence(err) ? ' ' + sentence(err) : ''));
      showNext();
    } else {
      icon.innerHTML = '<div class="cross">✕</div>';
      note.textContent = noteError + (err || 'unknown error');
      showNext();
    }
  }
  function poll() {
    fetch(pollUrl, { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (b) {
      if (b.status && b.status !== 'sending' && b.status !== 'pending') {
        if ('next' in b) { nextUrl = b.next; }
        terminal(b.status, b.error, b);
      } else { setTimeout(poll, 1500); }
    }).catch(function () { setTimeout(poll, 3000); });
  }
  if (status === 'sending') { setTimeout(poll, 1200); }
  else if (status === 'approved') { setTimeout(advance, 1500); }
  else { showNext(); }
})();
