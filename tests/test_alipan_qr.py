from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from alipan_qr import AlipanQrLoginManager, decode_refresh_token, make_qr_svg


def response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://passport.aliyundrive.com/test"),
    )


def wrapped(data: dict) -> dict:
    return {"content": {"data": data}}


class AlipanQrLoginTests(unittest.IsolatedAsyncioTestCase):
    def test_decode_refresh_token_from_biz_ext(self) -> None:
        encoded = base64.b64encode(
            json.dumps(
                {"pds_login_result": {"refreshToken": "private-refresh-token"}}
            ).encode("gb18030")
        ).decode()
        self.assertEqual(decode_refresh_token(encoded), "private-refresh-token")

    def test_qr_svg_is_generated_without_pillow(self) -> None:
        svg = make_qr_svg("https://passport.aliyundrive.com/login?id=test")
        self.assertIn(b"<svg", svg)
        self.assertGreater(len(svg), 500)

    async def test_start_image_and_confirmed_poll(self) -> None:
        manager = AlipanQrLoginManager()
        manager._client = AsyncMock()
        encoded = base64.b64encode(
            json.dumps({"refresh_token": "rt-confirmed"}).encode()
        ).decode()
        manager._client.get.return_value = response(
            200,
            wrapped({"codeContent": "https://example.test/qr", "t": "123", "ck": "abc"}),
        )
        manager._client.post.return_value = response(
            200,
            wrapped({"qrCodeStatus": "CONFIRMED", "bizExt": encoded}),
        )

        started = await manager.start()
        svg = await manager.image(started["session_id"])
        polled = await manager.poll(started["session_id"])

        self.assertTrue(started["success"])
        self.assertIn(b"<svg", svg)
        self.assertEqual(polled["status"], "confirmed")
        self.assertEqual(polled["refresh_token"], "rt-confirmed")
        with self.assertRaises(KeyError):
            await manager.image(started["session_id"])

    async def test_scanned_status_does_not_expose_credentials(self) -> None:
        manager = AlipanQrLoginManager()
        manager._client = AsyncMock()
        manager._client.get.return_value = response(
            200,
            wrapped({"codeContent": "https://example.test/qr", "t": "123", "ck": "abc"}),
        )
        manager._client.post.return_value = response(
            200,
            wrapped({"qrCodeStatus": "SCANED"}),
        )
        started = await manager.start()

        result = await manager.poll(started["session_id"])

        self.assertEqual(result["status"], "scanned")
        self.assertNotIn("refresh_token", result)


if __name__ == "__main__":
    unittest.main()
