/* Lossless UI controller — vanilla JS, no build step. */

const $ = (sel) => document.querySelector(sel);

const state = {
  pollHandle: null,
  pendingActionId: null,
  approveButton: null,
};

// ---------- networking ----------

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  return r.json();
}

// ---------- status ----------

async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("#mcp-mode").textContent =
      s.mcp_mode === "dynatrace-mcp"
        ? "MCP: Dynatrace ✓"
        : "MCP: synthetic (demo)";
    $("#model-name").textContent = s.gemini_model;
  } catch (e) {
    $("#mcp-mode").textContent = "MCP: error";
  }
}

// ---------- dashboard polling ----------

function fmtMoney(n) {
  if (n == null) return "$0";
  return "$" + Math.round(n).toLocaleString();
}
function fmtPct(n) {
  if (n == null) return "—";
  return (n * 100).toFixed(2) + "%";
}

async function refreshDashboard() {
  try {
    const d = await api("/api/dashboard");
    const m = d.metrics;
    const r = d.revenue_impact_30m;

    // KPIs
    const lossEl = $("#kpi-loss");
    const lossVal = lossEl.querySelector(".kpi-value");
    const lossSub = lossEl.querySelector(".kpi-sub");
    lossVal.textContent = fmtMoney(r.estimated_loss_usd);
    lossSub.textContent =
      "vs. expected " + fmtMoney(r.expected_revenue_usd) + " · " +
      r.checkouts_lost + " checkouts lost";
    if (r.estimated_loss_usd > 1) {
      lossEl.classList.add("bleeding");
    } else {
      lossEl.classList.remove("bleeding");
    }

    $("#kpi-conv").textContent = fmtPct(m.conversion_rate);
    $("#kpi-conv-sub").textContent =
      "baseline " + fmtPct(m.baseline_conversion_rate);

    $("#kpi-sessions").textContent = m.active_sessions.toLocaleString();
    $("#kpi-cpm").textContent = m.checkouts_per_min;

    // Problems
    const probEl = $("#problems-list");
    const probs = d.problems || [];
    $("#problems-count").textContent = probs.length;
    if (probs.length === 0) {
      probEl.innerHTML =
        '<div class="empty">No open problems. Everything\'s running cleanly.</div>';
    } else {
      probEl.innerHTML = probs
        .map(
          (p) => `
        <div class="problem ${p.severity === "warning" ? "warn" : ""}">
          <div>
            <div class="problem-title">${escape(p.title)}</div>
            <div class="problem-meta">
              ${escape(p.incident_id)} · ${escape(p.service)} ·
              ${new Date(p.detected_at).toLocaleTimeString()}
            </div>
          </div>
          <span class="problem-tag">${escape(p.severity)}</span>
        </div>`
        )
        .join("");
    }

    // Services
    const svcBody = $("#svc-body");
    svcBody.innerHTML = Object.entries(m.services)
      .map(([name, s]) => {
        const latClass =
          s.latency_ms_p95 > 1500
            ? "svc-bad"
            : s.latency_ms_p95 > 800
            ? "svc-warn"
            : "";
        const errClass =
          s.error_rate > 0.05
            ? "svc-bad"
            : s.error_rate > 0.02
            ? "svc-warn"
            : "";
        return `
          <tr>
            <td class="svc-name">${escape(name)}</td>
            <td class="${latClass}">${s.latency_ms_p95} ms</td>
            <td class="${errClass}">${(s.error_rate * 100).toFixed(2)}%</td>
            <td>${s.requests_per_min}</td>
          </tr>`;
      })
      .join("");
  } catch (e) {
    console.warn("dashboard refresh failed", e);
  }
}

async function refreshActionLog() {
  try {
    const d = await api("/api/actions/recent?limit=12");
    const el = $("#action-log");
    if (!d.actions.length) {
      el.innerHTML = '<div class="row"><span class="muted">No agent activity yet.</span></div>';
      return;
    }
    el.innerHTML = d.actions
      .map((a) => {
        const when = new Date(a.at).toLocaleTimeString();
        let what;
        if (a.kind === "tool_call") {
          what = `<span class="name">${escape(a.name)}</span>(${escape(
            JSON.stringify(a.args).slice(0, 60)
          )})`;
        } else if (a.kind === "chat_turn") {
          what = `chat: <em>${escape(a.user.slice(0, 60))}</em> → ${a.tool_call_count} tool calls`;
        } else {
          what = escape(a.kind);
        }
        return `<div class="row"><span class="when">${when}</span><span class="what">${what}</span></div>`;
      })
      .join("");
  } catch (e) {
    // silent — non-critical
  }
}

