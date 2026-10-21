const state = {
  status: null,
  events: [],
  nodePage: document.querySelector("[data-node-page]")?.dataset.nodePage || null,
  eventFilter: new Set(["client", "process", "log", "storage", "ui"]),
  darkMode: false,
  lastLinkState: {},
  failoverHold: {
    clientA: false,
    clientB: false,
    replFwd: false,
    replRev: false,
  },
  lastRouteLabels: { a: "client to Host A", b: "standby failover to Host B", repl: "replication + ACK" },
};

const roleClass = (role) => {
  if (role === "PRIMARY") return "primary";
  if (role === "BACKUP") return "backup";
  if (role === "STARTING") return "starting";
  if (role === "PORT BUSY") return "busy";
  return "";
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function refreshStatus() {
  state.status = await api("/api/status");
  renderStatus();
}

function renderStatus() {
  if (!state.status) return;
  for (const [name, node] of Object.entries(state.status.nodes)) {
    renderNode(name, node);
    renderFiles(name, node.files || []);
  }
  renderMetrics();
  renderLinks();
  renderClientHealth();
  renderControls();
  renderNodePageHeading();
}

function renderMetrics() {
  if (!state.status) return;
  const primary = state.status.nodes.primary;
  const backup = state.status.nodes.backup;
  const fileTotal = new Set([
    ...(primary.files || []).map((file) => file.path),
    ...(backup.files || []).map((file) => file.path),
  ]).size;
  setText("#metric-primary", primary.role);
  setText("#metric-backup", backup.role);
  setText("#metric-files", String(fileTotal));
  setText("#metric-event", state.events[0]?.message || "None");

  const mirrorState = document.querySelector("#mirror-state");
  if (mirrorState) {
    const primaryFiles = (primary.files || []).map((file) => file.path).sort().join("|");
    const backupFiles = (backup.files || []).map((file) => file.path).sort().join("|");
    if (!primary.running && !backup.running) mirrorState.textContent = "Both hosts are down";
    else if (primaryFiles === backupFiles) mirrorState.textContent = "Mirrors match";
    else mirrorState.textContent = "Mirrors differ";
  }
}

function setClassIfChanged(selector, className, condition) {
  const el = typeof selector === "string" ? document.querySelector(selector) : selector;
  if (!el) return;
  const has = el.classList.contains(className);
  if (has === condition) return;
  el.classList.toggle(className, condition);
}

function renderLinks() {
  const primaryUp = state.status.nodes.primary?.running;
  const backupUp = state.status.nodes.backup?.running;
  const activePrimary = state.status.nodes.primary?.role === "PRIMARY";
  const activeBackup = state.status.nodes.backup?.role === "PRIMARY";
  const hostAIsBackup = state.status.nodes.primary?.role === "BACKUP";
  const hostBIsBackup = state.status.nodes.backup?.role === "BACKUP";
  const forwardReplication = primaryUp && backupUp && activePrimary && hostBIsBackup;
  const reverseReplication = primaryUp && backupUp && activeBackup && hostAIsBackup;
  const replicationActive = forwardReplication || reverseReplication;
  const clientActive = activePrimary || activeBackup;

  const hold = state.failoverHold;
  const prev = state.lastLinkState || {};

  // Set hold when a client route drops but the peer is still alive (failover in progress)
  if (prev.activePrimary && !activePrimary && backupUp) hold.clientA = true;
  if (prev.activeBackup && !activeBackup && primaryUp) hold.clientB = true;

  // Clear client hold when the opposite route becomes active or peer dies
  if (activeBackup || !backupUp) hold.clientA = false;
  if (activePrimary || !primaryUp) hold.clientB = false;

  // Visual state: actual OR hold for client routes only
  // Replication direction changes immediately with role switch; no hold needed
  const visPrimary = activePrimary || hold.clientA;
  const visBackup = activeBackup || hold.clientB;
  const visReplFwd = forwardReplication;
  const visReplRev = reverseReplication;
  const visClientActive = visPrimary || visBackup;
  const visReplication = visReplFwd || visReplRev;

  // Instant off when entire cluster is shut down
  const allDown = !primaryUp && !backupUp;
  if (allDown) {
    hold.clientA = false;
    hold.clientB = false;
  }
  setClassIfChanged(".topology", "shutdown", allDown);

  setClassIfChanged(".client-link", "active", visPrimary);
  setClassIfChanged(".failover-route", "active", visBackup);
  setClassIfChanged(".repl-forward", "active", visReplFwd);
  setClassIfChanged(".repl-reverse", "active", visReplRev);
  setClassIfChanged(".topology", "failover-active", visBackup);
  setClassIfChanged(".topology", "normal-active", visPrimary);
  setClassIfChanged(".topology", "replication-active", visReplication);
  setClassIfChanged(".topology", "replication-reverse", visReplRev);
  setClassIfChanged("#client-node", "client-active", visClientActive);

  // Particles only when genuinely active (not during hold)
  setClassIfChanged("#particle-group-client-a", "active", activePrimary);
  setClassIfChanged("#particle-group-client-b", "active", activeBackup);
  setClassIfChanged("#particle-group-repl-fwd", "active", forwardReplication);
  setClassIfChanged("#particle-group-repl-rev", "active", reverseReplication);

  state.lastLinkState = {
    activePrimary, activeBackup, forwardReplication, reverseReplication,
    replicationActive, clientActive,
  };

  renderRouteLabels(activePrimary, activeBackup, forwardReplication, reverseReplication);
}

function renderRouteLabels(activePrimary, activeBackup, forwardReplication, reverseReplication) {
  const labels = state.lastRouteLabels;

  if (activePrimary) {
    labels.a = "client to Host A";
    labels.b = "standby failover to Host B";
  } else if (activeBackup) {
    labels.a = "standby failover to Host A";
    labels.b = "client to Host B";
  }
  // During the gap (no primary), keep the last known labels — don't snap to defaults

  if (forwardReplication) labels.repl = "replication + ACK A -> B";
  else if (reverseReplication) labels.repl = "replication + ACK B -> A";
  else labels.repl = "replication + ACK";

  setText("#route-label-a", labels.a);
  setText("#route-label-b", labels.b);
  setText("#route-label-repl", labels.repl);
}

function renderClientHealth() {
  const target = document.querySelector("#client-health");
  if (!target || !state.status) return;
  const primary = state.status.nodes.primary.role;
  const backup = state.status.nodes.backup.role;
  if (primary === "PRIMARY") target.textContent = "Requests go to Host A";
  else if (backup === "PRIMARY") target.textContent = "Requests fail over to Host B";
  else target.textContent = "No primary available";
}

function renderNode(name, node) {
  const card = document.querySelector(`#node-${name}`);
  if (!card) return;
  const role = card.querySelector(".role");
  const meta = card.querySelector(".meta");
  const rc = roleClass(node.role);
  if (role) {
    if (role.textContent !== node.role) role.textContent = node.role;
    const expectedClass = `role ${rc}`;
    if (role.className !== expectedClass) role.className = expectedClass;
  }
  if (meta) {
    const html = [
      `PID: ${node.pid || "none"}`,
      `Client: 127.0.0.1:${node.client_port}`,
      `Replication: 127.0.0.1:${node.repl_port}`,
      `Files: ${(node.files || []).length}`,
    ].join("<br>");
    if (meta.innerHTML !== html) meta.innerHTML = html;
  }
  const isPrimary = node.role === "PRIMARY";
  const isBackup = node.role === "BACKUP";
  if (card.classList.contains("is-running") !== node.running) card.classList.toggle("is-running", node.running);
  if (card.classList.contains("is-primary") !== isPrimary) card.classList.toggle("is-primary", isPrimary);
  if (card.classList.contains("is-backup") !== isBackup) card.classList.toggle("is-backup", isBackup);
}

function renderControls() {
  if (!state.status) return;
  const primary = state.status.nodes.primary;
  const backup = state.status.nodes.backup;
  const clusterRunning = primary.running || backup.running;
  const currentPrimary = currentPrimaryNode();
  const failedNode = downNode();
  document.querySelectorAll("[data-cluster-action='start-cluster']").forEach((button) => {
    button.disabled = clusterRunning;
  });
  document.querySelectorAll("#crash-primary").forEach((button) => {
    button.disabled = !currentPrimary;
    button.textContent = currentPrimary ? `Crash ${nodeLabel(currentPrimary)}` : "Crash Primary";
  });
  document.querySelectorAll("#recover-failed").forEach((button) => {
    button.disabled = !failedNode;
    button.textContent = failedNode ? `Recover ${nodeLabel(failedNode)}` : "Recover Failed Host";
  });
  document.querySelectorAll("[data-action='start'][data-node='primary']").forEach((button) => {
    button.disabled = primary.running;
  });
  document.querySelectorAll("[data-action='start'][data-node='backup']").forEach((button) => {
    button.disabled = backup.running;
  });
}

function renderNodePageHeading() {
  if (!state.status || !state.nodePage) return;
  const node = state.status.nodes[state.nodePage];
  if (!node) return;
  setText("#node-page-title", nodeLabel(state.nodePage));
  setText("#node-page-role", `Current role: ${node.role}`);
}

function renderFiles(name, files) {
  const list = document.querySelector(`#files-${name}`);
  if (!list) return;
  if (!files.length) {
    list.innerHTML = `<li class="empty">No files on this host</li>`;
    return;
  }
  list.innerHTML = files.map((file) => {
    const safePath = escapeHtml(file.path);
    const safePreview = escapeHtml(file.preview || "");
    return (
      '<li data-file-path="' + safePath + '" data-file-content="' + safePreview + '">' +
        '<div class="file-row">' +
          '<strong>' + safePath + '</strong>' +
          '<span>' + file.bytes + ' bytes</span>' +
        '</div>' +
        '<code>' + (safePreview || "&nbsp;") + '</code>' +
      '</li>'
    );
  }).join("");

  list.querySelectorAll("li[data-file-path]").forEach((li) => {
    li.addEventListener("click", () => {
      openFileModal(li.dataset.filePath, li.dataset.fileContent);
    });
  });
}

function addEvent(event) {
  state.events.unshift(event);
  state.events = state.events.slice(0, 100);
  renderEvents();
  renderMetrics();
}

function renderEvents() {
  const list = document.querySelector("#events");
  if (!list) return;
  const filtered = state.events.filter((e) => state.eventFilter.has(e.kind));
  if (!filtered.length) {
    list.innerHTML = `<li class="empty">No events yet</li>`;
    return;
  }
  list.innerHTML = filtered.map((event, index) => {
    const node = event.node ? `<span class="event-node">${escapeHtml(event.node)}</span>` : "";
    const kind = escapeHtml(event.kind || "event");
    return `
      <li class="event ${kind}" style="animation-delay: ${Math.min(index * 30, 300)}ms">
        <span class="event-time">${event.time}</span>
        ${node}
        <span class="event-kind">${kind}</span>
        <span class="event-message">${escapeHtml(event.message)}</span>
      </li>`;
  }).join("");
}

function wireSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws/events`);
  socket.onmessage = (message) => addEvent(JSON.parse(message.data));
  socket.onclose = () => setTimeout(wireSocket, 1000);
}

function wireButtons() {
  document.querySelectorAll("[data-action][data-node]").forEach((button) => {
    button.addEventListener("click", async () => runButton(button, async () => {
      await api(`/api/${button.dataset.action}/${button.dataset.node}`, { method: "POST" });
    }));
  });

  document.querySelectorAll("[data-cluster-action]").forEach((button) => {
    button.addEventListener("click", async () => runButton(button, async () => {
      await api(`/api/${button.dataset.clusterAction}`, { method: "POST" });
    }));
  });

  document.querySelector("#crash-primary")?.addEventListener("click", async (event) => {
    await runButton(event.currentTarget, async () => {
      const node = currentPrimaryNode();
      if (!node) throw new Error("No primary host is currently running.");
      await api(`/api/stop/${node}`, { method: "POST" });
    });
  });

  document.querySelector("#recover-failed")?.addEventListener("click", async (event) => {
    await runButton(event.currentTarget, async () => {
      const node = downNode();
      if (!node) throw new Error("No failed host to recover.");
      await api(`/api/start/${node}`, { method: "POST" });
    });
  });

  document.querySelector("#reset-demo")?.addEventListener("click", async (event) => {
    await runButton(event.currentTarget, async () => api("/api/reset", { method: "POST" }));
  });

  document.querySelector("#clear-events")?.addEventListener("click", () => {
    state.events = [];
    renderEvents();
    renderMetrics();
    toast("Events cleared", "info");
  });

  document.querySelector("#operation-select")?.addEventListener("change", updateOperationForm);
  document.querySelector("#confirm-operation")?.addEventListener("click", clientRequest);

  const pathInput = document.querySelector("#client-path");
  if (pathInput) {
    pathInput.addEventListener("blur", () => {
      const action = document.querySelector("#operation-select")?.value;
      if (action === "update") {
        updateOperationForm();
      }
    });
  }

  updateOperationForm();
}

async function runButton(button, action) {
  button.disabled = true;
  button.classList.add("working");
  const original = button.textContent;
  button.textContent = "Working";
  try {
    await action();
    await refreshStatus();
    toast(`${original} completed`, "success");
  } catch (error) {
    showResult(String(error), false);
    toast(`${original} failed`, "error");
  } finally {
    button.textContent = original;
    button.classList.remove("working");
    button.disabled = false;
    renderControls();
  }
}

async function clientRequest() {
  const action = document.querySelector("#operation-select")?.value || "write";
  const path = document.querySelector("#client-path")?.value.trim();
  const content = document.querySelector("#client-content")?.value || "";
  if (!path) {
    showResult("Path is required.", false);
    toast("Path is required", "error");
    return;
  }
  // For WRITE: warn before overwriting an existing file
  if (action === "write") {
    try {
      const check = await api("/api/client/read", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      if (check.ok) {
        const confirmed = confirm(`File "${path}" already exists. Overwrite?`);
        if (!confirmed) {
          showResult("Write cancelled — file was not modified.", false);
          return;
        }
      }
    } catch {
      // ignore check errors, proceed with write
    }
  }

  const body = action === "write" || action === "update" ? { path, content } : { path };
  showResult("Sending request...", true);
  try {
    const response = await api(`/api/client/${action}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    showResult(response.raw, response.ok);
    if (response.ok) {
      toast(`${action.toUpperCase()} ${path} succeeded`, "success");
    } else {
      toast(`${action.toUpperCase()} ${path} failed`, "error");
    }
    await refreshStatus();
  } catch (error) {
    showResult(String(error), false);
    toast(`${action.toUpperCase()} failed`, "error");
  }
}

