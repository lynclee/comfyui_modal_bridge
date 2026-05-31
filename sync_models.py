"""
sync_models.py — 把本地 ComfyUI/models 同步到 Modal Volume(增量 + block 去重)

适用:HF 下老出错的 gated 模型(本地下好再传)、自训练/私有模型(HF 没有的)。
原理:扫本地 models/<type>/,和 Volume 现有比对,只上传 Volume 缺的;Modal Volume
     做 block 级去重,相同内容/重复跑秒过。

用法(在本机终端,需本机能 import modal —— 跑过一次 GUI 部署即已装):
    python sync_models.py            # 同步所有缺的
    python sync_models.py --dry-run  # 只看差异,不上传
    python sync_models.py --type loras   # 只同步某个类型目录

鉴权:自动读 ComfyUI/user/default/modal_bridge/config.json 里的 modal token。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMFY = HERE.parents[1]                                   # ComfyUI 根
MODELS = COMFY / "models"
CONFIG = COMFY / "user" / "default" / "modal_bridge" / "config.json"
MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx"}


def main():
    dry = "--dry-run" in sys.argv
    only_type = None
    if "--type" in sys.argv:
        i = sys.argv.index("--type")
        if i + 1 < len(sys.argv):
            only_type = sys.argv[i + 1]

    if not CONFIG.exists():
        print(f"✗ 找不到 config: {CONFIG}\n  先在 ComfyUI 里点 [⚙️ Modal Setup] 部署一次(会生成 config)")
        sys.exit(1)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if cfg.get("modal_token_id"):
        os.environ["MODAL_TOKEN_ID"] = cfg["modal_token_id"]
    if cfg.get("modal_token_secret"):
        os.environ["MODAL_TOKEN_SECRET"] = cfg["modal_token_secret"]
    vol_name = cfg.get("modal_volume_name", "comfyui-bridge-models")

    try:
        import modal
    except ImportError:
        print("✗ 本机 Python 没装 modal。先 `pip install modal`,或用跑过部署的那个 Python 解释器")
        sys.exit(1)

    vol = modal.Volume.from_name(vol_name)
    print(f"Volume: {vol_name}   本地 models: {MODELS}")

    def vol_existing(type_: str) -> set:
        try:
            return {Path(e.path).name for e in vol.listdir(f"models/{type_}")}
        except Exception:
            return set()  # 该类型目录在 Volume 还不存在

    if not MODELS.exists():
        print(f"✗ 找不到本地 models 目录: {MODELS}")
        sys.exit(1)

    to_upload = []  # (local_path, remote_path, type, name, size_mb)
    for type_dir in sorted(MODELS.iterdir()):
        if not type_dir.is_dir():
            continue
        type_ = type_dir.name
        if only_type and type_ != only_type:
            continue
        local_files = [f for f in type_dir.rglob("*")
                       if f.is_file() and f.suffix.lower() in MODEL_EXTS]
        if not local_files:
            continue  # 本地该类型没模型 → 不必查 Volume(省一次网络往返)
        existing = vol_existing(type_)
        for f in local_files:
            if f.name in existing:
                continue
            size_mb = f.stat().st_size // 1024 // 1024
            to_upload.append((f, f"models/{type_}/{f.name}", type_, f.name, size_mb))

    if not to_upload:
        print("✓ 本地模型都已在 Volume(或本地没有可同步的模型),无需同步")
        return

    total = sum(x[4] for x in to_upload)
    print(f"\n待同步 {len(to_upload)} 个文件,共 ~{total} MB:")
    for _, _, t, n, s in to_upload:
        print(f"  {t}/{n}  ({s} MB)")

    if dry:
        print("\n(--dry-run,未上传)")
        return

    print("\n开始上传(block 去重,相同内容秒过;大文件走你的上行带宽,耐心等)...")
    with vol.batch_upload(force=False) as batch:
        for local, remote, t, n, s in to_upload:
            print(f"  ↑ {t}/{n} ({s} MB)")
            batch.put_file(str(local), remote)
    print("\n✓ 同步完成。这些模型现在 Volume 里有了,工作流用到时会直接命中(秒过 seeding)。")


if __name__ == "__main__":
    main()
