import {api, downloadFile, thumbnailUrl} from "./api.js";

const STARTUP_DURATION_MS = 3500;

const state = {
  view: "baidu",
  paths: {local: "/", baidu: "/", quark: "/", alipan: "/"},
  providerStatus: {baidu: null, quark: null, alipan: null},
  settings: null,
  credentials: null,
  downloads: [],
  downloadDirectory: "",
  onboarding: null,
  baiduGuide: null,
  baiduGuideDeferred: false,
  guideStep: 1,
  settingsTab: "credentials",
  downloadTimer: null,
  startupFinished: false,
  renameTarget: null,
  qrProvider: "",
  qrSession: "",
  qrTimer: null,
  downloadsRefreshing: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function formatSize(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatTime(value) {
  if (!value) return "—";
  const numeric = Number(value);
  const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString("zh-CN", {hour12: false});
}

function formatDuration(seconds) {
  const value = Math.max(0, Math.round(Number(seconds || 0)));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.ceil(value / 60)} 分钟`;
  return `${Math.floor(value / 3600)} 小时 ${Math.ceil((value % 3600) / 60)} 分钟`;
}

function toast(message, error = false) {
  const item = document.createElement("div");
  item.className = `toast${error ? " is-error" : ""}`;
  item.textContent = message;
  $("#toast-region").append(item);
  window.setTimeout(() => item.remove(), 3600);
}

function setTheme(next) {
  if (next === "auto") {
    document.documentElement.removeAttribute("data-theme");
    localStorage.removeItem("clouddl:theme");
  } else {
    document.documentElement.dataset.theme = next;
    localStorage.setItem("clouddl:theme", next);
  }
  $$("[data-theme-toggle], #mobile-theme").forEach((button) => {
    button.title = `当前主题：${next === "auto" ? "跟随系统" : next === "light" ? "浅色" : "深色"}`;
  });
}

function applyNextTheme() {
  const current = localStorage.getItem("clouddl:theme") || "auto";
  setTheme(current === "auto" ? "light" : current === "light" ? "dark" : "auto");
}

function cycleTheme(event) {
  const root = document.documentElement;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || !document.startViewTransition) {
    root.classList.add("theme-fallback");
    applyNextTheme();
    window.setTimeout(() => root.classList.remove("theme-fallback"), 460);
    return;
  }
  const rect = event.currentTarget.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  const radius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  );
  root.style.setProperty("--theme-transition-x", `${x}px`);
  root.style.setProperty("--theme-transition-y", `${y}px`);
  root.style.setProperty("--theme-transition-radius", `${radius}px`);
  document.startViewTransition(applyNextTheme);
}

function finishStartup() {
  state.startupFinished = true;
  sessionStorage.setItem("clouddl:intro-seen", "1");
  $("#startup").classList.add("is-finished");
  $("#app-shell").classList.remove("is-preparing");
  window.setTimeout(() => {
    $("#startup").classList.add("is-hidden");
    showOnboardingIfNeeded();
  }, 430);
}

function startStartup() {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const seen = sessionStorage.getItem("clouddl:intro-seen") === "1";
  if (seen || reduced) {
    window.setTimeout(finishStartup, reduced ? 260 : 40);
    return;
  }
  window.setTimeout(finishStartup, STARTUP_DURATION_MS);
}

async function preload() {
  state.onboarding = await api.onboardingStatus();
  if (!state.onboarding.required) {
    await Promise.allSettled([loadSettings(), loadCredentials(), refreshDownloads(), loadBaiduGuide()]);
  } else if (state.startupFinished) {
    showOnboardingIfNeeded();
  }
}

function showOnboardingIfNeeded() {
  if (!state.onboarding?.required) return;
  $("#guide").classList.remove("is-hidden");
  $("#disclaimer-title").textContent = state.onboarding.title;
  const copy = $("#disclaimer-copy");
  copy.replaceChildren(...state.onboarding.paragraphs.map((paragraph) => {
    const element = document.createElement("p");
    element.textContent = paragraph;
    return element;
  }));
  renderGuideStep();
}

function renderGuideStep() {
  $$("[data-guide-step]").forEach((step) => {
    step.classList.toggle("is-active", Number(step.dataset.guideStep) === state.guideStep);
  });
  $$(".guide-progress span").forEach((bar, index) => {
    bar.classList.toggle("is-current", index < state.guideStep);
  });
  $("#guide-back").classList.toggle("is-hidden", state.guideStep === 1);
  $("#guide-next").classList.toggle("is-hidden", state.guideStep === 5);
  $("#guide-finish").classList.toggle("is-hidden", state.guideStep !== 5);
}

async function finishGuide() {
  if (!$("#disclaimer-accept").checked) return;
  try {
    state.onboarding = await api.acceptOnboarding(state.onboarding.version);
    $("#guide").classList.add("is-hidden");
    await Promise.allSettled([loadSettings(), loadCredentials(), refreshDownloads(), loadBaiduGuide()]);
    await activateView("baidu");
    toast("欢迎使用 多网盘下载器");
  } catch (error) {
    toast(error.message, true);
  }
}

async function activateView(view) {
  state.view = view;
  $$(".nav-item[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $$(".view").forEach((panel) => panel.classList.toggle("is-active", panel.dataset.panel === view));
  if (["local", "baidu", "quark", "alipan"].includes(view)) await loadProvider(view);
  if (view === "baidu") showBaiduGuideIfNeeded();
  if (view === "downloads") await refreshDownloads();
  if (view === "settings") {
    await Promise.all([loadSettings(), loadCredentials()]);
    fillSettingsForm();
    activateSettingsTab(state.settingsTab);
  }
}

async function loadBaiduGuide() {
  state.baiduGuide = await api.baiduGuideStatus();
}

function showBaiduGuideIfNeeded() {
  if (state.onboarding?.required || state.baiduGuideDeferred || !state.baiduGuide?.required) return;
  $("#baidu-guide").classList.remove("is-hidden");
}

async function completeBaiduGuide(openSettings = false) {
  try {
    state.baiduGuide = await api.completeBaiduGuide();
    $("#baidu-guide").classList.add("is-hidden");
    if (openSettings) {
      await activateView("settings");
      activateSettingsTab("credentials");
    }
  } catch (error) {
    toast(error.message, true);
  }
}

function activateSettingsTab(tab) {
  state.settingsTab = tab;
  $$("[data-settings-tab]").forEach((button) => {
    const active = button.dataset.settingsTab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $$("[data-settings-panel]").forEach((panel) => {
    const active = panel.dataset.settingsPanel === tab;
    panel.classList.toggle("is-active", active);
    panel.hidden = !active;
  });
}

async function loadProvider(provider) {
  renderBreadcrumbs(provider, state.paths[provider]);
  if (provider === "local") {
    try {
      state.settings = state.settings || await api.settings();
      $("#local-root-note").textContent = state.settings?.download_dir || "当前下载目录";
      await loadFiles(provider, state.paths[provider]);
    } catch (error) {
      showProviderEmpty(provider, error.message);
    }
    return;
  }
  try {
    const status = await api.providerStatus(provider);
    state.providerStatus[provider] = status;
    renderProviderStatus(provider, status);
    if (status.logged_in) await loadFiles(provider, state.paths[provider]);
    else {
      const label = provider === "baidu" ? "百度网盘" : provider === "quark" ? "夸克网盘" : "阿里云盘";
      showProviderEmpty(provider, status.error || (status.configured ? "凭据已保存，当前登录状态需要重新验证" : `请先在设置中配置${label}凭据`));
    }
  } catch (error) {
    showProviderEmpty(provider, error.message);
  }
}

function renderProviderStatus(provider, status) {
  const account = $(`#${provider}-account`);
  const dot = $(`#${provider}-dot`);
  account.textContent = status.logged_in ? `已连接：${status.username || "账号"}` : status.configured ? "已配置" : "未连接";
  account.classList.toggle("is-online", Boolean(status.logged_in));
  dot.classList.toggle("is-online", Boolean(status.logged_in));
}

function showProviderEmpty(provider, message) {
  $(`#${provider}-files`).replaceChildren();
  const empty = $(`#${provider}-empty`);
  empty.textContent = message;
  empty.classList.add("is-visible");
}

async function loadFiles(provider, path) {
  state.paths[provider] = path;
  const empty = $(`#${provider}-empty`);
  empty.textContent = "正在读取文件…";
  empty.classList.add("is-visible");
  $(`#${provider}-files`).replaceChildren();
  renderBreadcrumbs(provider, path);
  try {
    const result = await api.files(provider, path);
    renderFiles(provider, result.files || []);
  } catch (error) {
    showProviderEmpty(provider, error.message);
  }
}

function renderBreadcrumbs(provider, path) {
  const target = $(`#${provider}-breadcrumbs`);
  const parts = path.split("/").filter(Boolean);
  const entries = [{label: "全部文件", path: "/"}];
  let current = "";
  for (const part of parts) {
    current += `/${part}`;
    entries.push({label: part, path: current});
  }
  target.replaceChildren();
  entries.forEach((entry, index) => {
    if (index) {
      const separator = document.createElement("span");
      separator.className = "breadcrumb-separator";
      separator.textContent = "›";
      target.append(separator);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "breadcrumb";
    button.textContent = entry.label;
    if (index < entries.length - 1) button.addEventListener("click", () => loadFiles(provider, entry.path));
    target.append(button);
  });
}

function generatedFileIcon(file) {
  const icon = document.createElement("span");
  icon.className = `file-icon generated-file-icon ${file.is_dir ? "folder" : "document"}`;
  return icon;
}

function fileIcon(provider, file) {
  if (!file.has_thumbnail) return generatedFileIcon(file);
  const image = document.createElement("img");
  image.className = "file-thumbnail";
  image.alt = "";
  image.loading = "lazy";
  const key = provider === "local" || provider === "baidu"
    ? file.path
    : file.fid;
  image.src = thumbnailUrl(provider, key);
  image.addEventListener("error", () => image.replaceWith(generatedFileIcon(file)), {once: true});
  return image;
}

function renderFiles(provider, files) {
  const body = $(`#${provider}-files`);
  const empty = $(`#${provider}-empty`);
  body.replaceChildren();
  if (!files.length) {
    empty.textContent = "当前目录为空";
    empty.classList.add("is-visible");
    return;
  }
  empty.classList.remove("is-visible");
  const sorted = [...files].sort((a, b) => Number(b.is_dir) - Number(a.is_dir));
  sorted.forEach((file) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const nameButton = document.createElement("button");
    nameButton.type = "button";
    nameButton.className = `file-name-button${file.is_dir ? " is-folder" : ""}`;
    nameButton.append(fileIcon(provider, file));
    const name = document.createElement("span");
    name.textContent = file.name;
    nameButton.append(name);
    if (file.is_dir) {
      const path = file.path || `${state.paths[provider].replace(/\/$/, "")}/${file.name}`;
      nameButton.addEventListener("click", () => loadFiles(provider, path || "/"));
    }
    nameCell.append(nameButton);
    const size = document.createElement("td");
    size.textContent = file.is_dir ? "—" : formatSize(file.size);
    const modified = document.createElement("td");
    modified.textContent = formatTime(file.mtime);
    const action = document.createElement("td");
    action.className = "file-action-cell";
    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.className = "file-operation rename-operation";
    renameButton.title = `重命名 ${file.name}`;
    renameButton.setAttribute("aria-label", `重命名 ${file.name}`);
    const renameIcon = document.createElement("span");
    renameIcon.className = "rename-operation-icon";
    renameIcon.setAttribute("aria-hidden", "true");
    renameButton.append(renameIcon);
    renameButton.addEventListener("click", () => openRenameDialog(provider, file));
    action.append(renameButton);
    if (provider !== "local") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button download-button";
      button.textContent = file.is_dir ? "下载文件夹" : "下载";
      button.addEventListener("click", () => addDownload(provider, file));
      action.append(button);
    }
    row.append(nameCell, size, modified, action);
    body.append(row);
  });
}

