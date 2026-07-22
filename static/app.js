const state = {
  tasks: [],
  events: [],
  dashboard: {},
  filter: "all",
  view: "tasks",
  selectedTask: null,
  selectedStage: "plan",
};

const statusLabels = {
  queued: "排队中",
  running: "执行中",
  awaiting_approval: "等待审批",
  needs_revision: "需要修改",
  approved: "已通过",
  failed: "失败",
  rejected: "已驳回",
};

const stageLabels = {
  plan: "规划",
  implementation: "实现方案",
  review: "代码审查",
  verification: "最终验证",
  diff: "真实 Diff",
  test_output: "验证记录",
  execution_log: "执行日志",
};

const eventLabels = {
  "task.created": "任务创建",
  "task.started": "开始执行",
  "stage.completed": "阶段完成",
  "task.completed": "等待审批",
  "task.failed": "执行失败",
  "task.recovered": "任务恢复",
  "task.lease_lost": "租约失效",
  "task.approved": "审批通过",
  "task.rejected": "审批驳回",
  "task.retried": "任务重试",
  "codex.started": "Codex 执行",
  "runtime.started": "Agent 执行",
  "system.paused": "循环暂停",
  "system.resumed": "循环恢复",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN", { notation: value > 9999 ? "compact" : "standard" }).format(value || 0);
}

function formatTime(value, full = false) {
  if (!value) return "-";
  const date = new Date(value);
  return new Intl.DateTimeFormat("zh-CN", full
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
  ).format(date);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `请求失败 (${response.status})`);
  }
  return response.json();
}

async function refresh(silent = false) {
  try {
    const [tasks, events, dashboard] = await Promise.all([
      api("/api/tasks"),
      api("/api/events"),
      api("/api/dashboard"),
    ]);
    state.tasks = tasks;
    state.events = events;
    state.dashboard = dashboard;
    render();
  } catch (error) {
    if (!silent) toast(error.message, true);
  }
}

function render() {
  renderMetrics();
  renderTasks();
  renderEvents();
  renderApproval();
  renderRuntime();
}

function renderMetrics() {
  const data = state.dashboard;
  $("#activeMetric").textContent = formatNumber(data.active);
  $("#approvalMetric").textContent = formatNumber(data.awaiting_approval);
  $("#approvedMetric").textContent = formatNumber(data.approved);
  $("#tokenMetric").textContent = formatNumber((data.input_tokens || 0) + (data.output_tokens || 0));
  $("#approvalNavCount").textContent = data.awaiting_approval || 0;
  $("#pauseButton").textContent = data.paused ? "恢复循环" : "暂停循环";
  drawUsageChart();
}

function renderRuntime() {
  const labels = {
    "codex-cli": "Codex CLI 在线",
    "claude-code": "Claude Code 在线",
    openai: "OpenAI 在线",
    demo: "演示模式",
  };
  $("#runtimeMode").textContent = labels[state.dashboard.mode] || "执行器离线";
  const runtimes = state.dashboard.runtimes || [];
  const online = runtimes.filter((runtime) => (
    runtime.available && runtime.id !== "demo"
  ));
  $("#runtimeModel").textContent = `${online.length} 个 Agent 可用`;
  const runtimeStatusLabels = {
    configured: "已配置",
    installed: "已安装",
    ready: "可用",
    not_configured: "未配置",
    not_installed: "未安装",
  };
  $("#runtimeList").innerHTML = runtimes
    .filter((runtime) => runtime.id !== "demo")
    .map((runtime) => `
      <div class="runtime-item" title="${escapeHtml(`${runtime.config_source || ""} · connectivity: ${runtime.connectivity || "unknown"}`)}">
        <i class="runtime-health ${runtime.available ? "is-available" : ""}"></i>
        <div class="runtime-copy">
          <strong>${escapeHtml(runtime.name)} · ${escapeHtml(runtimeStatusLabels[runtime.status] || runtime.status)}</strong>
          <span>${escapeHtml(runtime.provider || "-")} · ${escapeHtml(runtime.model || "-")}</span>
        </div>
      </div>
    `).join("");
  const availability = new Map(
    runtimes.map((runtime) => [runtime.id, runtime.available])
  );
  $$('#taskForm select[name="runtime_requested"] option').forEach((option) => {
    option.dataset.label ||= option.textContent;
    const alwaysAvailable = ["auto", "demo"].includes(option.value);
    option.disabled = !alwaysAvailable && availability.get(option.value) === false;
    option.textContent = `${option.dataset.label}${option.disabled ? "（不可用）" : ""}`;
  });
  $("#runtimeDot").classList.toggle("is-live", !state.dashboard.paused);
}

function filteredTasks() {
  if (state.filter === "active") return state.tasks.filter((task) => ["queued", "running"].includes(task.status));
  if (state.filter === "review") return state.tasks.filter((task) => task.status === "awaiting_approval");
  return state.tasks;
}

