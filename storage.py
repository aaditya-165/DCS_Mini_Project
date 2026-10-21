"""
storage.py
Local file-system abstraction used by every server (primary or backup).

Each server gets its own storage_root directory so multiple servers can
run on the same physical machine without stepping on each other's files.
"""

import shutil
from pathlib import Path


class Storage:
    """
    Thin wrapper around the local file system.
    All paths are treated as *relative* to storage_root, preventing escape
    outside the designated directory (basic sandboxing).
    """

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _safe_path(self, relative_path: str) -> Path:
        """
        Resolve a user-supplied relative path inside the storage root.
        Raises ValueError if the path tries to escape the sandbox.
        """
        # Strip leading slashes so /foo.txt is treated the same as foo.txt
        rel = relative_path.lstrip("/\\")
        target = (self.root / rel).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise ValueError(f"Invalid path: {relative_path}")
        return target

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def read(self, path: str) -> str:
        """Read entire file contents as a string."""
        target = self._safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return target.read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        """Write (create or overwrite) a file."""
        target = self._safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def update(self, path: str, content: str) -> None:
        """Overwrite an existing file. Fails if the file does not exist."""
        target = self._safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        target.write_text(content, encoding="utf-8")

    def delete(self, path: str) -> None:
        """Delete a file."""
        target = self._safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        target.unlink()

    def exists(self, path: str) -> bool:
        """Check whether a file exists."""
        return self._safe_path(path).exists()

    # ------------------------------------------------------------------
    # Bulk helpers (used during recovery sync)
    # ------------------------------------------------------------------

    def list_all_files(self) -> list:
        """
        Return a list of (relative_path, content) for every regular file
        under the storage root.
        """
        files = []
        for item in self.root.rglob("*"):
            if item.is_file():
                rel = str(item.relative_to(self.root)).replace("\\", "/")
                files.append((rel, item.read_text(encoding="utf-8")))
        return files

    def clear_all(self) -> None:
        """Delete every file under the root (used before full sync)."""
        for item in self.root.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
