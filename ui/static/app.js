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

function clearTextSelection() {
  // Tab switches and table re-renders replace DOM under an existing text
  // selection; dropping it keeps the browser from normalizing the now
  // detached selection into a document-wide highlight (looks like ctrl+a).
  const selection = window.getSelection();
  if (selection && selection.rangeCount) selection.removeAllRanges();
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
  clearTextSelection();
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

  const mcpRows = status.mcp_servers.map((s) => {
    let statusCell;
    if (s.active === false) {
      statusCell = `<span class="pill muted">${s.source === "ui" ? "disabled" : "shadowed"}</span>`;
    } else if (s.ok) {
      statusCell = pill(true, `online · ${s.latency_ms} ms`);
    } else {
      statusCell = pill(false, "online", "unreachable");
    }
    const errorHint = s.error ? `<div class="hint">${esc(s.error)}</div>` : "";
    return `
    <tr>
      <td><strong>${esc(s.id)}</strong><div class="hint">${esc(s.source)}</div></td>
      <td class="mono">${esc(s.url)}</td>
      <td>${statusCell}${errorHint}</td>
    </tr>`;
  });
  $("#status-mcp").innerHTML = mcpRows.length
    ? `<table><tr><th>Server</th><th>URL</th><th>Status</th></tr>${mcpRows.join("")}</table>`
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
  const hoursChoice = $("#token-hours").value;
  const body = { identity, room };
  if (hoursChoice !== "") body.hours = Number(hoursChoice); // 0 = no expiry
  try {
    const result = await api("api/tokens/mint", { method: "POST", body });
    const validity = result.expires ? `${result.hours} h` : "no expiry (10-year token)";
    const out = $("#token-output");
    out.classList.remove("hidden");
    out.textContent =
      `url     : ${result.url || "(set PUBLIC_LIVEKIT_WS_URL)"}\n` +
      `room    : ${result.room}\nidentity: ${result.identity}\nvalid   : ${validity}\n\n${result.token}`;
  } catch (err) { toast(err.message, true); }
});
/* ------------------------------------------------------------------ */
/* MCP servers                                                         */
/* ------------------------------------------------------------------ */
let uiServers = [];             // working copy of the console-managed list
let serverViewAll = [];         // merged view from the API (all sources)
let statusByServer = {};        // server id -> latest probe result
let expandedServers = new Set(); // servers whose details are open
let testerTools = [];           // tools of the tester's selected server

loaders.mcp = async function () {
  const [mcpData, statusData] = await Promise.all([
    api("api/mcp-servers"),
    api("api/status").catch(() => null),
  ]);
  serverViewAll = mcpData.servers;
  statusByServer = {};
  ((statusData && statusData.mcp_servers) || []).forEach((s) => {
    statusByServer[s.id] = s;
  });
  uiServers = serverViewAll
    .filter((s) => s.source === "ui")
    .map((s) => ({
      id: s.id,
      url: s.url,
      // masked header values (xyz***) round-trip safely: the API keeps the
      // stored secret when it receives its masked form back
      headers: s.headers && Object.keys(s.headers).length ? JSON.stringify(s.headers) : "",
      enabled: s.active,
      disabled_tools: [...(s.disabled_tools || [])],
    }));
  renderMcp();
  renderTester();
};

function serverTools(id) {
  const status = statusByServer[id];
  if (!status) return [];
  if (status.tool_details && status.tool_details.length) return status.tool_details;
  return (status.tools || []).map((name) => ({ name }));
}

function serverStatusLabel(id, enabled) {
  if (enabled === false) return '<span class="pill warn">server off</span>';
  const status = statusByServer[id];
  if (!status || status.ok === null || status.ok === undefined) {
    return '<span class="pill muted">not probed</span>';
  }
  return status.ok
    ? pill(true, `online · ${status.latency_ms} ms`)
    : pill(false, "online", "unreachable");
}

function probeMetaHtml(id) {
  const status = statusByServer[id];
  if (!status) return "";
  const bits = [];
  if (status.server_info && status.server_info.name) {
    bits.push(
      `server: <strong>${esc(status.server_info.name)}</strong>` +
      (status.server_info.version ? ` v${esc(status.server_info.version)}` : "")
    );
  }
  if (status.protocol_version) bits.push(`protocol: MCP ${esc(status.protocol_version)}`);
  if (status.latency_ms) bits.push(`latency: ${status.latency_ms} ms`);
  if (!bits.length) return "";
  const error = status.error ? `<div class="hint">last probe: ${esc(status.error)}</div>` : "";
  return `<div class="mcp-meta">${bits.map((b) => `<span>${b}</span>`).join("")}</div>${error}`;
}

