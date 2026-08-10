from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, fields
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from credential_parser import extract_baidu_credentials, normalize_cookie
from urllib.parse import urlsplit


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DUPLICATE_POLICIES = {"error", "rename", "overwrite", "skip"}
LOG_LEVELS = {"ERROR", "WARNING", "INFO", "DEBUG"}


class SettingsValidationError(ValueError):
    pass


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_path, mode)
        except OSError:
            pass
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@dataclass(frozen=True)
class AppSettings:
    duplicate_policy: str = "error"
    reserve_space_gb: float = 50
    total_speed_limit_mbps: float = 0
    connections_per_file: int = 16
    segment_size_mb: int = 5
    max_segment_requests: int = 30
    schedule_enabled: bool = False
    schedule_start: str = "00:00"
    schedule_end: str = "00:00"
    concurrent_downloads: int = 5
    log_level: str = "INFO"
    log_retention_days: int = 7
    log_max_size_mb: int = 10
    aria2_enabled: bool = True
    aria2_rpc_url: str = "http://127.0.0.1:6800/jsonrpc"
    aria2_secret: str = ""
    baidu_app_id: int = 250528
    alipan_auth_mode: str = "refresh_token"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppSettings":
        allowed = {field.name for field in fields(cls)}
        unknown = set(raw) - allowed
        if unknown:
            raise SettingsValidationError(
                f"不支持的设置字段：{', '.join(sorted(unknown))}"
            )
        try:
            settings = cls(**raw)
        except TypeError as exc:
            raise SettingsValidationError(str(exc)) from exc
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.duplicate_policy not in DUPLICATE_POLICIES:
            raise SettingsValidationError("同名文件策略无效")
        self._number("reserve_space_gb", self.reserve_space_gb, 0, 1_000_000)
        self._number(
            "total_speed_limit_mbps",
            self.total_speed_limit_mbps,
            0,
            100_000,
        )
        self._integer("connections_per_file", self.connections_per_file, 1, 64)
        self._integer("segment_size_mb", self.segment_size_mb, 1, 50)
        self._integer("max_segment_requests", self.max_segment_requests, 1, 200)
        self._integer("concurrent_downloads", self.concurrent_downloads, 1, 50)
        self._integer("log_retention_days", self.log_retention_days, 1, 365)
        self._integer("log_max_size_mb", self.log_max_size_mb, 1, 1024)
        if type(self.schedule_enabled) is not bool:
            raise SettingsValidationError("schedule_enabled 必须是布尔值")
        if not TIME_PATTERN.fullmatch(self.schedule_start):
            raise SettingsValidationError("开始时间必须是 HH:MM")
        if not TIME_PATTERN.fullmatch(self.schedule_end):
            raise SettingsValidationError("结束时间必须是 HH:MM")
        if self.log_level not in LOG_LEVELS:
            raise SettingsValidationError("日志级别无效")
        if type(self.aria2_enabled) is not bool:
            raise SettingsValidationError("aria2_enabled 必须是布尔值")
        if not isinstance(self.aria2_rpc_url, str):
            raise SettingsValidationError("aria2_rpc_url 必须是字符串")
        parsed = urlsplit(self.aria2_rpc_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise SettingsValidationError("Aria2 RPC 地址格式无效")
        hostname = parsed.hostname.lower()
        try:
            is_loopback = ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = hostname == "localhost"
        if not is_loopback:
            raise SettingsValidationError("Aria2 RPC 仅允许连接本机地址")
        if not isinstance(self.aria2_secret, str):
            raise SettingsValidationError("aria2_secret 必须是字符串")
        if len(self.aria2_secret) > 4096:
            raise SettingsValidationError("aria2_secret 过长")
        self._integer("baidu_app_id", self.baidu_app_id, 1, 999999)
        if self.alipan_auth_mode not in {"refresh_token", "openapi"}:
            raise SettingsValidationError("alipan_auth_mode 无效")

    @staticmethod
    def _number(name: str, value: Any, minimum: float, maximum: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsValidationError(f"{name} 必须是数字")
        if not minimum <= value <= maximum:
            raise SettingsValidationError(
                f"{name} 必须在 {minimum} 到 {maximum} 之间"
            )

    @staticmethod
    def _integer(name: str, value: Any, minimum: int, maximum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(f"{name} 必须是整数")
        if not minimum <= value <= maximum:
            raise SettingsValidationError(
                f"{name} 必须在 {minimum} 到 {maximum} 之间"
            )


class SettingsStore:
    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / "settings.json"
        self._lock = threading.RLock()

    def load(self) -> AppSettings:
        with self._lock:
            if not self.path.exists():
                return AppSettings()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SettingsValidationError(f"读取设置失败：{exc}") from exc
            if not isinstance(raw, dict):
                raise SettingsValidationError("设置文件格式无效")
            merged = AppSettings().to_dict()
            merged.update(raw)
            return AppSettings.from_dict(merged)

    def update(self, changes: dict[str, Any]) -> AppSettings:
        if not isinstance(changes, dict):
            raise SettingsValidationError("设置内容必须是对象")
        with self._lock:
            merged = self.load().to_dict()
            merged.update(changes)
            settings = AppSettings.from_dict(merged)
            atomic_write_json(self.path, settings.to_dict())
            return settings


class CredentialStore:
    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / "credentials.json"
        self._lock = threading.RLock()

    def _load_all(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsValidationError(f"读取凭据失败：{exc}") from exc
        if not isinstance(raw, dict):
            raise SettingsValidationError("凭据文件格式无效")
        return raw

    def status(self) -> dict[str, dict[str, bool]]:
        with self._lock:
            credentials = self._load_all()
            return {
                "baidu": {"configured": bool(credentials.get("baidu", {}).get("bduss"))},
                "quark": {"configured": bool(credentials.get("quark", {}).get("cookie"))},
                "alipan": {
                    "configured": bool(credentials.get("alipan", {}).get("refresh_token"))
                },
            }

    def get(self, provider: str) -> dict[str, str]:
        if provider not in {"baidu", "quark", "alipan"}:
            raise SettingsValidationError("不支持的网盘类型")
        with self._lock:
            return dict(self._load_all().get(provider, {}))

    def update(self, provider: str, data: dict[str, Any]) -> None:
        if provider == "baidu":
            parsed = (
                extract_baidu_credentials(str(data.get("cookie", "")))
                if "cookie" in data
                else {
                    "bduss": str(data.get("bduss", "")).strip(),
                    "stoken": str(data.get("stoken", "")).strip(),
                }
            )
            bduss = parsed["bduss"]
            stoken = parsed["stoken"]
            if not bduss:
                raise SettingsValidationError("BDUSS 不能为空")
            cleaned = {"bduss": bduss, "stoken": stoken}
        elif provider == "quark":
            cookie = normalize_cookie(str(data.get("cookie", "")))
            if not cookie:
                raise SettingsValidationError("Cookie 不能为空")
            cleaned = {"cookie": cookie}
        elif provider == "alipan":
            refresh_token = str(data.get("refresh_token", "")).strip()
            if not refresh_token:
                raise SettingsValidationError("refresh_token 不能为空")
            cleaned = {
                "refresh_token": refresh_token,
                "client_id": str(data.get("client_id", "")).strip(),
                "client_secret": str(data.get("client_secret", "")).strip(),
                "device_id": str(data.get("device_id", "")).strip(),
                "signature": str(data.get("signature", "")).strip(),
            }
        else:
            raise SettingsValidationError("不支持的网盘类型")
        with self._lock:
            credentials = self._load_all()
            credentials[provider] = cleaned
            atomic_write_json(self.path, credentials)

    def clear(self, provider: str) -> None:
        if provider not in {"baidu", "quark", "alipan"}:
            raise SettingsValidationError("不支持的网盘类型")
        with self._lock:
            credentials = self._load_all()
            credentials.pop(provider, None)
            atomic_write_json(self.path, credentials)
