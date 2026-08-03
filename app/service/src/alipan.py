from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

WEBSV_REFRESH = "https://websv.aliyundrive.com/token/refresh"
ALIPAN_REFRESH = "https://auth.alipan.com/v2/account/token"
OPENAPI_REFRESH_URLS = (
    "https://open.aliyundrive.com/idp/oauth/access_token",
    "https://openapi.alipan.com/oauth/access_token",
)

PRIVATE_API = {
    "name": "private",
    "base": "https://api.aliyundrive.com",
    "user": "/v2/user/get",
    "list": "/adrive/v3/file/list",
    "search": "/v2/file/search",
    "download": "/v2/file/get_download_url",
    "rename": "/adrive/v2/file/update",
}
OPEN_API = {
    "name": "openapi",
    "base": "https://open.aliyundrive.com",
    "user": "/adrive/v1.0/user/getUserInfo",
    "list": "/adrive/v1.0/openFile/list",
    "search": "/adrive/v1.0/openFile/search",
    "download": "/adrive/v1.0/openFile/getDownloadUrl",
    "rename": "/adrive/v1.0/openFile/update",
}

DOWNLOAD_REFERER = "https://www.aliyundrive.com/"
REFRESH_HEADERS = {"x-requested-with": "XMLHttpRequest"}
PRIVATE_WEB_DOWNLOAD_LIMIT = 100 * 1024 * 1024
LOGGER = logging.getLogger("clouddl.alipan")


