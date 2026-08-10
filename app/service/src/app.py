# Original author: xiaocheng154
# Project: https://github.com/xiaocheng154/nas-cloud-downloader
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from aria2_rpc import Aria2Client
from baidu import BAIDU_PCS_USER_AGENT, BaiduPanClient
from config_store import (
    CredentialStore,
    SettingsStore,
    SettingsValidationError,
)
from credential_parser import extract_baidu_credentials, normalize_cookie
from diagnostics import (
    build_diagnostic_zip,
    clear_logs,
    configure_logging,
    run_diagnostics,
    tail_log,
)
from alipan import AlipanPanClient
from alipan_qr import AlipanQrLoginManager
from cloud_qr import CloudQrLoginManager
from downloader import DownloadManager
from local_files import LocalFileManager
from onboarding import BaiduGuideStore, OnboardingStore
from quark import QuarkPanClient


APP_VERSION = "1.5.7"
STARTED_AT = time.time()
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DOWNLOAD_DIR_FILE = Path(
    os.environ.get("DOWNLOAD_DIR_FILE", str(CONFIG_DIR.parent / "download_dir"))
)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

settings_store = SettingsStore(CONFIG_DIR)
credential_store = CredentialStore(CONFIG_DIR)
onboarding_store = OnboardingStore(CONFIG_DIR)
baidu_guide_store = BaiduGuideStore(CONFIG_DIR)
logger = configure_logging(settings_store.load(), CONFIG_DIR)
aria2_client = Aria2Client()


def _sync_aria2_settings():
    settings = settings_store.load()
    aria2_client.url = settings.aria2_rpc_url or "http://127.0.0.1:6800/jsonrpc"
    secret = settings.aria2_secret or ""
    secret_file_value = os.environ.get("ARIA2_SECRET_FILE", "")
    secret_file = Path(secret_file_value) if secret_file_value else None
    using_bundled = bool(os.environ.get("ARIA2_BIN")) and aria2_client.url == "http://127.0.0.1:6800/jsonrpc"
    if secret_file and secret_file.is_file() and (using_bundled or not secret):
        try:
            # The lifecycle script generates the bundled service secret. It must
            # take precedence over a stale custom value saved in settings.
            secret = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            if not secret:
                secret = ""
    aria2_client.secret = secret


dl_manager = DownloadManager(
    download_dir=DOWNLOAD_DIR,
    settings_store=settings_store,
    aria2_client=aria2_client,
)
local_file_manager = LocalFileManager(DOWNLOAD_DIR)
alipan_qr_manager = AlipanQrLoginManager()
cloud_qr_manager = CloudQrLoginManager()
clients_lock = asyncio.Lock()


def _new_baidu_client() -> BaiduPanClient:
    credentials = credential_store.get("baidu")
    settings = settings_store.load()
    return BaiduPanClient(
        bduss=credentials.get("bduss", ""),
        stoken=credentials.get("stoken", ""),
        app_id=settings.baidu_app_id,
    )


def _new_quark_client() -> QuarkPanClient:
    credentials = credential_store.get("quark")
    return QuarkPanClient(cookie=credentials.get("cookie", ""))


def _persist_alipan_refresh_token(refresh_token: str) -> None:
    credentials = credential_store.get("alipan")
    if not credentials.get("refresh_token"):
        return
    credentials["refresh_token"] = refresh_token
    credential_store.update("alipan", credentials)


def _new_alipan_client() -> AlipanPanClient:
    credentials = credential_store.get("alipan")
    settings = settings_store.load()
    return AlipanPanClient(
        refresh_token=credentials.get("refresh_token", ""),
        client_id=credentials.get("client_id", ""),
        client_secret=credentials.get("client_secret", ""),
        auth_mode=settings.alipan_auth_mode,
        device_id=credentials.get("device_id", ""),
        signature=credentials.get("signature", ""),
        on_refresh_token=_persist_alipan_refresh_token,
    )


def _baidu_download_headers() -> dict[str, str]:
    return {
        "User-Agent": BAIDU_PCS_USER_AGENT,
        "Cookie": f"BDUSS={baidu_client.bduss};",
        "Connection": "Keep-Alive",
    }


