// Deco Guard sidebar panel. Plain web component, no build step.
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const ago = (iso) => {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

class DecoGuardPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._state = null;
    this._error = null;
    this._busy = new Set();
    this._filter = "";
    this._tab = "all";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._render();
      this._load();
      this._timer = setInterval(() => this._load(), 15000);
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  connectedCallback() {
    if (this._hass && !this._timer) {
      this._load();
      this._timer = setInterval(() => this._load(), 15000);
    }
  }

  async _load() {
    try {
      this._state = await this._hass.connection.sendMessagePromise({ type: "deco_guard/state" });
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    // Don't rebuild the page (and steal focus) while the user is typing in the search box.
    if (this.shadowRoot.activeElement?.id === "filter") this._renderList();
    else this._renderBody();
  }

  async _act(action, mac, extra = {}) {
    this._busy.add(mac);
    this._renderBody();
    try {
      this._state = await this._hass.connection.sendMessagePromise({ type: "deco_guard/action", action, mac, ...extra });
      this._error = null;
    } catch (e) {
      this._error = `${action} ${mac}: ${e.message || e}`;
    }
    this._busy.delete(mac);
    this._renderBody();
  }

  async _setAutoBlock(on) {
    try {
      this._state = await this._hass.connection.sendMessagePromise({ type: "deco_guard/settings", auto_block: on });
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._renderBody();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; background: var(--primary-background-color); color: var(--primary-text-color); min-height:100vh; font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
        .toolbar { display:flex; align-items:center; height:56px; padding:0 12px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); font-size:20px; gap:8px; }
        .wrap { max-width: 980px; margin: 0 auto; padding: 16px; }
        .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color); margin-bottom: 16px; overflow:hidden; }
        .card h2 { font-size: 16px; margin: 0; padding: 14px 16px; border-bottom: 1px solid var(--divider-color); display:flex; align-items:center; gap:8px; }
        .muted { color: var(--secondary-text-color); font-size: 13px; }
        .row { display:flex; align-items:center; gap:12px; padding: 10px 16px; border-bottom: 1px solid var(--divider-color); }
        .row:last-child { border-bottom: none; }
        .main { flex:1; min-width:0; }
        .name { font-weight: 500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .meta { color: var(--secondary-text-color); font-size: 12px; font-family: var(--code-font-family, monospace); overflow-wrap:anywhere; }
        .dot { width:10px; height:10px; border-radius:50%; flex:none; background: var(--disabled-text-color, #999); }
        .dot.on { background: var(--success-color, #43a047); }
        .dot.blocked { background: var(--error-color, #db4437); }
        button { font: inherit; font-size: 13px; border-radius: 18px; padding: 6px 14px; cursor:pointer; border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color); white-space:nowrap; }
        button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
        button.danger { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
        button:disabled { opacity: .5; cursor: default; }
        .actions { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
        .pill { font-size: 11px; padding: 2px 8px; border-radius: 10px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
        .pill.blocked { background: rgba(219,68,55,.15); color: var(--error-color, #db4437); }
        .pill.pending { background: rgba(255,152,0,.18); color: var(--warning-color, #ff9800); }
        .controls { display:flex; gap:8px; padding: 10px 16px; border-bottom:1px solid var(--divider-color); flex-wrap:wrap; align-items:center; }
        input[type=search] { flex:1; min-width: 160px; padding: 8px 12px; border-radius: 8px; border:1px solid var(--divider-color); background: var(--secondary-background-color); color: var(--primary-text-color); font: inherit; }
        .tabs button.sel { background: var(--primary-color); color: var(--text-primary-color,#fff); border-color: transparent; }
        .settings { display:flex; align-items:center; gap:12px; padding: 14px 16px; }
        .error { background: rgba(219,68,55,.12); color: var(--error-color,#db4437); padding: 10px 16px; border-radius: 8px; margin-bottom: 16px; }
        .empty { padding: 16px; color: var(--secondary-text-color); }
        @media (max-width: 600px) { .row { flex-wrap: wrap; } .actions { width:100%; justify-content:flex-start; padding-left: 22px; } }
      </style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><span>Deco Guard</span></div>
      <div class="wrap" id="body"><div class="empty">Loading…</div></div>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;

    const body = this.shadowRoot.getElementById("body");
    body.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-action]");
      if (!btn) return;
      const { action, mac } = btn.dataset;
      if (action === "tab") {
        this._tab = btn.dataset.tab;
        this._renderBody();
      } else if (action === "autoblock") {
        this._setAutoBlock(!this._state.auto_block);
      } else if (action === "rename") {
        const cur = btn.dataset.name || "";
        const name = window.prompt("Name for this device", cur);
        if (name !== null) this._act("rename", mac, { name });
      } else {
        this._act(action, mac);
      }
    });
    body.addEventListener("input", (ev) => {
      if (ev.target.id === "filter") {
        this._filter = ev.target.value.toLowerCase();
        this._renderList();
      }
    });
  }

  _row(d, buttons) {
    const busy = this._busy.has(d.mac) ? "disabled" : "";
    const dot = d.blocked ? "blocked" : d.online ? "on" : "";
    const pill =
      d.status === "pending" ? `<span class="pill pending">pending</span>`
      : d.status === "denied" ? `<span class="pill blocked">denied</span>`
      : d.blocked ? `<span class="pill blocked">blocked</span>` : "";
    const meta = [d.mac, d.ip, d.client_type, d.connection, d.network === "guest" ? "guest Wi-Fi" : null]
      .filter(Boolean).map(esc).join(" · ");
    const seen = d.online ? "online" : d.last_seen ? `seen ${ago(d.last_seen)}` : "";
    return `<div class="row">
      <span class="dot ${dot}" title="${d.blocked ? "blocked" : d.online ? "online" : "offline"}"></span>
      <div class="main"><div class="name">${esc(d.name)} ${pill}</div>
        <div class="meta">${meta}${seen ? ` · ${esc(seen)}` : ""}</div></div>
      <div class="actions">${buttons.map((b) =>
        `<button class="${b.cls || ""}" data-action="${b.action}" data-mac="${esc(d.mac)}" data-name="${esc(d.name)}" ${busy}>${b.label}</button>`).join("")}</div>
    </div>`;
  }

  _renderBody() {
    const body = this.shadowRoot.getElementById("body");
    if (!body) return;
    const st = this._state;
    if (!st) {
      body.innerHTML = this._error ? `<div class="error">${esc(this._error)}</div>` : `<div class="empty">Loading…</div>`;
      return;
    }
    const pending = st.devices.filter((d) => d.status === "pending");
    const err = this._error ? `<div class="error">${esc(this._error)}</div>` : "";
    const stale = st.last_update_success ? "" : `<div class="error">Last poll of the Deco failed; data may be stale.</div>`;
    body.innerHTML = `${err}${stale}
      <div class="card">
        <h2>🛡️ New devices awaiting approval <span class="pill ${pending.length ? "pending" : ""}">${pending.length}</span></h2>
        ${pending.length
          ? pending.map((d) => this._row(d, [
              { action: "approve", label: "Allow", cls: "primary" },
              { action: "deny", label: "Keep blocked", cls: "danger" },
              { action: "rename", label: "Rename" },
            ])).join("")
          : `<div class="empty">No devices waiting. ${st.auto_block ? "New devices are blocked automatically and appear here." : "Auto-block is off, so new devices are allowed automatically."}</div>`}
      </div>
      <div class="card">
        <h2>Devices</h2>
        <div class="controls">
          <input id="filter" type="search" placeholder="Search name, MAC or IP" value="${esc(this._filter)}">
          <span class="tabs">
            ${[["all", "All"], ["online", "Online"], ["blocked", "Blocked"]].map(([k, l]) =>
              `<button data-action="tab" data-tab="${k}" class="${this._tab === k ? "sel" : ""}">${l}</button>`).join(" ")}
          </span>
        </div>
        <div id="list"></div>
      </div>
      <div class="card">
        <div class="settings">
          <div class="main"><div class="name">Auto-block new devices</div>
            <div class="muted">Any device never seen before is blocked at the Deco within one poll interval and listed above until you allow it.</div></div>
          <button data-action="autoblock" class="${st.auto_block ? "primary" : ""}">${st.auto_block ? "On" : "Off"}</button>
        </div>
      </div>`;
    this._renderList();
  }

  _renderList() {
    const list = this.shadowRoot.getElementById("list");
    if (!list || !this._state) return;
    const f = this._filter;
    const devices = this._state.devices
      .filter((d) => d.status !== "pending")
      .filter((d) => this._tab === "all" || (this._tab === "online" ? d.online : d.blocked))
      .filter((d) => !f || [d.name, d.mac, d.ip].some((v) => (v || "").toLowerCase().includes(f)))
      .sort((a, b) => (b.blocked - a.blocked) || (b.online - a.online) || a.name.localeCompare(b.name));
    list.innerHTML = devices.length
      ? devices.map((d) => this._row(d, [
          d.blocked
            ? { action: "unblock", label: "Unblock", cls: "primary" }
            : { action: "block", label: "Block", cls: "danger" },
          { action: "rename", label: "Rename" },
          ...(d.online ? [] : [{ action: "forget", label: "Forget" }]),
        ])).join("")
      : `<div class="empty">No matching devices.</div>`;
  }
}

customElements.define("deco-guard-panel", DecoGuardPanel);
