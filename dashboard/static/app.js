/* ============================================================
   SPG Sales Report – Frontend (matches SPG PDF layout)
   ============================================================ */

const state = {
  currentPage: "today",
  health: "pending",
  charts: [],
  widgets: [],
  filterOptions: {},
  hiddenSectionsByPage: {},
};

let CHATBOT_WIDGET_URL = "/chatbot-ui/";
let DASHBOARD_TIMEZONE = "Asia/Riyadh";

const PAGE_TITLES = {
  today:  "Today Sales Report",
  mtd:    "Month-To-Date Sales Report",
  qtd:    "Quarter-To-Date Sales Report",
  trends: "Daily Sales Trends",
  orders: "Order Wise Report",
  chatbot: "Chatbot Dashboard",
};

const DASHBOARD_WIDGET_PAGES = new Set(["today", "mtd", "qtd", "trends", "orders", "chatbot"]);

/* ---------- Helpers ---------- */

function fmtMoney(v) {
  const n = Number(v || 0);
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function fmtNum(v) {
  const n = Number(v || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString("en-US");
}

function fmtPct(v) {
  const n = Number(v || 0);
  const sign = n > 0 ? "+" : "";
  return sign + n.toFixed(1) + "%";
}

function trendClass(v) {
  const n = Number(v || 0);
  if (n > 0) return "trend-up";
  if (n < 0) return "trend-down";
  return "trend-neutral";
}

function trendArrow(v) {
  const n = Number(v || 0);
  if (n > 0) return "&#9650;";
  if (n < 0) return "&#9660;";
  return "&#8213;";
}

function escHtml(v) {
  const d = document.createElement("div");
  d.textContent = String(v ?? "");
  return d.innerHTML;
}

function monthKeyToLabel(key) {
  const [y, m] = String(key || "").split("-");
  const idx = Number(m) - 1;
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  if (idx >= 0 && idx < months.length && y) return `${months[idx]} ${String(y).slice(2)}`;
  return String(key || "");
}

const CHART_COLORS = [
  "#2563eb","#16a34a","#ea580c","#8b5cf6","#db2777",
  "#0891b2","#ca8a04","#dc2626","#4f46e5","#059669",
  "#d97706","#7c3aed","#be185d","#0d9488","#b91c1c",
];

/* ---------- API ---------- */

async function api(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

async function loadUiConfig() {
  try {
    const cfg = await api("/ui-config");
    if (cfg && typeof cfg.chatbot_widget_url === "string" && cfg.chatbot_widget_url.trim()) {
      CHATBOT_WIDGET_URL = cfg.chatbot_widget_url.trim();
    }
    if (cfg && typeof cfg.dashboard_timezone === "string" && cfg.dashboard_timezone.trim()) {
      DASHBOARD_TIMEZONE = cfg.dashboard_timezone.trim();
    }
  } catch (err) {
    console.warn("UI config load failed, using default widget URL:", CHATBOT_WIDGET_URL, err);
  }
}

async function apiDelete(url) {
  const r = await fetch(url, { method: "DELETE" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

/* ---------- Chart management ---------- */

function destroyCharts() {
  state.charts.forEach(c => { try { c.destroy(); } catch(e){} });
  state.charts = [];
}

function makeChart(canvas, cfg) {
  const c = new Chart(canvas, cfg);
  state.charts.push(c);
  return c;
}

/* ---------- Shared chart builders ---------- */

function buildBarChart(canvas, labels, datasets, horizontal) {
  if (!canvas) return;
  const indexAxis = horizontal ? "y" : "x";
  makeChart(canvas, {
    type: "bar",
    data: { labels, datasets },
    options: {
      indexAxis,
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: datasets.length > 1, position: "top", labels: { font: { size: 11 } } } },
      scales: {
        x: { grid: { display: !horizontal }, ticks: { font: { size: 10 }, maxRotation: 45 } },
        y: { grid: { display: horizontal }, ticks: { font: { size: 10 } } },
      },
    },
  });
}

function buildLineChart(canvas, labels, datasets) {
  if (!canvas) return;
  makeChart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: datasets.length > 1, position: "top", labels: { font: { size: 11 } } } },
      scales: {
        x: { ticks: { font: { size: 10 }, maxRotation: 45, autoSkipPadding: 12 } },
        y: { ticks: { font: { size: 10 } } },
      },
    },
  });
}

function buildDoughnutChart(canvas, labels, values) {
  if (!canvas) return;
  const colors = labels.map((_, i) => CHART_COLORS[i % CHART_COLORS.length]);
  makeChart(canvas, {
    type: "doughnut",
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: colors, borderWidth: 1 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "right", labels: { font: { size: 10 }, boxWidth: 12, padding: 8 } },
      },
    },
  });
}

function buildGaugeChart(canvas, actual, orderValue) {
  if (!canvas) return;
  const max = Math.max(actual, orderValue, 1);
  makeChart(canvas, {
    type: "doughnut",
    data: {
      labels: ["Actual Sales", "Remaining"],
      datasets: [{
        data: [actual, Math.max(0, max - actual)],
        backgroundColor: ["#16a34a", "#e5e7eb"],
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      circumference: 180,
      rotation: 270,
      cutout: "70%",
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              return ctx.label + ": " + fmtMoney(ctx.parsed);
            }
          }
        },
      },
    },
  });
}

/* ---------- KPI card builder ---------- */

function kpiCard(label, value, opts = {}) {
  const { color, sub, trend } = opts;
  let subHtml = "";
  if (trend !== undefined) {
    subHtml = `<div class="kpi-sub"><span class="${trendClass(trend)}">${trendArrow(trend)} ${fmtPct(trend)}</span>${sub ? " " + sub : ""}</div>`;
  } else if (sub) {
    subHtml = `<div class="kpi-sub">${sub}</div>`;
  }
  return `<div class="kpi-card ${color || "accent"}">`
    + `<div class="kpi-label">${label}</div>`
    + `<div class="kpi-value">${value}</div>`
    + subHtml
    + `</div>`;
}

/* ---------- Breakdown table builder ---------- */

function breakdownTable(title, items, columns) {
  if (!items || !items.length) return "";
  const cols = columns || [
    { key: "value", label: "Name" },
    { key: "total_sales", label: "Sales", fmt: fmtMoney },
    { key: "order_count", label: "Orders", fmt: fmtNum },
  ];
  const ths = cols.map(c => `<th>${c.label}</th>`).join("");
  const rows = items.map(it => {
    const tds = cols.map(c => {
      const raw = it[c.key];
      const val = c.fmt ? c.fmt(raw) : escHtml(raw);
      const cls = c.cls ? ` class="${typeof c.cls === "function" ? c.cls(raw) : c.cls}"` : "";
      return `<td${cls}>${val}</td>`;
    }).join("");
    return `<tr>${tds}</tr>`;
  }).join("");
  return `<div class="data-table-card">`
    + `<div class="chart-card-title">${title}</div>`
    + `<div class="table-scroll"><table>`
    + `<thead><tr>${ths}</tr></thead>`
    + `<tbody>${rows}</tbody></table></div></div>`;
}

/* ---------- Billing table builder ---------- */

function billingTable(title, items) {
  if (!items || !items.length) return "";
  const cols = [
    { key: "SalesOrganization", label: "Sales Org" },
    { key: "BillingDocument", label: "Billing Doc" },
    { key: "BillingDocumentType", label: "Type" },
    { key: "SoldToParty", label: "Customer" },
    { key: "TotalNetAmount", label: "Net Value", fmt: fmtMoney },
  ];
  return breakdownTable(title, items, cols);
}

