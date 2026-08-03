from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from config_store import AppSettings, SettingsStore
from downloader import (
    DuplicateFileError,
    has_required_space,
    is_schedule_allowed,
    resolve_destination,
)


class ScheduleTests(unittest.TestCase):
    def test_disabled_or_equal_times_mean_all_day(self) -> None:
        disabled = AppSettings(schedule_enabled=False)
        equal = AppSettings(
            schedule_enabled=True,
            schedule_start="00:00",
            schedule_end="00:00",
        )
        for settings in (disabled, equal):
            self.assertTrue(
                is_schedule_allowed(settings, datetime(2026, 7, 23, 14, 30))
            )

    def test_daytime_window(self) -> None:
        settings = AppSettings(
            schedule_enabled=True,
            schedule_start="08:30",
            schedule_end="18:00",
        )
        self.assertTrue(is_schedule_allowed(settings, datetime(2026, 7, 23, 9)))
        self.assertFalse(is_schedule_allowed(settings, datetime(2026, 7, 23, 20)))

    def test_cross_midnight_window(self) -> None:
        settings = AppSettings(
            schedule_enabled=True,
            schedule_start="22:00",
            schedule_end="06:00",
        )
        self.assertTrue(is_schedule_allowed(settings, datetime(2026, 7, 23, 23)))
        self.assertTrue(is_schedule_allowed(settings, datetime(2026, 7, 23, 5)))
        self.assertFalse(is_schedule_allowed(settings, datetime(2026, 7, 23, 12)))


class DuplicatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)
        self.existing = self.directory / "report.pdf"
        self.existing.write_bytes(b"existing content")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_error_policy_completes_only_when_hash_confirms_same_content(self) -> None:
        digest = hashlib.md5(self.existing.read_bytes()).hexdigest()
        decision = resolve_destination(
            self.directory,
            "report.pdf",
            policy="error",
            expected_size=self.existing.stat().st_size,
            remote_hash=digest,
        )
        self.assertEqual(decision.action, "complete")
        self.assertEqual(decision.path, self.existing)

        with self.assertRaises(DuplicateFileError):
            resolve_destination(
                self.directory,
                "report.pdf",
                policy="error",
                expected_size=self.existing.stat().st_size,
                remote_hash=None,
            )

    def test_error_policy_never_deletes_or_overwrites_local_file(self) -> None:
        before = self.existing.read_bytes()
        with self.assertRaises(DuplicateFileError):
            resolve_destination(
                self.directory,
                "report.pdf",
                policy="error",
                expected_size=999,
                remote_hash="0" * 32,
            )
        self.assertEqual(self.existing.read_bytes(), before)

    def test_rename_counts_from_left_to_right(self) -> None:
        (self.directory / "report (1).pdf").write_bytes(b"one")
        decision = resolve_destination(
            self.directory,
            "report.pdf",
            policy="rename",
        )
        self.assertEqual(decision.action, "download")
        self.assertEqual(decision.path.name, "report (2).pdf")

    def test_overwrite_uses_temporary_target(self) -> None:
        decision = resolve_destination(
            self.directory,
            "report.pdf",
            policy="overwrite",
        )
        self.assertEqual(decision.action, "download")
        self.assertEqual(decision.path, self.existing)
        self.assertNotEqual(decision.temporary_path, self.existing)
        self.assertTrue(decision.replace_on_success)

    def test_skip_preserves_local_file(self) -> None:
        decision = resolve_destination(
            self.directory,
            "report.pdf",
            policy="skip",
        )
        self.assertEqual(decision.action, "skip")
        self.assertEqual(decision.path, self.existing)

    def test_filename_is_confined_to_download_directory(self) -> None:
        with self.assertRaises(ValueError):
            resolve_destination(self.directory, "../escape.txt", policy="rename")


class DiskReserveTests(unittest.TestCase):
    def test_requires_remaining_bytes_plus_reserve(self) -> None:
        reserve = 50 * 1024**3
        remaining = 10 * 1024**3
        self.assertFalse(
            has_required_space(
                Path("."),
                remaining_bytes=remaining,
                reserve_space_gb=50,
                free_bytes=reserve + remaining - 1,
            )
        )
        self.assertTrue(
            has_required_space(
                Path("."),
                remaining_bytes=remaining,
                reserve_space_gb=50,
                free_bytes=reserve + remaining,
            )
        )


