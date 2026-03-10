/* ═══════════════════════════════════════════════
   MUNTQ – SAP AI Chatbot  |  Application Logic
   ═══════════════════════════════════════════════ */

let API_BASE = window.location.origin;
let DASHBOARD_API_BASE =
  window.DASHBOARD_API_BASE ||
  new URLSearchParams(window.location.search).get("dashboard_base") ||
  window.location.origin;


// ── State ──
let sessionId = "";
let isWaiting = false;
let chartCounter = 0;
let ws = null;           // WebSocket connection
let wsReady = false;     // Is WS connected and ready?
const DASHBOARD_PAGES = ["today", "mtd", "qtd", "trends", "orders", "chatbot"];

// ── DOM refs ──
const messagesEl    = document.getElementById("chat-messages");
const inputEl       = document.getElementById("user-input");
const sendBtn       = document.getElementById("btn-send");
const newSessionBtn = document.getElementById("btn-new-session");
const sessionInfoEl = document.getElementById("session-info");

// ══════════════════════════════════════════════════
// Session
// ══════════════════════════════════════════════════

async function startSession() {
  try {
    const res = await fetch(`${API_BASE}/session/start`, { method: "POST" });
    const data = await res.json();
    sessionId = data.session_id;
    sessionInfoEl.textContent = `Session: ${sessionId.slice(0, 8)}…`;
  } catch (err) {
    sessionInfoEl.textContent = "⚠ Could not start session";
    console.error("Session start failed:", err);
  }
}

function resetChat() {
  // Clear messages except welcome
  messagesEl.innerHTML = "";
  addBotMessage(
    "<p>Hello! I'm <strong>MUNTQ</strong>, your AI assistant.</p>" +
    '<p>Ask me about sales orders, purchase orders, invoices, materials, deliveries, business partners, or journal entries.</p>' +
    '<p class="hint">Try: <em>"Show me top 5 sales orders"</em></p>'
  );
  sessionId = "";
  chartCounter = 0;
  startSession();
}

// ══════════════════════════════════════════════════
// WebSocket Connection
// ══════════════════════════════════════════════════

