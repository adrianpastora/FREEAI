// FreeAI control room — talks to FastAPI backend at API_BASE
// Local dev: frontend on any port → backend on :8000
// Production: reverse proxy serves both frontend and API on the same origin
const _isLocal = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
const API_BASE = _isLocal
  ? `http://${window.location.hostname}:8000`
  : window.location.origin;

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

// ─────────────── JWT auth ───────────────

const ACCESS_KEY = "freeai_access_token";
const REFRESH_KEY = "freeai_refresh_token";
const USER_KEY = "freeai_user";

// Legacy compat — still read old admin token for migration flow
const TOKEN_KEY = "freeai_admin_token";

function getAccessToken() { return localStorage.getItem(ACCESS_KEY) || ""; }
function getRefreshToken() { return localStorage.getItem(REFRESH_KEY) || ""; }
function getCurrentUser() {
  try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); } catch { return null; }
}

function saveSession(data) {
  localStorage.setItem(ACCESS_KEY, data.access_token);
  localStorage.setItem(REFRESH_KEY, data.refresh_token);
  localStorage.setItem(USER_KEY, JSON.stringify(data.user));
  // Clear legacy token if present
  localStorage.removeItem(TOKEN_KEY);
  updateLockUI();
}

function clearSession() {
  const refresh = getRefreshToken();
  if (refresh) {
    // Best-effort logout — don't await
    fetch(`${API_BASE}/api/auth/logout`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ refresh_token: refresh }),
    }).catch(() => {});
  }
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(TOKEN_KEY);
  updateLockUI();
}

function isLoggedIn() { return !!getAccessToken(); }

async function refreshAccessToken() {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  try {
    const res = await fetch(`${API_BASE}/api/auth/refresh`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) { clearSession(); return false; }
    const data = await res.json();
    saveSession(data);
    return true;
  } catch { clearSession(); return false; }
}

// Check if access token is about to expire (< 2 min left)
function tokenNeedsRefresh() {
  const token = getAccessToken();
  if (!token) return true;
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return (payload.exp - Date.now() / 1000) < 120;
  } catch { return true; }
}

async function ensureValidToken() {
  if (tokenNeedsRefresh()) {
    return await refreshAccessToken();
  }
  return true;
}

// Legacy admin token getter (for backwards compat during migration)
function getAdminToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}
function setAdminToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function updateLockUI() {
  const btn = document.getElementById("lockButton");
  const icon = document.getElementById("lockIcon");
  const user = getCurrentUser();
  if (user) {
    btn.classList.remove("is-locked");
    icon.textContent = "⛓";
    btn.title = `${user.username} (${user.role}) — click to logout`;
    // Show users tab only for admins
    const usersTab = document.getElementById("usersTab");
    if (usersTab) usersTab.style.display = user.role === "admin" ? "" : "none";
  } else {
    btn.classList.add("is-locked");
    icon.textContent = "⛒";
    btn.title = "not logged in (click to login)";
    const usersTab = document.getElementById("usersTab");
    if (usersTab) usersTab.style.display = "none";
  }
}

function showLoginModal() {
  const modal = document.getElementById("adminModal");
  const wasAlreadyOpen = !modal.hidden;
  modal.hidden = false;
  document.getElementById("loginError").style.display = "none";
  // Only auto-focus if the modal was just opened — don't steal focus
  // from the user if they're already typing in the password field
  if (!wasAlreadyOpen) {
    setTimeout(() => document.getElementById("loginUsername").focus(), 50);
  }
}
function hideLoginModal() {
  document.getElementById("adminModal").hidden = true;
}

document.getElementById("lockButton").addEventListener("click", () => {
  if (isLoggedIn()) {
    if (confirm("Log out?")) { clearSession(); boot(); }
    return;
  }
  showLoginModal();
});

document.getElementById("loginSubmit").addEventListener("click", async () => {
  const username = document.getElementById("loginUsername").value.trim();
  const password = document.getElementById("loginPassword").value;
  const errEl = document.getElementById("loginError");
  if (!username || !password) return;
  errEl.style.display = "none";
  try {
    console.log("[login] attempting", API_BASE + "/api/auth/login", username);
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ username, password }),
    });
    console.log("[login] response status", res.status);
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      console.log("[login] error detail", detail);
      errEl.textContent = detail.detail || "Login failed";
      errEl.style.display = "block";
      return;
    }
    const data = await res.json();
    console.log("[login] success, user:", data.user, "token length:", data.access_token?.length);
    saveSession(data);
    hideLoginModal();
    document.getElementById("loginUsername").value = "";
    document.getElementById("loginPassword").value = "";
    errEl.style.display = "none";
    await boot();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = "block";
  }
});

document.getElementById("loginUsername").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("loginPassword").focus();
});
document.getElementById("loginPassword").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("loginSubmit").click();
});

// ─────────────── migration modal ───────────────

function showMigrateModal() {
  document.getElementById("migrateModal").hidden = false;
  document.getElementById("migrateError").style.display = "none";
  setTimeout(() => document.getElementById("migrateOldToken").focus(), 50);
}