function mcpToolsHtml(tools, disabledTools, editable) {
  if (!tools.length) {
    return '<p class="hint">No tools discovered yet - use "Test connection" to ask the server.</p>';
  }
  const disabled = new Set(disabledTools || []);
  const rows = tools.map((t) => {
    const desc = t.description
      ? `<div class="tool-desc">${esc(t.description)}</div>`
      : '<span class="hint">–</span>';
    let control = "";
    if (editable) {
      control =
        `<label class="tool-toggle"><input type="checkbox" data-tool="${esc(t.name)}"` +
        ` ${disabled.has(t.name) ? "" : "checked"} /> enabled</label>`;
    } else if (disabled.has(t.name)) {
      control = '<span class="pill warn">disabled</span>';
    }
    return `<tr><td class="mono">${esc(t.name)}</td><td>${desc}</td><td>${control}</td></tr>`;
  }).join("");
  return `
    <h3>Tools (${tools.length})</h3>
    <table class="tools-table">
      <tr><th>Tool</th><th>Description</th><th>${editable ? "Offered" : ""}</th></tr>
      ${rows}
    </table>
    ${editable
      ? '<p class="hint">Untick a tool to hide it from the assistant. Saved changes apply to new sessions.</p>'
      : '<p class="hint">Tools of environment-defined servers are always offered.</p>'}`;
}

function mcpUiCardHtml(entry, index) {
  const id = entry.id;
  const expanded = expandedServers.has(id);
  const tools = serverTools(id);
  const header = `
    <div class="mcp-header">
      <span class="chevron">&#9656;</span>
      <strong>${esc(id || "(new server)")}</strong>
      <span class="pill ok">console</span>
      ${tools.length ? `<span class="pill muted">${tools.length} tool${tools.length === 1 ? "" : "s"}</span>` : ""}
      ${serverStatusLabel(id, entry.enabled)}
      <span class="hint mono mcp-url">${esc(entry.url)}</span>
    </div>`;
  const body = `
    <div class="mcp-details ${expanded ? "" : "hidden"}">
      ${probeMetaHtml(id)}
      <div class="mcp-grid">
        <label>ID</label><input data-field="id" value="${esc(entry.id)}" placeholder="weather" />
        <label>Enabled</label>
        <span><input type="checkbox" data-field="enabled" ${entry.enabled ? "checked" : ""} style="width:auto" /> offer this server to the assistant</span>
        <label>URL</label><input data-field="url" value="${esc(entry.url)}" placeholder="http://localhost:9000/mcp" class="mono" />
        <label>Headers (JSON)</label><input data-field="headers" value="${esc(entry.headers)}" placeholder='{"Authorization":"Bearer xyz"}' class="mono" />
        <label></label>
        <div class="mcp-actions">
          <button class="ghost" data-test="1">Test connection</button>
          <button class="ghost" data-remove="1">Remove</button>
        </div>
      </div>
      <div class="probe-box hidden" data-probe></div>
      <div data-tools-wrap>${mcpToolsHtml(tools, entry.disabled_tools, true)}</div>
    </div>`;
  return `<div class="mcp-card ${expanded ? "expanded" : ""}" data-server="${esc(id)}" data-index="${index}">${header}${body}</div>`;
}

function mcpReadOnlyCardHtml(s) {
  const expanded = expandedServers.has(s.id);
  const tools = serverTools(s.id);
  const badge = s.active
    ? `<span class="pill muted">${esc(s.source)}</span>`
    : '<span class="pill warn">shadowed</span>';
  const origin = s.source === "home-assistant"
    ? "the Home Assistant integration (Settings)"
    : "the environment (MCP_SERVERS_JSON)";
  const header = `
    <div class="mcp-header">
      <span class="chevron">&#9656;</span>
      <strong>${esc(s.id)}</strong>
      ${badge}
      ${tools.length ? `<span class="pill muted">${tools.length} tool${tools.length === 1 ? "" : "s"}</span>` : ""}
      ${s.active ? serverStatusLabel(s.id, true) : ""}
      <span class="hint mono mcp-url">${esc(s.url)}</span>
    </div>`;
  const body = `
    <div class="mcp-details ${expanded ? "" : "hidden"}">
      ${probeMetaHtml(s.id)}
      <p class="hint">Defined via ${origin} - read-only here. A console entry with the same ID overrides it.</p>
      <div class="mcp-actions"><button class="ghost" data-test="1">Test connection</button></div>
      <div class="probe-box hidden" data-probe></div>
      <div data-tools-wrap>${mcpToolsHtml(tools, s.disabled_tools, false)}</div>
    </div>`;
  return `<div class="mcp-card ${expanded ? "expanded" : ""}" data-server="${esc(s.id)}">${header}${body}</div>`;
}