function getWsUrl() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/chat`;
}

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  ws = new WebSocket(getWsUrl());

  ws.onopen = () => {
    wsReady = true;
    console.log("WebSocket connected");
  };

  ws.onclose = () => {
    wsReady = false;
    console.log("WebSocket disconnected, reconnecting in 2s...");
    setTimeout(connectWebSocket, 2000);
  };

  ws.onerror = (err) => {
    console.error("WebSocket error:", err);
    wsReady = false;
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleWsMessage(msg);
    } catch (e) {
      console.error("WS parse error:", e);
    }
  };
}

// ── WS message state for current streaming response ──
let _wsBubble = null;      // current bot bubble element being streamed into
let _wsTypingEl = null;    // typing indicator element
let _wsStreamedText = "";  // accumulated streamed text

function handleWsMessage(msg) {
  switch (msg.type) {
    case "status":
      // Update typing indicator with status text
      if (_wsTypingEl) {
        const statusEl = _wsTypingEl.querySelector(".typing-status");
        if (statusEl) {
          statusEl.textContent = msg.text;
        }
      }
      break;

    case "token":
      // First token → remove typing indicator, create bot bubble
      if (_wsTypingEl) {
        _wsTypingEl.remove();
        _wsTypingEl = null;
        _wsBubble = addBotMessage("");
        _wsStreamedText = "";
      }
      // Append token to bubble
      _wsStreamedText += msg.text;
      if (_wsBubble) {
        _wsBubble.innerHTML = renderMarkdown(_wsStreamedText);
        scrollToBottom();
      }
      break;

    case "result":
      // Final metadata — chart, export, session update
      if (_wsTypingEl) {
        _wsTypingEl.remove();
        _wsTypingEl = null;
      }

      const data = msg.data || {};

      // Update session ID
      if (data.session_id) {
        sessionId = data.session_id;
        sessionInfoEl.textContent = `Session: ${sessionId.slice(0, 8)}…`;
      }

      // If no bubble was created (no tokens streamed), create one
      if (!_wsBubble && data.needs_input && data.input_prompt) {
        _wsBubble = addBotMessage(renderMarkdown(data.input_prompt));
      }

      // Render chart if present
      if (_wsBubble && data.chart_data && data.chart_data.labels && data.chart_data.values) {
        renderChart(_wsBubble, data.chart_data);
        attachAddToDashboardButton(
          _wsBubble,
          "chart",
          data.chart_data,
          buildWidgetTitleFromReply(_wsStreamedText, "Chart"),
        );
      }

      // Add table widget button from streamed markdown
      if (_wsBubble && _wsStreamedText) {
        const markdownTablePayload = buildTablePayloadFromMarkdown(_wsStreamedText);
        if (markdownTablePayload) {
          attachAddToDashboardButton(
            _wsBubble,
            "table",
            markdownTablePayload,
            buildWidgetTitleFromReply(_wsStreamedText, "Table"),
          );
        }
      }

      // Export button
      if (_wsBubble && data.export_url) {
        const exportContainer = document.createElement("div");
        exportContainer.className = "export-container";
        exportContainer.innerHTML = `
          <a href="${API_BASE}${data.export_url}" class="export-btn" target="_blank" download>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            Export to Excel
          </a>
        `;
        _wsBubble.appendChild(exportContainer);
      }

      // Clean up streaming state
      _wsBubble = null;
      _wsStreamedText = "";
      setWaiting(false);
      inputEl.focus();
      break;

    case "error":
      if (_wsTypingEl) {
        _wsTypingEl.remove();
        _wsTypingEl = null;
      }
      addErrorMessage(msg.text || "An unknown error occurred");
      _wsBubble = null;
      _wsStreamedText = "";
      setWaiting(false);
      inputEl.focus();
      break;
  }
}

// ══════════════════════════════════════════════════
// Send Message (via WebSocket with HTTP fallback)
// ══════════════════════════════════════════════════

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || isWaiting) return;

  addUserMessage(text);
  inputEl.value = "";
  autoResize();
  setWaiting(true);

  // Try WebSocket first, fall back to HTTP
  if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
    _wsTypingEl = addStreamingIndicator();
    _wsBubble = null;
    _wsStreamedText = "";

    ws.send(JSON.stringify({
      tenant_id: "",
      user_id: "",
      session_id: sessionId,
      message: text,
    }));
    // Response will be handled by handleWsMessage callbacks
  } else {
    // HTTP fallback (original logic)
    await sendMessageHttp(text);
  }
}

async function sendMessageHttp(text) {
  const typingEl = addTypingIndicator();

  try {
    const payload = {
      tenant_id: "",
      user_id: "",
      session_id: sessionId,
      message: text,
    };

    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();

    if (data.session_id) {
      sessionId = data.session_id;
      sessionInfoEl.textContent = `Session: ${sessionId.slice(0, 8)}…`;
    }

    typingEl.remove();
    renderBotResponse(data);
  } catch (err) {
    typingEl.remove();
    addErrorMessage(`Failed to get response: ${err.message}`);
    console.error("Chat error:", err);
  } finally {
    setWaiting(false);
    inputEl.focus();
  }
}

// ══════════════════════════════════════════════════
// Render Bot Response
// ══════════════════════════════════════════════════

function renderBotResponse(data) {
  const { reply, needs_input, input_prompt, chart_data, export_url } = data;

  // Build HTML content
  let html = "";

  // If needs_input, show badge
  if (needs_input) {
    html += '<div class="needs-input-badge">⚡ Follow-up required</div>';
  }

  // Render markdown reply
  html += renderMarkdown(reply);

  // Add the message bubble
  const bubbleEl = addBotMessage(html);

  // If chart_data exists, render chart inside the bubble
  if (chart_data && chart_data.labels && chart_data.values) {
    renderChart(bubbleEl, chart_data);
    attachAddToDashboardButton(
      bubbleEl,
      "chart",
      chart_data,
      buildWidgetTitleFromReply(reply, "Chart"),
    );
  }

  // If raw SAP rows exist, allow saving as table widget
  if (data.raw_sap && Array.isArray(data.raw_sap.data) && data.raw_sap.data.length > 0) {
    const tablePayload = buildTablePayload(data.raw_sap.data);
    if (tablePayload) {
      attachAddToDashboardButton(
        bubbleEl,
        "table",
        tablePayload,
        buildWidgetTitleFromReply(reply, "Table"),
      );
    }
  } else {
    const markdownTablePayload = buildTablePayloadFromMarkdown(reply);
    if (markdownTablePayload) {
      attachAddToDashboardButton(
        bubbleEl,
        "table",
        markdownTablePayload,
        buildWidgetTitleFromReply(reply, "Table"),
      );
    }
  }

  // If export_url exists, add Excel download button
  if (export_url) {
    const exportContainer = document.createElement("div");
    exportContainer.className = "export-container";
    exportContainer.innerHTML = `
      <a href="${API_BASE}${export_url}" class="export-btn" target="_blank" download>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        Export to Excel
      </a>
    `;
    bubbleEl.appendChild(exportContainer);
  }
}

function buildWidgetTitleFromReply(reply, fallbackType) {
  const clean = String(reply || "").replace(/[#*_`>\n]/g, " ").trim();
  const firstSentence = clean.split(/[.!?]/)[0].trim();
  if (!firstSentence) return `Chatbot ${fallbackType}`;
  return firstSentence.slice(0, 80);
}

