"""
local_nodes.py — 本地自写 custom_node 的上云通道(不走 git)。

为什么需要它:node_sync 那条路要求节点「能被云端 clone」(有 git remote 或 pyproject 里
写了仓库地址)。自己写的、只存在于本机的节点两条都不满足,过去只能判 missing_no_git ——
识别得出来,但补不了。

这条通道把节点目录直接打包传上 Volume,云端 worker 启动时解压进 /comfyui/custom_nodes/。
相比 git 路线有两个实质好处:
  - **私有代码不必推到 GitHub**(公司资产 / 半成品 / 一次性实验节点)
  - **改一行不用重 build 镜像**:Volume 是运行时挂载,重传 zip 即生效,省掉 3-5 分钟部署

代价:节点的 requirements.txt 只能在 worker 启动时 pip install(每个冷容器付一次),
所以重依赖的节点仍建议走 git 路线烤进镜像。

Volume 布局:
  _local_nodes/<folder>.zip      节点目录打包(已剔除 .git / __pycache__ / 模型文件)
  _local_nodes/<folder>.digest   内容指纹,本地据此判断是否需要重传
"""
import hashlib
import io
import zipfile
from pathlib import Path

VOLUME_PREFIX = "_local_nodes"


def _mv():
    """拿 modal_volume 模块。插件在 ComfyUI 里以包加载(相对导入),而 tests / sync_models.py
    走 sys.path 直接导入 —— 两种上下文都要能用,所以延迟到调用时解析。"""
    try:
        from . import modal_volume  # 包内上下文
    except ImportError:
        import modal_volume  # sys.path 上下文(测试 / 命令行)
    return modal_volume

# 单个节点包上限:自写节点是代码,正常几十 KB~几 MB。超这个数几乎总是误放了模型 /
# 数据集 / 测试素材 —— 与其静默传一个 GB 级 zip(每次冷启动都要解压),不如直接拒绝并说清楚。
MAX_PACK_BYTES = 200 * 1024 * 1024

# 目录级排除:命中即整棵子树跳过
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".idea", ".vscode", ".tox", "dist", "build",
}
# 后缀级排除。模型权重刻意在列:节点目录里的权重该走模型同步(Volume models/ + CAS 去重),
# 塞进节点包既撑爆体积上限,又绕开了去重。
_SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".egg-info",
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft",
    ".zip", ".tar", ".gz", ".7z", ".mp4", ".mov", ".avi",
}
_SKIP_NAMES = {".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes"}


