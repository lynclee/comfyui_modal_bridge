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
        # 同目录 rename：读者只能看到完整旧文件或完整新文件。并发保存各自有独立临时文件。
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
