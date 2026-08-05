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
    # ⚠ 必须从 GitHub 源码装:PyPI 上 sageattention 只发到 1.0.6(纯 Triton),2.x 从未上传,
    #   README 里那句 `pip install sageattention==2.2.0` 是失效的。2.2.0 只有 GitHub tag。
    #   ComfyUI 侧只 `from sageattention import sageattn`(见 comfy/ldm/modules/attention.py),
    #   两个版本 API 都满足;选 2.2.0 是为它的 per-thread INT4 量化和更彻底的 outlier smoothing。
    # ⚠ 三个 apt 包缺一不可,而且构建期全绿也不代表能跑:
    #   - build-essential:Triton 的 NVIDIA driver **初始化就要现场编译 C stub**,runtime
    #     基础镜像没 gcc → 运行期抛 "RuntimeError: Failed to find C compiler"。
    #     torch 2.13 起 H3 自己也会走 Triton 路径(2.11 时不走),所以这个包跟开不开 sage 无关,
    #     是硬需求。注意它跟 nvcc 两码事:Triton 自带 LLVM 直接出 PTX,不走 nvcc。
    #   - cuda-nvcc / cuda-cudart-dev:2.x 的 CUDA kernel 要现场编译,这才是 nvcc 的用途。
    #   - cuda-libraries-dev:PyTorch 的 ATen/cuda/CUDAContextLight.h 直接 include
    #     cusparse.h / cublas_v2.h / cusolverDn.h,而 cudart-dev 只带 Runtime 的头 →
    #     "fatal error: cusparse.h: No such file or directory"。这个 meta 包一次装齐
    #     cuBLAS/cuSPARSE/cuSOLVER/cuRAND/cuFFT 的 dev,比整套 cuda-toolkit 小
    #     (不含 profiler/samples)。编译任何 torch CUDA 扩展都会撞这个,不只 SageAttention。
    # ⚠ TORCH_CUDA_ARCH_LIST 只能填 9.0,别加 8.9。SageAttention 2.x 的核心 kernel
    #   (qk_int_sv_f8_cuda_sm90)用了 Hopper 独有指令 —— wgmma(warpgroup MMA)、
    #   mbarrier.arrive.expect_tx / cp.async.bulk.tensor(TMA 异步拷贝)。Ada(sm_89)硬件上
    #   没有这些单元,把它编到 compute_89 会被 ptxas 拒:
    #     "Instruction 'wgmma.mma_async with FP8 types' not supported on .target 'sm_89'"
    #   注意机制:填 9.0 后 _qattn_sm80/_qattn_sm89 这些模块**仍会编译并打进 wheel**
    #   (实测链接日志可见),只是里面装的全是 sm_90 的 SASS。所以 L40S/B200 上的失败点
    #   在**运行期**而非构建期:模块加载成功 → kernel 启动报 "no kernel image is
    #   available for execution on the device" → ComfyUI 的 try/except 捕获后自动
    #   回退 pytorch attention(attention.py:577),不崩、不报致命错,只在日志留一行 error。
    #   B200(sm_100)先不编:Blackwell 用 tcgen05 取代 wgmma,2.2.0 能否编过未验证,
    #   而 primary 档是 H100,先把主力跑通。之后要加再试 "9.0;10.0"。
    #   用 pip_install 自带的 env= 而非 .env():只在这一层构建期可见,不落进容器运行时。
    # ⚠ 编译要几十分钟,且 SageAttention 官方支持矩阵最高只标到 CUDA 12.8 —— 本镜像是 13.0,
    #   编不过的话回退 `.pip_install("sageattention==1.0.6")`(纯 Triton,零编译,API 同样兼容)。
    # ⚠ 不能用 .apt_install:nvidia/cuda 基础镜像在 Dockerfile 里 apt-mark hold 了自带的
    #   CUDA 运行库(libcublas-13-0=13.0.0.19、libnccl2),而仓库里最新的 libcublas-dev-13-0
    #   要求 libcublas-13-0 >=13.1.1.3 → 求解器想升级被 hold 挡住,报
    #   "E: you have held broken packages"。
    #   ⚠ --allow-change-held-packages 治不了这个(实测):它只放行"显式操作"里的 held 包,
    #   依赖求解器自动决策时仍把 hold 当硬约束、不会主动升级 → 必须先 apt-mark unhold。
    #   系统 CUDA 库升个小版本对 torch 无感 —— pip 的 cu130 wheel 自带整套
    #   nvidia-*-cu13 库(在 site-packages 里),运行时根本不用系统的。
    .run_commands(
        "apt-mark unhold $(apt-mark showhold) 2>/dev/null || true; "
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "build-essential cuda-nvcc-13-0 cuda-cudart-dev-13-0 cuda-libraries-dev-13-0"
    )
    # ⚠ CC/CXX/-ccbin 三个都得显式指:Modal 的 add_python 装的 Python 是 **Clang 编译的**
    #   (启动日志 "Python version: 3.13.0 ... [Clang 18.1.8]"),sysconfig 里记着 clang,
    #   setuptools 照抄 → nvcc 去查 clang++ 版本 → 系统只有 g++ 没有 clang → 探测返回 0.0.0,
    #   报 "current installed version of clang++ (0.0.0) is less than ... CUDA 13.0 (7.0)"。
    #   build-essential 装的 g++-13 一直在,只是没人指给它用。
    .pip_install(
        "sageattention @ git+https://github.com/thu-ml/SageAttention.git@v2.2.0",
        extra_options="--no-build-isolation",
        env={
            "TORCH_CUDA_ARCH_LIST": "9.0",
            "CC": "gcc",
            "CXX": "g++",
            "NVCC_PREPEND_FLAGS": "-ccbin /usr/bin/g++",
            # sm90 模块的 TMA(cuTensorMapEncodeTiled)走 driver API,链接要 -lcuda。
            # builder 容器无 GPU 无驱动,真 libcuda.so 不存在 → 用 toolkit 的 driver stub
            # (cuda-driver-dev 装在 /usr/local/cuda/lib64/stubs)。只影响构建期链接;
            # 运行期 GPU 容器里由 NVIDIA runtime 挂真的 libcuda.so.1,stub 不参与。
            "LIBRARY_PATH": "/usr/local/cuda/lib64/stubs",
        },
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
                             "aigc_delivery")
)
