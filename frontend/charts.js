// FreeAI analytics chart rendering — extracted from app.js for maintainability.
// Pure functions: receive data, write into DOM elements. No state, no fetching.

function escapeSvg(s) {
  return String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function formatTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function renderKpis(data) {
  const el = document.getElementById("kpiGrid");
  const success = data.success_calls;
  const fail = data.failed_calls;
  const pct = data.total_calls ? Math.round(data.success_rate * 1000) / 10 : 0;
  el.innerHTML = `
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