def _quark_download_headers() -> dict[str, str]:
    return quark_client.download_headers()


def _alipan_download_headers() -> dict[str, str]:
    return alipan_client.download_headers()


def _folder_relative_dir(root_name: str, relative_dir: str) -> str:
    return f"{root_name}/{relative_dir}" if relative_dir else root_name


baidu_client = _new_baidu_client()
quark_client = _new_quark_client()
alipan_client = _new_alipan_client()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _sync_aria2_settings()
    logger.info("多网盘下载器 %s started", APP_VERSION)
    try:
        yield
    finally:
        await aria2_client.close()
        await baidu_client.close()
        await quark_client.close()
        await alipan_client.close()
        await alipan_qr_manager.close()
        await cloud_qr_manager.close()
        logger.info("多网盘下载器 stopped")
        for handler in list(logger.handlers):
            handler.flush()
            handler.close()
            logger.removeHandler(handler)


app = FastAPI(
    title="多网盘下载器",
    version=APP_VERSION,
    lifespan=lifespan,
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.middleware("http")
async def require_disclaimer(request: Request, call_next):
    path = request.url.path
    is_business_api = path.startswith("/api/") and not path.startswith(
        "/api/onboarding/"
    )
    if is_business_api and onboarding_store.status()["required"]:
        return JSONResponse(
            status_code=403,
            content={
                "detail": "必须先完成新手引导并同意当前免责声明",
                "code": "DISCLAIMER_REQUIRED",
            },
        )
    return await call_next(request)


@app.get("/api/onboarding/status")
async def onboarding_status():
    return onboarding_store.status()


@app.post("/api/onboarding/accept")
async def onboarding_accept(data: dict[str, Any] = Body(...)):
    try:
        return onboarding_store.accept(
            str(data.get("version", "")),
            data.get("accepted") is True,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/guides/baidu/status")
async def baidu_guide_status():
    return baidu_guide_store.status()


@app.post("/api/guides/baidu/complete")
async def baidu_guide_complete():
    return baidu_guide_store.complete()


@app.get("/api/settings")
async def get_settings():
    return _public_settings()


def _public_settings() -> dict[str, Any]:
    settings = settings_store.load()
    result = settings.to_dict()
    result["download_dir"] = str(dl_manager.download_dir)
    result["aria2_secret"] = ""
    result["aria2_secret_configured"] = bool(settings.aria2_secret)
    return result


def _prepare_download_dir(raw_value: Any) -> Path:
    value = str(raw_value).strip()
    if not value:
        raise SettingsValidationError("下载目录不能为空")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise SettingsValidationError("下载目录必须是绝对路径")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".clouddl-write-test-",
            dir=candidate,
            delete=True,
        ):
            pass
    except OSError as exc:
        raise SettingsValidationError(f"下载目录不可写：{exc}") from exc
    return candidate.resolve()


def _persist_download_dir(download_dir: Path) -> None:
    DOWNLOAD_DIR_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{DOWNLOAD_DIR_FILE.name}.",
        suffix=".tmp",
        dir=DOWNLOAD_DIR_FILE.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{download_dir}\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, DOWNLOAD_DIR_FILE)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@app.put("/api/settings")
async def update_settings(data: dict[str, Any] = Body(...)):
    changes = dict(data)
    requested_download_dir = changes.pop("download_dir", None)
    if changes.get("aria2_secret") == "":
        changes.pop("aria2_secret")
    try:
        prepared_download_dir = (
            _prepare_download_dir(requested_download_dir)
            if requested_download_dir is not None
            else None
        )
        settings = settings_store.update(changes)
        if prepared_download_dir is not None:
            _persist_download_dir(prepared_download_dir)
            dl_manager.download_dir = prepared_download_dir
            local_file_manager.set_root(prepared_download_dir)
    except SettingsValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"保存下载目录失败：{exc}") from exc
    global logger
    logger = configure_logging(settings, CONFIG_DIR)
    _sync_aria2_settings()
    baidu_client.app_id = settings.baidu_app_id
    alipan_client.auth_mode = settings.alipan_auth_mode
    logger.info("Settings updated")
    return _public_settings()


