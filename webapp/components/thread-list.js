// Thread list widget: a set of conversation summaries shown under two tabs,
// Active and Archived — the same switch the chats and conversations pages
// carry, so an archived discussion is one tap away instead of folded out of
// sight.
//
// It is markup, styles and wiring for a host component's shadow root rather
// than an element of its own: the host owns the data and re-renders freely,
// and the widget only needs somewhere to keep the selected tab (`state`).
//
//   threadListHtml(threads, state)  -> the widget's HTML ('' when no threads)
//   wireThreadList(root, state)     -> tab presses switch the panel in place
//   THREAD_LIST_CSS                 -> styles for the host's stylesheet
//
// `state.tab` is 'active' | 'archived' once the user has chosen; until then
// the widget opens on Active, or on Archived when nothing is active.
import { esc, fmtAge } from './base.js';

const TABS = [['active', 'Active'], ['archived', 'Archived']];

function split(threads) {
  const list = Array.isArray(threads) ? threads : [];
  return {
    active: list.filter((t) => !t.archived),
    archived: list.filter((t) => t.archived),
  };
}

// The tab to show: the user's choice, else the one that has something in it.
export function threadListTab(threads, state) {
  const chosen = state && state.tab;
  if (chosen === 'active' || chosen === 'archived') return chosen;
  const { active, archived } = split(threads);
  return !active.length && archived.length ? 'archived' : 'active';
}

function rowHtml(t) {
  const age = fmtAge(t.updated);
  const unread = t.unread ? '<span class="dot" aria-label="Unread"></span>' : '';
  return `<a class="thread-row" href="/conversations.html#conversation-${esc(t.id)}">`
    + `${unread}<span class="t-title">${esc(t.title || 'Conversation')}</span>`
    + (age ? `<span class="t-age">${esc(age)}</span>` : '')
    + `</a>`;
}

export function threadListHtml(threads, state, heading = 'Threads') {
  const groups = split(threads);
  if (!groups.active.length && !groups.archived.length) return '';
  const current = threadListTab(threads, state);
  const tabs = TABS.map(([tab, label]) => {
    const on = tab === current;
    return `<button class="thread-tab${on ? ' on' : ''}" role="tab" `
      + `aria-selected="${on}" data-thread-tab="${tab}">${label} `
      + `<span class="thread-count">${groups[tab].length}</span></button>`;
  }).join('');
  const panels = TABS.map(([tab]) => {
    const rows = groups[tab];
    const inner = rows.length
      ? rows.map(rowHtml).join('')
      : `<p class="thread-empty">No ${tab} threads.</p>`;
    return `<div class="thread-rows" role="tabpanel" data-thread-panel="${tab}"`
      + `${tab === current ? '' : ' hidden'}>${inner}</div>`;
  }).join('');
  return `<div class="threads"><div class="threads-head">${esc(heading)}</div>`
    + `<div class="thread-tabs" role="tablist" aria-label="${esc(heading)}">${tabs}</div>`
    + panels + `</div>`;
}

// Both panels are in the DOM, so a tab press only flips which one shows — the
// host is not re-rendered and whatever the user was typing stays put.
export function wireThreadList(root, state) {
  const tabs = Array.from(root.querySelectorAll('[data-thread-tab]'));
  const panels = Array.from(root.querySelectorAll('[data-thread-panel]'));
  tabs.forEach((el) => el.addEventListener('click', () => {
    const tab = el.getAttribute('data-thread-tab');
    state.tab = tab;
    tabs.forEach((t) => {
      const on = t.getAttribute('data-thread-tab') === tab;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', String(on));
    });
    panels.forEach((p) => { p.hidden = p.getAttribute('data-thread-panel') !== tab; });
  }));
}

export const THREAD_LIST_CSS = `
  .threads { display: flex; flex-direction: column; gap: 6px; padding: 10px 0 2px; }
  .threads-head { font-size: .72rem; font-weight: 600; color: var(--muted, #8b93a3);
                  text-transform: uppercase; letter-spacing: .04em; }
  .thread-tabs { display: flex; background: var(--card-2, #1c2230); border-radius: 12px;
                 padding: 3px; }
  .thread-tab { flex: 1; background: transparent; border: 0; border-radius: 9px; padding: 6px;
                color: var(--muted, #8b93a3); cursor: pointer; font: inherit; font-size: .82rem;
                -webkit-tap-highlight-color: transparent; }
  .thread-tab.on { background: var(--accent, #6ea8fe); color: #0b0d12; font-weight: 600; }
  .thread-count { opacity: .75; font-weight: 400; }
  .thread-rows { display: flex; flex-direction: column; gap: 4px; }
  .thread-rows[hidden] { display: none; }
  .thread-empty { margin: 0; padding: 7px 12px; color: var(--muted, #8b93a3); font-size: .82rem; }
  .thread-row { display: flex; align-items: center; gap: 8px; padding: 7px 12px;
                border-radius: 12px; background: var(--card-2, #1c2230); text-decoration: none;
                color: var(--fg, #e7ebf2); font-size: .86rem;
                -webkit-tap-highlight-color: transparent; }
  .thread-row:hover { outline: 1px solid var(--accent, #6ea8fe); }
  .t-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .t-age { flex: none; color: var(--muted, #8b93a3); font-size: .72rem; }
  .dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--accent, #6ea8fe); }
`;
