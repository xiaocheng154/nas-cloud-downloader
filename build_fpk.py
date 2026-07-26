from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = PROJECT_ROOT / "clouddl_x86.fpk"
FNPACK_OUTPUT = PROJECT_ROOT / "clouddl.fpk"
LOCAL_FNPACK = PROJECT_ROOT / "fnpack-1.2.1-windows-amd64.exe"


def find_fnpack() -> str:
    if LOCAL_FNPACK.is_file():
        return str(LOCAL_FNPACK)

    executable = shutil.which("fnpack")
    if executable:
        return executable

    raise FileNotFoundError(
        "未找到 fnOS 官方 fnpack。请将 fnpack-1.2.1-windows-amd64.exe "
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
    return output_path


if __name__ == "__main__":
    built_path = build()
    print(f"已生成：{built_path}（{built_path.stat().st_size} 字节）")
