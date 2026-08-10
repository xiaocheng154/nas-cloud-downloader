from __future__ import annotations

import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from cloud_qr import CloudQrLoginManager


def response(status: int, *, payload: dict | None = None, content: bytes = b"", content_type: str = "application/json") -> httpx.Response:
    request = httpx.Request("GET", "https://example.test/qr")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request)
    return httpx.Response(status, content=content, headers={"content-type": content_type}, request=request)


class CloudQrLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_quark_start_generates_local_svg(self) -> None:
        manager = CloudQrLoginManager()
        client = AsyncMock()
        client.get.return_value = response(
            200,
            payload={"status": 2000000, "data": {"members": {"token": "qr-token"}}},
        )
        with patch.object(manager, "_client", return_value=client):
            started = await manager.start("quark")
        image, media_type = await manager.image("quark", started["session_id"])
        self.assertIn(b"<svg", image)
        self.assertEqual(media_type, "image/svg+xml")
        await manager.cancel(started["session_id"])

    async def test_baidu_start_proxies_official_qr_image(self) -> None:
        manager = CloudQrLoginManager()
        client = AsyncMock()
        client.get.side_effect = [
            response(200, payload={"sign": "channel", "imgurl": "//passport.baidu.com/qr.png"}),
            response(200, content=b"\x89PNG\r\n", content_type="image/png"),
        ]
        with patch.object(manager, "_client", return_value=client):
            started = await manager.start("baidu")
        image, media_type = await manager.image("baidu", started["session_id"])
        self.assertEqual(image, b"\x89PNG\r\n")
        self.assertEqual(media_type, "image/png")
        await manager.cancel(started["session_id"])

    async def test_quark_confirm_returns_cookie_only_to_backend_caller(self) -> None:
        manager = CloudQrLoginManager()
        client = AsyncMock()
        client.cookies = httpx.Cookies()
        client.cookies.set("__uid", "user", domain=".quark.cn")
        client.cookies.set("__puus", "session", domain="pan.quark.cn")
        client.get.side_effect = [
            response(200, payload={"status": 2000000, "message": "ok", "data": {"members": {"service_ticket": "ticket"}}}),
            response(200, payload={"status": 200, "data": {"nickname": "user"}}),
        ]
        started = await manager._store({
            "provider": "quark", "token": "token", "client": client,
            "image": b"svg", "media_type": "image/svg+xml",
        })
        result = await manager.poll("quark", started["session_id"])
        self.assertEqual(result["status"], "confirmed")
        self.assertIn("__uid=user", result["cookie"])
        self.assertNotIn(started["session_id"], manager._sessions)

    async def test_baidu_waiting_status_has_no_credentials(self) -> None:
        manager = CloudQrLoginManager()
        client = AsyncMock()
        client.get.return_value = response(200, payload={"errno": 1})
        started = await manager._store({
            "provider": "baidu", "token": "channel", "client": client,
            "image": b"png", "media_type": "image/png",
        })
        result = await manager.poll("baidu", started["session_id"])
        self.assertEqual(result["status"], "waiting")
        self.assertNotIn("cookie", result)
        await manager.cancel(started["session_id"])


if __name__ == "__main__":
    unittest.main()