/* ---------- Blocked/Unblocked table ---------- */

function blockedOrdersTable(title, items) {
  if (!items || !items.length) return "";
  const cols = [
    { key: "sales_org", label: "Sales Org" },
    { key: "sales_order", label: "Sales Order No." },
    { key: "status", label: "Status", cls: v => v === "Blocked" ? "status-blocked" : "status-unblocked" },
    { key: "order_value", label: "Order Value", fmt: fmtMoney },
  ];
  return breakdownTable(title, items, cols);
}

/* ---------- Debounce helper ---------- */

let _filterDebounceTimer = null;
function debouncedNavigate() {
  clearTimeout(_filterDebounceTimer);
  _filterDebounceTimer = setTimeout(() => navigateTo(state.currentPage), 300);
}

/* ---------- Filter bar ---------- */

async function loadFilters() {
  try {
    const data = await api("/sales/filters");
    state.filterOptions = data.filters || {};
    populateFilterDropdowns();
  } catch (err) {
    console.error("Failed to load filters:", err);
  }
}

function populateFilterDropdowns() {
  // Maps dimension key → element ID for all dropdowns
  const dimMap = {
    "sales_organization":          "filterSalesOrg",
    "sales_office":                "filterSalesOffice",
    "sales_group":                 "filterSalesGroup",
    "distribution_channel":        "filterDistChannel",
    "sales_district":              "filterSalesDistrict",
    "sales_order_type":            "filterOrderType",
    "customer_group":              "filterCustomerGroup",
    "sold_to_party":               "filterCustomer",
    "purchase_order_by_customer":  "filterPurchaseOrder",
  };

  for (const [dim, elId] of Object.entries(dimMap)) {
    const sel = document.getElementById(elId);
    if (!sel) continue;
    const current = sel.value;
    sel.innerHTML = `<option value="">All</option>`;
    const opts = state.filterOptions[dim] || [];
    for (const opt of opts.slice(0, 100)) {
      const o = document.createElement("option");
      o.value = opt.value;
      o.textContent = opt.value;
      sel.appendChild(o);
    }
    if (current) sel.value = current;
  }

  updateClearBtn();
}

function getActiveFilters() {
  const filters = {};
  document.querySelectorAll(".filter-select").forEach(sel => {
    if (sel.value) {
      filters[sel.dataset.dim] = sel.value;
    }
  });
  return filters;
}

function hasActiveFilters() {
  if (Object.keys(getActiveFilters()).length > 0) return true;
  const dateFrom = document.getElementById("filterDateFrom");
  const dateTo = document.getElementById("filterDateTo");
  if ((dateFrom && dateFrom.value) || (dateTo && dateTo.value)) return true;
  return false;
}

function updateClearBtn() {
  const btn = document.getElementById("clearFiltersBtn");
  if (btn) btn.style.display = hasActiveFilters() ? "inline-block" : "none";
}

function clearAllFilters() {
  document.querySelectorAll(".filter-select").forEach(sel => { sel.value = ""; });
  const dateFrom = document.getElementById("filterDateFrom");
  const dateTo = document.getElementById("filterDateTo");
  if (dateFrom) dateFrom.value = "";
  if (dateTo) dateTo.value = "";
  updateClearBtn();
  navigateTo(state.currentPage);
}

function buildFilterQS() {
  const filters = getActiveFilters();
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    params.set(k, v);
  }
  const dateFrom = document.getElementById("filterDateFrom");
  const dateTo = document.getElementById("filterDateTo");
  if (dateFrom && dateFrom.value) params.set("date_from", dateFrom.value);
  if (dateTo && dateTo.value) params.set("date_to", dateTo.value);
  const qs = params.toString();
  return qs ? "?" + qs : "";
}

/* ---------- Section visibility (per tab) ---------- */

function getHiddenSectionSet(page) {
  const arr = state.hiddenSectionsByPage[page] || [];
  return new Set(arr);
}

function persistHiddenSections() {
  try {
    localStorage.setItem("dashboard_hidden_sections_v1", JSON.stringify(state.hiddenSectionsByPage));
  } catch (err) {
    console.warn("Failed to persist section visibility:", err);
  }
}

function loadHiddenSections() {
  try {
    const raw = localStorage.getItem("dashboard_hidden_sections_v1");
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      state.hiddenSectionsByPage = parsed;
    }
  } catch (err) {
    console.warn("Failed to read section visibility:", err);
  }
}

function collectSectionTargets() {
  const wrap = document.getElementById("pageContent");
  if (!wrap) return [];

  const targets = [];
  const directChildren = Array.from(wrap.children || []);
  let idx = 0;

  directChildren.forEach((el) => {
    if (el.classList && el.classList.contains("kpi-row")) {
      idx += 1;
      const id = `${state.currentPage}-kpi-${idx}`;
      el.dataset.sectionId = id;
      el.dataset.sectionLabel = `KPI Row ${idx}`;
      targets.push(el);
    }
  });

  wrap.querySelectorAll(".chart-card, .data-table-card").forEach((el) => {
    idx += 1;
    const titleEl = el.querySelector(".chart-card-title");
    const label = (titleEl ? titleEl.textContent : `Section ${idx}`) || `Section ${idx}`;
    const id = `${state.currentPage}-sec-${idx}`;
    el.dataset.sectionId = id;
    el.dataset.sectionLabel = label.trim();
    targets.push(el);
  });

  return targets;
}

function applySectionVisibility() {
  const hidden = getHiddenSectionSet(state.currentPage);
  collectSectionTargets().forEach((el) => {
    const id = el.dataset.sectionId;
    el.style.display = hidden.has(id) ? "none" : "";
  });
}

function buildViewOptionsMenu() {
  const menu = document.getElementById("viewOptionsMenu");
  if (!menu) return;
  const targets = collectSectionTargets();
  if (!targets.length) {
    menu.innerHTML = `<div class="view-option-item">No sections available</div>`;
    return;
  }

  const hidden = getHiddenSectionSet(state.currentPage);
  menu.innerHTML = targets.map((el) => {
    const id = el.dataset.sectionId;
    const label = el.dataset.sectionLabel || id;
    const checked = hidden.has(id) ? "" : "checked";
    return `<label class="view-option-item"><input type="checkbox" data-section-id="${escHtml(id)}" ${checked}> <span>${escHtml(label)}</span></label>`;
  }).join("");

  menu.querySelectorAll("input[type='checkbox']").forEach((input) => {
    input.addEventListener("change", () => {
      const page = state.currentPage;
      const set = getHiddenSectionSet(page);
      const id = input.dataset.sectionId;
      if (!input.checked) set.add(id);
      else set.delete(id);
      state.hiddenSectionsByPage[page] = Array.from(set);
      persistHiddenSections();
      applySectionVisibility();
    });
  });
}

/* ---------- Widget management ---------- */

async function loadDashboardWidgets() {
  const data = await api("/dashboard/widgets");
  state.widgets = Array.isArray(data.items) ? data.items : [];
}

function getWidgetTargetPage(widget) {
  const raw = String(widget?.payload?._dashboard_page || "").trim().toLowerCase();
  return DASHBOARD_WIDGET_PAGES.has(raw) ? raw : "chatbot";
}

function getWidgetsForCurrentPage() {
  if (state.currentPage === "chatbot") {
    return state.widgets;
  }
  return state.widgets.filter(w => getWidgetTargetPage(w) === state.currentPage);
}