function buildTablePayload(rows) {
  if (!Array.isArray(rows) || !rows.length) return null;

  const limitedRows = rows.slice(0, 25).map((r) => {
    const out = {};
    Object.keys(r || {}).slice(0, 10).forEach((k) => {
      const v = r[k];
      out[k] = (typeof v === "object" && v !== null) ? JSON.stringify(v) : v;
    });
    return out;
  });

  const columns = Array.from(
    new Set(limitedRows.flatMap((r) => Object.keys(r)))
  ).slice(0, 10);

  return {
    columns,
    rows: limitedRows,
  };
}

function buildTablePayloadFromMarkdown(replyText) {
  const text = String(replyText || "");
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return null;

  let start = -1;
  for (let i = 0; i < lines.length - 1; i += 1) {
    const header = lines[i];
    const separator = lines[i + 1];
    if (header.includes("|") && /^\|?[\s:-|]+\|?$/.test(separator)) {
      start = i;
      break;
    }
  }
  if (start === -1) return null;

  const tableLines = [];
  for (let i = start; i < lines.length; i += 1) {
    if (!lines[i].includes("|")) break;
    tableLines.push(lines[i]);
  }
  if (tableLines.length < 3) return null;

  const splitRow = (line) =>
    line
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim());

  const columns = splitRow(tableLines[0]).filter((c) => c);
  if (!columns.length) return null;

  const rows = [];
  for (let i = 2; i < tableLines.length; i += 1) {
    const values = splitRow(tableLines[i]);
    if (!values.length) continue;
    const row = {};
    columns.forEach((col, idx) => {
      row[col] = values[idx] ?? "";
    });
    rows.push(row);
    if (rows.length >= 25) break;
  }

  if (!rows.length) return null;
  return { columns, rows };
}