class AlipanPanClient:
    """阿里云盘客户端。

    refresh_token 模式使用网页私有接口，仅适合浏览及不超过 100 MiB 的下载；
    openapi 模式使用官方开放平台，支持 NAS 上的大文件直链下载。
    """

    def __init__(
        self,
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        auth_mode: str = "refresh_token",
        device_id: str = "",
        signature: str = "",
        on_refresh_token: Callable[[str], None] | None = None,
    ):
        self.refresh_token = (refresh_token or "").strip()
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.auth_mode = auth_mode if auth_mode in {"refresh_token", "openapi"} else "refresh_token"
        self.device_id = (device_id or "").strip()
        self.signature = (signature or "").strip()
        self.on_refresh_token = on_refresh_token
        self._access_token = ""
        self._drive_id = ""
        self._username = ""
        self._logged_in = False
        self._client: httpx.AsyncClient | None = None
        self._file_cache: dict[str, dict[str, Any]] = {}
        self._thumbnail_urls: dict[str, str] = {}

    def _update_refresh_token(self, token: Any) -> None:
        if not isinstance(token, str) or not token or token == self.refresh_token:
            return
        self.refresh_token = token
        if self.on_refresh_token:
            try:
                self.on_refresh_token(token)
            except Exception as exc:
                LOGGER.warning("阿里云盘新 refresh_token 持久化失败: %s", exc)

    @property
    def _family(self) -> dict[str, str]:
        return OPEN_API if self.auth_mode == "openapi" else PRIVATE_API

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/150.0.0.0 Safari/537.36"
                    ),
                    "Referer": DOWNLOAD_REFERER,
                    "Origin": DOWNLOAD_REFERER.rstrip("/"),
                },
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _refresh_access_token(self) -> str:
        if not self.refresh_token:
            raise RuntimeError("缺少 refresh_token")
        client = self._get_client()

        if self.auth_mode == "openapi":
            if not self.client_id:
                raise RuntimeError("OpenAPI 模式缺少 client_id")
            body = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
            }
            if self.client_secret:
                body["client_secret"] = self.client_secret
            last_status = 0
            for url in OPENAPI_REFRESH_URLS:
                r = await client.post(url, json=body)
                last_status = r.status_code
                if r.status_code not in (200, 201):
                    continue
                data = r.json()
                access_token = data.get("access_token") if isinstance(data, dict) else None
                if isinstance(access_token, str) and access_token:
                    new_token = data.get("refresh_token")
                    self._update_refresh_token(new_token)
                    return access_token
            raise RuntimeError(f"OpenAPI token 刷新失败：HTTP {last_status}")

        r = await client.post(
            WEBSV_REFRESH,
            json={"refresh_token": self.refresh_token},
            headers=REFRESH_HEADERS,
        )
        if r.status_code == 200:
            data = r.json()
            nested = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
            access_token = data.get("access_token") or nested.get("access_token")
            if isinstance(access_token, str) and access_token:
                new_token = data.get("refresh_token") or nested.get("refresh_token")
                self._update_refresh_token(new_token)
                return access_token

        r = await client.post(
            ALIPAN_REFRESH,
            json={"refresh_token": self.refresh_token, "grant_type": "refresh_token"},
        )
        if r.status_code == 200:
            data = r.json()
            access_token = data.get("access_token") if isinstance(data, dict) else None
            if isinstance(access_token, str) and access_token:
                new_token = data.get("refresh_token")
                self._update_refresh_token(new_token)
                return access_token
        raise RuntimeError("无法用 refresh_token 换取 access_token，凭据可能已失效")

    def _ensure_access_token(self) -> None:
        if not self._access_token:
            raise RuntimeError("尚未登录，请先验证凭据")

    def _auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        if self.auth_mode == "refresh_token":
            if self.device_id:
                headers["x-device-id"] = self.device_id
            if self.signature:
                headers["x-signature"] = self.signature
        return headers

    async def _api(self, family: dict[str, str], path_key: str, body: dict) -> dict:
        """调用指定接口；401 时刷新令牌并且只重试一次。"""
        url = f"{family['base']}{family[path_key]}"
        for attempt in range(2):
            try:
                response = await self._get_client().post(
                    url, json=body, headers=self._auth_headers()
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"阿里云盘接口调用失败：{type(exc).__name__}") from exc
            if response.status_code in (200, 201):
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("阿里云盘接口返回格式异常")
                return data
            if response.status_code == 401 and attempt == 0:
                self._access_token = await self._refresh_access_token()
                continue
            detail = ""
            try:
                payload = response.json()
                detail = str(payload.get("message") or payload.get("code") or "")
            except (ValueError, AttributeError):
                pass
            suffix = f"（{detail}）" if detail else ""
            raise RuntimeError(f"阿里云盘接口调用失败：HTTP {response.status_code}{suffix}")
        raise RuntimeError("阿里云盘接口调用失败")

    async def verify_login(self) -> dict[str, Any]:
        if not self.refresh_token and not self._access_token:
            return {"success": False, "error": "缺少阿里云盘 refresh_token"}
        try:
            if not self._access_token:
                self._access_token = await self._refresh_access_token()
            user = await self._api(self._family, "user", {})
            drive_id = user.get("default_drive_id") or user.get("default_drive") or user.get("resource_drive_id") or ""
            if not drive_id:
                return {"success": False, "error": "阿里云盘未返回可用网盘空间"}
            self._drive_id = str(drive_id)
            self._username = str(
                user.get("user_name") or user.get("nick_name") or user.get("name")
                or user.get("user_id") or "阿里云盘用户"
            )
            self._logged_in = True
            return {
                "success": True,
                "username": self._username,
                "drive_id": self._drive_id,
                "auth_mode": self.auth_mode,
                "total": user.get("total_size", user.get("total_capacity", 0)),
                "used": user.get("used_size", user.get("use_capacity", 0)),
            }
        except Exception as exc:
            LOGGER.warning("阿里云盘验证登录失败: %s", exc)
            return {"success": False, "error": str(exc)}

    async def _list_by_parent(self, parent_id: str, page: int, page_size: int) -> dict:
        self._ensure_access_token()
        marker = ""
        data: dict[str, Any] = {}
        for _ in range(max(1, page)):
            body = {
                "drive_id": self._drive_id,
                "parent_file_id": parent_id,
                "limit": min(max(1, page_size), 100),
                "order_by": "name",
                "order_direction": "ASC",
                "fields": "*",
            }
            if marker:
                body["marker"] = marker
            data = await self._api(self._family, "list", body)
            marker = str(data.get("next_marker") or "")
            if not marker:
                break
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("阿里云盘文件列表格式异常")
        for item in items:
            if isinstance(item, dict) and item.get("file_id"):
                file_id = str(item["file_id"])
                self._file_cache[file_id] = item
                thumbnail_url = str(item.get("thumbnail") or item.get("thumb_url") or "")
                if thumbnail_url:
                    self._thumbnail_urls[file_id] = thumbnail_url
        return {
            "success": True,
            "items": items,
            "total": len(items),
            "next_marker": marker,
            "family": self._family["name"],
        }

    def _file_entry(self, item: dict[str, Any]) -> dict[str, Any]:
        file_id = str(item.get("file_id", ""))
        return {
            "name": item.get("name", ""),
            "fid": file_id,
            "is_dir": item.get("type") == "folder",
            "size": item.get("size", 0),
            "mtime": item.get("updated_at", 0),
            "has_thumbnail": file_id in self._thumbnail_urls,
        }

    async def _resolve_path(self, path: str) -> str:
        if path in ("", "/"):
            return "root"
        current_id = "root"
        for part in (piece for piece in path.split("/") if piece):
            found = ""
            page = 1
            while True:
                result = await self._list_by_parent(current_id, page, 100)
                for item in result["items"]:
                    if item.get("type") == "folder" and item.get("name") == part:
                        found = str(item.get("file_id", ""))
                        break
                if found or not result.get("next_marker"):
                    break
                page += 1
            if not found:
                return ""
            current_id = found
        return current_id

    async def list_files(self, path: str = "/", page: int = 1, page_size: int = 100) -> dict:
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified["success"]:
                return verified
        try:
            parent_id = "root" if path in ("", "/") else await self._resolve_path(path)
            if not parent_id:
                return {"success": False, "error": f"路径不存在：{path}"}
            result = await self._list_by_parent(parent_id, page, page_size)
            files = [self._file_entry(item) for item in result["items"] if isinstance(item, dict)]
            return {
                "success": True,
                "path": path,
                "files": files,
                "total": len(files),
                "next_marker": result.get("next_marker", ""),
                "family": result["family"],
            }
        except Exception as exc:
            LOGGER.warning("阿里云盘列目录失败: %s", exc)
            return {"success": False, "error": str(exc)}

    async def search_files(self, keyword: str, page: int = 1) -> dict:
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified["success"]:
                return verified
        try:
            data = await self._api(
                self._family,
                "search",
                {
                    "drive_id": self._drive_id,
                    "query": keyword,
                    "limit": min(max(1, page) * 50, 100),
                    "order_by": "name",
                    "order_direction": "ASC",
                },
            )
            items = data.get("items")
            if not isinstance(items, list):
                return {"success": False, "error": "阿里云盘搜索返回格式异常"}
            for item in items:
                if isinstance(item, dict) and item.get("file_id"):
                    file_id = str(item["file_id"])
                    self._file_cache[file_id] = item
                    thumbnail_url = str(item.get("thumbnail") or item.get("thumb_url") or "")
                    if thumbnail_url:
                        self._thumbnail_urls[file_id] = thumbnail_url
            files = [self._file_entry(item) for item in items if isinstance(item, dict)]
            return {"success": True, "files": files, "total": len(files)}
        except Exception as exc:
            LOGGER.warning("阿里云盘搜索失败: %s", exc)
            return {"success": False, "error": str(exc)}

    def download_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            "Referer": DOWNLOAD_REFERER,
        }

    async def get_download_url(self, file_id: str) -> dict[str, Any]:
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified["success"]:
                return verified
        try:
            data = await self._api(
                self._family,
                "download",
                {"drive_id": self._drive_id, "file_id": file_id},
            )
            cached = self._file_cache.get(file_id, {})
            url = data.get("cdn_url") or data.get("url") or data.get("download_url")
            size = int(data.get("size") or cached.get("size") or 0)
            name = str(data.get("name") or cached.get("name") or file_id)
            if not isinstance(url, str) or not url:
                if self.auth_mode == "refresh_token" and size >= PRIVATE_WEB_DOWNLOAD_LIMIT:
                    error = (
                        "阿里云盘网页接口不支持超过 100 MB 的文件下载；"
                        "请在设置中切换到 OpenAPI 模式后重新登录。"
                    )
                else:
                    error = "阿里云盘未返回可用下载直链，请刷新凭据后重试"
                return {"success": False, "error": error}
            LOGGER.info(
                "阿里云盘获取下载链接成功 file_id=%s mode=%s size=%d",
                file_id,
                self.auth_mode,
                size,
            )
            return {
                "success": True,
                "url": url,
                "name": name,
                "size": size,
                "fid": file_id,
                "content_hash": data.get("content_hash") or cached.get("content_hash"),
                "headers": self.download_headers(),
                "client_profile": self._family["name"],
            }
        except Exception as exc:
            LOGGER.warning("阿里云盘获取下载链接失败: %s", exc)
            return {"success": False, "error": str(exc)}

    async def rename(self, file_id: str, new_name: str) -> dict[str, Any]:
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified.get("success"):
                return verified
        try:
            data = await self._api(
                self._family,
                "rename",
                {"drive_id": self._drive_id, "file_id": file_id, "name": new_name},
            )
            cached = self._file_cache.get(file_id)
            if cached is not None:
                cached["name"] = str(data.get("name") or new_name)
            return {"success": True, "name": str(data.get("name") or new_name)}
        except Exception as exc:
            LOGGER.warning("阿里云盘重命名失败: %s", exc)
            return {"success": False, "error": str(exc)}

    async def fetch_thumbnail(self, file_id: str) -> dict[str, Any]:
        url = self._thumbnail_urls.get(file_id, "")
        if not url:
            return {"success": False, "error": "缩略图不存在"}
        try:
            response = await self._get_client().get(url, headers=self.download_headers())
            if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
                return {"success": False, "error": "缩略图读取失败"}
            return {
                "success": True,
                "content": response.content,
                "content_type": response.headers.get("content-type", "image/jpeg"),
            }
        except httpx.HTTPError:
            return {"success": False, "error": "缩略图读取失败"}

    async def walk_folder(self, root_fid: str) -> dict[str, Any]:
        if not root_fid:
            return {"success": False, "error": "阿里云盘文件夹 ID 无效"}
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified["success"]:
                return verified
        queue: list[tuple[str, str]] = [(root_fid, "")]
        files: list[dict[str, Any]] = []
        while queue:
            current_id, relative_dir = queue.pop(0)
            page = 1
            while True:
                try:
                    result = await self._list_by_parent(current_id, page, 100)
                except Exception as exc:
                    return {"success": False, "error": str(exc)}
                for entry in result["items"]:
                    if not isinstance(entry, dict):
                        continue
                    parsed = self._file_entry(entry)
                    name = str(parsed.get("name", ""))
                    if parsed["is_dir"]:
                        child_dir = f"{relative_dir}/{name}" if relative_dir else name
                        queue.append((str(parsed.get("fid", "")), child_dir))
                    else:
                        parsed["relative_dir"] = relative_dir
                        files.append(parsed)
                if not result.get("next_marker"):
                    break
                page += 1
        return {"success": True, "files": files}
