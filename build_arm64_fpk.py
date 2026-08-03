from __future__ import annotations

import hashlib
import io
import posixpath
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parent
BUILD_ROOT = PROJECT_ROOT / ".arm64-build"
DOWNLOAD_ROOT = BUILD_ROOT / "downloads"
STAGING_ROOT = BUILD_ROOT / "staging"
OUTPUT_PATH = PROJECT_ROOT / "clouddl_arm64.fpk"
FNPACK = PROJECT_ROOT / "fnpack-1.2.3-windows-amd64.exe"

PYTHON_ARCHIVE = (
    DOWNLOAD_ROOT
    / "cpython-3.11.15+20260728-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz"
)
PYTHON_SHA256 = "a6decd180099e6768269bd8e8968aeaa84bb01511f00f6921dc204fe648729aa"
PYDANTIC_CORE_WHEEL = (
    DOWNLOAD_ROOT
    / "pydantic_core-2.46.4-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def reset_staging() -> None:
    resolved_build = BUILD_ROOT.resolve()
    resolved_staging = STAGING_ROOT.resolve()
    if not resolved_staging.is_relative_to(resolved_build):
        raise RuntimeError(f"Unsafe staging path: {resolved_staging}")
    if STAGING_ROOT.exists():
        shutil.rmtree(STAGING_ROOT)


def copy_project() -> None:
    excluded_names = {
        ".git",
        ".venv",
        ".arm64-build",
        ".preview",
        ".playwright-cli",
        "output",
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in excluded_names}
        ignored.update(name for name in names if name.endswith(".fpk"))
        ignored.update(name for name in names if name.startswith("fnpack-"))
        return ignored

    shutil.copytree(PROJECT_ROOT, STAGING_ROOT, ignore=ignore)


def install_python_runtime() -> None:
    if not PYTHON_ARCHIVE.is_file():
        raise FileNotFoundError(PYTHON_ARCHIVE)
    actual_hash = sha256(PYTHON_ARCHIVE)
    if actual_hash != PYTHON_SHA256:
        raise RuntimeError(f"Python archive SHA-256 mismatch: {actual_hash}")

    runtime_root = STAGING_ROOT / "app" / "runtime" / "python"
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    runtime_root.mkdir(parents=True)

    prefix = PurePosixPath("python")
    links: dict[PurePosixPath, PurePosixPath] = {}
    with tarfile.open(PYTHON_ARCHIVE, "r|gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            if not relative.parts or not (member.isfile() or member.issym() or member.islnk()):
                continue
            destination = runtime_root.joinpath(*relative.parts)
            if member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Unable to extract {member.name}")
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                continue

            if member.issym():
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(member.name), member.linkname)
                )
            else:
                target = posixpath.normpath(member.linkname)
            target_path = PurePosixPath(target)
            try:
                relative_target = target_path.relative_to(prefix)
            except ValueError as error:
                raise RuntimeError(
                    f"Unsafe tar link: {member.name} -> {target}"
                ) from error
            links[relative] = relative_target

    def resolve_link(path: PurePosixPath, seen: set[PurePosixPath]) -> Path:
        if path in seen:
            raise RuntimeError(f"Tar link cycle: {path}")
        if path in links:
            return resolve_link(links[path], seen | {path})
        target = runtime_root.joinpath(*path.parts)
        if not target.is_file():
            raise RuntimeError(f"Missing tar link target: {path}")
        return target

    for relative, target in links.items():
        destination = runtime_root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = resolve_link(target, {relative})
        if destination.exists() and source.samefile(destination):
            continue
        shutil.copyfile(source, destination)


def install_pydantic_core() -> None:
    if not PYDANTIC_CORE_WHEEL.is_file():
        raise FileNotFoundError(PYDANTIC_CORE_WHEEL)

    vendor = STAGING_ROOT / "app" / "service" / "vendor"
    for path in vendor.glob("pydantic_core*"):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    with zipfile.ZipFile(PYDANTIC_CORE_WHEEL) as wheel:
        for item in wheel.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"Unsafe wheel path: {item.filename}")
            if item.is_dir():
                continue
            destination = vendor.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(wheel.read(item))


