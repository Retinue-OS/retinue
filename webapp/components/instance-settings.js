// <retinue-instance-settings> — the settings of this instance, the same for
// every device: how it presents itself (name and accent colour, GET/POST
// /instance), which model each subagent runs on (GET/POST /agent-models), and
// the system pages (gateways, Claude sign-in, pending sends, the life store).
//
// Two pages show it: the Settings page (settings.html, its "This instance"
// section) and settings-instance.html, which the Android app embeds as the
// last section of its own settings page. The element renders into the light
// DOM, so the page's styles.css applies; it carries no state of its own and
// re-renders every card from the server's answer, so what is shown is what is
// in force. A card whose endpoint the gateway does not offer (an older one)
// stays hidden.

const TEMPLATE = `
  <section class="scard" data-instance hidden>
    <h2>Name and colour</h2>
    <p class="hint">How this Retinue presents itself, on every device: a short
      name for the home-screen icon, the page titles and the Android app's
      endpoint menu, and the accent colour of the icons and the dashboard. An
      installed home-screen icon picks up a new name or colour when the
      browser next checks the manifest, about daily.</p>
    <label class="num-setting instance-name">Name
      <input type="text" autocomplete="off" spellcheck="false" maxlength="40" data-instance-name>
    </label>
    <label class="num-setting instance-color">Accent colour
      <input type="color" data-instance-color>
    </label>
    <div class="shell-actions">
      <button type="button" class="setting-btn" data-instance-stock-color>Stock colour</button>
    </div>
    <p class="hint" data-instance-note hidden></p>
  </section>

  <section class="scard" data-agent-models hidden>
    <h2>Agent models</h2>
    <p class="hint">Which model each of Ara's subagents runs on in dashboard
      threads, messenger turns, scheduled jobs and Ask-Ara answers; a change
      applies to their next dispatch. The Claude-app session keeps the
      built-in models. "Built-in" is the model the agent's definition pins.
      Every agent calls tools — even the Secretary reads its style files —
      so pick a model that supports tool use; small local tags often do
      not.</p>
    <div data-agent-models-rows></div>
    <p class="hint" data-agent-models-note hidden></p>
  </section>

  <section class="scard">
    <h2>System</h2>
    <ul class="linklist">
      <li><a href="/gateways">Messenger gateways &mdash; connection status &amp; pairing</a></li>
      <li><a href="/claude-auth">Claude sign-in &mdash; account status &amp; browser re-login</a></li>
      <li><a href="/sends">Pending sends &mdash; messages waiting for approval</a></li>
      <li><a href="/sparql">Life store &mdash; SPARQL endpoint, ontology &amp; runnable example queries</a></li>
    </ul>
  </section>
`;

class RetinueInstanceSettings extends HTMLElement {
  connectedCallback() {
    if (this._wired) return;
    this._wired = true;
    this.innerHTML = TEMPLATE;
    this._wireInstance();
    this._wireAgentModels();
  }