function openRenameDialog(provider, file) {
  state.renameTarget = {provider, file};
  $("#rename-current-name").textContent = `当前名称：${file.name}`;
  $("#rename-input").value = file.name;
  $("#rename-dialog").classList.remove("is-hidden");
  window.setTimeout(() => {
    const input = $("#rename-input");
    input.focus();
    const dot = file.is_dir ? -1 : file.name.lastIndexOf(".");
    input.setSelectionRange(0, dot > 0 ? dot : file.name.length);
  }, 0);
}

function closeRenameDialog() {
  state.renameTarget = null;
  $("#rename-dialog").classList.add("is-hidden");
}

async function submitRename(event) {
  event.preventDefault();
  const target = state.renameTarget;
  if (!target) return;
  const newName = $("#rename-input").value.trim();
  const {provider, file} = target;
  const payload = provider === "local" || provider === "baidu"
    ? {path: file.path, new_name: newName}
    : {fid: file.fid, new_name: newName};
  try {
    await api.rename(provider, payload);
    closeRenameDialog();
    toast(`已重命名为：${newName}`);
    await loadFiles(provider, state.paths[provider]);
  } catch (error) {
    toast(error.message, true);
  }
}

async function searchFiles(provider) {
  const input = $(`#${provider}-search`);
  const keyword = input.value.trim();
  if (!keyword) {
    await loadFiles(provider, state.paths[provider]);
    return;
  }
  try {
    const result = await api.search(provider, keyword, state.paths[provider]);
    renderFiles(provider, result.files || []);
  } catch (error) {
    showProviderEmpty(provider, error.message);
  }
}