@app.get("/api/credentials")
async def credential_status():
    return credential_store.status()


async def _replace_baidu(credentials: dict[str, Any], persist: bool) -> dict[str, Any]:
    cleaned = (
        extract_baidu_credentials(str(credentials.get("cookie", "")))
        if "cookie" in credentials
        else {
            "bduss": str(credentials.get("bduss", "")).strip(),
            "stoken": str(credentials.get("stoken", "")).strip(),
        }
    )
    candidate = BaiduPanClient(
        bduss=cleaned["bduss"],
        stoken=cleaned["stoken"],
    )
    result = await candidate.verify_login()
    if not result.get("success"):
        await candidate.close()
        raise HTTPException(400, result.get("error", "百度凭据验证失败"))
    global baidu_client
    async with clients_lock:
        previous = baidu_client
        baidu_client = candidate
        if persist:
            try:
                credential_store.update("baidu", cleaned)
            except SettingsValidationError as exc:
                baidu_client = previous
                await candidate.close()
                raise HTTPException(422, str(exc)) from exc
        await previous.close()
    logger.info("Baidu credentials updated")
    return result


async def _replace_quark(credentials: dict[str, Any], persist: bool) -> dict[str, Any]:
    cleaned = {"cookie": normalize_cookie(str(credentials.get("cookie", "")))}
    candidate = QuarkPanClient(cookie=cleaned["cookie"])
    result = await candidate.verify_login()
    if not result.get("success"):
        await candidate.close()
        raise HTTPException(400, result.get("error", "夸克凭据验证失败"))
    global quark_client
    async with clients_lock:
        previous = quark_client
        quark_client = candidate
        if persist:
            try:
                credential_store.update("quark", cleaned)
            except SettingsValidationError as exc:
                quark_client = previous
                await candidate.close()
                raise HTTPException(422, str(exc)) from exc
        await previous.close()
    logger.info("Quark credentials updated")
    return result


async def _replace_alipan(
    credentials: dict[str, Any],
    persist: bool,
    auth_mode_override: str | None = None,
) -> dict[str, Any]:
    settings = settings_store.load()
    auth_mode = auth_mode_override or settings.alipan_auth_mode
    cleaned = {
        "refresh_token": str(credentials.get("refresh_token", "")).strip(),
        "client_id": str(credentials.get("client_id", "")).strip(),
        "client_secret": str(credentials.get("client_secret", "")).strip(),
        "device_id": str(credentials.get("device_id", "")).strip(),
        "signature": str(credentials.get("signature", "")).strip(),
    }
    candidate = AlipanPanClient(
        refresh_token=cleaned["refresh_token"],
        client_id=cleaned["client_id"],
        client_secret=cleaned["client_secret"],
        auth_mode=auth_mode,
        device_id=cleaned["device_id"],
        signature=cleaned["signature"],
    )
    result = await candidate.verify_login()
    if not result.get("success"):
        await candidate.close()
        raise HTTPException(400, result.get("error", "阿里云盘凭据验证失败"))
    global alipan_client
    async with clients_lock:
        previous = alipan_client
        alipan_client = candidate
        if persist:
            try:
                cleaned["refresh_token"] = candidate.refresh_token
                credential_store.update("alipan", cleaned)
                candidate.on_refresh_token = _persist_alipan_refresh_token
                if auth_mode_override:
                    settings_store.update({"alipan_auth_mode": auth_mode_override})
            except SettingsValidationError as exc:
                alipan_client = previous
                await candidate.close()
                raise HTTPException(422, str(exc)) from exc
        await previous.close()
    logger.info("Alipan credentials updated")
    return result


@app.put("/api/credentials/{provider}")
async def update_credentials(
    provider: str,
    data: dict[str, Any] = Body(...),
):
    if provider == "baidu":
        result = await _replace_baidu(data, persist=True)
    elif provider == "quark":
        result = await _replace_quark(data, persist=True)
    elif provider == "alipan":
        result = await _replace_alipan(data, persist=True)
    else:
        raise HTTPException(404, "不支持的网盘类型")
    return {
        **credential_store.status(),
        "verified_username": result.get("username", ""),
    }