class DownloadManagerContractTests(unittest.TestCase):
    def test_constructor_accepts_named_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            self.assertEqual(manager.download_dir, root / "downloads")
            self.assertEqual(manager.settings_store.load().concurrent_downloads, 5)

    def test_status_exposes_connection_count_and_eta(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager.tasks["task"] = {
                "id": "task",
                "filename": "file.bin",
                "total_size": 1000,
                "downloaded": 250,
                "progress": 25,
                "status": "downloading",
                "speed": 100,
                "error": None,
                "started_at": 0,
                "backend": "builtin",
                "connections": 16,
                "eta_seconds": 7.5,
            }
            status = manager.get_status("task")
            self.assertEqual(status["connections"], 16)
            self.assertEqual(status["eta_seconds"], 7.5)

    def test_status_exposes_actual_save_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            expected = root / "downloads" / "demo.bin"
            manager.tasks["task"] = {
                "id": "task",
                "filename": "demo.bin",
                "save_path": str(expected),
            }

            self.assertEqual(
                manager.get_status("task")["save_path"],
                str(expected),
            )

    def test_status_exposes_source_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._run_task = AsyncMock()

            task_id = asyncio.run(
                manager.start_download(
                    "https://download.example/file",
                    "demo.bin",
                    source_profile="quark-desktop",
                )
            )

            self.assertEqual(
                manager.get_status(task_id)["source_profile"],
                "quark-desktop",
            )

    def test_status_exposes_range_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager.tasks["task"] = {
                "id": "task",
                "range_supported": True,
                "connections_used": 8,
                "degradation_reason": "",
                "http_status": 206,
                "per_connection_speed": 1024.0,
                "baidu_app_id_used": 125463,
            }

            status = manager.get_status("task")

            self.assertTrue(status["range_supported"])
            self.assertEqual(status["connections_used"], 8)
            self.assertEqual(status["http_status"], 206)
            self.assertEqual(status["per_connection_speed"], 1024.0)
            self.assertEqual(status["baidu_app_id_used"], 125463)


class NestedDirectoryDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_size_signed_url_uses_range_probe_without_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            client = MagicMock()
            client.head = AsyncMock()
            range_response = MagicMock()
            range_response.status_code = 206
            range_response.headers = {"content-range": "bytes 0-99/100"}
            range_context = MagicMock()
            range_context.__aenter__ = AsyncMock(return_value=range_response)
            range_context.__aexit__ = AsyncMock(return_value=False)
            client.stream.return_value = range_context
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=client)
            context.__aexit__ = AsyncMock(return_value=False)
            manager._download_stream = AsyncMock()
            manager._download_ranges = AsyncMock()
            task = {"total_size": 100}

            with patch.object(
                manager_module.httpx,
                "AsyncClient",
                return_value=context,
            ):
                await manager._download(
                    task,
                    "https://download.example/signed",
                    {"User-Agent": "bound-agent"},
                    root / "download.part",
                    None,
                )

            client.head.assert_not_awaited()
            client.stream.assert_called_once_with(
                "GET",
                "https://download.example/signed",
                headers={
                    "Range": "bytes=0-1023",
                    "Accept-Encoding": "identity",
                },
            )
            manager._download_ranges.assert_awaited_once()
            manager._download_stream.assert_not_awaited()

    async def test_failed_range_probe_falls_back_without_unbound_response(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            client = MagicMock()
            request = httpx.Request("GET", "https://download.example/signed")
            range_context = MagicMock()
            range_context.__aenter__ = AsyncMock(
                side_effect=httpx.ConnectError("probe failed", request=request)
            )
            range_context.__aexit__ = AsyncMock(return_value=False)
            client.stream.return_value = range_context
            head_response = MagicMock(
                status_code=200,
                headers={"content-length": "100", "accept-ranges": "bytes"},
            )
            client.head = AsyncMock(return_value=head_response)
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=client)
            context.__aexit__ = AsyncMock(return_value=False)
            manager._download_stream = AsyncMock()
            manager._download_ranges = AsyncMock()
            task = {"total_size": 100}

            with patch.object(
                manager_module.httpx,
                "AsyncClient",
                return_value=context,
            ):
                await manager._download(
                    task,
                    "https://download.example/signed",
                    {},
                    root / "download.part",
                    None,
                )

            client.head.assert_awaited_once()
            manager._download_stream.assert_awaited_once()
            self.assertEqual(task["http_status"], 200)
            self.assertIn("Range探测失败", task["degradation_reason"])

    async def test_failed_range_download_falls_back_to_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            client = MagicMock()
            range_response = MagicMock(
                status_code=206,
                headers={"content-range": "bytes 0-99/100"},
            )
            range_context = MagicMock()
            range_context.__aenter__ = AsyncMock(return_value=range_response)
            range_context.__aexit__ = AsyncMock(return_value=False)
            client.stream.return_value = range_context
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=client)
            context.__aexit__ = AsyncMock(return_value=False)
            manager._download_ranges = AsyncMock(
                side_effect=httpx.RemoteProtocolError("disconnected")
            )
            manager._download_stream = AsyncMock()
            task = {"total_size": 100}

            with patch.object(
                manager_module.httpx,
                "AsyncClient",
                return_value=context,
            ):
                await manager._download(
                    task,
                    "https://download.example/signed",
                    {},
                    root / "download.part",
                    None,
                )

            manager._download_stream.assert_awaited_once()
            self.assertFalse(task["range_supported"])
            self.assertIn("分片连接失败", task["degradation_reason"])

    async def test_quark_signed_url_uses_aligned_range_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            client = MagicMock()
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=client)
            context.__aexit__ = AsyncMock(return_value=False)
            manager._download_ranges = AsyncMock()
            manager._download_stream = AsyncMock()
            task = {
                "total_size": 100,
                "source_profile": "quark-web",
            }

            with patch.object(
                manager_module.httpx,
                "AsyncClient",
                return_value=context,
            ):
                await manager._download(
                    task,
                    "https://download.example/signed?token=secret",
                    {
                        "User-Agent": "browser",
                        "Origin": "https://pan.quark.cn",
                        "Referer": "https://pan.quark.cn/",
                        "Cookie": "secret=value",
                    },
                    root / "download.part",
                    None,
                )

            client.stream.assert_not_called()
            manager._download_ranges.assert_awaited_once()
            manager._download_stream.assert_not_awaited()
            self.assertEqual(task["connections_used"], 8)
            self.assertTrue(task["range_supported"])
            self.assertEqual(task["degradation_reason"], "")

    async def test_connections_used_is_the_actual_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._stream_range_to_file = AsyncMock()
            manager._wait_until_allowed = AsyncMock()
            manager._rate_limiter.consume = AsyncMock()
            task = {
                "total_size": 1,
                "downloaded": 0,
                "speed": 0.0,
            }

            await manager._download_ranges(
                task,
                MagicMock(),
                "https://download.example/file",
                root / "download.part",
                1,
                16,
            )

            self.assertEqual(task["connections_used"], 1)

    async def test_protocol_disconnect_splits_large_range_after_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._reload_semaphore()
            one_mib = 1024 * 1024
            left = MagicMock(status_code=206, content=b"a" * one_mib)
            right = MagicMock(status_code=206, content=b"b" * one_mib)
            client = MagicMock()
            client.get = AsyncMock(
                side_effect=[
                    httpx.RemoteProtocolError("incomplete message body"),
                    httpx.RemoteProtocolError("incomplete message body"),
                    left,
                    right,
                ]
            )

            with patch.object(
                manager_module.asyncio,
                "sleep",
                new=AsyncMock(),
            ):
                content = await manager._fetch_range(
                    client,
                    "https://download.example/file",
                    0,
                    2 * one_mib - 1,
                )

            self.assertEqual(content, left.content + right.content)
            requested_ranges = [
                call.kwargs["headers"]["Range"]
                for call in client.get.await_args_list
            ]
            self.assertEqual(
                requested_ranges,
                [
                    f"bytes=0-{2 * one_mib - 1}",
                    f"bytes=0-{2 * one_mib - 1}",
                    f"bytes=0-{one_mib - 1}",
                    f"bytes={one_mib}-{2 * one_mib - 1}",
                ],
            )

    async def test_streamed_range_resumes_without_buffering_whole_segment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._reload_semaphore()
            manager._rate_limiter.consume = AsyncMock()
            temporary = root / "download.part"
            temporary.touch()

            first = MagicMock(status_code=206)
            second = MagicMock(status_code=206)

            async def interrupted_chunks(_size):
                yield b"ab"
                raise httpx.RemoteProtocolError("connection interrupted")

            async def resumed_chunks(_size):
                yield b"cd"

            first.aiter_bytes = interrupted_chunks
            second.aiter_bytes = resumed_chunks
            contexts = []
            for response in (first, second):
                context = MagicMock()
                context.__aenter__ = AsyncMock(return_value=response)
                context.__aexit__ = AsyncMock(return_value=False)
                contexts.append(context)
            client = MagicMock()
            client.stream.side_effect = contexts
            task = {
                "total_size": 4,
                "downloaded": 0,
                "speed": 0.0,
                "connections_used": 1,
            }

            with patch.object(
                manager_module.asyncio,
                "sleep",
                new=AsyncMock(),
            ):
                await manager._stream_range_to_file(
                    task,
                    client,
                    "https://download.example/file",
                    temporary,
                    0,
                    3,
                    asyncio.Lock(),
                    time.monotonic(),
                    4,
                )

            self.assertEqual(temporary.read_bytes(), b"abcd")
            self.assertEqual(task["downloaded"], 4)
            requested_ranges = [
                call.kwargs["headers"]["Range"]
                for call in client.stream.call_args_list
            ]
            self.assertEqual(requested_ranges, ["bytes=0-3", "bytes=2-3"])

    async def test_baidu_download_allows_up_to_sixteen_connections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._stream_range_to_file = AsyncMock()
            manager._wait_until_allowed = AsyncMock()
            manager._rate_limiter.consume = AsyncMock()
            total = 16 * 1024 * 1024
            task = {
                "total_size": total,
                "downloaded": 0,
                "speed": 0.0,
                "baidu_app_id_used": 250528,
            }

            await manager._download_ranges(
                task,
                MagicMock(),
                "https://download.example/file",
                root / "download.part",
                total,
                16,
            )

            self.assertEqual(task["connections_used"], 16)

    async def test_alipan_openapi_uses_configured_connections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._stream_range_to_file = AsyncMock()
            manager._wait_until_allowed = AsyncMock()
            manager._rate_limiter.consume = AsyncMock()
            total = 16 * 1024 * 1024
            task = {
                "total_size": total,
                "downloaded": 0,
                "speed": 0.0,
                "source_profile": "alipan-openapi",
            }

            await manager._download_ranges(
                task, MagicMock(), "https://download.example/file",
                root / "download.part", total, 16,
            )

            self.assertEqual(task["connections_used"], 16)

    async def test_alipan_private_is_limited_to_three_connections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._stream_range_to_file = AsyncMock()
            manager._wait_until_allowed = AsyncMock()
            manager._rate_limiter.consume = AsyncMock()
            total = 40 * 1024 * 1024
            task = {
                "total_size": total,
                "downloaded": 0,
                "speed": 0.0,
                "source_profile": "alipan-private",
            }

            await manager._download_ranges(
                task, MagicMock(), "https://download.example/file",
                root / "download.part", total, 16,
            )

            self.assertEqual(task["connections_used"], 3)

    def test_small_file_range_plan_uses_multiple_connections(self) -> None:
        manager_module = __import__("downloader")

        ranges = manager_module.plan_download_ranges(
            total=1024 * 1024,
            preferred_segment_size=5 * 1024 * 1024,
            connections=16,
        )

        self.assertGreaterEqual(len(ranges), 4)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 1024 * 1024 - 1)

    async def test_download_can_target_a_safe_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            manager._run_task = AsyncMock()

            task_id = await manager.start_download(
                "https://download.example/file",
                "demo.bin",
                relative_dir="网盘目录/子目录",
            )
            await asyncio.sleep(0)

            save_path = Path(manager.tasks[task_id]["save_path"])
            self.assertEqual(
                save_path,
                root / "downloads" / "网盘目录" / "子目录" / "demo.bin",
            )
            self.assertTrue(save_path.parent.is_dir())

    async def test_download_rejects_unsafe_relative_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=root / "downloads",
                settings_store=SettingsStore(root / "config"),
            )
            with self.assertRaises(ValueError):
                await manager.start_download(
                    "https://download.example/file",
                    "demo.bin",
                    relative_dir="../escape",
                )


