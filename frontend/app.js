// FreeAI control room — talks to FastAPI backend at API_BASE
const API_BASE = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1"
  ? `http://${window.location.hostname}:8000`
  : window.location.origin.replace(/:\d+$/, ":8000");

document.getElementById("apiBase").textContent = API_BASE;

const STRATEGY_DESCRIPTIONS = {
  auto:         "Reads the prompt and picks a lane.",
  fastest:      "Lowest expected latency.",
  cheapest:     "Most generous free quotas.",
  best_quality: "Highest-rated reasoning models.",
  coding:       "Tuned for code, tracebacks, refactors.",
  reasoning:    "Multi-step thinking and explanations.",
  vision:       "Image-capable providers only.",
  long_context: "Large context windows.",
};

const TOKEN_KEY = "freeai_admin_token";

// ─────────────── admin token gate ───────────────

function getAdminToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function setAdminToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  updateLockUI();
}

function updateLockUI() {
  const btn = document.getElementById("lockButton");
  const icon = document.getElementById("lockIcon");
  if (getAdminToken()) {
    btn.classList.remove("is-locked");
    icon.textContent = "⛓";
    btn.title = "admin token saved (click to clear)";
  } else {
    btn.classList.add("is-locked");
    icon.textContent = "⛒";
    btn.title = "admin token not set (click to enter)";
  }
}

function showAdminModal() {
  document.getElementById("adminModal").hidden = false;
  setTimeout(() => document.getElementById("adminTokenInput").focus(), 50);
}
function hideAdminModal() {
  document.getElementById("adminModal").hidden = true;
}

document.getElementById("lockButton").addEventListener("click", () => {
  if (getAdminToken()) {
    if (confirm("Clear admin token from this browser?")) setAdminToken("");
    return;
  }
  showAdminModal();
});

document.getElementById("adminTokenSave").addEventListener("click", async () => {
  const token = document.getElementById("adminTokenInput").value.trim();
  if (!token) return;
  setAdminToken(token);
  hideAdminModal();
  document.getElementById("adminTokenInput").value = "";
  await boot();
});

document.getElementById("adminTokenInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("adminTokenSave").click();
});

// ─────────────── first-run setup (configuración inicial) ───────────────

function showSetupModal() {
  document.getElementById("setupModal").hidden = false;
}
function hideSetupModal() {
  document.getElementById("setupModal").hidden = true;
}

function buildSetupProviderFields(names) {
  const wrap = document.getElementById("setupProviderFields");
  wrap.innerHTML = "";
  names.forEach((n) => {
    const label = document.createElement("label");
    label.className = "field";
    const cap = document.createElement("span");
    cap.className = "field__label";
    cap.innerHTML = `${n.toUpperCase().replace(/_/g, " ")} <em>(opcional)</em>`;
    const input = document.createElement("input");
    input.type = "password";
    input.dataset.provider = n;
    input.className = "key-input";
    input.autocomplete = "off";
    input.placeholder = "pegar clave API…";
    label.appendChild(cap);
    label.appendChild(input);
    wrap.appendChild(label);
  });
}

function openFirstRunSetup(providerNames) {
  buildSetupProviderFields(providerNames || []);
  const err = document.getElementById("setupError");
  err.hidden = true;
  err.textContent = "";
  document.getElementById("setupAdminToken").value = "";
  document.getElementById("setupAdminToken2").value = "";
  showSetupModal();
  setTimeout(() => document.getElementById("setupAdminToken").focus(), 50);
}

document.getElementById("setupGenToken").addEventListener("click", () => {
  const a = new Uint8Array(22);
  crypto.getRandomValues(a);
  let s = "";
  a.forEach((b) => {
    s += (`0${b.toString(16)}`).slice(-2);
  });
  const tok = `adm_${s}`;
  document.getElementById("setupAdminToken").value = tok;
  document.getElementById("setupAdminToken2").value = tok;
});

