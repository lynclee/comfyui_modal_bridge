"""
Modal Image DSL — 用 layer-cache-friendly 链式构建
改一行 pip 只重 build 那一层,不会全量重 build

镜像里装的 custom_nodes 清单见 _custom_nodes_data.py(由 ComfyUI 里的
「一键添加缺失节点」按钮自动维护)。改清单 → 重新 modal deploy → 只重 build
clone + 装依赖这两层。

模型不进镜像 — Volume 挂到 /comfy-volume/models/
"""
import os as _os
from shlex import quote as _q

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

# extra_model_paths.yaml 也是部署期生成的本地状态(.gitignore;由 node_sync.write_extra_model_paths
# 按本机模型目录类型生成)。缺则写标准基线,避免下面的 add_local_file 因文件不存在而炸。
if not _EXTRA_MODEL_PATHS_YAML.exists():
    _STD_TYPES = ["checkpoints", "diffusion_models", "unet", "vae", "clip", "text_encoders",
                  "clip_vision", "style_models", "loras", "controlnet", "upscale_models",
                  "embeddings", "hypernetworks", "photomaker", "gligen", "diffusers",
                  "vae_approx", "pulid", "inpaint", "insightface", "onnx", "sams", "ultralytics"]
    _yaml = ["comfyui-bridge:", "    base_path: /comfy-volume/", "    is_default: true", ""]
    _yaml += [f"    {_t}: models/{_t}/" for _t in _STD_TYPES]
    _EXTRA_MODEL_PATHS_YAML.write_text("\n".join(_yaml) + "\n", encoding="utf-8")


def _clone_one(n: dict) -> str:
    """生成单个 custom_node 的 clone(+ 可选 checkout)命令。
    ⚠ url / name / commit 都插值进 shell,必须 quote:节点文件夹名含空格(Windows 用户常见)
    或 url 带 shell 元字符会让整个镜像 build 崩,报错还很难和"哪个节点"对上号。"""
    url = (n.get("url") or "").strip()
    name = (n.get("name") or "").strip()
    if not url or not name:
        # 第二道闸(第一道在 node_sync.write_baked_nodes):空 url 会生成
        # `git clone '' /comfyui/custom_nodes/…`,让整个镜像 build 崩在一个
        # 与真实原因完全对不上的报错里。返回空串交调用方过滤 —— 刻意不抛异常:
        # 本模块在**容器运行时**也会被 import,抛了会让 worker 直接起不来,
        # 把一个"少装一个节点"的问题升级成"整个 worker 挂掉"。
        print(f"[bridge] ⚠ 跳过无效 custom_node 条目(url/name 为空): {n!r}")
        return ""
    path = _q(f"/comfyui/custom_nodes/{name}")
    base = f"git clone {_q(url)} {path}"
    commit = (n.get("commit") or "").strip()
    if commit:
        return f"{base} && cd {path} && git checkout {_q(commit)}"
    return base


_CLONE_CMD = " && ".join([
    "mkdir -p /comfyui/custom_nodes",
    *[c for c in (_clone_one(n) for n in CUSTOM_NODES) if c],
])


def _install_reqs_one(n: dict) -> str:
    req = _q(f"/comfyui/custom_nodes/{n['name']}/requirements.txt")
    return (f"if [ -f {req} ]; then pip install -r {req}; "
            f"else echo {_q('no requirements for ' + n['name'])}; fi")


# ⚠ 空清单(全新安装 / 没同步过节点)时 join 出空串 → .run_commands("") 会生成空 RUN,
# Modal 直接拒绝("the 'RUN' Dockerfile command is not supported")。所以空时兜底成一个 no-op。
_INSTALL_REQS_CMD = " && ".join(
    _install_reqs_one(n) for n in CUSTOM_NODES
) or "echo 'no custom_nodes — skip requirements'"

