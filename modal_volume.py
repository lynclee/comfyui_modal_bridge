"""
modal_volume.py — 本地直接用 Modal SDK 操作 Volume(查 + 上传),不经任何 Modal endpoint。

为什么本地能直接做:跑过一次 GUI 部署后,ComfyUI 内嵌 Python 里已装 modal,且 config.json
里有 modal token。于是本地就能 `modal.Volume.from_name(...)` 列目录 / batch_upload,
通用大模型靠 Modal Volume 的 CAS 块级去重秒过(相同内容不重复上传)。

模型策略(新):模型都在本地 ComfyUI Desktop 下好 → 提交前查 Volume 缺哪些 → 本地有的直接
上传上去。不再从 HF/civitai 下载。

被 routes.py(GUI 提交链路)和 sync_models.py(命令行)共用。
"""
import os
import time
from pathlib import Path

MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx"}

# 互为别名的模型目录(ComfyUI 同一池子):查 Volume 时一并查别名目录,否则模型传在
# diffusion_models/ 但工作流声明 unet 会误判缺失、重传两份。和 routes._TYPE_ALIASES 对齐。
_VOL_TYPE_ALIASES = {
    "diffusion_models": ["unet"],
    "unet": ["diffusion_models"],
    "text_encoders": ["clip"],
    "clip": ["text_encoders"],
}

# 下载中的临时 / 控制文件后缀(出现这些 = 旁边那个最终名文件还没下完)
_INPROGRESS_SUFFIXES = (".part", ".tmp", ".download", ".incomplete", ".crdownload", ".aria2", ".st")


def _has_inprogress_sibling(path: Path) -> bool:
    """<name>.aria2 / <name>.part 之类存在 → 这个模型还在下。"""
    for suf in _INPROGRESS_SUFFIXES:
        if (path.parent / (path.name + suf)).exists():
            return True
    return False


def file_in_progress(path: Path, settle_check: bool = True) -> bool:
    """判断模型文件是否还在下载中(不该上传):0 字节 / 有下载控制文件 / 体积还在变大。"""
    try:
        st = path.stat()
    except OSError:
        return True
    if st.st_size == 0:
        return True
    if _has_inprogress_sibling(path):
        return True
    if settle_check:
        # 旁边没控制文件、但有的下载器直接按最终名原地写 → 隔一下看体积是否还在涨
        time.sleep(0.8)
        try:
            st2 = path.stat()
        except OSError:
            return True
        if st2.st_size != st.st_size:
            return True
    return False


# ============================================================================
# Modal SDK 接入
# ============================================================================
def apply_token(cfg: dict) -> None:
    """把 config 里的 modal token 注入环境变量(modal SDK 据此鉴权)。"""
    if cfg.get("modal_token_id"):
        os.environ["MODAL_TOKEN_ID"] = cfg["modal_token_id"]
    if cfg.get("modal_token_secret"):
        os.environ["MODAL_TOKEN_SECRET"] = cfg["modal_token_secret"]


def get_volume(cfg: dict):
    """拿到 Volume 句柄(本地 SDK)。调用方负责确保已 import modal 成功。"""
    import modal
    apply_token(cfg)
    vol_name = cfg.get("modal_volume_name", "comfyui-bridge-models")
    return modal.Volume.from_name(vol_name, create_if_missing=True)


def _listdir_names(vol, type_) -> set:
    try:
        return {Path(e.path).name for e in vol.listdir(f"models/{type_}")}
    except Exception:
        return set()  # 该 type 目录在 Volume 还不存在


def volume_files_by_type(cfg, types) -> dict:
    """返回 {type: set(filename)}。查询前 reload() 刷新元数据:否则刚 batch_upload 提交的文件
    在最终一致性窗口内可能 listdir 看不到 → 误判"缺失"→ 又触发上传(用户遇到的"传过了还触发")。
    每个 type 连同其别名目录(unet↔diffusion_models 等)一并查,避免传在别名目录的模型被误判缺失。"""
    vol = get_volume(cfg)
    try:
        vol.reload()
    except Exception:
        pass
    out = {}
    for t in sorted(set(types)):
        names = _listdir_names(vol, t)
        for alias in _VOL_TYPE_ALIASES.get(t, []):
            names |= _listdir_names(vol, alias)  # 并入别名目录的文件
        out[t] = names
    return out


# ============================================================================
# 本地模型查找
# ============================================================================
def find_local_model(type_: str, filename: str, roots) -> Path | None:
    """在给定若干根目录里找 <filename>(先平铺再递归)。roots: 该 type 对应的本地目录列表。"""
    base = Path(filename).name  # 容错:workflow 里偶有 "subdir/x.safetensors"
    for root in roots:
        r = Path(root)
        if not r.exists():
            continue
        direct = r / filename
        if direct.is_file():
            return direct
        flat = r / base
        if flat.is_file():
            return flat
        # 递归兜底(Desktop 有人按子目录归类模型)
        for hit in r.rglob(base):
            if hit.is_file():
                return hit
    return None


