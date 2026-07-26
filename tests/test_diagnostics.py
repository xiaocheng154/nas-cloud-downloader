from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path

from config_store import AppSettings
from diagnostics import (
    build_diagnostic_zip,
    clear_logs,
    configure_logging,
    redact_text,
    tail_log,
)


class RedactionTests(unittest.TestCase):
    def test_redacts_credentials_cookies_and_download_urls(self) -> None:
        source = (
            "BDUSS=alpha STOKEN=beta Cookie: session=gamma "
            "url=https://download.example/file?token=delta"
        )
        redacted = redact_text(source)
        for secret in ("alpha", "beta", "gamma", "delta"):
            self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)


class LoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tempdir.name)

    def tearDown(self) -> None:
        logger = logging.getLogger("clouddl")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        self.tempdir.cleanup()

    def test_logger_rotates_and_redacts_messages(self) -> None:
        logger = configure_logging(AppSettings(), self.config_dir)
        logger.info("Cookie: session=top-secret")
        for handler in logger.handlers:
            handler.flush()
        output = tail_log(self.config_dir, lines=20)
        self.assertNotIn("top-secret", output)
        self.assertIn("[REDACTED]", output)

    def test_clear_logs_removes_current_and_rotated_files(self) -> None:
        logger = configure_logging(AppSettings(), self.config_dir)
        logger.warning("sample")
        for handler in logger.handlers:
            handler.flush()
        (self.config_dir / "logs" / "clouddl.log.1").write_text(
            "old",
            encoding="utf-8",
        )
        clear_logs(self.config_dir)
        self.assertEqual(list((self.config_dir / "logs").glob("clouddl.log*")), [])


class DiagnosticExportTests(unittest.TestCase):
    def test_zip_contains_only_redacted_configuration_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp)
            log_dir = config_dir / "logs"
            log_dir.mkdir()
            (log_dir / "clouddl.log").write_text(
                "BDUSS=secret-bduss\nurl=https://host/file?token=secret-token\n",
                encoding="utf-8",
            )
            archive_bytes = build_diagnostic_zip(
                config_dir=config_dir,
                settings=AppSettings(aria2_secret="secret-aria2-rpc"),
                diagnostics={"network": {"ok": True}},
                version="1.0.0",
                uptime_seconds=123,
            )
            serialized = archive_bytes.decode("latin-1", errors="ignore")
            self.assertNotIn("secret-bduss", serialized)
            self.assertNotIn("secret-token", serialized)
            self.assertNotIn("secret-aria2-rpc", serialized)
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"system.json", "settings.json", "diagnostics.json", "clouddl.log"},
                )
                settings = json.loads(archive.read("settings.json"))
                self.assertEqual(settings["reserve_space_gb"], 50)
                self.assertEqual(settings["aria2_secret"], "[REDACTED]")
                log = archive.read("clouddl.log").decode("utf-8")
                self.assertNotIn("secret-bduss", log)
