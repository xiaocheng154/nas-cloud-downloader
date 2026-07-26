# Cookie Guidance and Motion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复夸克 Cookie 验证与接口兼容问题，加入百度/夸克凭据获取指引，实现从主题按钮扩散的主题过渡，并将首次启动动画完整时长调整为 5 秒。

**Architecture:** 夸克客户端集中封装请求参数、非 JSON 响应处理和当前网页端接口；FastAPI 凭据替换流程继续保证“先验证、后保存”。前端只使用原生 HTML、CSS 和 JavaScript：主题切换优先使用 View Transitions API，凭据指引使用语义化 `details`，启动动画沿用现有会话级控制。

**Tech Stack:** Python 3.11、httpx、FastAPI、unittest、原生 HTML/CSS/JavaScript、CSS View Transitions、fnpack。

## Global Constraints

- 目标系统为 x86_64 fnOS 1.1.3107，应用不得依赖 Docker。
- 不加入迅雷、BT、磁力链或 `.torrent` 支持。
- 不改变用户确认的 Logo、侧栏结构和少颜色视觉系统。
- Cookie、BDUSS、STOKEN 不得出现在 API 响应、日志或诊断包中。
- 主题扩散动画从实际点击按钮中心开始，约 450ms；减少动态效果时立即切换。
- 启动动画完整播放时长必须为 5000ms，保留跳过和会话内只播放一次。

---

### Task 1: 夸克客户端响应解析和当前接口

**Files:**
- Create: `tests/test_quark.py`
- Modify: `app/service/src/quark.py`

**Interfaces:**
- Produces: `QuarkPanClient.verify_login() -> dict`
- Produces: `QuarkPanClient.list_files(path="/", page=1, page_size=100) -> dict`
- Produces: `QuarkPanClient.get_download_url(fid: str) -> dict`
- Produces: `QuarkPanClient.search_files(keyword: str, page=1) -> dict`
- Produces: `QuarkPanClient._json_response(response, action: str) -> tuple[dict | None, str | None]`

- [ ] **Step 1: 写入失败测试**

```python
class QuarkClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_login_uses_account_endpoint_and_accepts_current_payload(self):
        response = httpx.Response(
            200,
            json={"status": 200, "data": {"nickname": "测试账号"}},
            request=httpx.Request("GET", "https://pan.quark.cn/account/info"),
        )
        client = QuarkPanClient("k=v")
        client._client = AsyncMock()
        client._client.get.return_value = response
        result = await client.verify_login()
        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "测试账号")
        self.assertEqual(
            str(client._client.get.await_args.args[0]),
            "https://pan.quark.cn/account/info",
        )

    async def test_verify_login_translates_empty_response(self):
        response = httpx.Response(
            200,
            text="",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://pan.quark.cn/account/info"),
        )
        client = QuarkPanClient("k=v")
        client._client = AsyncMock()
        client._client.get.return_value = response
        result = await client.verify_login()
        self.assertFalse(result["success"])
        self.assertIn("未返回 JSON", result["error"])
        self.assertNotIn("Expecting value", result["error"])

    async def test_verify_login_translates_forbidden_response(self):
        response = httpx.Response(
            403,
            text="<html>forbidden</html>",
            request=httpx.Request("GET", "https://pan.quark.cn/account/info"),
        )
        client = QuarkPanClient("k=v")
        client._client = AsyncMock()
        client._client.get.return_value = response
        result = await client.verify_login()
        self.assertEqual(result["error"], "夸克拒绝了验证请求，请重新获取 Cookie 后再试")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_quark -v`

Expected: FAIL，旧代码仍请求 `/api/user/info`，并将空响应暴露为 `Expecting value`。

- [ ] **Step 3: 实现统一解析和接口参数**

```python
ACCOUNT_URL = "https://pan.quark.cn/account/info"
DRIVE_API = "https://drive-pc.quark.cn/1/clouddrive"
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}

@staticmethod
def _json_response(response: httpx.Response, action: str):
    if response.status_code in {401, 403}:
        return None, f"夸克拒绝了{action}请求，请重新获取 Cookie 后再试"
    if response.status_code >= 400:
        return None, f"{action}失败：HTTP {response.status_code}"
    content_type = response.headers.get("content-type", "").lower()
    if not response.content or "json" not in content_type:
        return None, f"{action}接口未返回 JSON，可能遇到登录失效或网页风控"
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None, f"{action}接口返回了无法解析的数据，请稍后重试"
    if not isinstance(payload, dict):
        return None, f"{action}接口返回格式异常"
    return payload, None
```