async function addDownload(provider, file) {
  const id = provider === "baidu" ? file.fs_id : file.fid;
  try {
    if (file.is_dir) {
      const payload = provider === "baidu"
        ? {path: file.path, name: file.name}
        : {fid: file.fid, name: file.name};
      const result = await api.downloadFolder(provider, payload);
      toast(`已添加文件夹：${file.name}，共 ${result.task_count} 个任务`);
    } else {
      await api.download(provider, id, file.path);
      toast(`已添加：${file.name}`);
    }
    await refreshDownloads();
  } catch (error) {
    toast(error.message, true);
  }
}

const statusLabels = {
  pending: "等待中", queued: "排队中", waiting_schedule: "等待时段",
  connecting: "正在连接",
  downloading: "下载中", paused_disk: "磁盘空间不足", completed: "已完成",
  paused: "已暂停", skipped: "已跳过", error: "失败", cancelled: "已取消",
};

async function refreshDownloads() {
  if (state.downloadsRefreshing) return;
  state.downloadsRefreshing = true;
  try {
    const result = await api.downloads();
    state.downloads = result.tasks || [];
    state.downloadDirectory = result.download_directory || "";
    renderDownloads();
  } catch (error) {
    if (error.status !== 403) toast(error.message, true);
  } finally {
    state.downloadsRefreshing = false;
  }
}