async function updateOperationForm() {
  const action = document.querySelector("#operation-select")?.value || "write";
  const contentField = document.querySelector("#content-field");
  const confirm = document.querySelector("#confirm-operation");
  const pathInput = document.querySelector("#client-path");
  const contentInput = document.querySelector("#client-content");

  if (contentField) {
    contentField.hidden = action !== "write" && action !== "update";
  }
  if (confirm) {
    confirm.textContent = `Confirm ${action.toUpperCase()}`;
    confirm.classList.toggle("danger", action === "delete");
    confirm.classList.toggle("secondary", action === "read" || action === "update");
  }

  // Clear stale result message when switching away from UPDATE
  if (action !== "update") {
    const result = document.querySelector("#client-result");
    if (result && result.textContent === "Current content loaded. Edit and confirm to overwrite.") {
      result.textContent = "No request sent.";
      result.classList.remove("error");
    }
    return;
  }

  // For UPDATE: auto-fetch current content when path is entered
  if (pathInput && contentInput) {
    const path = pathInput.value.trim();
    if (path) {
      try {
        const response = await api("/api/client/read", {
          method: "POST",
          body: JSON.stringify({ path }),
        });
        if (response.ok) {
          contentInput.value = response.payload;
          contentInput.placeholder = "Edit content to overwrite...";
          showResult("Current content loaded. Edit and confirm to overwrite.", true);
        } else {
          showResult(response.raw, false);
        }
      } catch {
        // silent fail — user can still type manually
      }
    }
  }
}

