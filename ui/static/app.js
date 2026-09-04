/* Voice Assistant Console - vanilla JS, no build step. */
"use strict";

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */
const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function fmtTime(epochSeconds) {
  if (!epochSeconds) return "–";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function ago(epochSeconds) {
  if (!epochSeconds) return "–";
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function pill(ok, okText, badText) {
  return `<span class="pill ${ok ? "ok" : "bad"}">${ok ? okText : (badText || "error")}</span>`;
}

let toastTimer = null;
function toast(message, isError) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", Boolean(isError));
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
}

async function api(path, options = {}) {
  const opts = { headers: {}, credentials: "same-origin", ...options };
  if (opts.method && opts.method !== "GET") opts.headers["X-VA-Request"] = "1";
  if (opts.body && !opts.headers["Content-Type"]) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  if (response.status === 401) { showLogin(); throw new Error("Not signed in"); }
  let payload = null;
  try { payload = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

/* ------------------------------------------------------------------ */
/* auth / bootstrap                                                    */
/* ------------------------------------------------------------------ */
let currentUser = null;

function showLogin() {
  $("#login-view").classList.remove("hidden");
  $("#app-view").classList.add("hidden");
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#who").textContent = currentUser ? `${currentUser.user} (${currentUser.method})` : "";
  route();
}

async function bootstrap() {
  let methods;
  try {
    methods = await api("auth/methods");
  } catch (err) {
    toast(err.message, true);
    return;
  }
  $("#login-oidc").classList.toggle("hidden", !methods.oidc);
  $("#login-form").classList.toggle("hidden", methods.local !== "enabled");
  $("#login-setup-warning").classList.toggle("hidden", methods.local !== "setup");
  if (methods.local === "setup") $(".divider").classList.add("hidden");

  try {
    currentUser = await api("api/session");
    showApp();
  } catch {
    showLogin();
  }
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#login-submit");
  button.disabled = true;
  try {
    await api("auth/login", {
      method: "POST",
      body: {
        email: $("#login-email").value,
        password: $("#login-password").value,
      },
    });
    currentUser = await api("api/session");
    showApp();
  } catch (err) {
    const box = $("#login-error");
    box.textContent = err.message;
    box.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("auth/logout", { method: "POST" }); } catch { /* ignore */ }
  currentUser = null;
  showLogin();
});

/* ------------------------------------------------------------------ */
/* tabs                                                                */
/* ------------------------------------------------------------------ */
const TABS = ["dashboard", "devices", "mcp", "settings", "audit"];
const loaders = {};

function route() {
  const hash = (location.hash || "#dashboard").slice(1);
  const tab = TABS.includes(hash) ? hash : "dashboard";
  document.querySelectorAll("nav a[data-tab]").forEach((link) => {
    link.classList.toggle("active", link.dataset.tab === tab);
  });
  TABS.forEach((name) => {
    $(`#tab-${name}`).classList.toggle("hidden", name !== tab);
  });
  if (currentUser && loaders[tab]) loaders[tab]().catch((e) => toast(e.message, true));
}

window.addEventListener("hashchange", route);
/* ------------------------------------------------------------------ */
/* dashboard                                                           */
/* ------------------------------------------------------------------ */
loaders.dashboard = async function () {
  const status = await api("api/status");

  const cards = [
    {
      title: "LiveKit server",
      value: status.livekit.ok
        ? `${pill(true, "online")} · ${status.livekit.latency_ms} ms` +
          (status.livekit.rooms.length ? `<div class="hint">rooms: ${esc(status.livekit.rooms.join(", "))}</div>` : "")
        : pill(false, "online", "unreachable") + `<div class="hint">${esc(status.livekit.error)}</div>`,
    },
    {
      title: "Assistant agent",
      value: status.agent.ok
        ? pill(true, "running") + `<div class="hint">last activity ${Math.round(status.agent.last_seen_age_s ?? 0)}s ago</div>`
        : pill(false, "running", "no signal") + `<div class="hint">${status.agent.last_seen_age_s != null ? `last event ${Math.round(status.agent.last_seen_age_s)}s ago` : "no events yet"}</div>`,
    },
    {
      title: "Console",
      value: pill(true, "v" + status.console.version) + `<div class="hint">uptime ${Math.floor(status.console.uptime_s / 60)} min</div>`,
    },
    {
      title: "Audit log",
      value: `${status.database.events} events · ${status.database.devices} devices` +
        `<div class="hint">history kept ${status.database.retention_days} days</div>`,
    },
  ];
  $("#status-cards").innerHTML = cards
    .map((c) => `<div class="card"><div class="title">${c.title}</div><div class="value">${c.value}</div></div>`)
    .join("");

  const mcpRows = status.mcp_servers.map((s) => `
    <tr>
      <td><strong>${esc(s.id)}</strong><div class="hint">${esc(s.source)}</div></td>
      <td class="mono">${esc(s.url)}</td>
      <td>${s.ok ? pill(true, `${s.latency_ms} ms`) : pill(false, "fail")}
        ${s.error ? `<div class="hint">${esc(s.error)}</div>` : ""}</td>
      <td>${s.tools && s.tools.length ? esc(s.tools.join(", ")) : '<span class="hint">–</span>'}</td>
    </tr>`);
  $("#status-mcp").innerHTML = mcpRows.length
    ? `<table><tr><th>Server</th><th>URL</th><th>Status</th><th>Tools</th></tr>${mcpRows.join("")}</table>`
    : '<p class="hint">No MCP servers configured - add some under "MCP Servers".</p>';

  const provRows = status.providers.map((p) => `
    <tr>
      <td>${esc(p.name)}</td>
      <td>${p.configured ? pill(true, "configured") : pill(false, "not set", "not set")}</td>
      <td class="mono hint">${esc(p.detail)}</td>
    </tr>`);
  $("#status-providers").innerHTML =
    `<table><tr><th>Provider</th><th>Status</th><th>Env var</th></tr>${provRows.join("")}</table>`;
};

/* ------------------------------------------------------------------ */
/* devices                                                             */
/* ------------------------------------------------------------------ */
loaders.devices = async function () {
  const data = await api("api/devices");
  const rows = data.devices.map((d) => `
    <tr>
      <td><strong>${esc(d.name)}</strong><div class="hint mono">${esc(d.identity)}</div></td>
      <td>${d.online ? pill(true, "online") : pill(false, "online", "offline")}</td>
      <td>${esc(d.kind)}</td>
      <td>${esc(d.current_room || d.last_room || "–")}</td>
      <td title="${esc(fmtTime(d.last_seen))}">${esc(ago(d.last_seen))}</td>
      <td>${d.session_count}</td>
      <td>
        <button class="ghost" data-rename="${esc(d.identity)}">Rename</button>
        <button class="ghost" data-forget="${esc(d.identity)}">Forget</button>
      </td>
    </tr>`);
  $("#devices-table").innerHTML = data.devices.length
    ? `<table><tr><th>Device</th><th>Live</th><th>Type</th><th>Room</th><th>Last seen</th><th>Sessions</th><th></th></tr>${rows.join("")}</table>`
    : '<p class="hint">No devices seen yet. Connect the reSpeaker or use the Talk tab.</p>';
  if (!data.livekit_ok && data.livekit_error) {
    toast(`LiveKit query failed: ${data.livekit_error}`, true);
  }

  document.querySelectorAll("[data-rename]").forEach((button) => {
    button.onclick = async () => {
      const identity = button.dataset.rename;
      const name = prompt(`Friendly name for ${identity}`);
      if (name == null) return;
      await api(`api/devices/${encodeURIComponent(identity)}`, { method: "PATCH", body: { name } });
      loaders.devices();
    };
  });
  document.querySelectorAll("[data-forget]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm(`Forget ${button.dataset.forget}?`)) return;
      await api(`api/devices/${encodeURIComponent(button.dataset.forget)}`, { method: "DELETE" });
      loaders.devices();
    };
  });
};

