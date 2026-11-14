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
//                                 and move on
//
// A classic script at the end of the body, where the inline scripts stood,
// so everything it looks for is already there. Written for old WebViews
// too, like the inline scripts were: no modules, no arrow functions.
(function () {
  // The way out of the /sends pages once nothing is left to decide: back to
  // the page the approval link was tapped on. The dashboard opens it in place
  // (#359), so that is the latest same-origin history entry that is not a
  // /sends page (Navigation API), however many approval and status pages
  // followed it. A page opened on its own (a link from another app, another
  // site before it in the tab) has none, so it closes; where the browser
  // refuses that, the dashboard, never the empty /sends list.
  // The place in the thread the approval link was tapped at. The thread
  // writes it as the link is tapped (conversation.js, _noteSendsReturn: the
  // thread id, its shelfLocation() and the page it was open on); a way back
  // hands it to the thread page in the shelf's restore format (shelf-store.js
  // setRestore), so the thread opens at that exact message whether the
  // browser brings the page back from its cache or loads it afresh.
  var SENDS_RETURN_KEY = 'retinue.sends.return';
  var SHELF_RESTORE_KEY = 'retinue.shelf.restore';

  function sendsReturn(id) {
    try {
      var r = JSON.parse(sessionStorage.getItem(SENDS_RETURN_KEY) || 'null');
      return r && r.id === id ? r : null;
    } catch (e) { return null; }
  }
  function primeReturn(id) {
    var r = id && sendsReturn(id);
    if (!r || !r.location) return;
    try {
      sessionStorage.setItem(SHELF_RESTORE_KEY, JSON.stringify(
        { key: 'thread:' + id, location: r.location, at: Date.now() }));
    } catch (e) { /* the thread opens at its newest message */ }
  }
  function threadOf(u) {
    var m = /^#conversation-([0-9a-f]{32})$/.exec(u.hash || '');
    return m ? m[1] : null;
  }

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
  var row = document.getElementById('st-row');
  if (!row || !row.hasAttribute('data-status')) return;
  var status = row.getAttribute('data-status');
  var nextUrl = row.getAttribute('data-next') || null;
  var pollUrl = row.getAttribute('data-poll') || null;
  var noteDone = row.getAttribute('data-note-done') || '';
  var noteRejected = row.getAttribute('data-note-rejected') || '';
  var noteRetracted = row.getAttribute('data-note-retracted') || '';
  var noteError = row.getAttribute('data-note-error') || '';
  var icon = document.getElementById('st-icon');
  var note = document.getElementById('st-note');
  var nextBtn = document.getElementById('st-next');

  function showNext() { if (nextUrl && nextBtn) { nextBtn.href = nextUrl; nextBtn.style.display = ''; } }
  function advance() {
    if (nextUrl) { location = nextUrl; return; }
    leave();
  }
  function terminal(st, err) {
    if (st === 'approved') {
      icon.innerHTML = '<div class="check">✓</div>';
      note.textContent = noteDone;
      setTimeout(advance, 1500);
    } else if (st === 'rejected') {
      icon.innerHTML = '<div class="cross">✕</div>';
      note.textContent = noteRejected;
      showNext();
    } else if (st === 'retracted') {
      icon.innerHTML = '<div class="cross">✕</div>';
      note.textContent = noteRetracted;
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
        terminal(b.status, b.error);
      } else { setTimeout(poll, 1500); }
    }).catch(function () { setTimeout(poll, 3000); });
  }
  if (status === 'sending') { setTimeout(poll, 1200); }
  else if (status === 'approved') { setTimeout(advance, 1500); }
  else { showNext(); }
})();