function renderMcp() {
  const uiCards = uiServers.map((entry, index) => mcpUiCardHtml(entry, index)).join("");
  const otherCards = serverViewAll
    .filter((s) => s.source !== "ui")
    .map((s) => mcpReadOnlyCardHtml(s))
    .join("");
  $("#mcp-list").innerHTML =
    `<h3>Managed in this console</h3>` +
    (uiCards || '<p class="hint">No console-managed servers yet - add one above.</p>') +
    (otherCards ? `<h3>From environment / Home Assistant (read-only)</h3>${otherCards}` : "");
  wireMcpCards();
}

function toggleCard(card) {
  const details = card.querySelector(".mcp-details");
  const open = details.classList.contains("hidden");
  details.classList.toggle("hidden", !open);
  card.classList.toggle("expanded", open);
  if (open) expandedServers.add(card.dataset.server);
  else expandedServers.delete(card.dataset.server);
  // first expand without probe data: discover the server's tools right away
  const status = statusByServer[card.dataset.server];
  const needsProbe = !status || (!status.ok && !(status.tools && status.tools.length));
  const testBtn = card.querySelector("[data-test]");
  if (open && needsProbe && testBtn) testBtn.click();
}

function probeResultHtml(result) {
  if (!result.ok) {
    return `${pill(false, "ok", "failed")} <span class="hint">${esc(result.error)}</span>`;
  }
  const info = result.server_info && result.server_info.name
    ? `<span class="hint">${esc(result.server_info.name)}${result.server_info.version ? ` v${esc(result.server_info.version)}` : ""}</span>`
    : "";
  const proto = result.protocol_version ? `<span class="hint">MCP ${esc(result.protocol_version)}</span>` : "";
  const warn = result.error ? `<div class="hint">${esc(result.error)}</div>` : "";
  return `${pill(true, `ok · ${result.latency_ms} ms`)} ${info} ${proto} ${warn}`;
}

function uiHeadersFor(entry) {
  // parses the working copy's Headers (JSON) field; null on invalid input
  if (!entry.headers || !entry.headers.trim()) return {};
  try {
    const parsed = JSON.parse(entry.headers);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function wireMcpCards() {
  document.querySelectorAll("#mcp-list .mcp-card").forEach((card) => {
    const index = card.dataset.index !== undefined ? Number(card.dataset.index) : -1;
    const isUi = index >= 0 && uiServers[index];

    card.querySelector(".mcp-header").addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, select, label")) return;
      toggleCard(card);
    });

    if (isUi) {
      card.querySelectorAll("[data-field]").forEach((input) => {
        input.addEventListener("change", () => {
          const entry = uiServers[index];
          if (!entry) return;
          const field = input.dataset.field;
          entry[field] = field === "enabled" ? input.checked : input.value;
          if (field === "id") {
            // keep expansion state and card identity in sync with the new id
            const newId = input.value.trim();
            expandedServers.delete(card.dataset.server);
            expandedServers.add(newId);
            card.dataset.server = newId;
            const title = card.querySelector(".mcp-header strong");
            if (title) title.textContent = newId || "(new server)";
          }
        });
      });
      card.querySelectorAll("[data-tool]").forEach((box) => {
        box.addEventListener("change", () => {
          const entry = uiServers[index];
          if (!entry) return;
          const tool = box.dataset.tool;
          entry.disabled_tools = entry.disabled_tools || [];
          if (box.checked) {
            entry.disabled_tools = entry.disabled_tools.filter((t) => t !== tool);
          } else if (!entry.disabled_tools.includes(tool)) {
            entry.disabled_tools.push(tool);
          }
        });
      });
      const removeBtn = card.querySelector("[data-remove]");
      if (removeBtn) {
        removeBtn.addEventListener("click", () => {
          const entry = uiServers[index];
          if (!entry) return;
          if (!confirm(`Remove MCP server "${entry.id || "(unnamed)"}"?`)) return;
          uiServers.splice(index, 1);
          renderMcp();
        });
      }
    }

    const testBtn = card.querySelector("[data-test]");
    if (testBtn) testBtn.addEventListener("click", () => testCard(card, index));
  });
}

