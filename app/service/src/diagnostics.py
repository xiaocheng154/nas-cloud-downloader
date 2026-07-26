from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx

from config_store import AppSettings


LOGGER_NAME = "clouddl"
LOG_FILENAME = "clouddl.log"
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(BDUSS|STOKEN|cookie)\s*[:=]\s*([^\r\n]+)"
)
URL_ASSIGNMENT = re.compile(r"(?i)\b(url|download_url)\s*=\s*https?://\S+")
QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|sign|auth|access_token|bduss|stoken)=)[^&\s]+"
)


def redact_text(value: str) -> str:
    redacted = SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        str(value),
    )
    redacted = URL_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )
    return QUERY_SECRET.sub(r"\1[REDACTED]", redacted)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def _log_dir(config_dir: str | Path) -> Path:
    directory = Path(config_dir) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def configure_logging(
    settings: AppSettings,
    config_dir: str | Path,
) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, settings.log_level))
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    log_directory = _log_dir(config_dir)
    cutoff = time.time() - settings.log_retention_days * 86400
    for path in log_directory.glob(f"{LOG_FILENAME}*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass

    handler = RotatingFileHandler(
        log_directory / LOG_FILENAME,
        maxBytes=settings.log_max_size_mb * 1024**2,
        backupCount=max(1, min(settings.log_retention_days, 30)),
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    return logger


def tail_log(config_dir: str | Path, lines: int = 200) -> str:
    path = Path(config_dir) / "logs" / LOG_FILENAME
    if not path.exists():
        return ""
    bounded = max(1, min(int(lines), 5000))
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return redact_text("\n".join(content[-bounded:]))


def clear_logs(config_dir: str | Path) -> None:
    directory = _log_dir(config_dir)
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    for path in directory.glob(f"{LOG_FILENAME}*"):
        try:
            path.unlink()
        except OSError:
            pass


def _directory_check(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "writable": False,
    }
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".clouddl-write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        result["writable"] = True
        usage = shutil.disk_usage(path)
        result["free_bytes"] = usage.free
        result["total_bytes"] = usage.total
    except OSError as exc:
        result["error"] = str(exc)
    result["ok"] = result["exists"] and result["writable"]
    return result


def _aria2_check() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["aria2c", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first_line = (result.stdout or result.stderr).splitlines()
        return {
            "ok": result.returncode == 0,
            "version": first_line[0] if first_line else "",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}


def _network_check() -> dict[str, Any]:
    started = time.monotonic()
    try:
        with httpx.Client(timeout=5, follow_redirects=True) as client:
            response = client.head("https://pan.baidu.com")
        return {
            "ok": response.status_code < 500,
            "status_code": response.status_code,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}


def run_diagnostics(
    config_dir: str | Path,
    download_dir: str | Path,
) -> dict[str, Any]:
    return {
        "config_directory": _directory_check(Path(config_dir)),
        "download_directory": _directory_check(Path(download_dir)),
        "aria2": _aria2_check(),
        "network": _network_check(),
    }


def build_diagnostic_zip(
    *,
    config_dir: str | Path,
    settings: AppSettings,
    diagnostics: dict[str, Any],
    version: str,
    uptime_seconds: float,
) -> bytes:
    safe_settings = settings.to_dict()
    safe_settings["aria2_secret"] = (
        "[REDACTED]" if settings.aria2_secret else ""
    )
    system = {
        "app_version": version,
        "uptime_seconds": round(max(0, uptime_seconds), 1),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    files = {
        "system.json": json.dumps(
            system,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
        "settings.json": json.dumps(
            safe_settings,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
        "diagnostics.json": json.dumps(
            diagnostics,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
        "clouddl.log": tail_log(config_dir, lines=5000).encode("utf-8"),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()