function showResult(message, ok = true) {
  const result = document.querySelector("#client-result");
  if (!result) return;
  result.textContent = message;
  result.classList.toggle("error", !ok);
}

function setText(selector, value) {
  const node = document.querySelector(selector);
  if (node) node.textContent = value;
}

function currentPrimaryNode() {
  if (!state.status) return null;
  if (state.status.nodes.primary?.role === "PRIMARY") return "primary";
  if (state.status.nodes.backup?.role === "PRIMARY") return "backup";
  return null;
}

function downNode() {
  if (!state.status) return null;
  if (!state.status.nodes.primary?.running) return "primary";
  if (!state.status.nodes.backup?.running) return "backup";
  return null;
}

function nodeLabel(node) {
  if (node === "primary") return "Host A";
  if (node === "backup") return "Host B";
  return "Host";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/* Toast notifications */
function toast(message, type = "info") {
  const container = document.querySelector("#toast-container");
  if (!container) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  el.addEventListener("animationend", (e) => {
    if (e.animationName.includes("toast-out")) {
      el.remove();
    }
  });
}

/* Dark mode — initial theme already set inline in <head> */
function initTheme() {
  state.darkMode = document.documentElement.dataset.theme === "dark";

  document.querySelector("#theme-toggle")?.addEventListener("click", () => {
    const isDark = document.documentElement.dataset.theme === "dark";
    setTheme(!isDark);
  });

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (!localStorage.getItem("theme")) {
      setTheme(e.matches);
    }
  });
}