function renderTasks() {
  const tasks = filteredTasks();
  $("#taskSummary").textContent = `${tasks.length} 个任务`;
  $("#taskEmpty").hidden = tasks.length > 0;
  $("#taskList").innerHTML = tasks.map((task) => `
    <div class="task-row" role="row" data-task-id="${escapeHtml(task.id)}" tabindex="0">
      <div class="task-name">
        <strong>${escapeHtml(task.title)}</strong>
        <span>${escapeHtml(task.repository || task.repository_path || task.issue_url || "本地任务")} · ${escapeHtml(task.executor_mode || task.runtime_requested || "auto")}</span>
      </div>
      <span class="badge status-${escapeHtml(task.status)}">${escapeHtml(statusLabels[task.status] || task.status)}</span>
      <span class="priority priority-P${task.priority}">P${task.priority}</span>
      <span class="time">${formatTime(task.updated_at)}</span>
      <span class="row-arrow" aria-hidden="true">›</span>
    </div>
  `).join("");

  $$("[data-task-id]").forEach((row) => {
    const open = () => openTask(row.dataset.taskId);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") open();
    });
  });
}

function renderEvents() {
  const html = state.events.map((event) => `
    <div class="event-item ${event.kind === "task.completed" || event.kind === "task.approved" ? "is-important" : ""}">
      <strong>${escapeHtml(event.message)}</strong>
      <span>${escapeHtml(eventLabels[event.kind] || event.kind)} · ${formatTime(event.created_at, true)}</span>
    </div>
  `).join("");
  $("#eventList").innerHTML = html || '<div class="empty-state"><span>暂无运行事件</span></div>';
  $("#eventTable").innerHTML = state.events.map((event) => `
    <div class="event-line">
      <span>${formatTime(event.created_at, true)}</span>
      <span class="event-kind">${escapeHtml(event.kind)}</span>
      <strong>${escapeHtml(event.message)}</strong>
    </div>
  `).join("") || '<div class="empty-state"><span>暂无运行记录</span></div>';
}

function renderApproval() {
  const tasks = state.tasks.filter((task) => task.status === "awaiting_approval");
  $("#approvalList").innerHTML = tasks.map((task) => `
    <article class="approval-item">
      <div class="approval-main">
        <h3>${escapeHtml(task.title)}</h3>
        <p>${escapeHtml(task.repository || task.repository_path || "本地任务")} · ${escapeHtml(task.executor_mode || "pending")} · ${formatNumber(task.input_tokens + task.output_tokens)} tokens</p>
      </div>
      <div class="approval-actions">
        <button class="button button-secondary" data-open-task="${escapeHtml(task.id)}">查看结果</button>
        <button class="button button-success" data-decision="approve" data-id="${escapeHtml(task.id)}">通过</button>
      </div>
    </article>
  `).join("") || '<div class="empty-state"><strong>没有等待审批的任务</strong><span>完成四阶段执行的任务会出现在这里。</span></div>';

  $$("[data-open-task]").forEach((button) => button.addEventListener("click", () => openTask(button.dataset.openTask)));
  $$("[data-decision]").forEach((button) => button.addEventListener("click", () => decide(button.dataset.id, button.dataset.decision)));
}

function drawUsageChart() {
  const canvas = $("#usageChart");
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);

  const values = state.tasks.slice(0, 12).reverse().map((task) => task.input_tokens + task.output_tokens);
  const points = values.length > 1 ? values : [0, values[0] || 0];
  const max = Math.max(...points, 1);
  const pad = 4;
  const step = (width - pad * 2) / (points.length - 1);

  context.beginPath();
  points.forEach((value, index) => {
    const x = pad + step * index;
    const y = height - pad - (value / max) * (height - pad * 2);
    if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
  });
  context.strokeStyle = "#237a50";
  context.lineWidth = 2;
  context.stroke();
}

