const jsonHeaders = {"Content-Type": "application/json"};

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = payload?.detail || payload?.error || payload || `请求失败（${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    error.code = payload?.code;
    throw error;
  }
  return payload;
}

function query(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  });
  return `${url.pathname}${url.search}`;
}

export const api = {
  onboardingStatus: () => request("/api/onboarding/status"),
  acceptOnboarding: (version) => request("/api/onboarding/accept", {
    method: "POST", headers: jsonHeaders, body: JSON.stringify({version, accepted: true})
  }),
  baiduGuideStatus: () => request("/api/guides/baidu/status"),
  completeBaiduGuide: () => request("/api/guides/baidu/complete", {method: "POST"}),
  settings: () => request("/api/settings"),
  saveSettings: (data) => request("/api/settings", {
    method: "PUT", headers: jsonHeaders, body: JSON.stringify(data)
  }),
  credentials: () => request("/api/credentials"),
  saveCredential: (provider, data) => request(`/api/credentials/${provider}`, {
    method: "PUT", headers: jsonHeaders, body: JSON.stringify(data)
  }),
  clearCredential: (provider) => request(`/api/credentials/${provider}`, {method: "DELETE"}),
  providerStatus: (provider) => request(`/api/${provider}/status`),
  startAlipanQr: () => request("/api/alipan/qr/start", {method: "POST"}),
  alipanQrStatus: (sessionId) => request(`/api/alipan/qr/${encodeURIComponent(sessionId)}/status`),
  cancelAlipanQr: (sessionId) => request(`/api/alipan/qr/${encodeURIComponent(sessionId)}`, {method: "DELETE"}),
  files: (provider, path) => request(query(`/api/${provider}/list`, {path})),
  search: (provider, keyword, path) => request(query(`/api/${provider}/search`, {keyword, path})),
  rename: (provider, payload) => request(`/api/${provider}/rename`, {
    method: "POST", headers: jsonHeaders, body: JSON.stringify(payload)
  }),
  download: (provider, id, path = "") => request(
    query(`/api/${provider}/download/${encodeURIComponent(id)}`, {path}),
    {method: "POST"},
  ),
  downloadFolder: (provider, payload) => request(`/api/${provider}/download-folder`, {
    method: "POST", headers: jsonHeaders, body: JSON.stringify(payload)
  }),
  downloads: () => request("/api/downloads"),
  cancelDownload: (id) => request(`/api/downloads/${encodeURIComponent(id)}`, {method: "DELETE"}),
  clearDownloads: () => request("/api/downloads/clear", {method: "POST"}),
  logs: (lines = 300) => request(query("/api/logs", {lines})),
  clearLogs: () => request("/api/logs", {method: "DELETE"}),
  diagnostics: () => request("/api/diagnostics", {method: "POST"}),
  aria2Status: () => request("/api/aria2/status"),
};

export function thumbnailUrl(provider, key) {
  return query(`/api/${provider}/thumbnail`, {
    [provider === "local" ? "path" : "key"]: key,
  });
}

export function downloadFile(path) {
  const anchor = document.createElement("a");
  anchor.href = path;
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}