function renderDownloads() {
  const list = $("#download-list");
  const empty = $("#downloads-empty");
  $("#download-directory").textContent = (
    state.downloadDirectory || "正在读取保存目录…"
  );
  list.replaceChildren();
  const tasks = [...state.downloads].reverse();
  empty.classList.toggle("is-hidden", tasks.length > 0);
  const active = tasks.filter((task) => !["completed", "error", "cancelled", "skipped", "paused"].includes(task.status)).length;
  $("#download-count").textContent = String(active);
  ["baidu", "quark", "alipan"].forEach((provider) => {
    const providerTasks = tasks.filter((task) => task.provider === provider);
    const providerActive = providerTasks.filter((task) => !["completed", "error", "cancelled", "skipped", "paused"].includes(task.status)).length;
    const providerCompleted = providerTasks.filter((task) => task.status === "completed").length;
    const providerDownloaded = providerTasks.reduce((sum, task) => sum + Math.max(0, Number(task.downloaded || 0)), 0);
    $(`#${provider}-active`).textContent = String(providerActive);
    $(`#${provider}-completed`).textContent = String(providerCompleted);
    $(`#${provider}-total`).textContent = formatSize(providerDownloaded);
  });
  tasks.forEach((task) => {
    const card = document.createElement("article");
    card.className = "download-card";
    const head = document.createElement("div");
    head.className = "download-head";
    const name = document.createElement("strong");
    name.textContent = task.filename;
    const status = document.createElement("span");
    status.className = "status-label";
    status.textContent = statusLabels[task.status] || task.status;
    head.append(name, status);
    const track = document.createElement("div");
    track.className = "progress-track";
    const fill = document.createElement("div");
    fill.className = "progress-fill";
    fill.style.width = `${Math.max(0, Math.min(100, Number(task.progress || 0)))}%`;
    track.append(fill);
    const meta = document.createElement("div");
    meta.className = "download-meta";
    const amount = document.createElement("span");
    amount.textContent = `${formatSize(task.downloaded)} / ${formatSize(task.total_size)} · ${Number(task.progress || 0).toFixed(1)}%`;
    const speed = document.createElement("span");
    const transfer = [];
    if (task.speed) transfer.push(`${formatSize(task.speed)}/s`);
    if (task.connections_used) transfer.push(`${task.connections_used} 连接`);
    if (task.per_connection_speed && task.connections_used > 1) {
      transfer.push(`单连接约 ${formatSize(task.per_connection_speed)}/s`);
    }
    if (task.degradation_reason) transfer.push(`降级：${task.degradation_reason}`);
    if (task.range_supported === false && !task.degradation_reason) transfer.push("流式降级");
    if (task.baidu_app_id_used) transfer.push(`百度 app_id ${task.baidu_app_id_used}`);
    if (task.source_profile === "quark-desktop") transfer.push("夸克 PC 身份");
    if (task.source_profile === "quark-web") transfer.push("网页回退");
    if (task.source_profile === "alipan-openapi") transfer.push("阿里云盘 OpenAPI");
    if (task.source_profile === "alipan-private") transfer.push("阿里云盘网页兼容通道");
    if (task.resumed_bytes > 0) transfer.push(`已续传 ${formatSize(task.resumed_bytes)}`);
    if (task.url_refresh_count > 0) transfer.push(`直链已刷新 ${task.url_refresh_count} 次`);
    if (task.resume_available && ["error", "cancelled"].includes(task.status)) {
      transfer.push("可断点续传");
    }
    if (task.eta_seconds != null && task.status === "downloading") {
      transfer.push(`预计剩余 ${formatDuration(task.eta_seconds)}`);
    }
    speed.textContent = task.error || transfer.join(" · ") || statusLabels[task.status] || "";
    meta.append(amount, speed);
    card.append(head, track, meta);
    if (task.save_path) {
      const savePath = document.createElement("div");
      savePath.className = "download-save-path";
      savePath.textContent = `保存位置：${task.save_path}`;
      savePath.title = task.save_path;
      card.append(savePath);
    }
    if (!["completed", "error", "cancelled", "skipped"].includes(task.status)) {
      const actions = document.createElement("div");
      actions.className = "inline-actions";
      const pause = document.createElement("button");
      pause.type = "button";
      pause.className = "button button-secondary";
      pause.textContent = task.status === "paused" ? "\u7EE7\u7EED" : "\u6682\u505C";
      pause.addEventListener("click", async () => {
        if (task.status === "paused") await api.resumeDownload(task.id);
        else await api.pauseDownload(task.id);
        await refreshDownloads();
      });
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "button button-text danger";
      cancel.textContent = "取消";
      cancel.addEventListener("click", async () => {
        await api.cancelDownload(task.id);
        await refreshDownloads();
      });
      actions.append(pause, cancel);
      card.append(actions);
    }
    list.append(card);
  });
}