function buildWidgetTable(title, payload, id) {
  const columns = Array.isArray(payload.columns) ? payload.columns : [];
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  if (!columns.length || !rows.length) {
    return `<div class="chart-card"><div class="chart-card-title">${escHtml(title)}</div><div class="empty-state">No table data</div></div>`;
  }
  const header = columns.map(c => `<th>${escHtml(c)}</th>`).join("");
  const body = rows.slice(0, 15).map(row => {
    const tds = columns.map(c => `<td>${escHtml(row[c] ?? "")}</td>`).join("");
    return `<tr>${tds}</tr>`;
  }).join("");
  return `<div class="data-table-card chatbot-widget-card" data-widget-id="${id}"><div class="widget-head"><div class="chart-card-title">${escHtml(title)}</div><button class="widget-remove-btn" data-widget-id="${id}">Remove</button></div><div class="table-scroll"><table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table></div></div>`;
}

function renderWidgetCharts(widgetChartJobs) {
  widgetChartJobs.forEach(({ canvasId, payload }) => {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const labels = Array.isArray(payload.labels) ? payload.labels : [];
    const values = Array.isArray(payload.values) ? payload.values : [];
    const datasetLabel = payload.dataset_label || "Value";
    const type = String(payload.type || "bar").toLowerCase();
    if (!labels.length || !values.length) return;
    makeChart(canvas, {
      type: ["bar", "line", "pie", "scatter", "doughnut"].includes(type) ? type : "bar",
      data: {
        labels,
        datasets: [{
          label: datasetLabel,
          data: values,
          backgroundColor: type === "pie" || type === "doughnut"
            ? labels.map((_, i) => CHART_COLORS[i % CHART_COLORS.length])
            : CHART_COLORS[0],
          borderColor: CHART_COLORS[0],
          borderWidth: type === "line" ? 2 : 0,
          fill: type === "line",
          tension: 0.3,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: type === "pie" || type === "doughnut", position: "bottom" } },
      },
    });
  });
}

async function removeDashboardWidget(widgetId) {
  try {
    await apiDelete(`/dashboard/widgets/${widgetId}`);
    await loadDashboardWidgets();
    await renderChatbotWidgets();
  } catch (err) {
    console.error("Widget remove failed:", err);
  }
}

async function renderChatbotWidgets() {
  const wrap = document.getElementById("pageContent");
  if (!wrap) return;
  const existing = document.getElementById("chatbotWidgetSection");
  if (existing) existing.remove();
  const widgets = getWidgetsForCurrentPage();
  if (!widgets.length) return;

  const section = document.createElement("section");
  section.id = "chatbotWidgetSection";
  section.className = "chatbot-widget-section";
  section.innerHTML = `<h3 class="chatbot-widget-title">${state.currentPage === "chatbot" ? "Chatbot Widgets" : "Added From Chatbot"}</h3>`;
  const grid = document.createElement("div");
  grid.className = "chart-grid";

  const widgetChartJobs = [];
  for (const widget of widgets) {
    if (widget.widget_type === "chart") {
      const canvasId = `widget-canvas-${String(widget.id).replace(/[^a-zA-Z0-9_-]/g, "")}`;
      const card = document.createElement("div");
      card.className = "chart-card chatbot-widget-card";
      card.setAttribute("data-widget-id", widget.id);
      card.innerHTML = `<div class="widget-head"><div class="chart-card-title">${escHtml(widget.title || "Chatbot Chart")}</div><button class="widget-remove-btn" data-widget-id="${widget.id}">Remove</button></div><div class="chart-container h-250"><canvas id="${canvasId}"></canvas></div>`;
      grid.appendChild(card);
      widgetChartJobs.push({ canvasId, payload: widget.payload || {} });
    } else if (widget.widget_type === "table") {
      const tableCard = document.createElement("div");
      tableCard.innerHTML = buildWidgetTable(widget.title || "Chatbot Table", widget.payload || {}, widget.id);
      grid.appendChild(tableCard.firstElementChild);
    }
  }
  section.appendChild(grid);
  wrap.appendChild(section);
  renderWidgetCharts(widgetChartJobs);
  section.querySelectorAll(".widget-remove-btn").forEach(btn => {
    btn.addEventListener("click", () => removeDashboardWidget(btn.dataset.widgetId));
  });
}

async function renderChatbotPage() {
  const wrap = document.getElementById("pageContent");
  wrap.innerHTML = `
    <div class="chart-card">
      <div class="chart-card-title">Chatbot Widget Board</div>
      <p class="empty-state">Use "Add to Dashboard" in chatbot and choose a target page. All widgets are visible here.</p>
    </div>
  `;
}

/* ============================================================
   PAGE RENDERERS — matching SPG PDF layout
   ============================================================ */

async function renderToday() {
  const d = await api("/sales/today" + buildFilterQS());
  const wrap = document.getElementById("pageContent");

  let html = ``;
  if (d.no_new_data) {
    const asOf = d.latest_sales_date ? escHtml(d.latest_sales_date) : "N/A";
    html += `<div class="chart-card" style="margin-bottom:12px;border-left:4px solid #f59e0b;">
      <div class="chart-card-title">No New Data For Today</div>
      <p style="margin:8px 0 0;color:#6b7280;">Latest available sales data is from <strong>${asOf}</strong>. Today's metrics are shown as 0.</p>
    </div>`;
  }

  // Row 1: KPI cards (matching PDF page 1 top section)
  html += `<div class="kpi-row">`;
  html += kpiCard("Today's Sales Order", fmtNum(d.sales_order_count), { color: "accent" });
  html += kpiCard("Today's Sales Order Value", fmtMoney(d.sales_order_value), { color: "accent" });
  html += kpiCard("% Change vs Last Year", fmtPct(d.pct_change_ly), {
    color: d.pct_change_ly >= 0 ? "green" : "red",
    trend: d.pct_change_ly,
  });
  html += kpiCard("Today's Actual Sales", fmtMoney(d.actual_sales || 0), { color: "green" });
  html += kpiCard("Same Day LY Sales", fmtMoney(d.same_day_ly), { color: "orange" });
  html += `</div>`;

  // Row 2: Blocked/Unblocked KPIs
  html += `<div class="kpi-row">`;
  html += kpiCard("Blocked Sales Order", fmtNum(d.blocked_count || 0), { color: "red" });
  html += kpiCard("Blocked Sales Order Value", fmtMoney(d.blocked_value || 0), { color: "red" });
  html += kpiCard("Unblocked Sales Order", fmtNum(d.unblocked_count || 0), { color: "green" });
  html += kpiCard("Unblocked Sales Order Value", fmtMoney(d.unblocked_value || 0), { color: "green" });
  html += `</div>`;

  // Charts row 1: Last 4 Days + Product Category + Gauge
  html += `<div class="chart-grid cols-3">`;
  html += `<div class="chart-card"><div class="chart-card-title">Last 4 Days Actual Sales</div><div class="chart-container h-250"><canvas id="chartLast4"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Daily Sales by Product Category</div><div class="chart-container h-250"><canvas id="chartByType"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Sale Order Net Value VS Actual Sales (Today)</div><div class="gauge-wrap"><canvas id="chartGauge" class="gauge-canvas"></canvas><div class="gauge-value-label">${fmtMoney(d.actual_sales || 0)}</div><div class="gauge-range"><span>0.00M</span><span>${fmtMoney(d.sales_order_value)}</span></div></div></div>`;
  html += `</div>`;

  // Charts row 2: Blocked table + Sales by Office + Sales vs Order by Org
  html += `<div class="chart-grid cols-3">`;
  // Blocked/Unblocked table in a chart-card
  html += `<div class="chart-card"><div class="chart-card-title">Today's Sales Order Blocked/Unblocked</div><div class="table-scroll" style="max-height:250px;">`;
  if ((d.blocked_orders_list || []).length) {
    html += `<table><thead><tr><th>Sales Org</th><th>Order No.</th><th>Status</th><th>Value</th></tr></thead><tbody>`;
    (d.blocked_orders_list || []).forEach(r => {
      const cls = r.status === "Blocked" ? "status-blocked" : "status-unblocked";
      html += `<tr><td>${escHtml(r.sales_org)}</td><td>${escHtml(r.sales_order)}</td><td class="${cls}">${r.status}</td><td>${fmtMoney(r.order_value)}</td></tr>`;
    });
    html += `</tbody></table>`;
  } else {
    html += `<div style="padding:20px;color:#6b7280;text-align:center;">No orders today</div>`;
  }
  html += `</div></div>`;

  html += `<div class="chart-card"><div class="chart-card-title">Today's Sales by Sales Office</div><div class="chart-container h-250"><canvas id="chartByGroup"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Actual Sales VS Sale Order Value A/C Sales Org</div><div class="chart-container h-250"><canvas id="chartByOrg"></canvas></div></div>`;
  html += `</div>`;

  // Billings table
  html += billingTable("Today Billings", d.today_billings);

  wrap.innerHTML = html;

  // ── Render charts ──

  // Last 4 days
  const l4 = d.last_4_days || {};
  buildBarChart(document.getElementById("chartLast4"),
    Object.keys(l4).map(k => k.slice(5)),
    [{ label: "Sales", data: Object.values(l4), backgroundColor: "#2563eb", borderRadius: 3 }]);

  // By type (product category) — horizontal bar
  const typeItems = d.by_order_type || [];
  buildBarChart(document.getElementById("chartByType"),
    typeItems.map(x => x.value), [{ label: "Sales", data: typeItems.map(x => x.total_sales), backgroundColor: CHART_COLORS.slice(0, typeItems.length) }], true);

  // Gauge
  buildGaugeChart(document.getElementById("chartGauge"), d.actual_sales || 0, d.sales_order_value || 0);

  // By group (sales office) — horizontal bar like treemap
  const grpItems = d.by_sales_group || [];
  buildBarChart(document.getElementById("chartByGroup"),
    grpItems.map(x => x.value), [{ label: "Sales", data: grpItems.map(x => x.total_sales), backgroundColor: CHART_COLORS.slice(0, grpItems.length) }], true);

  // By org — grouped bar (order value vs actual)
  const orgItems = d.by_sales_org || [];
  const billingByOrg = d.billing_by_org || [];
  const orgLabels = orgItems.map(x => x.value);
  const orgSalesData = orgItems.map(x => x.total_sales);
  const orgActualData = orgLabels.map(lbl => {
    const found = billingByOrg.find(b => b.value === lbl);
    return found ? found.total_sales : 0;
  });
  buildBarChart(document.getElementById("chartByOrg"), orgLabels, [
    { label: "Order Value", data: orgSalesData, backgroundColor: "#2563eb", borderRadius: 3 },
    { label: "Actual Sales", data: orgActualData, backgroundColor: "#16a34a", borderRadius: 3 },
  ]);
}