document.getElementById("migrateSubmit").addEventListener("click", async () => {
  const oldToken = document.getElementById("migrateOldToken").value.trim();
  const username = document.getElementById("migrateUsername").value.trim();
  const password = document.getElementById("migratePassword").value;
  const password2 = document.getElementById("migratePassword2").value;
  const errEl = document.getElementById("migrateError");

  if (!oldToken || !username || !password) return;
  if (password !== password2) {
    errEl.textContent = "Passwords do not match";
    errEl.style.display = "block";
    return;
  }
  if (password.length < 8) {
    errEl.textContent = "Password must be at least 8 characters";
    errEl.style.display = "block";
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/auth/migrate-token`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        admin_token: oldToken,
        username, password,
        password_confirm: password,
      }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      errEl.textContent = detail.detail || "Migration failed";
      errEl.style.display = "block";
      return;
    }
    const data = await res.json();
    saveSession(data);
    document.getElementById("migrateModal").hidden = true;
    await boot();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = "block";
  }
});

// ─────────────── first-run setup ───────────────

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
    input.placeholder = "paste API key…";
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
  // restore the form view (in case this modal had been left on reveal)
  document.getElementById("setupForm").hidden = false;
  document.getElementById("setupTokenReveal").hidden = true;
  // reset any toggle back to masked
  document.querySelectorAll(".setup-toggle").forEach((btn) => {
    const input = document.getElementById(btn.dataset.target);
    if (input) input.type = "password";
    btn.textContent = "MOSTRAR";
  });
  showSetupModal();
  setTimeout(() => document.getElementById("setupAdminToken").focus(), 50);
}

// Toggle password/text for the setup token fields.
document.querySelectorAll(".setup-toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    const input = document.getElementById(btn.dataset.target);
    if (!input) return;
    if (input.type === "password") {
      input.type = "text";
      btn.textContent = "OCULTAR";
    } else {
      input.type = "password";
      btn.textContent = "MOSTRAR";
    }
  });
});

document.getElementById("setupGenToken").addEventListener("click", () => {
  const a = new Uint8Array(22);
  crypto.getRandomValues(a);
  let s = "";
  a.forEach((b) => {
    s += (`0${b.toString(16)}`).slice(-2);
  });
  const tok = `adm_${s}`;
  const t1 = document.getElementById("setupAdminToken");
  const t2 = document.getElementById("setupAdminToken2");
  t1.value = tok;
  t2.value = tok;
  // Auto-reveal both fields so the user can see what was just generated.
  t1.type = "text";
  t2.type = "text";
  document.querySelectorAll(".setup-toggle").forEach((btn) => {
    btn.textContent = "OCULTAR";
  });
});

document.getElementById("setupSubmit").addEventListener("click", async () => {
  const err = document.getElementById("setupError");
  err.hidden = true;
  const bootstrap = document.getElementById("setupBootstrapToken").value.trim();
  const t1 = document.getElementById("setupAdminToken").value.trim();
  const t2 = document.getElementById("setupAdminToken2").value.trim();
  if (!bootstrap) {
    err.textContent =
      "Enter the bootstrap token printed to the server logs.";
    err.hidden = false;
    return;
  }
  if (t1.length < 12) {
    err.textContent = "The admin token must be at least 12 characters.";
    err.hidden = false;
    return;
  }
  if (t1 !== t2) {
    err.textContent = "The two admin-token fields do not match.";
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
      headers: {
        "Content-Type": "application/json",
        "X-Bootstrap-Token": bootstrap,
      },
      body: JSON.stringify({
        admin_token: t1,
        admin_token_confirm: t2,
        provider_keys: keys,
      }),
    });
    if (res.status === 403) {
      err.textContent =
        "Initial setup is no longer available. Use the lock icon and enter your admin token.";
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
    await res.json().catch(() => ({}));
    setAdminToken(t1);
    // Show the reveal-once screen instead of closing the modal.
    // The token is hashed in the DB, so this is the user's only chance
    // to copy it in cleartext.
    document.getElementById("setupForm").hidden = true;
    const revealInput = document.getElementById("setupRevealedToken");
    revealInput.value = t1;
    document.getElementById("setupTokenReveal").hidden = false;
    document.getElementById("setupCopyHint").hidden = true;
  } catch (e) {
    err.textContent = String(e.message || e);
    err.hidden = false;
  }
});

// Reveal-once: copy token to clipboard.
document.getElementById("setupCopyToken").addEventListener("click", async () => {
  const input = document.getElementById("setupRevealedToken");
  const hint = document.getElementById("setupCopyHint");
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    // Fallback for browsers without clipboard API (or insecure context).
    input.select();
    try { document.execCommand("copy"); } catch {}
  }
  hint.textContent = "Copied to clipboard.";
  hint.hidden = false;
});

// Reveal-once: user confirms they've saved the token — close modal and boot.
document.getElementById("setupRevealContinue").addEventListener("click", async () => {
  hideSetupModal();
  // Clear the revealed token from the DOM so it isn't left sitting in memory.
  document.getElementById("setupRevealedToken").value = "";
  document.getElementById("setupAdminToken").value = "";
  document.getElementById("setupAdminToken2").value = "";
  await boot();
});

// ─────────────── tab switcher ───────────────

const ribbonTabs = document.querySelectorAll(".ribbon__item");
const panels = document.querySelectorAll(".panel");
const ribbonBurger = document.getElementById("ribbonBurger");
const ribbonItems = document.getElementById("ribbonItems");
const ribbonActiveLabel = document.getElementById("ribbonActiveLabel");

ribbonTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    ribbonTabs.forEach((t) => t.classList.remove("ribbon__item--active"));
    tab.classList.add("ribbon__item--active");
    panels.forEach((p) => p.classList.remove("panel--active"));
    document.querySelector(`[data-panel="${tab.dataset.tab}"]`).classList.add("panel--active");
    if (tab.dataset.tab === "clients") refreshClients();
    if (tab.dataset.tab === "analytics") refreshAnalytics();
    if (tab.dataset.tab === "strategy") refreshStrategies();
    if (tab.dataset.tab === "users") refreshUsers();
    // mobile: close drawer & update active label
    ribbonItems.classList.remove("ribbon__items--open");
    ribbonBurger.classList.remove("ribbon__burger--open");
    ribbonActiveLabel.textContent =
      tab.querySelector(".ribbon__index").textContent + " " +
      tab.querySelector(".ribbon__label").textContent;
  });
});

// mobile hamburger toggle
ribbonBurger.addEventListener("click", () => {
  ribbonItems.classList.toggle("ribbon__items--open");
  ribbonBurger.classList.toggle("ribbon__burger--open");
});

// ─────────────── http helpers ───────────────

class AuthError extends Error {}

async function adminApi(path, opts = {}) {
  // Ensure token is fresh
  await ensureValidToken();
  const token = getAccessToken();
  const headers = {
    "Content-Type": "application/json",
    ...(opts.headers || {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (res.status === 401) {
    // Try refresh once
    if (await refreshAccessToken()) {
      headers["Authorization"] = `Bearer ${getAccessToken()}`;
      const retry = await fetch(`${API_BASE}${path}`, { ...opts, headers });
      if (retry.status === 401) throw new AuthError("session expired");
      if (!retry.ok) throw new Error(`${retry.status} ${await retry.text()}`);
      if (retry.status === 204) return null;
      return retry.json();
    }
    throw new AuthError("session expired");
  }
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

function showModelWarning(cardNode, warning, suggestions, modelSelect) {
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
      const id = s.dataset.model;
      if (![...modelSelect.options].some((o) => o.value === id)) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = id;
        modelSelect.appendChild(opt);
      }
      modelSelect.value = id;
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

// ─── Provider setup wizard data ───
const PROVIDER_GUIDES = {
  groq: {
    displayName: "Groq",
    signupUrl: "https://console.groq.com/signup",
    docsUrl: "https://console.groq.com/docs/quickstart",
    freeTier: "Free: 30 req/min, 14,400 req/day, no credit card required",
    steps: [
      "Go to <a href=\"https://console.groq.com/signup\" target=\"_blank\" rel=\"noopener\">console.groq.com/signup</a> and create an account (Google/GitHub work).",
      "Once in, open <strong>API Keys</strong> from the left sidebar.",
      "Click <strong>Create API Key</strong>, name it (e.g. \"FreeAI\") and copy the generated key.",
      "Paste the key into the <em>API KEY</em> field on the Groq card in FreeAI and click <strong>SAVE</strong>."
    ]
  },
  gemini: {
    displayName: "Google Gemini",
    signupUrl: "https://aistudio.google.com/apikey",
    docsUrl: "https://ai.google.dev/gemini-api/docs/quickstart",
    freeTier: "Free: 15 req/min, 1,500 req/day, requires a Google account",
    steps: [
      "Go to <a href=\"https://aistudio.google.com/apikey\" target=\"_blank\" rel=\"noopener\">aistudio.google.com/apikey</a> and sign in with your Google account.",
      "Click <strong>Create API Key</strong> and pick a Google Cloud project (one is created automatically if you don't have any).",
      "Copy the generated API key.",
      "Paste the key into the <em>API KEY</em> field on the Gemini card in FreeAI and click <strong>SAVE</strong>."
    ]
  },
  mistral: {
    displayName: "Mistral AI",
    signupUrl: "https://console.mistral.ai/",
    docsUrl: "https://docs.mistral.ai/getting-started/quickstart/",
    freeTier: "Free: 60 req/min on the experimental free plan; signup required",
    steps: [
      "Go to <a href=\"https://console.mistral.ai/\" target=\"_blank\" rel=\"noopener\">console.mistral.ai</a> and create an account.",
      "From the dashboard, open <strong>API Keys</strong> in the sidebar.",
      "Click <strong>Create new key</strong>, name it, and copy the key.",
      "Paste the key into the <em>API KEY</em> field on the Mistral card in FreeAI and click <strong>SAVE</strong>."
    ]
  },
  openrouter: {
    displayName: "OpenRouter",
    signupUrl: "https://openrouter.ai/",
    docsUrl: "https://openrouter.ai/docs/quickstart",
    freeTier: "Free: 20 req/min, 200 req/day on models flagged with \":free\"",
    steps: [
      "Go to <a href=\"https://openrouter.ai/\" target=\"_blank\" rel=\"noopener\">openrouter.ai</a> and sign up (Google/GitHub work).",
      "Open <strong>Keys</strong> in your profile (<a href=\"https://openrouter.ai/keys\" target=\"_blank\" rel=\"noopener\">openrouter.ai/keys</a>).",
      "Click <strong>Create Key</strong>, name it, and copy the key (starts with <code>sk-or-</code>).",
      "Paste the key into the <em>API KEY</em> field on the OpenRouter card in FreeAI and click <strong>SAVE</strong>.",
      "<strong>Note:</strong> FreeAI uses models with the <code>:free</code> suffix. You don't need to add credits."
    ]
  },
  cohere: {
    displayName: "Cohere",
    signupUrl: "https://dashboard.cohere.com/welcome/register",
    docsUrl: "https://docs.cohere.com/docs/the-cohere-platform",
    freeTier: "Free (Trial): 20 req/min, 1,000 req/day, no credit card required",
    steps: [
      "Go to <a href=\"https://dashboard.cohere.com/welcome/register\" target=\"_blank\" rel=\"noopener\">dashboard.cohere.com</a> and create an account.",
      "From the dashboard, open <strong>API Keys</strong> in the sidebar.",
      "A <strong>Trial key</strong> is already generated for you. If you need a new one, click <strong>+ New Trial key</strong>.",
      "Copy the key and paste it into the <em>API KEY</em> field on the Cohere card in FreeAI. Click <strong>SAVE</strong>."
    ]
  },
  huggingface: {
    displayName: "Hugging Face",
    signupUrl: "https://huggingface.co/join",
    docsUrl: "https://huggingface.co/docs/api-inference/",
    freeTier: "Free: 30 req/min, 1,000 req/day on the free Inference API",
    steps: [
      "Go to <a href=\"https://huggingface.co/join\" target=\"_blank\" rel=\"noopener\">huggingface.co/join</a> and create an account.",
      "Open <strong>Settings → Access Tokens</strong> (<a href=\"https://huggingface.co/settings/tokens\" target=\"_blank\" rel=\"noopener\">huggingface.co/settings/tokens</a>).",
      "Click <strong>Create new token</strong>, pick the <strong>Read</strong> scope (or <em>Fine-grained</em> with Inference access), name it, and generate the token.",
      "Copy the token (starts with <code>hf_</code>) and paste it into the <em>API KEY</em> field on the HuggingFace card in FreeAI. Click <strong>SAVE</strong>."
    ]
  }
};

function openSetupWizard(providerName) {
  const guide = PROVIDER_GUIDES[providerName];
  if (!guide) return;
  const modal = document.getElementById("wizardModal");
  modal.querySelector(".wizard__provider-name").textContent = guide.displayName;
  modal.querySelector(".wizard__free-tier").textContent = guide.freeTier;
  const stepsList = modal.querySelector(".wizard__steps");
  stepsList.innerHTML = guide.steps
    .map((s, i) => `<li><span class="wizard__step-num">${i + 1}</span><span class="wizard__step-text">${s}</span></li>`)
    .join("");
  const linksRow = modal.querySelector(".wizard__links");
  linksRow.innerHTML = `
    <a href="${guide.signupUrl}" target="_blank" rel="noopener" class="ghost-button ghost-button--small">SIGN&nbsp;UP ↗</a>
    <a href="${guide.docsUrl}" target="_blank" rel="noopener" class="ghost-button ghost-button--small">DOCS ↗</a>
  `;
  modal.hidden = false;
}

document.addEventListener("click", (e) => {
  if (e.target.closest("#wizardModalClose") || e.target.closest("#wizardModal > .modal__backdrop")) {
    document.getElementById("wizardModal").hidden = true;
  }
});

// ─────────────── multi-step provider setup wizard ───────────────

const _pwProviderOrder = ["groq", "gemini", "mistral", "openrouter", "cohere", "huggingface"];
let _pwStep = 0; // 0..N-1 = providers, N = summary
let _pwConfigured = {}; // { providerName: bool }

function _pwTotalSteps() { return _pwProviderOrder.length + 1; } // providers + summary

async function openProviderWizard() {
  // Fetch current state from backend
  try {
    const myProviders = await adminApi("/api/me/providers");
    _pwConfigured = {};
    _pwProviderOrder.forEach(n => { _pwConfigured[n] = false; });
    myProviders.forEach(p => {
      if (p.has_key) _pwConfigured[p.provider_name] = true;
    });
  } catch {
    _pwProviderOrder.forEach(n => { _pwConfigured[n] = false; });
  }

  // Check if ALL providers are configured
  const allDone = _pwProviderOrder.every(n => _pwConfigured[n]);

  // Find first unconfigured provider, or go to summary
  if (allDone) {
    _pwStep = _pwProviderOrder.length; // summary
  } else {
    _pwStep = _pwProviderOrder.findIndex(n => !_pwConfigured[n]);
    if (_pwStep === -1) _pwStep = 0;
  }

  _pwRender();
  document.getElementById("providerWizardModal").hidden = false;
}

function _pwRender() {
  const total = _pwTotalSteps();
  const isSummary = _pwStep >= _pwProviderOrder.length;

  // Counter
  document.getElementById("pwCounter").textContent = `${_pwStep + 1} / ${total}`;

  // Progress bar
  const pct = (((_pwStep + 1) / total) * 100).toFixed(1);
  document.getElementById("pwProgressFill").style.width = `${pct}%`;

  // Track dots
  const track = document.getElementById("pwTrack");
  track.innerHTML = _pwProviderOrder.map((name, i) => {
    const active = i === _pwStep ? "is-active" : "";
    const done = _pwConfigured[name] ? "is-done" : "";
    const cls = active || done;
    const abbr = name.slice(0, 2).toUpperCase();
    return `<div class="pw__track-dot ${cls}" data-pw-goto="${i}" title="${name}">${_pwConfigured[name] ? "✓" : abbr}</div>`;
  }).join("") + `<div class="pw__track-dot is-summary ${isSummary ? "is-active" : ""}" data-pw-goto="${_pwProviderOrder.length}">FIN</div>`;

  // Click handlers for track dots
  track.querySelectorAll("[data-pw-goto]").forEach(dot => {
    dot.addEventListener("click", () => {
      _pwStep = parseInt(dot.dataset.pwGoto, 10);
      _pwRender();
    });
  });

  // Body
  const body = document.getElementById("pwBody");
  if (isSummary) {
    _pwRenderSummary(body);
  } else {
    _pwRenderProviderStep(body, _pwProviderOrder[_pwStep]);
  }

  // Nav buttons
  document.getElementById("pwPrev").style.visibility = _pwStep > 0 ? "visible" : "hidden";
  document.getElementById("pwSkip").style.display = isSummary ? "none" : "";
  document.getElementById("pwNext").textContent = isSummary ? "CLOSE" : "NEXT →";
}

function _pwRenderProviderStep(body, name) {
  const guide = PROVIDER_GUIDES[name];
  if (!guide) { body.innerHTML = ""; return; }
  const isConfigured = _pwConfigured[name];

  body.innerHTML = `
    <div class="pw__provider-step">
      <div class="pw__provider-name">${escapeHtml(guide.displayName)}</div>
      <div class="pw__provider-status">
        <span class="pw__status-badge ${isConfigured ? "is-configured" : "is-pending"}">
          ${isConfigured ? "✓ CONFIGURED" : "⚠ PENDING"}
        </span>
      </div>
      <div class="pw__free-tier">${escapeHtml(guide.freeTier)}</div>

      ${isConfigured ? `
        <div class="pw__configured-msg">
          <div class="pw__configured-check">✓</div>
          <div class="pw__configured-text">This provider already has an API key configured.</div>
          <button class="ghost-button ghost-button--small pw__reconfigure-btn" id="pwReconfigure">RECONFIGURE</button>
        </div>
        <div id="pwGuideSection" hidden>
          ${_pwGuideHtml(guide, name)}
        </div>
      ` : _pwGuideHtml(guide, name)}
    </div>
  `;

  // Reconfigure button
  const reconf = body.querySelector("#pwReconfigure");
  if (reconf) {
    reconf.addEventListener("click", () => {
      body.querySelector(".pw__configured-msg").hidden = true;
      body.querySelector("#pwGuideSection").hidden = false;
    });
  }

  // Save button
  const saveBtn = body.querySelector("#pwSaveKey");
  if (saveBtn) {
    saveBtn.addEventListener("click", () => _pwSaveKey(name));
  }

  // Enter on input
  const keyInput = body.querySelector("#pwKeyInput");
  if (keyInput) {
    keyInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") _pwSaveKey(name);
    });
    if (!isConfigured) setTimeout(() => keyInput.focus(), 100);
  }
}

function _pwGuideHtml(guide, name) {
  return `
    <ol class="pw__guide-steps">
      ${guide.steps.map((s, i) => `<li><span class="pw__step-num">${i + 1}</span><span class="pw__step-text">${s}</span></li>`).join("")}
    </ol>
    <div class="pw__links">
      <a href="${guide.signupUrl}" target="_blank" rel="noopener" class="ghost-button ghost-button--small">SIGN UP ↗</a>
      <a href="${guide.docsUrl}" target="_blank" rel="noopener" class="ghost-button ghost-button--small">DOCS ↗</a>
    </div>
    <div class="pw__key-section">
      <div class="pw__key-label">API KEY — ${escapeHtml(guide.displayName)}</div>
      <div class="pw__key-row">
        <input type="password" id="pwKeyInput" placeholder="paste your key here..." autocomplete="off" />
        <button class="primary-button" id="pwSaveKey" style="padding:10px 16px">SAVE</button>
      </div>
      <div class="pw__key-saved" id="pwKeySaved">✓ Key saved</div>
      <div class="pw__key-error" id="pwKeyError"></div>
    </div>
  `;
}

async function _pwSaveKey(providerName) {
  const input = document.getElementById("pwKeyInput");
  const savedEl = document.getElementById("pwKeySaved");
  const errorEl = document.getElementById("pwKeyError");
  const key = input?.value.trim();
  if (!key) return;

  savedEl.classList.remove("is-visible");
  errorEl.classList.remove("is-visible");

  try {
    await adminApi(`/api/me/providers/${providerName}`, {
      method: "PATCH",
      body: JSON.stringify({ api_key: key }),
    });
    _pwConfigured[providerName] = true;
    savedEl.classList.add("is-visible");
    input.value = "";
    // Update track dot
    _pwRender();
    // Re-show the saved message after re-render
    setTimeout(() => {
      const s = document.getElementById("pwKeySaved");
      if (s) s.classList.add("is-visible");
    }, 50);
  } catch (e) {
    errorEl.textContent = e.message || "Failed to save";
    errorEl.classList.add("is-visible");
  }
}

function _pwRenderSummary(body) {
  const allDone = _pwProviderOrder.every(n => _pwConfigured[n]);
  const configured = _pwProviderOrder.filter(n => _pwConfigured[n]).length;

  if (allDone) {
    body.innerHTML = `
      <div class="pw__all-done">
        <div class="pw__all-done-icon">◆</div>
        <div class="pw__all-done-text">All providers configured</div>
        <div class="pw__all-done-sub">${configured} / ${_pwProviderOrder.length} providers have an API key — FreeAI will route your requests automatically.</div>
      </div>
    `;
  } else {
    body.innerHTML = `
      <div class="pw__summary">
        <div class="pw__summary-title">Setup summary</div>
        <div class="pw__summary-grid">
          ${_pwProviderOrder.map(name => {
            const guide = PROVIDER_GUIDES[name];
            const ok = _pwConfigured[name];
            return `
              <div class="pw__summary-row">
                <span class="pw__summary-provider">${guide?.displayName || name}</span>
                <span class="pw__summary-status ${ok ? "is-ok" : "is-missing"}">${ok ? "✓ OK" : "NO KEY"}</span>
              </div>
            `;
          }).join("")}
        </div>
        <div class="pw__all-done-sub" style="text-align:center">${configured} / ${_pwProviderOrder.length} configured — you can return to this wizard any time.</div>
      </div>
    `;
  }
}

// Wizard nav event listeners
document.getElementById("pwPrev")?.addEventListener("click", () => {
  if (_pwStep > 0) { _pwStep--; _pwRender(); }
});
document.getElementById("pwNext")?.addEventListener("click", () => {
  if (_pwStep >= _pwProviderOrder.length) {
    // Close wizard
    document.getElementById("providerWizardModal").hidden = true;
    refreshProviders(true);
    return;
  }
  _pwStep++;
  _pwRender();
});
document.getElementById("pwSkip")?.addEventListener("click", () => {
  if (_pwStep < _pwProviderOrder.length) { _pwStep++; _pwRender(); }
});

// Open wizard button
document.getElementById("openProviderWizard")?.addEventListener("click", openProviderWizard);

// Close on backdrop click
document.addEventListener("click", (e) => {
  if (e.target.closest("#providerWizardModal > .modal__backdrop")) {
    document.getElementById("providerWizardModal").hidden = true;
    refreshProviders(true);
  }
});

async function populateModelSelect(select, providerName, currentModel) {
  try {
    const data = await adminApi(`/api/providers/${providerName}/models`);
    select.innerHTML = "";
    data.models.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.note ? `${m.id} (${m.note})` : m.id;
      select.appendChild(opt);
    });
    if (currentModel) {
      const known = data.models.some((m) => m.id === currentModel);
      if (!known) {
        const opt = document.createElement("option");
        opt.value = currentModel;
        opt.textContent = `${currentModel} (custom)`;
        select.appendChild(opt);
      }
      select.value = currentModel;
    }
  } catch {
    select.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = currentModel || "";
    opt.textContent = currentModel || "unavailable";
    select.appendChild(opt);
  }
}

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
    // Add "no key" banner
    const banner = document.createElement("div");
    banner.className = "provider-card__nokey-banner";
    banner.textContent = "⚠ NO API KEY SET — use the Setup Wizard or paste your key below";
    node.querySelector(".provider-card__head").after(banner);
  } else if (!p.healthy) {
    statusLabel.textContent = "quarantined";
    statusDot.classList.add("dot--down");
    node.classList.add("is-error");
    node.classList.add("is-configured");
  } else if (!p.enabled) {
    statusLabel.textContent = "disabled";
    statusDot.classList.add("dot--idle");
    node.classList.add("is-disabled");
    node.classList.add("is-configured");
  } else {
    statusLabel.textContent = "live";
    node.classList.add("is-configured");
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
  populateModelSelect(modelInput, p.name, p.default_model);

  // limit inputs
  const rpmLimitInput = node.querySelector(".rpm-limit-input");
  const rpdLimitInput = node.querySelector(".rpd-limit-input");
  const tpdLimitInput = node.querySelector(".tpd-limit-input");
  const weightInput   = node.querySelector(".weight-input");
  rpmLimitInput.value = p.rpm_limit ?? "";
  rpdLimitInput.value = p.rpd_limit ?? "";
  tpdLimitInput.value = p.tpd_limit ?? "";
  weightInput.value   = p.weight ?? 1.0;

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

  // tokens today meter
  const tpdFill  = node.querySelector(".meter__fill--cyan");
  const tokensValue = node.querySelector(".tokens-value");
  if (tokensValue) {
    const t = p.tokens_today || 0;
    const fmt = t >= 1_000_000 ? (t / 1_000_000).toFixed(1) + "M" : t >= 1000 ? (t / 1000).toFixed(1) + "k" : String(t);
    if (p.tpd_limit) {
      const limFmt = p.tpd_limit >= 1_000_000 ? (p.tpd_limit / 1_000_000).toFixed(1) + "M" : p.tpd_limit >= 1000 ? (p.tpd_limit / 1000).toFixed(0) + "k" : String(p.tpd_limit);
      tokensValue.textContent = `${fmt}/${limFmt}`;
      const pct = Math.min(100, (t / p.tpd_limit) * 100);
      if (tpdFill) {
        tpdFill.style.right = `${100 - pct}%`;
        if (pct > 80) tpdFill.classList.add("is-warn");
      }
    } else {
      tokensValue.textContent = `${fmt}/—`;
      if (tpdFill) tpdFill.style.right = "100%";
    }
  }

  // Show EMA if available, fall back to single sample
  const latVal = node.querySelector(".latency-value");
  if (p.latency_ema_ms != null) {
    latVal.textContent = `${Math.round(p.latency_ema_ms)} ms (ema)`;
  } else if (p.last_latency_ms != null) {
    latVal.textContent = `${p.last_latency_ms} ms`;
  } else {
    latVal.textContent = "—";
  }

  if (p.last_error) {
    const errRow = node.querySelector(".provider-row--error");
    errRow.hidden = false;
    errRow.querySelector(".error-value").textContent = p.last_error;
  }

  const enableToggle = node.querySelector(".enable-toggle");
  enableToggle.checked = p.enabled;
  enableToggle.addEventListener("change", async (e) => {
    try {
      await adminApi(_providerEndpoint(p.name), {
        method: "PATCH",
        body: JSON.stringify({ enabled: e.target.checked }),
      });
      refreshProviders(true);
    } catch (err) {
      if (err instanceof AuthError) showLoginModal();
    }
  });

  node.querySelector(".save-key").addEventListener("click", async () => {
    const body = {};
    if (keyInput.value) body.api_key = keyInput.value;
    if (modelInput.value !== p.default_model) body.default_model = modelInput.value;
    const newRpm = rpmLimitInput.value ? parseInt(rpmLimitInput.value, 10) : null;
    const newRpd = rpdLimitInput.value ? parseInt(rpdLimitInput.value, 10) : null;
    const newTpd = tpdLimitInput.value ? parseInt(tpdLimitInput.value, 10) : null;
    const newWeight = weightInput.value ? parseFloat(weightInput.value) : null;
    if (newRpm !== p.rpm_limit) body.rpm_limit = newRpm;
    if (newRpd !== p.rpd_limit) body.rpd_limit = newRpd;
    if (newTpd !== p.tpd_limit) body.tpd_limit = newTpd;
    if (newWeight !== null && newWeight !== p.weight) body.weight = newWeight;
    if (!Object.keys(body).length) return flashButton(node.querySelector(".save-key"), "NO CHANGE");
    try {
      const result = await adminApi(_providerEndpoint(p.name), { method: "PATCH", body: JSON.stringify(body) });
      flashButton(node.querySelector(".save-key"), "SAVED");
      keyInput.value = "";
      // Show the model warning inline if the backend sent one
      showModelWarning(node, result?.model_warning, result?.model_suggestions, modelInput);
      refreshProviders(true);
    } catch (err) {
      if (err instanceof AuthError) { showAdminModal(); return; }
      flashButton(node.querySelector(".save-key"), "ERROR");
    }
  });

  node.querySelector(".reset-btn").addEventListener("click", async () => {
    try {
      await adminApi(`/api/providers/${p.name}/reset`, { method: "POST" }); // admin-only, hidden for regular users
      refreshProviders(true);
    } catch (err) {
      if (err instanceof AuthError) showLoginModal();
    }
  });

  const wizardBtn = node.querySelector(".wizard-btn");
  if (wizardBtn && PROVIDER_GUIDES[p.name]) {
    wizardBtn.addEventListener("click", () => openSetupWizard(p.name));
  } else if (wizardBtn) {
    wizardBtn.hidden = true;
  }

  return node;
}

/**
 * Lightweight status-only update for a single provider card.
 * Patches meters, latency, error, status dot — skips model dropdown.
 */
function updateProviderCard(node, p) {
  // status
  const statusLabel = node.querySelector(".provider-card__status-label");
  const statusDot   = node.querySelector(".provider-card__status .dot");
  statusDot.className = "dot";
  node.classList.remove("is-disabled", "is-error", "is-configured");
  // Remove existing banner if any
  node.querySelector(".provider-card__nokey-banner")?.remove();
  if (!p.has_key) {
    statusLabel.textContent = "no key";
    statusDot.classList.add("dot--idle");
    node.classList.add("is-disabled");
    const banner = document.createElement("div");
    banner.className = "provider-card__nokey-banner";
    banner.textContent = "⚠ NO API KEY SET — use the Setup Wizard or paste your key below";
    node.querySelector(".provider-card__head").after(banner);
  } else if (!p.healthy) {
    statusLabel.textContent = "quarantined";
    statusDot.classList.add("dot--down");
    node.classList.add("is-error", "is-configured");
  } else if (!p.enabled) {
    statusLabel.textContent = "disabled";
    statusDot.classList.add("dot--idle");
    node.classList.add("is-disabled", "is-configured");
  } else {
    statusLabel.textContent = "live";
    node.classList.add("is-configured");
  }

  // RPM meter
  const rpmFill  = node.querySelector(".meter:nth-child(1) .meter__fill");
  const rpmValue = node.querySelector(".rpm-value");
  rpmFill.classList.remove("is-warn");
  if (p.rpm_limit) {
    const pct = Math.min(100, (p.requests_this_minute / p.rpm_limit) * 100);
    rpmFill.style.right = `${100 - pct}%`;
    if (pct > 80) rpmFill.classList.add("is-warn");
    rpmValue.textContent = `${p.requests_this_minute}/${p.rpm_limit}`;
  } else {
    rpmFill.style.right = "100%";
    rpmValue.textContent = `${p.requests_this_minute}/—`;
  }

  // RPD meter
  const rpdFill  = node.querySelector(".meter:nth-child(2) .meter__fill");
  const rpdValue = node.querySelector(".rpd-value");
  rpdFill.classList.remove("is-warn");
  if (p.rpd_limit) {
    const pct = Math.min(100, (p.requests_today / p.rpd_limit) * 100);
    rpdFill.style.right = `${100 - pct}%`;
    if (pct > 80) rpdFill.classList.add("is-warn");
    rpdValue.textContent = `${p.requests_today}/${p.rpd_limit}`;
  } else {
    rpdFill.style.right = "100%";
    rpdValue.textContent = `${p.requests_today}/—`;
  }

  // tokens today meter
  const tpdFillUpd = node.querySelector(".meter__fill--cyan");
  const tokVal = node.querySelector(".tokens-value");
  if (tokVal) {
    const t = p.tokens_today || 0;
    const fmt = t >= 1_000_000 ? (t / 1_000_000).toFixed(1) + "M" : t >= 1000 ? (t / 1000).toFixed(1) + "k" : String(t);
    if (p.tpd_limit) {
      const limFmt = p.tpd_limit >= 1_000_000 ? (p.tpd_limit / 1_000_000).toFixed(1) + "M" : p.tpd_limit >= 1000 ? (p.tpd_limit / 1000).toFixed(0) + "k" : String(p.tpd_limit);
      tokVal.textContent = `${fmt}/${limFmt}`;
      const pct = Math.min(100, (t / p.tpd_limit) * 100);
      if (tpdFillUpd) {
        tpdFillUpd.classList.remove("is-warn");
        tpdFillUpd.style.right = `${100 - pct}%`;
        if (pct > 80) tpdFillUpd.classList.add("is-warn");
      }
    } else {
      tokVal.textContent = `${fmt}/—`;
      if (tpdFillUpd) tpdFillUpd.style.right = "100%";
    }
  }

  // latency — prefer EMA over single sample
  const latUpd = node.querySelector(".latency-value");
  if (p.latency_ema_ms != null) {
    latUpd.textContent = `${Math.round(p.latency_ema_ms)} ms (ema)`;
  } else if (p.last_latency_ms != null) {
    latUpd.textContent = `${p.last_latency_ms} ms`;
  } else {
    latUpd.textContent = "—";
  }

  // error
  const errRow = node.querySelector(".provider-row--error");
  if (p.last_error) {
    errRow.hidden = false;
    errRow.querySelector(".error-value").textContent = p.last_error;
  } else {
    errRow.hidden = true;
  }

  // enable toggle (sync without re-binding)
  node.querySelector(".enable-toggle").checked = p.enabled;
}

function _isAdmin() {
  const u = getCurrentUser();
  return u && u.role === "admin";
}

function _providerEndpoint(name) {
  // Every user edits their own provider config
  return `/api/me/providers/${name}`;
}

async function refreshProviders(fullRender = false) {
  try {
    // All users see their own providers (catalog + their config merged)
    const [catalog, myProviders] = await Promise.all([
      adminApi("/api/me/providers/catalog"),
      adminApi("/api/me/providers"),
    ]);
    console.log("[providers] catalog:", catalog?.length, "myProviders:", myProviders?.length, myProviders);
    const myMap = {};
    myProviders.forEach(p => { myMap[p.provider_name] = p; });
    console.log("[providers] myMap keys:", Object.keys(myMap), "catalog names:", catalog.map(c => c.name));
    const data = catalog.map(c => {
      const my = myMap[c.name] || {};
      const hasKey = my.has_key || false;
      if (!hasKey && my.provider_name) console.warn("[providers] has_key is false for", c.name, "but my object is:", my);
      return {
        name: c.name,
        enabled: my.enabled ?? c.enabled,
        has_key: hasKey,
        healthy: true,
        requests_today: 0,
        requests_this_minute: 0,
        rpm_limit: my.rpm_limit ?? c.rpm_limit,
        rpd_limit: my.rpd_limit ?? c.rpd_limit,
        tpd_limit: my.tpd_limit ?? c.tpd_limit,
        tokens_today: 0,
        weight: my.weight ?? c.weight,
        last_error: null,
        last_latency_ms: null,
        latency_ema_ms: null,
        tags: c.tags || [],
        default_model: my.default_model ?? c.default_model,
      };
    });
    providersCache = data;

    // If cards already exist and no structural change, patch in-place
    const existingCards = grid.querySelectorAll(".provider-card");
    if (!fullRender && existingCards.length === data.length) {
      data.forEach((p) => {
        const card = grid.querySelector(`.provider-card[data-name="${p.name}"]`);
        if (card) updateProviderCard(card, p);
      });
    } else {
      // Full render: rebuild cards + model dropdowns
      grid.innerHTML = "";
      data.forEach((p) => grid.appendChild(renderProvider(p)));
    }

    updateUplink(true, data);
    updateProviderSelect(data);
  } catch (err) {
    if (err instanceof AuthError) {
      updateUplink(false);
      grid.innerHTML = `<div class="output-empty"><pre>login required\nclick the lock icon</pre></div>`;
      showLoginModal();
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

document.getElementById("refreshProviders").addEventListener("click", () => refreshProviders(true));

// ─────────────── strategy panel ───────────────

const strategyGrid = document.getElementById("strategyGrid");
let configCache = null;
let strategiesCache = [];

async function refreshConfig() {
  try {
    configCache = await adminApi("/api/config");
  } catch (err) {
    if (err instanceof AuthError) showLoginModal();
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

// ─────────────── strategy card render ───────────────

// Pretty-print one DSL clause for the strategy card body. Used both
// for require ("tags ∋ coding") and prefer ("tags ∋ fast (×5)").
function renderClauseShort(c, isPrefer) {
  if (!c || typeof c !== "object") return "";
  const f = c.field || "?";
  const op = c.op || "?";
  const v = Array.isArray(c.value) ? `[${c.value.join(", ")}]` : c.value;
  const sym = op === "contains" ? "∋" : op;
  const w = isPrefer && typeof c.weight === "number" ? ` (×${c.weight})` : "";
  return `${escapeHtml(f)} ${escapeHtml(sym)} ${escapeHtml(String(v))}${w}`;
}

function renderStrategyRules(definition) {
  // For the special `auto` strategy and any strategy with no DSL rules,
  // show a single italic placeholder. The user knows what `auto` does
  // by name; the placeholder is for the rare baseline-only case.
  if (!definition || (
    (!definition.require || definition.require.length === 0) &&
    (!definition.prefer  || definition.prefer.length === 0)
  )) {
    return '<span class="strategy-card__rule-empty">baseline scoring (no DSL rules)</span>';
  }
  const lines = [];
  for (const c of definition.require || []) {
    lines.push(`<span class="strategy-card__rule strategy-card__rule--require">${renderClauseShort(c, false)}</span>`);
  }
  for (const c of definition.prefer || []) {
    lines.push(`<span class="strategy-card__rule strategy-card__rule--prefer">${renderClauseShort(c, true)}</span>`);
  }
  return lines.join("");
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
      <div class="strategy-card__rules">
        ${renderStrategyRules(s.definition)}
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
        if (err instanceof AuthError) showLoginModal();
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
        if (err instanceof AuthError) showLoginModal();
        else alert(err.message);
      }
    });
    strategyGrid.appendChild(card);
  });
}

// ─────────────── strategy editor — DSL form builder ───────────────

// In-memory state for the editor. The DOM is rerendered from this on
// every change so we never have to keep IDs in sync. Each clause is a
// dict matching the DSL JSON shape; the editor mutates these in place.
let _stratEditorExisting = null;
let _editorState = { require: [], prefer: [] };

// Vocabulary loaded once when the editor opens. Populates the value
// dropdown when field === "tags" so the user can only pick tags that
// actually exist on a provider.
let _editorTagsCache = [];

// Schema mirror of FIELD_TYPES / OPS_BY_TYPE in app/strategy_dsl.py.
// Kept here so the editor can offer the right operators per field
// without a round-trip. If the backend gains a field, add it here too.
const DSL_FIELDS = {
  tags:                 { type: "string_array" },
  name:                 { type: "string" },
  weight:               { type: "number" },
  enabled:              { type: "bool" },
  last_latency_ms:      { type: "number" },
  // EMA of latency (alpha=0.3). More stable than last_latency_ms —
  // prefer this for latency-based strategy rules.
  latency_ema_ms:       { type: "number" },
  requests_today:       { type: "number" },
  requests_this_minute: { type: "number" },
  rpd_remaining:        { type: "number" },
  rpm_remaining:        { type: "number" },
  total_failures:       { type: "number" },
};
const DSL_OPS_BY_TYPE = {
  string_array: ["contains"],
  string:       ["==", "!=", "in"],
  number:       ["==", "!=", "<", "<=", ">", ">="],
  bool:         ["==", "!="],
};

function _opsFor(fieldName) {
  const t = DSL_FIELDS[fieldName]?.type || "string";
  return DSL_OPS_BY_TYPE[t] || ["=="];
}

// Best-effort guess at the right input type for a clause's value.
function _valueInputType(fieldName) {
  const t = DSL_FIELDS[fieldName]?.type;
  if (t === "number") return "number";
  return "text";
}

// Coerce a string the user typed into the right value type for the field.
function _coerceValue(fieldName, raw) {
  const t = DSL_FIELDS[fieldName]?.type;
  if (raw === "" || raw == null) return raw;
  if (t === "number") {
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  if (t === "bool") return raw === "true";
  return raw;
}

function _renderClauseRow(clause, idx, isPrefer) {
  // Build a row for one clause. Fully replaces itself on every state
  // mutation — no event delegation to maintain.
  const row = document.createElement("div");
  row.className = "dsl-clause" + (isPrefer ? " dsl-clause--prefer" : "");

  const fieldSel = document.createElement("select");
  fieldSel.className = "dsl-clause__field";
  Object.keys(DSL_FIELDS).forEach((f) => {
    const opt = document.createElement("option");
    opt.value = f;
    opt.textContent = f;
    if (f === clause.field) opt.selected = true;
    fieldSel.appendChild(opt);
  });
  fieldSel.addEventListener("change", () => {
    clause.field = fieldSel.value;
    // Reset the operator to the first valid one for the new field type.
    clause.op = _opsFor(clause.field)[0];
    // Reset value if the type changes drastically.
    if (DSL_FIELDS[clause.field].type === "number") clause.value = 0;
    else clause.value = "";
    _renderEditorClauses();
    _schedulePreview();
  });

  const opSel = document.createElement("select");
  opSel.className = "dsl-clause__op";
  _opsFor(clause.field).forEach((op) => {
    const opt = document.createElement("option");
    opt.value = op;
    opt.textContent = op;
    if (op === clause.op) opt.selected = true;
    opSel.appendChild(opt);
  });
  opSel.addEventListener("change", () => {
    clause.op = opSel.value;
    _schedulePreview();
  });

  // Value: a tag dropdown when field=tags (vocabulary discovery), a
  // typed input otherwise.
  let valueEl;
  if (clause.field === "tags" && clause.op === "contains") {
    valueEl = document.createElement("select");
    valueEl.className = "dsl-clause__value";
    if (_editorTagsCache.length === 0) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "(no tags discovered)";
      valueEl.appendChild(o);
    }
    _editorTagsCache.forEach((t) => {
      const o = document.createElement("option");
      o.value = t.tag;
      o.textContent = `${t.tag} (${t.providers.length} provider${t.providers.length === 1 ? "" : "s"})`;
      if (t.tag === clause.value) o.selected = true;
      valueEl.appendChild(o);
    });
    if (!_editorTagsCache.find((t) => t.tag === clause.value) && clause.value) {
      // Custom value the user typed before — keep it as an option.
      const o = document.createElement("option");
      o.value = clause.value;
      o.textContent = `${clause.value} (custom — won't match yet)`;
      o.selected = true;
      valueEl.appendChild(o);
    }
    valueEl.addEventListener("change", () => {
      clause.value = valueEl.value;
      _schedulePreview();
    });
  } else {
    valueEl = document.createElement("input");
    valueEl.className = "dsl-clause__value";
    valueEl.type = _valueInputType(clause.field);
    valueEl.value = clause.value ?? "";
    valueEl.placeholder = "value";
    valueEl.addEventListener("input", () => {
      clause.value = _coerceValue(clause.field, valueEl.value);
      _schedulePreview();
    });
  }

  row.appendChild(fieldSel);
  row.appendChild(opSel);
  row.appendChild(valueEl);

  if (isPrefer) {
    const wLabel = document.createElement("span");
    wLabel.className = "dsl-clause__weight-label";
    wLabel.textContent = "weight";
    row.appendChild(wLabel);

    const wInput = document.createElement("input");
    wInput.className = "dsl-clause__weight";
    wInput.type = "number";
    wInput.step = "0.5";
    wInput.value = clause.weight ?? 5;
    wInput.addEventListener("input", () => {
      clause.weight = Number(wInput.value);
      _schedulePreview();
    });
    row.appendChild(wInput);
  }

  const del = document.createElement("button");
  del.type = "button";
  del.className = "dsl-clause__del";
  del.textContent = "×";
  del.title = "remove clause";
  del.addEventListener("click", () => {
    const arr = isPrefer ? _editorState.prefer : _editorState.require;
    arr.splice(idx, 1);
    _renderEditorClauses();
    _schedulePreview();
  });
  row.appendChild(del);

  return row;
}

function _renderEditorClauses() {
  const reqList = document.getElementById("dslRequireList");
  const prefList = document.getElementById("dslPreferList");
  reqList.innerHTML = "";
  prefList.innerHTML = "";
  _editorState.require.forEach((c, i) => {
    reqList.appendChild(_renderClauseRow(c, i, false));
  });
  _editorState.prefer.forEach((c, i) => {
    prefList.appendChild(_renderClauseRow(c, i, true));
  });
}

function _editorDefinitionForApi() {
  // Build the JSON shape the API expects from the in-memory state.
  // Filter out clauses with an empty value so we don't send half-edited rows.
  const cleanClause = (c, isPrefer) => {
    const out = { field: c.field, op: c.op, value: c.value };
    if (isPrefer) out.weight = Number(c.weight) || 0;
    return out;
  };
  return {
    require: _editorState.require
      .filter((c) => c.value !== "" && c.value != null)
      .map((c) => cleanClause(c, false)),
    prefer: _editorState.prefer
      .filter((c) => c.value !== "" && c.value != null)
      .map((c) => cleanClause(c, true)),
  };
}

// ─────── live preview ───────

let _previewTimer = null;

function _schedulePreview() {
  if (_previewTimer) clearTimeout(_previewTimer);
  _previewTimer = setTimeout(_runPreview, 300);
}

async function _runPreview() {
  const status = document.getElementById("dslPreviewStatus");
  const list = document.getElementById("dslPreviewList");
  const warningsEl = document.getElementById("dslPreviewWarnings");
  status.textContent = "running…";
  try {
    const result = await adminApi("/api/strategies/preview", {
      method: "POST",
      body: JSON.stringify({ definition: _editorDefinitionForApi() }),
    });
    list.innerHTML = "";
    if (result.candidates.length === 0) {
      const empty = document.createElement("div");
      empty.className = "strategy-card__rule-empty";
      empty.textContent = "no provider would match this strategy right now";
      list.appendChild(empty);
    } else {
      const max = Math.max(...result.candidates.map((c) => c.score));
      result.candidates.forEach((c) => {
        const row = document.createElement("div");
        row.className = "dsl-preview__row";
        row.innerHTML = `
          <span class="dsl-preview__name">${escapeHtml(c.name)}</span>
          <span class="dsl-preview__score">${c.score.toFixed(2)}</span>
          <span class="dsl-preview__bar" style="width:${Math.max(8, (c.score / max) * 100)}%"></span>
        `;
        list.appendChild(row);
      });
    }
    if (result.excluded.length > 0) {
      result.excluded.forEach((name) => {
        const row = document.createElement("div");
        row.className = "dsl-preview__row dsl-preview__row--excluded";
        row.innerHTML = `
          <span class="dsl-preview__name">${escapeHtml(name)}</span>
          <span class="dsl-preview__score">excluded</span>
          <span></span>
        `;
        list.appendChild(row);
      });
    }
    if (result.warnings && result.warnings.length > 0) {
      warningsEl.innerHTML = result.warnings
        .map((w) => `<span>⚠ ${escapeHtml(w)}</span>`).join("");
      warningsEl.hidden = false;
    } else {
      warningsEl.hidden = true;
    }
    status.textContent = `${result.candidates.length} match · ${result.excluded.length} excluded`;
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    status.textContent = "preview error";
    list.innerHTML = `<div class="strategy-card__rule-empty">${escapeHtml(err.message)}</div>`;
    warningsEl.hidden = true;
  }
}

// ─────── editor open / save / cancel ───────

async function openStrategyEditor(existing) {
  _stratEditorExisting = existing;
  // Hydrate the editor state from the strategy's definition (if any).
  const def = existing?.definition || { require: [], prefer: [] };
  _editorState = {
    require: (def.require || []).map((c) => ({ ...c })),
    prefer:  (def.prefer  || []).map((c) => ({ ...c })),
  };

  const modal = document.getElementById("strategyEditorModal");
  const nameInput = document.getElementById("stratEditorName");
  const descInput = document.getElementById("stratEditorDesc");
  const errEl = document.getElementById("stratEditorError");
  errEl.hidden = true;

  document.getElementById("stratEditorTitle").textContent = existing ? "EDIT STRATEGY" : "NEW STRATEGY";
  nameInput.value = existing ? existing.name : "";
  nameInput.disabled = !!existing;
  descInput.value = existing?.description || "";

  // Load the tag vocabulary every time the editor opens. Cheap (one
  // round-trip) and stays fresh against provider edits.
  try {
    _editorTagsCache = await adminApi("/api/tags");
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    _editorTagsCache = [];
  }

  _renderEditorClauses();
  modal.hidden = false;
  setTimeout(() => (existing ? descInput : nameInput).focus(), 50);
  // Run an initial preview so the user sees what the current state ranks.
  _schedulePreview();
}

document.getElementById("dslAddRequire").addEventListener("click", () => {
  _editorState.require.push({ field: "tags", op: "contains", value: "" });
  _renderEditorClauses();
});
document.getElementById("dslAddPrefer").addEventListener("click", () => {
  _editorState.prefer.push({ field: "tags", op: "contains", value: "", weight: 5 });
  _renderEditorClauses();
});

document.getElementById("stratEditorCancel").addEventListener("click", () => {
  document.getElementById("strategyEditorModal").hidden = true;
});

document.getElementById("stratEditorSave").addEventListener("click", async () => {
  const nameInput = document.getElementById("stratEditorName");
  const descInput = document.getElementById("stratEditorDesc");
  const errEl = document.getElementById("stratEditorError");
  errEl.hidden = true;

  const name = nameInput.value.trim();
  if (!name || !/^[a-z0-9_]+$/.test(name)) {
    errEl.textContent = "Name must use a-z, 0-9, underscore only.";
    errEl.hidden = false;
    return;
  }
  const description = descInput.value.trim();
  const definition = _editorDefinitionForApi();
  const method = _stratEditorExisting ? "PATCH" : "POST";
  const path = _stratEditorExisting ? `/api/strategies/${name}` : "/api/strategies";

  try {
    await adminApi(path, {
      method,
      body: JSON.stringify({ name, definition, description }),
    });
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
    if (err instanceof AuthError) showLoginModal();
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

function getPlaygroundHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  const token = getAccessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

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
  const res = await fetch(`${API_BASE}/v1/chat/completions`, {
    method: "POST",
    headers: getPlaygroundHeaders(),
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
    headers: getPlaygroundHeaders({ "Accept": "text/event-stream" }),
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

// ─────────────── audio transcription ───────────────

document.getElementById("pgTranscribe").addEventListener("click", async () => {
  const fileInput = document.getElementById("pgAudioFile");
  const out = document.getElementById("pgAudioOutput");
  const btn = document.getElementById("pgTranscribe");

  if (!fileInput.files.length) {
    out.innerHTML = `<div class="output-meta"><span>error</span></div><div class="output-body">Select an audio file first.</div>`;
    return;
  }

  const formData = new FormData();
  formData.append("file", fileInput.files[0]);
  formData.append("model", "whisper-1");
  const lang = document.getElementById("pgAudioLang").value.trim();
  if (lang) formData.append("language", lang);

  out.innerHTML = `<div class="output-empty"><pre>┄┄ transcribing ┄┄</pre></div>`;
  btn.disabled = true;

  try {
    const headers = {};
    const token = getAccessToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const res = await fetch(`${API_BASE}/v1/audio/transcriptions`, {
      method: "POST",
      headers,
      body: formData,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text);
    }
    const data = await res.json();
    out.innerHTML = `
      <div class="output-meta">
        <span>provider: <b>${escapeHtml(data.provider || "unknown")}</b></span>
        <span>model: <b>${escapeHtml(data.model || "unknown")}</b></span>
        <span>${data.latency_ms || 0} ms</span>
        ${data.fallback_position > 1 ? `<span>fallback #${data.fallback_position}</span>` : ""}
      </div>
      <div class="output-body">${escapeHtml(data.text)}</div>
    `;
  } catch (e) {
    out.innerHTML = `<div class="output-meta"><span>error</span></div><div class="output-body">${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    refreshProviders();
  }
});

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
        if (err instanceof AuthError) showLoginModal();
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
    if (err instanceof AuthError) showLoginModal();
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
  renderByClient(data);
  // Enriched analytics (Sprint 7)
  renderKpisExtra(data);
  renderErrorsByKind(data);
  renderByModel(data);
  renderFallbackChain(data);
  renderTokenSplit(data);
  renderHourlyPattern(data);
}

// ─────────────── historical analytics (rollups) ───────────────

const historicalDaysSel = document.getElementById("historicalDays");
const historicalBlock = document.getElementById("historicalBlock");

async function refreshHistorical() {
  if (!historicalDaysSel) return;
  const days = parseInt(historicalDaysSel.value, 10);
  let data;
  try {
    data = await adminApi(`/api/analytics/historical?days=${days}`);
  } catch (err) {
    if (err instanceof AuthError) { showAdminModal(); return; }
    const el = document.getElementById("historicalKpis");
    if (el) el.innerHTML = `<div class="chart-card__empty">${escapeHtml(err.message)}</div>`;
    return;
  }
  renderHistoricalKpis(data);
  renderHistoricalDaily(data);
  renderHistoricalByProvider(data);
  renderHistoricalByModel(data);
}

// Chart rendering functions live in charts.js (loaded before this file).

document.getElementById("analyticsRefresh").addEventListener("click", refreshAnalytics);
analyticsWindowSel.addEventListener("change", refreshAnalytics);

if (historicalDaysSel) {
  // Lazy-load: only fetch the first time the user expands the section, then
  // refetch when they change the range or click refresh.
  let historicalLoaded = false;
  historicalBlock.addEventListener("toggle", () => {
    if (historicalBlock.open && !historicalLoaded) {
      historicalLoaded = true;
      refreshHistorical();
    }
  });
  historicalDaysSel.addEventListener("change", refreshHistorical);
  document.getElementById("historicalRefresh").addEventListener("click", refreshHistorical);
}

// ─────────────── dark mode ───────────────

(function initTheme() {
  const saved = localStorage.getItem("freeai_theme");
  let useDark = false;
  if (saved === "dark" || saved === "light") {
    useDark = saved === "dark";
  } else {
    // No preference saved — respect system setting
    useDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  if (useDark) {
    document.body.classList.add("dark");
    const icon = document.getElementById("themeIcon");
    if (icon) icon.textContent = "◑";
  }
  // Listen for system theme changes (only when no explicit preference)
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (localStorage.getItem("freeai_theme")) return; // user chose manually
    document.body.classList.toggle("dark", e.matches);
    const icon = document.getElementById("themeIcon");
    if (icon) icon.textContent = e.matches ? "◑" : "◐";
  });
})();

document.getElementById("themeToggle").addEventListener("click", () => {
  document.body.classList.toggle("dark");
  const isDark = document.body.classList.contains("dark");
  localStorage.setItem("freeai_theme", isDark ? "dark" : "light");
  document.getElementById("themeIcon").textContent = isDark ? "◑" : "◐";
});

// ─────────────── savings estimator ───────────────

// GPT-4o pricing (per 1K tokens, blended input/output estimate)
const GPT4O_COST_PER_1K = 0.00375; // ~$2.50/1M input + $10/1M output, blended

function updateSavingsBadge(totalTokens) {
  const el = document.getElementById("savingsAmount");
  const sub = document.getElementById("savingsSub");
  const wrapper = document.getElementById("savingsValue");
  if (!el) return;

  const cost = (totalTokens / 1000) * GPT4O_COST_PER_1K;
  const formatted = cost < 0.01 ? cost.toFixed(4)
    : cost < 1 ? cost.toFixed(2)
    : cost < 100 ? cost.toFixed(2)
    : cost.toFixed(0);

  // Animate glow on update
  if (wrapper) {
    wrapper.classList.add("is-updating");
    setTimeout(() => wrapper.classList.remove("is-updating"), 600);
  }

  el.textContent = formatted;

  // Token count summary
  if (sub) {
    const tkFmt = totalTokens >= 1_000_000 ? (totalTokens / 1_000_000).toFixed(1) + "M"
      : totalTokens >= 1_000 ? (totalTokens / 1_000).toFixed(1) + "k"
      : String(totalTokens);
    sub.textContent = `${tkFmt} tokens // $0 cost`;
  }
}

// Fetch lifetime savings (max window = 7 days)
async function refreshSavings() {
  try {
    const data = await adminApi("/api/analytics?window_seconds=604800&bucket_count=1");
    updateSavingsBadge(data.total_tokens || 0);
  } catch {
    // silently fail — badge stays at last known value
  }
}

// ─────────────── boot ───────────────

// ─────────────── user management (admin) ───────────────

// Palette cycled across user series (up to 5 users). Picked to read in both themes.
const USER_SERIES_COLORS = ["#ff7a18", "#14d6c4", "#6da34d", "#ffb066", "#ff4d4d"];

function formatRelative(ts) {
  if (!ts) return "never";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

function sparkline(daily, color) {
  if (!daily || !daily.length) return "";
  const w = 72, h = 22;
  const max = Math.max(...daily.map(d => d.calls), 1);
  const stepX = w / Math.max(daily.length - 1, 1);
  const pts = daily.map((d, i) => [
    (i * stepX).toFixed(1),
    (h - (d.calls / max) * (h - 2)).toFixed(1),
  ]);
  const path = pts.map((p, i) => (i ? "L" : "M") + p[0] + "," + p[1]).join(" ");
  const area = `M0,${h} ${path.slice(1)} L${w},${h} Z`;
  return `<svg class="user-card__spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${area}" fill="${color}" fill-opacity="0.18" />
    <path d="${path}" fill="none" stroke="${color}" stroke-width="1.4" />
  </svg>`;
}

function renderUsersKpis(payload) {
  const el = document.getElementById("usersKpiGrid");
  if (!el) return;
  const users = payload.users;
  const totalUsers = users.length;
  const activeToday = users.filter(u => u.last_seen && (Date.now() / 1000 - u.last_seen) < 86400).length;
  const totalCalls = users.reduce((a, u) => a + u.calls, 0);
  const totalTokens = users.reduce((a, u) => a + u.tokens, 0);
  el.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">USERS</div>
      <div class="kpi-card__value">${totalUsers}<span class="kpi-card__unit">&nbsp;/&nbsp;5</span></div>
      <div class="kpi-card__sub"><b>${activeToday}</b> active in last 24h</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">TOTAL&nbsp;CALLS&nbsp;(7D)</div>
      <div class="kpi-card__value">${totalCalls.toLocaleString()}</div>
      <div class="kpi-card__sub">across all users</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">TOTAL&nbsp;TOKENS&nbsp;(7D)</div>
      <div class="kpi-card__value">${formatTokens(totalTokens)}</div>
      <div class="kpi-card__sub">prompt + completion</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">DAYS&nbsp;COVERED</div>
      <div class="kpi-card__value">${payload.days}<span class="kpi-card__unit">&nbsp;d</span></div>
      <div class="kpi-card__sub">rolling window</div>
    </div>
  `;
}

function renderUserCards(users) {
  const list = document.getElementById("userList");
  if (!list) return;
  const me = getCurrentUser()?.id;
  if (!users.length) {
    list.innerHTML = `<div class="chart-card__empty">no users yet</div>`;
    return;
  }
  list.innerHTML = users.map((u, i) => {
    const color = USER_SERIES_COLORS[i % USER_SERIES_COLORS.length];
    const rate = u.success_rate == null
      ? "&mdash;"
      : `${(u.success_rate * 100).toFixed(1)}%`;
    const tokFmt = formatTokens(u.tokens);
    const isMe = u.id === me;
    return `
      <article class="user-card" data-user-color="${color}">
        <header class="user-card__head">
          <span class="user-card__chip" style="background:${color}"></span>
          <div class="user-card__identity">
            <div class="user-card__name">${escapeHtml(u.username)}${isMe ? ' <span class="user-card__me">(you)</span>' : ''}</div>
            <div class="user-card__meta">ID&nbsp;${u.id} · ${escapeHtml(u.role)} · joined ${new Date(u.created_at * 1000).toISOString().slice(0, 10)}</div>
          </div>
          ${isMe ? '' : `<button class="ghost-button user-card__del" data-delete="${u.id}" data-username="${escapeHtml(u.username)}">DELETE</button>`}
        </header>

        <dl class="user-card__stats">
          <div><dt>PROVIDERS</dt><dd><b>${u.providers_active}</b><span class="user-card__stat-sub">&nbsp;/&nbsp;${u.providers_configured}</span></dd></div>
          <div><dt>CLIENTS</dt><dd><b>${u.clients_enabled}</b><span class="user-card__stat-sub">&nbsp;/&nbsp;${u.clients_configured}</span></dd></div>
          <div><dt>CALLS&nbsp;7D</dt><dd><b>${u.calls.toLocaleString()}</b></dd></div>
          <div><dt>TOKENS&nbsp;7D</dt><dd><b>${tokFmt}</b></dd></div>
          <div><dt>SUCCESS</dt><dd><b>${rate}</b></dd></div>
          <div><dt>LAST&nbsp;SEEN</dt><dd><b>${formatRelative(u.last_seen)}</b></dd></div>
        </dl>

        <div class="user-card__spark-wrap">
          <span class="user-card__spark-label">ACTIVITY&nbsp;7D</span>
          ${sparkline(u.daily, color)}
        </div>
      </article>`;
  }).join("");

  list.querySelectorAll("[data-delete]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Delete user ${btn.dataset.username}?`)) return;
      await adminApi(`/api/users/${btn.dataset.delete}`, { method: "DELETE" });
      refreshUsers();
    });
  });
}

