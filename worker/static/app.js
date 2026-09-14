const $ = (id) => document.getElementById(id);
const state = {
  connected: false,
  busy: false,
  traceCount: 0,
  tools: [],
  credentials: [],
  calls: [],
  artifacts: [],
  artifactsReady: false,
  activeArtifactId: null,
  status: null,
};

const PERSONA_ICONS = new Set([
  "ph-kanban", "ph-terminal-window", "ph-magnifying-glass", "ph-pen-nib",
  "ph-shield-check", "ph-globe-hemisphere-west", "ph-flow-arrow",
]);
const FALLBACK_PERSONA_ICON = "ph-user-circle";

function personaIcon(persona) {
  return PERSONA_ICONS.has(persona?.icon) ? persona.icon : FALLBACK_PERSONA_ICON;
}

function applyPersonaIcon(persona) {
  const icon = personaIcon(persona);
  const colour = typeof persona?.colour === "string" && /^#[0-9a-f]{6}$/i.test(persona.colour)
    ? persona.colour
    : "";
  document.documentElement.style.setProperty("--persona-colour", colour || "var(--accent)");
  for (const id of ["sidebar-persona-icon", "header-persona-icon", "welcome-persona-icon"]) {
    const target = $(id);
    if (target) target.className = `ph ${icon} persona-icon${id === "welcome-persona-icon" ? " welcome-persona-icon" : ""}`;
  }
}

const CONNECTIONS = {
  legacy: { key: "legacy-local", label: "Legacy Datum" },
  office: { key: "officecli-demo", label: "OfficeCLI document demo" },
  postgres: { key: "postgres-demo", label: "PostgreSQL analytics demo" },
  harness: { key: "harness-tools", label: "Datum Harness tools" },
};

function trace(title, detail, kind = "") {
  const row = document.createElement("div");
  row.className = `trace-entry ${kind}`;
  const dot = document.createElement("span");
  dot.className = "trace-node";
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  const text = document.createElement("p");
  strong.textContent = title;
  text.textContent = detail;
  copy.append(strong, text);
  row.append(dot, copy);
  $("trace").prepend(row);
  state.traceCount += 1;
}

function message(role, text = "") {
  $("welcome").style.display = "none";
  const wrapper = document.createElement("div");
  wrapper.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble stream-text";
  bubble.textContent = text;
  wrapper.append(bubble);
  $("messages").append(wrapper);
  $("messages").scrollTop = $("messages").scrollHeight;
  return bubble;
}

function renderPrompts(prompts) {
  const container = $("welcome-prompts");
  container.replaceChildren();
  for (const prompt of prompts.slice(0, 3)) {
    const button = document.createElement("button");
    button.className = "prompt-chip";
    button.type = "button";
    const icon = document.createElement("i");
    icon.className = "ph ph-arrow-up-right";
    icon.setAttribute("aria-hidden", "true");
    button.append(icon, document.createTextNode(prompt));
    button.addEventListener("click", () => sendPrompt(prompt));
    container.append(button);
  }
}

function toolGroup(toolName) {
  return toolName.split("__", 1)[0] || "datum";
}

function connectionFor(toolName) {
  return CONNECTIONS[toolGroup(toolName)] || { key: toolGroup(toolName), label: toolGroup(toolName) };
}

function recordToolEvent(data) {
  const current = [...state.calls].reverse().find((call) =>
    call.tool === data.tool && call.server === data.server && call.phase === "started",
  );
  if (data.phase === "started") {
    state.calls.push({
      tool: data.tool,
      server: data.server,
      phase: "started",
      startedAt: new Date(),
      durationMs: null,
      hasError: false,
    });
    return;
  }
  if (current) {
    current.phase = data.phase;
    current.durationMs = data.duration_ms ?? null;
    current.hasError = Boolean(data.has_error);
    current.completedAt = new Date();
    return;
  }
  state.calls.push({
    tool: data.tool,
    server: data.server,
    phase: data.phase || "completed",
    startedAt: new Date(),
    durationMs: data.duration_ms ?? null,
    hasError: Boolean(data.has_error),
  });
}

