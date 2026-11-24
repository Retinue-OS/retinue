/* window.Retinue — the client a chamber app uses for Retinue's services.
 *
 * A chamber app (docs/chamber-apps.md) is a Progressive Web App a chamber
 * ships in <chamber>/.retinue/apps/<app>/. Served by the dashboard at
 * /apps/<chamber>/<app>/, it loads this file:
 *
 *   <script src="/apps/retinue.js"></script>
 *   <script type="module" src="./app.js"></script>
 *
 * and feature-detects `window.Retinue`. Elsewhere (a static copy, a file, a
 * dev server) this file does not load, `window.Retinue` is absent, and the
 * app runs standalone. The object talks to a small, stable HTTP API under
 * /apps/api/, never to the dashboard's internals, so another host — a
 * browser extension, later — can supply the same object unchanged.
 *
 *   Retinue.version                 this client's API version (1)
 *   Retinue.app                     {chamber, id} of the running app
 *   Retinue.shell                   {version} inside the Android shell, else null
 *   Retinue.sparql(query, endpoint = "life")         → SPARQL JSON results
 *   Retinue.submit(name, blob, dir?, {replace}?)     → {path, bytes}
 *   Retinue.ask(agent, text, {thread, title, importance, due, …}?) → thread id
 *   Retinue.schedule(job) · unschedule(id) · jobs()
 *   Retinue.done(id) · skip(id) · snooze(id, minutes?)
 *   Retinue.speak(text, lang?)      → resolves when spoken (or not possible)
 *   Retinue.notify(title, body?)    an immediate notification while the app is open
 *
 * Every call returns a Promise; an HTTP error rejects with an Error whose
 * `status` is the code and `message` the server's reason.
 *
 * A classic script, not a module: the object exists as soon as the tag has
 * run, before the app's own module looks for it.
 */
