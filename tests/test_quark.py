from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

import httpx

from quark import QuarkPanClient


def response(
    status_code: int,
    *,
    json_data: dict | None = None,
    text: str = "",
    content_type: str | None = None,
    response_headers: dict[str, str] | None = None,
) -> httpx.Response:
    headers = dict(response_headers or {})
    if content_type:
        headers["content-type"] = content_type
    request = httpx.Request("GET", "https://pan.quark.cn/account/info")
    if json_data is not None:
        return httpx.Response(
            status_code,
            json=json_data,
            headers=headers,
            request=request,
        )
    return httpx.Response(
        status_code,
        text=text,
        headers=headers,
        request=request,
    )


class QuarkClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_rename_uses_file_rename_endpoint(self) -> None:
        client = QuarkPanClient("k=v")
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.return_value = response(
            200,
            json_data={"status": 200, "data": {}},
        )

        result = await client.rename("fid-1", "新名称.txt")

        self.assertTrue(result["success"])
        call = client._client.post.await_args
        self.assertTrue(str(call.args[0]).endswith("/file/rename"))
        self.assertEqual(call.kwargs["json"], {"fid": "fid-1", "file_name": "新名称.txt"})

    async def test_verify_login_uses_account_endpoint_and_current_payload(
        self,
    ) -> None:
        client = QuarkPanClient("k=v")
        client._client = AsyncMock()
        client._client.get.return_value = response(
            200,
            json_data={
                "status": 200,
                "data": {
                    "nickname": "测试账号",
                    "total_capacity": 100,
                    "use_capacity": 40,
                },
            },
        )

        result = await client.verify_login()

        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "测试账号")
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["used"], 40)
        self.assertEqual(
            str(client._client.get.await_args.args[0]),
            "https://pan.quark.cn/account/info",
        )

    async def test_verify_login_translates_empty_non_json_response(self) -> None:
        client = QuarkPanClient("k=v")
        client._client = AsyncMock()
        client._client.get.return_value = response(
            200,
            text="",
            content_type="text/html",
        )

        result = await client.verify_login()

        self.assertFalse(result["success"])
        self.assertIn("未返回 JSON", result["error"])
        self.assertNotIn("Expecting value", result["error"])

    async def test_verify_login_translates_html_risk_control_response(
        self,
    ) -> None:
        client = QuarkPanClient("k=v")
        client._client = AsyncMock()
        client._client.get.return_value = response(
            200,
            text="<html>risk control</html>",
            content_type="text/html; charset=utf-8",
        )

        result = await client.verify_login()

        self.assertFalse(result["success"])
        self.assertIn("网页风控", result["error"])
        self.assertNotIn("<html>", result["error"])

    async def test_verify_login_translates_forbidden_response(self) -> None:
        client = QuarkPanClient("k=v")
        client._client = AsyncMock()
        client._client.get.return_value = response(
            403,
            text="<html>forbidden</html>",
            content_type="text/html",
        )

        result = await client.verify_login()

        self.assertEqual(
            result["error"],
            "夸克拒绝了验证登录请求，请重新获取 Cookie 后再试",
        )

    async def test_cookie_header_prefix_is_removed(self) -> None:
        client = QuarkPanClient(" Cookie: __uid=abc; __pus=def ")

        self.assertEqual(client.cookie, "__uid=abc; __pus=def")

    async def test_download_uses_current_post_endpoint(self) -> None:
        client = QuarkPanClient("k=v")
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.return_value = response(
            200,
            json_data={
                "status": 200,
                "data": [
                    {
                        "fid": "file-id",
                        "file_name": "demo.bin",
                        "download_url": "https://download.example/demo.bin",
                        "size": 123,
                    }
                ],
            },
        )

        with self.assertLogs("clouddl.quark", level="INFO") as captured:
            result = await client.get_download_url("file-id")

        self.assertTrue(result["success"])
        self.assertEqual(
            str(client._client.post.await_args.args[0]),
            "https://drive.quark.cn/1/clouddrive/file/download",
        )
        self.assertEqual(
            client._client.post.await_args.kwargs["json"],
            {"fids": ["file-id"]},
        )
        params = client._client.post.await_args.kwargs["params"]
        self.assertEqual(params, {"pr": "ucpro", "fr": "pc"})
        self.assertIn(
            "quark-cloud-drive/2.5.20",
            client._client.post.await_args.kwargs["headers"]["User-Agent"],
        )
        self.assertTrue(any("HTTP 200" in line for line in captured.output))
        self.assertTrue(any("获取下载链接成功" in line for line in captured.output))
        self.assertEqual(result["client_profile"], "desktop")
        self.assertEqual(
            client.download_headers()["User-Agent"],
            client._client.post.await_args.kwargs["headers"]["User-Agent"],
        )
        self.assertEqual(client.download_headers()["Cookie"], "k=v")
        self.assertNotIn("Accept-Encoding", client.download_headers())
        self.assertNotIn("Origin", client.download_headers())
        self.assertEqual(
            client.download_headers()["Referer"],
            "https://pan.quark.cn",
        )
        self.assertNotIn("Sec-Fetch-Mode", client.download_headers())
        self.assertEqual(
            result["headers"]["User-Agent"],
            client._client.post.await_args.kwargs["headers"]["User-Agent"],
        )
        self.assertNotIn("X-Urlp", result["headers"])

    async def test_download_uses_cookie_refreshed_by_link_response(self) -> None:
        client = QuarkPanClient("__puus=old; other=value")
        client._logged_in = True
        client._client = AsyncMock()
        client._client.headers = {}
        client._client.post.return_value = response(
            200,
            json_data={
                "status": 200,
                "data": [
                    {
                        "fid": "file-id",
                        "file_name": "demo.bin",
                        "download_url": "https://download.example/demo.bin",
                        "size": 123,
                    }
                ],
            },
            response_headers={
                "set-cookie": "__puus=new; Path=/; HttpOnly",
            },
        )

        result = await client.get_download_url("file-id")

        self.assertTrue(result["success"])
        self.assertIn("__puus=new", result["headers"]["Cookie"])
        self.assertIn("other=value", result["headers"]["Cookie"])
        self.assertNotIn("__puus=old", result["headers"]["Cookie"])

    async def test_download_keeps_one_desktop_identity_on_risk_control(
        self,
    ) -> None:
        client = QuarkPanClient("k=v")
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.side_effect = [
            response(
                200,
                json_data={
                    "status": 400,
                    "code": 23018,
                    "message": "risk control",
                },
            ),
            response(
                200,
                json_data={
                    "status": 200,
                    "data": [
                        {
                            "fid": "file-id",
                            "file_name": "demo.bin",
                            "download_url": "https://download.example/demo.bin",
                        }
                    ],
                },
            ),
        ]

        result = await client.get_download_url("file-id")

        self.assertFalse(result["success"])
        self.assertEqual(client._client.post.await_count, 1)
        request_headers = client._client.post.await_args.kwargs["headers"]
        self.assertIn(
            "quark-cloud-drive/2.5.20",
            request_headers["User-Agent"],
        )

    async def test_download_does_not_mix_identity_after_http_400(
        self,
    ) -> None:
        client = QuarkPanClient("k=v")
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.side_effect = [
            response(
                400,
                json_data={
                    "status": 400,
                    "code": 23018,
                    "message": "risk control",
                },
            ),
            response(
                200,
                json_data={
                    "status": 200,
                    "data": [
                        {
                            "fid": "file-id",
                            "file_name": "demo.bin",
                            "download_url": "https://download.example/demo.bin",
                        }
                    ],
                },
            ),
        ]

        result = await client.get_download_url("file-id")

        self.assertFalse(result["success"])
        self.assertEqual(client._client.post.await_count, 1)

    async def test_walk_folder_preserves_nested_relative_paths(self) -> None:
        client = QuarkPanClient("k=v")
        client._logged_in = True
        client._list_by_fid = AsyncMock(
            side_effect=[
                {
                    "success": True,
                    "payload": {
                        "data": {
                            "list": [
                                {
                                    "fid": "child-dir",
                                    "file_name": "子目录",
                                    "dir": True,
                                    "file_type": 0,
                                },
                                {
                                    "fid": "root-file",
                                    "file_name": "根文件.txt",
                                    "dir": False,
                                    "file_type": 1,
                                    "size": 1,
                                },
                            ]
                        }
                    },
                },
                {
                    "success": True,
                    "payload": {
                        "data": {
                            "list": [
                                {
                                    "fid": "nested-file",
                                    "file_name": "内层.txt",
                                    "dir": False,
                                    "file_type": 1,
                                    "size": 2,
                                }
                            ]
                        }
                    },
                },
            ]
        )

        result = await client.walk_folder("root")

        self.assertTrue(result["success"])
        self.assertEqual(
            [(item["fid"], item["relative_dir"]) for item in result["files"]],
            [("root-file", ""), ("nested-file", "子目录")],
        )
