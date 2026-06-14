"""
Modal Image DSL — 用 layer-cache-friendly 链式构建
改一行 pip 只重 build 那一层,不会全量重 build

镜像里装的 custom_nodes 清单见 _custom_nodes_data.py(由 ComfyUI 里的
「一键添加缺失节点」按钮自动维护)。改清单 → 重新 modal deploy → 只重 build
clone + 装依赖这两层。

模型不进镜像 — Volume 挂到 /comfy-volume/models/
"""
import os as _os
import modal
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_EXTRA_MODEL_PATHS_YAML = _HERE / "extra_model_paths.yaml"

# _custom_nodes_data.py 是本地状态(.gitignore,不入库;由 ComfyUI 的节点同步维护)。
# 全新安装 / 还没同步过节点时它可能不存在 → 自建空清单,避免下面的 import 和后面的
# add_local_python_source 因文件缺失而炸。内联实现(刻意不 import node_sync):本文件在
# Modal 容器运行时也会被重新 import,而容器里没有 node_sync(它不在 add_local_python_source 里)。
_DATA_FILE = _HERE / "_custom_nodes_data.py"
if not _DATA_FILE.exists():
    _DATA_FILE.write_text("CUSTOM_NODES = []\n", encoding="utf-8")
try:
    from _custom_nodes_data import CUSTOM_NODES
except Exception:
    CUSTOM_NODES = []


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

# ⚠ 空清单(全新安装 / 没同步过节点)时 join 出空串 → .run_commands("") 会生成空 RUN,
# Modal 直接拒绝("the 'RUN' Dockerfile command is not supported")。所以空时兜底成一个 no-op。
_INSTALL_REQS_CMD = " && ".join(
    f"if [ -f /comfyui/custom_nodes/{n['name']}/requirements.txt ]; then "
    f"pip install -r /comfyui/custom_nodes/{n['name']}/requirements.txt; "
    f"else echo 'no requirements for {n['name']}'; fi"
    for n in CUSTOM_NODES
) or "echo 'no custom_nodes — skip requirements'"

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
    # 把部署时的 MODAL_BRIDGE_* 配置烤进镜像环境 → 容器运行时能读到真实值。
    # ⚠ 关键:modal deploy 子进程的 env(node_sync.deploy_env 注入)只在"部署解析期"可见,
    # 不会自动进容器运行时。必须用 .env() 显式烤进镜像,否则容器里 os.environ 读不到 → 回退默认值。
    # 这些都在 modal_app.py 模块顶层(容器运行时也会重新 import)被读:
    #   - VERSION       → health.deployed_version(版本契约)
    #   - DEFAULT_GPU   → health.deployed_gpu(GPU 契约;漏烤会让非 H100 显卡永远上报 H100 → 前端死循环重部署)
    #   - APP_NAME      → health.app + warm-stats 的 Cls.from_name(自定义 app 名时必须对)
    #   - VOLUME/SECRET → 运行时 reload Volume / from_name(自定义名时必须对)
    .env({k: _os.environ[k] for k in (
        "MODAL_BRIDGE_VERSION", "MODAL_BRIDGE_DEFAULT_GPU", "MODAL_BRIDGE_CHEAP_GPU",
        "MODAL_BRIDGE_TOP_GPU", "MODAL_BRIDGE_APP_NAME",
        "MODAL_BRIDGE_VOLUME", "MODAL_BRIDGE_SECRET", "MODAL_BRIDGE_TIMEOUT",
        "MODAL_BRIDGE_SNAPSHOT", "MODAL_BRIDGE_VOLUME_THRESHOLD_MB",
    ) if _os.environ.get(k)})
    .add_local_file(str(_EXTRA_MODEL_PATHS_YAML), "/comfyui/extra_model_paths.yaml")
    .add_local_python_source("modal_image", "_comfy_ws", "_custom_nodes_data")
)