async function testCard(card, index) {
  const entry = index >= 0 ? uiServers[index] : null;
  const box = card.querySelector("[data-probe]");
  const toolsWrap = card.querySelector("[data-tools-wrap]");
  box.classList.remove("hidden");
  box.innerHTML = '<span class="hint">testing…</span>';

  let body;
  if (entry) {
    if (!entry.url.trim()) {
      box.innerHTML = '<span class="error">URL required</span>';
      return;
    }
    const headers = uiHeadersFor(entry);
    if (headers === null) {
      box.innerHTML = '<span class="error">Headers are not valid JSON</span>';
      return;
    }
    body = { url: entry.url.trim(), headers };
  } else {
    // environment-defined server: the console resolves URL + auth headers
    body = { id: card.dataset.server };
  }
  try {
    const result = await api("api/mcp-servers/test", { method: "POST", body });
    statusByServer[card.dataset.server] = {
      ...(statusByServer[card.dataset.server] || {}), ...result,
    };
    box.innerHTML = probeResultHtml(result);
    if (toolsWrap) {
      const source = serverViewAll.find((s) => s.id === card.dataset.server) || {};
      const disabled = entry
        ? entry.disabled_tools
        : source.disabled_tools;
      toolsWrap.innerHTML =
        mcpToolsHtml(serverTools(card.dataset.server), disabled, Boolean(entry));
      if (entry) wireToolToggles(card, index);
    }
  } catch (err) {
    box.innerHTML = `<span class="error">${esc(err.message)}</span>`;
  }
}

$("#mcp-add").addEventListener("click", () => {
  uiServers.push({ id: "", url: "", headers: "", enabled: true, disabled_tools: [] });
  renderMcp();
  const cards = document.querySelectorAll('#mcp-list .mcp-card[data-server=""]');
  if (cards.length) cards[cards.length - 1].scrollIntoView({ block: "center" });
});

$("#mcp-save").addEventListener("click", async () => {
  const cleaned = [];
  for (const s of uiServers) {
    if (!s.id.trim() && !s.url.trim()) continue; // untouched empty row
    if (!s.id.trim() || !s.url.trim()) {
      toast("Every MCP server needs an ID and a URL", true);
      return;
    }
    const headers = uiHeadersFor(s);
    if (headers === null) {
      toast(`Headers for "${s.id}" are not valid JSON`, true);
      return;
    }
    cleaned.push({
      id: s.id.trim(),
      url: s.url.trim(),
      headers,
      enabled: Boolean(s.enabled),
      disabled_tools: [...(s.disabled_tools || [])],
    });
  }
  try {
    await api("api/mcp-servers", { method: "PUT", body: { servers: cleaned } });
    toast("MCP servers saved - active for new assistant sessions");
    loaders.mcp();
  } catch (err) { toast(err.message, true); }
});

/* ------------------------------------------------------------------ */
/* tool tester (call a tool with typed request arguments)              */
/* ------------------------------------------------------------------ */
let testerServerId = null;

function renderTester() {
  const select = $("#mcp-test-server");
  const previous = select.value;
  // console-managed entries first (they may shadow env definitions)
  const options = [
    ...uiServers.filter((u) => u.id.trim()).map((u) => ({ id: u.id, active: u.enabled })),
    ...serverViewAll
      .filter((s) => s.source !== "ui" && !uiServers.some((u) => u.id === s.id))
      .map((s) => ({ id: s.id, active: s.active })),
  ];
  select.innerHTML = options.map((o) =>
    `<option value="${esc(o.id)}">${esc(o.id)}${o.active === false ? " (disabled)" : ""}</option>`
  ).join("");
  if (previous && options.some((o) => o.id === previous)) select.value = previous;
  if (!options.length) {
    $("#mcp-test-tool").innerHTML = '<option value="">(no servers)</option>';
    $("#mcp-test-desc").textContent = "";
    testerServerId = null;
    return;
  }
  // probe only when the selection changed or no tools were loaded yet
  if (select.value !== testerServerId || !$("#mcp-test-tool").options.length) {
    testerServerId = select.value;
    refreshTesterTools();
  }
}

function wireToolToggles(card, index) {
  card.querySelectorAll("[data-tool]").forEach((box) => {
    box.addEventListener("change", () => {
      const entry = uiServers[index];
      if (!entry) return;
      const tool = box.dataset.tool;
      entry.disabled_tools = entry.disabled_tools || [];
      if (box.checked) {
        entry.disabled_tools = entry.disabled_tools.filter((t) => t !== tool);
      } else if (!entry.disabled_tools.includes(tool)) {
        entry.disabled_tools.push(tool);
      }
    });
  });
}