async function loadSettings() {
  try { state.settings = await api.settings(); }
  catch (error) { if (error.status !== 403) throw error; }
}

async function loadCredentials() {
  try {
    state.credentials = await api.credentials();
    renderCredentialStates();
  } catch (error) {
    if (error.status !== 403) throw error;
  }
}

function renderCredentialStates() {
  if (!state.credentials) return;
  ["baidu", "quark", "alipan"].forEach((provider) => {
    const element = $(`#${provider}-credential-state`);
    const configured = Boolean(state.credentials[provider]?.configured);
    element.textContent = configured ? "已配置" : "未配置";
    element.classList.toggle("is-configured", configured);
  });
}

function fillSettingsForm() {
  if (!state.settings) return;
  const form = $("#settings-form");
  Object.entries(state.settings).forEach(([key, value]) => {
    const input = form.elements.namedItem(key);
    if (!input) return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = String(value);
  });
}

function settingsPayload() {
  const form = $("#settings-form");
  return {
    download_dir: form.elements.download_dir.value.trim(),
    duplicate_policy: form.elements.duplicate_policy.value,
    reserve_space_gb: Number(form.elements.reserve_space_gb.value),
    total_speed_limit_mbps: Number(form.elements.total_speed_limit_mbps.value),
    connections_per_file: Number(form.elements.connections_per_file.value),
    concurrent_downloads: Number(form.elements.concurrent_downloads.value),
    schedule_enabled: form.elements.schedule_enabled.checked,
    schedule_start: form.elements.schedule_start.value,
    schedule_end: form.elements.schedule_end.value,
    log_level: form.elements.log_level.value,
    log_retention_days: Number(form.elements.log_retention_days.value),
    log_max_size_mb: Number(form.elements.log_max_size_mb.value),
    segment_size_mb: Number(form.elements.segment_size_mb?.value || 5),
    max_segment_requests: Number(form.elements.max_segment_requests?.value || 30),
    aria2_enabled: form.elements.aria2_enabled?.checked || false,
    aria2_rpc_url: form.elements.aria2_rpc_url?.value || "",
    aria2_secret: form.elements.aria2_secret?.value || "",
    baidu_app_id: Number(form.elements.baidu_app_id?.value || 250528),
    alipan_auth_mode: form.elements.alipan_auth_mode?.value || "refresh_token",
  };
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    state.settings = await api.saveSettings(settingsPayload());
    fillSettingsForm();
    toast("设置已保存并生效");
  } catch (error) {
    toast(error.message, true);
  }
}

function cookiePairs(raw) {
  const text = String(raw || "")
    .replace(/^\s*cookie\s*:\s*/i, "")
    .replace(/\r\n|\r|\n/g, ";");
  const seen = new Set();
  return text.split(";").flatMap((part) => {
    const item = part.trim();
    const separator = item.indexOf("=");
    if (separator < 1) return [];
    const name = item.slice(0, separator).trim();
    const value = item.slice(separator + 1).trim();
    if (!name || !value || seen.has(name)) return [];
    seen.add(name);
    return [[name, value]];
  });
}

