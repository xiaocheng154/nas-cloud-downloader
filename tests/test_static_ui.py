from __future__ import annotations

import re
import unittest
from pathlib import Path


STATIC = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "service"
    / "src"
    / "static"
)


class StaticUiTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (STATIC / relative).read_text(encoding="utf-8")

    def test_visible_brand_uses_multicloud_downloader_name(self) -> None:
        html = self.read("index.html")
        script = self.read("app.js")
        self.assertIn("<title>多网盘下载器</title>", html)
        self.assertIn("多网盘下载器", html)
        self.assertIn("欢迎使用 多网盘下载器", script)

    def test_assets_are_local_and_split_by_responsibility(self) -> None:
        html = self.read("index.html")
        for asset in ("styles.css", "api.js", "app.js", "assets/logo.svg"):
            self.assertTrue((STATIC / asset).exists(), asset)
        self.assertIn('/static/styles.css', html)
        self.assertIn('/static/app.js', html)
        self.assertNotRegex(html, r'https?://')
        self.assertNotRegex(html, r'\son\w+\s*=')

    def test_navigation_matches_approved_layout(self) -> None:
        html = self.read("index.html")
        for view, label in (
            ("baidu", "百度网盘"),
            ("quark", "夸克网盘"),
            ("downloads", "下载任务"),
            ("settings", "设置"),
        ):
            self.assertIn(f'data-view="{view}"', html)
            self.assertIn(label, html)
            self.assertRegex(
                html,
                rf'data-view="{view}"[^>]+aria-label="{label}"',
            )
        css = self.read("styles.css").lower()
        self.assertIn("--sidebar-width: 220px", css)
        self.assertIn("#f7f7f5", css)
        self.assertIn("#3568d4", css)
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn("max-width: 700px", css)

    def test_file_manager_has_local_remote_rename_and_generated_icons(self) -> None:
        html = self.read("index.html")
        script = self.read("app.js")
        api = self.read("api.js")
        css = self.read("styles.css")
        self.assertIn('data-view="local"', html)
        self.assertIn('id="rename-dialog"', html)
        self.assertIn("openRenameDialog", script)
        self.assertIn("thumbnailUrl", script)
        self.assertIn("/rename", api)
        self.assertIn("/thumbnail", api)
        self.assertIn("file-manager-icons.png", css)
        self.assertTrue((STATIC / "assets" / "file-manager-icons.png").is_file())
        self.assertNotIn("▰", script)
        self.assertNotIn("▧", script)

    def test_all_cloud_qr_logins_are_integrated(self) -> None:
        html = self.read("index.html")
        script = self.read("app.js")
        api = self.read("api.js")
        for provider in ("baidu", "quark", "alipan"):
            self.assertIn(f'id="{provider}-qr-login"', html)
        self.assertIn('id="cloud-qr-dialog"', html)
        self.assertIn("startQr", script)
        self.assertIn("pollQr", script)
        self.assertIn("status.error ||", script)
        self.assertIn("/qr/start", api)
        self.assertIn("/status", api)

    def test_download_cards_expose_resume_and_url_refresh_diagnostics(self) -> None:
        script = self.read("app.js")
        self.assertIn("resumed_bytes", script)
        self.assertIn("url_refresh_count", script)
        self.assertIn("resume_available", script)

    def test_settings_contains_every_required_control(self) -> None:
        html = self.read("index.html")
        control_names = (
            "baidu_cookie",
            "quark_cookie",
            "download_dir",
            "duplicate_policy",
            "reserve_space_gb",
            "total_speed_limit_mbps",
            "connections_per_file",
            "segment_size_mb",
            "max_segment_requests",
            "aria2_enabled",
            "aria2_rpc_url",
            "aria2_secret",
            "baidu_app_id",
            "schedule_enabled",
            "schedule_start",
            "schedule_end",
            "concurrent_downloads",
            "log_level",
            "log_retention_days",
            "log_max_size_mb",
        )
        for name in control_names:
            self.assertRegex(html, rf'name="{name}"')
        self.assertNotRegex(html, r'name="bduss"')
        self.assertNotRegex(html, r'name="stoken"')
        for label in (
            "下载保存目录",
            "同名文件",
            "磁盘保留空间",
            "总下载限速",
            "单文件连接数",
            "仅在指定时段下载",
            "下载并发数",
            "日志与诊断",
            "\u5185\u7f6e Aria2 \u52a0\u901f",
            "\u542f\u7528\u5185\u7f6e Aria2",
            "RPC 地址",
            "RPC 密钥",
            "百度 app_id",
            "分片大小",
            "全局分片请求上限",
            "保存",
            "取消",
        ):
            self.assertIn(label, html)

    def test_settings_are_split_into_top_level_tabs(self) -> None:
        html = self.read("index.html")
        script = self.read("app.js")
        css = self.read("styles.css")
        self.assertIn('class="settings-tabs"', html)
        self.assertIn('role="tablist"', html)
        for tab, label in (
            ("credentials", "账号与凭据"),
            ("downloads", "下载策略"),
            ("diagnostics", "日志与诊断"),
        ):
            self.assertRegex(
                html,
                rf'data-settings-tab="{tab}"[^>]+role="tab"[^>]*>{label}<',
            )
            self.assertIn(f'data-settings-panel="{tab}"', html)
        self.assertIn("function activateSettingsTab(tab)", script)
        self.assertIn('setAttribute("aria-selected"', script)
        self.assertIn("overflow-x: auto", css)

    def test_first_use_guide_has_mandatory_disclaimer(self) -> None:
        html = self.read("index.html")
        self.assertEqual(len(re.findall(r'data-guide-step="\d"', html)), 5)
        self.assertIn('id="disclaimer-accept"', html)
        self.assertIn("我已阅读并同意", html)
        self.assertIn('id="guide-finish"', html)
        self.assertIn("disabled", html)
        app_script = self.read("app.js")
        api_script = self.read("api.js")
        self.assertIn("/api/onboarding/status", api_script)
        self.assertIn("/api/onboarding/accept", api_script)
        self.assertIn("api.onboardingStatus()", app_script)
        self.assertIn("api.acceptOnboarding(", app_script)

    def test_onboarding_still_opens_when_preload_finishes_after_intro(self) -> None:
        script = self.read("app.js")
        self.assertIn("startupFinished: false", script)
        self.assertIn("state.startupFinished = true", script)
        self.assertRegex(
            script,
            r"async function preload\(\)[\s\S]+"
            r"state\.startupFinished[\s\S]+showOnboardingIfNeeded\(\)",
        )

    def test_startup_animation_is_session_scoped_and_accessible(self) -> None:
        html = self.read("index.html")
        css = self.read("styles.css")
        script = self.read("app.js")
        self.assertIn('id="startup"', html)
        self.assertIn('id="skip-startup"', html)
        self.assertIn("clouddl:intro-seen", script)
        self.assertIn("sessionStorage", script)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("prefers-reduced-motion", script)

    def test_startup_animation_runs_for_three_and_half_seconds(self) -> None:
        html = self.read("index.html")
        css = self.read("styles.css")
        script = self.read("app.js")
        self.assertIn("const STARTUP_DURATION_MS = 3500", script)
        self.assertIn(
            "window.setTimeout(finishStartup, STARTUP_DURATION_MS)",
            script,
        )
        self.assertIn("2.85s", css)
        self.assertIn('id="skip-startup"', html)

    def test_theme_transition_expands_from_clicked_button(self) -> None:
        script = self.read("app.js")
        css = self.read("styles.css")
        self.assertIn("document.startViewTransition", script)
        self.assertIn(
            "event.currentTarget.getBoundingClientRect()",
            script,
        )
        self.assertIn("--theme-transition-x", script)
        self.assertIn("--theme-transition-y", script)
        self.assertIn("::view-transition-new(root)", css)
        self.assertIn("clip-path", css)
        self.assertIn("prefers-reduced-motion: reduce", css)

    def test_credentials_page_has_acquisition_guides(self) -> None:
        html = self.read("index.html")
        css = self.read("styles.css")
        for text in (
            "如何获取百度凭据",
            "如何获取夸克 Cookie",
            "Request Headers",
            "Network",
            "可以直接粘贴带",
            "退出账号会话并重新登录",
        ):
            self.assertIn(text, html)
        self.assertGreaterEqual(
            html.count('class="credential-help"'),
            2,
        )
        self.assertIn("#settings-form { padding-bottom: 84px; }", css)

    def test_credential_inputs_explain_and_apply_automatic_cookie_cleanup(self) -> None:
        html = self.read("index.html")
        script = self.read("app.js")

        self.assertIn('name="baidu_cookie"', html)
        self.assertIn("自动提取并只保存 BDUSS、STOKEN", html)
        self.assertIn("自动去除 Cookie: 前缀、换行、空项、格式错误项和重复字段", html)
        self.assertIn("function normalizeCookieInput(raw)", script)
        self.assertIn("function extractBaiduCookie(raw)", script)
        self.assertIn('addEventListener("input"', script)
        self.assertIn('addEventListener("blur"', script)

    def test_api_module_uses_required_endpoints(self) -> None:
        script = self.read("api.js")
        for endpoint in (
            "/api/settings",
            "/api/credentials",
            "/api/downloads",
            "/api/logs",
            "/api/diagnostics",
        ):
            self.assertIn(endpoint, script)

    def test_download_cards_show_speed_connections_and_eta(self) -> None:
        script = self.read("app.js")
        self.assertIn("task.connections", script)
        self.assertIn("task.eta_seconds", script)
        self.assertIn("预计剩余", script)
        self.assertIn("task.source_profile", script)
        self.assertIn("夸克 PC 身份", script)
        self.assertIn("网页回退", script)

    def test_folder_download_is_available_for_both_providers(self) -> None:
        script = self.read("app.js")
        api_script = self.read("api.js")
        self.assertIn("file.is_dir", script)
        self.assertIn("downloadFolder", script)
        self.assertIn("/download-folder", api_script)
        self.assertIn("下载文件夹", script)

    def test_baidu_file_download_sends_remote_path(self) -> None:
        script = self.read("app.js")
        api_script = self.read("api.js")
        self.assertIn("api.download(provider, id, file.path)", script)
        self.assertIn("{path}", api_script)

    def test_baidu_credential_guide_is_server_persisted(self) -> None:
        html = self.read("index.html")
        script = self.read("app.js")
        api_script = self.read("api.js")
        self.assertIn('id="baidu-guide"', html)
        self.assertIn("如何配置 BDUSS 和 STOKEN", html)
        self.assertIn("Network", html)
        self.assertIn("/api/guides/baidu/status", api_script)
        self.assertIn("/api/guides/baidu/complete", api_script)
        self.assertIn("api.baiduGuideStatus()", script)
        self.assertIn("api.completeBaiduGuide()", script)
        self.assertIn("前往设置并不再提示", html)
        self.assertIn("完成后以后不再自动提示", html)

    def test_download_location_and_bug_contact_are_visible(self) -> None:
        html = self.read("index.html")
        script = self.read("app.js")
        self.assertIn('id="download-directory"', html)
        self.assertIn("2556574539@qq.com", html)
        self.assertIn('href="mailto:2556574539@qq.com"', html)
        self.assertIn("task.save_path", script)
        self.assertIn("result.download_directory", script)

    def test_speed_limit_and_optional_aria2_are_explained(self) -> None:
        html = self.read("index.html")
        self.assertIn("0.1 MB/s 约等于 102 KB/s", html)
        self.assertIn("\u5b89\u88c5\u5305\u5df2\u5185\u7f6e Aria2", html)

    def test_logo_is_flat_theme_aware_svg(self) -> None:
        logo = self.read("assets/logo.svg")
        self.assertIn("<svg", logo)
        self.assertIn("currentColor", logo)
        self.assertIn("#3568D4", logo)
        self.assertNotIn("linearGradient", logo)
        self.assertNotIn("filter", logo)
