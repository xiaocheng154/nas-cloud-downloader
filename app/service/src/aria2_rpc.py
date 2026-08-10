from __future__ import annotations

import json
from typing import Any

import httpx

ARIA2_DEFAULT_URL = "http://127.0.0.1:6800/jsonrpc"


class Aria2Error(Exception):
    pass


class Aria2Client:
    """Aria2 JSON-RPC client for controlling aria2 download engine."""

    def __init__(
        self,
        url: str = ARIA2_DEFAULT_URL,
        secret: str = "",
    ):
        self.url = url
        self.secret = secret
        self._request_id = 0
        self._client: httpx.AsyncClient | None = None
        self._online = False
        self._last_error = ""

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(5.0),
                headers={"Content-Type": "application/json"},
                # A NAS-wide HTTP proxy must never intercept local JSON-RPC.
                trust_env=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _build_params(self, params: list[Any]) -> list[Any]:
        if self.secret:
            return [f"token:{self.secret}", *params]
        return params

    async def _call(self, method: str, params: list[Any] | None = None) -> Any:
        client = await self._get_client()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": self._build_params(params or []),
        }
        try:
            response = await client.post(
                self.url,
                content=json.dumps(payload),
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            self._online = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise Aria2Error(f"Aria2 RPC request failed: {exc}") from exc
        if not isinstance(result, dict):
            self._online = False
            self._last_error = "Aria2 RPC returned an invalid response"
            raise Aria2Error(self._last_error)
        if "error" in result:
            err = result["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            self._last_error = f"Aria2 RPC error: {msg}"
            raise Aria2Error(self._last_error)
        self._online = True
        self._last_error = ""
        return result.get("result")

    async def check_connection(self) -> bool:
        try:
            await self._call("aria2.getVersion")
            self._online = True
            return True
        except (Aria2Error, httpx.HTTPError):
            self._online = False
            return False

    async def add_uri(
        self,
        uri: str,
        options: dict[str, Any] | None = None,
        position: int | None = None,
    ) -> str:
        params: list[Any] = [[uri]]
        if options:
            params.append(options)
        if position is not None:
            params.append(position)
        return await self._call("aria2.addUri", params)

    async def remove(self, gid: str, force: bool = False) -> str:
        method = "aria2.forceRemove" if force else "aria2.remove"
        return await self._call(method, [gid])

    async def pause(self, gid: str, force: bool = False) -> str:
        method = "aria2.forcePause" if force else "aria2.pause"
        return await self._call(method, [gid])

    async def unpause(self, gid: str) -> str:
        return await self._call("aria2.unpause", [gid])

    async def tell_status(
        self, gid: str, keys: list[str] | None = None
    ) -> dict[str, Any]:
        params: list[Any] = [gid]
        if keys:
            params.append(keys)
        return await self._call("aria2.tellStatus", params)

    async def tell_active(
        self, keys: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        if keys:
            params.append(keys)
        return await self._call("aria2.tellActive", params)

    async def tell_waiting(
        self, offset: int = 0, num: int = 100, keys: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = [offset, num]
        if keys:
            params.append(keys)
        return await self._call("aria2.tellWaiting", params)

    async def tell_stopped(
        self, offset: int = 0, num: int = 100, keys: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = [offset, num]
        if keys:
            params.append(keys)
        return await self._call("aria2.tellStopped", params)

    async def get_global_stat(self) -> dict[str, Any]:
        return await self._call("aria2.getGlobalStat")

    @property
    def is_online(self) -> bool:
        return self._online

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_configured(self) -> bool:
        return bool(self.url)
