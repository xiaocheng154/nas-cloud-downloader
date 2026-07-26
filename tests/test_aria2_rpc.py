from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from aria2_rpc import Aria2Client, Aria2Error


class Aria2ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_defaults(self) -> None:
        client = Aria2Client()
        self.assertEqual(client.url, "http://127.0.0.1:6800/jsonrpc")
        self.assertEqual(client.secret, "")
        self.assertFalse(client.is_online)

    async def test_init_with_secret(self) -> None:
        client = Aria2Client(
            url="http://10.0.0.1:6800/jsonrpc",
            secret="mysecret",
        )
        self.assertEqual(client.url, "http://10.0.0.1:6800/jsonrpc")
        self.assertEqual(client.secret, "mysecret")
        self.assertTrue(client.is_configured)

    async def test_build_params_without_secret(self) -> None:
        client = Aria2Client()
        params = client._build_params(["arg1"])
        self.assertEqual(params, ["arg1"])

    async def test_build_params_with_secret(self) -> None:
        client = Aria2Client(secret="mytoken")
        params = client._build_params(["arg1"])
        self.assertEqual(params, ["token:mytoken", "arg1"])

    async def test_build_params_empty(self) -> None:
        client = Aria2Client()
        params = client._build_params([])
        self.assertEqual(params, [])

    async def test_next_id_increments(self) -> None:
        client = Aria2Client()
        self.assertEqual(client._next_id(), 1)
        self.assertEqual(client._next_id(), 2)
        self.assertEqual(client._next_id(), 3)

    async def test_close_idempotent(self) -> None:
        client = Aria2Client()
        # Should not raise when client was never used
        await client.close()

    async def test_check_connection_offline(self) -> None:
        client = Aria2Client(url="http://127.0.0.1:1")
        online = await client.check_connection()
        self.assertFalse(online)

    async def test_get_global_stat_empty(self) -> None:
        client = Aria2Client(url="http://127.0.0.1:1")
        with self.assertRaises(Aria2Error):
            await client.get_global_stat()

    async def test_is_configured(self) -> None:
        client = Aria2Client(url="", secret="")
        self.assertFalse(client.is_configured)

        client2 = Aria2Client(url="http://localhost:6800/jsonrpc")
        self.assertTrue(client2.is_configured)

    async def test_call_rejects_http_errors_and_invalid_jsonrpc_shapes(self) -> None:
        import httpx

        client = Aria2Client()
        response = Mock()
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "unauthorized",
            request=httpx.Request("POST", client.url),
            response=httpx.Response(401),
        )
        transport = AsyncMock()
        transport.post.return_value = response
        client._get_client = AsyncMock(return_value=transport)
        with self.assertRaises(Aria2Error):
            await client.get_global_stat()

        response.raise_for_status.side_effect = None
        response.json.return_value = ["not", "an", "object"]
        with self.assertRaises(Aria2Error):
            await client.get_global_stat()


if __name__ == "__main__":
    unittest.main()