async function renderPeriod(endpoint, periodLabel) {
  const d = await api(endpoint + buildFilterQS());
  const wrap = document.getElementById("pageContent");

  // KPI Row 1
  let html = `<div class="kpi-row">`;
  html += kpiCard(`${periodLabel} Sales Order`, fmtNum(d.order_count), { color: "accent" });
  html += kpiCard(`${periodLabel} Sales Order Value`, fmtMoney(d.sales_total), { color: "accent" });
  html += kpiCard("% Change vs Last Year", fmtPct(d.pct_change_ly), {
    color: d.pct_change_ly >= 0 ? "green" : "red",
    trend: d.pct_change_ly,
  });
  html += kpiCard(`${periodLabel} Actual Sales`, fmtMoney(d.actual_sales || 0), { color: "green" });
  html += kpiCard(`${periodLabel} LY Actual Sales`, fmtMoney(d.actual_sales_ly || 0), { color: "orange" });
  html += `</div>`;

  // KPI Row 2: Blocked/Unblocked
  html += `<div class="kpi-row">`;
  html += kpiCard(`${periodLabel} Sales Order Unblocked`, fmtNum(d.unblocked_count || 0), { color: "green" });
  html += kpiCard("Sales Order Unblocked Value", fmtMoney(d.unblocked_value || 0), { color: "green" });
  html += kpiCard(`${periodLabel} Sales Order Blocked`, fmtNum(d.blocked_count || 0), { color: "red" });
  html += kpiCard("Sales Order Blocked Value", fmtMoney(d.blocked_value || 0), { color: "red" });
  html += `</div>`;

  // Gauge
  html += `<div class="chart-grid cols-3">`;
  html += `<div class="chart-card"><div class="chart-card-title">Sale Order Net Value VS Actual Sales (${periodLabel})</div><div class="gauge-wrap"><canvas id="chartGauge" class="gauge-canvas"></canvas><div class="gauge-value-label">${fmtMoney(d.actual_sales || 0)}</div><div class="gauge-range"><span>0.00M</span><span>${fmtMoney(d.sales_total)}</span></div></div></div>`;

  // Daily trend chart
  html += `<div class="chart-card" style="grid-column: span 2;"><div class="chart-card-title">${periodLabel} Actual Sales</div><div class="chart-container h-300"><canvas id="chartDailyTrend"></canvas></div></div>`;
  html += `</div>`;

  // Row: Product category + blocked table + sales by office
  html += `<div class="chart-grid cols-3">`;
  html += `<div class="chart-card"><div class="chart-card-title">Sales ${periodLabel} Current Month by Product Category</div><div class="chart-container h-250"><canvas id="chartByType"></canvas></div></div>`;

  // Blocked/Unblocked table
  html += `<div class="chart-card"><div class="chart-card-title">${periodLabel} Sales Order Blocked/Unblocked</div><div class="table-scroll" style="max-height:250px;">`;
  if ((d.blocked_orders_list || []).length) {
    html += `<table><thead><tr><th>Sales Org</th><th>Order No.</th><th>Status</th><th>Value</th></tr></thead><tbody>`;
    (d.blocked_orders_list || []).slice(0, 30).forEach(r => {
      const cls = r.status === "Blocked" ? "status-blocked" : "status-unblocked";
      html += `<tr><td>${escHtml(r.sales_org)}</td><td>${escHtml(r.sales_order)}</td><td class="${cls}">${r.status}</td><td>${fmtMoney(r.order_value)}</td></tr>`;
    });
    html += `</tbody></table>`;
  } else {
    html += `<div style="padding:20px;color:#6b7280;text-align:center;">No blocked orders</div>`;
  }
  html += `</div></div>`;

  html += `<div class="chart-card"><div class="chart-card-title">${periodLabel} Sales by Sales Office</div><div class="chart-container h-250"><canvas id="chartByGroup"></canvas></div></div>`;
  html += `</div>`;

  // Row: Sales vs Order by Org + Billings
  html += `<div class="chart-grid">`;
  html += `<div class="chart-card"><div class="chart-card-title">${periodLabel} Actual VS ${periodLabel} LY Sales A/C Sales Org</div><div class="chart-container h-300"><canvas id="chartByOrg"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">${periodLabel} Billings</div><div class="table-scroll" style="max-height:300px;">`;
  const bills = d.billings || [];
  if (bills.length) {
    html += `<table><thead><tr><th>Sales Org</th><th>Billing Doc</th><th>Type</th><th>Customer</th><th>Net Value</th></tr></thead><tbody>`;
    bills.forEach(b => {
      html += `<tr><td>${escHtml(b.SalesOrganization || "")}</td><td>${escHtml(b.BillingDocument || "")}</td><td>${escHtml(b.BillingDocumentType || "")}</td><td>${escHtml(b.SoldToParty || "")}</td><td>${fmtMoney(b.TotalNetAmount)}</td></tr>`;
    });
    html += `</tbody></table>`;
  } else {
    html += `<div style="padding:20px;color:#6b7280;text-align:center;">No billing data</div>`;
  }
  html += `</div></div>`;
  html += `</div>`;

  wrap.innerHTML = html;

  // ── Render charts ──

  buildGaugeChart(document.getElementById("chartGauge"), d.actual_sales || 0, d.sales_total || 0);

  const dt = d.daily_trend || {};
  buildBarChart(document.getElementById("chartDailyTrend"),
    Object.keys(dt).map(k => k.slice(5)),
    [{ label: "Sales", data: Object.values(dt), backgroundColor: "#2563eb", borderRadius: 3 }]);

  const typeItems = d.by_order_type || [];
  buildBarChart(document.getElementById("chartByType"),
    typeItems.map(x => x.value), [{ label: "Sales", data: typeItems.map(x => x.total_sales), backgroundColor: CHART_COLORS.slice(0, typeItems.length) }], true);

  const grpItems = d.by_sales_group || [];
  buildBarChart(document.getElementById("chartByGroup"),
    grpItems.map(x => x.value), [{ label: "Sales", data: grpItems.map(x => x.total_sales), backgroundColor: CHART_COLORS.slice(0, grpItems.length) }], true);

  const orgItems = d.by_sales_org || [];
  buildBarChart(document.getElementById("chartByOrg"),
    orgItems.map(x => x.value), [
      { label: "Current", data: orgItems.map(x => x.total_sales), backgroundColor: "#2563eb", borderRadius: 3 },
    ]);
}