$("#device-add").addEventListener("click", async () => {
  const identity = $("#device-new-identity").value.trim();
  const name = $("#device-new-name").value.trim();
  if (!identity) { toast("Identity required", true); return; }
  try {
    await api(`api/devices/${encodeURIComponent(identity)}`, { method: "PATCH", body: { name } });
    $("#device-new-identity").value = "";
    $("#device-new-name").value = "";
    loaders.devices();
  } catch (err) { toast(err.message, true); }
});

$("#token-mint").addEventListener("click", async () => {
  const identity = $("#token-identity").value.trim() || `web-${Math.random().toString(36).slice(2, 6)}`;
  const room = $("#token-room").value.trim();
  try {
    const result = await api("api/tokens/mint", { method: "POST", body: { identity, room } });
    const out = $("#token-output");
    out.classList.remove("hidden");
    out.textContent =
      `url     : ${result.url || "(set PUBLIC_LIVEKIT_WS_URL)"}\n` +
      `room    : ${result.room}\nidentity: ${result.identity}\nvalid   : ${result.hours} h\n\n${result.token}`;
  } catch (err) { toast(err.message, true); }
});
/* ------------------------------------------------------------------ */
/* MCP servers                                                         */
/* ------------------------------------------------------------------ */
let uiServers = []; // working copy of the UI-managed list