将列表、搜索和下载端点分别改为 `file/sort`、`file/search` 和 `file/download`，公共参数使用 `pr=ucpro&fr=pc&uc_param_str=`；下载接口改为 POST JSON `{"fids": [fid]}`。

- [ ] **Step 4: 运行夸克测试并确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_quark -v`

Expected: PASS，且错误消息中不存在 Python JSON 解码器原文。

### Task 2: 验证失败不保存凭据

**Files:**
- Modify: `tests/test_api.py`
- Verify: `app/service/src/app.py`

**Interfaces:**
- Consumes: `QuarkPanClient.verify_login() -> {"success": bool, ...}`
- Verifies: `PUT /api/credentials/quark`

- [ ] **Step 1: 写入 API 失败测试**

```python
def test_invalid_quark_cookie_is_not_persisted(self):
    self.accept_disclaimer()
    with patch.object(
        self.module.QuarkPanClient,
        "verify_login",
        new=AsyncMock(return_value={"success": False, "error": "验证失败"}),
    ):
        response = self.client.put(
            "/api/credentials/quark",
            json={"cookie": "invalid-cookie"},
        )
    self.assertEqual(response.status_code, 400)
    self.assertFalse(
        self.client.get("/api/credentials").json()["quark"]["configured"]
    )
    self.assertNotIn("invalid-cookie", response.text)
```

- [ ] **Step 2: 运行测试确认现有事务语义**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_api.ApiTests.test_invalid_quark_cookie_is_not_persisted -v`

Expected: PASS；若失败，只修改 `_replace_quark`，确保 `credential_store.update()` 位于成功验证之后。

- [ ] **Step 3: 增加 Cookie 输入规范化测试**

```python
async def test_cookie_prefix_is_removed_before_request(self):
    client = QuarkPanClient("Cookie: __uid=abc; __pus=def")
    self.assertEqual(client.cookie, "__uid=abc; __pus=def")
```

- [ ] **Step 4: 实现最小规范化并验证**

```python
value = cookie.strip()
if value[:7].lower() == "cookie:":
    value = value[7:].strip()
self.cookie = value
```

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_quark tests.test_api -v`

Expected: PASS。

### Task 3: 百度与夸克凭据获取指引

**Files:**
- Modify: `tests/test_static_ui.py`
- Modify: `app/service/src/static/index.html`
- Modify: `app/service/src/static/styles.css`

**Interfaces:**
- Produces: `details.credential-help` 语义化折叠指引
- Produces: 百度步骤、夸克步骤和凭据泄露警告

- [ ] **Step 1: 写入静态 UI 失败测试**

```python
def test_credentials_page_has_acquisition_guides(self):
    html = INDEX.read_text(encoding="utf-8")
    for text in (
        "如何获取百度凭据",
        "如何获取夸克 Cookie",
        "Request Headers",
        "Network",
        "不要复制开头的 Cookie:",
        "退出账号会话并重新登录",
    ):
        self.assertIn(text, html)
    self.assertGreaterEqual(html.count('class="credential-help"'), 2)
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_static_ui.StaticUiTests.test_credentials_page_has_acquisition_guides -v`

Expected: FAIL，现有凭据卡片没有获取步骤。

- [ ] **Step 3: 加入语义化指引**

```html
<details class="credential-help">
  <summary>如何获取夸克 Cookie</summary>
  <ol>
    <li>登录 <code>https://pan.quark.cn</code>。</li>
    <li>按 F12，进入 <strong>Network</strong>，然后刷新页面。</li>
    <li>选择 pan.quark.cn 或 drive-pc.quark.cn 请求。</li>
    <li>在 <strong>Request Headers</strong> 中复制完整 Cookie 值。</li>
    <li>不要复制开头的 Cookie: 字样。</li>
  </ol>
</details>
```

百度卡片加入对应的 BDUSS/STOKEN 提取步骤；两张卡片下方加入泄露后“退出账号会话并重新登录”的警告。CSS 沿用现有边框、表面色和一个蓝色强调色。

- [ ] **Step 4: 运行静态测试确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_static_ui -v`

Expected: PASS。

### Task 4: 从主题按钮扩散的主题切换

**Files:**
- Modify: `tests/test_static_ui.py`
- Modify: `app/service/src/static/app.js`
- Modify: `app/service/src/static/styles.css`

**Interfaces:**
- Produces: `cycleTheme(event: MouseEvent) -> void`
- Produces: `applyNextTheme() -> void`