// ---------- chat ----------

function appendMsg(role, text, opts = {}) {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = escape(text).replace(/\n/g, "<br/>");
  if (opts.toolCalls && opts.toolCalls.length) {
    const tools = document.createElement("div");
    tools.className = "msg-tool";
    tools.textContent = opts.toolCalls
      .map((t) => `• ${t.name}(${shortArgs(t.args)})`)
      .join("\n");
    div.appendChild(tools);
  }
  if (opts.approveActionId) {
    const block = document.createElement("div");
    block.className = "approve-block";
    block.innerHTML = `
      <div class="label">⚠ This change needs your sign-off before I apply it.</div>
      <button class="btn-approve">Approve & apply</button>`;
    const btn = block.querySelector("button");
    btn.onclick = () => approveAndExecute(opts.approveActionId, btn);
    div.appendChild(block);
    state.pendingActionId = opts.approveActionId;
    state.approveButton = btn;
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function appendThinking() {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg agent";
  div.id = "thinking";
  div.innerHTML = '<span class="dots"><span></span><span></span><span></span></span>';
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}
function removeThinking() {
  const t = $("#thinking");
  if (t) t.remove();
}

function shortArgs(args) {
  try {
    const s = JSON.stringify(args);
    return s.length > 50 ? s.slice(0, 47) + "…" : s;
  } catch {
    return "{}";
  }
}

function escape(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function sendChat(message) {
  if (!message.trim()) return;
  appendMsg("user", message);
  $("#chat-input").value = "";
  $("#chat-send").disabled = true;
  appendThinking();
  try {
    const r = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    removeThinking();
    appendMsg("agent", r.reply, {
      toolCalls: r.tool_calls || [],
      approveActionId: r.proposed_action_id,
    });
  } catch (e) {
    removeThinking();
    appendMsg("system", "Agent error: " + e.message);
  } finally {
    $("#chat-send").disabled = false;
    refreshActionLog();
    refreshDashboard();
  }
}

async function approveAndExecute(actionId, btn) {
  btn.disabled = true;
  btn.textContent = "Applying…";
  try {
    await api(`/api/actions/${actionId}/approve`, { method: "POST" });
    // Then send a follow-up message so the agent executes & verifies
    await sendChat(
      "Approved. Please execute the staged action and verify recovery."
    );
    btn.textContent = "✓ Approved";
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Approve & apply";
    appendMsg("system", "Approval failed: " + e.message);
  }
}

// ---------- demo controls ----------

document.querySelectorAll(".btn-demo").forEach((b) => {
  b.addEventListener("click", async () => {
    const kind = b.dataset.kind;
    if (b.id === "btn-reset") {
      await api("/api/demo/reset", { method: "POST" });
      $("#chat").innerHTML = "";
      welcome();
    } else {
      await api("/api/demo/inject", {
        method: "POST",
        body: JSON.stringify({ kind }),
      });
      appendMsg(
        "system",
        "Demo: injected synthetic " + kind.replace(/_/g, " ") + " incident."
      );
    }
    refreshDashboard();
    refreshActionLog();
  });
});

// ---------- chat form ----------

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  sendChat($("#chat-input").value);
});
document.querySelectorAll(".chip").forEach((c) => {
  c.addEventListener("click", () => sendChat(c.dataset.msg));
});

// ---------- bootstrap ----------

function welcome() {
  appendMsg(
    "agent",
    "Hi — I'm Lossless. I'm watching your storefront for problems that cost you customers. " +
      "Ask me anything (\"how's the store doing?\") or trigger a demo incident from the panel on the left."
  );
}

(async function init() {
  await loadStatus();
  await refreshDashboard();
  await refreshActionLog();
  welcome();
  state.pollHandle = setInterval(() => {
    refreshDashboard();
    refreshActionLog();
  }, 3000);
})();