async function refreshTesterTools() {
  const id = $("#mcp-test-server").value;
  const toolSelect = $("#mcp-test-tool");
  $("#mcp-test-desc").textContent = "";
  if (!id) return;
  toolSelect.innerHTML = '<option value="">loading…</option>';

  let body;
  const entry = uiServers.find((u) => u.id === id);
  if (entry) {
    const headers = uiHeadersFor(entry);
    if (headers === null) {
      toolSelect.innerHTML = '<option value=""></option>';
      $("#mcp-test-desc").textContent = "Headers are not valid JSON";
      return;
    }
    body = { url: entry.url.trim(), headers };
  } else {
    body = { id };
  }
  try {
    const result = await api("api/mcp-servers/test", { method: "POST", body });
    statusByServer[id] = { ...(statusByServer[id] || {}), ...result };
    testerTools = result.tool_details && result.tool_details.length
      ? result.tool_details
      : (result.tools || []).map((name) => ({ name }));
    if (!result.ok) {
      toolSelect.innerHTML = '<option value="">(probe failed)</option>';
      $("#mcp-test-desc").textContent = result.error || "probe failed";
      return;
    }
    if (!testerTools.length) {
      toolSelect.innerHTML = '<option value="">(no tools discovered)</option>';
      $("#mcp-test-desc").textContent = result.error || "";
      return;
    }
    toolSelect.innerHTML = testerTools.map((t) =>
      `<option value="${esc(t.name)}">${esc(t.name)}</option>`
    ).join("");
    onTesterToolChange();
  } catch (err) {
    toolSelect.innerHTML = '<option value=""></option>';
    $("#mcp-test-desc").textContent = err.message;
  }
}

function sampleValue(prop) {
  if (!prop || typeof prop !== "object") return null;
  if (Array.isArray(prop.enum) && prop.enum.length) return prop.enum[0];
  if (prop.default !== undefined) return prop.default;
  if (Array.isArray(prop.examples) && prop.examples.length) return prop.examples[0];
  switch (prop.type) {
    case "integer":
    case "number": return 0;
    case "boolean": return false;
    case "array": return [];
    case "object": return {};
    default: return "";
  }
}

function sampleArguments(schema) {
  if (!schema || typeof schema !== "object") return {};
  const props = schema.properties && typeof schema.properties === "object"
    ? schema.properties : {};
  const required = new Set(schema.required || []);
  const sample = {};
  for (const [name, prop] of Object.entries(props)) {
    if (required.size && !required.has(name)) continue;
    sample[name] = sampleValue(prop);
  }
  return sample;
}

function onTesterToolChange() {
  const name = $("#mcp-test-tool").value;
  const tool = testerTools.find((t) => t.name === name);
  $("#mcp-test-desc").textContent = tool && tool.description ? tool.description : "";
  if (tool) {
    $("#mcp-test-args").value = JSON.stringify(sampleArguments(tool.input_schema), null, 2);
  }
}

$("#mcp-test-server").addEventListener("change", () => {
  refreshTesterTools().catch((e) => toast(e.message, true));
});
$("#mcp-test-tool").addEventListener("change", onTesterToolChange);
$("#mcp-test-refresh").addEventListener("click", () => {
  refreshTesterTools().catch((e) => toast(e.message, true));
});

$("#mcp-test-run").addEventListener("click", async () => {
  const out = $("#mcp-test-output");
  const tool = $("#mcp-test-tool").value;
  if (!tool || tool.startsWith("(")) {
    toast("Pick a tool first - use Refresh tools if the list is empty", true);
    return;
  }
  let args;
  try {
    args = JSON.parse($("#mcp-test-args").value || "{}");
  } catch (err) {
    toast(`Arguments are not valid JSON: ${err.message}`, true);
    return;
  }
  const id = $("#mcp-test-server").value;
  const entry = uiServers.find((u) => u.id === id);
  let body;
  if (entry) {
    const headers = uiHeadersFor(entry);
    if (headers === null) { toast("Headers are not valid JSON", true); return; }
    body = { url: entry.url.trim(), headers };
  } else {
    body = { id };
  }
  out.classList.remove("hidden");
  out.textContent = "calling…";
  try {
    const result = await api("api/mcp-servers/call", {
      method: "POST",
      body: { ...body, tool, arguments: args },
    });
    const headline = result.ok
      ? (result.is_error ? `tool error (${result.latency_ms} ms)` : `ok (${result.latency_ms} ms)`)
      : `failed: ${result.error}`;
    out.textContent = `${headline}\n\n${JSON.stringify(result, null, 2)}`;
  } catch (err) {
    out.textContent = `error: ${err.message}`;
  }
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
  // the render replaced nodes an existing selection pointed into
  clearTextSelection();
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