document.getElementById("setupSubmit").addEventListener("click", async () => {
  const err = document.getElementById("setupError");
  err.hidden = true;
  const t1 = document.getElementById("setupAdminToken").value.trim();
  const t2 = document.getElementById("setupAdminToken2").value.trim();
  if (t1.length < 12) {
    err.textContent = "El token debe tener al menos 12 caracteres.";
    err.hidden = false;
    return;
  }
  if (t1 !== t2) {
    err.textContent = "Los dos campos del token no coinciden.";
    err.hidden = false;
    return;
  }
  const keys = {};
  document.querySelectorAll("#setupProviderFields input[data-provider]").forEach((el) => {
    const v = el.value.trim();
    if (v) keys[el.dataset.provider] = v;
  });
  try {
    const res = await fetch(`${API_BASE}/api/setup/initial`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        admin_token: t1,
        admin_token_confirm: t2,
        provider_keys: keys,
      }),
    });
    if (res.status === 403) {
      err.textContent =
        "La configuración inicial ya no está disponible. Usa el candado e introduce tu token de administrador.";
      err.hidden = false;
      hideSetupModal();
      await boot();
      return;
    }
    if (!res.ok) {
      let msg = `${res.status}`;
      try {
        const j = await res.json();
        if (Array.isArray(j.detail)) msg = j.detail.map((d) => d.msg || d).join("; ");
        else if (typeof j.detail === "string") msg = j.detail;
        else msg = JSON.stringify(j);
      } catch {
        msg = await res.text();
      }
      err.textContent = msg;
      err.hidden = false;
      return;
    }
    const body = await res.json().catch(() => ({}));
    setAdminToken(t1);
    hideSetupModal();
    if (body.detail) window.alert(body.detail);
    await boot();
  } catch (e) {
    err.textContent = String(e.message || e);
    err.hidden = false;
  }
});

// ─────────────── tab switcher ───────────────

const ribbon = document.querySelectorAll(".ribbon__item");
const panels = document.querySelectorAll(".panel");
ribbon.forEach((tab) => {
  tab.addEventListener("click", () => {
    ribbon.forEach((t) => t.classList.remove("ribbon__item--active"));
    tab.classList.add("ribbon__item--active");
    panels.forEach((p) => p.classList.remove("panel--active"));
    document.querySelector(`[data-panel="${tab.dataset.tab}"]`).classList.add("panel--active");
    if (tab.dataset.tab === "clients") refreshClients();
    if (tab.dataset.tab === "analytics") refreshAnalytics();
    if (tab.dataset.tab === "strategy") refreshStrategies();
  });
});

// ─────────────── http helpers ───────────────

class AuthError extends Error {}

async function adminApi(path, opts = {}) {
  const token = getAdminToken();
  const headers = {
    "Content-Type": "application/json",
    ...(opts.headers || {}),
  };
  if (token) headers["X-Admin-Token"] = token;
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (res.status === 401) throw new AuthError("admin auth required");
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  if (res.status === 204) return null;
  return res.json();
}

