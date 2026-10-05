"""私有 JSON 原子写入；CLI 和插件配置共用，不依赖 ComfyUI。

mkstemp 以 O_EXCL 创建全新的随机临时文件，POSIX 权限从创建起就是 0600。
不复用固定 .tmp，不跟随旧临时文件的符号链接，也不需要事后 chmod。
"""
import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, data: dict) -> None:
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            stream.write(payload)
            # replace 之前先把内容落盘:不 fsync 的话,断电后可能出现「rename 已生效、内容还在
            # 页缓存里」的空文件 / 半个 JSON —— 现在那会被 load_config 判成损坏、拒绝一切写入
            # (2026-10-05 深度 review)。
            stream.flush()
            os.fsync(stream.fileno())
        # 同目录 rename：读者只能看到完整旧文件或完整新文件。并发保存各自有独立临时文件。
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    """把目录项(rename 本身)也落盘。Windows 打不开目录 fd,跳过即可 —— NTFS 的
    MoveFileEx 由文件系统日志保证;尽力而为,失败不影响已经完成的替换。"""
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
