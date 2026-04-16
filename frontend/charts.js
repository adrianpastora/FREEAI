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
  const labelW = 75, valW = 70, gap = 6;
  const barZone = 140;
  const w = labelW + barZone + gap + valW;
  const rowH = 28;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    const successRate = r.calls ? Math.round((r.success / r.calls) * 100) : 0;
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(r.provider)}</text>
      <rect class="svg-bar" x="${labelW}" y="${y + 4}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls} · ${successRate}%</text>
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

function renderByClient(data) {
  const el = document.getElementById("chartByClient");
  const rows = data.by_client;
  if (!rows || !rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no client data</div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const labelW = 85, valW = 80, gap = 6;
  const barZone = 130;
  const w = labelW + barZone + gap + valW;
  const rowH = 28;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    const successRate = r.calls ? Math.round((r.success / r.calls) * 100) : 0;
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(r.client)}</text>
      <rect class="svg-bar svg-bar--cyan" x="${labelW}" y="${y + 4}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls} · ${formatTokens(r.tokens)}t</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function renderByStrategy(data) {
  const el = document.getElementById("chartByStrategy");
  const rows = data.by_strategy;
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no data</div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const labelW = 90, valW = 40, gap = 6;
  const barZone = 140;
  const w = labelW + barZone + gap + valW;
  const rowH = 26;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(r.strategy)}</text>
      <rect class="svg-bar" x="${labelW}" y="${y + 3}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls}</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

// ─────────────── Sprint 7: enriched analytics ───────────────

