from __future__ import annotations

from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg",
    ".png", ".tif", ".tiff", ".webp",
}


class LocalFileManager:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def set_root(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def resolve(self, virtual_path: str, *, must_exist: bool = True) -> Path:
        value = str(virtual_path or "/").replace("\\", "/")
        relative = value.lstrip("/")
        candidate = (self.root / relative).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("路径超出 NAS 下载目录")
        if must_exist and not candidate.exists():
            raise FileNotFoundError("文件或文件夹不存在")
        return candidate

    @staticmethod
    def validate_name(name: str) -> str:
        value = str(name).strip()
        if not value or value in {".", ".."}:
            raise ValueError("新名称不能为空")
        if any(character in value for character in ("/", "\\", "\0")):
            raise ValueError("新名称不能包含路径分隔符")
        return value

    def _entry(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        relative = path.relative_to(self.root).as_posix()
        virtual_path = f"/{relative}" if relative != "." else "/"
        is_dir = path.is_dir()
        return {
            "name": path.name,
            "path": virtual_path,
            "is_dir": is_dir,
            "size": 0 if is_dir else stat.st_size,
            "mtime": stat.st_mtime,
            "has_thumbnail": not is_dir and path.suffix.lower() in IMAGE_EXTENSIONS,
        }

    def list_files(self, virtual_path: str = "/") -> dict[str, Any]:
        directory = self.resolve(virtual_path)
        if not directory.is_dir():
            raise ValueError("目标不是文件夹")
        items: list[dict[str, Any]] = []
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            raise ValueError(f"无法读取目录：{exc}") from exc
        for child in children:
            try:
                items.append(self._entry(child))
            except OSError:
                continue
        return {"success": True, "path": virtual_path, "files": items, "total": len(items)}

    def search(self, keyword: str, virtual_path: str = "/", limit: int = 300) -> dict[str, Any]:
        directory = self.resolve(virtual_path)
        query = keyword.casefold()
        files: list[dict[str, Any]] = []
        for child in directory.rglob("*"):
            if query not in child.name.casefold():
                continue
            try:
                resolved = child.resolve(strict=False)
                if resolved != self.root and self.root not in resolved.parents:
                    continue
                files.append(self._entry(child))
            except OSError:
                continue
            if len(files) >= limit:
                break
        return {"success": True, "files": files, "total": len(files)}

    def rename(self, virtual_path: str, new_name: str) -> dict[str, Any]:
        source = self.resolve(virtual_path)
        if source == self.root:
            raise ValueError("不能重命名下载根目录")
        safe_name = self.validate_name(new_name)
        destination = (source.parent / safe_name).resolve(strict=False)
        if destination.parent != source.parent:
            raise ValueError("新名称无效")
        if destination.exists() and destination != source:
            raise FileExistsError("同名文件或文件夹已存在")
        source.rename(destination)
        return {"success": True, "name": safe_name, "path": self._entry(destination)["path"]}

    def thumbnail_path(self, virtual_path: str) -> Path:
        path = self.resolve(virtual_path)
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError("该文件不是支持的图片格式")
        return path