loaders.mcp = async function () {
  const data = await api("api/mcp-servers");
  uiServers = data.servers.filter((s) => s.source === "ui").map((s) => ({
    id: s.id, url: s.url, headers: "", enabled: s.active,
  }));
  renderMcp(data.servers);
};

function renderMcp(serverView) {
  const readOnly = serverView
    .filter((s) => s.source !== "ui")
    .map((s) => `
      <div class="mcp-card">
        <div class="row-between">
          <div><strong>${esc(s.id)}</strong>
            <span class="pill ${s.active ? "muted" : "warn"}">${s.active ? esc(s.source) : "shadowed"}</span></div>
          <span class="hint mono">${esc(s.url)}</span>
        </div>
      </div>`)
    .join("");

  const editable = uiServers.map((s, index) => `
    <div class="mcp-card" data-index="${index}">
      <div class="mcp-grid">
        <label>ID</label><input data-field="id" value="${esc(s.id)}" placeholder="weather" />
        <label>Enabled</label>
        <span><input type="checkbox" data-field="enabled" ${s.enabled ? "checked" : ""} style="width:auto" /></span>
        <label>URL</label><input data-field="url" value="${esc(s.url)}" placeholder="http://localhost:9000/mcp" class="mono" />
        <label>Headers (JSON)</label><input data-field="headers" value="${esc(s.headers)}" placeholder='{"Authorization":"Bearer xyz"}' class="mono" />
        <label></label>
        <div class="mcp-actions">
          <button class="ghost" data-test="${index}">Test</button>
          <button class="ghost" data-remove="${index}">Remove</button>
        </div>
      </div>
      <div class="tools-list" data-tools="${index}"></div>
    </div>`)
    .join("");

  $("#mcp-list").innerHTML =
    (readOnly ? `<h3>From environment / Home Assistant</h3>${readOnly}` : "") +
    `<h3>Managed in this console</h3>` +
    (editable || '<p class="hint">No UI-managed servers yet.</p>');
  wireMcpActions();
}

function wireMcpActions() {
  document.querySelectorAll("#mcp-list .mcp-card[data-index]").forEach((card) => {
    const index = Number(card.dataset.index);
    card.querySelectorAll("[data-field]").forEach((input) => {
      input.addEventListener("change", () => {
        const field = input.dataset.field;
        uiServers[index][field] = field === "enabled" ? input.checked : input.value;
      });
    });
    const test = card.querySelector("[data-test]");
    if (test) test.onclick = async () => {
      syncCard(card, index);
      const box = card.querySelector(`[data-tools="${index}"]`);
      box.textContent = "testing…";
      let headers = {};
      try { headers = uiServers[index].headers ? JSON.parse(uiServers[index].headers || "{}") : {}; }
      catch { box.textContent = "Headers are not valid JSON"; return; }
      try {
        const result = await api("api/mcp-servers/test", {
          method: "POST",
          body: { url: uiServers[index].url, headers },
        });
        box.innerHTML = result.ok
          ? `${pill(true, `ok · ${result.latency_ms} ms`)}${result.tools.length ? ` tools: ${esc(result.tools.join(", "))}` : ""}`
          : `${pill(false, "ok", "failed")} ${esc(result.error)}`;
      } catch (err) { box.textContent = err.message; }
    };
    const remove = card.querySelector("[data-remove]");
    if (remove) remove.onclick = () => {
      uiServers.splice(index, 1);
      renderMcp([]);
    };
  });
}