(function () {
  'use strict';
  if (window.Retinue) return;   // another host supplied it first
  var m = location.pathname.match(/^\/apps\/([^/]+)\/([^/]+)\//);
  if (!m) return;               // not an app page of this dashboard
  var app = Object.freeze({ chamber: decodeURIComponent(m[1]), id: decodeURIComponent(m[2]) });
  var API = '/apps/api';
  var enc = encodeURIComponent;
  var REMINDER_PARAM = 'retinue-reminder';

  function shellBridge() {
    var s = window.RetinueShell;
    return s && typeof s === 'object' ? s : null;
  }
  function shellHas(name) {
    var s = shellBridge();
    return !!(s && typeof s[name] === 'function');
  }

  function request(method, path, body, headers) {
    var init = { method: method, credentials: 'same-origin', headers: headers || {} };
    if (body !== undefined) {
      if (body instanceof Blob || body instanceof ArrayBuffer || typeof body === 'string') {
        init.body = body;
      } else {
        init.body = JSON.stringify(body);
        init.headers['Content-Type'] = 'application/json';
      }
    }
    return fetch(API + path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
        if (!res.ok) {
          var err = new Error((data && data.error) || ('HTTP ' + res.status));
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  // The Android shell arms reminders as local alarms; it hears about a
  // changed plan at once rather than on its next fetch.
  function remindersChanged(result) {
    if (shellHas('remindersChanged')) {
      try { window.RetinueShell.remindersChanged(); } catch (_) { /* the plan is on the server anyway */ }
    }
    return result;
  }

  function schedulePath(id) {
    var p = '/schedule/' + enc(app.chamber) + '/' + enc(app.id);
    return id === undefined ? p : p + '/' + enc(id);
  }

  function checkId(id) {
    if (typeof id !== 'string' || !id) throw new TypeError('a job id is a non-empty string');
    return id;
  }

  var speakSeq = 0;

  var Retinue = {
    version: 1,
    app: app,
    get shell() {
      var s = shellBridge();
      return s ? { version: Number(s.version) || 0, platform: s.platform || null } : null;
    },

    sparql: function (query, endpoint) {
      return request('POST', '/sparql', { query: String(query), endpoint: endpoint || 'life' });
    },

    submit: function (name, blob, dir, opts) {
      if (typeof name !== 'string' || !name) return Promise.reject(new TypeError('name the file'));
      var segs = [enc(app.chamber)];
      if (dir) String(dir).split('/').filter(Boolean).forEach(function (s) { segs.push(enc(s)); });
      segs.push(enc(name));
      var q = opts && opts.replace ? '?replace=1' : '';
      var body = blob instanceof Blob ? blob : new Blob([typeof blob === 'string' ? blob : JSON.stringify(blob)]);
      return request('PUT', '/files/' + segs.join('/') + q, body,
        { 'Content-Type': body.type || 'application/octet-stream' });
    },

    ask: function (agent, text, opts) {
      var body = Object.assign({}, opts || {}, { agent: agent, text: text, app: app });
      return request('POST', '/ask', body).then(function (r) { return r.thread; });
    },

    schedule: function (job) {
      return request('POST', schedulePath(), job).then(remindersChanged);
    },
    unschedule: function (id) {
      return request('DELETE', schedulePath(checkId(id))).then(remindersChanged);
    },
    jobs: function () {
      return request('GET', schedulePath()).then(function (r) { return r.jobs; });
    },
    done: function (id) {
      return request('POST', schedulePath(checkId(id)) + '/done', {}).then(remindersChanged);
    },
    skip: function (id) {
      return request('POST', schedulePath(checkId(id)) + '/skip', {}).then(remindersChanged);
    },
    snooze: function (id, minutes) {
      return request('POST', schedulePath(checkId(id)) + '/snooze',
        minutes ? { minutes: minutes } : {}).then(remindersChanged);
    },

    speak: function (text, lang) {
      text = String(text || '');
      if (!text) return Promise.resolve(false);
      if (shellHas('speak')) {
        // The shell's native text-to-speech answers with a retinue-shell
        // event {type: "speak", id, state: "done" | "error"}.
        var id = 'speak-' + (++speakSeq);
        return new Promise(function (resolve) {
          var timer = setTimeout(finish.bind(null, false), Math.min(120000, 4000 + text.length * 120));
          function finish(ok) {
            clearTimeout(timer);
            window.removeEventListener('retinue-shell', onEvent);
            resolve(ok);
          }
          function onEvent(e) {
            var d = e.detail || {};
            if (d.type === 'speak' && d.id === id) finish(d.state === 'done');
          }
          window.addEventListener('retinue-shell', onEvent);
          try { window.RetinueShell.speak(text, lang || '', id); } catch (_) { finish(false); }
        });
      }
      if (!('speechSynthesis' in window)) return Promise.resolve(false);
      return new Promise(function (resolve) {
        var u = new SpeechSynthesisUtterance(text);
        if (lang) u.lang = lang;
        u.onend = function () { resolve(true); };
        u.onerror = function () { resolve(false); };
        window.speechSynthesis.speak(u);
      });
    },

    notify: function (title, body) {
      title = String(title || '');
      body = body == null ? '' : String(body);
      if (shellHas('notify')) {
        try { window.RetinueShell.notify(title, body); return Promise.resolve(true); } catch (_) { /* fall through */ }
      }
      if (!('Notification' in window)) return Promise.resolve(false);
      var ask = Notification.permission === 'default' ? Notification.requestPermission() : Promise.resolve(Notification.permission);
      return Promise.resolve(ask).then(function (perm) {
        if (perm !== 'granted') return false;
        var opts = { body: body, tag: 'app-' + app.chamber + '-' + app.id };
        if (navigator.serviceWorker && navigator.serviceWorker.getRegistration) {
          return navigator.serviceWorker.getRegistration().then(function (reg) {
            if (reg) return reg.showNotification(title, opts).then(function () { return true; });
            new Notification(title, opts);
            return true;
          });
        }
        new Notification(title, opts);
        return true;
      }).catch(function () { return false; });
    }
  };

  Object.defineProperty(window, 'Retinue', { value: Object.freeze(Retinue), configurable: false });

  // Opened from a reminder (its notification, the home list, the shell):
  // the item is cleared, and the marker leaves the address so a reload or a
  // bookmark does not report it again.
  try {
    var url = new URL(location.href);
    var rid = url.searchParams.get(REMINDER_PARAM);
    if (rid) {
      url.searchParams.delete(REMINDER_PARAM);
      history.replaceState(history.state, '', url.pathname + url.search + url.hash);
      request('POST', schedulePath(rid) + '/opened', {}).catch(function () { /* the item stays listed */ });
    }
  } catch (_) { /* an old engine without URL: nothing to report */ }
})();
