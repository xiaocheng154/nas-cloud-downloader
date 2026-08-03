from __future__ import annotations

import asyncio
import logging
import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from config_store import AppSettings, SettingsStore


LOGGER = logging.getLogger("clouddl.downloader")
MIB = 1024**2
GIB = 1024**3
MIN_RANGE_SEGMENT = 256 * 1024
MIN_PROTOCOL_SPLIT_SIZE = MIB
PROBE_RANGE_END = 1023
TERMINAL_STATES = {"completed", "skipped", "error", "cancelled"}
MAX_SIGNED_URL_REFRESHES = 3
BAIDU_MAX_CONNECTIONS = 6
BAIDU_SEGMENT_SIZE = 4 * MIB
BAIDU_STREAM_CHUNK_SIZE = 64 * 1024


def _resume_state_path(temporary: Path) -> Path:
    return Path(f"{temporary}.resume.json")


def _write_resume_state(
    temporary: Path,
    *,
    total: int,
    remote_hash: str,
    completed: set[tuple[int, int]],
) -> None:
    state_path = _resume_state_path(temporary)
    pending_path = Path(f"{state_path}.tmp")
    payload = {
        "version": 1,
        "total": total,
        "remote_hash": remote_hash,
        "completed": [list(item) for item in sorted(completed)],
    }
    with pending_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending_path, state_path)


def _discard_resume_files(temporary: Path) -> None:
    for path in (temporary, _resume_state_path(temporary)):
        if path.exists():
            path.unlink()


def _load_resume_state(
    temporary: Path,
    *,
    total: int,
    remote_hash: str,
) -> set[tuple[int, int]]:
    state_path = _resume_state_path(temporary)
    if not temporary.is_file() or not state_path.is_file():
        if temporary.exists() or state_path.exists():
            _discard_resume_files(temporary)
        return set()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or int(payload.get("total", 0)) != total:
            raise ValueError("resume metadata mismatch")
        saved_hash = str(payload.get("remote_hash", ""))
        if remote_hash and saved_hash and saved_hash.lower() != remote_hash.lower():
            raise ValueError("resume hash mismatch")
        file_size = temporary.stat().st_size
        completed = {
            (int(item[0]), int(item[1]))
            for item in payload.get("completed", [])
            if isinstance(item, list) and len(item) == 2
        }
        if any(
            start < 0 or end < start or end >= total or file_size < end + 1
            for start, end in completed
        ):
            raise ValueError("invalid completed range")
        return completed
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        _discard_resume_files(temporary)
        return set()


def _provider_name(source_profile: str, baidu_app_id: int | None) -> str:
    if baidu_app_id or source_profile.startswith("baidu-"):
        return "baidu"
    if source_profile.startswith("quark-"):
        return "quark"
    if source_profile.startswith("alipan-"):
        return "alipan"
    return "direct"


def _task_provider(task: dict[str, Any]) -> str:
    return str(
        task.get("provider")
        or _provider_name(
            str(task.get("source_profile", "")),
            task.get("baidu_app_id_used"),
        )
    )


class DuplicateFileError(FileExistsError):
    pass


@dataclass(frozen=True)
class DuplicateDecision:
    action: str
    path: Path
    temporary_path: Path | None = None
    replace_on_success: bool = False


def _safe_filename(filename: str) -> str:
    cleaned = filename.strip()
    if (
        not cleaned
        or Path(cleaned).is_absolute()
        or Path(cleaned).name != cleaned
        or cleaned in {".", ".."}
    ):
        raise ValueError("文件名无效")
    return cleaned