async function publicApi(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

function showModelWarning(cardNode, warning, suggestions, modelInput) {
  cardNode.querySelector(".model-warning")?.remove();
  if (!warning) return;
  const wrap = document.createElement("div");
  wrap.className = "model-warning";
  wrap.innerHTML = `
    <b>HEADS UP:</b> ${escapeHtml(warning)}
    ${suggestions?.length ? `<div class="model-warning__suggestions">did you mean: ${
      suggestions.map((s) => `<span data-model="${escapeHtml(s)}">${escapeHtml(s)}</span>`).join(" · ")
    }</div>` : ""}
  `;
  wrap.querySelectorAll("[data-model]").forEach((s) => {
    s.addEventListener("click", () => {
      modelInput.value = s.dataset.model;
      wrap.remove();
    });
  });
  cardNode.querySelector(".provider-card__rows").appendChild(wrap);
}

function flashButton(btn, text) {
  const original = btn.textContent;
  btn.textContent = text;
  btn.style.background = "var(--amber)";
  btn.style.color = "var(--char)";
  setTimeout(() => {
    btn.textContent = original;
    btn.style.background = "";
    btn.style.color = "";
  }, 1100);
}

// ─────────────── system clock ───────────────

function tickDate() {
  const d = new Date();
  document.getElementById("systemDate").textContent =
    d.toISOString().slice(0, 19).replace("T", " ") + " UTC";
}
setInterval(tickDate, 1000);
tickDate();

// ─────────────── providers panel ───────────────

const grid = document.getElementById("providerGrid");
const tpl  = document.getElementById("providerCardTemplate");

let providersCache = [];

function renderProvider(p) {
  const node = tpl.content.firstElementChild.cloneNode(true);
  node.dataset.name = p.name;
  node.querySelector(".provider-card__name").textContent = p.name;

  const statusLabel = node.querySelector(".provider-card__status-label");
  const statusDot   = node.querySelector(".provider-card__status .dot");
  if (!p.has_key) {
    statusLabel.textContent = "no key";
    statusDot.classList.add("dot--idle");
    node.classList.add("is-disabled");
  } else if (!p.healthy) {
    statusLabel.textContent = "quarantined";
    statusDot.classList.add("dot--down");
    node.classList.add("is-error");
  } else if (!p.enabled) {
    statusLabel.textContent = "disabled";
    statusDot.classList.add("dot--idle");
    node.classList.add("is-disabled");
  } else {
    statusLabel.textContent = "live";
  }

  const tags = node.querySelector(".provider-card__tags");
  p.tags.forEach((t) => {
    const el = document.createElement("span");
    el.className = "tag";
    el.textContent = t;
    tags.appendChild(el);
  });

  const keyInput = node.querySelector(".key-input");
  if (p.has_key) keyInput.placeholder = "•••••••• key on file";

  const modelInput = node.querySelector(".model-input");
  modelInput.value = p.default_model || "";

  // meters
  const rpmFill  = node.querySelector(".meter:nth-child(1) .meter__fill");
  const rpmValue = node.querySelector(".rpm-value");
  if (p.rpm_limit) {
    const pct = Math.min(100, (p.requests_this_minute / p.rpm_limit) * 100);
    rpmFill.style.right = `${100 - pct}%`;
    if (pct > 80) rpmFill.classList.add("is-warn");
    rpmValue.textContent = `${p.requests_this_minute}/${p.rpm_limit}`;
  } else {
    rpmFill.style.right = "100%";
    rpmValue.textContent = `${p.requests_this_minute}/—`;
  }

  const rpdFill  = node.querySelector(".meter:nth-child(2) .meter__fill");
  const rpdValue = node.querySelector(".rpd-value");
  if (p.rpd_limit) {
    const pct = Math.min(100, (p.requests_today / p.rpd_limit) * 100);
    rpdFill.style.right = `${100 - pct}%`;
    if (pct > 80) rpdFill.classList.add("is-warn");
    rpdValue.textContent = `${p.requests_today}/${p.rpd_limit}`;
  } else {
    rpdFill.style.right = "100%";
    rpdValue.textContent = `${p.requests_today}/—`;
  }

  node.querySelector(".latency-value").textContent =
    p.last_latency_ms != null ? `${p.last_latency_ms} ms` : "—";

  if (p.last_error) {
    const errRow = node.querySelector(".provider-row--error");
    errRow.hidden = false;
    errRow.querySelector(".error-value").textContent = p.last_error;
  }

  const enableToggle = node.querySelector(".enable-toggle");
  enableToggle.checked = p.enabled;
  enableToggle.addEventListener("change", async (e) => {
    try {
      await adminApi(`/api/providers/${p.name}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: e.target.checked }),
      });
      refreshProviders();
    } catch (err) {
      if (err instanceof AuthError) showAdminModal();
    }
  });

  node.querySelector(".save-key").addEventListener("click", async () => {
    const body = {};
    if (keyInput.value) body.api_key = keyInput.value;
    if (modelInput.value !== p.default_model) body.default_model = modelInput.value;
    if (!Object.keys(body).length) return flashButton(node.querySelector(".save-key"), "NO CHANGE");
    try {
      const result = await adminApi(`/api/providers/${p.name}`, { method: "PATCH", body: JSON.stringify(body) });
      flashButton(node.querySelector(".save-key"), "SAVED");
      keyInput.value = "";
      // Show the model warning inline if the backend sent one
      showModelWarning(node, result?.model_warning, result?.model_suggestions, modelInput);
      refreshProviders();
    } catch (err) {
      if (err instanceof AuthError) { showAdminModal(); return; }
      flashButton(node.querySelector(".save-key"), "ERROR");
    }
  });

  node.querySelector(".reset-btn").addEventListener("click", async () => {
    try {
      await adminApi(`/api/providers/${p.name}/reset`, { method: "POST" });
      refreshProviders();
    } catch (err) {
      if (err instanceof AuthError) showAdminModal();
    }
  });

  return node;
}

async function refreshProviders() {
  try {
    const data = await adminApi("/api/providers");
    providersCache = data;
    grid.innerHTML = "";
    data.forEach((p) => grid.appendChild(renderProvider(p)));
    updateUplink(true, data);
    updateProviderSelect(data);
  } catch (err) {
    if (err instanceof AuthError) {
      updateUplink(false);
      grid.innerHTML = `<div class="output-empty"><pre>admin token required\nclick the lock icon</pre></div>`;
      showAdminModal();
      return;
    }
    updateUplink(false);
    grid.innerHTML = `<div class="output-empty"><pre>uplink down — is the backend running?\n${API_BASE}</pre></div>`;
  }
}

function updateUplink(ok, data) {
  const el = document.getElementById("uplinkStatus");
  if (!ok || !data) {
    el.innerHTML = `<span class="dot dot--down"></span> OFFLINE`;
    document.getElementById("providersOnline").textContent = "0 / 0";
    return;
  }
  el.innerHTML = `<span class="dot"></span> ONLINE`;
  const online = data.filter((p) => p.has_key && p.enabled && p.healthy).length;
  document.getElementById("providersOnline").textContent = `${online} / ${data.length}`;
}

document.getElementById("refreshProviders").addEventListener("click", refreshProviders);

// ─────────────── strategy panel ───────────────

const strategyGrid = document.getElementById("strategyGrid");
let configCache = null;
let strategiesCache = [];

async function refreshConfig() {
  try {
    configCache = await adminApi("/api/config");
  } catch (err) {
    if (err instanceof AuthError) showAdminModal();
    return;
  }
  document.getElementById("strategyValue").textContent = configCache.default_strategy;
  document.getElementById("fallbackToggle").checked = configCache.enable_fallback;

  // populate playground strategy dropdown from the same list
  const pgStrategy = document.getElementById("pgStrategy");
  pgStrategy.innerHTML = "";
  configCache.available_strategies.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name.replace(/_/g, " ");
    pgStrategy.appendChild(opt);
  });
  pgStrategy.value = configCache.default_strategy;

  // strategies come from their own endpoint so we get tags + builtin flag
  await refreshStrategies();
}

async function refreshStrategies() {
  try {
    strategiesCache = await adminApi("/api/strategies");
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    return;
  }
  strategyGrid.innerHTML = "";
  strategiesCache.forEach((s, i) => {
    const card = document.createElement("div");
    card.className = "strategy-card" + (s.name === (configCache?.default_strategy) ? " is-active" : "");
    card.innerHTML = `
      ${s.is_builtin ? '<span class="strategy-card__builtin">BUILT-IN</span>' : ''}
      <div class="strategy-card__index">// ${String(i + 1).padStart(2, "0")}</div>
      <div class="strategy-card__name">${escapeHtml(s.name.replace(/_/g, " "))}</div>
      <div class="strategy-card__desc">${escapeHtml(s.description || STRATEGY_DESCRIPTIONS[s.name] || "")}</div>
      <div class="strategy-card__tags">
        ${s.tags.map((t) => `<span class="strategy-card__tag">${escapeHtml(t)}</span>`).join("") || '<span class="strategy-card__tag">auto-detect</span>'}
      </div>
      <div class="strategy-card__actions">
        <button class="mini-btn edit-strategy">EDIT</button>
        ${!s.is_builtin ? '<button class="mini-btn delete-strategy">DEL</button>' : ''}
      </div>
    `;
    // select-as-default on click (avoid button clicks)
    card.addEventListener("click", async (e) => {
      if (e.target.closest(".strategy-card__actions")) return;
      try {
        await adminApi("/api/config/strategy", {
          method: "PUT",
          body: JSON.stringify({ default_strategy: s.name }),
        });
        refreshConfig();
      } catch (err) {
        if (err instanceof AuthError) showAdminModal();
      }
    });
    card.querySelector(".edit-strategy")?.addEventListener("click", (e) => {
      e.stopPropagation();
      openStrategyEditor(s);
    });
    card.querySelector(".delete-strategy")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete strategy "${s.name}"?`)) return;
      try {
        await adminApi(`/api/strategies/${s.name}`, { method: "DELETE" });
        refreshConfig();
      } catch (err) {
        if (err instanceof AuthError) showAdminModal();
        else alert(err.message);
      }
    });
    strategyGrid.appendChild(card);
  });
}

