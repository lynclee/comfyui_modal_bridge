"""
node_compat_check.py — 隔离的「自定义节点兼容性检测」app。

**不碰生产 modal_app**(独立 app 名 comfyui-bridge-nodecheck),用同一个生产镜像 cuda_image
(节点、ComfyUI 版本都和生产一致)。在一张便宜 GPU(L40S)上 boot 一次 ComfyUI,捕获启动日志,
解析每个自定义节点导入成功 / 失败(失败 = 与当前 ComfyUI 版本不兼容 / 缺依赖 / commit 坏)。

用法(部署后由 routes._deploy 自动跑;也可手动):
    cd custom_nodes/comfyui_modal_bridge/modal_app
    python -m modal run node_compat_check.py

GPU(而非 CPU):有的节点在 import 时探测 CUDA,CPU 跑会误判失败 → 用便宜 GPU 保证和生产同环境。
"""
import os
import subprocess
import time

import modal

from modal_image import cuda_image
from comfy_log import parse_import_failures

VOLUME_NAME = os.environ.get("MODAL_BRIDGE_VOLUME", "comfyui-bridge-models")
CHECK_GPU = os.environ.get("MODAL_BRIDGE_CHEAP_GPU", "L40S")  # 用便宜档跑,import 检测和卡型无关

app = modal.App("comfyui-bridge-nodecheck")
models_vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


@app.function(image=cuda_image, gpu=CHECK_GPU, volumes={"/comfy-volume": models_vol},
              timeout=900, max_containers=1)
def check() -> dict:
    """Boot 一次 ComfyUI,读启动日志到"服务起来"为止,解析自定义节点导入结果。"""
    cmd = ["python", "/comfyui/main.py", "--listen", "127.0.0.1", "--port", "8188",
           "--extra-model-paths-config", "/comfyui/extra_model_paths.yaml"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    lines: list[str] = []
    deadline = time.time() + 300
    try:
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break          # ComfyUI 进程提前退出(可能硬崩),用已收日志解析
                continue
            print(line, end="")    # 同时进 Modal 日志,便于看 traceback
            lines.append(line)
            # 导入块("Import times for custom nodes")在服务启动之前打印完,见到这些就够了
            if "To see the GUI" in line or "Starting server" in line:
                break
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    res = parse_import_failures("".join(lines))
    res["comfyui_tag"] = os.environ.get("MODAL_BRIDGE_COMFYUI_TAG", "v0.22.0")
    return res


@app.local_entrypoint()
def main():
    r = check.remote()
    tag = r.get("comfyui_tag", "?")
    ok = r.get("ok", [])
    failed = r.get("failed", [])
    print(f"\n=== 自定义节点兼容性检测 (ComfyUI {tag}) ===")
    print(f"✅ 导入成功:{len(ok)}")
    for n in ok:
        print(f"   ✓ {n}")
    if failed:
        print(f"\n⚠ 导入失败:{len(failed)}(可能与 {tag} 不兼容 / 缺依赖 / commit 坏)")
        for f in failed:
            print(f"   ✗ {f['name']}: {f.get('error') or '(见上方日志 traceback)'}")
        print("\n提示:更新/修好这些节点(本地换版本或 commit)后重新部署即可;"
              "失败节点不影响其它工作流,部署不阻断。")
    else:
        print("全部导入成功 ✓")