def _safe_download_directory(
    download_dir: Path,
    relative_dir: str,
) -> Path:
    if not relative_dir:
        return download_dir
    relative = Path(relative_dir)
    if relative.is_absolute() or relative.drive:
        raise ValueError("下载子目录无效")
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("下载子目录无效")
    safe_parts = [_safe_filename(part) for part in parts]
    root = download_dir.resolve()
    target = download_dir.joinpath(*safe_parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError("下载子目录超出下载目录")
    return target


def _digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(MIB), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _content_matches(
    path: Path,
    expected_size: int | None,
    remote_hash: str | None,
) -> bool:
    if not remote_hash or expected_size is None or expected_size < 0:
        return False
    if path.stat().st_size != expected_size:
        return False
    normalized = remote_hash.lower().removeprefix("md5:").removeprefix("sha256:")
    if len(normalized) == 32:
        algorithm = "md5"
    elif len(normalized) == 64:
        algorithm = "sha256"
    else:
        return False
    if any(character not in "0123456789abcdef" for character in normalized):
        return False
    return _digest(path, algorithm) == normalized


def resolve_destination(
    download_dir: str | Path,
    filename: str,
    *,
    policy: str,
    expected_size: int | None = None,
    remote_hash: str | None = None,
) -> DuplicateDecision:
    directory = Path(download_dir)
    safe_name = _safe_filename(filename)
    target = directory / safe_name
    temporary = directory / f".{safe_name}.clouddl.part"
    if not target.exists():
        return DuplicateDecision("download", target, temporary, False)

    if policy == "error":
        if _content_matches(target, expected_size, remote_hash):
            return DuplicateDecision("complete", target)
        raise DuplicateFileError(
            "同名文件已存在，且无法确认内容完全相同；已保留本地文件"
        )
    if policy == "skip":
        return DuplicateDecision("skip", target)
    if policy == "overwrite":
        return DuplicateDecision("download", target, temporary, True)
    if policy == "rename":
        stem = target.stem
        suffix = target.suffix
        index = 1
        while True:
            candidate = directory / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return DuplicateDecision(
                    "download",
                    candidate,
                    directory / f".{candidate.name}.clouddl.part",
                    False,
                )
            index += 1
    raise ValueError("同名文件策略无效")


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def is_schedule_allowed(
    settings: AppSettings,
    current: datetime | None = None,
) -> bool:
    if not settings.schedule_enabled:
        return True
    start = _minutes(settings.schedule_start)
    end = _minutes(settings.schedule_end)
    if start == end:
        return True
    now = current or datetime.now()
    value = now.hour * 60 + now.minute
    if start < end:
        return start <= value < end
    return value >= start or value < end


def has_required_space(
    path: str | Path,
    *,
    remaining_bytes: int,
    reserve_space_gb: float,
    free_bytes: int | None = None,
) -> bool:
    available = (
        shutil.disk_usage(Path(path)).free if free_bytes is None else free_bytes
    )
    required = max(0, remaining_bytes) + int(reserve_space_gb * GIB)
    return available >= required


def plan_download_ranges(
    *,
    total: int,
    preferred_segment_size: int,
    connections: int,
) -> list[tuple[int, int]]:
    """按文件大小和连接数生成连续、无重叠的 Range 分片。"""
    if total <= 0:
        return []
    connection_count = max(1, connections)
    target_size = (total + connection_count - 1) // connection_count
    segment_size = min(
        max(1, preferred_segment_size),
        max(MIN_RANGE_SEGMENT, target_size),
    )
    return [
        (start, min(total - 1, start + segment_size - 1))
        for start in range(0, total, segment_size)
    ]


class SharedRateLimiter:
    def __init__(self, settings_store: SettingsStore):
        self.settings_store = settings_store
        self._lock = asyncio.Lock()
        self._window_started = time.monotonic()
        self._window_bytes = 0
        self._limit_checked_at = 0.0
        self._cached_limit_mbps = 0.0

    async def consume(self, byte_count: int) -> None:
        now = time.monotonic()
        if now - self._limit_checked_at >= 1:
            self._cached_limit_mbps = (
                self.settings_store.load().total_speed_limit_mbps
            )
            self._limit_checked_at = now
        limit_mbps = self._cached_limit_mbps
        if limit_mbps <= 0 or byte_count <= 0:
            return
        limit = int(limit_mbps * MIB)
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._window_started
            if elapsed >= 1:
                self._window_started = now
                self._window_bytes = 0
                elapsed = 0
            projected = self._window_bytes + byte_count
            if projected > limit:
                await asyncio.sleep(max(0, 1 - elapsed))
                self._window_started = time.monotonic()
                self._window_bytes = 0
            self._window_bytes += byte_count


class DownloadManager:
    def __init__(
        self,
        download_dir: str | Path = "/downloads",
        settings_store: SettingsStore | None = None,
        aria2_client: Any | None = None,
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.settings_store = settings_store or SettingsStore("/config")
        self.tasks: dict[str, dict[str, Any]] = {}
        self._active_downloads = 0
        self._slot_condition = asyncio.Condition()
        self._segment_semaphore: asyncio.Semaphore | None = None
        self._rate_limiter = SharedRateLimiter(self.settings_store)
        self._max_requests = 0
        self.aria2_client = aria2_client

    def _reload_semaphore(self) -> None:
        limit = self.settings_store.load().max_segment_requests
        if limit != self._max_requests or self._segment_semaphore is None:
            self._max_requests = limit
            self._segment_semaphore = asyncio.Semaphore(limit)

    async def start_download(
        self,
        url: str,
        filename: str,
        headers: dict[str, str] | None = None,
        expected_size: int | None = None,
        remote_hash: str | None = None,
        num_threads: int | None = None,
        relative_dir: str = "",
        source_profile: str = "",
        baidu_app_id_used: int | None = None,
        url_refresher: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex[:12]
        settings = self.settings_store.load()
        task: dict[str, Any] = {
            "id": task_id,
            "filename": filename,
            "save_path": "",
            "total_size": max(0, int(expected_size or 0)),
            "downloaded": 0,
            "status": "pending",
            "progress": 0.0,
            "speed": 0.0,
            "started_at": time.time(),
            "error": None,
            "cancel_requested": False,
            "range_supported": None,
            "connections_used": 0,
            "degradation_reason": "",
            "http_status": 0,
            "per_connection_speed": 0.0,
            "baidu_app_id_used": baidu_app_id_used,
            "provider": _provider_name(source_profile, baidu_app_id_used),
            "resumed_bytes": 0,
            "resume_available": False,
            "url_refresh_count": 0,
            "last_progress_at": None,
            "backend": "builtin",
            "connections": max(
                1,
                min(
                    int(num_threads or settings.connections_per_file),
                    64,
                ),
            ),
            "eta_seconds": None,
            "source_profile": source_profile,
            "_remote_hash": str(remote_hash or ""),
            "_url_refresher": url_refresher,
        }
        download_root = self.download_dir
        destination_dir = _safe_download_directory(
            download_root,
            relative_dir,
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        self.tasks[task_id] = task
        try:
            decision = resolve_destination(
                destination_dir,
                filename,
                policy=settings.duplicate_policy,
                expected_size=expected_size,
                remote_hash=remote_hash,
            )
        except (DuplicateFileError, ValueError) as exc:
            task["status"] = "error"
            task["error"] = str(exc)
            return task_id

        task["filename"] = decision.path.name
        task["save_path"] = str(decision.path)
        task["_download_root"] = str(download_root)
        existing_id = next(
            (
                candidate_id
                for candidate_id, existing in self.tasks.items()
                if candidate_id != task_id
                and existing.get("status") not in TERMINAL_STATES
                and existing.get("save_path") == task["save_path"]
            ),
            "",
        )
        if existing_id:
            self.tasks.pop(task_id, None)
            return existing_id
        if decision.action == "complete":
            size = decision.path.stat().st_size
            task.update(
                status="completed",
                total_size=size,
                downloaded=size,
                progress=100.0,
            )
            return task_id
        if decision.action == "skip":
            task["status"] = "skipped"
            task["error"] = "同名文件已存在，已按设置跳过"
            return task_id

        temporary = decision.temporary_path or (
            destination_dir / f".{decision.path.name}.clouddl.part"
        )
        task["_temporary_path"] = str(temporary)
        task["resume_available"] = temporary.is_file() and temporary.stat().st_size > 0
        LOGGER.info(
            "Download task %s: provider=%s file=%s size=%d resume=%s",
            task_id,
            _task_provider(task),
            decision.path.name,
            task["total_size"],
            task["resume_available"],
        )
        if (
            settings.aria2_enabled
            and self.aria2_client is not None
            and await self.aria2_client.check_connection()
        ):
            try:
                await self._start_aria2_task(
                    task,
                    url,
                    dict(headers or {}),
                    decision.path,
                    temporary,
                    num_threads,
                )
                return task_id
            except Exception:
                task["backend"] = "builtin"
        worker = asyncio.create_task(
            self._run_task(
                task,
                url,
                dict(headers or {}),
                decision.path,
                temporary,
                decision.replace_on_success,
                num_threads,
            )
        )
        task["_worker"] = worker
        return task_id

    async def _start_aria2_task(
        self,
        task: dict[str, Any],
        url: str,
        headers: dict[str, str],
        target: Path,
        temporary: Path,
        num_threads: int | None,
    ) -> None:
        self._reload_semaphore()
        settings = self.settings_store.load()
        connections = max(
            1,
            min(int(num_threads or settings.connections_per_file), 64),
        )
        options: dict[str, Any] = {
            "dir": str(temporary.parent.resolve()),
            "out": temporary.name,
            "split": str(connections),
            "max-connection-per-server": str(connections),
            "min-split-size": f"{settings.segment_size_mb}M",
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
            "continue": "true",
        }
        if headers:
            options["header"] = [
                f"{name}: {value}" for name, value in headers.items()
            ]
        gid = await self.aria2_client.add_uri(url, options)
        if not isinstance(gid, str) or not gid:
            raise IOError("Aria2 未返回有效任务编号")
        task.update(backend="aria2", gid=gid, status="queued")
        task["connections"] = connections
        task["_worker"] = asyncio.create_task(
            self._monitor_aria2_task(task, target, temporary)
        )

    async def _monitor_aria2_task(
        self,
        task: dict[str, Any],
        target: Path,
        temporary: Path,
    ) -> None:
        try:
            while True:
                if task["cancel_requested"]:
                    await self.aria2_client.remove(task["gid"], force=True)
                    raise asyncio.CancelledError
                status = await self.aria2_client.tell_status(
                    task["gid"],
                    [
                        "status",
                        "totalLength",
                        "completedLength",
                        "downloadSpeed",
                        "errorMessage",
                    ],
                )
                state = status.get("status", "")
                total = int(status.get("totalLength", 0) or 0)
                completed = int(status.get("completedLength", 0) or 0)
                speed = int(status.get("downloadSpeed", 0) or 0)
                task.update(
                    total_size=total,
                    downloaded=completed,
                    speed=speed,
                    progress=(
                        min(99.9, round(completed / total * 100, 1))
                        if total
                        else 0.0
                    ),
                    status={
                        "active": "downloading",
                        "waiting": "queued",
                        "paused": "paused",
                    }.get(state, task["status"]),
                    eta_seconds=(
                        max(0, (total - completed) / speed)
                        if total > completed and speed > 0
                        else None
                    ),
                )
                if state == "complete":
                    if not temporary.exists():
                        raise IOError("Aria2 报告完成，但未找到下载文件")
                    os.replace(temporary, target)
                    resume_state = _resume_state_path(temporary)
                    if resume_state.exists():
                        resume_state.unlink()
                    size = target.stat().st_size
                    task.update(
                        status="completed",
                        total_size=size,
                        downloaded=size,
                        progress=100.0,
                        speed=0.0,
                    )
                    return
                if state in {"error", "removed"}:
                    raise IOError(
                        status.get("errorMessage")
                        or f"Aria2 下载失败：{state}"
                    )
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            task["status"] = "cancelled"
            raise
        except Exception as exc:
            task["status"] = "error"
            task["error"] = str(exc)
        finally:
            task["resume_available"] = (
                task["status"] != "completed"
                and temporary.is_file()
                and temporary.stat().st_size > 0
            )
            aria2_state = Path(f"{temporary}.aria2")
            if task["status"] == "completed" and aria2_state.exists():
                aria2_state.unlink()

    async def _wait_for_task_slot(self, task: dict[str, Any]) -> None:
        while True:
            if task["cancel_requested"]:
                raise asyncio.CancelledError
            async with self._slot_condition:
                limit = self.settings_store.load().concurrent_downloads
                if self._active_downloads < limit:
                    self._active_downloads += 1
                    return
                task["status"] = "queued"
                try:
                    await asyncio.wait_for(
                        self._slot_condition.wait(),
                        timeout=1,
                    )
                except TimeoutError:
                    pass

    async def _release_task_slot(self) -> None:
        async with self._slot_condition:
            self._active_downloads = max(0, self._active_downloads - 1)
            self._slot_condition.notify_all()

    async def _wait_until_allowed(
        self,
        task: dict[str, Any],
        remaining_bytes: int,
    ) -> None:
        while True:
            if task["cancel_requested"]:
                raise asyncio.CancelledError
            settings = self.settings_store.load()
            if not is_schedule_allowed(settings):
                task["status"] = "waiting_schedule"
                await asyncio.sleep(1)
                continue
            if not has_required_space(
                Path(task.get("_download_root") or self.download_dir),
                remaining_bytes=remaining_bytes,
                reserve_space_gb=settings.reserve_space_gb,
            ):
                task["status"] = "paused_disk"
                await asyncio.sleep(2)
                continue
            task["status"] = "downloading"
            return

    async def _run_task(
        self,
        task: dict[str, Any],
        url: str,
        headers: dict[str, str],
        target: Path,
        temporary: Path,
        replace_on_success: bool,
        num_threads: int | None,
    ) -> None:
        slot_acquired = False
        try:
            self._reload_semaphore()
            await self._wait_for_task_slot(task)
            slot_acquired = True
            task["status"] = "connecting"
            await self._download(
                task,
                url,
                headers,
                temporary,
                num_threads,
            )
            if task["cancel_requested"]:
                raise asyncio.CancelledError
            os.replace(temporary, target)
            state_path = _resume_state_path(temporary)
            if state_path.exists():
                state_path.unlink()
            size = target.stat().st_size
            task.update(
                status="completed",
                downloaded=size,
                total_size=size,
                progress=100.0,
                speed=0.0,
                resume_available=False,
            )
        except asyncio.CancelledError:
            task["status"] = "cancelled"
            task["resume_available"] = temporary.is_file() and temporary.stat().st_size > 0
            LOGGER.info(
                "Download task %s cancelled: provider=%s resume_available=%s",
                task.get("id", "unknown"),
                _task_provider(task),
                task["resume_available"],
            )
        except Exception as exc:
            task["status"] = "error"
            public_error = self._public_error(exc)
            task["error"] = public_error
            task["resume_available"] = temporary.is_file() and temporary.stat().st_size > 0
            LOGGER.error(
                "File %s: provider=%s download failed: %s: %s; "
                "resume_available=%s refreshed=%d",
                target.name,
                _task_provider(task),
                type(exc).__name__,
                public_error,
                task["resume_available"],
                task["url_refresh_count"],
            )
        finally:
            if slot_acquired:
                await self._release_task_slot()

    async def _download(
        self,
        task: dict[str, Any],
        url: str,
        headers: dict[str, str],
        temporary: Path,
        num_threads: int | None,
    ) -> None:
        request_headers = dict(headers)
        request_headers.setdefault("User-Agent", "Mozilla/5.0")
        timeout = httpx.Timeout(connect=10, read=60, write=20, pool=10)
        async with httpx.AsyncClient(
            headers=request_headers,
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=64,
                max_keepalive_connections=32,
                keepalive_expiry=30,
            ),
        ) as client:
            total = task["total_size"]
            accepts_ranges = False
            http_status = 0
            probe_failure = ""
            if (
                total
                and str(task.get("source_profile", "")).startswith("quark-")
            ):
                quark_connections = min(
                    max(
                        1,
                        int(
                            self.settings_store.load().connections_per_file
                        ),
                    ),
                    8,
                )
                task.update(
                    total_size=total,
                    http_status=0,
                    range_supported=True,
                    connections_used=quark_connections,
                    degradation_reason="",
                )
                LOGGER.info(
                    "File %s: Quark aligned range download, size=%d",
                    os.path.basename(str(temporary)),
                    total,
                )
                current_url = url
                while True:
                    try:
                        await self._download_ranges(
                            task,
                            client,
                            current_url,
                            temporary,
                            total,
                            quark_connections,
                        )
                        return
                    except (httpx.HTTPError, IOError) as exc:
                        refresher = task.get("_url_refresher")
                        refresh_count = int(task.get("url_refresh_count", 0))
                        if not callable(refresher) or refresh_count >= MAX_SIGNED_URL_REFRESHES:
                            raise
                        LOGGER.warning(
                            "File %s: provider=quark signed URL failed (%s), "
                            "refreshing link attempt=%d/%d resumed=%d",
                            os.path.basename(str(temporary)),
                            type(exc).__name__,
                            refresh_count + 1,
                            MAX_SIGNED_URL_REFRESHES,
                            int(task.get("downloaded", 0)),
                        )
                        refreshed = await refresher()
                        if not isinstance(refreshed, dict) or not refreshed.get("success"):
                            raise IOError(
                                str(
                                    refreshed.get("error", "刷新夸克下载链接失败")
                                    if isinstance(refreshed, dict)
                                    else "刷新夸克下载链接失败"
                                )
                            ) from exc
                        refreshed_url = refreshed.get("url")
                        if not isinstance(refreshed_url, str) or not refreshed_url:
                            raise IOError("夸克刷新后未返回下载链接") from exc
                        refreshed_headers = refreshed.get("headers")
                        if isinstance(refreshed_headers, dict):
                            client.headers.update(
                                {
                                    str(name): str(value)
                                    for name, value in refreshed_headers.items()
                                }
                            )
                        task["url_refresh_count"] = refresh_count + 1
                        current_url = refreshed_url
                        LOGGER.info(
                            "File %s: provider=quark signed URL refreshed, "
                            "count=%d resume_bytes=%d",
                            os.path.basename(str(temporary)),
                            task["url_refresh_count"],
                            int(task.get("downloaded", 0)),
                        )
            if total:
                try:
                    async with client.stream(
                        "GET",
                        url,
                        headers={
                            "Range": f"bytes=0-{PROBE_RANGE_END}",
                            "Accept-Encoding": "identity",
                        },
                    ) as response:
                        http_status = response.status_code
                        content_range = response.headers.get(
                            "content-range",
                            "",
                        ).lower()
                        accepts_ranges = (
                            response.status_code == 206
                            and content_range.startswith("bytes 0-")
                        )
                except httpx.HTTPError as exc:
                    probe_failure = f"Range探测失败：{type(exc).__name__}"
                    try:
                        head_response = await client.head(url)
                        http_status = head_response.status_code
                    except httpx.HTTPError:
                        pass
            else:
                try:
                    response = await client.head(url)
                    http_status = response.status_code
                    total = int(response.headers.get("content-length", "0") or 0)
                    accepts_ranges = "bytes" in response.headers.get(
                        "accept-ranges",
                        "",
                    ).lower()
                except httpx.HTTPError:
                    pass
            task["total_size"] = total
            task["http_status"] = http_status
            if total > 0 and accepts_ranges:
                task["range_supported"] = True
                task["degradation_reason"] = ""
                LOGGER.info(
                    "File %s: starting range download, size=%d",
                    os.path.basename(str(temporary)),
                    total,
                )
                started = time.monotonic()
                try:
                    await self._download_ranges(
                        task,
                        client,
                        url,
                        temporary,
                        total,
                        num_threads,
                    )
                except (httpx.HTTPError, IOError) as exc:
                    if _task_provider(task) == "baidu":
                        LOGGER.warning(
                            "File %s: provider=baidu range retries exhausted; "
                            "preserving partial file instead of stream fallback",
                            os.path.basename(str(temporary)),
                        )
                        raise
                    reason = f"分片连接失败：{type(exc).__name__}"
                    LOGGER.warning(
                        "File %s: range download failed, fallback to stream: %s",
                        os.path.basename(str(temporary)),
                        type(exc).__name__,
                    )
                    if temporary.exists():
                        temporary.unlink()
                    task.update(
                        downloaded=0,
                        progress=0.0,
                        speed=0.0,
                        eta_seconds=None,
                        range_supported=False,
                        connections_used=1,
                        degradation_reason=reason,
                    )
                    await self._download_stream(
                        task,
                        client,
                        url,
                        temporary,
                        total,
                        reason=reason,
                    )
                    return
                elapsed = max(0.001, time.monotonic() - started)
                task["per_connection_speed"] = (
                    total / elapsed / max(1, task.get("connections_used", 1))
                )
                LOGGER.info(
                    "File %s: completed, total=%.2fMB, speed=%.2fMB/s, connections=%d",
                    os.path.basename(str(temporary)),
                    total / MIB,
                    task["per_connection_speed"] * task.get("connections_used", 1) / MIB,
                    task.get("connections_used", 1),
                )
            else:
                reason = probe_failure
                if not reason and not total:
                    reason = "无法确认文件大小"
                elif not reason and not accepts_ranges:
                    reason = (
                        f"服务器不支持Range分片（HTTP {http_status}）"
                        if http_status
                        else "服务器不支持Range分片"
                    )
                task["range_supported"] = False
                task["connections_used"] = 1
                task["degradation_reason"] = reason
                LOGGER.info(
                    "File %s: degradation to stream mode, reason=%s",
                    os.path.basename(str(temporary)),
                    reason,
                )
                await self._download_stream(
                    task,
                    client,
                    url,
                    temporary,
                    total,
                    reason=reason,
                )

    async def _download_stream(
        self,
        task: dict[str, Any],
        client: httpx.AsyncClient,
        url: str,
        temporary: Path,
        total: int,
        reason: str = "",
    ) -> None:
        task["range_supported"] = False
        task["connections_used"] = 1
        task["degradation_reason"] = reason or "不支持Range分片，降级为流式下载"
        LOGGER.info(
            "流式降级: %s, 原因=%s",
            os.path.basename(str(temporary)),
            task["degradation_reason"],
        )
        state_path = _resume_state_path(temporary)
        if state_path.exists():
            state_path.unlink()
        started = time.monotonic()
        existing = temporary.stat().st_size if temporary.is_file() else 0
        if total and existing > total:
            temporary.unlink()
            existing = 0
        if total and existing == total:
            task.update(
                downloaded=total,
                progress=99.9,
                resumed_bytes=total,
                resume_available=True,
            )
            return
        request_headers = (
            {"Range": f"bytes={existing}-", "Accept-Encoding": "identity"}
            if existing > 0
            else None
        )
        async with client.stream("GET", url, headers=request_headers) as response:
            response.raise_for_status()
            if not total:
                response_size = int(response.headers.get("content-length", "0") or 0)
                total = existing + response_size if response.status_code == 206 else response_size
                task["total_size"] = total
            can_resume = (
                existing > 0
                and response.status_code == 206
                and response.headers.get("content-range", "").lower().startswith(
                    f"bytes {existing}-"
                )
            )
            downloaded = existing if can_resume else 0
            if existing and not can_resume:
                LOGGER.warning(
                    "File %s: provider=%s stream resume rejected, restarting",
                    os.path.basename(str(temporary)),
                    _task_provider(task),
                )
            task["resumed_bytes"] = downloaded
            task["_speed_base_downloaded"] = downloaded
            task["resume_available"] = downloaded > 0
            if downloaded:
                self._update_progress(task, downloaded, total, started)
                LOGGER.info(
                    "File %s: provider=%s stream resumed at %d bytes",
                    os.path.basename(str(temporary)),
                    _task_provider(task),
                    downloaded,
                )
            with temporary.open("ab" if can_resume else "wb") as handle:
                async for chunk in response.aiter_bytes(256 * 1024):
                    remaining = max(0, total - downloaded) if total else 0
                    await self._wait_until_allowed(task, remaining)
                    await self._rate_limiter.consume(len(chunk))
                    handle.write(chunk)
                    downloaded += len(chunk)
                    self._update_progress(
                        task,
                        max(downloaded, int(task.get("downloaded", 0))),
                        total,
                        started,
                    )

    async def _download_ranges(
        self,
        task: dict[str, Any],
        client: httpx.AsyncClient,
        url: str,
        temporary: Path,
        total: int,
        num_threads: int | None,
    ) -> None:
        self._reload_semaphore()
        settings = self.settings_store.load()
        connections = max(
            1,
            min(
                int(num_threads or settings.connections_per_file),
                64,
            ),
        )
        segment_size = max(1, int(settings.segment_size_mb)) * MIB
        # Adaptive segment size based on file size
        if total > 1024 * MIB:  # > 1GB
            segment_size = max(segment_size, 10 * MIB)
        elif total < 100 * MIB:  # < 100MB
            segment_size = min(segment_size, MIB)
        if _task_provider(task) == "baidu":
            connections = min(connections, BAIDU_MAX_CONNECTIONS)
            segment_size = min(segment_size, BAIDU_SEGMENT_SIZE)
        if str(task.get("source_profile", "")).startswith("quark-"):
            connections = min(connections, 8)
            segment_size = 10 * MIB
        if task.get("source_profile") == "alipan-private":
            # 网页私有接口仅作兼容通道；开放平台按用户设置使用并发数。
            connections = min(connections, 3)
            segment_size = 10 * MIB
        planned_ranges = plan_download_ranges(
            total=total,
            preferred_segment_size=segment_size,
            connections=connections,
        )
        completed_ranges = _load_resume_state(
            temporary,
            total=total,
            remote_hash=str(task.get("_remote_hash", "")),
        )
        completed_ranges.intersection_update(planned_ranges)
        ranges = asyncio.Queue()
        for byte_range in planned_ranges:
            if byte_range not in completed_ranges:
                await ranges.put(byte_range)
        worker_count = min(connections, ranges.qsize())
        downloaded = sum(end - start + 1 for start, end in completed_ranges)
        progress_lock = asyncio.Lock()
        started = time.monotonic()
        temporary.touch()
        task["connections_used"] = worker_count
        task["resumed_bytes"] = downloaded
        task["resume_available"] = downloaded > 0
        task["_speed_base_downloaded"] = downloaded
        if downloaded:
            self._update_progress(task, downloaded, total, started)
            LOGGER.info(
                "File %s: provider=%s range resume loaded, bytes=%d, "
                "completed_ranges=%d, pending_ranges=%d",
                os.path.basename(str(temporary)),
                _task_provider(task),
                downloaded,
                len(completed_ranges),
                ranges.qsize(),
            )
        if not ranges.qsize():
            task["downloaded"] = total
            task["progress"] = 99.9
            return
        LOGGER.info(
            "分片下载: %s, provider=%s, %d连接, %d bytes, resume=%d",
            os.path.basename(str(temporary)),
            _task_provider(task),
            worker_count,
            total,
            downloaded,
        )

        async def worker() -> None:
            nonlocal downloaded
            while not ranges.empty():
                try:
                    start, end = ranges.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await self._wait_until_allowed(task, total - downloaded)
                await self._stream_range_to_file(
                    task,
                    client,
                    url,
                    temporary,
                    start,
                    end,
                    progress_lock,
                    started,
                    total,
                )
                async with progress_lock:
                    downloaded += end - start + 1
                    self._update_progress(task, downloaded, total, started)
                    completed_ranges.add((start, end))
                    _write_resume_state(
                        temporary,
                        total=total,
                        remote_hash=str(task.get("_remote_hash", "")),
                        completed=completed_ranges,
                    )
                    task["resume_available"] = True
                ranges.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        try:
            await asyncio.gather(*workers)
        except Exception:
            for worker_task in workers:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        if downloaded != total:
            raise IOError(f"分片下载不完整：{downloaded}/{total}")

    async def _stream_range_to_file(
        self,
        task: dict[str, Any],
        client: httpx.AsyncClient,
        url: str,
        temporary: Path,
        start: int,
        end: int,
        progress_lock: asyncio.Lock,
        started: float,
        total: int,
    ) -> None:
        position = start
        last_error: Exception | None = None
        provider = _task_provider(task)
        attempts = (
            1
            if provider == "baidu"
            or (provider == "quark" and callable(task.get("_url_refresher")))
            else 3
        )
        stream_chunk_size = (
            BAIDU_STREAM_CHUNK_SIZE if provider == "baidu" else 256 * 1024
        )
        for attempt in range(attempts):
            if position > end:
                return
            try:
                async with self._segment_semaphore:
                    async with client.stream(
                        "GET",
                        url,
                        headers={
                            "Range": f"bytes={position}-{end}",
                            "Accept-Encoding": "identity",
                        },
                    ) as response:
                        if response.status_code != 206:
                            raise IOError(
                                "服务器未返回 Range 分片："
                                f"HTTP {response.status_code}"
                            )
                        with temporary.open("r+b", buffering=0) as handle:
                            handle.seek(position)
                            async for chunk in response.aiter_bytes(
                                stream_chunk_size
                            ):
                                if not chunk:
                                    continue
                                if position + len(chunk) - 1 > end:
                                    raise IOError("分片长度超过请求范围")
                                await self._rate_limiter.consume(len(chunk))
                                handle.write(chunk)
                                position += len(chunk)
                                async with progress_lock:
                                    visible = int(task.get("downloaded", 0))
                                    self._update_progress(
                                        task,
                                        visible + len(chunk),
                                        total,
                                        started,
                                    )
                if position == end + 1:
                    return
                raise IOError(
                    f"分片长度不完整：{position - start}/{end - start + 1}"
                )
            except (httpx.HTTPError, IOError) as exc:
                last_error = exc
                LOGGER.warning(
                    "Range retry: task=%s provider=%s bytes=%d-%d "
                    "position=%d attempt=%d/%d reason=%s",
                    task.get("id", "unknown"),
                    _task_provider(task),
                    start,
                    end,
                    position,
                    attempt + 1,
                    attempts,
                    type(exc).__name__,
                )
                if attempt < attempts - 1:
                    await asyncio.sleep(2**attempt)
        remaining = end - position + 1
        if _task_provider(task) == "baidu" and remaining > MIN_RANGE_SEGMENT:
            midpoint = position + remaining // 2 - 1
            LOGGER.warning(
                "Baidu range adaptive split: task=%s bytes=%d-%d -> "
                "bytes=%d-%d + bytes=%d-%d",
                task.get("id", "unknown"),
                position, end, position, midpoint, midpoint + 1, end,
            )
            await self._stream_range_to_file(
                task, client, url, temporary, position, midpoint,
                progress_lock, started, total,
            )
            await self._stream_range_to_file(
                task, client, url, temporary, midpoint + 1, end,
                progress_lock, started, total,
            )
            return
        raise IOError(f"分片下载失败：{last_error}")

    async def _fetch_range(
        self,
        client: httpx.AsyncClient,
        url: str,
        start: int,
        end: int,
    ) -> bytes:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self._segment_semaphore:
                    response = await client.get(
                        url,
                        headers={
                            "Range": f"bytes={start}-{end}",
                            "Accept-Encoding": "identity",
                        },
                    )
                if response.status_code != 206:
                    raise IOError(
                        f"服务器未返回 Range 分片：HTTP {response.status_code}"
                    )
                expected = end - start + 1
                if len(response.content) != expected:
                    raise IOError(
                        f"分片长度不匹配：{len(response.content)}/{expected}"
                    )
                return response.content
            except httpx.RemoteProtocolError as exc:
                last_error = exc
                range_size = end - start + 1
                if attempt >= 1 and range_size > MIN_PROTOCOL_SPLIT_SIZE:
                    midpoint = (start + end) // 2
                    LOGGER.warning(
                        "分片连接连续中断，自动拆分: bytes=%d-%d -> "
                        "bytes=%d-%d + bytes=%d-%d",
                        start,
                        end,
                        start,
                        midpoint,
                        midpoint + 1,
                        end,
                    )
                    left = await self._fetch_range(
                        client,
                        url,
                        start,
                        midpoint,
                    )
                    right = await self._fetch_range(
                        client,
                        url,
                        midpoint + 1,
                        end,
                    )
                    return left + right
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except (httpx.HTTPError, IOError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise IOError(f"分片下载失败：{last_error}")

    @staticmethod
    def _public_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            return f"下载服务器返回 HTTP {exc.response.status_code}"
        if isinstance(exc, httpx.TimeoutException):
            return "下载连接超时"
        if isinstance(exc, httpx.RemoteProtocolError):
            return "下载服务器提前断开连接"
        return str(exc)

    @staticmethod
    def _update_progress(
        task: dict[str, Any],
        downloaded: int,
        total: int,
        started: float,
    ) -> None:
        elapsed = max(0.001, time.monotonic() - started)
        task["downloaded"] = downloaded
        base_downloaded = int(task.get("_speed_base_downloaded", 0))
        task["speed"] = max(0, downloaded - base_downloaded) / elapsed
        task["per_connection_speed"] = (
            task["speed"] / max(1, int(task.get("connections_used", 1)))
        )
        task["last_progress_at"] = time.time()
        if total > 0:
            task["progress"] = min(99.9, round(downloaded / total * 100, 1))
            task["eta_seconds"] = (
                max(0, (total - downloaded) / task["speed"])
                if task["speed"] > 0
                else None
            )

    def cancel_download(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task["status"] in TERMINAL_STATES:
            return False
        task["cancel_requested"] = True
        if task.get("backend") == "aria2" and task.get("gid"):
            asyncio.create_task(
                self.aria2_client.remove(task["gid"], force=True)
            )
        worker = task.get("_worker")
        if worker and not worker.done():
            worker.cancel()
        return True

    def get_status(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            return {"error": "not found"}
        public_keys = (
            "id",
            "filename",
            "total_size",
            "downloaded",
            "progress",
            "status",
            "speed",
            "error",
            "started_at",
            "backend",
            "connections",
            "eta_seconds",
            "save_path",
            "source_profile",
            "range_supported",
            "connections_used",
            "degradation_reason",
            "http_status",
            "per_connection_speed",
            "baidu_app_id_used",
            "provider",
            "resumed_bytes",
            "resume_available",
            "url_refresh_count",
            "last_progress_at",
        )
        return {key: task.get(key) for key in public_keys}

    def list_tasks(self) -> list[dict[str, Any]]:
        return [self.get_status(task_id) for task_id in self.tasks]

    def clear_completed(self) -> None:
        for task_id in list(self.tasks):
            if self.tasks[task_id]["status"] in TERMINAL_STATES:
                self.tasks.pop(task_id, None)
