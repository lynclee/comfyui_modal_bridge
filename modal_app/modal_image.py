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

# _local_nodes_data.py:走 Volume 通道的自写节点的 pip 依赖(部署期由 node_sync 生成)。
# 同样是 .gitignore 的本地状态,处理方式与上面一致 —— 缺则自建空清单。
# ⚠ 这些依赖**必须**在 build 期装:worker 启动时 pip install 是 ComfyUI Registry
# 明令禁止的模式(「Runtime package installation through subprocess calls is not
# permitted」),而且那样每个冷容器都要重付一次安装时间。
_LOCAL_REQS_FILE = _HERE / "_local_nodes_data.py"
if not _LOCAL_REQS_FILE.exists():
    _LOCAL_REQS_FILE.write_text("LOCAL_NODE_REQS = []\n", encoding="utf-8")
try:
    from _local_nodes_data import LOCAL_NODE_REQS
except Exception:
    LOCAL_NODE_REQS = []

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

# basicsr 兼容性 shim(2026-09-02)。**与 PEP 667 那条无关的第二个坑**:
# basicsr/data/degradations.py 从 torchvision.transforms.functional_tensor 取
# rgb_to_grayscale,而 torchvision 0.17 把这个模块删了 —— 函数本体挪到了公开的
# transforms.functional,签名不变。实测(2026-09-02,torchvision 0.29):换完
# basicsr / data.degradations / archs.rrdbnet_arch / utils.download_util 四个子模块
# 全部 import 通过,且 rgb_to_grayscale 与旧实现(ITU-R 601-2 亮度公式)在张量上
# 最大绝对误差 0.0。
#
# ⚠ 与上面 sage 那层刻意采用**相反**的失败策略:sage 是本镜像必装、打不上就意味着
# 静默数值损坏,所以 fail-closed;basicsr 是**用户依赖**,绝大多数用户根本没装,
# 找不到就跳过才是对的 —— 无条件 fail-closed 会让没装 basicsr 的人直接部署不了。
# 只有"确实找到了、也确实改了"这条路径才校验改对没有。三条路径都幂等。
#
# MB_BASICSR_DEGRADATIONS 是留给测试注入路径的口子(测试真跑这段 shell,而不是
# 检查源码字符串);构建时不会设,走 find_spec 那条。
_BASICSR_SHIM_CMD = (
    # ⚠ **必须是一条单行命令**:Modal 把每条 run_commands 原样变成 Dockerfile 的一行
    #    `RUN`,多行 shell 会让 Dockerfile 解析器直接报 "expected any_breakable"
    #    (2026-09-02 踩到,第一版写成 if/elif/else 多行,镜像构建当场失败)。
    #    上面 sage 那条写成超长单行也是同一个原因。test_core 有一条测试盯着。
    r"""D="${MB_BASICSR_DEGRADATIONS:-}"; """
    r"""[ -n "$D" ] || D=$(python -c "import importlib.util as u,os;s=u.find_spec('basicsr');"""
    r"""print(os.path.join(os.path.dirname(s.origin),'data','degradations.py') if s and s.origin else '')" 2>/dev/null); """
    # ① 没装 / 找不到 → 跳过(**不能 fail**:basicsr 是用户依赖,多数用户根本没装)
    r"""if [ -z "$D" ] || [ ! -f "$D" ]; then echo '[bridge] basicsr 未安装 - 跳过 shim'; """
    # ② 已修或写法已变 → 跳过(幂等)
    r"""elif ! grep -q 'transforms\.functional_tensor import rgb_to_grayscale' "$D"; then """
    r"""echo '[bridge] basicsr 无需 shim(已修或写法已变)- 跳过'; """
    # ③ 确实要改 → 改完才校验
    r"""else sed -i 's/from torchvision\.transforms\.functional_tensor import rgb_to_grayscale/"""
    r"""from torchvision.transforms.functional import rgb_to_grayscale  """
    r"""# bridge patch: 旧模块在 torchvision>=0.17 已移除/' "$D"; """
    r"""if ! grep -q 'from torchvision\.transforms\.functional import rgb_to_grayscale' "$D"; then """
    r"""echo '[bridge] basicsr shim 改写失败'; exit 1; fi; """
    # 残留检查故意用最宽的词。⚠ 所以上面 sed 追加的注释**不能包含该词**,
    # 否则会匹配到补丁自己写下的注释、把成功判成失败(第一版就这么炸的)。
    r"""if grep -q 'functional_tensor' "$D"; then echo '[bridge] basicsr shim 有残留'; exit 1; fi; """
    r"""if ! python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$D"; then """
    r"""echo '[bridge] basicsr shim 改完不是合法 Python'; exit 1; fi; """
    r"""echo '[bridge] basicsr functional_tensor shim applied'; fi"""
)

