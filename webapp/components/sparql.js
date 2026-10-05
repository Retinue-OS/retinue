// The life store, readable and runnable: the SPARQL page (sparql.html, served
// at /sparql when a browser asks the endpoint itself).
//
// Two light-DOM elements over YASGUI:
//
//   <retinue-sparql-doc>        renders a framework doc (docs/ontology.md by
//                               default, ?doc=<name> for another) with the
//                               dashboard's own Markdown renderer and makes
//                               every ```sparql block in it runnable. Run turns
//                               that block, where it stands, into an editor
//                               (YASQE) with its results (YASR) beneath it. The
//                               documentation IS the example collection, so the
//                               two cannot drift apart.
//   <retinue-sparql-workbench>  a full YASGUI — tabs, kept per device — for
//                               queries of one's own.
//
// Both query /sparql, the gateway's read-only door to the life store behind
// the dashboard's sign-in. Light DOM, because YASGUI styles itself with a
// global stylesheet that a shadow root would keep out.
//
// YASGUI is vendored at image build (scripts/vendor-yasgui.sh) and loaded on
// first use, so reading the page costs nothing. Nothing here talks to any
// origin but this one. YASQE's stock autocompleters would: prefix.cc for
// prefixes, and the LOV API, with what is being typed, for classes and
// properties. They are replaced by one that knows the prefixes the ontology's
// own examples declare, so typing `sosa:` declares `PREFIX sosa: <…>` from the
// documentation rather than from the web. And no result is ever kept in the
// browser's storage, only the workbench's queries.

import { esc } from './base.js';
import { renderMarkdown, MD_CSS } from './markdown.js';

const ENDPOINT = '/sparql';
const VENDOR = '/vendor/yasgui';
const ONTOLOGY = 'ontology';
const DOC_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
// A doc named in running text — `docs/triple-stores.md` — links to its own
// rendering here, so the docs read as one connected set.
const DOC_MENTION_RE = /^(?:\/workspace\/)?docs\/([A-Za-z0-9][A-Za-z0-9_-]*)\.md$/;
const SPARQL_FENCE_RE = /^```sparql[^\n]*\n([\s\S]*?)^```/gm;
const PREFIX_RE = /^\s*PREFIX\s+([A-Za-z][\w.-]*)?:\s*<([^>\s]*)>/gim;

// The store answers JSON for SELECT/ASK and Turtle for CONSTRUCT/DESCRIBE.
const REQUEST = {
  endpoint: ENDPOINT,
  method: 'POST',
  acceptHeaderSelect: 'application/sparql-results+json,*/*;q=0.9',
  acceptHeaderGraph: 'text/turtle,*/*;q=0.9',
};

// ── Pure helpers (exported for tests) ────────────────────────────────────────

export function docUrl(name) {
  return `/docs/${DOC_NAME_RE.test(name || '') ? name : ONTOLOGY}.md`;
}

// Every ```sparql block of a Markdown document, in order.
export function sparqlBlocks(markdown) {
  return [...String(markdown || '').matchAll(SPARQL_FENCE_RE)].map((m) => m[1].replace(/\n$/, ''));
}

// {prefix: namespace} as the document's queries declare them.
export function prefixesOf(markdown) {
  const out = {};
  for (const block of sparqlBlocks(markdown)) {
    for (const m of block.matchAll(PREFIX_RE)) out[m[1] || ''] = m[2];
  }
  return out;
}

// ── Loading YASGUI ───────────────────────────────────────────────────────────

let yasguiPromise = null;
let prefixesPromise = null;

// The namespaces of docs/ontology.md's examples: the prefix completions, and
// how result IRIs are abbreviated, whichever doc is on screen.
function ontologyPrefixes() {
  if (!prefixesPromise) {
    prefixesPromise = fetch(docUrl(ONTOLOGY))
      .then((r) => (r.ok ? r.text() : ''))
      .then(prefixesOf)
      .catch(() => ({}));
  }
  return prefixesPromise;
}

