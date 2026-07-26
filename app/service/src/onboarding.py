from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from config_store import atomic_write_json


DISCLAIMER_VERSION = "2026-07-23"
DISCLAIMER_TITLE = "多网盘下载器使用与风险免责声明"
DISCLAIMER_PARAGRAPHS = [
    "本应用是第三方工具，并非百度网盘、夸克网盘或飞牛 fnOS 官方产品。",
    "应用通过用户主动提供的 Cookie、BDUSS 或 STOKEN 访问对应账号。凭据具有账号访问能力，请仅在可信设备上使用并自行承担保管责任。",
    "第三方服务的接口、限速、风控和账号政策可能随时变化。使用本应用可能导致登录失效、下载失败、账号限制或其他不可预期结果。",
    "用户必须确保下载、保存和使用的内容具有合法授权，并遵守所在地法律法规及相关服务协议。",
    "软件按现状提供。开发者不对账号、数据、设备、网络或其他直接及间接损失承担责任。",
]


class OnboardingStore:
    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / "onboarding.json"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def status(self) -> dict[str, Any]:
        state = self._load()
        accepted_at = state.get("accepted_at")
        required = (
            state.get("version") != DISCLAIMER_VERSION
            or not isinstance(accepted_at, (int, float))
        )
        return {
            "required": required,
            "version": DISCLAIMER_VERSION,
            "accepted_at": None if required else accepted_at,
            "title": DISCLAIMER_TITLE,
            "paragraphs": list(DISCLAIMER_PARAGRAPHS),
        }

    def accept(self, version: str, accepted: bool) -> dict[str, Any]:
        if version != DISCLAIMER_VERSION:
            raise ValueError("免责声明版本已更新，请重新阅读")
        if accepted is not True:
            raise ValueError("必须明确同意免责声明")
        atomic_write_json(
            self.path,
            {
                "version": DISCLAIMER_VERSION,
                "accepted_at": time.time(),
            },
        )
        return self.status()


class BaiduGuideStore:
    """保存百度凭据引导是否已经完成。"""

    def __init__(self, config_dir: str | Path):
        self.path = Path(config_dir) / "ui_state.json"

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def status(self) -> dict[str, Any]:
        completed_at = self._load().get("baidu_guide_completed_at")
        required = not isinstance(completed_at, (int, float))
        return {
            "required": required,
            "completed_at": None if required else completed_at,
        }

    def complete(self) -> dict[str, Any]:
        state = self._load()
        state["baidu_guide_completed_at"] = time.time()
        atomic_write_json(self.path, state)
        return self.status()
