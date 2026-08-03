from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.config_dir = root / "config"
        self.download_dir = root / "downloads"
        self.environment = patch.dict(
            os.environ,
            {
                "CONFIG_DIR": str(self.config_dir),
                "DOWNLOAD_DIR": str(self.download_dir),
            },
        )
        self.environment.start()
        import app

        self.module = importlib.reload(app)
        self.client_context = TestClient(self.module.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.tempdir.cleanup()

    def accept_disclaimer(self) -> None:
        status = self.client.get("/api/onboarding/status").json()
        response = self.client.post(
            "/api/onboarding/accept",
            json={"version": status["version"], "accepted": True},
        )
        self.assertEqual(response.status_code, 200)

    def test_business_api_is_blocked_until_disclaimer_is_accepted(self) -> None:
        self.assertEqual(self.client.get("/api/settings").status_code, 403)
        status = self.client.get("/api/onboarding/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["required"])
        self.accept_disclaimer()
        self.assertEqual(self.client.get("/api/settings").status_code, 200)

    def test_settings_round_trip_and_validation(self) -> None:
        self.accept_disclaimer()
        defaults = self.client.get("/api/settings").json()
        self.assertEqual(defaults["reserve_space_gb"], 50)
        updated = self.client.put(
            "/api/settings",
            json={"concurrent_downloads": 5, "total_speed_limit_mbps": 12.5},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["concurrent_downloads"], 5)
        invalid = self.client.put(
            "/api/settings",
            json={"connections_per_file": 0},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.client.get("/api/settings").json()["connections_per_file"], 16)

    def test_aria2_secret_is_write_only_and_blank_update_preserves_it(self) -> None:
        self.accept_disclaimer()
        saved = self.client.put(
            "/api/settings",
            json={
                "aria2_enabled": True,
                "aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc",
                "aria2_secret": "rpc-top-secret",
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn("rpc-top-secret", saved.text)
        self.assertEqual(saved.json()["aria2_secret"], "")
        self.assertTrue(saved.json()["aria2_secret_configured"])

        unchanged = self.client.put(
            "/api/settings",
            json={"aria2_secret": "", "concurrent_downloads": 6},
        )
        self.assertEqual(unchanged.status_code, 200)
        self.assertNotIn("rpc-top-secret", unchanged.text)
        self.assertEqual(
            self.module.settings_store.load().aria2_secret,
            "rpc-top-secret",
        )

    def test_aria2_rpc_url_must_be_a_local_http_endpoint(self) -> None:
        self.accept_disclaimer()
        for value in (
            "file:///tmp/aria2.sock",
            "http://example.com:6800/jsonrpc",
            "not-a-url",
        ):
            response = self.client.put(
                "/api/settings",
                json={"aria2_rpc_url": value},
            )
            self.assertEqual(response.status_code, 422, value)

    def test_configured_baidu_app_id_is_used_by_new_clients(self) -> None:
        self.accept_disclaimer()
        response = self.client.put(
            "/api/settings",
            json={"baidu_app_id": 309847},
        )
        self.assertEqual(response.status_code, 200)
        candidate = self.module._new_baidu_client()
        self.assertEqual(candidate.app_id, 309847)

    def test_quark_download_passes_cookie_to_download_engine(self) -> None:
        self.accept_disclaimer()
        self.module.quark_client.cookie = "__uid=secret-cookie"
        self.module.quark_client.download_headers = Mock(
            return_value={
                "User-Agent": "signed-link-agent",
                "Cookie": "__uid=secret-cookie",
            }
        )
        self.module.quark_client.get_download_url = AsyncMock(
            return_value={
                "success": True,
                "url": "https://download.example/file",
                "name": "demo.bin",
                "size": 10,
                "client_profile": "desktop",
            }
        )
        self.module.dl_manager.start_download = AsyncMock(
            return_value="task-id"
        )

        response = self.client.post("/api/quark/download/file-id")

        self.assertEqual(response.status_code, 200)
        headers = self.module.dl_manager.start_download.await_args.kwargs[
            "headers"
        ]
        self.assertEqual(
            headers,
            {
                "User-Agent": "signed-link-agent",
                "Cookie": "__uid=secret-cookie",
            },
        )
        self.assertEqual(
            self.module.dl_manager.start_download.await_args.kwargs[
                "source_profile"
            ],
            "quark-desktop",
        )

    def test_baidu_download_passes_cookie_to_download_engine(self) -> None:
        self.accept_disclaimer()
        self.module.baidu_client.bduss = "bduss"
        self.module.baidu_client.stoken = "stoken"
        self.module.baidu_client.get_download_links = AsyncMock(
            return_value={
                "success": True,
                "app_id_used": 125463,
                "items": [
                    {
                        "url": "https://download.example/file",
                        "name": "demo.bin",
                        "size": 10,
                    }
                ],
            }
        )
        self.module.dl_manager.start_download = AsyncMock(
            return_value="task-id"
        )

        response = self.client.post(
            "/api/baidu/download/1",
            params={"path": "/demo.bin"},
        )

        self.assertEqual(response.status_code, 200)
        self.module.baidu_client.get_download_links.assert_awaited_once_with(
            [1],
            paths=["/demo.bin"],
        )
        headers = self.module.dl_manager.start_download.await_args.kwargs[
            "headers"
        ]
        self.assertEqual(headers["User-Agent"], "softxm;netdisk")
        self.assertEqual(headers["Cookie"], "BDUSS=bduss;")
        self.assertEqual(headers["Connection"], "Keep-Alive")
        self.assertNotIn("Referer", headers)
        self.assertEqual(
            self.module.dl_manager.start_download.await_args.kwargs[
                "baidu_app_id_used"
            ],
            125463,
        )

    def test_downloads_response_exposes_configured_directory(self) -> None:
        self.accept_disclaimer()

        response = self.client.get("/api/downloads")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["download_directory"],
            str(self.download_dir),
        )

    def test_download_directory_can_be_changed_and_persisted(self) -> None:
        self.accept_disclaimer()
        changed_dir = self.download_dir.parent / "media" / "cloud"

        response = self.client.put(
            "/api/settings",
            json={"download_dir": str(changed_dir)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["download_dir"], str(changed_dir.resolve()))
        self.assertEqual(self.module.dl_manager.download_dir, changed_dir.resolve())
        self.assertTrue(changed_dir.is_dir())
        self.assertEqual(
            (self.config_dir.parent / "download_dir").read_text(
                encoding="utf-8"
            ).strip(),
            str(changed_dir.resolve()),
        )
        downloads = self.client.get("/api/downloads").json()
        self.assertEqual(
            downloads["download_directory"],
            str(changed_dir.resolve()),
        )

    def test_download_directory_rejects_relative_path(self) -> None:
        self.accept_disclaimer()

        response = self.client.put(
            "/api/settings",
            json={"download_dir": "relative/downloads"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.module.dl_manager.download_dir, self.download_dir)

    def test_baidu_folder_download_preserves_directory_structure(self) -> None:
        self.accept_disclaimer()
        self.module.baidu_client.walk_folder = AsyncMock(
            return_value={
                "success": True,
                "files": [
                    {
                        "fs_id": 11,
                        "name": "内层.txt",
                        "path": "/根目录/子目录/内层.txt",
                        "size": 2,
                        "relative_dir": "子目录",
                    }
                ],
            }
        )
        self.module.baidu_client.get_download_links = AsyncMock(
            return_value={
                "success": True,
                "items": [
                    {
                        "fs_id": 11,
                        "name": "内层.txt",
                        "url": "https://download.example/file",
                        "size": 2,
                    }
                ],
            }
        )
        self.module.dl_manager.start_download = AsyncMock(
            return_value="task-id"
        )

        response = self.client.post(
            "/api/baidu/download-folder",
            json={"path": "/根目录", "name": "根目录"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_count"], 1)
        self.module.baidu_client.get_download_links.assert_awaited_once_with(
            [11],
            paths=["/根目录/子目录/内层.txt"],
        )
        kwargs = self.module.dl_manager.start_download.await_args.kwargs
        self.assertEqual(kwargs["relative_dir"], "根目录/子目录")

    def test_baidu_file_download_requires_path(self) -> None:
        self.accept_disclaimer()

        response = self.client.post("/api/baidu/download/1")

        self.assertEqual(response.status_code, 422)
        self.assertIn("路径", response.text)

    def test_baidu_folder_stops_after_account_authorization_failure(
        self,
    ) -> None:
        self.accept_disclaimer()
        self.module.baidu_client.walk_folder = AsyncMock(
            return_value={
                "success": True,
                "files": [
                    {
                        "fs_id": 11,
                        "name": "一.txt",
                        "path": "/根目录/一.txt",
                    },
                    {
                        "fs_id": 12,
                        "name": "二.txt",
                        "path": "/根目录/二.txt",
                    },
                ],
            }
        )
        self.module.baidu_client.get_download_links = AsyncMock(
            return_value={
                "success": False,
                "error": "百度账号授权签名失效，请重新保存百度凭据后重试",
            }
        )

        response = self.client.post(
            "/api/baidu/download-folder",
            json={"path": "/根目录", "name": "根目录"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("授权签名失效", response.text)
        self.module.baidu_client.get_download_links.assert_awaited_once()

    def test_quark_folder_download_preserves_directory_structure(self) -> None:
        self.accept_disclaimer()
        self.module.quark_client.walk_folder = AsyncMock(
            return_value={
                "success": True,
                "files": [
                    {
                        "fid": "nested",
                        "name": "内层.txt",
                        "size": 2,
                        "relative_dir": "子目录",
                    }
                ],
            }
        )
        self.module.quark_client.get_download_url = AsyncMock(
            return_value={
                "success": True,
                "fid": "nested",
                "name": "内层.txt",
                "url": "https://download.example/file",
                "size": 2,
            }
        )
        self.module.dl_manager.start_download = AsyncMock(
            return_value="task-id"
        )

        response = self.client.post(
            "/api/quark/download-folder",
            json={"fid": "root", "name": "根目录"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_count"], 1)
        kwargs = self.module.dl_manager.start_download.await_args.kwargs
        self.assertEqual(kwargs["relative_dir"], "根目录/子目录")

    def test_credentials_are_never_returned(self) -> None:
        self.accept_disclaimer()
        with patch.object(
            self.module.BaiduPanClient,
            "verify_login",
            new=AsyncMock(return_value={"success": True, "username": "tester"}),
        ):
            response = self.client.put(
                "/api/credentials/baidu",
                json={"bduss": "secret-bduss", "stoken": "secret-stoken"},
            )
        self.assertEqual(response.status_code, 200)
        serialized = json.dumps(response.json(), ensure_ascii=False)
        self.assertNotIn("secret-bduss", serialized)
        self.assertNotIn("secret-stoken", serialized)
        self.assertTrue(response.json()["baidu"]["configured"])

        status = self.client.get("/api/credentials")
        self.assertNotIn("secret-bduss", status.text)
        cleared = self.client.delete("/api/credentials/baidu")
        self.assertFalse(cleared.json()["baidu"]["configured"])

    def test_baidu_full_cookie_is_filtered_before_verification_and_storage(self) -> None:
        self.accept_disclaimer()
        with patch.object(
            self.module.BaiduPanClient,
            "verify_login",
            new=AsyncMock(return_value={"success": True, "username": "tester"}),
        ):
            response = self.client.put(
                "/api/credentials/baidu",
                json={
                    "cookie": (
                        "Cookie: BAIDUID=discard; BDUSS=kept-bduss; "
                        "STOKEN=kept-stoken; other=discard"
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.module.baidu_client.bduss, "kept-bduss")
        self.assertEqual(self.module.baidu_client.stoken, "kept-stoken")
        self.assertEqual(
            self.module.credential_store.get("baidu"),
            {"bduss": "kept-bduss", "stoken": "kept-stoken"},
        )

    def test_quark_cookie_is_normalized_before_verification_and_storage(self) -> None:
        self.accept_disclaimer()
        with patch.object(
            self.module.QuarkPanClient,
            "verify_login",
            new=AsyncMock(return_value={"success": True, "username": "tester"}),
        ):
            response = self.client.put(
                "/api/credentials/quark",
                json={"cookie": "Cookie: __uid=one; invalid;\n__uid=two; __kps=three"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.module.quark_client.cookie,
            "__uid=one; __kps=three",
        )
        self.assertEqual(
            self.module.credential_store.get("quark"),
            {"cookie": "__uid=one; __kps=three"},
        )

    def test_provider_status_lazily_verifies_persisted_credentials(self) -> None:
        self.accept_disclaimer()
        self.module.credential_store.update(
            "baidu",
            {"bduss": "saved-bduss", "stoken": "saved-stoken"},
        )
        self.module.baidu_client._logged_in = False
        self.module.baidu_client.verify_login = AsyncMock(
            return_value={"success": True, "username": "持久用户"},
        )

        response = self.client.get("/api/baidu/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "logged_in": True,
                "username": "持久用户",
                "configured": True,
            },
        )
        self.module.baidu_client.verify_login.assert_awaited_once()

    def test_invalid_quark_cookie_is_not_persisted(self) -> None:
        self.accept_disclaimer()
        with patch.object(
            self.module.QuarkPanClient,
            "verify_login",
            new=AsyncMock(
                return_value={"success": False, "error": "验证失败"}
            ),
        ):
            response = self.client.put(
                "/api/credentials/quark",
                json={"cookie": "invalid-cookie"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            self.client.get("/api/credentials")
            .json()["quark"]["configured"]
        )
        self.assertNotIn("invalid-cookie", response.text)

    def test_logs_and_diagnostic_export_are_available(self) -> None:
        self.accept_disclaimer()
        self.module.logger.info("test log")
        logs = self.client.get("/api/logs?lines=20")
        self.assertEqual(logs.status_code, 200)
        self.assertIn("test log", logs.json()["content"])
        with patch.object(
            self.module,
            "run_diagnostics",
            return_value={"network": {"ok": True}},
        ):
            result = self.client.post("/api/diagnostics")
            export = self.client.get("/api/diagnostics/export")
        self.assertTrue(result.json()["network"]["ok"])
        self.assertEqual(export.headers["content-type"], "application/zip")
        self.assertGreater(len(export.content), 100)

    def test_local_files_can_be_listed_renamed_and_previewed(self) -> None:
        self.accept_disclaimer()
        image = self.download_dir / "旧照片.png"
        image.write_bytes(b"not-a-real-png")

        listed = self.client.get("/api/local/list?path=/")
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(listed.json()["files"][0]["has_thumbnail"])

        renamed = self.client.post(
            "/api/local/rename",
            json={"path": "/旧照片.png", "new_name": "新照片.png"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertTrue((self.download_dir / "新照片.png").exists())

        thumbnail = self.client.get("/api/local/thumbnail?path=/新照片.png")
        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(thumbnail.content, b"not-a-real-png")

    def test_local_file_api_rejects_path_traversal(self) -> None:
        self.accept_disclaimer()
        response = self.client.get("/api/local/list?path=/../../")
        self.assertEqual(response.status_code, 400)

    def test_remote_rename_routes_validate_and_delegate(self) -> None:
        self.accept_disclaimer()
        self.module.baidu_client.rename = AsyncMock(
            return_value={"success": True, "name": "新名称.txt"}
        )
        response = self.client.post(
            "/api/baidu/rename",
            json={"path": "/旧名称.txt", "new_name": "新名称.txt"},
        )
        self.assertEqual(response.status_code, 200)
        self.module.baidu_client.rename.assert_awaited_once_with(
            "/旧名称.txt", "新名称.txt"
        )

        invalid = self.client.post(
            "/api/quark/rename",
            json={"fid": "fid", "new_name": "../越界"},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_alipan_qr_routes_never_return_refresh_token(self) -> None:
        self.accept_disclaimer()
        self.module.alipan_qr_manager.start = AsyncMock(
            return_value={"success": True, "session_id": "qr-session", "expires_in": 300}
        )
        self.module.alipan_qr_manager.image = AsyncMock(return_value=b"<svg></svg>")
        self.module.alipan_qr_manager.poll = AsyncMock(
            return_value={
                "success": True,
                "status": "confirmed",
                "message": "扫码登录成功",
                "refresh_token": "secret-refresh-token",
            }
        )
        with patch.object(
            self.module,
            "_replace_alipan",
            new=AsyncMock(return_value={"success": True, "username": "扫码账号"}),
        ) as replace:
            started = self.client.post("/api/alipan/qr/start")
            image = self.client.get("/api/alipan/qr/qr-session.svg")
            status = self.client.get("/api/alipan/qr/qr-session/status")

        self.assertEqual(started.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/svg+xml")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["logged_in"])
        self.assertNotIn("secret-refresh-token", status.text)
        replace.assert_awaited_once()
        self.assertEqual(replace.await_args.kwargs["auth_mode_override"], "refresh_token")

    def test_root_is_available_before_onboarding(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("多网盘下载器", response.text)
