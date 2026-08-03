from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_files import LocalFileManager


class LocalFileManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = LocalFileManager(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_list_marks_images_for_thumbnail(self) -> None:
        (self.root / "照片.jpg").write_bytes(b"image")
        (self.root / "文档.txt").write_text("text", encoding="utf-8")
        (self.root / "目录").mkdir()

        result = self.manager.list_files("/")
        entries = {item["name"]: item for item in result["files"]}

        self.assertTrue(entries["照片.jpg"]["has_thumbnail"])
        self.assertFalse(entries["文档.txt"]["has_thumbnail"])
        self.assertTrue(entries["目录"]["is_dir"])

    def test_rename_file_and_folder(self) -> None:
        (self.root / "旧文件.txt").write_text("data", encoding="utf-8")
        (self.root / "旧目录").mkdir()

        file_result = self.manager.rename("/旧文件.txt", "新文件.txt")
        folder_result = self.manager.rename("/旧目录", "新目录")

        self.assertEqual(file_result["path"], "/新文件.txt")
        self.assertEqual(folder_result["path"], "/新目录")
        self.assertTrue((self.root / "新文件.txt").is_file())
        self.assertTrue((self.root / "新目录").is_dir())

    def test_rejects_traversal_and_duplicate_name(self) -> None:
        (self.root / "一.txt").write_text("1", encoding="utf-8")
        (self.root / "二.txt").write_text("2", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.manager.resolve("/../../outside")
        with self.assertRaises(FileExistsError):
            self.manager.rename("/一.txt", "二.txt")
        with self.assertRaises(ValueError):
            self.manager.rename("/一.txt", "../越界.txt")

    def test_thumbnail_only_accepts_supported_image(self) -> None:
        image = self.root / "图.webp"
        image.write_bytes(b"webp")
        text = self.root / "说明.txt"
        text.write_text("text", encoding="utf-8")

        self.assertEqual(self.manager.thumbnail_path("/图.webp"), image.resolve())
        with self.assertRaises(ValueError):
            self.manager.thumbnail_path("/说明.txt")


if __name__ == "__main__":
    unittest.main()