@app.delete("/api/credentials/{provider}")
async def clear_credentials(provider: str):
    try:
        credential_store.clear(provider)
    except SettingsValidationError as exc:
        raise HTTPException(404, str(exc)) from exc
    global baidu_client, quark_client, alipan_client
    async with clients_lock:
        if provider == "baidu":
            await baidu_client.close()
            baidu_client = BaiduPanClient()
        elif provider == "quark":
            await quark_client.close()
            quark_client = QuarkPanClient()
        else:
            await alipan_client.close()
            alipan_client = AlipanPanClient()
    logger.info("%s credentials cleared", provider)
    return credential_store.status()


@app.post("/api/baidu/login")
async def baidu_login(data: dict[str, Any] = Body(...)):
    return await _replace_baidu(data, persist=True)


@app.get("/api/baidu/list")
async def baidu_list(
    path: str = Query("/"),
    page: int = Query(1),
    page_size: int = Query(100),
):
    result = await baidu_client.list_files(
        path=path,
        page=page,
        page_size=page_size,
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "未知错误"))
    return result


@app.post("/api/baidu/download/{fs_id}")
async def baidu_download(fs_id: int, path: str = Query("")):
    remote_path = path.strip()
    if not remote_path.startswith("/"):
        raise HTTPException(422, "百度文件路径无效")
    link_result = await baidu_client.get_download_links(
        [fs_id],
        paths=[remote_path],
    )
    if not link_result.get("success"):
        raise HTTPException(400, link_result.get("error", "获取下载链接失败"))
    item = link_result["items"][0]
    task_id = await dl_manager.start_download(
        url=item["url"],
        filename=item["name"],
        expected_size=item.get("size"),
        remote_hash=item.get("md5") or item.get("sha256"),
        headers=_baidu_download_headers(),
        source_profile="baidu-pcs",
        baidu_app_id_used=link_result.get("app_id_used"),
    )
    return {
        "success": True,
        "task_id": task_id,
        "filename": item["name"],
        "size": item.get("size", 0),
    }


@app.post("/api/baidu/download-folder")
async def baidu_download_folder(data: dict[str, Any] = Body(...)):
    root_path = str(data.get("path", "")).strip()
    root_name = str(data.get("name", "")).strip()
    if not root_path or not root_name:
        raise HTTPException(422, "百度文件夹路径和名称不能为空")
    walked = await baidu_client.walk_folder(root_path)
    if not walked.get("success"):
        raise HTTPException(400, walked.get("error", "读取百度文件夹失败"))
    task_ids: list[str] = []
    failures: list[str] = []
    for file in walked.get("files", []):
        link_result = await baidu_client.get_download_links(
            [file.get("fs_id")],
            paths=[str(file.get("path", ""))],
        )
        if not link_result.get("success") or not link_result.get("items"):
            link_error = str(link_result.get("error", ""))
            if (
                "授权签名失效" in link_error
                or "无法获取百度账号 UID" in link_error
            ):
                raise HTTPException(400, link_error)
            failures.append(str(file.get("name", "未知文件")))
            continue
        item = link_result["items"][0]
        task_ids.append(
            await dl_manager.start_download(
                url=item["url"],
                filename=item.get("name") or file.get("name", "download"),
                expected_size=item.get("size") or file.get("size"),
                remote_hash=item.get("md5") or item.get("sha256"),
                headers=_baidu_download_headers(),
                source_profile="baidu-pcs",
                relative_dir=_folder_relative_dir(
                    root_name,
                    str(file.get("relative_dir", "")),
                ),
                baidu_app_id_used=link_result.get("app_id_used"),
            )
        )
    if not task_ids and failures:
        raise HTTPException(400, "文件夹中的文件均未能获取下载链接")
    return {
        "success": True,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "failed_count": len(failures),
    }


@app.get("/api/aria2/status")
async def aria2_status():
    _sync_aria2_settings()
    online = await aria2_client.check_connection()
    return {
        "configured": aria2_client.is_configured,
        "online": online,
        "url": aria2_client.url,
        "bundled": bool(os.environ.get("ARIA2_BIN")),
        "error": aria2_client.last_error,
    }


