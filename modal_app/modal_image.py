"""
Modal Image DSL — 用 layer-cache-friendly 链式构建
改一行 pip 只重 build 那一层,不会全量重 build

镜像里装的 custom_nodes 清单见 _custom_nodes_data.py(由 ComfyUI 里的
「一键添加缺失节点」按钮自动维护)。改清单 → 重新 modal deploy → 只重 build
clone + 装依赖这两层。

模型不进镜像 — Volume 挂到 /comfy-volume/models/
"""
import modal
from pathlib import Path

from _custom_nodes_data import CUSTOM_NODES

_HERE = Path(__file__).resolve().parent
_EXTRA_MODEL_PATHS_YAML = _HERE / "extra_model_paths.yaml"


def _clone_one(n: dict) -> str:
    """生成单个 custom_node 的 clone(+ 可选 checkout)命令。"""
    base = f"git clone {n['url']} /comfyui/custom_nodes/{n['name']}"
    commit = (n.get("commit") or "").strip()
    if commit:
        return f"{base} && cd /comfyui/custom_nodes/{n['name']} && git checkout {commit}"
    return base


_CLONE_CMD = " && ".join([
    "mkdir -p /comfyui/custom_nodes",
    *[_clone_one(n) for n in CUSTOM_NODES],
])

_INSTALL_REQS_CMD = " && ".join(
    f"if [ -f /comfyui/custom_nodes/{n['name']}/requirements.txt ]; then "
    f"pip install -r /comfyui/custom_nodes/{n['name']}/requirements.txt; "
    f"else echo 'no requirements for {n['name']}'; fi"
    for n in CUSTOM_NODES
)

cuda_image = (
    modal.Image.from_registry(
        "nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04",
        add_python="3.13",
    )
    .apt_install("git", "wget", "libgl1", "libglib2.0-0", "libsm6", "libxext6", "libxrender1", "ffmpeg")
    .run_commands(
        "git clone --depth=1 --branch v0.22.0 https://github.com/comfyanonymous/ComfyUI /comfyui"
    )
    .pip_install(
        "torch==2.11.*", "torchvision", "torchaudio",
        extra_index_url="https://download.pytorch.org/whl/cu130",
    )
    .run_commands("cd /comfyui && pip install -r requirements.txt")
    .run_commands(_CLONE_CMD)
    .run_commands(_INSTALL_REQS_CMD)
    # worker 自身需要的小包。模型不在容器里下载(本地 SDK 传 Volume),所以不再装
    # huggingface_hub / hf_xet。这层经常改,放最后让 cache 命中率最高。
    .pip_install(
        "websocket-client",
        "requests",
        "fastapi[standard]",
        "pyyaml",
    )
    .run_commands("mkdir -p /comfy-volume")
    .add_local_file(str(_EXTRA_MODEL_PATHS_YAML), "/comfyui/extra_model_paths.yaml")
    .add_local_python_source("modal_image", "_comfy_ws", "_custom_nodes_data")
)