- [ ] **Step 1: 写入失败测试**

```python
def test_theme_transition_expands_from_clicked_button(self):
    script = SCRIPT.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")
    self.assertIn("document.startViewTransition", script)
    self.assertIn("event.currentTarget.getBoundingClientRect()", script)
    self.assertIn("--theme-transition-x", script)
    self.assertIn("--theme-transition-y", script)
    self.assertIn("prefers-reduced-motion: reduce", css)
    self.assertIn("::view-transition-new(root)", css)
    self.assertIn("clip-path", css)
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_static_ui.StaticUiTests.test_theme_transition_expands_from_clicked_button -v`

Expected: FAIL，当前 `cycleTheme()` 立即切换主题。

- [ ] **Step 3: 实现扩散动画**

```javascript
function cycleTheme(event) {
  const apply = () => {
    const current = localStorage.getItem("clouddl:theme") || "auto";
    setTheme(current === "auto" ? "light" : current === "light" ? "dark" : "auto");
  };
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || !document.startViewTransition) {
    apply();
    return;
  }
  const rect = event.currentTarget.getBoundingClientRect();
  document.documentElement.style.setProperty("--theme-transition-x", `${rect.left + rect.width / 2}px`);
  document.documentElement.style.setProperty("--theme-transition-y", `${rect.top + rect.height / 2}px`);
  document.startViewTransition(apply);
}
```

CSS 的新主题快照使用圆形 `clip-path` 从 `0` 扩散到足以覆盖视口的半径，时长 `450ms`，动画层禁用指针事件。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_static_ui -v`

Expected: PASS。

### Task 5: 五秒启动动画

**Files:**
- Modify: `tests/test_static_ui.py`
- Modify: `app/service/src/static/app.js`
- Modify: `app/service/src/static/styles.css`

**Interfaces:**
- Produces: `STARTUP_DURATION_MS = 5000`
- Preserves: `finishStartup()`、跳过按钮、`clouddl:intro-seen`

- [ ] **Step 1: 写入失败测试**

```python
def test_startup_animation_runs_for_five_seconds(self):
    script = SCRIPT.read_text(encoding="utf-8")
    css = STYLES.read_text(encoding="utf-8")
    self.assertIn("const STARTUP_DURATION_MS = 5000", script)
    self.assertIn("window.setTimeout(finishStartup, STARTUP_DURATION_MS)", script)
    self.assertIn("4.25s", css)
    self.assertIn('id="skip-startup"', self.html)
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_static_ui.StaticUiTests.test_startup_animation_runs_for_five_seconds -v`

Expected: FAIL，当前完整时长为 3000ms。

- [ ] **Step 3: 实现五秒节奏**

```javascript
const STARTUP_DURATION_MS = 5000;
// startStartup 中：
window.setTimeout(finishStartup, STARTUP_DURATION_MS);
```

将云轮廓、箭头、名称和说明的延迟分布到 0.4–4.25 秒；保留 `prefers-reduced-motion`、跳过按钮和会话内跳过逻辑。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_static_ui -v`

Expected: PASS。

### Task 6: 回归验证和重新打包

**Files:**
- Modify: `manifest`
- Modify: `APP_VERSION`
- Generate: `clouddl_x86.fpk`

**Interfaces:**
- Produces: fnOS x86_64 原生 FPK

- [ ] **Step 1: 将补丁版本更新为 1.2.1**

```text
manifest: version = 1.2.1
APP_VERSION: 1.2.1
```

- [ ] **Step 2: 运行全部自动化测试**

Run: `.\.venv\Scripts\python.exe -m unittest discover -v`

Expected: 所有测试 PASS，无 traceback。

- [ ] **Step 3: 使用官方 fnpack 重新生成安装包**

Run: `.\.venv\Scripts\python.exe build_fpk.py`

Expected: `Packing successfully.` 并生成 `clouddl_x86.fpk`。

- [ ] **Step 4: 检查安装包**

检查 manifest 版本为 1.2.1、无 Docker 资源、内置 Python 为 ELF64 x86_64、修改后的 `quark.py/index.html/app.js/styles.css` 均位于 `app.tgz`。

- [ ] **Step 5: 记录交付哈希**

Run: `Get-FileHash .\clouddl_x86.fpk -Algorithm SHA256`

Expected: 输出新安装包的 SHA-256；交付时明确说明仍需在 fnOS 1.1.3107 上实机验证夸克当前接口和动画效果。