async function refreshTools() {
  if (!state.connected) {
    state.tools = [];
    $("tool-count").textContent = "0";
    return;
  }
  try {
    const response = await fetch("/api/tools", { cache: "no-store" });
    const data = await response.json();
    state.tools = Array.isArray(data.tools) ? data.tools : [];
    $("tool-count").textContent = String(state.tools.length);
  } catch (_) {
    state.tools = [];
    $("tool-count").textContent = "—";
  }
}

function switchPaneMode(mode) {
  document.querySelectorAll(".pane-mode").forEach((item) => item.classList.toggle("active", item.dataset.mode === mode));
  document.querySelectorAll(".pane-view").forEach((item) => item.classList.toggle("active", item.dataset.view === mode));
}

function formatArtifactSize(value) {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(value < 10240 ? 1 : 0)} KB`;
}

function renderArtifacts() {
  const list = $("artifact-list");
  list.replaceChildren();
  $("artifact-count").textContent = String(state.artifacts.length);
  if (!state.artifacts.length) {
    list.append(node("p", "artifact-empty", state.connected ? "No resources visible to this agent." : "Connect an agent to inspect resources."));
    return;
  }
  for (const artifact of state.artifacts) {
    const button = node("button", `artifact-item${artifact.id === state.activeArtifactId ? " active" : ""}`);
    button.type = "button";
    const icon = node("i", `ph ${artifact.media_type === "text/html" ? "ph-browser" : artifact.media_type.includes("json") ? "ph-brackets-curly" : "ph-file-text"}`);
    icon.setAttribute("aria-hidden", "true");
    const copy = node("span", "artifact-item-copy");
    copy.append(node("strong", "", artifact.title), node("small", "", `v${artifact.version} · ${formatArtifactSize(artifact.bytes)} · ${artifact.access}`));
    button.append(icon, copy);
    button.addEventListener("click", () => openArtifact(artifact));
    list.append(button);
  }
}

async function openArtifact(artifact) {
  state.activeArtifactId = artifact.id;
  renderArtifacts();
  switchPaneMode("artifacts");
  if (!$("artifact-pane").classList.contains("open")) $("btn-trace").click();
  $("artifact-empty").classList.add("hidden");
  $("artifact-meta").classList.remove("hidden");
  $("artifact-title").textContent = artifact.title;
  $("artifact-path").textContent = artifact.path;
  $("artifact-owner").textContent = `Owned by ${artifact.owner} · ${artifact.media_type}`;
  $("artifact-access").textContent = artifact.access === "owner" ? "Private owner view" : "Shared with this agent";
  const frame = $("artifact-frame");
  const text = $("artifact-text");
  frame.classList.add("hidden"); text.classList.add("hidden"); frame.removeAttribute("src");
  if (artifact.media_type === "text/html") {
    frame.src = `/api/artifacts/${encodeURIComponent(artifact.id)}/content`;
    frame.classList.remove("hidden");
    return;
  }
  text.textContent = "Loading…"; text.classList.remove("hidden");
  try {
    const response = await fetch(`/api/artifacts/${encodeURIComponent(artifact.id)}/content`, { cache: "no-store" });
    if (!response.ok) throw new Error("Artifact access was denied");
    const raw = await response.text();
    if (artifact.media_type.includes("json")) {
      try { text.textContent = JSON.stringify(JSON.parse(raw), null, 2); }
      catch (_) { text.textContent = raw; }
    } else text.textContent = raw;
  } catch (error) { text.textContent = error.message; }
}

async function refreshArtifacts({ openNew = false } = {}) {
  if (!state.connected) {
    state.artifacts = []; state.artifactsReady = false; state.activeArtifactId = null;
    renderArtifacts(); return;
  }
  try {
    const known = new Set(state.artifacts.map((item) => item.id));
    const response = await fetch("/api/artifacts", { cache: "no-store" });
    if (!response.ok) throw new Error("resource list unavailable");
    const data = await response.json();
    const next = Array.isArray(data.items) ? data.items : [];
    const added = state.artifactsReady ? next.find((item) => !known.has(item.id)) : null;
    state.artifacts = next; state.artifactsReady = true;
    renderArtifacts();
    if (openNew && added) {
      trace("Artifact available", `${added.title} · ${added.access}`, "ok");
      await openArtifact(added);
    }
  } catch (_) { renderArtifacts(); }
}

async function refreshCredentials() {
  if (!state.connected) {
    state.credentials = [];
    return;
  }
  try {
    const response = await fetch("/api/credentials", { cache: "no-store" });
    const data = await response.json();
    state.credentials = Array.isArray(data.items) ? data.items : [];
  } catch (_) {
    state.credentials = [];
  }
}

async function refresh() {
  const response = await fetch("/api/status", { cache: "no-store" });
  const data = await response.json();
  state.status = data;
  state.connected = data.connected;
  const persona = data.principal?.persona;
  const display = persona?.display || data.principal?.display_name || data.principal?.name;
  const role = persona?.role || "Approved agent session governed by Datum Sync.";
  applyPersonaIcon(persona);

  $("model-card").textContent = data.model;
  $("agent").textContent = display || "Not connected";
  $("sidebar-agent").textContent = display || "Not connected";
  $("sidebar-role").textContent = data.connected ? role : "Choose an approved Datum Sync persona.";
  $("header-persona-name").textContent = display || "Federated worker";
  $("connection-pill").classList.toggle("gov-on", data.connected);
  $("connection-pill").classList.toggle("gov-off", !data.connected);
  $("connection-label").textContent = data.connected ? "GOVERNED" : "NOT CONNECTED";
  $("connect").classList.toggle("hidden", data.connected);
  $("disconnect").classList.toggle("hidden", !data.connected);
  $("welcome-connect").classList.toggle("hidden", data.connected);
  $("input").disabled = !data.connected;
  $("input").placeholder = data.connected ? "Message this governed persona…" : "Connect an agent to begin";
  $("btn-send").disabled = !data.connected;
  $("welcome-name").textContent = display || "Connect a Datum Sync agent";
  $("welcome-role").textContent = data.connected
    ? role
    : "Choose an approved persona. Its tools and service credentials stay behind the Datum Sync MCP gateway.";
  renderPrompts(data.connected ? (persona?.starter_prompts || []) : []);
  if (data.connected) {
    trace("Agent authorized", `${display || data.principal?.name} · short-lived access granted by Datum Sync`, "ok");
  }
  await Promise.all([refreshTools(), refreshCredentials(), refreshArtifacts()]);
}

function parseSSE(buffer, handler) {
  const blocks = buffer.split("\n\n");
  const remainder = blocks.pop();
  for (const block of blocks) {
    let type = "message";
    let raw = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) type = line.slice(7);
      if (line.startsWith("data: ")) raw += line.slice(6);
    }
    if (raw) handler(type, JSON.parse(raw));
  }
  return remainder;
}

async function sendPrompt(text) {
  if (!state.connected || state.busy || !text.trim()) return;
  state.busy = true;
  $("btn-send").disabled = true;
  $("run-state").textContent = "Running";
  message("user", text.trim());
  const answer = message("assistant", "");
  trace("Turn submitted", "Codex is selecting governed tools");
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text.trim() }),
    });
    if (!response.ok) throw new Error((await response.json()).detail || "Request failed");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      buffer = parseSSE(buffer, (type, data) => {
        if (type === "delta") {
          answer.textContent += data.text;
          $("messages").scrollTop = $("messages").scrollHeight;
        } else if (type === "turn") {
          trace("Codex thread", `${data.model} · ${data.turn_id.slice(0, 12)}`, "ok");
        } else if (type === "tool") {
          const finished = data.phase === "completed";
          recordToolEvent(data);
          trace(
            `${data.tool} ${finished ? "completed" : "started"}`,
            `${data.server}${data.duration_ms ? ` · ${data.duration_ms} ms` : ""}`,
            data.has_error ? "error" : (finished ? "ok" : ""),
          );
        } else if (type === "done") {
          trace("Turn complete", data.status, data.error ? "error" : "ok");
        } else if (type === "error") {
          throw new Error(data.detail);
        }
      });
      if (done) break;
    }
    if (!answer.textContent) answer.textContent = "The worker completed without a text response.";
  } catch (error) {
    answer.textContent = `Worker error: ${error.message}`;
    trace("Worker error", error.message, "error");
  } finally {
    state.busy = false;
    $("run-state").textContent = "Idle";
    $("btn-send").disabled = !state.connected;
    await Promise.all([refreshTools(), refreshCredentials(), refreshArtifacts({ openNew: true })]);
  }
}

function node(tag, className, content) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (content !== undefined) item.textContent = content;
  return item;
}

function dataRow(label, value) {
  const row = node("div", "card-data-row");
  row.append(node("span", "", label), node("b", "", value || "—"));
  return row;
}

function chipList(items, emptyText = "None") {
  const wrap = node("div", "card-chips");
  if (!items.length) {
    wrap.append(node("span", "card-empty-chip", emptyText));
    return wrap;
  }
  for (const item of items) wrap.append(node("code", "", item));
  return wrap;
}

function cardSection(title, description, iconName = "") {
  const section = node("section", "worker-card-section");
  const heading = node("h3", iconName ? "card-heading" : "", title);
  if (iconName) {
    const icon = node("i", `ph ${iconName}`);
    icon.setAttribute("aria-hidden", "true");
    heading.prepend(icon);
  }
  section.append(heading);
  if (description) section.append(node("p", "card-description", description));
  return section;
}

function formatExpiry(value) {
  if (!value) return "Session-bound";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Session-bound" : date.toLocaleString();
}

function proxyGroups() {
  const groups = new Map();
  for (const tool of state.tools) {
    if (!tool?.name) continue;
    const info = connectionFor(tool.name);
    if (!groups.has(info.key)) groups.set(info.key, { ...info, tools: [] });
    groups.get(info.key).tools.push(tool.name);
  }
  for (const credential of state.credentials) {
    if (!credential?.connection) continue;
    if (!groups.has(credential.connection)) {
      groups.set(credential.connection, { key: credential.connection, label: credential.label || credential.connection, tools: [] });
    }
    groups.get(credential.connection).credential = credential;
  }
  return [...groups.values()].sort((a, b) => a.label.localeCompare(b.label));
}

function buildSessionCard() {
  const body = document.createDocumentFragment();
  const status = state.status || {};
  const persona = status.principal?.persona;
  const section = cardSection("This worker session", "The browser holds a short-lived Datum Sync agent token. The worker does not retain an upstream service credential.", personaIcon(persona));
  section.append(
    dataRow("Connection", state.connected ? "Governed and active" : "Not connected"),
    dataRow("Agent", persona?.display || status.principal?.display_name || status.principal?.name || "—"),
    dataRow("Model", status.model || "—"),
    dataRow("Authorization expiry", state.connected ? formatExpiry(status.authorization_expires_at) : "—"),
    dataRow("Visible tools", state.connected ? String(state.tools.length) : "0"),
  );
  body.append(section);
  const action = node("a", "worker-card-action", state.connected ? "Disconnect this session" : "Connect an agent");
  if (state.connected) {
    action.href = "#disconnect";
    action.addEventListener("click", async (event) => {
      event.preventDefault();
      await fetch("/api/disconnect", { method: "POST" });
      location.reload();
    });
  } else {
    action.href = "/connect";
  }
  body.append(action);
  return body;
}

function buildActivityCard() {
  const body = document.createDocumentFragment();
  const section = cardSection("MCP calls from this browser", "These are the governed tool calls made during the current worker session. Arguments, responses, and credentials are never retained here.", "ph-chart-bar");
  const calls = [...state.calls].reverse();
  if (!calls.length) {
    section.append(node("p", "card-empty", "No MCP tool calls yet. Send a prompt to create a deterministic trace."));
  } else {
    const list = node("div", "call-card-list");
    for (const call of calls.slice(0, 12)) {
      const item = node("article", `call-card ${call.hasError ? "error" : call.phase === "completed" ? "ok" : ""}`);
      const heading = node("strong", "", call.tool);
      const detail = node("span", "", `${connectionFor(call.tool).label} · ${call.phase}${call.durationMs !== null ? ` · ${call.durationMs} ms` : ""}`);
      item.append(heading, detail);
      list.append(item);
    }
    section.append(list);
  }
  body.append(section);
  return body;
}

function buildProxyCard() {
  const body = document.createDocumentFragment();
  const section = cardSection("Managed access visible to this agent", "Datum Sync keeps service secrets behind its proxy. This card shows tool availability and the agent's managed grants, never the secret values.", "ph-plugs");
  const groups = proxyGroups();
  if (!groups.length) {
    section.append(node("p", "card-empty", "Connect an agent to inspect its currently available MCP routes."));
  }
  for (const group of groups) {
    const item = node("article", "proxy-card");
    const title = node("div", "proxy-card-head");
    title.append(node("strong", "", group.label), node("span", group.tools.length ? "card-status live" : "card-status", group.tools.length ? "Available" : "No live tools"));
    item.append(title, dataRow("Tools available now", String(group.tools.length)));
    if (group.credential) {
      item.append(dataRow("Managed credential", group.credential.label || "Provisioned"));
      item.append(dataRow("Granted tools", String((group.credential.granted_tools || []).length)));
      if (group.credential.pending_request) item.append(node("p", "card-note", "A credential access request is awaiting review."));
    } else if (group.key === "legacy-local") {
      item.append(node("p", "card-note", "This local Datum adapter is governed by the agent policy and has no upstream secret."));
    }
    item.append(chipList(group.tools.slice(0, 8), "No callable tools"));
    section.append(item);
  }
  body.append(section);
  return body;
}

function buildPrincipalCard() {
  const body = document.createDocumentFragment();
  const status = state.status || {};
  const persona = status.principal?.persona || {};
  const display = persona.display || status.principal?.display_name || status.principal?.name || "No connected agent";
  const section = cardSection(display, state.connected
    ? "This is the current principal selected at OAuth consent. Its bundle is evaluated again by Datum Sync on each catalogue request and tool call."
    : "Connect an approved agent to inspect its identity and effective worker-visible capability.", personaIcon(persona));
  section.append(
    dataRow("Principal ID", status.principal?.name || "—"),
    dataRow("State", status.principal?.state || "—"),
    dataRow("Role", persona.role || "—"),
    dataRow("MCP connections", state.connected ? String(proxyGroups().length) : "0"),
  );
  const tools = state.tools.map((tool) => tool.name).sort();
  section.append(node("h4", "", "Callable tools"), chipList(tools, "Connect an agent to load its tool catalogue."));
  body.append(section);
  return body;
}

const CARD_VIEWS = {
  session: { title: "Agent session", kicker: "Worker state", render: buildSessionCard },
  activity: { title: "MCP activity", kicker: "This browser session", render: buildActivityCard },
  proxies: { title: "Secrets & proxies", kicker: "Managed access", render: buildProxyCard },
  principal: { title: "Principal", kicker: "Effective agent bundle", render: buildPrincipalCard },
};

function closeCard() {
  $("worker-card-overlay").classList.remove("visible");
  $("worker-card-overlay").setAttribute("aria-hidden", "true");
  document.querySelectorAll("[data-card]").forEach((button) => button.classList.remove("active"));
}

function openCard(name) {
  const view = CARD_VIEWS[name];
  if (!view) return;
  $("worker-card-title").textContent = view.title;
  $("worker-card-kicker").textContent = view.kicker;
  $("worker-card-body").replaceChildren(view.render());
  $("worker-card-overlay").classList.add("visible");
  $("worker-card-overlay").setAttribute("aria-hidden", "false");
  document.querySelectorAll("[data-card]").forEach((button) => button.classList.toggle("active", button.dataset.card === name));
  $("sidebar").classList.remove("open");
  $("sidebar-backdrop").classList.remove("visible");
  $("worker-card-close").focus();
}

function setPaneFocused(focused) {
  const pane = $("artifact-pane");
  const button = $("btn-pane-focus");
  const backdrop = $("pane-focus-backdrop");
  pane.classList.toggle("focused", focused);
  document.body.classList.toggle("pane-focused", focused);
  backdrop.classList.toggle("visible", focused);
  backdrop.setAttribute("aria-hidden", String(!focused));
  button.setAttribute("aria-pressed", String(focused));
  button.setAttribute("aria-label", focused ? "Restore split pane" : "Expand pane");
  button.title = focused ? "Restore split pane" : "Expand pane";
  button.querySelector("i").className = `ph ${focused ? "ph-corners-in" : "ph-corners-out"}`;
  if (focused) {
    pane.setAttribute("role", "dialog");
    pane.setAttribute("aria-modal", "true");
  } else {
    pane.removeAttribute("role");
    pane.removeAttribute("aria-modal");
  }
}

$("input-area").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("input").value;
  $("input").value = "";
  $("input").style.height = "auto";
  sendPrompt(value);
});
$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("input-area").requestSubmit();
  }
});
$("input").addEventListener("input", () => {
  $("input").style.height = "auto";
  $("input").style.height = `${Math.min($("input").scrollHeight, 160)}px`;
});
$("disconnect").addEventListener("click", async () => {
  await fetch("/api/disconnect", { method: "POST" });
  location.reload();
});
$("btn-menu").addEventListener("click", () => {
  $("sidebar").classList.toggle("open");
  $("sidebar-backdrop").classList.toggle("visible");
});
$("sidebar-backdrop").addEventListener("click", () => {
  $("sidebar").classList.remove("open");
  $("sidebar-backdrop").classList.remove("visible");
});
$("btn-trace").addEventListener("click", () => {
  const pane = $("artifact-pane");
  if (pane.classList.contains("open")) setPaneFocused(false);
  pane.classList.toggle("open");
  $("body").classList.toggle("artifacts-open", pane.classList.contains("open"));
  $("btn-trace").classList.toggle("active", pane.classList.contains("open"));
});
$("btn-pane-focus").addEventListener("click", () => {
  const focused = !$("artifact-pane").classList.contains("focused");
  setPaneFocused(focused);
  $("btn-pane-focus").focus();
});
$("pane-focus-backdrop").addEventListener("click", () => setPaneFocused(false));
$("btn-pane-close").addEventListener("click", () => $("btn-trace").click());
document.querySelectorAll(".pane-mode").forEach((button) => {
  button.addEventListener("click", () => switchPaneMode(button.dataset.mode));
});
document.querySelectorAll("[data-card]").forEach((button) => button.addEventListener("click", () => openCard(button.dataset.card)));
$("worker-card-close").addEventListener("click", closeCard);
$("worker-card-overlay").addEventListener("click", (event) => { if (event.target === $("worker-card-overlay")) closeCard(); });
document.addEventListener("keydown", (event) => {
  const pane = $("artifact-pane");
  if (event.key === "Escape" && pane.classList.contains("focused")) {
    setPaneFocused(false);
    $("btn-pane-focus").focus();
    return;
  }
  if (event.key === "Escape") closeCard();
  if (event.key !== "Tab" || !pane.classList.contains("focused")) return;
  const controls = [...pane.querySelectorAll("button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled])")];
  if (!controls.length) return;
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

refresh().catch(() => trace("Status unavailable", "Start Datum Sync and try again", "error"));
setInterval(() => { if (state.connected && !state.busy) refreshArtifacts({ openNew: true }); }, 5000);