async function renderTrends() {
  const d = await api("/sales/trends" + buildFilterQS());
  const wrap = document.getElementById("pageContent");

  let html = `<div class="kpi-row">`;
  html += kpiCard("Total Sales (1Y)", fmtMoney(d.total_sales), { color: "accent" });
  html += kpiCard("Total Orders", fmtNum(d.total_orders), { color: "accent" });
  html += kpiCard("Total Customers", fmtNum(d.total_customers), { color: "green" });
  html += `</div>`;

  const highDaily = d.highest_daily_sales || {};
  const lowDaily = d.lowest_daily_sales || {};
  const highMonth = d.highest_monthly_sales || {};
  const lowMonth = d.lowest_monthly_sales || {};
  const maxCust = d.max_customer_year || {};
  const minCust = d.min_customer_year || {};
  html += `<div class="kpi-row">`;
  html += kpiCard("Highest Daily Sales (1Y)", fmtMoney(highDaily.total_sales || 0), { color: "green", sub: highDaily.date || "-" });
  html += kpiCard("Lowest Daily Sales (1Y)", fmtMoney(lowDaily.total_sales || 0), { color: "orange", sub: lowDaily.date || "-" });
  html += kpiCard("Highest Month Sales", fmtMoney(highMonth.total_sales || 0), { color: "green", sub: highMonth.period || "-" });
  html += kpiCard("Lowest Month Sales", fmtMoney(lowMonth.total_sales || 0), { color: "orange", sub: lowMonth.period || "-" });
  html += kpiCard("Highest Paying Customer", fmtMoney(maxCust.total_sales || 0), { color: "accent", sub: maxCust.value || "-" });
  html += kpiCard("Lowest Active Customer", fmtMoney(minCust.total_sales || 0), { color: "orange", sub: minCust.value || "-" });
  html += `</div>`;

  html += `<div class="chart-grid">`;
  html += `<div class="chart-card full-width"><div class="chart-card-title">Daily Sales</div><div class="chart-container h-300"><canvas id="chartDailySales"></canvas></div></div>`;
  html += `<div class="chart-card full-width"><div class="chart-card-title">Day-over-Day % Change</div><div class="chart-container h-250"><canvas id="chartDod"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Monthly Totals by Year</div><div class="chart-container h-300"><canvas id="chartMonthly"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Monthly Max vs Min Sales</div><div class="chart-container h-300"><canvas id="chartMonthlyMaxMin"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Top Product Categories</div><div class="chart-container h-300"><canvas id="chartTopCat"></canvas></div></div>`;
  html += `</div>`;

  html += breakdownTable("Top Customers", d.top_customers, [
    { key: "value", label: "Customer" },
    { key: "total_sales", label: "Sales", fmt: fmtMoney },
    { key: "order_count", label: "Orders", fmt: fmtNum },
  ]);

  wrap.innerHTML = html;

  // Daily sales bar
  const ds = d.daily_sales || {};
  const dsKeys = Object.keys(ds);
  const last60 = dsKeys.slice(-60);
  buildBarChart(document.getElementById("chartDailySales"),
    last60.map(k => k.slice(5)),
    [{ label: "Sales", data: last60.map(k => ds[k]), backgroundColor: "#2563eb", borderRadius: 2 }]);

  // DoD %
  const dod = d.daily_dod_pct || {};
  const dodKeys = Object.keys(dod).slice(-60);
  buildLineChart(document.getElementById("chartDod"),
    dodKeys.map(k => k.slice(5)),
    [{ label: "DoD %", data: dodKeys.map(k => dod[k]), borderColor: "#6366f1", backgroundColor: "rgba(99,102,241,0.1)", fill: true, pointRadius: 0, tension: 0.3 }]);

  // Monthly by year
  const mby = d.monthly_by_year || {};
  const years = Object.keys(mby).sort();
  const monthLabels = ["01","02","03","04","05","06","07","08","09","10","11","12"];
  const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const datasets = years.map((yr, i) => ({
    label: yr,
    data: monthLabels.map(m => (mby[yr] || {})[m] || 0),
    backgroundColor: CHART_COLORS[i % CHART_COLORS.length],
    borderRadius: 3,
  }));
  buildBarChart(document.getElementById("chartMonthly"), monthNames, datasets);

  const maxByMonth = [];
  const minByMonth = [];
  const maxYearsByMonth = [];
  const minYearsByMonth = [];
  monthLabels.forEach((m) => {
    const entries = years
      .map((yr) => {
        const yrObj = mby[yr] || {};
        if (!Object.prototype.hasOwnProperty.call(yrObj, m)) return null;
        const v = Number(yrObj[m]);
        return Number.isFinite(v) ? { year: yr, value: v } : null;
      })
      .filter((v) => v !== null);
    if (!entries.length) {
      maxByMonth.push(0);
      minByMonth.push(0);
      maxYearsByMonth.push([]);
      minYearsByMonth.push([]);
      return;
    }
    const values = entries.map((e) => e.value);
    const maxVal = Math.max(...values);
    const minVal = Math.min(...values);
    maxByMonth.push(maxVal);
    minByMonth.push(minVal);
    maxYearsByMonth.push(entries.filter((e) => e.value === maxVal).map((e) => e.year));
    minYearsByMonth.push(entries.filter((e) => e.value === minVal).map((e) => e.year));
  });
  const maxMinCanvas = document.getElementById("chartMonthlyMaxMin");
  if (maxMinCanvas) {
    makeChart(maxMinCanvas, {
      type: "bar",
      data: {
        labels: monthNames,
        datasets: [
          { label: "Monthly Max", data: maxByMonth, backgroundColor: "#16a34a", borderRadius: 3 },
          { label: "Monthly Min", data: minByMonth, backgroundColor: "#dc2626", borderRadius: 3 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, position: "top", labels: { font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const ds = ctx.dataset?.label || "";
                const value = Number(ctx.parsed?.y ?? ctx.parsed ?? 0);
                const years = ds === "Monthly Max"
                  ? (maxYearsByMonth[ctx.dataIndex] || [])
                  : (minYearsByMonth[ctx.dataIndex] || []);
                const yearText = years.length ? ` (${years.join(", ")})` : "";
                return `${ds}: ${fmtMoney(value)}${yearText}`;
              },
            },
          },
        },
        scales: {
          x: { ticks: { font: { size: 10 }, maxRotation: 45 } },
          y: { ticks: { font: { size: 10 } } },
        },
      },
    });
  }

  // Top categories doughnut
  const cats = d.top_product_categories || [];
  buildDoughnutChart(document.getElementById("chartTopCat"),
    cats.map(c => c.value), cats.map(c => c.total_sales));
}