let _stratEditorExisting = null;

function openStrategyEditor(existing) {
  _stratEditorExisting = existing;
  const modal = document.getElementById("strategyEditorModal");
  const nameInput = document.getElementById("stratEditorName");
  const tagsInput = document.getElementById("stratEditorTags");
  const descInput = document.getElementById("stratEditorDesc");
  const errEl = document.getElementById("stratEditorError");
  errEl.hidden = true;

  document.getElementById("stratEditorTitle").textContent = existing ? "EDIT STRATEGY" : "NEW STRATEGY";
  nameInput.value = existing ? existing.name : "";
  nameInput.disabled = !!existing;
  tagsInput.value = (existing?.tags || []).join(", ");
  descInput.value = existing?.description || "";

  modal.hidden = false;
  setTimeout(() => (existing ? tagsInput : nameInput).focus(), 50);
}

document.getElementById("stratEditorCancel").addEventListener("click", () => {
  document.getElementById("strategyEditorModal").hidden = true;
});

document.getElementById("stratEditorSave").addEventListener("click", async () => {
  const nameInput = document.getElementById("stratEditorName");
  const tagsInput = document.getElementById("stratEditorTags");
  const descInput = document.getElementById("stratEditorDesc");
  const errEl = document.getElementById("stratEditorError");
  errEl.hidden = true;

  const name = nameInput.value.trim();
  if (!name || !/^[a-z0-9_]+$/.test(name)) {
    errEl.textContent = "Name must use a-z, 0-9, underscore only.";
    errEl.hidden = false;
    return;
  }
  const tags = tagsInput.value.split(",").map((t) => t.trim()).filter(Boolean);
  const description = descInput.value.trim();
  const method = _stratEditorExisting ? "PATCH" : "POST";
  const path = _stratEditorExisting ? `/api/strategies/${name}` : "/api/strategies";

  try {
    await adminApi(path, { method, body: JSON.stringify({ name, tags, description }) });
    document.getElementById("strategyEditorModal").hidden = true;
    refreshConfig();
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

document.getElementById("strategyCreate").addEventListener("click", () => openStrategyEditor(null));

document.getElementById("fallbackToggle").addEventListener("change", async (e) => {
  try {
    await adminApi("/api/config/fallback", {
      method: "PUT",
      body: JSON.stringify({ enable_fallback: e.target.checked }),
    });
  } catch (err) {
    if (err instanceof AuthError) showAdminModal();
  }
});

function updateProviderSelect(providers) {
  const sel = document.getElementById("pgProvider");
  sel.innerHTML = '<option value="">— auto —</option>';
  providers.forEach((p) => {
    if (!p.has_key || !p.enabled) return;
    const opt = document.createElement("option");
    opt.value = p.name;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
}

// ─────────────── playground ───────────────

const pgSend = document.getElementById("pgSend");

async function runPlayground() {
  const prompt = document.getElementById("pgPrompt").value.trim();
  if (!prompt) return;
  const stream = document.getElementById("pgStream").checked;
  const system = document.getElementById("pgSystem").value || null;
  const messages = [];
  if (system) messages.push({ role: "system", content: system });
  messages.push({ role: "user", content: prompt });

  const body = {
    messages,
    strategy: document.getElementById("pgStrategy").value,
    preferred_provider: document.getElementById("pgProvider").value || null,
    temperature: parseFloat(document.getElementById("pgTemp").value),
    stream,
  };

  const out = document.getElementById("pgOutput");
  pgSend.disabled = true;

  try {
    if (stream) {
      await runStreaming(body, out);
    } else {
      await runOneShot(body, out);
    }
  } catch (e) {
    out.innerHTML = `<div class="output-meta"><span>error</span></div><div class="output-body">${escapeHtml(e.message)}</div>`;
  } finally {
    pgSend.disabled = false;
    refreshProviders();
  }
}

async function runOneShot(body, out) {
  out.innerHTML = `<div class="output-empty"><pre>┄┄ transmitting ┄┄</pre></div>`;
  // /v1/chat/completions does NOT require admin — it's a client endpoint.
  // In bootstrap mode (no clients) it's open. Otherwise the user needs an
  // outbound key. The playground hits it directly.
  const res = await fetch(`${API_BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text);
  }
  const data = await res.json();
  out.innerHTML = `
    <div class="output-meta">
      <span>provider: <b>${data.provider}</b></span>
      <span>model: <b>${data.model}</b></span>
      <span>strategy: <b>${data.strategy_used}</b></span>
      <span>${data.latency_ms} ms</span>
      <span>${data.usage.total_tokens} tokens</span>
    </div>
    <div class="output-body">${escapeHtml(data.choices[0].message.content)}</div>
    <div class="output-fallback">chain: <b>${data.fallback_chain.join(" → ")}</b></div>
  `;
}

async function runStreaming(body, out) {
  out.innerHTML = `
    <div class="output-meta" id="streamMeta"><span>connecting…</span></div>
    <div class="output-body" id="streamBody"><span class="output-stream-cursor"></span></div>
  `;
  const meta = document.getElementById("streamMeta");
  const bodyEl = document.getElementById("streamBody");
  let acc = "";
  let firstChunk = true;
  let provider = null, model = null, strategy = null;
  const started = performance.now();

  const res = await fetch(`${API_BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(await res.text());
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const event = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = event.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") continue;
      let chunk;
      try { chunk = JSON.parse(payload); } catch { continue; }
      if (chunk.error) {
        throw new Error(chunk.error.message);
      }
      if (firstChunk) {
        firstChunk = false;
        provider = chunk.provider;
        model    = chunk.model;
        strategy = chunk.strategy_used;
        meta.innerHTML = `
          <span>provider: <b>${provider}</b></span>
          <span>model: <b>${model}</b></span>
          <span>strategy: <b>${strategy}</b></span>
          <span id="streamLatency">streaming…</span>
        `;
      }
      const delta = chunk.choices?.[0]?.delta?.content || "";
      if (delta) {
        acc += delta;
        bodyEl.innerHTML = escapeHtml(acc) + '<span class="output-stream-cursor"></span>';
      }
    }
  }
  const elapsed = Math.round(performance.now() - started);
  bodyEl.innerHTML = escapeHtml(acc);
  const lat = document.getElementById("streamLatency");
  if (lat) lat.textContent = `${elapsed} ms`;
}

pgSend.addEventListener("click", runPlayground);

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// ─────────────── clients panel ───────────────

async function refreshClients() {
  const list = document.getElementById("clientList");
  list.innerHTML = "";
  let clients;
  try {
    clients = await adminApi("/api/clients");
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    list.innerHTML = `<div class="output-empty"><pre>${escapeHtml(err.message)}</pre></div>`;
    return;
  }
  if (!clients.length) {
    list.innerHTML = `<div class="output-empty"><pre>
   ┌──────────────────────────────┐
   │   no clients — bootstrap     │
   │   mode active (open access)  │
   └──────────────────────────────┘
    </pre></div>`;
    return;
  }
  clients.forEach((c) => {
    const card = document.createElement("article");
    card.className = "client-card";
    card.innerHTML = `
      <div class="client-card__name">${escapeHtml(c.name)}</div>
      <div class="client-card__meta">
        <span>${c.rpm_limit} rpm</span>
        <span>${c.enabled ? "enabled" : "disabled"}</span>
      </div>
      <div class="client-card__hash">${c.key_hash.slice(0, 24)}…</div>
      <button class="ghost-button ghost-button--small">REVOKE</button>
    `;
    card.querySelector("button").addEventListener("click", async () => {
      if (!confirm(`Revoke client "${c.name}"? Apps using this key will stop working.`)) return;
      try {
        await adminApi(`/api/clients/${c.key_hash}`, { method: "DELETE" });
        refreshClients();
      } catch (err) {
        if (err instanceof AuthError) showAdminModal();
      }
    });
    list.appendChild(card);
  });
}

document.getElementById("clientCreate").addEventListener("click", async () => {
  const name = document.getElementById("clientName").value.trim();
  const rpm  = parseInt(document.getElementById("clientRpm").value, 10) || 60;
  if (!name) return;
  try {
    const created = await adminApi("/api/clients", {
      method: "POST",
      body: JSON.stringify({ name, rpm_limit: rpm }),
    });
    const flash = document.getElementById("clientFlash");
    flash.hidden = false;
    flash.innerHTML = `
      <div class="client-flash__title">Key issued — copy it now</div>
      <div class="client-flash__key">${escapeHtml(created.api_key)}</div>
      <div class="client-flash__warn">This is the only time you'll see this key. Save it somewhere safe.</div>
    `;
    document.getElementById("clientName").value = "";
    refreshClients();
  } catch (err) {
    if (err instanceof AuthError) showAdminModal();
    else alert(err.message);
  }
});

// ─────────────── analytics panel ───────────────

const analyticsWindowSel = document.getElementById("analyticsWindow");

async function refreshAnalytics() {
  const windowSec = parseInt(analyticsWindowSel.value, 10);
  const buckets = windowSec <= 3600 ? 12 : windowSec <= 21600 ? 18 : 24;
  let data;
  try {
    data = await adminApi(`/api/analytics?window_seconds=${windowSec}&bucket_count=${buckets}`);
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    document.getElementById("kpiGrid").innerHTML = `<div class="chart-card__empty">${escapeHtml(err.message)}</div>`;
    return;
  }

  renderKpis(data);
  renderTimeSeries(data);
  renderByProvider(data);
  renderByOutcome(data);
  renderByStrategy(data);
}

// Chart rendering functions live in charts.js (loaded before this file).

document.getElementById("analyticsRefresh").addEventListener("click", refreshAnalytics);
analyticsWindowSel.addEventListener("change", refreshAnalytics);

// ─────────────── boot ───────────────

async function boot() {
  updateLockUI();
  try {
    const st = await publicApi("/api/setup/status");
    if (st.needs_initial_setup) {
      openFirstRunSetup(st.provider_names);
      return;
    }
  } catch {
    /* uplink down — sigue e intenta el resto */
  }
  try {
    const h = await publicApi("/api/health");
    if (h.auth_required && !getAdminToken()) {
      showAdminModal();
    }
  } catch {}
  await Promise.all([refreshProviders(), refreshConfig()]);
}

(async function start() {
  await boot();
  setInterval(() => {
    const activePanel = document.querySelector('.panel--active');
    if (!activePanel) return;
    const panel = activePanel.dataset.panel;
    if (panel === "providers") refreshProviders();
    if (panel === "analytics") refreshAnalytics();
  }, 8000);
})();
