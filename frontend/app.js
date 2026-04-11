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

function openStrategyEditor(existing) {
  const name = existing
    ? existing.name
    : prompt("Strategy name (a-z, 0-9, underscore):", "");
  if (!name) return;
  if (!/^[a-z0-9_]+$/.test(name)) { alert("invalid name — use a-z, 0-9, _"); return; }
  const tagsStr = prompt(
    "Tag priority (comma-separated). These match against provider tags:\n" +
    "Known tags: fast, cheap, quality, coding, reasoning, vision, long_context, rag, variety",
    (existing?.tags || []).join(", "),
  );
  if (tagsStr === null) return;
  const tags = tagsStr.split(",").map((t) => t.trim()).filter(Boolean);
  const description = prompt("Short description:", existing?.description || "") || "";

  const method = existing ? "PATCH" : "POST";
  const path = existing ? `/api/strategies/${name}` : "/api/strategies";
  adminApi(path, { method, body: JSON.stringify({ name, tags, description }) })
    .then(() => refreshConfig())
    .catch((err) => {
      if (err instanceof AuthError) showAdminModal();
      else alert(err.message);
    });
}

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
const kpiGrid = document.getElementById("kpiGrid");

async function refreshAnalytics() {
  const windowSec = parseInt(analyticsWindowSel.value, 10);
  const buckets = windowSec <= 3600 ? 12 : windowSec <= 21600 ? 18 : 24;
  let data;
  try {
    data = await adminApi(`/api/analytics?window_seconds=${windowSec}&bucket_count=${buckets}`);
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    kpiGrid.innerHTML = `<div class="chart-card__empty">${escapeHtml(err.message)}</div>`;
    return;
  }

  renderKpis(data);
  renderTimeSeries(data);
  renderByProvider(data);
  renderByOutcome(data);
  renderByStrategy(data);
}

function renderKpis(data) {
  const success = data.success_calls;
  const fail = data.failed_calls;
  const pct = data.total_calls ? Math.round(data.success_rate * 1000) / 10 : 0;
  kpiGrid.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">TOTAL&nbsp;CALLS</div>
      <div class="kpi-card__value">${data.total_calls.toLocaleString()}</div>
      <div class="kpi-card__sub"><b>${success}</b> ok · <b>${fail}</b> failed</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">SUCCESS&nbsp;RATE</div>
      <div class="kpi-card__value">${pct}<span class="kpi-card__unit">%</span></div>
      <div class="kpi-card__sub">across all providers</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">LATENCY&nbsp;P50</div>
      <div class="kpi-card__value">${data.p50_latency_ms ?? 0}<span class="kpi-card__unit">ms</span></div>
      <div class="kpi-card__sub">p95: <b>${data.p95_latency_ms ?? 0} ms</b></div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">TOKENS</div>
      <div class="kpi-card__value">${formatTokens(data.total_tokens)}</div>
      <div class="kpi-card__sub">prompt + completion</div>
    </div>
  `;
}

function formatTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function renderTimeSeries(data) {
  const el = document.getElementById("chartTimeSeries");
  const buckets = data.time_buckets;
  if (!buckets.length || buckets.every((b) => b.calls === 0)) {
    el.innerHTML = `<div class="chart-card__empty">no traffic yet — send a prompt from the playground</div>`;
    return;
  }
  const w = 800, h = 220, pad = 32;
  const maxCalls = Math.max(...buckets.map((b) => b.calls), 1);
  const stepX = (w - 2 * pad) / Math.max(buckets.length - 1, 1);

  const pointsCalls = buckets.map((b, i) => [
    pad + i * stepX,
    h - pad - (b.calls / maxCalls) * (h - 2 * pad),
  ]);
  const pointsSuccess = buckets.map((b, i) => [
    pad + i * stepX,
    h - pad - (b.success / maxCalls) * (h - 2 * pad),
  ]);

  const pathCalls = pointsCalls.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const pathSuccess = pointsSuccess.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const area = `M${pad},${h - pad} ${pathCalls.slice(1)} L${w - pad},${h - pad} Z`;

  // Y grid
  const ticks = 4;
  const gridLines = [];
  for (let i = 0; i <= ticks; i++) {
    const y = pad + (i * (h - 2 * pad)) / ticks;
    const label = Math.round(maxCalls * (1 - i / ticks));
    gridLines.push(
      `<line class="svg-grid" x1="${pad}" x2="${w - pad}" y1="${y}" y2="${y}" />`,
      `<text class="svg-label" x="${pad - 6}" y="${y + 3}" text-anchor="end">${label}</text>`,
    );
  }

  // X labels (first, middle, last)
  const xLabels = [0, Math.floor(buckets.length / 2), buckets.length - 1].map((i) => {
    const t = new Date(buckets[i].bucket_start * 1000);
    const hh = String(t.getHours()).padStart(2, "0");
    const mm = String(t.getMinutes()).padStart(2, "0");
    return `<text class="svg-label" x="${pad + i * stepX}" y="${h - 10}" text-anchor="middle">${hh}:${mm}</text>`;
  });

  el.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
      ${gridLines.join("")}
      <path class="svg-area" d="${area}" />
      <path class="svg-line" d="${pathCalls}" />
      <path class="svg-line svg-line--success" d="${pathSuccess}" />
      ${xLabels.join("")}
      <line class="svg-axis" x1="${pad}" x2="${w - pad}" y1="${h - pad}" y2="${h - pad}" />
    </svg>
  `;
}