function normalizeCookieInput(raw) {
  return cookiePairs(raw).map(([name, value]) => `${name}=${value}`).join("; ");
}

function extractBaiduCookie(raw) {
  const wanted = new Map(
    cookiePairs(raw)
      .filter(([name]) => ["BDUSS", "STOKEN"].includes(name.toUpperCase()))
      .map(([name, value]) => [name.toUpperCase(), value]),
  );
  return ["BDUSS", "STOKEN"]
    .filter((name) => wanted.has(name))
    .map((name) => `${name}=${wanted.get(name)}`)
    .join("; ");
}

function updateCredentialDetection(provider, commit = false) {
  const form = $("#settings-form");
  const input = form.elements[provider === "baidu" ? "baidu_cookie" : "quark_cookie"];
  const cleaned = provider === "baidu"
    ? extractBaiduCookie(input.value)
    : normalizeCookieInput(input.value);
  const count = cookiePairs(cleaned).length;
  const status = $(`#${provider}-cookie-detection`);
  if (provider === "baidu") {
    const names = cookiePairs(cleaned).map(([name]) => name);
    status.textContent = count
      ? `已识别：${names.join("、")}；只会保存这两个必要字段。`
      : "尚未识别到 BDUSS、STOKEN，请粘贴完整 Cookie。";
  } else {
    status.textContent = count
      ? `已识别并清理 ${count} 个有效字段，保存时会再次检查。`
      : "尚未识别到有效 Cookie 字段。";
  }
  if (commit && cleaned) input.value = cleaned;
}

