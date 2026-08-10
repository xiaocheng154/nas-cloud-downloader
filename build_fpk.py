from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = PROJECT_ROOT / "clouddl_x86.fpk"
FNPACK_OUTPUT = PROJECT_ROOT / "clouddl.fpk"
LOCAL_FNPACK = PROJECT_ROOT / "fnpack-1.2.3-windows-amd64.exe"


def find_fnpack() -> str:
    if LOCAL_FNPACK.is_file():
        return str(LOCAL_FNPACK)

    executable = shutil.which("fnpack")
    if executable:
        return executable

    raise FileNotFoundError(
        "未找到 fnOS 官方 fnpack。请将 fnpack-1.2.3-windows-amd64.exe "
        "放在项目根目录，或把 fnpack 加入 PATH。"
    )


def remove_generated_caches() -> None:
    app_root = (PROJECT_ROOT / "app").resolve()
    for cache_dir in app_root.rglob("__pycache__"):
        resolved = cache_dir.resolve()
        if not resolved.is_relative_to(app_root):
            raise RuntimeError(f"拒绝清理 app 目录之外的路径：{resolved}")
        shutil.rmtree(resolved)
    for compiled_file in app_root.rglob("*.pyc"):
        resolved = compiled_file.resolve()
        if not resolved.is_relative_to(app_root):
            raise RuntimeError(f"拒绝清理 app 目录之外的路径：{resolved}")
        resolved.unlink()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def audit_package(package: Path) -> int:
    with tarfile.open(package, "r:*") as outer:
        manifest_file = outer.extractfile("manifest")
        app_file = outer.extractfile("app.tgz")
        if manifest_file is None or app_file is None:
            raise RuntimeError("安装包缺少 manifest 或 app.tgz")
        manifest = {}
        for line in manifest_file.read().decode("utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                manifest[key.strip()] = value.strip()
        if manifest.get("platform") != "x86":
            raise RuntimeError("安装包未声明为 fnOS x86 平台")
        if "arch" in manifest:
            raise RuntimeError("安装包仍包含已废弃的 arch 字段")
        app_data = app_file.read()

    elf_count = 0
    required = {
        "runtime/python/bin/python3",
        "runtime/aria2/aria2c",
        "service/vendor/pydantic_core/_pydantic_core.cpython-311-x86_64-linux-gnu.so",
    }
    found = set()
    with tarfile.open(fileobj=io.BytesIO(app_data), mode="r:gz") as app_archive:
        for member in app_archive.getmembers():
            if not member.isfile():
                continue
            if member.name in required:
                found.add(member.name)
            source = app_archive.extractfile(member)
            if source is None:
                continue
            header = source.read(20)
            if header[:4] != b"\x7fELF":
                continue
            elf_count += 1
            machine = int.from_bytes(header[18:20], "little")
            if header[4] != 2 or machine != 62:
                raise RuntimeError(f"包内存在非 x86_64 ELF：{member.name}={machine}")
    if missing := required - found:
        raise RuntimeError("安装包缺少 x86_64 运行依赖：" + ", ".join(sorted(missing)))
    if elf_count == 0:
        raise RuntimeError("安装包内未发现 x86_64 ELF")
    return elf_count


def build(output_path: Path = OUTPUT_PATH) -> Path:
    remove_generated_caches()
    if FNPACK_OUTPUT.exists():
        FNPACK_OUTPUT.unlink()
    result = subprocess.run(
        [find_fnpack(), "build", "--directory", str(PROJECT_ROOT)],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"fnpack 构建失败，退出码：{result.returncode}")
    if not FNPACK_OUTPUT.is_file():
        raise RuntimeError("fnpack 未生成安装包，请查看上方官方校验器输出。")

    FNPACK_OUTPUT.replace(output_path)
    elf_count = audit_package(output_path)
    print(f"x86_64 ELF files: {elf_count}")
    print(f"SHA256: {sha256(output_path)}")
    return output_path


if __name__ == "__main__":
    built_path = build()
    print(f"已生成：{built_path}（{built_path.stat().st_size} 字节）")