# ============================================================================
# 检查:工作流要的模型,Volume 有没有 / 本地能不能补
# ============================================================================
def check_models(cfg: dict, required: list, resolver) -> dict:
    """
    required: [{type, filename}, ...](由 routes.extract_required_models 从 prompt 解析)
    resolver: (type_, filename) -> Path|None,定位本地模型文件(routes 用 folder_paths)

    返回:
      {
        "required": [...],
        "present":  [{type, filename}],            # Volume 已有
        "missing_local": [{type, filename, local_path, size_mb}],  # Volume 没、本地有且下载完 → 可上传
        "downloading": [{type, filename}],         # Volume 没、本地有但还在下载中 → 先别传(会传成残缺)
        "missing_no_source": [{type, filename}],   # Volume 没、本地也没 → 没法自动补
      }
    """
    if not required:
        return {"required": [], "present": [], "missing_local": [], "downloading": [], "missing_no_source": []}

    types = [r["type"] for r in required]
    have = volume_files_by_type(cfg, types)

    present, missing_local, downloading, missing_no_source = [], [], [], []
    for item in required:
        t, fn = item["type"], item["filename"]
        base = Path(fn).name
        if base in have.get(t, set()) or fn in have.get(t, set()):
            present.append({"type": t, "filename": fn})
            continue
        local = resolver(t, fn)
        if local is None:
            missing_no_source.append({"type": t, "filename": fn})
        elif file_in_progress(local):
            downloading.append({"type": t, "filename": base})
        else:
            missing_local.append({
                "type": t, "filename": base,
                "local_path": str(local),
                "size_mb": local.stat().st_size // 1024 // 1024,
            })
    return {
        "required": required,
        "present": present,
        "missing_local": missing_local,
        "downloading": downloading,
        "missing_no_source": missing_no_source,
    }


# ============================================================================
# 上传:本地文件 → Volume(batch_upload,CAS 去重)
# ============================================================================
def _fmt_eta(sec: float) -> str:
    """秒 → 人类可读时长(通用小工具,有单测)。"""
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def upload_models(cfg: dict, items: list, on_progress=None) -> dict:
    """
    items: [{type, filename, local_path}, ...]
    on_progress(event): 流式进度回调(可选)。event 形态:
      {phase:"begin", count, total_mb, files:[{name,size_mb}]}  # 开始前:列出要传的文件
      {phase:"end",   count, total_mb, secs, rate_mbps}         # 全部传完:总耗时 + 均速

    ⚠ 为什么没有逐文件实时进度:Modal 的 batch_upload 是 context manager,所有 put_file 只是
    "登记",真正的上传/提交发生在 with 块退出那一刻(一次性并行传)。所以循环里测不到单文件
    真实速率(测出来≈0,是假的)。这里只给"开始(列清单)+ 结束(总耗时/均速)"两个真实事件。

    返回 {uploaded: [...], skipped: [...], total_mb: int}
    Modal Volume CAS 块级去重:相同内容(网上通用大模型)秒过,只有新内容真正占上行带宽。
    """
    import time
    if not items:
        return {"uploaded": [], "skipped": [], "total_mb": 0}

    # 上传前再查一次 Volume:并发/重试时另一个请求可能刚把同一个模型传完了 →
    # 已存在的直接跳过,既不重传(省带宽)、也避免两个 batch 写同一路径撞车。
    have = volume_files_by_type(cfg, {it["type"] for it in items})

    uploaded, skipped, total_mb = [], [], 0
    pending = []
    for it in items:
        local = Path(it["local_path"])
        if not local.is_file():
            skipped.append({**it, "reason": "local file missing"})
            continue
        if file_in_progress(local, settle_check=False):
            skipped.append({**it, "reason": "still downloading"})
            continue
        if it["filename"] in have.get(it["type"], set()):
            skipped.append({**it, "reason": "already in volume"})
            continue
        pending.append((it, local))

    if pending:
        sizes = [max(1, Path(p[1]).stat().st_size // 1024 // 1024) for p in pending]
        grand_total = sum(sizes)
        if on_progress:
            on_progress({"phase": "begin", "count": len(pending), "total_mb": grand_total,
                         "files": [{"name": f"{it['type']}/{it['filename']}", "size_mb": sz}
                                   for (it, _), sz in zip(pending, sizes)]})
        vol = get_volume(cfg)
        t0 = time.time()
        with vol.batch_upload(force=False) as batch:  # 真正上传在此 with 退出时一次性发生
            for (it, local), sz in zip(pending, sizes):
                batch.put_file(str(local), f"models/{it['type']}/{it['filename']}")
                total_mb += sz
                uploaded.append({**it, "size_mb": sz})
        secs = time.time() - t0
        if on_progress:
            on_progress({"phase": "end", "count": len(pending), "total_mb": grand_total,
                         "secs": round(secs, 1),
                         "rate_mbps": round(grand_total / secs, 1) if secs > 0 else 0})
    return {"uploaded": uploaded, "skipped": skipped, "total_mb": total_mb}


def download_volume_file(cfg: dict, vol_path: str, local_path: str) -> int:
    """从 Volume 把 vol_path 直连下载到本地 local_path(同步阻塞)。返回字节数。
    大产物(视频/3D)走这条,避开 base64+modal.Dict 上限 + 省一道浏览器中转。"""
    vol = get_volume(cfg)
    try:
        vol.reload()  # 最终一致:worker 刚 commit,本地读前刷新一下视图
    except Exception:
        pass
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "wb") as f:
        return vol.read_file_into_fileobj(vol_path, f)


def remove_volume_path(cfg: dict, vol_path: str) -> None:
    """下完删掉 Volume 上的 vol_path(避免 _outputs 堆积)。失败忽略。"""
    try:
        get_volume(cfg).remove_file(vol_path, recursive=True)
    except Exception:
        pass


def modal_importable() -> bool:
    try:
        import modal  # noqa: F401
        return True
    except Exception:
        return False