@app.get("/api/baidu/search")
async def baidu_search(
    keyword: str = Query(...),
    path: str = Query("/"),
    page: int = Query(1),
):
    result = await baidu_client.search_files(
        keyword=keyword,
        path=path,
        page=page,
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "搜索失败"))
    return result


@app.get("/api/baidu/status")
async def baidu_status():
    configured = credential_store.status()["baidu"]["configured"]
    verification: dict[str, Any] = {}
    if configured and not baidu_client._logged_in:
        verification = await baidu_client.verify_login()
    return {
        "logged_in": bool(
            baidu_client._logged_in or verification.get("success")
        ),
        "username": (
            baidu_client._username or verification.get("username", "")
        ),
        "configured": configured,
    }


@app.post("/api/quark/login")
async def quark_login(data: dict[str, Any] = Body(...)):
    return await _replace_quark(data, persist=True)


@app.get("/api/quark/list")
async def quark_list(
    path: str = Query("/"),
    page: int = Query(1),
    page_size: int = Query(100),
):
    result = await quark_client.list_files(
        path=path,
        page=page,
        page_size=page_size,
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "未知错误"))
    return result


@app.post("/api/quark/download/{fid}")
async def quark_download(fid: str):
    link_result = await quark_client.get_download_url(fid)
    if not link_result.get("success"):
        raise HTTPException(400, link_result.get("error", "获取下载链接失败"))
    task_id = await dl_manager.start_download(
        url=link_result["url"],
        filename=link_result["name"],
        expected_size=link_result.get("size"),
        remote_hash=link_result.get("md5") or link_result.get("sha256"),
        headers=link_result.get("headers") or _quark_download_headers(),
        source_profile=(
            f"quark-{link_result.get('client_profile', 'web')}"
        ),
        url_refresher=lambda: quark_client.get_download_url(fid),
    )
    return {
        "success": True,
        "task_id": task_id,
        "filename": link_result["name"],
        "size": link_result.get("size", 0),
    }


@app.post("/api/quark/download-folder")
async def quark_download_folder(data: dict[str, Any] = Body(...)):
    root_fid = str(data.get("fid", "")).strip()
    root_name = str(data.get("name", "")).strip()
    if not root_fid or not root_name:
        raise HTTPException(422, "夸克文件夹 ID 和名称不能为空")
    walked = await quark_client.walk_folder(root_fid)
    if not walked.get("success"):
        raise HTTPException(400, walked.get("error", "读取夸克文件夹失败"))
    task_ids: list[str] = []
    failures: list[str] = []
    for file in walked.get("files", []):
        file_fid = str(file.get("fid", ""))
        link_result = await quark_client.get_download_url(file_fid)
        if not link_result.get("success"):
            failures.append(str(file.get("name", "未知文件")))
            continue
        task_ids.append(
            await dl_manager.start_download(
                url=link_result["url"],
                filename=(
                    link_result.get("name")
                    or file.get("name", "download")
                ),
                expected_size=(
                    link_result.get("size")
                    or file.get("size")
                ),
                remote_hash=(
                    link_result.get("md5")
                    or link_result.get("sha256")
                ),
                headers=(
                    link_result.get("headers")
                    or _quark_download_headers()
                ),
                source_profile=(
                    f"quark-{link_result.get('client_profile', 'web')}"
                ),
                url_refresher=(
                    lambda refresh_fid=file_fid: quark_client.get_download_url(
                        refresh_fid
                    )
                ),
                relative_dir=_folder_relative_dir(
                    root_name,
                    str(file.get("relative_dir", "")),
                ),
            )
        )
    if not task_ids and failures:
        raise HTTPException(400, "文件夹中的文件均未能获取下载链接")
    return {
        "success": True,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "failed_count": len(failures),
    }


@app.get("/api/quark/search")
async def quark_search(
    keyword: str = Query(...),
    page: int = Query(1),
):
    result = await quark_client.search_files(keyword=keyword, page=page)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "搜索失败"))
    return result


