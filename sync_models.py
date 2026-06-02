"""
sync_models.py — 把本地 ComfyUI/models 整体同步到 Modal Volume(命令行,批量)

GUI 提交时会自动只同步当前工作流要、且 Volume 缺的模型(routes.py + modal_volume.py)。
这个脚本用于"一次性把本地模型库整体推上去",省得每个工作流第一次跑时等上传。

原理:扫本地 models/<type>/,和 Volume 现有比对,只传 Volume 缺的。Modal Volume 块级
     去重(CAS):网上通用大模型秒过,只有新内容真正占上行带宽。

用法(本机终端,需本机能 import modal —— 跑过一次 GUI 部署即已装):
    python sync_models.py            # 同步所有缺的
    python sync_models.py --dry-run  # 只看差异,不上传
    python sync_models.py --type loras   # 只同步某个类型目录

鉴权:自动读 ComfyUI/user/default/modal_bridge/config.json 里的 modal token。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMFY = HERE.parents[1]                                   # ComfyUI 根
MODELS = COMFY / "models"
CONFIG = COMFY / "user" / "default" / "modal_bridge" / "config.json"

sys.path.insert(0, str(HERE))
import modal_volume  # noqa: E402  (同目录,复用查/传逻辑)


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

    if not modal_volume.modal_importable():
        print("✗ 本机 Python 没装 modal。先 `pip install modal`,或用跑过部署的那个 Python 解释器")
        sys.exit(1)
    if not MODELS.exists():
        print(f"✗ 找不到本地 models 目录: {MODELS}")
        sys.exit(1)

    vol_name = cfg.get("modal_volume_name", "comfyui-bridge-models")
    print(f"Volume: {vol_name}   本地 models: {MODELS}")

    # 扫本地每个 type 目录下的模型文件
    type_dirs = [d for d in sorted(MODELS.iterdir())
                 if d.is_dir() and (not only_type or d.name == only_type)]
    types_with_files = {}
    for d in type_dirs:
        files = [f for f in d.rglob("*")
                 if f.is_file() and f.suffix.lower() in modal_volume.MODEL_EXTS]
        if files:
            types_with_files[d.name] = files
    if not types_with_files:
        print("✓ 本地没有可同步的模型")
        return

    have = modal_volume.volume_files_by_type(cfg, types_with_files.keys())

    items, in_progress = [], []
    for type_, files in types_with_files.items():
        existing = have.get(type_, set())
        for f in files:
            if f.name in existing:
                continue
            if modal_volume.file_in_progress(f):
                in_progress.append(f"{type_}/{f.name}")
                continue
            items.append({"type": type_, "filename": f.name, "local_path": str(f),
                          "size_mb": f.stat().st_size // 1024 // 1024})

    if in_progress:
        print(f"\n⏳ 跳过 {len(in_progress)} 个还在下载中的文件(等下完再跑本脚本):")
        for n in in_progress:
            print(f"   {n}")

    if not items:
        print("\n✓ 本地模型都已在 Volume(或剩下的还在下载),无需同步")
        return

    total = sum(x["size_mb"] for x in items)
    print(f"\n待同步 {len(items)} 个文件,共 ~{total} MB:")
    for it in items:
        print(f"  {it['type']}/{it['filename']}  ({it['size_mb']} MB)")

    if dry:
        print("\n(--dry-run,未上传)")
        return

    print("\n开始上传(块级去重,相同内容秒过;新内容走你的上行带宽,耐心等)...")

    def _prog(ev):
        if ev["phase"] == "start":
            pct = int(ev["done_mb"] * 100 / ev["total_mb"]) if ev["total_mb"] else 0
            speed = f"{ev['rate_mbps']} MB/s, ETA {ev['eta']}" if ev["rate_mbps"] else "测速中…"
            print(f"  ↑ [{ev['idx']+1}/{ev['total']}] {ev['name']} ({ev['size_mb']} MB) "
                  f"— {ev['done_mb']}/{ev['total_mb']} MB ({pct}%), {speed}")
        else:
            print(f"    ✓ {ev['name']} {ev['size_mb']}MB / {ev['secs']}s ({ev['rate_mbps']} MB/s)")

    result = modal_volume.upload_models(cfg, items, on_progress=_prog)
    print(f"\n✓ 同步完成:{len(result['uploaded'])} 个,共 ~{result['total_mb']} MB。"
          f"这些模型现在 Volume 里有了,工作流用到时直接命中。")


if __name__ == "__main__":
    main()