function renderByProvider(data) {
  const el = document.getElementById("chartByProvider");
  const rows = data.by_provider;
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no data</div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const w = 280, rowH = 28;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = (r.calls / max) * (w - 100);
    const successRate = r.calls ? Math.round((r.success / r.calls) * 100) : 0;
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(r.provider)}</text>
      <rect class="svg-bar" x="70" y="${y + 4}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${70 + barW + 4}" y="${y + 13}">${r.calls} · ${successRate}%</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function renderByOutcome(data) {
  const el = document.getElementById("chartByOutcome");
  const rows = data.by_outcome;
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no data</div>`;
    return;
  }
  const total = rows.reduce((s, r) => s + r.calls, 0);
  const colors = {
    success: "var(--moss)",
    rate_limited: "var(--amber)",
    server_error: "var(--rose)",
    auth: "var(--rose)",
    network: "var(--rose)",
    client_error: "var(--char-3)",
    parsing: "var(--char-3)",
    unknown: "var(--char-3)",
  };
  const segs = rows.map((r) => {
    const pct = (r.calls / total) * 100;
    const color = colors[r.outcome] || "var(--char-3)";
    return `<div class="stack-bar__seg" style="width:${pct}%; background:${color};" title="${r.outcome}: ${r.calls}"></div>`;
  }).join("");
  const legend = rows.map((r) => {
    const color = colors[r.outcome] || "var(--char-3)";
    return `<div style="display:flex;align-items:center;gap:6px;font-size:10px;letter-spacing:.1em;margin-top:6px;">
      <span style="width:10px;height:10px;background:${color};display:inline-block;"></span>
      <span>${r.outcome}</span>
      <span style="margin-left:auto;font-weight:700;">${r.calls}</span>
    </div>`;
  }).join("");
  el.innerHTML = `
    <div style="width:100%;">
      <div class="stack-bar">${segs}</div>
      <div style="margin-top:12px;">${legend}</div>
    </div>
  `;
}

function renderByStrategy(data) {
  const el = document.getElementById("chartByStrategy");
  const rows = data.by_strategy;
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no data</div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const w = 280, rowH = 26;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = (r.calls / max) * (w - 110);
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(r.strategy)}</text>
      <rect class="svg-bar" x="90" y="${y + 3}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${90 + barW + 4}" y="${y + 13}">${r.calls}</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function escapeSvg(s) {
  return String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

document.getElementById("analyticsRefresh").addEventListener("click", refreshAnalytics);
analyticsWindowSel.addEventListener("change", refreshAnalytics);

// ─────────────── boot ───────────────

async function boot() {
  updateLockUI();
  // health is public
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
    if (document.querySelector('[data-panel="providers"]').classList.contains("panel--active")) {
      refreshProviders();
    }
  }, 8000);
})();
