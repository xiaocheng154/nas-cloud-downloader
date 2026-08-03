import json
import hashlib
import logging
import posixpath
import time
import httpx

BAIDU_API = "https://pan.baidu.com/api"
BAIDU_PCS_FILE_API = "https://d.pcs.baidu.com/rest/2.0/pcs/file"
BAIDU_TIEBA_LOGIN_API = "https://tieba.baidu.com/c/s/login"
BAIDU_PAN_APP_ID = 250528
BAIDU_PCS_USER_AGENT = "softxm;netdisk"
_LOCATE_SIGN_SALT = "ebrcUYiuxaZv2XGu7KIYKxUrqfnOfpDF"
LOGGER = logging.getLogger("clouddl.baidu")
_SENSITIVE_LOG_KEYS = ("cookie", "token", "dlink", "url", "sign", "auth")


def _safe_response_preview(value: object) -> str:
    def clean(item: object) -> object:
        if isinstance(item, dict):
            return {
                str(key): (
                    "[REDACTED]"
                    if any(part in str(key).lower() for part in _SENSITIVE_LOG_KEYS)
                    else clean(nested)
                )
                for key, nested in item.items()
            }
        if isinstance(item, list):
            return [clean(nested) for nested in item[:10]]
        return item

    return json.dumps(clean(value), ensure_ascii=False)[:200]

