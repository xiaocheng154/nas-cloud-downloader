from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx

from alipan_qr import make_qr_svg


QR_TIMEOUT_SECONDS = 300
BAIDU_QR_URL = "https://passport.baidu.com/v2/api/getqrcode"
BAIDU_STATUS_URL = "https://passport.baidu.com/channel/unicast"
BAIDU_EXCHANGE_URL = "https://passport.baidu.com/v3/login/main/qrbdusslogin"
QUARK_TOKEN_URL = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
QUARK_STATUS_URL = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
QUARK_ACCOUNT_URL = "https://pan.quark.cn/account/info"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


def _json_or_jsonp(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = response.text.strip()
        match = re.search(r"^[^(]*\((.*)\)\s*;?$", text, re.S)
        if not match:
            raise RuntimeError("\u626b\u7801\u63a5\u53e3\u8fd4\u56de\u683c\u5f0f\u5f02\u5e38")
        body = match.group(1)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            # Baidu occasionally returns JavaScript object notation with
            # single-quoted property names and JavaScript-only backslash
            # escapes instead of strict JSON.
            body = re.sub(r"([{,]\s*)'([^'\\]+)'\s*:", r'\1"\2":', body)
            body = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', body)
            payload = json.loads(body)
    if not isinstance(payload, dict):
        raise RuntimeError("\u626b\u7801\u63a5\u53e3\u8fd4\u56de\u683c\u5f0f\u5f02\u5e38")
    return payload


def _baidu_image_url(value: str) -> str:
    """Normalize every URL shape currently returned by Baidu's QR API."""
    value = value.strip().replace("\\/", "/")
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("passport.baidu.com/"):
        return f"https://{value}"
    return urljoin("https://passport.baidu.com/", value)


def _cookie_string(client: httpx.AsyncClient, domain: str) -> str:
    values: dict[str, str] = {}
    for cookie in client.cookies.jar:
        if cookie.domain and domain in cookie.domain:
            values[cookie.name] = cookie.value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _baidu_netdisk_stoken(session: dict[str, Any]) -> str:
    raw = session.get("stokenList")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict):
        for key in ("netdisk", "pan", "cloud"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
        if len(raw) == 1:
            value = next(iter(raw.values()))
            if isinstance(value, str) and value:
                return value
    if isinstance(raw, list):
        fallback = ""
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = str(item.get("stoken") or item.get("token") or "")
            if not value:
                continue
            name = str(item.get("tpl") or item.get("name") or item.get("product") or "")
            if name in {"netdisk", "pan", "cloud"}:
                return value
            fallback = fallback or value
        if fallback:
            return fallback
    return str(session.get("stoken") or "")


class CloudQrLoginManager:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _expired(session: dict[str, Any]) -> bool:
        return time.monotonic() - float(session["created_at"]) >= QR_TIMEOUT_SECONDS

    @staticmethod
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=20.0,
            follow_redirects=True,
        )

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session["client"].aclose()

    async def start(self, provider: str) -> dict[str, Any]:
        if provider == "quark":
            return await self._start_quark()
        if provider == "baidu":
            return await self._start_baidu()
        raise ValueError("\u4e0d\u652f\u6301\u7684\u626b\u7801\u767b\u5f55\u7c7b\u578b")

    async def _store(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = secrets.token_urlsafe(24)
        session["created_at"] = time.monotonic()
        async with self._lock:
            self._sessions[session_id] = session
        return {"success": True, "session_id": session_id, "expires_in": QR_TIMEOUT_SECONDS}

    async def _start_quark(self) -> dict[str, Any]:
        client = self._client()
        try:
            response = await client.get(
                QUARK_TOKEN_URL,
                params={"client_id": "532", "v": "1.2", "request_id": str(uuid.uuid4())},
            )
            response.raise_for_status()
            payload = _json_or_jsonp(response)
            token = str(payload.get("data", {}).get("members", {}).get("token") or "")
            if payload.get("status") != 2000000 or not token:
                raise RuntimeError(str(payload.get("message") or "\u5938\u514b\u672a\u8fd4\u56de\u6709\u6548\u767b\u5f55\u4e8c\u7ef4\u7801"))
            qr_url = (
                "https://su.quark.cn/4_eMHBJ?token=" + token
                + "&client_id=532&ssb=weblogin&uc_param_str="
                + "&uc_biz_str=S%3Acustom%7COPT%3ASAREA%400%7COPT%3AIMMERSIVE%401"
            )
            return await self._store({
                "provider": "quark", "token": token, "client": client,
                "image": make_qr_svg(qr_url), "media_type": "image/svg+xml",
            })
        except Exception:
            await client.aclose()
            raise

    async def _start_baidu(self) -> dict[str, Any]:
        client = self._client()
        try:
            response = await client.get(
                BAIDU_QR_URL,
                params={"lp": "pc", "qrloginfrom": "pc", "tpl": "netdisk", "apiver": "v3"},
                headers={"Referer": "https://pan.baidu.com/"},
            )
            response.raise_for_status()
            payload = _json_or_jsonp(response)
            sign = str(payload.get("sign") or "")
            image_url = str(payload.get("imgurl") or payload.get("img") or "")
            if not sign or not image_url:
                raise RuntimeError(str(payload.get("errmsg") or "\u767e\u5ea6\u672a\u8fd4\u56de\u6709\u6548\u767b\u5f55\u4e8c\u7ef4\u7801"))
            image_response = await client.get(
                _baidu_image_url(image_url),
                headers={"Referer": "https://pan.baidu.com/"},
            )
            image_response.raise_for_status()
            content_type = image_response.headers.get("content-type", "").split(";", 1)[0]
            if not image_response.content or not content_type.startswith("image/"):
                raise RuntimeError("\u767e\u5ea6\u767b\u5f55\u4e8c\u7ef4\u7801\u8bfb\u53d6\u5931\u8d25")
            return await self._store({
                "provider": "baidu", "token": sign, "client": client,
                "image": image_response.content, "media_type": content_type,
            })
        except Exception:
            await client.aclose()
            raise

    async def image(self, provider: str, session_id: str) -> tuple[bytes, str]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session or session.get("provider") != provider or self._expired(session):
                stale = self._sessions.pop(session_id, None)
                if stale:
                    await stale["client"].aclose()
                raise KeyError("\u4e8c\u7ef4\u7801\u4e0d\u5b58\u5728\u6216\u5df2\u8fc7\u671f")
            return bytes(session["image"]), str(session["media_type"])

    async def poll(self, provider: str, session_id: str) -> dict[str, Any]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session or session.get("provider") != provider or self._expired(session):
                stale = self._sessions.pop(session_id, None)
                if stale:
                    await stale["client"].aclose()
                return {"success": False, "status": "expired", "error": "\u4e8c\u7ef4\u7801\u5df2\u8fc7\u671f"}
        if provider == "quark":
            return await self._poll_quark(session_id, session)
        return await self._poll_baidu(session_id, session)

    async def _poll_quark(self, session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        client = session["client"]
        response = await client.get(
            QUARK_STATUS_URL,
            params={
                "client_id": "532", "v": "1.2", "token": session["token"],
                "request_id": str(uuid.uuid4()),
            },
        )
        response.raise_for_status()
        payload = _json_or_jsonp(response)
        status = payload.get("status")
        members = payload.get("data", {}).get("members", {})
        ticket = str(members.get("service_ticket") or "") if isinstance(members, dict) else ""
        if status == 2000000 and ticket:
            account = await client.get(
                QUARK_ACCOUNT_URL,
                params={"st": ticket, "lw": "scan"},
                headers={"Referer": "https://pan.quark.cn/"},
            )
            account.raise_for_status()
            cookie = _cookie_string(client, "quark.cn")
            if not cookie:
                return {"success": True, "status": "scanned", "message": "\u5df2\u786e\u8ba4\uff0c\u6b63\u5728\u83b7\u53d6\u767b\u5f55\u51ed\u636e"}
            await self.cancel(session_id, close_client=False)
            await client.aclose()
            return {"success": True, "status": "confirmed", "message": "\u626b\u7801\u767b\u5f55\u6210\u529f", "cookie": cookie}
        if status in {50004002, 50004003, 50004004}:
            await self.cancel(session_id)
            return {"success": False, "status": "expired", "error": "\u4e8c\u7ef4\u7801\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u751f\u6210"}
        return {"success": True, "status": "waiting", "message": "\u7b49\u5f85\u626b\u7801\u5e76\u5728\u5938\u514b App \u4e2d\u786e\u8ba4"}

    async def _poll_baidu(self, session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        client = session["client"]
        now = str(int(time.time() * 1000))
        try:
            response = await client.get(
                BAIDU_STATUS_URL,
                params={
                    "channel_id": session["token"], "tpl": "netdisk", "apiver": "v3",
                    "tt": now, "_": now, "callback": f"bd__cbs__{now}",
                },
                headers={"Referer": "https://pan.baidu.com/"},
            )
        except httpx.ReadTimeout:
            # Baidu intentionally keeps this request open while the QR code has
            # not been scanned. Treat a long-poll timeout as a waiting state.
            return {"success": True, "status": "waiting", "message": "等待扫码"}
        response.raise_for_status()
        payload = _json_or_jsonp(response)
        errno = payload.get("errno")
        if errno in {1, "1"}:
            return {"success": True, "status": "waiting", "message": "\u7b49\u5f85\u626b\u7801"}
        if errno not in {0, "0"}:
            if errno in {2, 3, -1, "2", "3", "-1"}:
                await self.cancel(session_id)
                return {"success": False, "status": "expired", "error": "\u4e8c\u7ef4\u7801\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u751f\u6210"}
            return {"success": True, "status": "scanned", "message": "\u5df2\u626b\u7801\uff0c\u8bf7\u5728\u767e\u5ea6\u7f51\u76d8 App \u4e2d\u786e\u8ba4"}
        channel = payload.get("channel_v") or payload.get("channel") or ""
        if isinstance(channel, str):
            try:
                channel = json.loads(channel)
            except json.JSONDecodeError:
                channel = {"v": channel}
        authorization = str(channel.get("v") or channel.get("url") or "") if isinstance(channel, dict) else ""
        if not authorization:
            return {"success": True, "status": "scanned", "message": "\u5df2\u786e\u8ba4\uff0c\u6b63\u5728\u83b7\u53d6\u767b\u5f55\u51ed\u636e"}
        authorization = authorization.lstrip("/")
        exchange = await client.get(
            BAIDU_EXCHANGE_URL,
            params={
                "alg": "v3", "apiver": "v3", "bduss": authorization,
                "callback": f"bd__cbs__{now}", "loginVersion": "v4",
                "qrcode": "1", "time": now, "tpl": "netdisk",
                "traceid": "", "tt": now, "v": now,
            },
            headers={"Referer": "https://pan.baidu.com/"},
        )
        exchange.raise_for_status()
        exchange_payload = _json_or_jsonp(exchange)
        data = exchange_payload.get("data", {})
        session_data = data.get("session", {}) if isinstance(data, dict) else {}
        response_cookies = {
            cookie.name: cookie.value
            for cookie in client.cookies.jar
            if cookie.domain and "baidu.com" in cookie.domain
        }
        bduss = str(
            response_cookies.get("BDUSS")
            or response_cookies.get("BDUSS_BFESS")
            or (session_data.get("bduss") if isinstance(session_data, dict) else "")
            or ""
        )
        stoken = str(
            response_cookies.get("STOKEN")
            or (_baidu_netdisk_stoken(session_data) if isinstance(session_data, dict) else "")
            or ""
        )
        if not bduss or not stoken:
            message = str(
                exchange_payload.get("message")
                or exchange_payload.get("errInfo", {}).get("msg", "")
                or "百度未返回完整登录凭据"
            )
            await self.cancel(session_id)
            return {
                "success": False,
                "status": "expired",
                "error": f"{message}，请重新生成二维码后扫码",
            }
        cookie = f"BDUSS={bduss}; STOKEN={stoken}"
        await self.cancel(session_id, close_client=False)
        await client.aclose()
        return {"success": True, "status": "confirmed", "message": "\u626b\u7801\u767b\u5f55\u6210\u529f", "cookie": cookie}

    async def cancel(self, session_id: str, close_client: bool = True) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if close_client and session:
            await session["client"].aclose()