async function renderOrders() {
  const d = await api("/sales/orders" + buildFilterQS());
  const wrap = document.getElementById("pageContent");

  /* ── KPI Cards (5) ──────────────────────────────────────────── */
  let html = `<div class="kpi-row kpi-row-5">`;
  html += kpiCard("Total Orders", fmtNum(d.total_orders), { color: "accent", sub: "Last 12 months" });
  html += kpiCard("Total Sale Order Value", fmtMoney(d.total_value), { color: "accent" });
  html += kpiCard("No. Of Open Orders", fmtNum(d.open_count), { color: "orange" });
  html += kpiCard("No. Of Closed Orders", fmtNum(d.closed_count), { color: "green" });
  html += kpiCard("Average Value Per Order", fmtMoney(d.avg_value), { color: "accent" });
  html += `</div>`;

  const highOrder = d.highest_single_order || {};
  const lowOrder = d.lowest_nonzero_single_order || {};
  const topCustVal = d.top_customer_by_value || {};
  const topCustOrd = d.top_customer_by_orders || {};
  const topMatVal = d.top_material_by_value || {};
  const topMatOrd = d.top_material_by_orders || {};
  html += `<div class="kpi-row">`;
  html += kpiCard("Highest Single Order", fmtMoney(highOrder.total_value || 0), { color: "green", sub: highOrder.sales_order || "-" });
  html += kpiCard("Lowest Non-zero Order", fmtMoney(lowOrder.total_value || 0), { color: "orange", sub: lowOrder.sales_order || "-" });
  html += kpiCard("Top Customer by Sales", fmtMoney(topCustVal.total_sales || 0), { color: "accent", sub: topCustVal.value || "-" });
  html += kpiCard("Top Customer by Orders", fmtNum(topCustOrd.order_count || 0), { color: "accent", sub: topCustOrd.value || "-" });
  html += kpiCard("Top Material by Sales", fmtMoney(topMatVal.total_value || 0), { color: "green", sub: topMatVal.material || "-" });
  html += kpiCard("Top Material by Orders", fmtNum(topMatOrd.order_count || 0), { color: "accent", sub: topMatOrd.material || "-" });
  html += `</div>`;

  /* ── Row 1: Monthly order count + Day-over-Day change ──────── */
  html += `<div class="chart-grid">`;
  html += `<div class="chart-card"><div class="chart-card-title">No. Of Sale Order (Monthly)</div><div class="chart-container h-250"><canvas id="chartMonthlyOrders"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Day-over-Day Count Of Orders Change (%)</div><div class="chart-container h-250"><canvas id="chartDod"></canvas></div></div>`;
  html += `</div>`;

  /* ── Row 2: Avg Orders/Month VS Avg Value/Month + Top Materials ── */
  html += `<div class="chart-grid">`;
  html += `<div class="chart-card"><div class="chart-card-title">Avg No. Of Order Per Month VS Avg Order Value Per Month</div><div class="chart-container h-300"><canvas id="chartAvgMonthly"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">Top Materials Having Most No. Of Sales Order</div><div class="chart-container h-300"><canvas id="chartTopMaterials"></canvas></div></div>`;
  html += `</div>`;

  /* ── Row 3: Status (doughnut) + Type (doughnut) + Daily Count vs Value ── */
  html += `<div class="chart-grid cols-3">`;
  html += `<div class="chart-card"><div class="chart-card-title">No. Of Sales Order By Status</div><div class="chart-container h-250"><canvas id="chartStatus"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">No. Of Sales Order By Order Type</div><div class="chart-container h-250"><canvas id="chartType"></canvas></div></div>`;
  html += `<div class="chart-card"><div class="chart-card-title">No. Of Sale Order VS Total Order Value (Daily)</div><div class="chart-container h-250"><canvas id="chartDailyCountValue"></canvas></div></div>`;
  html += `</div>`;

  /* ── Sales Order Status Table ──────────────────────────────── */
  const statusTable = d.sales_order_status_table || [];
  if (statusTable.length) {
    html += `<div class="data-table-card">`;
    html += `<div class="chart-card-title">Sales Order Status</div>`;
    html += `<div class="table-scroll"><table>`;
    html += `<thead><tr><th>Sales Order</th><th>No. Of Materials</th><th>Order Type</th><th>Customer</th><th>Salesman</th><th>Order Status</th><th>Order Aging (Days)</th><th>Block Status</th></tr></thead><tbody>`;
    statusTable.slice(0, 50).forEach(r => {
      const blockCls = r.block_status === "Blocked" ? "status-blocked" : "status-unblocked";
      const statusCls = r.order_status === "Completed" ? "status-completed" : (r.order_status === "Open" ? "status-open" : "");
      html += `<tr>`;
      html += `<td>${escHtml(r.sales_order)}</td>`;
      html += `<td style="text-align:center;">${r.no_of_material_type !== "" ? escHtml(r.no_of_material_type) : "—"}</td>`;
      html += `<td>${escHtml(r.order_type || "")}</td>`;
      html += `<td>${escHtml(r.customer)}</td>`;
      html += `<td>${escHtml(r.salesman)}</td>`;
      html += `<td class="${statusCls}">${escHtml(r.order_status)}</td>`;
      html += `<td style="text-align:center;">${r.order_aging_days}</td>`;
      html += `<td class="${blockCls}">${escHtml(r.block_status)}</td>`;
      html += `</tr>`;
    });
    html += `</tbody></table></div></div>`;
  }

  /* ── Detailed View of Sales Order A/C To Line Item ─────────── */
  const lineItemAc = d.line_item_ac_table || [];
  if (lineItemAc.length) {
    html += `<div class="data-table-card">`;
    html += `<div class="chart-card-title">Detailed View Of Sales Order A/C To Line Item</div>`;
    html += `<div class="table-scroll"><table>`;
    html += `<thead><tr><th>SalesOrder</th><th>Item</th><th>Material</th><th>Description</th><th>Quantity</th><th>UoM</th><th>Net Amount</th><th>Currency</th></tr></thead><tbody>`;
    lineItemAc.slice(0, 50).forEach(r => {
      html += `<tr>`;
      html += `<td>${escHtml(r.sales_order)}</td>`;
      html += `<td>${escHtml(r.item_no)}</td>`;
      html += `<td>${escHtml(r.material)}</td>`;
      html += `<td>${escHtml(r.description)}</td>`;
      html += `<td>${fmtNum(r.quantity)}</td>`;
      html += `<td>${escHtml(r.uom)}</td>`;
      html += `<td>${fmtMoney(r.net_amount)}</td>`;
      html += `<td>${escHtml(r.currency)}</td>`;
      html += `</tr>`;
    });
    html += `</tbody></table></div></div>`;
  }

  /* ── Detailed View table (header-level) ────────────────────── */
  const detailView = d.detailed_view_table || [];
  if (detailView.length) {
    html += `<div class="data-table-card">`;
    html += `<div class="chart-card-title">Detailed View</div>`;
    html += `<div class="table-scroll"><table>`;
    html += `<thead><tr><th>Sales Org</th><th>Sales Office</th><th>Dist. Channel</th><th>Material</th><th>Product Category</th><th>Region</th><th>Customer Group</th><th>Customer Name</th><th>Salesman Name</th><th>No. Of Sales Order</th><th>Total Value</th></tr></thead><tbody>`;
    detailView.slice(0, 50).forEach(r => {
      html += `<tr>`;
      html += `<td>${escHtml(r.sales_org)}</td>`;
      html += `<td>${escHtml(r.sales_office)}</td>`;
      html += `<td>${escHtml(r.distribution_channel)}</td>`;
      html += `<td>${escHtml(r.material)}</td>`;
      html += `<td>${escHtml(r.product_category)}</td>`;
      html += `<td>${escHtml(r.region)}</td>`;
      html += `<td>${escHtml(r.customer_group)}</td>`;
      html += `<td>${escHtml(r.customer_name)}</td>`;
      html += `<td>${escHtml(r.salesman_name)}</td>`;
      html += `<td>${escHtml(r.no_of_sales_order)}</td>`;
      html += `<td>${fmtMoney(r.total_value)}</td>`;
      html += `</tr>`;
    });
    html += `</tbody></table></div></div>`;
  }

  wrap.innerHTML = html;

  /* ══════════════════════════════════════════════════════════════
     RENDER CHARTS
     ══════════════════════════════════════════════════════════════ */

  // Monthly order count — horizontal bar
  const mo = d.monthly_order_count || {};
  const moKeys = Object.keys(mo);
  buildBarChart(document.getElementById("chartMonthlyOrders"),
    moKeys.map(k => { const [y, m] = k.split("-"); return ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][parseInt(m,10)-1] || m; }),
    [{ label: "Orders", data: moKeys.map(k => mo[k]), backgroundColor: "#2563eb", borderRadius: 4 }],
    true);

  // Day-over-Day %
  const dod = d.daily_dod_pct || {};
  const dodKeys = Object.keys(dod).slice(-30);
  buildLineChart(document.getElementById("chartDod"),
    dodKeys.map(k => k.slice(5)),
    [{
      label: "DoD %",
      data: dodKeys.map(k => dod[k]),
      borderColor: "#16a34a",
      backgroundColor: "rgba(22,163,74,0.08)",
      fill: true,
      pointRadius: 2,
      pointBackgroundColor: "#16a34a",
      tension: 0.3,
      borderWidth: 2,
    }]);

  // Avg Orders/Day per Month VS Avg Sales/Day per Month (dual axis)
  const avgOrd = d.avg_orders_per_day || {};
  const avgSales = d.avg_sales_per_day || {};
  let avgKeys = Object.keys(avgOrd);
  if (!avgKeys.length) {
    const moCount = d.monthly_order_count || {};
    const moValue = d.monthly_order_value || {};
    avgKeys = Object.keys(moCount);
    avgKeys.forEach((k) => {
      const parts = k.split("-");
      const y = Number(parts[0]);
      const m = Number(parts[1]);
      const days = (y && m) ? new Date(y, m, 0).getDate() : 30;
      avgOrd[k] = days > 0 ? Number(moCount[k] || 0) / days : 0;
      avgSales[k] = days > 0 ? Number(moValue[k] || 0) / days : 0;
    });
  }
  const avgCanvas = document.getElementById("chartAvgMonthly");
  if (avgCanvas && avgKeys.length) {
    makeChart(avgCanvas, {
      type: "bar",
      data: {
        labels: avgKeys.map(k => { const [y, m] = k.split("-"); return ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][parseInt(m,10)-1] + " " + y.slice(2); }),
        datasets: [
          {
            label: "Average Orders per Day",
            data: avgKeys.map(k => avgOrd[k]),
            backgroundColor: "#2563eb",
            borderRadius: 4,
            yAxisID: "y",
            order: 2,
          },
          {
            label: "Average Sales per Day",
            data: avgKeys.map(k => avgSales[k]),
            type: "line",
            borderColor: "#ea580c",
            backgroundColor: "rgba(234,88,12,0.08)",
            pointRadius: 2,
            pointBackgroundColor: "#ea580c",
            tension: 0.3,
            borderWidth: 2,
            fill: false,
            yAxisID: "y1",
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "top", labels: { font: { size: 10 }, boxWidth: 12 } } },
        scales: {
          x: { ticks: { font: { size: 9 }, maxRotation: 45 } },
          y: { position: "left", title: { display: true, text: "Orders/Day", font: { size: 10 } }, ticks: { font: { size: 9 } } },
          y1: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Sales/Day", font: { size: 10 } }, ticks: { font: { size: 9 } } },
        },
      },
    });
  }

  // Top Materials — horizontal bar with dual: order count + value
  let topMat = d.top_materials || [];
  if (!topMat.length && Array.isArray(d.line_item_ac_table) && d.line_item_ac_table.length) {
    const materialMap = new Map();
    d.line_item_ac_table.forEach((r) => {
      const mat = String(r.material || "").trim();
      if (!mat) return;
      const key = mat;
      if (!materialMap.has(key)) {
        materialMap.set(key, { material: key, _orders: new Set(), total_value: 0 });
      }
      const obj = materialMap.get(key);
      obj._orders.add(String(r.sales_order || ""));
      obj.total_value += Number(r.net_amount || 0);
    });
    topMat = Array.from(materialMap.values())
      .map((x) => ({ material: x.material, order_count: x._orders.size, total_value: x.total_value }))
      .sort((a, b) => b.order_count - a.order_count)
      .slice(0, 15);
  }
  const matCanvas = document.getElementById("chartTopMaterials");
  if (matCanvas && topMat.length) {
    makeChart(matCanvas, {
      type: "bar",
      data: {
        labels: topMat.map(m => m.material.length > 14 ? m.material.slice(0, 14) + "..." : m.material),
        datasets: [
          {
            label: "Total Order Count",
            data: topMat.map(m => m.order_count),
            backgroundColor: "#2563eb",
            borderRadius: 3,
            yAxisID: "y",
          },
          {
            label: "Net Value",
            data: topMat.map(m => m.total_value),
            backgroundColor: "#16a34a",
            borderRadius: 3,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "top", labels: { font: { size: 10 }, boxWidth: 10 } } },
        scales: {
          x: { position: "top", ticks: { font: { size: 9 } } },
          y: { ticks: { font: { size: 9 } } },
          y1: { display: false },
        },
      },
    });
  }

  // Status doughnut
  const st = d.by_status || [];
  if (st.length) {
    const statusCanvas = document.getElementById("chartStatus");
    if (statusCanvas) {
      const statusColors = st.map(s => {
        const v = s.value.toLowerCase();
        if (v.includes("completed") || v.includes("closed")) return "#16a34a";
        if (v.includes("open")) return "#ea580c";
        if (v.includes("partial")) return "#ca8a04";
        return "#6b7280";
      });
      makeChart(statusCanvas, {
        type: "doughnut",
        data: {
          labels: st.map(s => s.value),
          datasets: [{ data: st.map(s => s.count), backgroundColor: statusColors, borderWidth: 1 }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: "right", labels: { font: { size: 10 }, boxWidth: 12, padding: 8 } },
            title: { display: true, text: `ORDER STATUS`, font: { size: 10, weight: "bold" }, color: "#6b7280", padding: { bottom: 4 } },
          },
        },
      });
    }
  }

  // Type doughnut
  const bt = d.by_type || [];
  if (bt.length) {
    const typeCanvas = document.getElementById("chartType");
    if (typeCanvas) {
      const typeColorMap = { "ZCOD": "#2563eb", "ZCRE": "#16a34a", "ZRO": "#ea580c", "ZSO": "#8b5cf6" };
      const typeColors = bt.map((t, i) => typeColorMap[t.value] || CHART_COLORS[i % CHART_COLORS.length]);
      makeChart(typeCanvas, {
        type: "doughnut",
        data: {
          labels: bt.map(t => t.value),
          datasets: [{ data: bt.map(t => t.order_count), backgroundColor: typeColors, borderWidth: 1 }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: "right", labels: { font: { size: 10 }, boxWidth: 12, padding: 8 } },
            title: { display: true, text: `ORDER TYPES`, font: { size: 10, weight: "bold" }, color: "#6b7280", padding: { bottom: 4 } },
          },
        },
      });
    }
  }

  // Daily order count vs value (dual axis: bar for count, line for value)
  const dailyCounts = d.daily_order_counts || {};
  const dailyValues = d.daily_order_values || {};
  const dcKeys = Object.keys(dailyCounts).slice(-20);
  const dcvCanvas = document.getElementById("chartDailyCountValue");
  if (dcvCanvas && dcKeys.length) {
    makeChart(dcvCanvas, {
      type: "bar",
      data: {
        labels: dcKeys.map(k => k.slice(5)),
        datasets: [
          {
            label: "No. Of Sales Order",
            data: dcKeys.map(k => dailyCounts[k]),
            backgroundColor: "#2563eb",
            borderRadius: 3,
            yAxisID: "y",
            order: 2,
          },
          {
            label: "Sum of NetValue",
            data: dcKeys.map(k => dailyValues[k] || 0),
            type: "line",
            borderColor: "#ea580c",
            backgroundColor: "transparent",
            pointRadius: 2,
            pointBackgroundColor: "#ea580c",
            tension: 0.3,
            borderWidth: 2,
            yAxisID: "y1",
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "top", labels: { font: { size: 9 }, boxWidth: 10 } } },
        scales: {
          x: { ticks: { font: { size: 8 }, maxRotation: 45 } },
          y: { position: "left", title: { display: true, text: "Orders", font: { size: 9 } }, ticks: { font: { size: 8 } } },
          y1: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Value", font: { size: 9 } }, ticks: { font: { size: 8 } } },
        },
      },
    });
  }
}