function renderUserActivityChart(users) {
  const el = document.getElementById("chartUserActivity");
  const legend = document.getElementById("chartUserActivityLegend");
  if (!el) return;
  if (!users.length || users.every(u => u.daily.every(d => d.calls === 0))) {
    el.innerHTML = `<div class="chart-card__empty">no activity in the selected window</div>`;
    if (legend) legend.innerHTML = "";
    return;
  }

  const w = 800, h = 240, pad = 36;
  const days = users[0].daily.map(d => d.day);
  const maxCalls = Math.max(1, ...users.flatMap(u => u.daily.map(d => d.calls)));
  const stepX = (w - 2 * pad) / Math.max(days.length - 1, 1);

  const ticks = 4;
  const grid = [];
  for (let i = 0; i <= ticks; i++) {
    const y = pad + (i * (h - 2 * pad)) / ticks;
    const label = Math.round(maxCalls * (1 - i / ticks));
    grid.push(
      `<line class="svg-grid" x1="${pad}" x2="${w - pad}" y1="${y}" y2="${y}" />`,
      `<text class="svg-label" x="${pad - 6}" y="${y + 3}" text-anchor="end">${label}</text>`,
    );
  }
  const xLabels = [0, Math.floor(days.length / 2), days.length - 1].map(i => {
    const x = pad + i * stepX;
    return `<text class="svg-label" x="${x}" y="${h - pad + 14}" text-anchor="middle">${days[i].slice(5)}</text>`;
  });

  const paths = users.map((u, i) => {
    const color = USER_SERIES_COLORS[i % USER_SERIES_COLORS.length];
    const pts = u.daily.map((d, j) => [
      pad + j * stepX,
      h - pad - (d.calls / maxCalls) * (h - 2 * pad),
    ]);
    const d = pts.map((p, j) => (j ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
    const dots = pts.map(p => `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.4" fill="${color}" />`).join("");
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" />${dots}`;
  }).join("");

  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
    ${grid.join("")}
    ${paths}
    ${xLabels.join("")}
  </svg>`;

  if (legend) {
    legend.innerHTML = users.map((u, i) => {
      const color = USER_SERIES_COLORS[i % USER_SERIES_COLORS.length];
      return `<span class="chart-legend__item"><i style="background:${color}"></i>${escapeHtml(u.username)}</span>`;
    }).join("");
  }
}

function renderUserUsageChart(users) {
  const el = document.getElementById("chartUserUsage");
  if (!el) return;
  const rows = users.slice().sort((a, b) => b.calls - a.calls);
  const max = Math.max(1, ...rows.map(u => u.calls));
  if (max === 1 && rows.every(u => u.calls === 0)) {
    el.innerHTML = `<div class="chart-card__empty">no usage in the selected window</div>`;
    return;
  }
  el.innerHTML = `
    <ul class="user-usage-list">
      ${rows.map((u, i) => {
        const color = USER_SERIES_COLORS[users.indexOf(u) % USER_SERIES_COLORS.length];
        const wPct = (u.calls / max) * 100;
        const successPct = u.calls > 0 ? (u.success / u.calls) * 100 : 0;
        const failPct = 100 - successPct;
        return `
        <li class="user-usage-row">
          <span class="user-usage-row__name" style="border-left-color:${color}">${escapeHtml(u.username)}</span>
          <div class="user-usage-row__bar" style="width:${wPct.toFixed(1)}%">
            <span class="user-usage-row__seg user-usage-row__seg--ok" style="flex:${successPct}"></span>
            <span class="user-usage-row__seg user-usage-row__seg--fail" style="flex:${failPct}"></span>
          </div>
          <span class="user-usage-row__stat"><b>${u.calls.toLocaleString()}</b> calls</span>
          <span class="user-usage-row__stat user-usage-row__stat--dim">${formatTokens(u.tokens)} tk</span>
        </li>`;
      }).join("")}
    </ul>
  `;
}

async function refreshUsers() {
  const list = document.getElementById("userList");
  if (!list) return;
  try {
    const payload = await adminApi("/api/users/analytics?days=7");
    renderUsersKpis(payload);
    renderUserCards(payload.users);
    renderUserActivityChart(payload.users);
    renderUserUsageChart(payload.users);
  } catch (e) {
    list.innerHTML = `<div class="chart-card__empty" style="color:var(--rose)">${escapeHtml(e.message)}</div>`;
  }
}

document.getElementById("refreshUsers")?.addEventListener("click", refreshUsers);

document.getElementById("userCreate")?.addEventListener("click", async () => {
  const username = document.getElementById("newUsername").value.trim();
  const password = document.getElementById("newPassword").value;
  if (!username || !password) return;
  if (password.length < 8) { alert("Password must be at least 8 characters"); return; }
  try {
    await adminApi("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password, password_confirm: password }),
    });
    document.getElementById("newUsername").value = "";
    document.getElementById("newPassword").value = "";
    refreshUsers();
  } catch (e) {
    alert(e.message);
  }
});


async function boot() {
  updateLockUI();

  // Check auth status — determines if we need setup, migration, or login
  try {
    const authStatus = await publicApi("/api/auth/status");

    if (authStatus.status === "needs_setup") {
      // Fresh install — check old setup wizard
      const st = await publicApi("/api/setup/status");
      if (st.needs_initial_setup) {
        openFirstRunSetup(st.provider_names);
        return;
      }
      // No legacy setup needed either — show registration
      if (!isLoggedIn()) {
        document.getElementById("loginTitle").textContent = "CREATE ADMIN";
        document.getElementById("loginSubtitle").textContent = "Create the first admin account.";
        showLoginModal();
        // Override login to register instead for first user
        const origHandler = document.getElementById("loginSubmit").onclick;
        document.getElementById("loginSubmit").onclick = async () => {
          const username = document.getElementById("loginUsername").value.trim();
          const password = document.getElementById("loginPassword").value;
          const errEl = document.getElementById("loginError");
          if (!username || !password) return;
          if (password.length < 8) {
            errEl.textContent = "Password must be at least 8 characters";
            errEl.style.display = "block";
            return;
          }
          try {
            const res = await fetch(`${API_BASE}/api/auth/register`, {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({ username, password, password_confirm: password }),
            });
            if (!res.ok) {
              const d = await res.json().catch(() => ({}));
              errEl.textContent = d.detail || "Registration failed";
              errEl.style.display = "block";
              return;
            }
            saveSession(await res.json());
            hideLoginModal();
            document.getElementById("loginTitle").textContent = "SIGN IN";
            document.getElementById("loginSubtitle").textContent = "Enter your credentials to access the control panel.";
            await boot();
          } catch (e) {
            errEl.textContent = e.message;
            errEl.style.display = "block";
          }
        };
        return;
      }
    } else if (authStatus.status === "needs_migration") {
      // Legacy admin token exists but no users — show migration wizard
      if (!isLoggedIn()) {
        showMigrateModal();
        return;
      }
    } else {
      // Ready — users exist
      if (!isLoggedIn()) {
        showLoginModal();
        return;
      }
      // Validate current session
      if (tokenNeedsRefresh() && !(await refreshAccessToken())) {
        showLoginModal();
        return;
      }
    }
  } catch {
    // Backend down — try anyway if logged in
    if (!isLoggedIn()) {
      showLoginModal();
      return;
    }
  }

  await Promise.all([refreshProviders(true), refreshConfig(), refreshSavings()]);
}

(async function start() {
  await boot();
  setInterval(() => {
    const activePanel = document.querySelector('.panel--active');
    if (!activePanel) return;
    const panel = activePanel.dataset.panel;
    if (panel === "providers") { refreshProviders(); refreshSavings(); }
    if (panel === "analytics") refreshAnalytics();
    if (panel === "users") refreshUsers();
  }, 8000);
})();