async function saveCredential(provider) {
  const form = $("#settings-form");
  if (provider === "alipan") {
    const payload = {
      refresh_token: form.elements.alipan_refresh_token.value.trim(),
      client_id: form.elements.alipan_client_id?.value.trim() || "",
      client_secret: form.elements.alipan_client_secret?.value.trim() || "",
      device_id: form.elements.alipan_device_id?.value.trim() || "",
      signature: form.elements.alipan_signature?.value.trim() || "",
    };
    try {
      state.credentials = await api.saveCredential(provider, payload);
      form.elements.alipan_refresh_token.value = "";
      form.elements.alipan_client_secret.value = "";
      form.elements.alipan_signature.value = "";
      renderCredentialStates();
      toast("阿里云盘凭据已验证并安全保存");
      await loadProvider(provider);
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  const inputName = provider === "baidu" ? "baidu_cookie" : "quark_cookie";
  const cleaned = provider === "baidu"
    ? extractBaiduCookie(form.elements[inputName].value)
    : normalizeCookieInput(form.elements[inputName].value);
  form.elements[inputName].value = cleaned;
  const payload = {cookie: cleaned};
  try {
    state.credentials = await api.saveCredential(provider, payload);
    form.elements.baidu_cookie.value = "";
    form.elements.quark_cookie.value = "";
    renderCredentialStates();
    toast("凭据已验证并安全保存");
    await loadProvider(provider);
  } catch (error) {
    toast(error.message, true);
  }
}

function clearQrTimer() {
  if (state.qrTimer) window.clearTimeout(state.qrTimer);
  state.qrTimer = null;
}

async function closeQrDialog(cancelRemote = true) {
  clearQrTimer();
  const provider = state.qrProvider;
  const sessionId = state.qrSession;
  state.qrProvider = "";
  state.qrSession = "";
  $("#cloud-qr-dialog").classList.add("is-hidden");
  $("#cloud-qr-image").removeAttribute("src");
  if (cancelRemote && provider && sessionId) {
    try { await api.cancelQr(provider, sessionId); } catch (_) { /* session expires automatically */ }
  }
}

async function pollQr() {
  const provider = state.qrProvider;
  const sessionId = state.qrSession;
  if (!provider || !sessionId) return;
  try {
    const result = await api.qrStatus(provider, sessionId);
    if (provider !== state.qrProvider || sessionId !== state.qrSession) return;
    $("#cloud-qr-status").textContent = result.message || result.error || "\u7b49\u5f85\u626b\u7801";
    if (result.status === "confirmed") {
      await closeQrDialog(false);
      await Promise.all([loadSettings(), loadCredentials()]);
      fillSettingsForm();
      const names = {baidu: "\u767e\u5ea6\u7f51\u76d8", quark: "\u5938\u514b\u7f51\u76d8", alipan: "\u963f\u91cc\u4e91\u76d8"};
      toast(`${names[provider]}\u626b\u7801\u767b\u5f55\u6210\u529f${result.username ? `\uff1a${result.username}` : ""}`);
      await loadProvider(provider);
      return;
    }
    if (["expired", "cancelled"].includes(result.status)) {
      clearQrTimer();
      return;
    }
    state.qrTimer = window.setTimeout(pollQr, 2000);
  } catch (error) {
    clearQrTimer();
    $("#cloud-qr-status").textContent = error.message;
  }
}

async function startQr(provider = state.qrProvider) {
  await closeQrDialog();
  const names = {baidu: "\u767e\u5ea6\u7f51\u76d8", quark: "\u5938\u514b\u7f51\u76d8", alipan: "\u963f\u91cc\u4e91\u76d8"};
  state.qrProvider = provider;
  $("#cloud-qr-eyebrow").textContent = `${names[provider]}\u626b\u7801\u767b\u5f55`;
  $("#cloud-qr-title").textContent = `\u4f7f\u7528${names[provider]} App \u626b\u7801`;
  $("#cloud-qr-image").alt = `${names[provider]}\u767b\u5f55\u4e8c\u7ef4\u7801`;
  $("#cloud-qr-help").textContent = provider === "alipan"
    ? "\u626b\u7801\u540e\u8bf7\u5728\u624b\u673a\u4e2d\u786e\u8ba4\u3002\u672c\u65b9\u5f0f\u5c06\u4f7f\u7528\u7f51\u9875 refresh_token \u517c\u5bb9\u6a21\u5f0f\u3002"
    : "\u626b\u7801\u540e\u8bf7\u5728\u624b\u673a\u4e2d\u786e\u8ba4\uff0c\u767b\u5f55\u51ed\u636e\u53ea\u4fdd\u5b58\u5728 NAS \u672c\u5730\u3002";
  $("#cloud-qr-dialog").classList.remove("is-hidden");
  $("#cloud-qr-status").textContent = "\u6b63\u5728\u751f\u6210\u4e8c\u7ef4\u7801";
  try {
    const result = await api.startQr(provider);
    state.qrSession = result.session_id;
    $("#cloud-qr-image").src = provider === "alipan"
      ? `/api/alipan/qr/${encodeURIComponent(result.session_id)}.svg`
      : `/api/${provider}/qr/${encodeURIComponent(result.session_id)}/image`;
    $("#cloud-qr-status").textContent = "\u7b49\u5f85\u626b\u7801";
    state.qrTimer = window.setTimeout(pollQr, 1200);
  } catch (error) {
    $("#cloud-qr-status").textContent = error.message;
  }
}

async function clearCredential(provider) {
  const names = {baidu: "百度网盘", quark: "夸克网盘", alipan: "阿里云盘"};
  if (!window.confirm(`确定清除${names[provider] || provider}凭据？`)) return;
  try {
    state.credentials = await api.clearCredential(provider);
    renderCredentialStates();
    renderProviderStatus(provider, {logged_in: false, configured: false});
    showProviderEmpty(provider, "凭据已清除");
    toast("凭据已清除");
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshLogs() {
  try {
    const result = await api.logs(500);
    $("#log-viewer").textContent = result.content || "暂无日志。";
  } catch (error) { toast(error.message, true); }
}

async function runDiagnostics() {
  const target = $("#diagnostic-results");
  target.textContent = "正在检查…";
  try {
    const result = await api.diagnostics();
    target.replaceChildren();
    try {
      const ax = await api.aria2Status();
      const r = document.createElement("div"); r.className = "diagnostic-row";
      const l = document.createElement("span"); l.textContent = "Aria2 服务";
      const s = document.createElement("strong");
      s.className = ax.online ? "ok" : "fail";
      const reason = ax.error ? `\uFF1A${String(ax.error).slice(0, 160)}` : "";
      s.textContent = ax.online ? "\u5728\u7EBF" : (ax.configured ? `\u672A\u8FDE\u63A5${reason}` : "\u672A\u914D\u7F6E");
      r.append(l, s); target.append(r);
    } catch (e) {}
    Object.entries(result).forEach(([name, value]) => {
      const row = document.createElement("div");
      row.className = "diagnostic-row";
      const label = document.createElement("span");
      label.textContent = {config_directory: "配置目录", download_directory: "下载目录", aria2: "外部 Aria2", network: "网络"}[name] || name;
      const status = document.createElement("strong");
      status.className = value.ok ? "ok" : "fail";
      status.textContent = value.ok ? "正常" : `异常${value.error ? `：${value.error}` : ""}`;
      row.append(label, status);
      target.append(row);
    });
  } catch (error) {
    target.textContent = "";
    toast(error.message, true);
  }
}

function bindEvents() {
  $$(".nav-item[data-view]").forEach((button) => button.addEventListener("click", () => activateView(button.dataset.view)));
  $$("[data-settings-tab]").forEach((button) => {
    button.addEventListener("click", () => activateSettingsTab(button.dataset.settingsTab));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const tabs = $$("[data-settings-tab]");
      const current = tabs.indexOf(button);
      const offset = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(current + offset + tabs.length) % tabs.length];
      activateSettingsTab(next.dataset.settingsTab);
      next.focus();
    });
  });
  $$("[data-theme-toggle], #mobile-theme").forEach((button) => button.addEventListener("click", cycleTheme));
  $("#skip-startup").addEventListener("click", finishStartup);
  $("#guide-next").addEventListener("click", () => { state.guideStep = Math.min(5, state.guideStep + 1); renderGuideStep(); });
  $("#guide-back").addEventListener("click", () => { state.guideStep = Math.max(1, state.guideStep - 1); renderGuideStep(); });
  $("#disclaimer-accept").addEventListener("change", (event) => { $("#guide-finish").disabled = !event.target.checked; });
  $("#guide-finish").addEventListener("click", finishGuide);
  $("#baidu-guide-later").addEventListener("click", () => {
    state.baiduGuideDeferred = true;
    $("#baidu-guide").classList.add("is-hidden");
  });
  $("#baidu-guide-complete").addEventListener("click", () => completeBaiduGuide(true));
  ["local", "baidu", "quark", "alipan"].forEach((provider) => {
    $(`#${provider}-refresh`).addEventListener("click", () => loadFiles(provider, state.paths[provider]));
    $(`#${provider}-search`).addEventListener("keydown", (event) => { if (event.key === "Enter") searchFiles(provider); });
    if (provider !== "local") $(`#${provider}-account`).addEventListener("click", () => activateView("settings"));
  });
  $("#rename-form").addEventListener("submit", submitRename);
  $("#rename-cancel").addEventListener("click", closeRenameDialog);
  $("#rename-dialog").addEventListener("click", (event) => {
    if (event.target.id === "rename-dialog") closeRenameDialog();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#rename-dialog").classList.contains("is-hidden")) closeRenameDialog();
    if (event.key === "Escape" && !$("#cloud-qr-dialog").classList.contains("is-hidden")) closeQrDialog();
  });
  $("#clear-downloads").addEventListener("click", async () => { await api.clearDownloads(); await refreshDownloads(); });
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#cancel-settings").addEventListener("click", fillSettingsForm);
  $("#save-baidu-credential").addEventListener("click", () => saveCredential("baidu"));
  $("#save-quark-credential").addEventListener("click", () => saveCredential("quark"));
  $("#save-alipan-credential").addEventListener("click", () => saveCredential("alipan"));
  ["baidu", "quark", "alipan"].forEach((provider) => {
    $(`#${provider}-qr-login`).addEventListener("click", () => startQr(provider));
  });
  $("#cloud-qr-refresh").addEventListener("click", () => startQr());
  $("#cloud-qr-cancel").addEventListener("click", () => closeQrDialog());
  $("#cloud-qr-dialog").addEventListener("click", (event) => {
    if (event.target.id === "cloud-qr-dialog") closeQrDialog();
  });
  [["baidu", "baidu_cookie"], ["quark", "quark_cookie"]].forEach(([provider, name]) => {
    const input = $("#settings-form").elements[name];
    input.addEventListener("input", () => updateCredentialDetection(provider));
    input.addEventListener("blur", () => updateCredentialDetection(provider, true));
  });
  $("#clear-baidu-credential").addEventListener("click", () => clearCredential("baidu"));
  $("#clear-quark-credential").addEventListener("click", () => clearCredential("quark"));
  $("#clear-alipan-credential").addEventListener("click", () => clearCredential("alipan"));
  $("#refresh-logs").addEventListener("click", refreshLogs);
  $("#download-logs").addEventListener("click", () => downloadFile("/api/logs/download"));
  $("#clear-logs").addEventListener("click", async () => {
    if (!window.confirm("确定清空全部应用日志？")) return;
    await api.clearLogs();
    $("#log-viewer").textContent = "日志已清空。";
  });
  $("#run-diagnostics").addEventListener("click", runDiagnostics);
  $("#export-diagnostics").addEventListener("click", () => downloadFile("/api/diagnostics/export"));
}

async function boot() {
  const savedTheme = localStorage.getItem("clouddl:theme");
  if (savedTheme) setTheme(savedTheme);
  bindEvents();
  const preloadPromise = preload().catch((error) => toast(error.message, true));
  startStartup();
  await preloadPromise;
  if (!state.onboarding?.required) await activateView("baidu");
  state.downloadTimer = window.setInterval(() => {
    refreshDownloads();
  }, 1000);
}

boot();