# ⚠ cuda_image 不止 modal_app.py 用 —— snapshot_bench.py / node_compat_check.py 两个旁路
# app 也 import 它。往里加的东西(编译工具链、sageattention 等约 +2GB)三处一起承担;
# 将来要回退 sage 时,删这里一处即全部生效,不用去动那两个文件。
cuda_image = (
    modal.Image.from_registry(
        "nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04",
        add_python="3.13",
    )
    .apt_install("git", "wget", "libgl1", "libglib2.0-0", "libsm6", "libxext6", "libxrender1", "ffmpeg")
    .run_commands(
        # ComfyUI 版本跟随本机:部署时 node_sync 把本机版本解析成 tag 注入 MODAL_BRIDGE_COMFYUI_TAG。
        # 没注入(老流程 / 直接 modal deploy)则兜底 v0.22.0。改 tag → 重 build 这层及之后。
        f"git clone --depth=1 --branch {_os.environ.get('MODAL_BRIDGE_COMFYUI_TAG', 'v0.22.0')} "
        f"https://github.com/comfyanonymous/ComfyUI /comfyui"
    )
    # torch 版本与本机无关(本机是 MPS 构建),这里写死跟着镜像的 CUDA 13.0 走。
    # 改版本号 → 这一层及之后(ComfyUI requirements / custom_nodes)全部重 build。
    .pip_install(
        "torch==2.13.*", "torchvision", "torchaudio",
        extra_index_url="https://download.pytorch.org/whl/cu130",
    )
    # SageAttention:量化 attention kernel(QK 走 INT8,PV 保 FP16+FP16 累加器),用来替换
    # ComfyUI 默认的 PyTorch SDPA。开关在运行时(MODAL_BRIDGE_SAGE_ATTENTION),包这里总是装,
    # 这样切换 A/B 只重建最后的 .env() 层,不用重新编译 kernel。
    # 位置刻意放在 torch 之后、ComfyUI requirements 之前:后面那些会变的层(custom_nodes
    # 增删、ComfyUI tag 跟随本机升级)就不会触发这一层重建。
    # build-essential 是硬需求且与 sage 无关:torch 2.13 起 H3 走 Triton 路径(2.11 不走),
    # 而 Triton 的 NVIDIA driver **初始化就要现场编译 C stub** —— runtime 基础镜像没 gcc,
    # 缺了会在运行期抛 "RuntimeError: Failed to find C compiler"(构建期全绿,运行期才炸)。
    # 注意它跟 nvcc 两码事:Triton 自带 LLVM 直接出 PTX,不走 nvcc。
    .apt_install("build-essential")
    # SageAttention 装预编译 wheel(本仓库 Release 自托管),不再现场编译:
    #   - 全网没有可用的 Linux 二进制(2026-08-06 复查):官方 thu-ml Releases 零资产、PyPI 只到
    #     1.0.6(纯 Triton)、woct0rdho 全 win_amd64;有 Linux wheel 的社区仓全是 cp312/torch≤2.12,
    #     唯一 cp313+cu13 的(snw35)从坏的 v2.2.0 tag 编 —— 只能自己发。
    #   - ⚠ 不能用上游 v2.2.0 tag:它带 PR #218 引入的 sm90 wrapper bug(custom op 写 output
    #     没声明 mutates_args → torch 当纯函数把写入丢弃 → kernel "成功"返回垃圾,H100 上
    #     输出全花且无异常无回退)。上游 issue #288/#320,2025-12-22 起 main 已修,
    #     实测 H100: 48.6 → 24.6 s/it(−49%),画质正常。
    #   - multiarch 版含真·双架构:_qattn_sm89=sm_89(L40S/L20/4090)、_qattn_sm90=sm_90a(H100)、
    #     _fused=双架构。上游 setup.py 所有扩展共享 NVCC_FLAGS(thu-ml#360),单一 ARCH_LIST 会把
    #     每个 .so 编成同一架构 —— 初版 wheel 的 _qattn_sm89 里全是 sm_90a 代码,L40S 上
    #     CUDA illegal access(2026-08-06 实测)。构建时需给 setup.py 打「每扩展各自 gencode」补丁,
    #     配方+双卡冒烟数据见 Release 页;B200(sm100)无 dispatch 分支,Python 异常被 ComfyUI
    #     兜住回退 SDPA(attention.py:578)。是否传 --use-sage-attention 由 _worker_boot 按
    #     compute_cap 门控(见 modal_app.py)。
    .pip_install(
        "sageattention @ https://github.com/lynclee/comfyui_modal_bridge/releases/download/"
        "sage-2.2.0-d1a57a5-multiarch/sageattention-2.2.0-cp313-cp313-linux_x86_64.whl"
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
    # SageAttention int32 指针溢出补丁(2026-08-28)—— 上游 main 至今未修,必须打。
    # 根因:triton/quant_per_thread.py 的量化 kernel 用 int32 算行偏移 offs_n * stride_in。
    # H3 的 fused QKV 布局下 Q 的 seq-stride=21504,行号 > 2^31/21504 ≈ 99865 时地址 wrap
    # 成负数 → 读垃圾(**尾几帧塌灰噪**)或 illegal memory access(偶发 crash,极易被误判成
    # "宿主机瞬态")。K 因 k-km 物化连续(stride=128)幸免;contiguous 布局永远安全。
    # 定论与真机复现来自 comfyagent 会话(RunPod,2026-08-20),补丁等价于其
    # scripts/patch_sage_int64.py,本地已逐字节比对一致。
    #
    # ⚠ attention kernel 走 CUDA(.so)不代表安全:量化是它前面的独立步骤,走 Triton。
    # 而 sageattn_qk_int8_pv_fp8_cuda_sm90 的 qk_quant_gran 默认就是 "per_thread"、
    # core.py 的 dispatch 也没覆盖它 —— H100 必然走到这条含 bug 的路径。
    # 当前 0.9MP/15s 实测 L=90720,离 99865 只剩 10%:再长 10% 的片子就翻车。
    #
    # 为什么改 .py 就够:Triton kernel 是运行时 JIT 的 Python 源码,不进 .so,
    # 所以不必重编译 wheel,也不必重发 Release。
    # 为什么放在这里而不是紧跟 .pip_install(sageattention):放这儿只重建尾部轻层,
    # 紧跟装包会击穿 ComfyUI requirements / custom_nodes clone 那几层重的。
    # 补丁本身幂等(二次执行不产生双重 .to(tl.int64)),失败即 build 失败(fail-closed),
    # 不会出现"镜像跑起来了但补丁没打上"的静默状态。
    # 遗留:上游还有一个独立 bug —— L%128==0 时 V 不 pad、strided 直进 per_channel_fp8
    # 会 crash。本补丁不涉及,换配置时注意 L 别整除 128。
    .run_commands(
        r"""P=$(python -c "import sageattention,os;print(os.path.join(os.path.dirname(sageattention.__file__),'triton','quant_per_thread.py'))") && """
        r"""sed -i -E 's/offs_n([0-9]?)\[:, None\] \* stride_(in|on)/offs_n\1.to(tl.int64)[:, None] * stride_\2/g' "$P" && """
        r"""test "$(grep -c 'offs_n[0-9]*\[:, None\] \* stride_' "$P")" = 0 && """
        r"""test "$(grep -c 'to(tl.int64)' "$P")" -ge 4 && """
        r"""python -c "import ast,os,sageattention;ast.parse(open(os.path.join(os.path.dirname(sageattention.__file__),'triton','quant_per_thread.py')).read())" && """
        r"""echo '[bridge] sage int64 patch OK'"""
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
        "MODAL_BRIDGE_VERSION", "MODAL_BRIDGE_COMFYUI_TAG",
        "MODAL_BRIDGE_DEFAULT_GPU", "MODAL_BRIDGE_CHEAP_GPU",
        "MODAL_BRIDGE_TOP_GPU", "MODAL_BRIDGE_APP_NAME",
        "MODAL_BRIDGE_VOLUME", "MODAL_BRIDGE_SECRET", "MODAL_BRIDGE_TIMEOUT",
        "MODAL_BRIDGE_SNAPSHOT", "MODAL_BRIDGE_VOLUME_THRESHOLD_MB",
        # 运行时读:_worker_boot 据此决定要不要给 ComfyUI 加 --disable-dynamic-vram。
        # ⚠ 只在 node_sync 里设不够 —— 那只进部署子进程,不进容器;漏在这里 = 开关静默失效。
        "MODAL_BRIDGE_DISABLE_DYNAMIC_VRAM",
        # 同上:_worker_boot 据此决定要不要加 --use-sage-attention。
        "MODAL_BRIDGE_SAGE_ATTENTION",
    ) if _os.environ.get(k)})
    .add_local_file(str(_EXTRA_MODEL_PATHS_YAML), "/comfyui/extra_model_paths.yaml")
    .add_local_python_source("modal_image", "_comfy_ws", "_custom_nodes_data", "comfy_log",
                             "aigc_delivery", "_local_nodes_boot")
)
