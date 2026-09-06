"""本地下载回执：只有本插件确认完成且未被修改的文件才可用于重试。

放在私有 user 目录，不与可下载的产物混放。scope 包含云端、job 和 completed_at，
避免同名输出或重跑的 job 误用旧文件。回执不包含凭据或图像内容。
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path


class ResultReceipts:
    def __init__(self, root: Path, scope: list):
        self.root = root
        self.scope = scope

    def _path(self, source: str, local: Path) -> Path:
        key = json.dumps([self.scope, source, str(local.absolute())], ensure_ascii=True)
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    @staticmethod
    def _stat(local: Path) -> list[int]:
        if local.is_symlink() or not local.is_file():
            raise ValueError("not a regular output file")
        st = local.stat()
        return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev]

    def completed_size(self, source: str, local: Path) -> int | None:
        try:
            saved = json.loads(self._path(source, local).read_text(encoding="utf-8"))
            current = self._stat(local)
            return current[0] if saved == current else None
        except (OSError, ValueError):
            return None

    def record(self, source: str, local: Path) -> None:
        data = self._stat(local)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(source, local)
        fd, name = tempfile.mkstemp(prefix="receipt-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