# ⚠ cuda_image 不止 modal_app.py 用 —— snapshot_bench.py / node_compat_check.py 两个旁路
# app 也 import 它。往里加的东西(编译工具链、sageattention 等约 +2GB)三处一起承担;
# 将来要回退 sage 时,删这里一处即全部生效,不用去动那两个文件。
cuda_image = (
    modal.Image.from_registry(
        "nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04",
        # ⚠ 钉 3.12 而不是更新的 3.13,不是"求稳" —— **3.13 会让一整类老包装不上**。
        # Python 3.13 实装了 PEP 667(Consistent views of namespaces):函数作用域的
        # locals() 改为返回**独立快照**,exec() 往里写的东西在后续 locals() 里看不到。
        # 而"用 exec() 执行 version.py、再从 locals() 取 __version__"是 2020 年前
        # setup.py 里非常常见的写法:
        #     def get_version():
        #         with open(version_file) as f: exec(compile(f.read(), version_file, 'exec'))
        #         return locals()['__version__']          # 3.13 上 KeyError
        #
        # 2026-09-02 Modal 上 3.12/3.13 同条命令对照实测:
        #     3.13.3  → pip install basicsr==1.4.2 失败,栈底 KeyError: '__version__'
        #     3.12.10 → 成功建出 wheel
        # 受影响的远不止 basicsr:facexlib / gfpgan / realesrgan 的 setup.py 是**逐字节
        # 相同**的模板(都在第 69-72 行),而这几个正是人脸修复 / 放大类节点的标配依赖。
        #
        # ⚠ 钉 setuptools<81 救不了这条:失败发生在 pip 的隔离构建环境
        # (/tmp/pip-build-env-*/overlay),那里用的是临时装的最新 setuptools,
        # 镜像里这份够不着 —— 与"钉版本不拖累构建"是同一枚硬币的两面。
        #
        # ⚠ 修掉的是**构建期**这一类,不等于"老包全好了"。basicsr 另有一个独立的坑:
        # basicsr/data/degradations.py 从 torchvision.transforms.functional_tensor 取
        # rgb_to_grayscale,而 torchvision 0.17 删了这个模块 → 3.12 上装得上、import
        # 时才抛 ModuleNotFoundError(实测 torchvision 0.29 确无该模块)。净效果仍是
        # 改善:此前整个镜像构建就挂、根本部署不了;现在只有那一个包 import 失败,
        # 且 import_failure_hint() 会把真实 ImportError 原样报给用户。facexlib 不碰
        # functional_tensor,3.12 下干净;gfpgan / realesrgan 依赖 basicsr,会跟着中招。
        #
        # 用户的诉求是"本地能跑,推上云端就能跑",而本地是历史累积的环境、云端是从零按
        # requirements.txt 全新安装,差异天然存在;对齐 Python 版本能消掉其中最大的一块。
        # 后续可考虑像 MODAL_BRIDGE_COMFYUI_TAG 那样**跟随本机**版本,而不是写死。
        #
        # ⚠⚠ **改这一行必须同时换下面 SageAttention wheel 的 ABI tag**(cp313 ↔ cp312):
        #     那个 wheel 带 C 扩展,ABI 锁死在某个 CPython,错版本 pip 直接判 not supported、
        #     镜像构建失败。两者是成对的,test_core 里有一条测试盯着不让它们走散。
        add_python="3.12",
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
    #     同一份源码 / 同一套补丁,cp312 与 cp313 两个 ABI 的 wheel 都挂在那个 Release 下,
    #     换 add_python 时连着换 URL 即可(cp313 那份留作回滚)。
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
        "sage-2.2.0-d1a57a5-multiarch/sageattention-2.2.0-cp312-cp312-linux_x86_64.whl"
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
        # ⚠ 钉住提供 pkg_resources 的 setuptools。**setuptools 84.0.0 把 pkg_resources
        # 整个移除了**(实测:80.9.0 的 wheel 里 19 个文件、84.0.0 里 0 个),而
        # `import pkg_resources` 是 2023 年前一大批 custom_node 的标配写法。
        # 云端一旦装到 ≥84,这些节点会**整包 IMPORT FAILED**,而用户在前端只看到
        # 「Node 'X' not found / missing_node_type」—— 完全无从推断是依赖缺失,
        # 只会以为节点没同步上去、反复去点同步(2026-08-31 实测,撞上的是 art-venture)。
        # 本地 venv 自带旧 setuptools 所以从不暴露,上云才炸 —— 和 basicsr 那次同构:
        # 本地"缺了也能跑",云端是全有或全无。
        #
        # 为什么不怕拖累构建:pip 默认开 build isolation,构建别的包时用的是隔离环境里
        # 临时装的新 setuptools,和镜像里这份互不相干。这里钉的只是**运行时**那份。
        "setuptools<81",
    )
    # 本地自写节点(Volume 通道)的依赖。清单空时 pip_install() 不生成任何层,
    # 所以没有自写节点的用户完全不受影响。
    # 代码仍走 Volume(改一行免重 build);只有依赖变了才会动这一层。
    .pip_install(*LOCAL_NODE_REQS)
    # SageAttention 两个上游缺陷的 build 期补丁(2026-08-28)—— 上游 main 至今都未修。
    #
    # ① int32 指针溢出(**已致 H3 尾几帧塌坏**):triton/quant_per_thread.py 的量化 kernel
    #    用 int32 算行偏移 offs_n * stride_in。H3 的 fused QKV 布局下 Q 的 seq-stride=21504,
    #    行号 > 2^31/21504 ≈ 99865 时地址 wrap 成负 → 读垃圾(尾帧塌灰噪)或 illegal memory
    #    access(偶发 crash,极易被误判成"宿主机瞬态")。K 因 k-km 物化连续(stride=128)幸免。
    #    风险窗口极窄:0.9MP/15s 实测 L=90720,离 99865 只剩 10%。
    #    ⚠ attention kernel 走 CUDA(.so)不代表安全:量化是它前面的独立步骤、走 Triton,
    #    而 sageattn_qk_int8_pv_fp8_cuda_sm90 的 qk_quant_gran 默认就是 "per_thread"、
    #    core.py 的 dispatch 也没覆盖它 —— H100 必然走到这条含 bug 的路径。
    #
    # ② V 以 strided 视图进 CUDA 扩展(**crash,尚未踩到但只差一个配置**):
    #    core.py 只在 kv_len % 128 != 0 时用 torch.cat 给 V 补 pad,而 cat 的副作用正是
    #    把 V 变成 contiguous。于是 **kv_len % 128 == 0 时不 pad,V 保持 strided 视图**
    #    直接进 per_channel_fp8 → _fused.transpose_pad_permute_cuda 按 contiguous 假设
    #    算地址 → 越界。当前 L=90720 除 128 余 96 侥幸躲过,换片长/分辨率就是抽签。
    #    修在 quant.py 一处即同时覆盖 sm89(core.py:808)和 sm90(core.py:982)两个调用点;
    #    .contiguous() 对已连续张量是零拷贝 no-op,pad 分支本就要 cat 拷贝一次,故无额外开销。
    #
    # 为什么改 .py 就够:Triton kernel 是运行时 JIT 的 Python 源码、CUDA 扩展的调用方也是
    # Python,两个补丁都不进 .so —— 不必重编译 wheel,也不必重发 Release。
    # 为什么放在这里而不是紧跟 .pip_install(sageattention):放这儿只重建尾部轻层,
    # 紧跟装包会击穿 ComfyUI requirements / custom_nodes clone 那几层重的(实测部署 42s)。
    # 两个补丁都幂等,四道 fail-closed 闸(脆弱写法清零 / int64 够数 / contiguous 恰好一处 /
    # 两文件仍是合法 Python),任一不满足即 build 失败 —— 杜绝"镜像跑起来了但补丁没打上"。
    # 定论与真机复现来自同机 comfyagent 会话(RunPod,2026-08-20);① 与其
    # scripts/patch_sage_int64.py 输出逐字节一致。
    .run_commands(
        r"""P=$(python -c "import sageattention,os;print(os.path.join(os.path.dirname(sageattention.__file__),'triton','quant_per_thread.py'))") && """
        r"""Q=$(python -c "import sageattention,os;print(os.path.join(os.path.dirname(sageattention.__file__),'quant.py'))") && """
        # ① int32 → int64
        r"""sed -i -E 's/offs_n([0-9]?)\[:, None\] \* stride_(in|on)/offs_n\1.to(tl.int64)[:, None] * stride_\2/g' "$P" && """
        r"""test "$(grep -c 'offs_n[0-9]*\[:, None\] \* stride_' "$P")" = 0 && """
        r"""test "$(grep -c 'to(tl.int64)' "$P")" -ge 4 && """
        # ② V 强制 contiguous(幂等：已打过就跳过 sed)
        r"""if ! grep -q 'v = v.contiguous()' "$Q"; then """
        r"""sed -i 's|^    _fused\.transpose_pad_permute_cuda(v, |    v = v.contiguous()  # bridge patch\n    _fused.transpose_pad_permute_cuda(v, |' "$Q"; fi && """
        r"""test "$(grep -c 'v = v.contiguous()' "$Q")" = 1 && """
        # ③ 两个文件都必须仍是合法 Python
        r"""python -c "import ast,sys;[ast.parse(open(f).read()) for f in sys.argv[1:]]" "$P" "$Q" && """
        r"""echo '[bridge] sage patches OK (int64 + v-contiguous)'"""
    )
    # 单独一层而不是并进上面那条:失败策略相反(那条 fail-closed、这条 skip-if-absent),
    # 混在一起读的人容易把两者的语义搞混。层本身很轻,不值得为省一层牺牲可读性。
    .run_commands(_BASICSR_SHIM_CMD)
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
                             "aigc_delivery", "_local_nodes_boot", "_local_nodes_data")
)