def remove_generated_caches() -> None:
    app_root = (STAGING_ROOT / "app").resolve()
    for cache_dir in app_root.rglob("__pycache__"):
        resolved = cache_dir.resolve()
        if not resolved.is_relative_to(app_root):
            raise RuntimeError(f"Unsafe cache path: {resolved}")
        shutil.rmtree(resolved)
    for compiled_file in app_root.rglob("*.pyc"):
        resolved = compiled_file.resolve()
        if not resolved.is_relative_to(app_root):
            raise RuntimeError(f"Unsafe compiled file path: {resolved}")
        resolved.unlink()


def set_arm_manifest() -> None:
    manifest = STAGING_ROOT / "manifest"
    content = manifest.read_text(encoding="utf-8")
    if "platform = x86" not in content:
        raise RuntimeError("Expected fnOS x86 platform marker was not found")
    manifest.write_text(
        content.replace("platform = x86", "platform = arm"),
        encoding="utf-8",
    )


def audit_elf_tree(root: Path) -> list[Path]:
    elf_files: list[Path] = []
    wrong_architecture: list[tuple[Path, int]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as source:
            header = source.read(20)
        if header[:4] != b"\x7fELF":
            continue
        elf_files.append(path)
        machine = int.from_bytes(header[18:20], "little")
        if header[4] != 2 or machine != 183:
            wrong_architecture.append((path, machine))

    if wrong_architecture:
        details = ", ".join(f"{path.relative_to(root)}={machine}" for path, machine in wrong_architecture)
        raise RuntimeError(f"Non-AArch64 ELF files found: {details}")
    if not elf_files:
        raise RuntimeError("No AArch64 ELF files found")
    required = {
        root / "runtime" / "python" / "bin" / "python3",
        root
        / "service"
        / "vendor"
        / "pydantic_core"
        / "_pydantic_core.cpython-311-aarch64-linux-gnu.so",
    }
    missing = required.difference(elf_files)
    if missing:
        raise RuntimeError(
            "Required AArch64 ELF files are missing: "
            + ", ".join(str(path.relative_to(root)) for path in sorted(missing))
        )
    return elf_files


def build_package() -> Path:
    if not FNPACK.is_file():
        raise FileNotFoundError(FNPACK)
    generated = STAGING_ROOT / "clouddl.fpk"
    result = subprocess.run(
        [str(FNPACK), "build", "--directory", str(STAGING_ROOT)],
        cwd=STAGING_ROOT,
        check=False,
    )
    if result.returncode != 0 or not generated.is_file():
        raise RuntimeError(f"fnpack build failed: {result.returncode}")
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()
    shutil.move(generated, OUTPUT_PATH)
    return OUTPUT_PATH


def audit_package(package: Path) -> int:
    with tarfile.open(package, "r:*") as outer:
        manifest = outer.extractfile("manifest")
        app_tgz = outer.extractfile("app.tgz")
        if manifest is None or app_tgz is None:
            raise RuntimeError("Invalid FPK layout")
        manifest_values = {}
        for line in manifest.read().decode("utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                manifest_values[key.strip()] = value.strip()
        if manifest_values.get("platform") != "arm":
            raise RuntimeError("Packaged manifest is not marked for fnOS ARM")
        if "arch" in manifest_values:
            raise RuntimeError("Packaged manifest still contains deprecated arch field")
        app_data = app_tgz.read()

    elf_count = 0
    with tarfile.open(fileobj=io.BytesIO(app_data), mode="r:gz") as app:
        for member in app.getmembers():
            if not member.isfile():
                continue
            source = app.extractfile(member)
            if source is None:
                continue
            header = source.read(20)
            if header[:4] != b"\x7fELF":
                continue
            elf_count += 1
            machine = int.from_bytes(header[18:20], "little")
            if header[4] != 2 or machine != 183:
                raise RuntimeError(f"Packaged non-AArch64 ELF: {member.name}={machine}")
    if elf_count == 0:
        raise RuntimeError("Packaged app contains no AArch64 ELF files")
    return elf_count


def main() -> None:
    reset_staging()
    copy_project()
    install_python_runtime()
    install_pydantic_core()
    remove_generated_caches()
    set_arm_manifest()
    staged_elfs = audit_elf_tree(STAGING_ROOT / "app")
    package = build_package()
    packaged_elf_count = audit_package(package)
    print(f"Built: {package}")
    print(f"Size: {package.stat().st_size}")
    print(f"SHA256: {sha256(package)}")
    print(f"AArch64 ELF files: staged={len(staged_elfs)}, packaged={packaged_elf_count}")


if __name__ == "__main__":
    main()
