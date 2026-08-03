from __future__ import annotations

import unittest
import hashlib
from unittest.mock import AsyncMock, Mock, patch

from baidu import BaiduPanClient


class BaiduClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_rename_uses_filemanager_endpoint(self) -> None:
        client = BaiduPanClient("bduss", "stoken")
        client._logged_in = True
        response = Mock(status_code=200)
        response.json.return_value = {"errno": 0}
        transport = Mock()
        transport.post = AsyncMock(return_value=response)
        client._get_client = Mock(return_value=transport)

        result = await client.rename("/旧名称.txt", "新名称.txt")

        self.assertTrue(result["success"])
        call = transport.post.await_args
        self.assertTrue(call.args[0].endswith("/api/filemanager"))
        self.assertEqual(call.kwargs["params"]["opera"], "rename")
        self.assertIn("新名称.txt", call.kwargs["data"]["filelist"])

    async def test_path_download_uses_pcs_locatedownload(self) -> None:
        client = BaiduPanClient(
            bduss="bduss",
            stoken="stoken",
            app_id=309847,
        )
        client._logged_in = True
        client._user_id = 123456789
        response = Mock(status_code=200)
        response.json.return_value = {
            "urls": [{"url": "https://d.example/file?sign=s"}],
            "size": 10,
        }
        transport = Mock()
        transport.get = AsyncMock(return_value=response)
        client._get_client = Mock(return_value=transport)

        timestamp = 1720000000
        with patch("baidu.time.time", return_value=timestamp):
            result = await client.get_download_links(
                [1],
                paths=["/目录/file.bin"],
            )

        self.assertTrue(result["success"])
        request = transport.get.await_args
        self.assertEqual(
            request.args[0],
            "https://d.pcs.baidu.com/rest/2.0/pcs/file",
        )
        params = request.kwargs["params"]
        devuid = hashlib.md5(b"bduss").hexdigest().upper() + "|0"
        expected_rand = hashlib.sha1(
            (
                hashlib.sha1(b"bduss").hexdigest()
                + "123456789"
                + "ebrcUYiuxaZv2XGu7KIYKxUrqfnOfpDF"
                + str(timestamp)
                + devuid
            ).encode()
        ).hexdigest()
        self.assertEqual(params["app_id"], 309847)
        self.assertEqual(params["path"], "/目录/file.bin")
        self.assertEqual(params["clienttype"], "17")
        self.assertEqual(params["time"], str(timestamp))
        self.assertEqual(params["devuid"], devuid)
        self.assertEqual(params["cuid"], devuid)
        self.assertEqual(params["rand"], expected_rand)
        self.assertEqual(
            request.kwargs["headers"]["User-Agent"],
            "softxm;netdisk",
        )
        self.assertEqual(result["items"][0]["name"], "file.bin")
        self.assertEqual(result["app_id_used"], 309847)
        self.assertNotIn("bdstoken=stoken", result["items"][0]["url"])

    async def test_missing_path_is_rejected_without_network_request(self) -> None:
        client = BaiduPanClient("bduss", "stoken", app_id=309847)
        client._logged_in = True
        transport = Mock()
        transport.post = AsyncMock()
        transport.get = AsyncMock()
        client._get_client = Mock(return_value=transport)

        result = await client.get_download_links([1])

        self.assertFalse(result["success"])
        self.assertIn("文件路径", result["error"])
        transport.post.assert_not_awaited()
        transport.get.assert_not_awaited()

    async def test_permission_denied_app_id_falls_back_to_default(
        self,
    ) -> None:
        client = BaiduPanClient(
            bduss="bduss",
            stoken="stoken",
            app_id=250527,
        )
        client._logged_in = True
        client._user_id = 123456789
        denied = Mock(status_code=200)
        denied.json.return_value = {
            "error_code": 4,
            "error_msg": "No permission to do this operation",
        }
        accepted = Mock(status_code=200)
        accepted.json.return_value = {
            "urls": [{"url": "https://d.example/file?sign=s"}],
            "size": 10,
        }
        transport = Mock()
        transport.get = AsyncMock(side_effect=[denied, accepted])
        client._get_client = Mock(return_value=transport)

        result = await client.get_download_links(
            [1],
            paths=["/folder/file.bin"],
        )

        self.assertTrue(result["success"])
        self.assertEqual(transport.get.await_count, 2)
        attempted_app_ids = [
            call.kwargs["params"]["app_id"]
            for call in transport.get.await_args_list
        ]
        self.assertEqual(attempted_app_ids, [250527, 250528])
        self.assertEqual(result["app_id_used"], 250528)
        self.assertEqual(client.app_id, 250528)

    async def test_failure_logs_are_written_under_application_logger(self) -> None:
        client = BaiduPanClient("bduss", "stoken")
        client._logged_in = True
        client._user_id = 123456789
        failed = Mock(status_code=200)
        failed.json.return_value = {
            "error_code": 31326,
            "error_msg": "user is not authorized, hitcode:125",
        }
        transport = Mock()
        transport.get = AsyncMock(return_value=failed)
        client._get_client = Mock(return_value=transport)

        with self.assertLogs(
            "clouddl.baidu",
            level="WARNING",
        ) as captured:
            result = await client.get_download_links(
                [1],
                paths=["/file.bin"],
            )

        self.assertFalse(result["success"])
        self.assertIn("百度账号授权签名失效", result["error"])
        self.assertTrue(any("errno=31326" in line for line in captured.output))

    async def test_fetch_user_id_uses_tieba_login_response(self) -> None:
        client = BaiduPanClient("bduss", "stoken")
        client._logged_in = True
        response = Mock(status_code=200)
        response.json.return_value = {
            "error_code": "0",
            "user": {"id": "123456789"},
        }
        transport = Mock()
        transport.post = AsyncMock(return_value=response)
        client._get_client = Mock(return_value=transport)

        user_id = await client._fetch_user_id()

        self.assertEqual(user_id, 123456789)
        request = transport.post.await_args
        self.assertEqual(
            request.args[0],
            "https://tieba.baidu.com/c/s/login",
        )
        self.assertEqual(request.kwargs["data"]["bdusstoken"], "bduss|null")
        self.assertRegex(request.kwargs["data"]["sign"], r"^[0-9A-F]{32}$")

    async def test_walk_folder_preserves_nested_relative_paths(self) -> None:
        client = BaiduPanClient("bduss", "stoken")
        client._logged_in = True
        client.list_files = AsyncMock(
            side_effect=[
                {
                    "success": True,
                    "files": [
                        {
                            "name": "子目录",
                            "path": "/根目录/子目录",
                            "is_dir": True,
                            "fs_id": 2,
                        },
                        {
                            "name": "根文件.txt",
                            "path": "/根目录/根文件.txt",
                            "is_dir": False,
                            "fs_id": 3,
                            "size": 1,
                        },
                    ],
                },
                {
                    "success": True,
                    "files": [
                        {
                            "name": "内层.txt",
                            "path": "/根目录/子目录/内层.txt",
                            "is_dir": False,
                            "fs_id": 4,
                            "size": 2,
                        }
                    ],
                },
            ]
        )

        result = await client.walk_folder("/根目录")

        self.assertTrue(result["success"])
        self.assertEqual(
            [(item["fs_id"], item["relative_dir"]) for item in result["files"]],
            [(3, ""), (4, "子目录")],
        )


if __name__ == "__main__":
    unittest.main()
