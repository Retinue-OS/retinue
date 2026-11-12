// The script of the /claude-auth page the gateway renders itself
// (scripts/web-gateway.py, _render_claude_auth_html): the two-step browser
// sign-in. Start asks the gateway for an authorize URL and shows the link to
// it; Complete hands the pasted code back, and the page either reloads or,
// when the agent container restarts, waits for the gateway to come back.
//
// It used to be written into the page. It is a file of the dashboard's own
// because the pages' Content-Security-Policy runs scripts only from such
// files (docs/dashboard.md, "The Content-Security-Policy"). A classic script
// at the end of the body, where the inline script stood; written for old
// WebViews too, as that one was.
(function () {
  var attempt = null;

  function setResult(msg, bad) {
    var el = document.getElementById('result');
    if (!el) return;
    el.textContent = msg;
    el.style.color = bad ? 'var(--high)' : 'var(--ok)';
  }

  // Grace period before polling: the container needs a moment to go down, or
  // the first poll still reaches the old gateway and reloads straight into
  // the dying one.
  function pollBack() {
    setTimeout(function () {
      setInterval(function () {
        fetch('/claude-auth/status', { cache: 'no-store' }).then(function (r) {
          if (r.ok) location.reload();
        }).catch(function () {});
      }, 3000);
    }, 8000);
  }

  var startBtn = document.getElementById('start');
  if (startBtn) startBtn.onclick = function () {
    var b = this;
    b.disabled = true;
    fetch('/claude-auth/login/start', { method: 'POST' })
      .then(function (r) { return r.json().then(function (d) { return { r: r, d: d }; }); })
      .then(function (x) {
        if (!x.r.ok) throw new Error(x.d.error || ('HTTP ' + x.r.status));
        attempt = x.d.attempt;
        document.getElementById('authlink').href = x.d.url;
        document.getElementById('flow').style.display = '';
        b.style.display = 'none';
        setResult('');
      })
      .catch(function (e) {
        setResult('Could not start the sign-in: ' + e.message, true);
        b.disabled = false;
      });
  };

  var finishBtn = document.getElementById('finish');
  if (finishBtn) finishBtn.onclick = function () {
    var b = this;
    var code = document.getElementById('code').value.trim();
    if (!code) { setResult('Paste the code first.', true); return; }
    b.disabled = true;
    fetch('/claude-auth/login/finish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ attempt: attempt, code: code,
        restart: document.getElementById('restart').checked }),
    })
      .then(function (r) { return r.json().then(function (d) { return { r: r, d: d }; }); })
      .then(function (x) {
        if (!x.r.ok || !x.d.ok) throw new Error(x.d.error || ('HTTP ' + x.r.status));
        if (x.d.restarting) {
          setResult('Signed in. Restarting the agent container — this page reconnects automatically…');
          pollBack();
        } else {
          setResult('Signed in. Reloading…');
          setTimeout(function () { location.reload(); }, 1200);
        }
      })
      .catch(function (e) { setResult('Sign-in failed: ' + e.message, true); b.disabled = false; });
  };
})();