class Aria2DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_local_aria2_receives_new_download(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            downloads = Path(root) / "downloads"
            settings = SettingsStore(config)
            settings.update(
                {
                    "aria2_enabled": True,
                    "connections_per_file": 16,
                    "segment_size_mb": 5,
                }
            )
            aria2 = AsyncMock()
            aria2.check_connection.return_value = True
            aria2.add_uri.return_value = "gid-1"
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=downloads,
                settings_store=settings,
                aria2_client=aria2,
            )

            task_id = await manager.start_download(
                "https://download.example/file",
                "example.bin",
                expected_size=100,
                headers={"Referer": "https://pan.example/"},
            )

            aria2.add_uri.assert_awaited_once()
            options = aria2.add_uri.await_args.args[1]
            self.assertEqual(options["split"], "16")
            self.assertEqual(options["min-split-size"], "5M")
            self.assertEqual(options["out"], f"example.bin.{task_id}.part")
            self.assertIn("Referer: https://pan.example/", options["header"])
            self.assertEqual(manager.tasks[task_id]["backend"], "aria2")
            manager.cancel_download(task_id)

    async def test_unavailable_aria2_falls_back_to_builtin_downloader(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            downloads = Path(root) / "downloads"
            settings = SettingsStore(config)
            settings.update({"aria2_enabled": True})
            aria2 = AsyncMock()
            aria2.check_connection.return_value = False
            manager_module = __import__("downloader")
            manager = manager_module.DownloadManager(
                download_dir=downloads,
                settings_store=settings,
                aria2_client=aria2,
            )
            manager._run_task = AsyncMock()

            task_id = await manager.start_download(
                "https://download.example/file",
                "example.bin",
            )
            await asyncio.sleep(0)

            self.assertEqual(manager.tasks[task_id]["backend"], "builtin")
            manager._run_task.assert_awaited_once()
