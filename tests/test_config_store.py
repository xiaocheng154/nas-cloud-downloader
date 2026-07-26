from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config_store import (
    CredentialStore,
    SettingsStore,
    SettingsValidationError,
)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tempdir.name)
        self.store = SettingsStore(self.config_dir)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_defaults_match_product_spec(self) -> None:
        settings = self.store.load()
        self.assertEqual(settings.duplicate_policy, "error")
        self.assertEqual(settings.reserve_space_gb, 50)
        self.assertEqual(settings.total_speed_limit_mbps, 0)
        self.assertEqual(settings.connections_per_file, 16)
        self.assertEqual(settings.segment_size_mb, 5)
        self.assertEqual(settings.max_segment_requests, 30)
        self.assertEqual(settings.concurrent_downloads, 5)
        self.assertEqual(settings.schedule_start, "00:00")
        self.assertEqual(settings.schedule_end, "00:00")

    def test_update_persists_and_preserves_unspecified_values(self) -> None:
        updated = self.store.update(
            {
                "duplicate_policy": "rename",
                "reserve_space_gb": 64,
                "schedule_enabled": True,
                "schedule_start": "23:30",
                "schedule_end": "06:15",
            }
        )
        reloaded = SettingsStore(self.config_dir).load()
        self.assertEqual(updated, reloaded)
        self.assertEqual(reloaded.reserve_space_gb, 64)
        self.assertEqual(reloaded.concurrent_downloads, 5)

    def test_invalid_update_keeps_previous_file(self) -> None:
        self.store.update({"reserve_space_gb": 80})
        original = (self.config_dir / "settings.json").read_text("utf-8")
        with self.assertRaises(SettingsValidationError):
            self.store.update({"connections_per_file": 0})
        self.assertEqual(
            original,
            (self.config_dir / "settings.json").read_text("utf-8"),
        )

    def test_rejects_invalid_enum_time_and_ranges(self) -> None:
        invalid = (
            {"duplicate_policy": "delete"},
            {"schedule_start": "25:00"},
            {"total_speed_limit_mbps": -1},
            {"connections_per_file": 65},
            {"concurrent_downloads": 0},
            {"log_level": "TRACE"},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(SettingsValidationError):
                    self.store.update(payload)


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self.tempdir.name))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_status_never_returns_secret_values(self) -> None:
        self.store.update(
            "baidu",
            {"bduss": "very-secret-bduss", "stoken": "secret-stoken"},
        )
        status = self.store.status()
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertTrue(status["baidu"]["configured"])
        self.assertNotIn("very-secret-bduss", serialized)
        self.assertNotIn("secret-stoken", serialized)

    def test_update_requires_provider_fields(self) -> None:
        with self.assertRaises(SettingsValidationError):
            self.store.update("baidu", {"bduss": ""})
        with self.assertRaises(SettingsValidationError):
            self.store.update("quark", {"cookie": ""})

    def test_clear_only_removes_selected_provider(self) -> None:
        self.store.update("baidu", {"bduss": "a", "stoken": "b"})
        self.store.update("quark", {"cookie": "c=1"})
        self.store.clear("baidu")
        self.assertFalse(self.store.status()["baidu"]["configured"])
        self.assertTrue(self.store.status()["quark"]["configured"])
        self.assertEqual(self.store.get("quark")["cookie"], "c=1")
