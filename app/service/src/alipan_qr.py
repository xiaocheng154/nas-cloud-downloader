from __future__ import annotations

import asyncio
import base64
import io
import json
import secrets
import time
from typing import Any

import httpx
import qrcode
from qrcode.image.svg import SvgPathImage


QR_HOST = "https://passport.aliyundrive.com"
GENERATE_PATH = "/newlogin/qrcode/generate.do"
QUERY_PATH = "/newlogin/qrcode/query.do"
QR_TIMEOUT_SECONDS = 300
COMMON_QUERY = {
    "appName": "aliyun_drive",
    "fromSite": "52",
    "_bx-v": "2.0.31",
}
QR_HEADERS = {
    "Referer": "https://aliyundrive.com",
    "User-Agent": (
        "AliApp(AYSD/5.8.0) com.alicloud.databox/37029260 "
        "Channel/36176927979800@rimet_android_5.8.0 "
        "language/zh-CN /Android Mobile/Xiaomi Redmi"
    ),
    "x-canary": "client=Android,app=adrive,version=v5.8.0",
}


def _nested_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    content = payload.get("content")
    if not isinstance(content, dict):
        return {}
    data = content.get("data")
    return data if isinstance(data, dict) else {}


def _find_refresh_token(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"refreshToken", "refresh_token"} and nested:
                return str(nested)
            found = _find_refresh_token(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_refresh_token(nested)
            if found:
                return found
    return ""


def decode_refresh_token(biz_ext: str) -> str:
    if not biz_ext:
        return ""
    try:
        raw = base64.b64decode(biz_ext)
    except (ValueError, TypeError):
        return ""
    for encoding in ("gb18030", "utf-8"):
        try:
            return _find_refresh_token(json.loads(raw.decode(encoding)))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return ""


def make_qr_svg(content: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    return output.getvalue()


class AlipanQrLoginManager:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=QR_HEADERS,
                timeout=20.0,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def start(self) -> dict[str, Any]:
        response = await self._get_client().get(
            f"{QR_HOST}{GENERATE_PATH}",
            params={
                **COMMON_QUERY,
                "appEntrance": "web",
                "isMobile": "false",
                "lang": "zh_CN",
                "returnUrl": "",
                "bizParams": "",
            },
        )
        response.raise_for_status()
        data = _nested_data(response.json())
        content = str(data.get("codeContent") or "")
        timestamp = str(data.get("t") or "")
        cookie_key = str(data.get("ck") or "")
        if not content or not timestamp or not cookie_key:
            raise RuntimeError("阿里云盘未返回有效登录二维码")
        session_id = secrets.token_urlsafe(24)
        async with self._lock:
            self._sessions[session_id] = {
                "t": timestamp,
                "ck": cookie_key,
                "created_at": time.monotonic(),
                "svg": make_qr_svg(content),
            }
        return {
            "success": True,
            "session_id": session_id,
            "expires_in": QR_TIMEOUT_SECONDS,
        }

    async def image(self, session_id: str) -> bytes:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session or self._expired(session):
                self._sessions.pop(session_id, None)
                raise KeyError("二维码不存在或已过期")
            return bytes(session["svg"])

    @staticmethod
    def _expired(session: dict[str, Any]) -> bool:
        return time.monotonic() - float(session["created_at"]) >= QR_TIMEOUT_SECONDS

    async def poll(self, session_id: str) -> dict[str, Any]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session or self._expired(session):
                self._sessions.pop(session_id, None)
                return {"success": False, "status": "expired", "error": "二维码已过期"}
            timestamp = str(session["t"])
            cookie_key = str(session["ck"])
        response = await self._get_client().post(
            f"{QR_HOST}{QUERY_PATH}",
            params=COMMON_QUERY,
            data={
                "t": timestamp,
                "ck": cookie_key,
                "appName": "aliyun_drive",
                "appEntrance": "web",
                "isMobile": "false",
                "lang": "zh_CN",
                "returnUrl": "",
                "fromSite": "52",
                "bizParams": "",
                "navlanguage": "zh-CN",
                "navPlatform": "MacIntel",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = _nested_data(response.json())
        status = str(data.get("qrCodeStatus") or "NEW").upper()
        mapping = {
            "NEW": ("waiting", "等待扫码"),
            "SCANED": ("scanned", "已扫码，请在手机确认"),
            "CANCELED": ("cancelled", "已取消登录"),
            "EXPIRED": ("expired", "二维码已过期"),
        }
        if status != "CONFIRMED":
            public_status, message = mapping.get(status, ("waiting", "等待扫码"))
            if public_status in {"cancelled", "expired"}:
                await self.cancel(session_id)
            return {"success": True, "status": public_status, "message": message}
        refresh_token = decode_refresh_token(str(data.get("bizExt") or ""))
        if not refresh_token:
            return {"success": True, "status": "scanned", "message": "已确认，正在获取登录凭据"}
        await self.cancel(session_id)
        return {
            "success": True,
            "status": "confirmed",
            "message": "扫码登录成功",
            "refresh_token": refresh_token,
        }

    async def cancel(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