def safe_folder(root: Path, folder: str) -> Path:
    """把「节点文件夹名」解析成 root 下的真实路径,越界即抛错。
    folder 来自 HTTP body(本地 API 无鉴权,同机任意进程/网页都能打)——不能直接 root / folder:
    `../x` 会跑出 custom_nodes,把用户任意目录打包上传。要求单段名 + resolve 后仍在 root 内。"""
    name = (folder or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError(f"非法节点名(必须是 custom_nodes 下的单个目录名): {folder!r}")
    root_res = Path(root).resolve()
    target = (root_res / name).resolve()
    try:
        target.relative_to(root_res)
    except ValueError:
        raise ValueError(f"节点路径越界: {folder!r}") from None
    return target


def should_skip(rel_path: str) -> bool:
    """相对路径是否应排除(纯函数)。rel_path 用 / 分隔。"""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    if not parts:
        return True
    if any(p in _SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in _SKIP_NAMES or name in _SKIP_DIRS:
        return True
    suf = Path(name).suffix.lower()
    return suf in _SKIP_SUFFIXES


def scan_node_dir(path: Path) -> tuple[list[tuple[str, Path, int]], int]:
    """遍历节点目录,返回 ([(rel, abs, size)...], total_bytes)。已按 rel 排序(指纹要稳定)。"""
    path = Path(path)
    files = []
    total = 0
    if not path.is_dir():
        return files, 0
    for p in path.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(path).as_posix()
        if should_skip(rel):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append((rel, p, size))
        total += size
    files.sort(key=lambda x: x[0])
    return files, total


def compute_digest(files: list[tuple[str, Path, int]]) -> str:
    """内容指纹:sha256(每个文件的 相对路径 + 大小 + 内容)。
    刻意不用 mtime —— git checkout / 换机重装会刷新 mtime 但内容没变,那样会导致无谓重传。"""
    h = hashlib.sha256()
    for rel, p, size in files:
        h.update(rel.encode("utf-8"))
        h.update(str(size).encode())
        try:
            with open(p, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


def pack_node_dir(path: Path) -> tuple[bytes, str, int, int]:
    """打包节点目录 → (zip_bytes, digest, file_count, raw_bytes)。
    超 MAX_PACK_BYTES 抛 ValueError(带人话说明)。"""
    path = Path(path)
    files, total = scan_node_dir(path)
    if not files:
        raise ValueError(f"{path.name}: 没有可打包的文件(全被排除规则过滤了?)")
    if total > MAX_PACK_BYTES:
        raise ValueError(
            f"{path.name}: 打包内容 {total // 1024 // 1024}MB 超上限 "
            f"{MAX_PACK_BYTES // 1024 // 1024}MB —— 节点目录里通常是误放了模型/素材,"
            f"模型请放 ComfyUI models/ 走模型同步")
    digest = compute_digest(files)
    buf = io.BytesIO()
    # ZIP_DEFLATED + 固定时间戳:同样内容打出字节一致的包,便于比对与幂等重传
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, p, _size in files:
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())
    return buf.getvalue(), digest, len(files), total


# ============================================================================
# Volume 侧(需要 modal SDK,由 routes 调用)
# ============================================================================
def expected_digests(folders: list[str], root: Path) -> dict:
    """本次提交期望云端跑的那版指纹 {folder: digest}(纯本地算,不查 Volume)。
    随 /run 一起发给 worker —— 暖容器据此发现自己装的是旧版并自我纠偏。"""
    out = {}
    for folder in folders or []:
        try:
            files, _ = scan_node_dir(safe_folder(root, folder))
            if files:
                out[folder] = compute_digest(files)
        except Exception:
            pass
    return out


def volume_digests(cfg: dict, folders: list[str]) -> dict:
    """读 Volume 上已存的 <folder>.digest → {folder: digest}。读不到的不出现在结果里。"""
    out = {}
    try:
        vol = _mv().get_volume(cfg)
        try:
            vol.reload()
        except Exception:
            pass
    except Exception:
        return out
    for folder in folders:
        buf = io.BytesIO()
        try:
            vol.read_file_into_fileobj(f"{VOLUME_PREFIX}/{folder}.digest", buf)
            out[folder] = buf.getvalue().decode("utf-8", "replace").strip()
        except Exception:
            pass
    return out


def plan_local_uploads(cfg: dict, folders: list[str], root: Path) -> dict:
    """哪些本地节点需要传 / 已是最新 / 打包失败。
    返回 {upload: [{folder, digest, files, raw_mb}], uptodate: [folder], failed: [{folder, error}]}"""
    remote = volume_digests(cfg, folders)
    upload, uptodate, failed = [], [], []
    for folder in folders:
        try:
            path = safe_folder(root, folder)
            files, total = scan_node_dir(path)
            if not files:
                failed.append({"folder": folder, "error": "目录为空或全被排除"})
                continue
            digest = compute_digest(files)
            if remote.get(folder) == digest:
                uptodate.append({"folder": folder, "digest": digest})
            else:
                upload.append({"folder": folder, "digest": digest,
                               "files": len(files), "raw_mb": max(1, total // 1024 // 1024)})
        except Exception as e:
            failed.append({"folder": folder, "error": str(e)})
    return {"upload": upload, "uptodate": uptodate, "failed": failed}


def upload_local_nodes(cfg: dict, folders: list[str], root: Path, on_progress=None) -> dict:
    """打包并上传指定节点到 Volume。返回 {uploaded:[{folder,zip_kb,files}], failed:[...]}"""
    uploaded, failed = [], []
    packs = []
    for folder in folders:
        try:
            blob, digest, count, raw = pack_node_dir(safe_folder(root, folder))
            packs.append((folder, blob, digest, count, raw))
        except Exception as e:
            failed.append({"folder": folder, "error": str(e)})
    if not packs:
        return {"uploaded": uploaded, "failed": failed}

    if on_progress:
        on_progress({"phase": "begin", "count": len(packs),
                     "folders": [p[0] for p in packs]})
    vol = _mv().get_volume(cfg)
    with vol.batch_upload(force=True) as batch:  # force:同名覆盖(节点改了就是要覆盖)
        for folder, blob, digest, count, _raw in packs:
            batch.put_file(io.BytesIO(blob), f"{VOLUME_PREFIX}/{folder}.zip")
            batch.put_file(io.BytesIO(digest.encode()), f"{VOLUME_PREFIX}/{folder}.digest")
            # 回传实际上传的那个 digest:提交时必须声明「Volume 上真实存在的版本」,
            # 而不是提交那一刻重新扫目录算出来的(编辑器在两步之间保存一下就对不上了,
            # 结果是声明了一个云端根本没有的版本 → worker 永远刷新不到 → 任务失败)。
            uploaded.append({"folder": folder, "zip_kb": max(1, len(blob) // 1024),
                             "files": count, "digest": digest})
    if on_progress:
        on_progress({"phase": "end", "count": len(uploaded)})
    return {"uploaded": uploaded, "failed": failed}


def list_volume_local_nodes(cfg: dict) -> list[str]:
    """Volume 上现有的本地节点包名单(供「管理云端节点」展示 / 清理)。"""
    try:
        vol = _mv().get_volume(cfg)
        try:
            vol.reload()
        except Exception:
            pass
        return sorted(Path(e.path).name[:-4] for e in vol.listdir(VOLUME_PREFIX)
                      if e.path.endswith(".zip"))
    except Exception:
        return []


def remove_volume_local_node(cfg: dict, folder: str) -> dict:
    """从 Volume 删掉某个本地节点包(zip + digest)。
    ⚠ 返回真实结果,不吞异常:modal_volume.remove_volume_path 是 best-effort(内部吞异常),
      直接用它会让「Modal 不可用 / 删除失败」也显示成 ✓ —— 用户以为清掉了,下次冷启动
      那个包还在,又被解压回去。所以这里自己调 SDK 并如实回报。
    返回 {ok, removed: [path], errors: [{path, error}]}"""
    removed, errors = [], []
    try:
        vol = _mv().get_volume(cfg)
    except Exception as e:
        return {"ok": False, "removed": [], "errors": [{"path": folder, "error": str(e)}]}
    for suffix in (".zip", ".digest"):
        path = f"{VOLUME_PREFIX}/{folder}{suffix}"
        try:
            vol.remove_file(path, recursive=False)
            removed.append(path)
        except Exception as e:
            # 文件本来就不存在算成功(幂等):目标状态「Volume 上没有它」已经达成
            if "not found" in str(e).lower() or "no such" in str(e).lower():
                continue
            errors.append({"path": path, "error": str(e)})
    return {"ok": not errors, "removed": removed, "errors": errors}