@app.get("/api/quark/status")
async def quark_status():
    configured = credential_store.status()["quark"]["configured"]
    verification: dict[str, Any] = {}
    if configured and not quark_client._logged_in:
        verification = await quark_client.verify_login()
    return {
        "logged_in": bool(
            quark_client._logged_in or verification.get("success")
        ),
        "username": (
            quark_client._username or verification.get("username", "")
        ),
        "configured": configured,
    }


@app.post("/api/alipan/login")
async def alipan_login(data: dict[str, Any] = Body(...)):
    return await _replace_alipan(data, persist=True)


@app.post("/api/alipan/qr/start")
async def alipan_qr_start():
    try:
        return await alipan_qr_manager.start()
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        logger.warning("阿里云盘二维码生成失败: %s", type(exc).__name__)
        raise HTTPException(502, f"生成阿里云盘二维码失败：{exc}") from exc


@app.get("/api/alipan/qr/{session_id}.svg")
async def alipan_qr_image(session_id: str):
    try:
        svg = await alipan_qr_manager.image(session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/alipan/qr/{session_id}/status")
async def alipan_qr_status(session_id: str):
    try:
        result = await alipan_qr_manager.poll(session_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "查询阿里云盘扫码状态失败") from exc
    refresh_token = str(result.pop("refresh_token", ""))
    if result.get("status") != "confirmed" or not refresh_token:
        return result
    credentials = credential_store.get("alipan")
    credentials["refresh_token"] = refresh_token
    verified = await _replace_alipan(
        credentials,
        persist=True,
        auth_mode_override="refresh_token",
    )
    return {
        **result,
        "logged_in": True,
        "username": verified.get("username", ""),
    }


@app.delete("/api/alipan/qr/{session_id}")
async def alipan_qr_cancel(session_id: str):
    await alipan_qr_manager.cancel(session_id)
    return {"success": True}


@app.post("/api/{provider}/qr/start")
async def cloud_qr_start(provider: str):
    if provider not in {"baidu", "quark"}:
        raise HTTPException(404, "\u4e0d\u652f\u6301\u7684\u626b\u7801\u767b\u5f55\u7c7b\u578b")
    try:
        return await cloud_qr_manager.start(provider)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        logger.warning("%s QR login start failed: %s: %s", provider, type(exc).__name__, exc)
        raise HTTPException(502, f"\u751f\u6210\u626b\u7801\u767b\u5f55\u4e8c\u7ef4\u7801\u5931\u8d25\uff1a{exc}") from exc


@app.get("/api/{provider}/qr/{session_id}/image")
async def cloud_qr_image(provider: str, session_id: str):
    if provider not in {"baidu", "quark"}:
        raise HTTPException(404, "\u4e0d\u652f\u6301\u7684\u626b\u7801\u767b\u5f55\u7c7b\u578b")
    try:
        image, media_type = await cloud_qr_manager.image(provider, session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=image,
        media_type=media_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/{provider}/qr/{session_id}/status")
async def cloud_qr_status(provider: str, session_id: str):
    if provider not in {"baidu", "quark"}:
        raise HTTPException(404, "\u4e0d\u652f\u6301\u7684\u626b\u7801\u767b\u5f55\u7c7b\u578b")
    try:
        result = await cloud_qr_manager.poll(provider, session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        logger.warning("%s QR login poll failed: %s: %s", provider, type(exc).__name__, exc)
        raise HTTPException(502, f"查询扫码状态失败：{exc}") from exc
    credential = str(result.pop("cookie", ""))
    if result.get("status") != "confirmed" or not credential:
        return result
    verified = (
        await _replace_baidu({"cookie": credential}, persist=True)
        if provider == "baidu"
        else await _replace_quark({"cookie": credential}, persist=True)
    )
    return {**result, "logged_in": True, "username": verified.get("username", "")}


@app.delete("/api/{provider}/qr/{session_id}")
async def cloud_qr_cancel(provider: str, session_id: str):
    if provider not in {"baidu", "quark"}:
        raise HTTPException(404, "\u4e0d\u652f\u6301\u7684\u626b\u7801\u767b\u5f55\u7c7b\u578b")
    await cloud_qr_manager.cancel(session_id)
    return {"success": True}


@app.get("/api/alipan/list")
async def alipan_list(
    path: str = Query("/"),
    page: int = Query(1),
    page_size: int = Query(100),
):
    result = await alipan_client.list_files(
        path=path,
        page=page,
        page_size=page_size,
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "未知错误"))
    return result


@app.post("/api/alipan/download/{file_id}")
async def alipan_download(file_id: str):
    link_result = await alipan_client.get_download_url(file_id)
    if not link_result.get("success"):
        raise HTTPException(400, link_result.get("error", "获取下载链接失败"))
    task_id = await dl_manager.start_download(
        url=link_result["url"],
        filename=link_result["name"],
        expected_size=link_result.get("size"),
        remote_hash=link_result.get("sha256") or link_result.get("content_hash"),
        headers=link_result.get("headers") or _alipan_download_headers(),
        source_profile=(
            f"alipan-{link_result.get('client_profile', 'desktop')}"
        ),
    )
    return {
        "success": True,
        "task_id": task_id,
        "filename": link_result["name"],
        "size": link_result.get("size", 0),
    }


@app.post("/api/alipan/download-folder")
async def alipan_download_folder(data: dict[str, Any] = Body(...)):
    root_fid = str(data.get("fid", "")).strip()
    root_name = str(data.get("name", "")).strip()
    if not root_fid or not root_name:
        raise HTTPException(422, "阿里云盘文件夹 ID 和名称不能为空")
    walked = await alipan_client.walk_folder(root_fid)
    if not walked.get("success"):
        raise HTTPException(400, walked.get("error", "读取阿里云盘文件夹失败"))
    task_ids: list[str] = []
    failures: list[str] = []
    for file in walked.get("files", []):
        link_result = await alipan_client.get_download_url(
            str(file.get("fid", ""))
        )
        if not link_result.get("success"):
            failures.append(str(file.get("name", "未知文件")))
            continue
        task_ids.append(
            await dl_manager.start_download(
                url=link_result["url"],
                filename=(
                    link_result.get("name")
                    or file.get("name", "download")
                ),
                expected_size=(
                    link_result.get("size")
                    or file.get("size")
                ),
                remote_hash=(
                    link_result.get("content_hash")
                    or link_result.get("sha256")
                ),
                headers=(
                    link_result.get("headers")
                    or _alipan_download_headers()
                ),
                source_profile=(
                    f"alipan-{link_result.get('client_profile', 'desktop')}"
                ),
                relative_dir=_folder_relative_dir(
                    root_name,
                    str(file.get("relative_dir", "")),
                ),
            )
        )
    if not task_ids and failures:
        raise HTTPException(400, "文件夹中的文件均未能获取下载链接")
    return {
        "success": True,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "failed_count": len(failures),
    }


@app.get("/api/alipan/search")
async def alipan_search(
    keyword: str = Query(...),
    page: int = Query(1),
):
    result = await alipan_client.search_files(keyword=keyword, page=page)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "搜索失败"))
    return result