function setTheme(dark) {
  state.darkMode = dark;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  localStorage.setItem("theme", dark ? "dark" : "light");
}

/* File modal */
function openFileModal(path, content) {
  const modal = document.querySelector("#file-modal");
  const title = document.querySelector("#file-modal-title");
  const body = document.querySelector("#file-modal-content");
  if (!modal || !title || !body) return;
  title.textContent = path;
  body.textContent = content || "(empty file)";
  modal.hidden = false;
}

function closeModals() {
  document.querySelectorAll(".modal").forEach((m) => (m.hidden = true));
}

function initModals() {
  document.querySelectorAll(".modal-close, .modal-backdrop").forEach((el) => {
    el.addEventListener("click", closeModals);
  });
}

/* Keyboard shortcuts */
function initKeyboard() {
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") {
      if (e.key === "Escape") {
        e.target.blur();
        closeModals();
      }
      return;
    }

    const key = e.key.toLowerCase();
    if (key === "?") {
      e.preventDefault();
      document.querySelector("#shortcuts-modal").hidden = false;
      return;
    }
    if (key === "escape") {
      closeModals();
      state.events = [];
      renderEvents();
      renderMetrics();
      return;
    }
    if (key === "t") {
      e.preventDefault();
      setTheme(!state.darkMode);
      return;
    }
    if (key === "s") {
      e.preventDefault();
      document.querySelector("[data-cluster-action='start-cluster']")?.click();
      return;
    }
    if (key === "x") {
      e.preventDefault();
      document.querySelector("[data-cluster-action='stop-cluster']")?.click();
      return;
    }
    if (key === "c") {
      e.preventDefault();
      document.querySelector("#crash-primary")?.click();
      return;
    }
    if (key === "r") {
      e.preventDefault();
      document.querySelector("#recover-failed")?.click();
      return;
    }
  });

  document.querySelector(".shortcut-hint")?.addEventListener("click", () => {
    document.querySelector("#shortcuts-modal").hidden = false;
  });
}