function renderKpisExtra(data) {
  const el = document.getElementById("kpiGridExtra");
  if (!el) return;
  const p50 = data.p50_latency_ms ?? 0;
  const p95 = data.p95_latency_ms ?? 0;
  const p99 = data.p99_latency_ms ?? 0;
  const ttfb = data.avg_ttfb_ms;
  const promptT = data.prompt_tokens ?? 0;
  const completionT = data.completion_tokens ?? 0;
  const totalT = promptT + completionT;
  const promptPct = totalT ? Math.round((promptT / totalT) * 100) : 0;
  const completionPct = totalT ? 100 - promptPct : 0;
  const fallbackTotal = (data.fallback_hist || []).reduce((s, r) => s + r.calls, 0);
  const fallbackFirst = (data.fallback_hist || []).find((r) => r.position === 1)?.calls || 0;
  const firstHitPct = fallbackTotal ? Math.round((fallbackFirst / fallbackTotal) * 100) : 0;

  el.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">LATENCY&nbsp;PERCENTILES</div>
      <div class="kpi-card__value">${p95}<span class="kpi-card__unit">ms</span></div>
      <dl class="kpi-card__stack">
        <dt>P50</dt><dd>${p50} ms</dd>
        <dt>P95</dt><dd><b>${p95} ms</b></dd>
        <dt>P99</dt><dd>${p99} ms</dd>
      </dl>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">TTFB&nbsp;AVG</div>
      <div class="kpi-card__value">${ttfb ?? "—"}<span class="kpi-card__unit">${ttfb != null ? "ms" : ""}</span></div>
      <div class="kpi-card__sub">streaming only</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">TOKEN&nbsp;MIX</div>
      <div class="kpi-card__value">${promptPct}<span class="kpi-card__unit">/${completionPct}%</span></div>
      <div class="kpi-card__sub">prompt · completion</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">FIRST-HIT&nbsp;RATE</div>
      <div class="kpi-card__value">${firstHitPct}<span class="kpi-card__unit">%</span></div>
      <div class="kpi-card__sub">served without fallback</div>
    </div>
  `;
}

function renderTokenSplit(data) {
  const el = document.getElementById("chartTokenSplit");
  if (!el) return;
  const promptT = data.prompt_tokens ?? 0;
  const completionT = data.completion_tokens ?? 0;
  const total = promptT + completionT;
  if (!total) {
    el.innerHTML = `<div class="chart-card__empty">no tokens yet</div>`;
    return;
  }
  const promptPct = (promptT / total) * 100;
  const completionPct = 100 - promptPct;
  el.innerHTML = `
    <div class="token-split">
      <div class="token-split__bar">
        <div class="token-split__seg token-split__seg--in" style="width:${promptPct.toFixed(1)}%;" title="prompt: ${promptT}">
          ${promptPct > 10 ? formatTokens(promptT) : ""}
        </div>
        <div class="token-split__seg token-split__seg--out" style="width:${completionPct.toFixed(1)}%;" title="completion: ${completionT}">
          ${completionPct > 10 ? formatTokens(completionT) : ""}
        </div>
      </div>
      <div class="token-split__legend">
        <div class="token-split__legend-item">
          <span class="token-split__legend-sw token-split__legend-sw--in"></span>
          <span>PROMPT</span>
          <span class="token-split__legend-val">${formatTokens(promptT)}</span>
        </div>
        <div class="token-split__legend-item">
          <span class="token-split__legend-sw token-split__legend-sw--out"></span>
          <span>COMPLETION</span>
          <span class="token-split__legend-val">${formatTokens(completionT)}</span>
        </div>
      </div>
    </div>
  `;
}

function renderErrorsByKind(data) {
  const el = document.getElementById("chartErrorsByKind");
  if (!el) return;
  const rows = data.errors_by_kind || [];
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no errors — all green</div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const kindClass = {
    rate_limited: "svg-bar",                // amber (default)
    server_error: "svg-bar svg-bar--error", // rose
    auth: "svg-bar svg-bar--error",
    network: "svg-bar svg-bar--error",
    client_error: "svg-bar svg-bar--cyan",
    parsing: "svg-bar svg-bar--cyan",
    unknown: "svg-bar svg-bar--cyan",
  };
  const labelW = 95, valW = 50, gap = 6;
  const barZone = 130;
  const w = labelW + barZone + gap + valW;
  const rowH = 26;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    const cls = kindClass[r.kind] || "svg-bar svg-bar--error";
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(r.kind)}</text>
      <rect class="${cls}" x="${labelW}" y="${y + 3}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls}</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function renderByModel(data) {
  const el = document.getElementById("chartByModel");
  if (!el) return;
  const rows = data.by_model || [];
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no model data</div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const labelW = 135, valW = 70, gap = 6;
  const barZone = 120;
  const w = labelW + barZone + gap + valW;
  const rowH = 26;
  const h = rows.length * rowH + 12;
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    const successRate = r.calls ? Math.round((r.success / r.calls) * 100) : 0;
    return `
      <text class="svg-label" x="0" y="${y + 13}"><title>${escapeSvg(r.model)}</title>${escapeSvg(trunc(r.model, 18))}</text>
      <rect class="svg-bar" x="${labelW}" y="${y + 3}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls} · ${successRate}%</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function renderFallbackChain(data) {
  const el = document.getElementById("chartFallbackChain");
  if (!el) return;
  const rows = data.fallback_hist || [];
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no data</div>`;
    return;
  }
  const labels = { 1: "1st (primary)", 2: "2nd (fallback)", 3: "3rd+ (deep)" };
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const total = rows.reduce((s, r) => s + r.calls, 0);
  const labelW = 110, valW = 80, gap = 6;
  const barZone = 130;
  const w = labelW + barZone + gap + valW;
  const rowH = 28;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    const pct = total ? Math.round((r.calls / total) * 100) : 0;
    const cls = r.position === 1 ? "svg-bar svg-bar--success" : "svg-bar";
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(labels[r.position] || r.position)}</text>
      <rect class="${cls}" x="${labelW}" y="${y + 4}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls} · ${pct}%</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function renderHourlyPattern(data) {
  const el = document.getElementById("chartHourlyPattern");
  if (!el) return;
  const rows = data.hourly_pattern || [];
  if (!rows.length || rows.every((r) => r.calls === 0)) {
    el.innerHTML = `<div class="chart-card__empty">no traffic in the last 7 days</div>`;
    return;
  }
  const w = 800, h = 180, pad = 32;
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const barZone = w - 2 * pad;
  const barW = barZone / 24;

  const ticks = 4;
  const gridLines = [];
  for (let i = 0; i <= ticks; i++) {
    const y = pad + (i * (h - 2 * pad)) / ticks;
    const label = Math.round(max * (1 - i / ticks));
    gridLines.push(
      `<line class="svg-grid" x1="${pad}" x2="${w - pad}" y1="${y}" y2="${y}" />`,
      `<text class="svg-label" x="${pad - 6}" y="${y + 3}" text-anchor="end">${label}</text>`,
    );
  }

  const bars = rows.map((r) => {
    const bH = (r.calls / max) * (h - 2 * pad);
    const x = pad + r.hour * barW;
    const y = h - pad - bH;
    return `<rect class="svg-bar" x="${(x + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${(barW - 2).toFixed(1)}" height="${bH.toFixed(1)}"><title>${r.hour.toString().padStart(2, "0")}:00 — ${r.calls} calls</title></rect>`;
  }).join("");

  const xLabels = [0, 6, 12, 18, 23].map((hr) => {
    const x = pad + hr * barW + barW / 2;
    return `<text class="svg-label" x="${x.toFixed(1)}" y="${h - 10}" text-anchor="middle">${String(hr).padStart(2, "0")}:00</text>`;
  });

  el.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
      ${gridLines.join("")}
      ${bars}
      ${xLabels.join("")}
      <line class="svg-axis" x1="${pad}" x2="${w - pad}" y1="${h - pad}" y2="${h - pad}" />
    </svg>
  `;
}

// ─────────────── HISTORICAL renderers ───────────────