function loadYasgui() {
  if (yasguiPromise) return yasguiPromise;
  yasguiPromise = new Promise((resolve, reject) => {
    if (!document.querySelector(`link[href="${VENDOR}/yasgui.min.css"]`)) {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = `${VENDOR}/yasgui.min.css`;
      document.head.append(css);
    }
    const js = document.createElement('script');
    js.src = `${VENDOR}/yasgui.min.js`;
    js.onload = () => (window.Yasgui ? resolve(window.Yasgui) : reject(new Error('YASGUI did not load')));
    js.onerror = () => reject(new Error('YASGUI is not installed'));
    document.head.append(js);
  }).then((Yasgui) => {
    const { Yasqe } = Yasgui;
    Yasqe.forkAutocompleter('prefixes', {
      name: 'ontology-prefixes',
      persistenceId: null,
      get: () => ontologyPrefixes().then((p) =>
        Object.entries(p).map(([prefix, ns]) => `${prefix}: <${ns}>`).sort()),
    }, false);
    // Replaced, not merged: Yasgui deep-merges its config into these
    // defaults, and a merged array keeps the stock entries it should drop.
    Yasqe.defaults.autocompleters = ['ontology-prefixes', 'variables'];
    return Yasgui;
  });
  yasguiPromise.catch(() => { yasguiPromise = null; });
  return yasguiPromise;
}

function injectStyle(id, css) {
  if (document.getElementById(id)) return;
  const style = document.createElement('style');
  style.id = id;
  style.textContent = css;
  document.head.append(style);
}

// ── <retinue-sparql-doc> ─────────────────────────────────────────────────────

class RetinueSparqlDoc extends HTMLElement {
  connectedCallback() {
    if (this.dataset.ready) return;
    this.dataset.ready = '1';
    injectStyle('retinue-md-css', MD_CSS);
    this.addEventListener('click', (e) => {
      const run = e.target.closest('.sparql-run');
      if (run && this.contains(run)) this.run(run.closest('.sparql-example'));
    });
    const name = new URLSearchParams(location.search).get('doc') || ONTOLOGY;
    this.load(this.getAttribute('src') || docUrl(name));
  }

  async load(src) {
    this.innerHTML = '<p class="sparql-note">Loading…</p>';
    try {
      const res = await fetch(src);
      if (!res.ok) throw new Error(`${res.status}`);
      this.render(await res.text());
    } catch (err) {
      this.innerHTML = `<p class="sparql-note">Could not load ${esc(src)} (${esc(err.message)}).</p>`;
    }
    // Only now does the page have its height. The workbench waits for this
    // to measure where it is, and a jump the URL asked for (#workbench) was
    // made against the 'Loading…' placeholder, so it is made again.
    this.toggleAttribute('loaded', true);
    this.dispatchEvent(new Event('retinue-doc-loaded'));
    const target = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
    if (target) target.scrollIntoView();
  }

  render(text) {
    this.queries = [];
    this.innerHTML = renderMarkdown(text, {
      hardWrapped: true,
      code: (raw, lang, inner) => {
        if (lang.toLowerCase() !== 'sparql') return inner;
        const i = this.queries.push(raw) - 1;
        return `<figure class="sparql-example" data-example="${i}">${inner}` +
          '<button type="button" class="sparql-run" title="Run this query on the life store">' +
          'Run</button></figure>';
      },
    });
    for (const code of this.querySelectorAll('.md :not(pre) > code')) {
      const m = DOC_MENTION_RE.exec(code.textContent);
      if (!m) continue;
      const a = document.createElement('a');
      a.href = `?doc=${encodeURIComponent(m[1])}`;
      code.replaceWith(a);
      a.append(code);
    }
    const title = this.querySelector('h1');
    if (title) document.title = `Retinue — ${title.textContent}`;
  }

  // The example, in place: the code block gives way to an editor holding the
  // same query, and the results open beneath it. Running again, or after an
  // edit, is the editor's own ▶ (or Ctrl/Cmd-Enter).
  async run(figure) {
    if (!figure || figure.classList.contains('live')) return;
    const button = figure.querySelector('.sparql-run');
    const query = this.queries[Number(figure.dataset.example)];
    button.disabled = true;
    button.textContent = 'Loading…';
    let Yasgui;
    try {
      Yasgui = await loadYasgui();
    } catch (err) {
      button.textContent = 'Unavailable';
      button.title = `${err.message} in this build (scripts/vendor-yasgui.sh)`;
      return;
    }
    const prefixes = await ontologyPrefixes();
    figure.classList.add('live');
    const editor = document.createElement('div');
    const results = document.createElement('div');
    figure.replaceChildren(editor, results);
    const yasqe = new Yasgui.Yasqe(editor, {
      value: query,
      persistenceId: null,
      createShareableLink: null,
      consumeShareLink: null,
      // As tall as the query, wrapped lines included, like the block it
      // replaces (CodeMirror's auto-height; the resize handle still works).
      editorHeight: 'auto',
      viewportMargin: Infinity,
      requestConfig: REQUEST,
    });
    const yasr = new Yasgui.Yasr(results, {
      persistenceId: null,
      prefixes: () => ({ ...prefixes, ...yasqe.getPrefixesFromQuery() }),
    });
    yasqe.on('queryResponse', (_yasqe, response, duration) => yasr.setResponse(response, duration));
    yasqe.query().catch(() => {});
  }
}