/* Event filters */
function initEventFilters() {
  const container = document.querySelector("#event-filters");
  if (!container) return;
  const kinds = [
    { key: "client", label: "Client", color: "#2f5f9f" },
    { key: "process", label: "Process", color: "#17825d" },
    { key: "log", label: "Log", color: "#a36b13" },
    { key: "storage", label: "Storage", color: "#607083" },
    { key: "ui", label: "UI", color: "#08756d" },
  ];
  container.innerHTML = kinds.map((k) => `
    <button class="event-filter active" data-kind="${k.key}" title="Toggle ${k.label} events">
      <span class="filter-dot" style="background:${k.color}"></span>
      ${k.label}
    </button>
  `).join("");

  container.querySelectorAll(".event-filter").forEach((btn) => {
    const kind = btn.dataset.kind;
    btn.classList.toggle("active", state.eventFilter.has(kind));
    btn.addEventListener("click", () => {
      if (state.eventFilter.has(kind)) {
        state.eventFilter.delete(kind);
        btn.classList.remove("active");
      } else {
        state.eventFilter.add(kind);
        btn.classList.add("active");
      }
      renderEvents();
    });
  });
}

/* SPA navigation — intercept link clicks to avoid full page reloads */
function initSpaNavigation() {
  document.addEventListener("click", (e) => {
    const link = e.target.closest("a");
    if (!link) return;
    if (link.target || link.getAttribute("href")?.startsWith("#")) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (!link.href.startsWith(window.location.origin)) return;
    e.preventDefault();
    navigateTo(link.href);
  });

  window.addEventListener("popstate", () => {
    navigateTo(window.location.href, false);
  });
}

async function navigateTo(url, push = true) {
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error("HTTP " + response.status);
    const html = await response.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, "text/html");

    const newMain = doc.querySelector("main");
    const currentMain = document.querySelector("main");
    if (newMain && currentMain) {
      currentMain.innerHTML = newMain.innerHTML;
    }

    const newTitle = doc.querySelector("title")?.textContent;
    if (newTitle) document.title = newTitle;

    const newBodyPage = doc.body?.dataset?.page;
    if (newBodyPage) document.body.dataset.page = newBodyPage;
    state.nodePage = document.querySelector("[data-node-page]")?.dataset.nodePage || null;

    const path = new URL(url).pathname;
    document.querySelectorAll("nav a").forEach((a) => {
      a.classList.toggle("active", a.getAttribute("href") === path);
    });

    wireButtons();
    initEventFilters();
    renderStatus();
    renderEvents();
    if (push) history.pushState(null, "", url);
  } catch {
    window.location.href = url;
  }
}

initTheme();
initModals();
initKeyboard();
initEventFilters();
wireButtons();
wireSocket();
initSpaNavigation();
refreshStatus();
setInterval(refreshStatus, 1500);