function renderHistoricalKpis(data) {
  const el = document.getElementById("historicalKpis");
  if (!el) return;
  const pct = data.total_calls ? Math.round(data.success_rate * 1000) / 10 : 0;
  const avgPerDay = data.days ? Math.round(data.total_calls / data.days) : 0;
  el.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-card__label">TOTAL&nbsp;CALLS</div>
      <div class="kpi-card__value">${data.total_calls.toLocaleString()}</div>
      <div class="kpi-card__sub">over <b>${data.days}</b> days</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">SUCCESS&nbsp;RATE</div>
      <div class="kpi-card__value">${pct}<span class="kpi-card__unit">%</span></div>
      <div class="kpi-card__sub"><b>${data.success_calls.toLocaleString()}</b> ok · <b>${data.failed_calls.toLocaleString()}</b> failed</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">AVG&nbsp;CALLS&nbsp;/&nbsp;DAY</div>
      <div class="kpi-card__value">${avgPerDay.toLocaleString()}</div>
      <div class="kpi-card__sub">mean daily traffic</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-card__label">TOKENS</div>
      <div class="kpi-card__value">${formatTokens(data.total_tokens)}</div>
      <div class="kpi-card__sub">prompt + completion</div>
    </div>
  `;
}

function renderHistoricalDaily(data) {
  const el = document.getElementById("chartHistoricalDaily");
  if (!el) return;
  const rows = data.daily || [];
  if (!rows.length || rows.every((r) => r.calls === 0)) {
    el.innerHTML = `<div class="chart-card__empty">no rollup data yet — daily aggregates are written hourly by the background job</div>`;
    return;
  }
  const w = 800, h = 220, pad = 34;
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const stepX = (w - 2 * pad) / Math.max(rows.length - 1, 1);

  const pointsCalls = rows.map((r, i) => [
    pad + i * stepX,
    h - pad - (r.calls / max) * (h - 2 * pad),
  ]);
  const pointsSuccess = rows.map((r, i) => [
    pad + i * stepX,
    h - pad - (r.success / max) * (h - 2 * pad),
  ]);
  const pathCalls = pointsCalls.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const pathSuccess = pointsSuccess.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const area = `M${pad},${h - pad} ${pathCalls.slice(1)} L${w - pad},${h - pad} Z`;

  const ticks = 4;
  const gridLines = [];
  for (let i = 0; i <= ticks; i++) {
    const y = pad + (i * (h - 2 * pad)) / ticks;
    const label = Math.round(max * (1 - i / ticks));
    gridLines.push(
      `<line class="svg-grid" x1="${pad}" x2="${w - pad}" y1="${y}" y2="${y}" />`,
      `<text class="svg-label" x="${pad - 6}" y="${y + 3}" text-anchor="end">${label}</text>`,
    );
  }

  const xIdxs = rows.length <= 4
    ? rows.map((_, i) => i)
    : [0, Math.floor(rows.length / 3), Math.floor((2 * rows.length) / 3), rows.length - 1];
  const xLabels = xIdxs.map((i) => {
    const d = rows[i].day.slice(5); // "MM-DD"
    return `<text class="svg-label" x="${(pad + i * stepX).toFixed(1)}" y="${h - 10}" text-anchor="middle">${d}</text>`;
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

function renderHistoricalByProvider(data) {
  const el = document.getElementById("chartHistoricalByProvider");
  if (!el) return;
  const rows = data.by_provider || [];
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no data</div>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const labelW = 85, valW = 90, gap = 6;
  const barZone = 130;
  const w = labelW + barZone + gap + valW;
  const rowH = 28;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    return `
      <text class="svg-label" x="0" y="${y + 13}">${escapeSvg(r.provider)}</text>
      <rect class="svg-bar" x="${labelW}" y="${y + 4}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls} · ${formatTokens(r.tokens)}t</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function renderHistoricalByModel(data) {
  const el = document.getElementById("chartHistoricalByModel");
  if (!el) return;
  const rows = data.by_model || [];
  if (!rows.length) {
    el.innerHTML = `<div class="chart-card__empty">no data</div>`;
    return;
  }
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const max = Math.max(...rows.map((r) => r.calls), 1);
  const labelW = 135, valW = 90, gap = 6;
  const barZone = 110;
  const w = labelW + barZone + gap + valW;
  const rowH = 28;
  const h = rows.length * rowH + 12;
  const bars = rows.map((r, i) => {
    const y = i * rowH + 8;
    const barW = Math.max(2, (r.calls / max) * barZone);
    return `
      <text class="svg-label" x="0" y="${y + 13}"><title>${escapeSvg(r.model)}</title>${escapeSvg(trunc(r.model, 18))}</text>
      <rect class="svg-bar svg-bar--cyan" x="${labelW}" y="${y + 4}" width="${barW.toFixed(1)}" height="14" />
      <text class="svg-label" x="${labelW + barZone + gap}" y="${y + 13}">${r.calls} · ${formatTokens(r.tokens)}t</text>
    `;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}