class BaiduPanClient:
    """百度网盘客户端 - 使用 BDUSS + STOKEN 认证"""

    def __init__(self, bduss: str = "", stoken: str = "", app_id: int = 250528):
        self.bduss = bduss
        self.stoken = stoken
        self.app_id = app_id
        self._client = None
        self._logged_in = False
        self._username = ""
        self._user_id: int | None = None
        self._thumbnail_urls: dict[str, str] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://pan.baidu.com/",
                    "Origin": "https://pan.baidu.com",
                    "Cookie": f"BDUSS={self.bduss}; STOKEN={self.stoken}",
                },
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def verify_login(self) -> dict:
        try:
            r = await self._get_client().get(f"{BAIDU_API}/quota", params={"checkfree": 1, "checkexpire": 1})
            d = r.json()
            if d.get("errno") == 0:
                self._logged_in = True
                try:
                    ir = await self._get_client().get(
                        "https://pan.baidu.com/rest/2.0/membership/user?method=get"
                    )
                    idata = ir.json()
                    self._username = idata.get("username", "") or idata.get("nickname", "")
                except Exception:
                    self._username = "百度网盘用户"
                return {
                    "success": True, "username": self._username,
                    "total": d.get("total", 0), "used": d.get("used", 0),
                    "expire": d.get("expire", False),
                }
            elif d.get("errno") == -6:
                return {"success": False, "error": "Cookie已失效，请重新获取"}
            else:
                return {"success": False, "error": f"验证失败: {d.get('errno', '未知错误')}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _fetch_user_id(self) -> int | None:
        """使用 BDUSS 从百度贴吧登录接口取得签名所需的 UID。"""
        timestamp = str(int(time.time()))
        imei_seed = 53202347234687234
        for character in self.bduss:
            imei_seed += (imei_seed << 5) + ord(character)
        imei_seed %= 10**15
        if imei_seed < 10**14:
            imei_seed += 10**14
        imei = str(imei_seed)
        client_version = "7.0.0.0"
        source = "mini_ad_wandoujia"
        data = {
            "bdusstoken": f"{self.bduss}|null",
            "channel_id": "",
            "channel_uid": "",
            "stErrorNums": "0",
            "subapp_type": "mini",
            "timestamp": f"{timestamp}922",
            "_client_type": "2",
            "_client_version": client_version,
            "_phone_imei": imei,
            "from": source,
            "model": "S3",
        }
        data["cuid"] = (
            hashlib.md5(
                (
                    f"{self.bduss}_{client_version}_{imei}_{source}"
                ).encode()
            ).hexdigest().upper()
            + "|"
            + imei[::-1]
        )
        signature_source = "".join(
            f"{key}={data[key]}" for key in sorted(data)
        )
        data["sign"] = hashlib.md5(
            f"{signature_source}tiebaclient!!!".encode()
        ).hexdigest().upper()
        try:
            response = await self._get_client().post(
                BAIDU_TIEBA_LOGIN_API,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": "ka=open",
                    "net": "1",
                    "User-Agent": "bdtb for Android 6.9.2.1",
                    "client_logid": f"{timestamp}416",
                    "Connection": "Keep-Alive",
                },
            )
            result = response.json()
            raw_user_id = result.get("user", {}).get("id")
            user_id = int(raw_user_id)
            if user_id <= 0:
                raise ValueError("invalid user id")
            self._user_id = user_id
            return user_id
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            LOGGER.warning(
                "百度 UID 获取失败: %s",
                type(exc).__name__,
            )
            return None

    def _locate_download_params(
        self,
        path: str,
        user_id: int,
        app_id: int | None = None,
    ) -> dict[str, object]:
        timestamp = str(int(time.time()))
        devuid = hashlib.md5(
            self.bduss.encode()
        ).hexdigest().upper() + "|0"
        bduss_sha1 = hashlib.sha1(self.bduss.encode()).hexdigest()
        rand = hashlib.sha1(
            (
                bduss_sha1
                + str(user_id)
                + _LOCATE_SIGN_SALT
                + timestamp
                + devuid
            ).encode()
        ).hexdigest()
        return {
            "ant": "1",
            "check_blue": "1",
            "es": "1",
            "esl": "1",
            "app_id": app_id if app_id is not None else self.app_id,
            "method": "locatedownload",
            "path": path,
            "ver": "4.0",
            "clienttype": "17",
            "channel": "0",
            "apn_id": "1_0",
            "freeisp": "0",
            "queryfree": "0",
            "use": "0",
            "time": timestamp,
            "rand": rand,
            "devuid": devuid,
            "cuid": devuid,
        }

    async def list_files(self, path: str = "/", page: int = 1, page_size: int = 100) -> dict:
        if not self._logged_in:
            v = await self.verify_login()
            if not v["success"]: return v
        try:
            r = await self._get_client().get(f"{BAIDU_API}/list", params={
                "dir": path, "page": page, "num": page_size,
                "order": "time", "desc": 1, "showempty": 0, "web": 1,
            })
            d = r.json()
            if d.get("errno") == 0:
                fl = []
                for i in d.get("list", []):
                    thumbnails = i.get("thumbs") if isinstance(i.get("thumbs"), dict) else {}
                    thumbnail_url = str(
                        thumbnails.get("url3")
                        or thumbnails.get("url2")
                        or thumbnails.get("url1")
                        or i.get("thumb_url")
                        or ""
                    )
                    item_path = str(i["path"])
                    if thumbnail_url:
                        self._thumbnail_urls[item_path] = thumbnail_url
                    fl.append({
                        "name": i["server_filename"], "path": item_path,
                        "is_dir": i["isdir"] == 1, "size": i.get("size", 0),
                        "mtime": i.get("mtime", 0), "fs_id": i.get("fs_id", 0),
                        "has_thumbnail": bool(thumbnail_url),
                    })
                return {"success": True, "path": path, "files": fl, "total": d.get("sum", len(fl))}
            return {"success": False, "error": f"获取列表失败: {d.get('errno')}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_download_links(
        self,
        fs_ids: list,
        paths: list[str] | None = None,
    ) -> dict:
        """按网盘路径获取下载链接。

        Cookie 登录不能直接使用需要 OAuth access_token 的 xpan filemetas
        接口，因此使用 PCS locatedownload，并只保留短 app_id 回退链。
        """
        if not self._logged_in:
            v = await self.verify_login()
            if not v["success"]: return v
        if (
            not paths
            or len(paths) != len(fs_ids)
            or any(
                not isinstance(path, str) or not path.startswith("/")
                for path in paths
            )
        ):
            return {
                "success": False,
                "error": "缺少有效的百度文件路径，请刷新文件列表后重试",
            }
        try:
            user_id = self._user_id or await self._fetch_user_id()
            if not user_id:
                return {
                    "success": False,
                    "error": "无法获取百度账号 UID，请重新保存百度凭据后重试",
                }
            last_errno = None
            items = []
            for fs_id, path in zip(fs_ids, paths):
                found = None
                candidate_app_ids = [self.app_id]
                if self.app_id != BAIDU_PAN_APP_ID:
                    candidate_app_ids.append(BAIDU_PAN_APP_ID)
                for candidate_app_id in candidate_app_ids:
                    try:
                        response = await self._get_client().get(
                            BAIDU_PCS_FILE_API,
                            params=self._locate_download_params(
                                path,
                                user_id,
                                candidate_app_id,
                            ),
                            headers={"User-Agent": BAIDU_PCS_USER_AGENT},
                        )
                    except httpx.HTTPError as exc:
                        last_errno = type(exc).__name__
                        LOGGER.warning(
                            "百度 PCS HTTP请求异常: %s",
                            type(exc).__name__,
                        )
                        return {
                            "success": False,
                            "error": f"获取下载链接失败: {last_errno}",
                        }
                    try:
                        result = response.json()
                    except (ValueError, json.JSONDecodeError):
                        last_errno = (
                            f"HTTP {response.status_code} 非JSON响应"
                        )
                        LOGGER.warning("百度 PCS 返回非JSON响应")
                        return {
                            "success": False,
                            "error": f"获取下载链接失败: {last_errno}",
                        }
                    urls = result.get("urls")
                    if not isinstance(urls, list):
                        urls = []
                    download_url = next(
                        (
                            entry.get("url")
                            for entry in urls
                            if isinstance(entry, dict)
                            and isinstance(entry.get("url"), str)
                            and entry.get("url")
                        ),
                        "",
                    )
                    if (
                        not download_url
                        and isinstance(result.get("url"), str)
                    ):
                        download_url = result["url"]
                    if download_url:
                        found = {
                            "name": (
                                result.get("server_filename")
                                or posixpath.basename(path.rstrip("/"))
                                or "download"
                            ),
                            "url": download_url,
                            "size": result.get("size", 0),
                            "fs_id": fs_id,
                        }
                        self.app_id = candidate_app_id
                        LOGGER.info(
                            "百度 PCS app_id=%d 获取链接成功",
                            candidate_app_id,
                        )
                        break
                    last_errno = result.get(
                        "errno",
                        result.get(
                            "error_code",
                            f"HTTP {response.status_code}",
                        ),
                    )
                    if last_errno not in (None, 0):
                        detail = _safe_response_preview(result)
                        LOGGER.warning(
                            "百度 PCS app_id=%d errno=%s, 响应: %s",
                            candidate_app_id,
                            last_errno,
                            detail,
                        )
                    if (
                        last_errno == 4
                        and candidate_app_id != BAIDU_PAN_APP_ID
                    ):
                        LOGGER.info(
                            "百度 PCS app_id=%d 无权限，"
                            "自动回退 app_id=%d",
                            candidate_app_id,
                            BAIDU_PAN_APP_ID,
                        )
                        continue
                    break
                if not found:
                    LOGGER.warning(
                        "百度 PCS 获取链接失败, errno=%s",
                        last_errno,
                    )
                    if last_errno == 31326:
                        return {
                            "success": False,
                            "error": (
                                "百度账号授权签名失效，请重新保存百度凭据后重试"
                            ),
                        }
                    return {
                        "success": False,
                        "error": f"获取下载链接失败: {last_errno}",
                    }
                items.append(found)
            return {
                "success": True,
                "items": items,
                "app_id_used": self.app_id,
            }
        except Exception as e:
            LOGGER.error("百度获取下载链接异常: %s", e)
            return {"success": False, "error": str(e)}

    async def search_files(self, keyword: str, path: str = "/", page: int = 1) -> dict:
        if not self._logged_in:
            v = await self.verify_login()
            if not v["success"]: return v
        try:
            r = await self._get_client().get(f"{BAIDU_API}/search", params={
                "key": keyword, "dir": path, "page": page, "num": 50, "recursion": 1,
            })
            d = r.json()
            if d.get("errno") == 0:
                fl = []
                for i in d.get("list", []):
                    thumbnails = i.get("thumbs") if isinstance(i.get("thumbs"), dict) else {}
                    thumbnail_url = str(thumbnails.get("url3") or thumbnails.get("url2") or "")
                    item_path = str(i["path"])
                    if thumbnail_url:
                        self._thumbnail_urls[item_path] = thumbnail_url
                    fl.append({
                        "name": i["server_filename"], "path": item_path,
                        "is_dir": i["isdir"] == 1, "size": i.get("size", 0),
                        "fs_id": i.get("fs_id", 0),
                        "has_thumbnail": bool(thumbnail_url),
                    })
                return {"success": True, "files": fl, "total": d.get("sum", 0)}
            return {"success": False, "error": f"搜索失败: {d.get('errno')}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def rename(self, path: str, new_name: str) -> dict:
        if not self._logged_in:
            verified = await self.verify_login()
            if not verified.get("success"):
                return verified
        try:
            response = await self._get_client().post(
                f"{BAIDU_API}/filemanager",
                params={"opera": "rename", "async": 2, "onnest": "fail"},
                data={
                    "filelist": json.dumps(
                        [{"path": path, "newname": new_name}],
                        ensure_ascii=False,
                    )
                },
            )
            payload = response.json()
            if payload.get("errno") == 0:
                self._thumbnail_urls.pop(path, None)
                return {"success": True, "name": new_name}
            return {"success": False, "error": f"百度重命名失败：{payload.get('errno', response.status_code)}"}
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return {"success": False, "error": f"百度重命名失败：{type(exc).__name__}"}

    async def fetch_thumbnail(self, path: str) -> dict:
        url = self._thumbnail_urls.get(path, "")
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

    async def walk_folder(self, root_path: str) -> dict:
        """递归列出文件夹中的文件，并保留相对目录。"""
        if not root_path.startswith("/"):
            return {"success": False, "error": "百度文件夹路径无效"}
        queue: list[tuple[str, str]] = [(root_path, "")]
        files: list[dict] = []
        while queue:
            current_path, relative_dir = queue.pop(0)
            page = 1
            while True:
                result = await self.list_files(
                    path=current_path,
                    page=page,
                    page_size=100,
                )
                if not result.get("success"):
                    return result
                entries = result.get("files", [])
                if not isinstance(entries, list):
                    return {
                        "success": False,
                        "error": "百度文件夹列表格式异常",
                    }
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name", ""))
                    if entry.get("is_dir"):
                        child_relative = (
                            f"{relative_dir}/{name}"
                            if relative_dir
                            else name
                        )
                        queue.append(
                            (str(entry.get("path", "")), child_relative)
                        )
                    else:
                        item = dict(entry)
                        item["relative_dir"] = relative_dir
                        files.append(item)
                if len(entries) < 100:
                    break
                page += 1
        return {"success": True, "files": files}