  // Name and colour (GET/POST /instance): each field saves on change and the
  // card re-renders from the server's answer; the deployment default
  // (RETINUE_INSTANCE_NAME) shows as the name's placeholder.
  _wireInstance() {
    const card = this.querySelector('[data-instance]');
    const name = this.querySelector('[data-instance-name]');
    const color = this.querySelector('[data-instance-color]');
    const stock = this.querySelector('[data-instance-stock-color]');
    const note = this.querySelector('[data-instance-note]');
    let stockColor = '#6ea8fe';
    // A colour is stored (the Stock colour button has something to clear).
    let storedColor = false;
    const render = (data) => {
      card.hidden = false;
      stockColor = data.stock_color || stockColor;
      if (document.activeElement !== name) name.value = data.name || '';
      name.placeholder = (data.defaults && data.defaults.name) || 'Retinue';
      if (Number.isFinite(data.max_name_length)) name.maxLength = data.max_name_length;
      color.value = (data.effective && data.effective.color) || stockColor;
      storedColor = !!data.color;
      stock.disabled = !storedColor;
      this._changed();
    };
    const save = async (body) => {
      name.disabled = color.disabled = stock.disabled = true;
      try {
        const res = await fetch('/instance', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        note.hidden = true;
        render(data);
      } catch (err) {
        note.textContent = `Could not save: ${err.message}`;
        note.hidden = false;
        this._changed();
      } finally {
        // On success render has set these from the answer; on failure they go
        // back to what was in force before the attempt.
        name.disabled = color.disabled = false;
        stock.disabled = !storedColor;
      }
    };
    name.addEventListener('change', () => save({ name: name.value }));
    color.addEventListener('change', () => save({ color: color.value }));
    stock.addEventListener('click', () => save({ color: '' }));
    (async () => {
      try {
        const res = await fetch('/instance', { cache: 'no-store' });
        if (!res.ok) return;
        render(await res.json());
      } catch (_err) { /* card stays hidden */ }
    })();
  }

  // Agent models (GET/POST /agent-models): one dropdown per subagent, built
  // from the same model list the thread picker offers, after "Built-in" and,
  // where RETINUE_AGENT_MODELS sets one, "Deployment default". A choice is
  // saved on change and the card re-rendered from the server's answer, so what
  // is shown is what the next spawned dispatch will run. The card stays hidden
  // when the gateway offers no model.
  _wireAgentModels() {
    const card = this.querySelector('[data-agent-models]');
    const rows = this.querySelector('[data-agent-models-rows]');
    const note = this.querySelector('[data-agent-models-note]');
    const render = (data) => {
      const models = Array.isArray(data.models) ? data.models : [];
      const agents = Array.isArray(data.agents) ? data.agents : [];
      if (!agents.length || !models.length) { card.hidden = true; this._changed(); return; }
      card.hidden = false;
      rows.replaceChildren();
      for (const a of agents) {
        const label = document.createElement('label');
        label.className = 'num-setting agent-model';
        const agentName = document.createElement('span');
        agentName.textContent = a.label;
        agentName.title = a.description || '';
        label.appendChild(agentName);
        const select = document.createElement('select');
        select.dataset.agent = a.id;
        const addOption = (value, text, selected, reset = false) => {
          const opt = document.createElement('option');
          opt.value = value; opt.textContent = text; opt.selected = selected;
          if (reset) opt.dataset.reset = '1';
          select.appendChild(opt);
        };
        // A fixed agent runs on its built-in model whatever is stored for it.
        const pick = a.overridable ? a.chosen : '';
        const deployment = a.overridable ? a.env_default : '';
        addOption('', `Built-in${a.builtin ? ` (${a.builtin})` : ''}`,
          pick === '' || (pick === null && !deployment));
        // No dashboard choice: the agent follows RETINUE_AGENT_MODELS.
        if (deployment) addOption('', `Deployment default (${deployment})`, pick === null, true);
        let present = !pick;
        for (const m of models) {
          if (pick && m.id === pick) present = true;
          addOption(m.id, m.label || m.id, !!pick && m.id === pick);
        }
        // A stored choice no longer on the offered list (dropped upstream
        // since): show it rather than lie.
        if (!present) addOption(pick, pick, true);
        select.disabled = !a.overridable;
        if (!a.overridable) agentName.title = 'This agent’s definition cannot be overridden.';
        select.addEventListener('change', async () => {
          select.disabled = true;
          try {
            const res = await fetch('/agent-models', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(select.selectedOptions[0]?.dataset.reset
                ? { agent: a.id, reset: true } : { agent: a.id, model: select.value }),
            });
            const body = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            note.hidden = true;
            render(body);
          } catch (err) {
            note.textContent = `Could not save: ${err.message}`;
            note.hidden = false;
            select.disabled = false;
            this._changed();
          }
        });
        label.appendChild(select);
        rows.appendChild(label);
      }
      this._changed();
    };
    (async () => {
      try {
        const res = await fetch('/agent-models', { cache: 'no-store' });
        if (!res.ok) return;
        render(await res.json());
      } catch (_err) { /* card stays hidden */ }
    })();
  }

  // A card appeared, disappeared or changed size: the page embedding this one
  // (settings-instance.html) adjusts its frame.
  _changed() {
    this.dispatchEvent(new CustomEvent('retinue-instance-settings-change', { bubbles: true }));
  }
}

if (!customElements.get('retinue-instance-settings')) {
  customElements.define('retinue-instance-settings', RetinueInstanceSettings);
}
