// Routine timer — a generic example of a chamber app (docs/chamber-apps.md).
//
// It runs a timed routine from routine.json, speaks each step, and logs
// every session. Under Retinue (window.Retinue present) it speaks through the
// host, files each session as a JSON file in its chamber's inbox, and keeps a
// daily reminder the server fires even while the app is closed. Standalone it
// does the same with the browser's own speech, keeps the log on the device,
// and says that it cannot remind you while it is closed.

const R = window.Retinue || null;
const REMINDER_ID = 'daily';
const LOG_KEY = 'routine-log';
const $ = (id) => document.getElementById(id);

let routine = null;
let running = null;   // {index, left, timer, started}

function fmt(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function speak(text) {
  if (R) return R.speak(text, routine && routine.lang);
  if ('speechSynthesis' in window) {
    const u = new SpeechSynthesisUtterance(text);
    if (routine && routine.lang) u.lang = routine.lang;
    speechSynthesis.speak(u);
  }
  return Promise.resolve();
}

function localLog() {
  try { return JSON.parse(localStorage.getItem(LOG_KEY) || '[]'); } catch (_) { return []; }
}

function renderLog() {
  const list = $('log');
  list.textContent = '';
  for (const entry of localLog().slice(-10).reverse()) {
    const li = document.createElement('li');
    li.textContent = `${new Date(entry.ended).toLocaleString()} — ${entry.outcome}` +
      (entry.filed ? ' (filed)' : '');
    list.append(li);
  }
}

async function record(outcome) {
  const entry = { routine: routine.id, started: running && running.started,
                  ended: new Date().toISOString(), outcome, steps: routine.steps.length };
  if (R) {
    const name = `session-${entry.ended.replace(/[:.]/g, '-')}.json`;
    try {
      await R.submit(name, new Blob([JSON.stringify(entry, null, 2)], { type: 'application/json' }));
      entry.filed = true;
    } catch (err) {
      entry.filed = false;
      console.warn('session not filed:', err.message);
    }
    // A finished session settles today's reminder; a reminder already on the
    // home list leaves it.
    if (outcome === 'done') R.done(REMINDER_ID).catch(() => {});
  }
  const log = localLog();
  log.push(entry);
  try { localStorage.setItem(LOG_KEY, JSON.stringify(log.slice(-200))); } catch (_) { /* storage full */ }
  renderLog();
}

function showStep() {
  const step = routine.steps[running.index];
  $('step').textContent = step.say;
  $('clock').textContent = fmt(running.left);
}

function tick() {
  running.left -= 1;
  if (running.left === 3 && running.index + 1 < routine.steps.length) {
    // A heads-up while the app is open: not a reminder.
    if (R) R.notify(routine.title, `Next: ${routine.steps[running.index + 1].say}`);
  }
  if (running.left > 0) {
    $('clock').textContent = fmt(running.left);
    return;
  }
  running.index += 1;
  if (running.index >= routine.steps.length) {
    stop('done');
    return;
  }
  running.left = routine.steps[running.index].seconds;
  showStep();
  speak(routine.steps[running.index].say);
}

function start() {
  running = { index: 0, left: routine.steps[0].seconds, started: new Date().toISOString() };
  showStep();
  speak(routine.steps[0].say);
  running.timer = setInterval(tick, 1000);
  $('start').disabled = true;
  $('stop').disabled = false;
}

function stop(outcome) {
  if (!running) return;
  clearInterval(running.timer);
  record(outcome);
  running = null;
  $('step').textContent = outcome === 'done' ? 'Done. Well done.' : 'Stopped.';
  $('clock').textContent = '–';
  $('start').disabled = false;
  $('stop').disabled = true;
  if (outcome === 'done') speak('Done.');
}

async function showReminder() {
  const note = $('reminder-note');
  if (!R) {
    note.textContent = 'Reminders need Retinue: on its own, this app can only remind you while it is open.';
    for (const id of ['reminder-set', 'reminder-clear', 'skip-today']) $(id).disabled = true;
    return;
  }
  try {
    const job = (await R.jobs()).find((j) => j.id === REMINDER_ID);
    if (job) {
      $('reminder-at').value = job.at;
      note.textContent = `Every day at ${job.at}` + (job.next ? ` — next ${new Date(job.next).toLocaleString()}` : '');
    } else {
      note.textContent = 'No reminder set.';
    }
  } catch (err) {
    note.textContent = `Reminders unavailable: ${err.message}`;
  }
}

async function setReminder() {
  await R.schedule({
    kind: 'reminder', id: REMINDER_ID,
    at: $('reminder-at').value, days: 'mon-sun',
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    title: routine.title, body: `${routine.steps.length} steps — tap to start`,
    open: '#start', importance: 3, lead: '30m',
  });
  showReminder();
}

async function init() {
  routine = await (await fetch('routine.json')).json();
  $('title').textContent = routine.title;
  $('host').textContent = R ? `Running under Retinue (${R.app.chamber}/${R.app.id})` : 'Running on its own';
  $('start').addEventListener('click', start);
  $('stop').addEventListener('click', () => stop('stopped'));
  $('reminder-set').addEventListener('click', () => setReminder().catch((e) => { $('reminder-note').textContent = e.message; }));
  $('reminder-clear').addEventListener('click', () => R.unschedule(REMINDER_ID).catch(() => {}).then(showReminder));
  $('skip-today').addEventListener('click', () => {
    if (R) R.skip(REMINDER_ID).then(() => { $('reminder-note').textContent = 'Skipped for today.'; }).catch(() => {});
    record('skipped');
  });
  renderLog();
  showReminder();
  // Opened from the reminder (open: "#start"): offer to begin at once.
  if (location.hash === '#start') $('step').textContent = 'Time for your routine — press Start.';
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
}

init();
