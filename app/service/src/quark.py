from __future__ import annotations

import json
from typing import Any

import logging
import httpx


ACCOUNT_URL = "https://pan.quark.cn/account/info"
DRIVE_API = "https://drive.quark.cn/1/clouddrive"
COMMON_PARAMS = {
    "pr": "ucpro",
    "fr": "pc",
    "uc_param_str": "",
}
# Try different pr/fr combinations for speed optimization
# "pr=ucpro&fr=pc" is the desktop client identity
QUARK_ALT_PARAMS = {
    "pr": "ucpro",
    "fr": "android",
    "uc_param_str": "",
}
QUARK_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)
QUARK_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Electron/18.3.5.4-b478491100 Safari/537.36 "
    "Channel/pckk_other_ch"
)
LOGGER = logging.getLogger("clouddl.quark")


class QuarkPanClient:
    """使用用户提供的 Cookie 访问夸克网盘网页端接口。"""

    def __init__(self, cookie: str = ""):
        value = cookie.strip()
        if value[:7].lower() == "cookie:":
            value = value[7:].strip()
        self.cookie = value
        self._client: httpx.AsyncClient | None = None
        self._logged_in = False
        self._username = ""
        self._download_user_agent = QUARK_DESKTOP_USER_AGENT
        self._thumbnail_urls: dict[str, str] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": QUARK_DESKTOP_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://pan.quark.cn",
                    "Cookie": self.cookie,
                },
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    def download_headers(self, url: str = "") -> dict[str, str]:
        """返回与当前夸克直链签发请求一致的下载请求头。"""
        return {
            "User-Agent": self._download_user_agent,
            "Referer": "https://pan.quark.cn",
            "Cookie": self.cookie,
        }

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _refresh_cookie_from_response(
        self,
        response: httpx.Response,
    ) -> None:
        refreshed = dict(response.cookies.items())
        if not refreshed:
            return
        pairs: dict[str, str] = {}
        for part in self.cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                pairs[name] = value
        pairs.update(refreshed)
        self.cookie = "; ".join(
            f"{name}={value}" for name, value in pairs.items()
        )
        if self._client is not None and hasattr(self._client, "headers"):
            self._client.headers["Cookie"] = self.cookie

    @staticmethod
    def _json_response(
        response: httpx.Response,
        action: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if response.status_code in {401, 403}:
            return (
                None,
                f"夸克拒绝了{action}请求，请重新获取 Cookie 后再试",
            )
        if response.status_code >= 400:
            return None, f"{action}失败：HTTP {response.status_code}"
        content_type = response.headers.get("content-type", "").lower()
        if not response.content or "json" not in content_type:
            return (
                None,
                f"{action}接口未返回 JSON，可能遇到登录失效或网页风控",
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, f"{action}接口返回了无法解析的数据，请稍后重试"
        if not isinstance(payload, dict):
            return None, f"{action}接口返回格式异常"
        return payload, None

    @staticmethod
    def _success(payload: dict[str, Any]) -> bool:
        accepted = {0, 200, "0", "200"}
        return (
            payload.get("status") in accepted
            or payload.get("code") in accepted
            or payload.get("success") is True
        )

    @staticmethod
    def _message(payload: dict[str, Any], fallback: str) -> str:
        return str(
            payload.get("message")
            or payload.get("msg")
            or fallback
        )

    async def verify_login(self) -> dict[str, Any]:
        """验证 Cookie，并返回不包含凭据的账号摘要。"""
        if not self.cookie:
            return {"success": False, "error": "Cookie 不能为空"}
        try:
            response = await self._get_client().get(ACCOUNT_URL)
            payload, error = self._json_response(response, "验证登录")
            if error:
                LOGGER.warning("夸克验证登录失败: %s", error)
                return {"success": False, "error": error}
            assert payload is not None
            data = payload.get("data")
            if not self._success(payload) or not isinstance(data, dict):
                err_msg = self._message(payload, "Cookie 可能已失效")
                LOGGER.warning("夸克验证登录失败: %s", err_msg)
                return {"success": False, "error": err_msg}
            self._logged_in = True
            self._username = str(
                data.get("nickname")
                or data.get("nick_name")
                or data.get("phone")
                or data.get("mobile")
                or ""
            )
            return {
                "success": True,
                "username": self._username,
                "total": data.get(
                    "total_capacity",
                    data.get("total_size", 0),
                ),
                "used": data.get(
                    "use_capacity",
                    data.get("used_size", 0),
                ),
            }
        except httpx.TimeoutException:
            return {"success": False, "error": "连接夸克超时，请稍后重试"}
        except httpx.RequestError:
            return {
                "success": False,
                "error": "无法连接夸克，请检查 NAS 网络和 DNS",
            }
        except Exception:
            return {
                "success": False,
                "error": "验证夸克登录时发生内部错误",
            }

    async def _list_by_fid(
        self,
        fid: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        params = {
            **COMMON_PARAMS,
            "pdir_fid": fid,
            "_page": page,
            "_size": page_size,
            "_sort": "file_type:asc,updated_at:desc",
        }
        response = await self._get_client().get(
            f"{DRIVE_API}/file/sort",
            params=params,
        )
        payload, error = self._json_response(response, "获取文件列表")
        if error:
            return {"success": False, "error": error}
        assert payload is not None
        if not self._success(payload):
            return {
                "success": False,
                "error": self._message(payload, "获取文件列表失败"),
            }
        return {"success": True, "payload": payload}

    def _file_entry(self, item: dict[str, Any]) -> dict[str, Any]:
        is_dir = bool(item.get("dir")) or item.get("file_type", 0) == 0
        fid = str(item.get("fid", ""))
        thumbnail_url = str(
            item.get("thumbnail")
            or item.get("big_thumbnail")
            or item.get("small_thumbnail")
            or ""
        )
        if fid and thumbnail_url:
            self._thumbnail_urls[fid] = thumbnail_url
        return {
            "name": item.get("file_name", ""),
            "fid": fid,
            "is_dir": is_dir,
            "size": item.get("size", 0),
            "mtime": item.get("updated_at", 0),
            "has_thumbnail": bool(thumbnail_url),
        }

    async def list_files(
        self,
        path: str = "/",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """获取指定路径下的文件列表。"""
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified["success"]:
                return verified
        try:
            fid = "0"
            if path != "/":
                fid = await self._resolve_path(path)
                if not fid:
                    return {
                        "success": False,
                        "error": f"路径不存在：{path}",
                    }
            result = await self._list_by_fid(fid, page, page_size)
            if not result["success"]:
                return result
            payload = result["payload"]
            data = payload.get("data", {})
            items = data.get("list", []) if isinstance(data, dict) else []
            files = [
                self._file_entry(item)
                for item in items
                if isinstance(item, dict)
            ]
            metadata = payload.get("metadata", {})
            total = (
                data.get("total")
                if isinstance(data, dict)
                else None
            )
            if total is None and isinstance(metadata, dict):
                total = metadata.get("_total")
            return {
                "success": True,
                "path": path,
                "files": files,
                "total": total if total is not None else len(files),
            }
        except httpx.TimeoutException:
            return {"success": False, "error": "获取文件列表超时"}
        except httpx.RequestError:
            return {"success": False, "error": "无法连接夸克文件接口"}
        except Exception:
            return {"success": False, "error": "读取夸克文件列表失败"}

    async def _resolve_path(self, path: str) -> str:
        parts = [part for part in path.split("/") if part]
        current_fid = "0"
        for part in parts:
            found = ""
            for page in range(1, 11):
                result = await self._list_by_fid(
                    current_fid,
                    page,
                    100,
                )
                if not result["success"]:
                    return ""
                payload = result["payload"]
                data = payload.get("data", {})
                items = (
                    data.get("list", [])
                    if isinstance(data, dict)
                    else []
                )
                if not items:
                    break
                for item in items:
                    if (
                        isinstance(item, dict)
                        and item.get("file_name") == part
                        and self._file_entry(item)["is_dir"]
                    ):
                        found = str(item.get("fid", ""))
                        break
                if found:
                    break
            if not found:
                return ""
            current_fid = found
        return current_fid

    async def get_download_url(self, fid: str) -> dict[str, Any]:
        """获取单个文件的临时下载地址。"""
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified["success"]:
                return verified
        try:
            request_profile = "desktop"
            request_user_agent = QUARK_DESKTOP_USER_AGENT
            response = await self._get_client().post(
                f"{DRIVE_API}/file/download",
                params={"pr": "ucpro", "fr": "pc"},
                json={"fids": [fid]},
                headers={"User-Agent": request_user_agent},
            )
            self._refresh_cookie_from_response(response)
            LOGGER.info(
                "夸克 fid=%s profile=%s HTTP %d",
                fid,
                request_profile,
                response.status_code,
            )
            payload, error = self._json_response(response, "获取下载链接")
            if error:
                LOGGER.warning(
                    "夸克获取下载链接失败 fid=%s: %s",
                    fid,
                    error,
                )
                return {"success": False, "error": error}
            assert payload is not None
            if not self._success(payload):
                message = self._message(payload, "获取下载链接失败")
                LOGGER.warning(
                    "夸克API返回失败 fid=%s: %s",
                    fid,
                    message,
                )
                return {
                    "success": False,
                    "error": message,
                }
            items = payload.get("data", [])
            if not isinstance(items, list) or not items:
                return {
                    "success": False,
                    "error": "夸克未返回可用的下载链接",
                }
            item = items[0]
            if not isinstance(item, dict) or not item.get("download_url"):
                return {
                    "success": False,
                    "error": "夸克返回的下载链接为空",
                }
            self._download_user_agent = request_user_agent
            LOGGER.info(
                "夸克获取下载链接成功 fid=%s profile=%s size=%s",
                fid,
                request_profile,
                item.get("size", 0),
            )
            return {
                "success": True,
                "url": item["download_url"],
                "name": item.get("file_name", ""),
                "size": item.get("size", 0),
                "fid": fid,
                "headers": self.download_headers(item["download_url"]),
                "client_profile": request_profile,
            }
        except httpx.TimeoutException:
            return {"success": False, "error": "获取下载链接超时"}
        except httpx.RequestError:
            return {"success": False, "error": "无法连接夸克下载接口"}
        except Exception:
            return {"success": False, "error": "获取夸克下载链接失败"}

    async def walk_folder(self, root_fid: str) -> dict[str, Any]:
        """递归列出文件夹中的文件，并保留相对目录。"""
        if not root_fid:
            return {"success": False, "error": "夸克文件夹 ID 无效"}
        queue: list[tuple[str, str]] = [(root_fid, "")]
        files: list[dict[str, Any]] = []
        while queue:
            current_fid, relative_dir = queue.pop(0)
            page = 1
            while True:
                result = await self._list_by_fid(
                    current_fid,
                    page,
                    100,
                )
                if not result.get("success"):
                    return result
                payload = result.get("payload", {})
                data = payload.get("data", {})
                entries = (
                    data.get("list", [])
                    if isinstance(data, dict)
                    else []
                )
                if not isinstance(entries, list):
                    return {
                        "success": False,
                        "error": "夸克文件夹列表格式异常",
                    }
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    parsed = self._file_entry(entry)
                    name = str(parsed.get("name", ""))
                    if parsed["is_dir"]:
                        child_relative = (
                            f"{relative_dir}/{name}"
                            if relative_dir
                            else name
                        )
                        queue.append(
                            (str(parsed.get("fid", "")), child_relative)
                        )
                    else:
                        parsed["relative_dir"] = relative_dir
                        files.append(parsed)
                if len(entries) < 100:
                    break
                page += 1
        return {"success": True, "files": files}

    async def search_files(
        self,
        keyword: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """搜索账号中的文件。"""
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified["success"]:
                return verified
        try:
            params = {
                **COMMON_PARAMS,
                "q": keyword,
                "_page": page,
                "_size": 50,
                "_fetch_total": 1,
                "_sort": "file_type:desc,updated_at:desc",
                "_is_hl": 1,
            }
            response = await self._get_client().get(
                f"{DRIVE_API}/file/search",
                params=params,
            )
            payload, error = self._json_response(response, "搜索文件")
            if error:
                return {"success": False, "error": error}
            assert payload is not None
            if not self._success(payload):
                return {
                    "success": False,
                    "error": self._message(payload, "搜索文件失败"),
                }
            data = payload.get("data", {})
            items = data.get("list", []) if isinstance(data, dict) else []
            files = [
                self._file_entry(item)
                for item in items
                if isinstance(item, dict)
            ]
            metadata = payload.get("metadata", {})
            total = (
                data.get("total")
                if isinstance(data, dict)
                else None
            )
            if total is None and isinstance(metadata, dict):
                total = metadata.get("_total")
            return {
                "success": True,
                "files": files,
                "total": total if total is not None else len(files),
            }
        except httpx.TimeoutException:
            return {"success": False, "error": "搜索夸克文件超时"}
        except httpx.RequestError:
            return {"success": False, "error": "无法连接夸克搜索接口"}
        except Exception:
            return {"success": False, "error": "搜索夸克文件失败"}

    async def rename(self, fid: str, new_name: str) -> dict[str, Any]:
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified.get("success"):
                return verified
        try:
            response = await self._get_client().post(
                f"{DRIVE_API}/file/rename",
                params=COMMON_PARAMS,
                json={"fid": fid, "file_name": new_name},
            )
            payload, error = self._json_response(response, "重命名")
            if error:
                return {"success": False, "error": error}
            assert payload is not None
            if not self._success(payload):
                return {"success": False, "error": self._message(payload, "重命名失败")}
            self._thumbnail_urls.pop(fid, None)
            return {"success": True, "name": new_name}
        except httpx.HTTPError:
            return {"success": False, "error": "夸克重命名请求失败"}

    async def fetch_thumbnail(self, fid: str) -> dict[str, Any]:
        url = self._thumbnail_urls.get(fid, "")
        if not url:
            return {"success": False, "error": "缩略图不存在"}
        try:
            response = await self._get_client().get(url)
            if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
                return {"success": False, "error": "缩略图读取失败"}
            return {
                "success": True,
                "content": response.content,
                "content_type": response.headers.get("content-type", "image/jpeg"),
            }
        except httpx.HTTPError:
            return {"success": False, "error": "缩略图读取失败"}
