"""本地下载回执：只有本插件确认完成且未被修改的文件才可用于重试。

放在私有 user 目录，不与可下载的产物混放。scope 包含云端、job 和 completed_at，
避免同名输出或重跑的 job 误用旧文件。回执不包含凭据或图像内容。
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

# 回执只在「成功响应丢失 / 进程重启后再次取回」时有用,云端 _outputs 的 GC 远短于此。
# 不清的话 download_receipts/ 只增不删,每个取回过的产物留一个文件(2026-10-05 深度 review)。
RECEIPT_MAX_AGE_S = 30 * 24 * 3600


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

    def prune(self, max_age_s: float = RECEIPT_MAX_AGE_S) -> int:
        """按 mtime 删掉超过 max_age_s 的回执(连同崩溃残留的 receipt-*.tmp),返回删了几个。

        与 scope 无关:回执目录是全插件共用的。尽力而为,任何一个文件删不掉都跳过。"""
        cutoff = time.time() - max_age_s
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return 0
        for p in entries:
            if not (p.suffix == ".json" or (p.name.startswith("receipt-") and p.suffix == ".tmp")):
                continue
            try:
                if p.is_symlink() or not p.is_file() or p.stat().st_mtime >= cutoff:
                    continue
                p.unlink()
                removed += 1
            except OSError:
                continue
        return removed