@app.get("/api/alipan/status")
async def alipan_status():
    configured = credential_store.status()["alipan"]["configured"]
    verification: dict[str, Any] = {}
    if configured and not alipan_client._logged_in:
        verification = await alipan_client.verify_login()
    return {
        "logged_in": bool(
            alipan_client._logged_in or verification.get("success")
        ),
        "username": (
            alipan_client._username or verification.get("username", "")
        ),
        "configured": configured,
        "error": "" if (alipan_client._logged_in or verification.get("success")) else str(
            verification.get("error", "")
        ),
    }


def _validated_rename_name(value: Any) -> str:
    try:
        return LocalFileManager.validate_name(str(value))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/local/list")
async def local_list(path: str = Query("/")):
    try:
        return local_file_manager.list_files(path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/local/search")
async def local_search(keyword: str = Query(...), path: str = Query("/")):
    try:
        return local_file_manager.search(keyword, path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/local/rename")
async def local_rename(data: dict[str, Any] = Body(...)):
    try:
        return local_file_manager.rename(
            str(data.get("path", "")),
            _validated_rename_name(data.get("new_name", "")),
        )
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/local/thumbnail")
async def local_thumbnail(path: str = Query(...)):
    try:
        image_path = local_file_manager.thumbnail_path(path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(
        image_path,
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.post("/api/baidu/rename")
async def baidu_rename(data: dict[str, Any] = Body(...)):
    path = str(data.get("path", ""))
    if not path.startswith("/"):
        raise HTTPException(422, "百度文件路径无效")
    result = await baidu_client.rename(path, _validated_rename_name(data.get("new_name", "")))
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "百度重命名失败"))
    return result


@app.post("/api/quark/rename")
async def quark_rename(data: dict[str, Any] = Body(...)):
    fid = str(data.get("fid", ""))
    if not fid:
        raise HTTPException(422, "夸克文件 ID 无效")
    result = await quark_client.rename(fid, _validated_rename_name(data.get("new_name", "")))
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "夸克重命名失败"))
    return result