function attachAddToDashboardButton(containerEl, widgetType, payload, title) {
  const wrap = document.createElement("div");
  wrap.className = "add-dashboard-wrap";

  const btn = document.createElement("button");
  btn.className = "add-dashboard-btn";
  btn.type = "button";
  btn.textContent = "Add to Dashboard";
  btn.addEventListener("click", async () => {
    const targetPage = await chooseTargetPage();
    if (!targetPage) return;

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Adding...";

    try {
      const resp = await fetch(`${DASHBOARD_API_BASE}/dashboard/widgets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          widget_type: widgetType,
          payload: { ...payload, _dashboard_page: targetPage },
          source: "chatbot",
        }),
      });

      if (!resp.ok) {
        const msg = await resp.text();
        throw new Error(msg || `HTTP ${resp.status}`);
      }

      showToast(`Added to ${targetPage.toUpperCase()} dashboard`);
    } catch (err) {
      showToast(`Add failed: ${err.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  });

  wrap.appendChild(btn);
  containerEl.appendChild(wrap);
}

async function chooseTargetPage() {
  return await showTargetPageDropdown();
}

function showTargetPageDropdown() {
  return new Promise((resolve) => {
    const existing = document.getElementById("dashboard-target-modal");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "dashboard-target-modal";
    overlay.style.position = "fixed";
    overlay.style.inset = "0";
    overlay.style.background = "rgba(15, 23, 42, 0.45)";
    overlay.style.display = "flex";
    overlay.style.alignItems = "center";
    overlay.style.justifyContent = "center";
    overlay.style.zIndex = "9999";

    const card = document.createElement("div");
    card.style.width = "min(92vw, 360px)";
    card.style.background = "#ffffff";
    card.style.borderRadius = "12px";
    card.style.boxShadow = "0 20px 50px rgba(2, 6, 23, 0.3)";
    card.style.padding = "16px";
    card.style.fontFamily = "inherit";

    const label = document.createElement("label");
    label.textContent = "Add widget to dashboard page";
    label.style.display = "block";
    label.style.fontWeight = "600";
    label.style.marginBottom = "10px";
    label.setAttribute("for", "dashboard-target-select");

    const select = document.createElement("select");
    select.id = "dashboard-target-select";
    select.style.width = "100%";
    select.style.padding = "10px";
    select.style.border = "1px solid #cbd5e1";
    select.style.borderRadius = "8px";
    select.style.marginBottom = "14px";

    DASHBOARD_PAGES.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p.toUpperCase();
      if (p === "chatbot") opt.selected = true;
      select.appendChild(opt);
    });

    const actions = document.createElement("div");
    actions.style.display = "flex";
    actions.style.justifyContent = "flex-end";
    actions.style.gap = "8px";

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "Cancel";
    cancelBtn.style.padding = "8px 12px";
    cancelBtn.style.border = "1px solid #cbd5e1";
    cancelBtn.style.background = "#fff";
    cancelBtn.style.borderRadius = "8px";
    cancelBtn.style.cursor = "pointer";

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = "Add";
    addBtn.style.padding = "8px 12px";
    addBtn.style.border = "none";
    addBtn.style.background = "#2563eb";
    addBtn.style.color = "#fff";
    addBtn.style.borderRadius = "8px";
    addBtn.style.cursor = "pointer";

    const close = (value) => {
      overlay.remove();
      resolve(value);
    };

    cancelBtn.addEventListener("click", () => close(null));
    addBtn.addEventListener("click", () => {
      const value = String(select.value || "").trim().toLowerCase();
      if (!DASHBOARD_PAGES.includes(value)) {
        showToast("Invalid page selected", true);
        return;
      }
      close(value);
    });
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(null);
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(addBtn);
    card.appendChild(label);
    card.appendChild(select);
    card.appendChild(actions);
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    select.focus();
  });
}

async function loadUiConfig() {
  try {
    const resp = await fetch(`${API_BASE}/ui-config`);
    if (!resp.ok) return;
    const cfg = await resp.json();
    if (cfg && typeof cfg.dashboard_base_url === "string" && cfg.dashboard_base_url.trim()) {
      DASHBOARD_API_BASE = cfg.dashboard_base_url.trim();
    }
  } catch (err) {
    console.warn("UI config load failed; using fallback dashboard URL", err);
  }
}

function showToast(message, isError = false) {
  const id = "dashboard-toast";
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement("div");
    el.id = id;
    el.className = "dashboard-toast";
    document.body.appendChild(el);
  }

  el.textContent = message;
  el.classList.remove("error", "show");
  if (isError) el.classList.add("error");
  requestAnimationFrame(() => el.classList.add("show"));

  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => {
    el.classList.remove("show");
  }, 2200);
}

// ══════════════════════════════════════════════════
// Markdown Rendering
// ══════════════════════════════════════════════════

function renderMarkdown(text) {
  if (!text) return "";

  // Configure marked
  marked.setOptions({
    breaks: true,
    gfm: true,
  });

  let html = marked.parse(text);

  // Wrap tables in a scrollable container
  html = html.replace(/<table>/g, '<div class="table-wrapper"><table>');
  html = html.replace(/<\/table>/g, '</table></div>');

  return html;
}

// ══════════════════════════════════════════════════
// Chart Rendering
// ══════════════════════════════════════════════════