// ── <retinue-sparql-workbench> ───────────────────────────────────────────────

const WORKBENCH_QUERY = `# Your own query. Typing a prefix the ontology uses (sosa:, vcard:, kb:, …)
# declares it; Ctrl/Cmd-Enter runs.
SELECT * WHERE {
  ?s ?p ?o
} LIMIT 10`;

// A shared link — what the workbench's share button makes, /sparql#query=… —
// as URL parameters; the page's own anchors (#workbench) carry no query.
const sharedLink = () => new URLSearchParams(location.hash.slice(1));

class RetinueSparqlWorkbench extends HTMLElement {
  connectedCallback() {
    if (this.observer || this.yasgui) return;
    // YASGUI is a megabyte; it loads when the workbench comes into view.
    this.observer = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      this.observer.disconnect();
      this.start();
    }, { rootMargin: '200px' });
    // Watched only once the doc above has its height: before that the
    // workbench sits on the first screen, behind the doc's 'Loading…', and
    // would load YASGUI on every visit. A shared link is the exception that
    // brings it into view, since its query is what the visit is for.
    const watch = () => {
      if (!this.observer) return;
      if (sharedLink().get('query')) (this.closest('section') || this).scrollIntoView();
      this.observer.observe(this);
    };
    const doc = document.querySelector('retinue-sparql-doc');
    if (doc && !doc.hasAttribute('loaded')) doc.addEventListener('retinue-doc-loaded', watch, { once: true });
    else watch();
  }

  disconnectedCallback() {
    if (this.observer) this.observer.disconnect();
    this.observer = null;
  }

  async start() {
    let Yasgui;
    try {
      Yasgui = await loadYasgui();
    } catch (err) {
      this.innerHTML = `<p class="sparql-note">${esc(err.message)} in this build ` +
        '(scripts/vendor-yasgui.sh): the queries above can still be copied.</p>';
      return;
    }
    const prefixes = await ontologyPrefixes();
    const link = sharedLink();
    this.yasgui = new Yasgui(this, {
      requestConfig: REQUEST,
      endpointCatalogueOptions: { getData: () => [{ endpoint: ENDPOINT }] },
      // YASGUI does not read the URL itself: it would write whatever a link
      // names (endpoint, headers, arguments) into the defaults every later
      // tab starts from, query or no query. The link is read below instead.
      populateFromUrl: false,
      persistenceId: 'retinue-sparql-workbench',
      autofocus: false,
      yasqe: { value: WORKBENCH_QUERY },
      // Queries are kept per device; results never are.
      yasr: { maxPersistentResponseSize: 0, prefixes },
    });
    // Whatever a stored tab says, every tab asks this store with nothing but
    // its query and the dataset the query itself names.
    for (const id of this.yasgui.persistentConfig.getTabs()) {
      const tab = this.yasgui.getTab(id);
      if (tab) {
        tab.setRequestConfig({ endpoint: ENDPOINT, headers: {}, args: [],
                               namedGraphs: [], defaultGraphs: [] });
      }
    }
    // A shared link opens as a tab of its own, run at once, and brings
    // nothing but its query (and the tab's name).
    const query = link.get('query');
    if (query) {
      history.replaceState(null, '', location.pathname + location.search);
      // The same link opened again selects its tab rather than adding one.
      // (Not YASGUI's avoidDuplicateTabs: its selectTabId hands back the tab
      // that was active before, so the wrong query would run.)
      const known = this.yasgui.persistentConfig.getTabs()
        .find((id) => this.yasgui.persistentConfig.getTab(id)?.yasqe?.value === query);
      if (known) this.yasgui.selectTabId(known);
      const tab = known ? this.yasgui.getTab(known)
        : this.yasgui.addTab(true, { name: link.get('tabTitle') || undefined, yasqe: { value: query } });
      tab.query().catch(() => {});
    }
  }
}

customElements.define('retinue-sparql-doc', RetinueSparqlDoc);
customElements.define('retinue-sparql-workbench', RetinueSparqlWorkbench);