@app.post("/api/alipan/rename")
async def alipan_rename(data: dict[str, Any] = Body(...)):
    fid = str(data.get("fid", ""))
    if not fid:
        raise HTTPException(422, "阿里云盘文件 ID 无效")
    result = await alipan_client.rename(fid, _validated_rename_name(data.get("new_name", "")))
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "阿里云盘重命名失败"))
    return result


@app.get("/api/{provider}/thumbnail")
async def remote_thumbnail(
    provider: str,
    key: str = Query(...),
):
    clients = {"baidu": baidu_client, "quark": quark_client, "alipan": alipan_client}
    client = clients.get(provider)
    if client is None:
        raise HTTPException(404, "不支持的缩略图来源")
    result = await client.fetch_thumbnail(key)
    if not result.get("success"):
        raise HTTPException(404, result.get("error", "缩略图不存在"))
    content_type = str(result.get("content_type", "image/jpeg")).split(";", 1)[0]
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    return Response(
        content=result["content"],
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/api/downloads")
async def list_downloads():
    return {
        "tasks": dl_manager.list_tasks(),
        "download_directory": str(dl_manager.download_dir),
    }


@app.get("/api/downloads/{task_id}")
async def download_status(task_id: str):
    return dl_manager.get_status(task_id)


@app.post("/api/downloads/{task_id}/pause")
async def pause_download(task_id: str):
    return {"success": await dl_manager.pause_download(task_id)}


@app.post("/api/downloads/{task_id}/resume")
async def resume_download(task_id: str):
    return {"success": await dl_manager.resume_download(task_id)}


@app.delete("/api/downloads/{task_id}")
async def cancel_download(task_id: str):
    return {"success": dl_manager.cancel_download(task_id)}


@app.post("/api/downloads/clear")
async def clear_completed():
    dl_manager.clear_completed()
    return {"success": True}


@app.get("/api/logs")
async def get_logs(lines: int = Query(200, ge=1, le=5000)):
    return {"content": tail_log(CONFIG_DIR, lines=lines)}


@app.get("/api/logs/download")
async def download_logs():
    return PlainTextResponse(
        tail_log(CONFIG_DIR, lines=5000),
        headers={
            "Content-Disposition": 'attachment; filename="clouddl.log"',
        },
    )


@app.delete("/api/logs")
async def delete_logs():
    clear_logs(CONFIG_DIR)
    global logger
    logger = configure_logging(settings_store.load(), CONFIG_DIR)
    logger.info("Logs cleared")
    return {"success": True}


@app.post("/api/diagnostics")
async def diagnostics():
    return await asyncio.to_thread(
        run_diagnostics,
        CONFIG_DIR,
        dl_manager.download_dir,
    )


@app.get("/api/diagnostics/export")
async def export_diagnostics():
    results = await asyncio.to_thread(
        run_diagnostics,
        CONFIG_DIR,
        dl_manager.download_dir,
    )
    content = build_diagnostic_zip(
        config_dir=CONFIG_DIR,
        settings=settings_store.load(),
        diagnostics=results,
        version=APP_VERSION,
        uptime_seconds=time.time() - STARTED_AT,
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                'attachment; filename="clouddl-diagnostics.zip"'
            )
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return (static_dir / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8686"))
    uvicorn.run(app, host="0.0.0.0", port=port)
