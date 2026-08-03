from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

import httpx

from alipan import AlipanPanClient


def response(
    status_code: int,
    *,
    json_data: dict | None = None,
) -> httpx.Response:
    request = httpx.Request("POST", "https://api.aliyundrive.com/v2/user/get")
    if json_data is not None:
        return httpx.Response(status_code, json=json_data, request=request)
    return httpx.Response(status_code, text="{}", request=request)


class AlipanClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_openapi_rename_uses_open_file_update(self) -> None:
        client = AlipanPanClient(
            refresh_token="rt", client_id="client", auth_mode="openapi"
        )
        client._access_token = "at"
        client._drive_id = "drive"
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.return_value = response(
            200, json_data={"file_id": "fid", "name": "新名称.txt"}
        )

        result = await client.rename("fid", "新名称.txt")

        self.assertTrue(result["success"])
        call = client._client.post.await_args
        self.assertEqual(
            str(call.args[0]),
            "https://open.aliyundrive.com/adrive/v1.0/openFile/update",
        )

    async def test_verify_login_refreshes_token_and_gets_user(self) -> None:
        client = AlipanPanClient(refresh_token="rt-123")
        client._client = AsyncMock()
        client._client.post.side_effect = [
            response(200, json_data={"access_token": "at-abc", "refresh_token": "rt-new"}),
            response(
                200,
                json_data={
                    "default_drive_id": "232557691",
                    "user_name": "188***180",
                },
            ),
        ]

        result = await client.verify_login()

        self.assertTrue(result["success"])
        self.assertEqual(result["username"], "188***180")
        self.assertEqual(result["drive_id"], "232557691")
        self.assertEqual(client.refresh_token, "rt-new")
        self.assertTrue(client._logged_in)

    async def test_verify_login_refresh_failure(self) -> None:
        client = AlipanPanClient(refresh_token="bad-rt")
        client._client = AsyncMock()
        client._client.post.side_effect = [
            response(400),
            response(400),
        ]

        result = await client.verify_login()

        self.assertFalse(result["success"])
        self.assertFalse(client._logged_in)

    async def test_list_files_uses_v3_list_and_maps_entries(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        client._access_token = "at"
        client._drive_id = "232557691"
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.return_value = response(
            200,
            json_data={
                "items": [
                    {
                        "name": "海报.png",
                        "file_id": "file-1",
                        "type": "file",
                        "size": 1393429,
                        "updated_at": "2023-01-01T00:00:00Z",
                    },
                    {
                        "name": "文件夹",
                        "file_id": "folder-1",
                        "type": "folder",
                        "size": 0,
                    },
                ]
            },
        )

        result = await client.list_files("/")

        self.assertTrue(result["success"])
        files = result["files"]
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["fid"], "file-1")
        self.assertFalse(files[0]["is_dir"])
        self.assertEqual(files[0]["size"], 1393429)
        self.assertTrue(files[1]["is_dir"])
        # 验证请求走 v3 列表接口
        called_url = str(client._client.post.await_args.args[0])
        self.assertIn("/adrive/v3/file/list", called_url)

    async def test_get_download_url_returns_oss_link(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        client._access_token = "at"
        client._drive_id = "232557691"
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.return_value = response(
            200,
            json_data={
                "url": "https://cn-beijing-data.aliyundrive.net/test?ap=25dzX3vbYqktVxyX",
                "size": 1000,
                "name": "海报.png",
            },
        )

        result = await client.get_download_url("file-1")

        self.assertTrue(result["success"])
        self.assertIn("aliyundrive.net", result["url"])
        self.assertEqual(result["fid"], "file-1")
        self.assertEqual(result["headers"]["Referer"], "https://www.aliyundrive.com/")
        self.assertEqual(result["client_profile"], "private")

    async def test_get_download_url_empty_url_reports_share_limit(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        client._access_token = "at"
        client._drive_id = "232557691"
        client._logged_in = True
        client._client = AsyncMock()
        # desktop 与 web 族都返回空 url
        client._client.post.return_value = response(
            200,
            json_data={"url": "", "size": 1000},
        )

        result = await client.get_download_url("big-share-file")

        self.assertFalse(result["success"])
        self.assertIn("下载直链", result["error"])

    async def test_private_large_file_reports_official_web_limit(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        client._access_token = "at"
        client._drive_id = "drive"
        client._logged_in = True
        client._file_cache["large"] = {
            "file_id": "large",
            "name": "大文件.iso",
            "size": 101 * 1024 * 1024,
        }
        client._client = AsyncMock()
        client._client.post.return_value = response(200, json_data={"url": ""})

        result = await client.get_download_url("large")

        self.assertFalse(result["success"])
        self.assertIn("100 MB", result["error"])
        self.assertIn("OpenAPI", result["error"])

    async def test_openapi_uses_official_endpoint_and_cdn_url(self) -> None:
        client = AlipanPanClient(
            refresh_token="open-rt",
            client_id="client-id",
            client_secret="client-secret",
            auth_mode="openapi",
        )
        client._access_token = "open-at"
        client._drive_id = "drive"
        client._logged_in = True
        client._file_cache["large"] = {
            "file_id": "large",
            "name": "电影.mkv",
            "size": 20 * 1024 * 1024 * 1024,
        }
        client._client = AsyncMock()
        client._client.post.return_value = response(
            200,
            json_data={"cdn_url": "https://cdn.example.test/large"},
        )

        result = await client.get_download_url("large")

        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "电影.mkv")
        self.assertEqual(result["client_profile"], "openapi")
        called_url = str(client._client.post.await_args.args[0])
        self.assertEqual(
            called_url,
            "https://open.aliyundrive.com/adrive/v1.0/openFile/getDownloadUrl",
        )

    async def test_api_refreshes_once_after_401(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        client._access_token = "old-at"
        client._drive_id = "drive"
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.side_effect = [
            response(401),
            response(200, json_data={"access_token": "new-at", "refresh_token": "new-rt"}),
            response(200, json_data={"items": []}),
        ]

        result = await client.list_files("/")

        self.assertTrue(result["success"])
        self.assertEqual(client._access_token, "new-at")
        self.assertEqual(client.refresh_token, "new-rt")

    async def test_list_page_uses_next_marker(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        client._access_token = "at"
        client._drive_id = "drive"
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.side_effect = [
            response(200, json_data={"items": [], "next_marker": "page-2"}),
            response(200, json_data={"items": [{"file_id": "f2", "name": "二.txt", "type": "file"}]}),
        ]

        result = await client.list_files("/", page=2)

        self.assertTrue(result["success"])
        self.assertEqual(result["files"][0]["fid"], "f2")
        second_body = client._client.post.await_args_list[1].kwargs["json"]
        self.assertEqual(second_body["marker"], "page-2")

    async def test_walk_folder_preserves_relative_paths(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        client._access_token = "at"
        client._drive_id = "232557691"
        client._logged_in = True
        client._client = AsyncMock()
        client._client.post.side_effect = [
            response(
                200,
                json_data={
                    "items": [
                        {
                            "name": "子目录",
                            "file_id": "dir-1",
                            "type": "folder",
                        },
                        {
                            "name": "根文件.txt",
                            "file_id": "f-1",
                            "type": "file",
                            "size": 1,
                        },
                    ]
                },
            ),
            response(
                200,
                json_data={
                    "items": [
                        {
                            "name": "内层.txt",
                            "file_id": "f-2",
                            "type": "file",
                            "size": 2,
                        }
                    ]
                },
            ),
        ]

        result = await client.walk_folder("root")

        self.assertTrue(result["success"])
        self.assertEqual(
            [(item["fid"], item["relative_dir"]) for item in result["files"]],
            [("f-1", ""), ("f-2", "子目录")],
        )

    async def test_download_headers_include_referer(self) -> None:
        client = AlipanPanClient(refresh_token="rt")
        headers = client.download_headers()
        self.assertEqual(headers["Referer"], "https://www.aliyundrive.com/")
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