function syncCard(card, index) {
  card.querySelectorAll("[data-field]").forEach((input) => {
    const field = input.dataset.field;
    uiServers[index][field] = field === "enabled" ? input.checked : input.value;
  });
}

$("#mcp-add").addEventListener("click", () => {
  uiServers.push({ id: "", url: "", headers: "", enabled: true });
  renderMcp([]);
});

$("#mcp-save").addEventListener("click", async () => {
  document.querySelectorAll("#mcp-list .mcp-card[data-index]").forEach((card) => {
    syncCard(card, Number(card.dataset.index));
  });
  const cleaned = uiServers
    .filter((s) => s.id.trim() && s.url.trim())
    .map((s) => {
      let headers = {};
      try { headers = s.headers ? JSON.parse(s.headers) : {}; }
      catch { throw new Error(`Headers for "${s.id}" are not valid JSON`); }
      return { id: s.id.trim(), url: s.url.trim(), headers, enabled: Boolean(s.enabled) };
    });
  try {
    await api("api/mcp-servers", { method: "PUT", body: { servers: cleaned } });
    toast("MCP servers saved - active for new assistant sessions");
    loaders.mcp();
  } catch (err) { toast(err.message, true); }
});
/* ------------------------------------------------------------------ */
/* settings                                                            */
/* ------------------------------------------------------------------ */
let settingDefs = [];

loaders.settings = async function () {
  const data = await api("api/settings");
  settingDefs = data.settings;

  const groups = [...new Set(data.settings.map((s) => s.group))];
  $("#settings-form").innerHTML = groups.map((group) => {
    const fields = data.settings
      .filter((s) => s.group === group)
      .map((s) => renderSetting(s))
      .join("");
    return `<fieldset><legend>${esc(group)}</legend>${fields}</fieldset>`;
  }).join("") +
  `<div><button type="button" id="settings-save">Save settings</button>
   <span class="hint">Applies to newly started assistant sessions.</span></div>`;
  $("#settings-save").addEventListener("click", saveSettings);

  $("#env-settings").innerHTML =
    "<table><tr><th>Setting</th><th>Value</th><th>Env var</th></tr>" +
    data.environment.map((e) => `
      <tr>
        <td>${esc(e.label)}</td>
        <td class="mono">${e.kind === "secret"
          ? (e.value ? "*** set ***" : '<span class="hint">not set</span>')
          : esc(e.value || "")}</td>
        <td class="mono hint">${esc(e.env_var)}</td>
      </tr>`).join("") +
    "</table>";
};

function renderSetting(s) {
  const meta = s.stored ? '<span class="meta">(override saved)</span>' : "";
  const help = s.help ? `<div class="desc">${esc(s.help)}</div>` : "";
  let input;
  if (s.kind === "bool") {
    input = `<input type="checkbox" data-key="${s.key}" ${s.value === "true" ? "checked" : ""} />`;
  } else if (s.choices && s.choices.length) {
    input = `<select data-key="${s.key}">${s.choices
      .map((c) => `<option value="${esc(c)}" ${c === s.value ? "selected" : ""}>${esc(c)}</option>`)
      .join("")}</select>`;
  } else if (s.kind === "text") {
    input = `<textarea data-key="${s.key}" rows="4">${esc(s.value)}</textarea>`;
  } else if (s.kind === "secret") {
    input = `<input type="password" data-key="${s.key}" placeholder="${s.value ? "saved (type to replace)" : "not set"}" autocomplete="new-password" />`;
  } else {
    input = `<input data-key="${s.key}" value="${esc(s.value)}" />`;
  }
  return `<div class="setting"><label>${esc(s.label)}${meta}${input}</label>${help}</div>`;
}