async function openTask(taskId) {
  try {
    state.selectedTask = await api(`/api/tasks/${taskId}`);
    state.selectedStage = ["diff", "verification", "review", "implementation", "plan"].find((key) => state.selectedTask[key]) || "plan";
    renderDetail();
    $("#detailDialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderDetail() {
  const task = state.selectedTask;
  if (!task) return;
  const availableStages = Object.keys(stageLabels).filter((key) => task[key] || ["plan", "implementation", "review", "verification"].includes(key));
  const actions = task.status === "awaiting_approval"
    ? `<button class="button button-danger" data-detail-action="reject">驳回</button><button class="button button-success" data-detail-action="approve">审批通过</button>`
    : ["failed", "rejected", "needs_revision"].includes(task.status)
      ? `<button class="button button-primary" data-detail-action="retry">重新排队</button>`
      : "";

  $("#detailContent").innerHTML = `
    <header class="dialog-head">
      <div>
        <span class="badge status-${escapeHtml(task.status)}">${escapeHtml(statusLabels[task.status] || task.status)}</span>
        <h2>${escapeHtml(task.title)}</h2>
      </div>
      <button class="icon-button" data-close-detail title="关闭" aria-label="关闭">×</button>
    </header>
    <div class="detail-meta">
      <span>仓库：${escapeHtml(task.repository || "未提供")}</span>
      <span>执行器：${escapeHtml(task.executor_mode || "等待分配")}</span>
      <span>请求运行时：${escapeHtml(task.runtime_requested || "auto")}</span>
      ${task.runtime_provider ? `<span>Provider：${escapeHtml(task.runtime_provider)}</span>` : ""}
      ${task.runtime_model ? `<span>模型：${escapeHtml(task.runtime_model)}</span>` : ""}
      <span>风险：${escapeHtml(task.risk)}</span>
      <span>尝试：${task.attempts}/${task.max_attempts}</span>
      <span>Token：${formatNumber(task.input_tokens + task.output_tokens)}</span>
      ${task.cost_usd ? `<span>费用：$${Number(task.cost_usd).toFixed(4)}</span>` : ""}
      ${task.worktree_path ? `<span class="meta-wide">工作树：${escapeHtml(task.worktree_path)}</span>` : ""}
    </div>
    <div class="detail-description">${escapeHtml(task.description)}</div>
    <div class="stage-tabs">
      ${availableStages.map((key) => `<button class="stage-tab ${state.selectedStage === key ? "is-active" : ""}" data-stage="${key}">${stageLabels[key]}</button>`).join("")}
    </div>
    <pre class="stage-output">${escapeHtml(task[state.selectedStage] || "该阶段尚未完成。")}</pre>
    <footer class="detail-actions">
      <span class="detail-error">${escapeHtml(task.error || "")}</span>
      ${actions}
    </footer>
  `;

  $("[data-close-detail]").addEventListener("click", () => $("#detailDialog").close());
  $$("[data-stage]").forEach((button) => button.addEventListener("click", () => {
    state.selectedStage = button.dataset.stage;
    renderDetail();
  }));
  requestAnimationFrame(() => {
    const stageStrip = $(".stage-tabs");
    const activeStage = $(".stage-tab.is-active");
    if (!stageStrip || !activeStage) return;
    const stripRect = stageStrip.getBoundingClientRect();
    const activeRect = activeStage.getBoundingClientRect();
    stageStrip.scrollLeft += activeRect.left - stripRect.left
      - (stageStrip.clientWidth - activeStage.clientWidth) / 2;
  });
  $$("[data-detail-action]").forEach((button) => button.addEventListener("click", async () => {
    await decide(task.id, button.dataset.detailAction);
  }));
}

async function decide(taskId, action) {
  try {
    await api(`/api/tasks/${taskId}/${action}`, { method: "POST" });
    if ($("#detailDialog").open) $("#detailDialog").close();
    toast(action === "approve" ? "任务已审批通过" : action === "reject" ? "任务已驳回" : "任务已重新排队");
    await refresh(true);
  } catch (error) {
    toast(error.message, true);
  }
}

function switchView(view) {
  state.view = view;
  const titles = { tasks: "任务流", approval: "审批区", events: "运行记录" };
  $("#pageTitle").textContent = titles[view];
  $("#tasksView").hidden = view !== "tasks";
  $("#approvalView").hidden = view !== "approval";
  $("#eventsView").hidden = view !== "events";
  $$(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.style.background = error ? "#9d3232" : "#242925";
  element.classList.add("is-visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("is-visible"), 2600);
}

function connectWebSocket() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket.addEventListener("open", () => socket.send("ready"));
  socket.addEventListener("message", () => refresh(true));
  socket.addEventListener("close", () => setTimeout(connectWebSocket, 2000));
}

$("#newTaskButton").addEventListener("click", () => {
  $("#taskForm").reset();
  $("#formError").textContent = "";
  $("#taskDialog").showModal();
});

$$('[data-close-task]').forEach((button) => button.addEventListener("click", () => $("#taskDialog").close()));

$("#taskForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.priority = Number(payload.priority);
  try {
    await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
    $("#taskDialog").close();
    toast("任务已加入维护队列");
    await refresh(true);
  } catch (error) {
    $("#formError").textContent = error.message;
  }
});

$("#refreshButton").addEventListener("click", () => refresh());
$("#pauseButton").addEventListener("click", async () => {
  try {
    await api("/api/control/pause", {
      method: "POST",
      body: JSON.stringify({ paused: !state.dashboard.paused }),
    });
    toast(state.dashboard.paused ? "任务循环已恢复" : "任务循环已暂停");
    await refresh(true);
  } catch (error) {
    toast(error.message, true);
  }
});

$$('[data-view]').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
$$('[data-filter]').forEach((button) => button.addEventListener("click", () => {
  state.filter = button.dataset.filter;
  $$("[data-filter]").forEach((item) => item.classList.toggle("is-active", item === button));
  renderTasks();
}));

$("#detailDialog").addEventListener("click", (event) => {
  if (event.target === $("#detailDialog")) $("#detailDialog").close();
});

refresh();
connectWebSocket();
setInterval(() => refresh(true), 15000);