function renderChart(bubbleEl, chartData) {
  chartCounter++;
  const canvasId = `chart-${chartCounter}`;

  const container = document.createElement("div");
  container.className = "chart-container";
  container.innerHTML = `<canvas id="${canvasId}"></canvas>`;
  bubbleEl.appendChild(container);

  // Wait for DOM to attach
  requestAnimationFrame(() => {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;

    const chartType = (chartData.type || "bar").toLowerCase();
    const colors = generateColors(chartData.labels.length);

    new Chart(ctx, {
      type: chartType,
      data: {
        labels: chartData.labels,
        datasets: [{
          label: chartData.dataset_label || "Value",
          data: chartData.values,
          backgroundColor: chartType === "pie" ? colors : colors[0],
          borderColor: chartType === "pie" ? "#fff" : colors[0],
          borderWidth: chartType === "pie" ? 2 : 0,
          borderRadius: chartType === "bar" ? 6 : 0,
          barThickness: chartType === "bar" ? 36 : undefined,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: {
            display: chartType === "pie",
            position: "bottom",
            labels: { padding: 16, font: { size: 12 } },
          },
          tooltip: {
            backgroundColor: "#1e293b",
            titleFont: { size: 13 },
            bodyFont: { size: 12 },
            padding: 10,
            cornerRadius: 8,
          },
        },
        scales: chartType === "pie" ? {} : {
          x: {
            grid: { display: false },
            ticks: { font: { size: 12 }, color: "#64748b" },
          },
          y: {
            beginAtZero: true,
            grid: { color: "#e2e8f0" },
            ticks: {
              font: { size: 12 },
              color: "#64748b",
              callback: (val) => val >= 1000 ? (val / 1000).toFixed(0) + "K" : val,
            },
          },
        },
      },
    });
  });
}

function generateColors(count) {
  const palette = [
    "#2563eb", "#7c3aed", "#059669", "#d97706", "#dc2626",
    "#0891b2", "#4f46e5", "#16a34a", "#ea580c", "#db2777",
    "#6366f1", "#14b8a6", "#f59e0b", "#ef4444", "#8b5cf6",
  ];
  return Array.from({ length: count }, (_, i) => palette[i % palette.length]);
}

// ══════════════════════════════════════════════════
// DOM Helpers
// ══════════════════════════════════════════════════

function addUserMessage(text) {
  const row = document.createElement("div");
  row.className = "message user-message";
  row.innerHTML = `
    <div class="avatar user-avatar">U</div>
    <div class="bubble user-bubble">${escapeHtml(text)}</div>
  `;
  messagesEl.appendChild(row);
  scrollToBottom();
}

function addBotMessage(html) {
  const row = document.createElement("div");
  row.className = "message bot-message";

  const avatar = document.createElement("div");
  avatar.className = "avatar bot-avatar";
  avatar.textContent = "M";

  const bubble = document.createElement("div");
  bubble.className = "bubble bot-bubble";
  bubble.innerHTML = html;

  row.appendChild(avatar);
  row.appendChild(bubble);
  messagesEl.appendChild(row);
  scrollToBottom();

  return bubble; // return bubble ref for chart attachment
}

function addErrorMessage(text) {
  const row = document.createElement("div");
  row.className = "message bot-message";
  row.innerHTML = `
    <div class="avatar bot-avatar">M</div>
    <div class="bubble bot-bubble error-bubble">
      <strong>Error:</strong> ${escapeHtml(text)}
    </div>
  `;
  messagesEl.appendChild(row);
  scrollToBottom();
}

function addTypingIndicator() {
  const row = document.createElement("div");
  row.className = "message bot-message";
  row.id = "typing-indicator";
  row.innerHTML = `
    <div class="avatar bot-avatar">M</div>
    <div class="bubble bot-bubble">
      <div class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
    </div>
  `;
  messagesEl.appendChild(row);
  scrollToBottom();
  return row;
}

function addStreamingIndicator() {
  const row = document.createElement("div");
  row.className = "message bot-message";
  row.id = "streaming-indicator";
  row.innerHTML = `
    <div class="avatar bot-avatar">M</div>
    <div class="bubble bot-bubble">
      <div class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
      <div class="typing-status" style="font-size:12px;color:#64748b;margin-top:6px;">Thinking...</div>
    </div>
  `;
  messagesEl.appendChild(row);
  scrollToBottom();
  return row;
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function setWaiting(val) {
  isWaiting = val;
  sendBtn.disabled = val;
  inputEl.disabled = val;
  if (!val) inputEl.focus();
}

// ── Auto-resize textarea ──
function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
}

// ══════════════════════════════════════════════════
// Event Listeners
// ══════════════════════════════════════════════════

sendBtn.addEventListener("click", sendMessage);

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

inputEl.addEventListener("input", autoResize);

newSessionBtn.addEventListener("click", resetChat);

// ══════════════════════════════════════════════════
// Init
// ══════════════════════════════════════════════════

async function bootstrap() {
  await loadUiConfig();
  await startSession();
  connectWebSocket();
  inputEl.focus();
}

bootstrap();