async function saveSettings() {
  const updates = {};
  $("#settings-form").querySelectorAll("[data-key]").forEach((input) => {
    const key = input.dataset.key;
    if (input.type === "checkbox") {
      updates[key] = input.checked ? "true" : "false";
    } else if (input.type === "password") {
      if (input.value) updates[key] = input.value; // empty = keep current
    } else {
      updates[key] = input.value;
    }
  });
  try {
    await api("api/settings", { method: "PUT", body: { updates } });
    toast("Settings saved - applied to new assistant sessions");
    loaders.settings();
  } catch (err) { toast(err.message, true); }
}
/* ------------------------------------------------------------------ */
/* audit log                                                           */
/* ------------------------------------------------------------------ */
const EVENT_TYPES = [
  "session.started", "session.ended", "device.join", "device.leave",
  "user_input", "agent_reply", "tool.call", "timer.expired", "agent.ready",
  "agent.heartbeat", "error", "config.changed", "token.minted",
  "auth.login", "auth.failed",
];
let oldestEventId = null;

loaders.audit = async function () {
  const select = $("#audit-type");
  if (select.options.length <= 1) {
    EVENT_TYPES.forEach((t) => {
      const option = document.createElement("option");
      option.value = t;
      option.textContent = t;
      select.appendChild(option);
    });
  }
  oldestEventId = null;
  await fetchEvents(false);
};

async function fetchEvents(append) {
  const params = new URLSearchParams();
  if ($("#audit-type").value) params.set("type", $("#audit-type").value);
  if ($("#audit-search").value.trim()) params.set("search", $("#audit-search").value.trim());
  if (append && oldestEventId) params.set("before", oldestEventId);
  const data = await api(`api/events?${params.toString()}`);
  const rows = data.events.map((e) => `
    <tr>
      <td title="${esc(fmtTime(e.ts))}">${esc(ago(e.ts))}</td>
      <td class="mono">${esc(e.type)}</td>
      <td class="mono">${esc(e.identity || e.room || "")}</td>
      <td class="evt-data">${esc(JSON.stringify(e.data))}</td>
    </tr>`);
  const table = $("#audit-table");
  if (append && table.querySelector("tbody")) {
    table.querySelector("tbody").insertAdjacentHTML("beforeend", rows.join(""));
  } else {
    table.innerHTML = data.events.length
      ? `<table><thead><tr><th>When</th><th>Event</th><th>Device / room</th><th>Data</th></tr></thead><tbody>${rows.join("")}</tbody></table>`
      : '<p class="hint">No events recorded yet.</p>';
  }
  oldestEventId = data.events.length ? data.events[data.events.length - 1].id : null;
  $("#audit-more").classList.toggle("hidden", data.events.length < 200);
  $("#audit-retention").innerHTML = data.transcripts_enabled
    ? "Transcript storage is <strong>on</strong> - utterances are stored (see Settings → Diagnostics)."
    : "Transcript storage is <strong>off</strong> - only interaction metadata is recorded.";
}

$("#audit-refresh").addEventListener("click", () => loaders.audit());
$("#audit-type").addEventListener("change", () => loaders.audit());
$("#audit-search").addEventListener("keydown", (e) => { if (e.key === "Enter") loaders.audit(); });
$("#audit-more").addEventListener("click", () => fetchEvents(true).catch((e) => toast(e.message, true)));
$("#audit-clear").addEventListener("click", async () => {
  if (!confirm("Delete ALL audit events?")) return;
  await api("api/events", { method: "DELETE" });
  loaders.audit();
});
$("#audit-export").href = "api/events/export?format=csv";

/* ------------------------------------------------------------------ */
bootstrap();




