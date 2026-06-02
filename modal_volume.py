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


def volume_files_by_type(cfg, types) -> dict:
    """返回 {type: set(filename)},一个 type 一次 listdir(只查需要的 type,省往返)。"""
    vol = get_volume(cfg)
    out = {}
    for t in sorted(set(types)):
        try:
            out[t] = {Path(e.path).name for e in vol.listdir(f"models/{t}")}
        except Exception:
            out[t] = set()  # 该 type 目录在 Volume 还不存在
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
def upload_models(cfg: dict, items: list, on_progress=None) -> dict:
    """
    items: [{type, filename, local_path}, ...]
    on_progress(idx, total, item): 每个文件开始上传前回调(可选,流式日志用)

    返回 {uploaded: [...], skipped: [...], total_mb: int}
    Modal Volume CAS 块级去重:相同内容(网上通用大模型)秒过,只有新内容真正占上行带宽。
    """
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
        vol = get_volume(cfg)
        with vol.batch_upload(force=False) as batch:
            for i, (it, local) in enumerate(pending):
                size_mb = local.stat().st_size // 1024 // 1024
                total_mb += size_mb
                if on_progress:
                    on_progress(i, len(pending), {**it, "size_mb": size_mb})
                batch.put_file(str(local), f"models/{it['type']}/{it['filename']}")
                uploaded.append({**it, "size_mb": size_mb})
    return {"uploaded": uploaded, "skipped": skipped, "total_mb": total_mb}


def modal_importable() -> bool:
    try:
        import modal  # noqa: F401
        return True
    except Exception:
        return False
