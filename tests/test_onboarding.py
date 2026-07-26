from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onboarding import BaiduGuideStore, DISCLAIMER_VERSION, OnboardingStore


class OnboardingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tempdir.name)
        self.store = OnboardingStore(self.config_dir)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_new_install_requires_acceptance(self) -> None:
        status = self.store.status()
        self.assertTrue(status["required"])
        self.assertEqual(status["version"], DISCLAIMER_VERSION)
        self.assertIsNone(status["accepted_at"])

    def test_acceptance_is_explicit_and_persisted(self) -> None:
        with self.assertRaises(ValueError):
            self.store.accept(DISCLAIMER_VERSION, accepted=False)
        with patch("onboarding.time.time", return_value=1_721_728_000):
            status = self.store.accept(DISCLAIMER_VERSION, accepted=True)
        self.assertFalse(status["required"])
        self.assertEqual(status["accepted_at"], 1_721_728_000)
        self.assertFalse(OnboardingStore(self.config_dir).status()["required"])

    def test_stale_version_requires_acceptance_again(self) -> None:
        self.store.accept(DISCLAIMER_VERSION, accepted=True)
        state_path = self.config_dir / "onboarding.json"
        state_path.write_text(
            '{"version":"2025-01","accepted_at":1}',
            encoding="utf-8",
        )
        self.assertTrue(OnboardingStore(self.config_dir).status()["required"])

    def test_rejects_wrong_version(self) -> None:
        with self.assertRaises(ValueError):
            self.store.accept("old-version", accepted=True)


class BaiduGuideStoreTests(unittest.TestCase):
    def test_guide_is_required_once_per_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = BaiduGuideStore(temp)
            self.assertTrue(store.status()["required"])

            accepted = store.complete()
            self.assertFalse(accepted["required"])
            self.assertIsInstance(accepted["completed_at"], (int, float))
            self.assertFalse(BaiduGuideStore(temp).status()["required"])

        with tempfile.TemporaryDirectory() as reinstalled:
            self.assertTrue(BaiduGuideStore(reinstalled).status()["required"])