/* ============================================================
   NAVIGATION & BOOTSTRAP
   ============================================================ */

async function navigateTo(page) {
  state.currentPage = page;

  document.querySelectorAll(".nav-item").forEach(el => {
    el.classList.toggle("active", el.dataset.page === page);
  });
  document.getElementById("pageTitle").textContent = PAGE_TITLES[page] || page;

  document.getElementById("pageContent").innerHTML = `<div class="loading-state">Loading ${PAGE_TITLES[page]}...</div>`;

  destroyCharts();

  try {
    switch (page) {
      case "today":  await renderToday(); break;
      case "mtd":    await renderPeriod("/sales/mtd", "MTD"); break;
      case "qtd":    await renderPeriod("/sales/qtd", "QTD"); break;
      case "trends": await renderTrends(); break;
      case "orders": await renderOrders(); break;
      case "chatbot": await renderChatbotPage(); break;
    }
    await loadDashboardWidgets();
    await renderChatbotWidgets();
    applySectionVisibility();
    buildViewOptionsMenu();
  } catch (err) {
    document.getElementById("pageContent").innerHTML =
      `<div class="empty-state">Failed to load: ${err.message}</div>`;
    console.error(err);
  }
}

async function checkHealth() {
  const badge = document.getElementById("healthBadge");
  try {
    const d = await api("/health");
    const status = String(d?.status || "").toLowerCase();
    state.health = (status === "running" || status === "healthy" || status === "ok") ? "ok" : "error";
  } catch {
    state.health = "error";
  }
  badge.className = "health-badge " + state.health;
  badge.textContent = state.health === "ok" ? "API Connected" : "API Unreachable";
}

