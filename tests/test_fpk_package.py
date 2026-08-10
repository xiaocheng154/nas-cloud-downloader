from __future__ import annotations

import io
import json
import os
import struct
import tarfile
import unittest
import zlib
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FPK_PATH = Path(
    os.environ.get("FPK_PATH", str(PROJECT_ROOT / "clouddl_x86.fpk"))
).resolve()
EXPECTED_ARCH = os.environ.get("EXPECTED_ARCH", "x86_64")
EXPECTED_ELF_MACHINE = {"x86_64": 62, "aarch64": 183}[EXPECTED_ARCH]
EXPECTED_PLATFORM = {"x86_64": "x86", "aarch64": "arm"}[EXPECTED_ARCH]


def png_rgba_pixels(path: Path) -> list[tuple[int, int, int, int]]:
    data = path.read_bytes()
    width, height, depth, color_type, _, _, interlace = struct.unpack(
        ">IIBBBBB", data[16:29]
    )
    if depth != 8 or color_type != 6 or interlace != 0:
        raise AssertionError("测试仅支持 8 位非交错 RGBA PNG")
    offset = 8
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        offset += 12 + length
    raw = zlib.decompress(compressed)
    stride = width * 4
    rows: list[bytearray] = []
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        previous = rows[-1] if rows else bytearray(stride)
        for index in range(stride):
            left = row[index - 4] if index >= 4 else 0
            up = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + up) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + up - upper_left
                distances = (
                    abs(predictor - left),
                    abs(predictor - up),
                    abs(predictor - upper_left),
                )
                nearest = (left, up, upper_left)[distances.index(min(distances))]
                row[index] = (row[index] + nearest) & 0xFF
            elif filter_type != 0:
                raise AssertionError(f"未知 PNG 过滤器：{filter_type}")
        rows.append(row)
    return [
        tuple(row[index : index + 4])
        for row in rows
        for index in range(0, stride, 4)
    ]


class FpkPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.archive = tarfile.open(FPK_PATH, "r:*")
        cls.members = {member.name: member for member in cls.archive.getmembers()}
        app_data = cls.archive.extractfile(cls.members["app.tgz"]).read()
        cls.app_archive = tarfile.open(fileobj=io.BytesIO(app_data), mode="r:gz")
        cls.app_members = {
            member.name: member for member in cls.app_archive.getmembers()
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app_archive.close()
        cls.archive.close()

    def read_outer(self, name: str) -> bytes:
        extracted = self.archive.extractfile(self.members[name])
        self.assertIsNotNone(extracted)
        return extracted.read()

    def read_app(self, name: str) -> bytes:
        extracted = self.app_archive.extractfile(self.app_members[name])
        self.assertIsNotNone(extracted)
        return extracted.read()

    def test_source_has_official_lifecycle_hooks(self) -> None:
        required = {
            "install_init",
            "install_callback",
            "uninstall_init",
            "uninstall_callback",
            "upgrade_init",
            "upgrade_callback",
            "config_init",
            "config_callback",
            "main",
        }
        self.assertTrue(
            required.issubset(path.name for path in (PROJECT_ROOT / "cmd").iterdir())
        )

    def test_package_uses_official_fnpack_layout(self) -> None:
        self.assertIn("app.tgz", self.members)
        self.assertFalse(any(name.startswith("app/") for name in self.members))

    def test_required_outer_files_exist(self) -> None:
        required = {
            "app.tgz",
            "manifest",
            "ICON.PNG",
            "ICON_256.PNG",
            "cmd/main",
            "cmd/install_init",
            "cmd/install_callback",
            "cmd/uninstall_init",
            "cmd/uninstall_callback",
            "cmd/upgrade_init",
            "cmd/upgrade_callback",
            "cmd/config_init",
            "cmd/config_callback",
            "config/privilege",
            "config/resource",
            "wizard/install",
        }
        self.assertTrue(required.issubset(self.members), required - self.members.keys())

    def test_required_application_files_are_nested_in_app_tgz(self) -> None:
        required = {
            "runtime/python/bin/python3",
            "runtime/python/bin/python3.11",
            "runtime/python/lib/libpython3.11.so.1.0",
            "runtime/aria2/aria2c",
            "runtime/aria2/LICENSE",
            "runtime/aria2/SOURCE.txt",
            "service/src/app.py",
            "service/src/cloud_qr.py",
            "service/src/alipan_qr.py",
            "service/src/local_files.py",
            "service/src/credential_parser.py",
            "service/src/static/app.js",
            "service/src/static/index.html",
            "service/src/static/assets/file-manager-icons.png",
            "service/vendor/fastapi/__init__.py",
            "service/vendor/qrcode/__init__.py",
            "service/vendor/qrcode/compat/png.py",
            "service/vendor/qrcode/image/pure.py",
            "service/vendor/qrcode/image/svg.py",
            "service/vendor/qrcode/image/styles/moduledrawers/base.py",
            "service/vendor/qrcode/image/styles/moduledrawers/svg.py",
            "service/vendor/qrcode-LICENSE.txt",
            "ui/config",
            "ui/images/icon_64.png",
            "ui/images/icon_256.png",
        }
        self.assertTrue(
            required.issubset(self.app_members),
            required - self.app_members.keys(),
        )

    def test_archive_paths_are_safe(self) -> None:
        for name in (*self.members, *self.app_members):
            path = PurePosixPath(name)
            self.assertFalse(path.is_absolute(), name)
            self.assertNotIn("..", path.parts, name)

    def test_generated_caches_are_not_packaged(self) -> None:
        self.assertFalse(
            any("__pycache__" in PurePosixPath(name).parts for name in self.app_members)
        )
        self.assertFalse(any(name.endswith(".pyc") for name in self.app_members))

    def test_linux_scripts_use_lf(self) -> None:
        for name in (
            "cmd/main",
            "cmd/install_init",
            "cmd/install_callback",
            "cmd/uninstall_init",
            "cmd/uninstall_callback",
            "cmd/upgrade_init",
            "cmd/upgrade_callback",
            "cmd/config_init",
            "cmd/config_callback",
        ):
            with self.subTest(name=name):
                self.assertNotIn(b"\r\n", self.read_outer(name))
        self.assertNotIn(b"\r\n", self.read_app("service/src/start.sh"))

    def test_main_manages_the_native_service_process(self) -> None:
        main = self.read_outer("cmd/main").decode("utf-8")
        self.assertIn('case "$1" in', main)
        self.assertIn('${TRIM_APPDEST}/runtime/python/bin/python3', main)
        self.assertIn('${TRIM_APPDEST}/service/src/app.py', main)
        self.assertIn('${TRIM_APPDEST}/runtime/aria2/aria2c', main)
        self.assertIn('ARIA2_SECRET_FILE=', main)
        self.assertIn('start_aria2', main)
        self.assertIn('stop_aria2', main)
        self.assertIn('PYTHONPATH="$VENDOR_DIR"', main)
        self.assertNotIn("docker", main.lower())

    def test_install_callback_prepares_native_data_directories(self) -> None:
        callback = self.read_outer("cmd/install_callback").decode("utf-8")
        self.assertIn("TRIM_PKGVAR", callback)
        self.assertIn("wizard_download_dir", callback)
        self.assertNotIn("TRIM_DATA_SHARE_PATHS", callback)
        self.assertIn('DOWNLOAD_FILE="${TRIM_PKGVAR}/download_dir"', callback)
        self.assertIn('"$TRIM_APPDEST/runtime/python/bin/python3"', callback)
        self.assertIn('"$TRIM_APPDEST/runtime/aria2/aria2c"', callback)
        self.assertIn("chmod 755", callback)
        self.assertNotIn("docker", callback.lower())

    def test_upgrade_migrates_broken_appshare_download_directory(self) -> None:
        callback = self.read_outer("cmd/upgrade_callback").decode("utf-8")
        self.assertIn("/@appshare/clouddl/downloads", callback)
        self.assertIn('${TRIM_PKGVAR}/downloads', callback)
        self.assertIn('DOWNLOAD_FILE="${TRIM_PKGVAR}/download_dir"', callback)

    def test_uninstall_removes_first_use_guide_state(self) -> None:
        callback = (PROJECT_ROOT / "cmd" / "uninstall_callback").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"${TRIM_PKGVAR}/config/onboarding.json"',
            callback,
        )
        self.assertIn(
            '"${TRIM_PKGVAR}/config/ui_state.json"',
            callback,
        )

    def test_manifest_matches_supported_fnos_target(self) -> None:
        manifest = {}
        for line in self.read_outer("manifest").decode("utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                manifest[key.strip()] = value.strip()
        self.assertEqual(manifest["appname"], "clouddl")
        self.assertEqual(manifest["version"], "1.5.7")
        self.assertEqual(manifest["display_name"], "多网盘下载器")
        self.assertEqual(manifest["maintainer"], "xiaocheng154")
        self.assertEqual(manifest["distributor"], "xiaocheng154")
        self.assertNotIn("arch", manifest)
        self.assertEqual(manifest["desktop_applaunchname"], "clouddl.Application")
        self.assertEqual(manifest["platform"], EXPECTED_PLATFORM)
        self.assertEqual(manifest["service_port"], "8686")
        self.assertEqual(manifest["ctl_stop"], "true")
        self.assertEqual(manifest["checkport"], "true")

    def test_json_metadata_is_valid_and_consistent(self) -> None:
        privilege = json.loads(self.read_outer("config/privilege"))
        resource = json.loads(self.read_outer("config/resource"))
        wizard = json.loads(self.read_outer("wizard/install"))
        ui = json.loads(self.read_app("ui/config"))

        self.assertEqual(privilege["defaults"]["run-as"], "root")
        self.assertEqual(privilege["username"], "clouddl")
        self.assertEqual(privilege["groupname"], "clouddl")
        self.assertEqual(
            resource["data-share"]["shares"][0]["name"],
            "clouddl/downloads",
        )
        self.assertIsInstance(wizard[1]["items"][0]["rules"], list)
        self.assertEqual(
            wizard[1]["items"][0]["field"],
            "wizard_download_dir",
        )
        self.assertEqual(
            wizard[1]["items"][0]["initValue"],
            "/vol1/downloads",
        )
        self.assertIn("clouddl.Application", ui[".url"])

    def test_package_contains_no_docker_runtime_definition(self) -> None:
        self.assertFalse(
            any("docker" in PurePosixPath(name).parts for name in self.app_members)
        )
        self.assertNotIn("docker-project", self.read_outer("config/resource").decode())

    def test_linux_runtime_dependencies_are_bundled(self) -> None:
        vendor = PROJECT_ROOT / "app/service/vendor"
        for package in ("fastapi", "uvicorn", "httpx", "pydantic"):
            self.assertTrue((vendor / package).is_dir(), package)
        self.assertTrue(
            any(vendor.glob("pydantic_core/_pydantic_core*.so")),
            "缺少 Linux pydantic-core 扩展",
        )

    def test_desktop_icons_render_the_approved_cloud_and_arrow(self) -> None:
        for path in (
            PROJECT_ROOT / "ICON.PNG",
            PROJECT_ROOT / "ICON_256.PNG",
            PROJECT_ROOT / "app/ui/images/icon_64.png",
            PROJECT_ROOT / "app/ui/images/icon_256.png",
        ):
            pixels = png_rgba_pixels(path)
            self.assertTrue(
                any(r < 70 and g < 80 and b < 100 and a > 200 for r, g, b, a in pixels),
                f"{path.name} 缺少深色云朵",
            )
            self.assertTrue(
                any(r > 240 and g > 240 and b > 240 and a > 200 for r, g, b, a in pixels),
                f"{path.name} 缺少白色背景",
            )
            self.assertTrue(
                any(b > 160 and b > r * 1.5 and a > 200 for r, g, b, a in pixels),
                f"{path.name} 缺少蓝色下载箭头",
            )

    def test_native_package_does_not_require_docker(self) -> None:
        resource = (PROJECT_ROOT / "config/resource").read_text(encoding="utf-8")
        main = (PROJECT_ROOT / "cmd/main").read_text(encoding="utf-8")
        self.assertNotIn("docker-project", resource)
        self.assertNotIn("docker ", main.lower())
        self.assertIn("${TRIM_APPDEST}/runtime/python/bin/python3", main)
        self.assertIn("${TRIM_APPDEST}/service/src/app.py", main)
        self.assertTrue(
            (PROJECT_ROOT / "app/runtime/python/bin/python3").is_file()
        )
        self.assertTrue((PROJECT_ROOT / "app/service/src/app.py").is_file())

    def test_bundled_python_is_linux_64_bit_elf(self) -> None:
        data = self.read_app("runtime/python/bin/python3")[:20]
        self.assertEqual(data[:4], b"\x7fELF")
        self.assertEqual(data[4], 2, "Python 必须是 64 位 ELF")
        self.assertEqual(
            int.from_bytes(data[18:20], "little"),
            EXPECTED_ELF_MACHINE,
        )

    def test_all_elf_files_match_package_architecture(self) -> None:
        elf_files = []
        for name, member in self.app_members.items():
            if not member.isfile():
                continue
            data = self.read_app(name)[:20]
            if data[:4] != b"\x7fELF":
                continue
            elf_files.append(name)
            self.assertEqual(data[4], 2, f"{name} 不是 64 位 ELF")
            self.assertEqual(
                int.from_bytes(data[18:20], "little"),
                EXPECTED_ELF_MACHINE,
                f"{name} 架构与安装包平台不一致",
            )
        self.assertTrue(elf_files, "安装包内未发现 ELF 文件")

    def test_visible_application_name_and_desktop_icon_are_consistent(self) -> None:
        source_manifest = (PROJECT_ROOT / "manifest").read_text(encoding="utf-8")
        source_ui = json.loads(
            (PROJECT_ROOT / "app/ui/config").read_text(encoding="utf-8")
        )
        source_wizard = json.loads(
            (PROJECT_ROOT / "wizard/install").read_text(encoding="utf-8")
        )
        launcher = source_ui[".url"]["clouddl.Application"]

        self.assertIn("display_name = 多网盘下载器", source_manifest)
        self.assertEqual(launcher["title"], "多网盘下载器")
        self.assertEqual(launcher["icon"], "images/icon_{0}.png")
        self.assertTrue(launcher["allUsers"])
        self.assertIn("多网盘下载器", source_wizard[0]["items"][0]["helpText"])

    def test_runtime_version_matches_package_version(self) -> None:
        source_manifest = (PROJECT_ROOT / "manifest").read_text(encoding="utf-8")
        source_app = (PROJECT_ROOT / "app/service/src/app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("version = 1.5.7", source_manifest)
        self.assertIn("platform = x86", source_manifest)
        self.assertNotIn("arch =", source_manifest)
        self.assertIn('APP_VERSION = "1.5.7"', source_app)

    def test_source_requires_original_author_attribution(self) -> None:
        license_text = (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
        notice = (PROJECT_ROOT / "NOTICE").read_text(encoding="utf-8")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        source_app = (PROJECT_ROOT / "app/service/src/app.py").read_text(encoding="utf-8")
        source_ui = (PROJECT_ROOT / "app/service/src/static/app.js").read_text(encoding="utf-8")
        for content in (license_text, notice, readme, source_app, source_ui):
            self.assertIn("xiaocheng154", content)
        self.assertIn("https://github.com/xiaocheng154/nas-cloud-downloader", license_text)


if __name__ == "__main__":
    unittest.main()