async function loadLastUpdated() {
  try {
    const d = await api("/sales/last-updated");
    if (d.last_updated) {
      const dt = new Date(d.last_updated);
      document.getElementById("lastUpdated").textContent =
        "Last updated: " + dt.toLocaleString("en-US", { timeZone: DASHBOARD_TIMEZONE });
    }
  } catch {}
}

function attachEvents() {
  document.querySelectorAll(".nav-item").forEach(el => {
    el.addEventListener("click", () => navigateTo(el.dataset.page));
  });

  document.getElementById("refreshBtn").addEventListener("click", async () => {
    await checkHealth();
    await loadLastUpdated();
    await navigateTo(state.currentPage);
  });

  // Filter events — debounced to avoid multiple API calls on rapid changes
  document.querySelectorAll(".filter-select").forEach(sel => {
    sel.addEventListener("change", () => {
      updateClearBtn();
      debouncedNavigate();
    });
  });

  const clearBtn = document.getElementById("clearFiltersBtn");
  if (clearBtn) clearBtn.addEventListener("click", clearAllFilters);

  const viewBtn = document.getElementById("viewOptionsBtn");
  const viewMenu = document.getElementById("viewOptionsMenu");
  if (viewBtn && viewMenu) {
    viewBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const show = viewMenu.style.display === "none";
      viewMenu.style.display = show ? "block" : "none";
      if (show) buildViewOptionsMenu();
    });
    document.addEventListener("click", (e) => {
      if (!viewMenu.contains(e.target) && e.target !== viewBtn) {
        viewMenu.style.display = "none";
      }
    });
  }

  // Date range filter events — debounced
  ["filterDateFrom", "filterDateTo"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", () => { updateClearBtn(); debouncedNavigate(); });
  });

  initChatWidget();
}

function initChatWidget() {
  const chatWidget = document.getElementById("chatWidget");
  const launcher = document.getElementById("chatLauncher");
  const panel = document.getElementById("chatPanel");
  const iframe = document.getElementById("chatIframe");
  const minimizeBtn = document.getElementById("chatMinimizeBtn");
  const closeBtn = document.getElementById("chatCloseBtn");

  if (!chatWidget || !launcher || !panel || !iframe) return;

  let iframeLoaded = false;

  const openChat = () => {
    chatWidget.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    if (!iframeLoaded) {
      iframe.src = CHATBOT_WIDGET_URL;
      iframeLoaded = true;
    }
  };

  const closeChat = () => {
    chatWidget.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
  };

  launcher.addEventListener("click", () => {
    chatWidget.classList.contains("open") ? closeChat() : openChat();
  });

  if (minimizeBtn) minimizeBtn.addEventListener("click", closeChat);
  if (closeBtn) closeBtn.addEventListener("click", closeChat);
}

async function bootstrap() {
  await loadUiConfig();
  loadHiddenSections();
  attachEvents();
  await checkHealth();
  await loadFilters();
  await loadLastUpdated();
  await navigateTo("today");

  // Auto-refresh every 3 minutes
  setInterval(async () => {
    await loadLastUpdated();
    await navigateTo(state.currentPage);
  }, 180000);
}

bootstrap().catch(err => {
  console.error("Bootstrap failed:", err);
  document.getElementById("pageContent").innerHTML =
    `<div class="empty-state">Dashboard failed to load: ${err.message}</div>`;
});
