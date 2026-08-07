"""
categories.py — 工作流「类别画像」(category profiles)。纯逻辑、可单测,是前后端共识的单一真源。

为什么要类别:不同类别的工作流在两件事上差异很大,需要区别对待:
  1) 显存:图像扩散显存≈权重×小系数;视频要在显存里堆很多帧的 latent 激活,
     远超权重本身 → 同样的权重大小,视频的真实显存需求高得多。
  2) 时长:视频比图像慢得多,worker 超时上限要给够。

设计:每个类别一条 profile(怎么识别 + 跑多久 + 显存怎么估)。
**加新类别(音频 / 3D / …)只在 PROFILES 里加一条,不改任何调用逻辑。**

注意「时长」是上限(ceiling),不是每任务实际时长。Modal worker 的 timeout 是部署期固定的,
高上限不拖慢快任务(按实际运行计费),所以 worker 超时统一取 max_worker_timeout_s()。
"""

# 类别画像。新增类别只在这里加一条。
#   match_class_types : 工作流里出现任一该 class_type 即归此类(image 是兜底,留空集)
#   worker_timeout_s  : 该类工作流允许的最长运行时间(s)。用来推 worker 超时上限 + 前端等待上限。
#   vram_base_factor  : 模型权重总大小(GB)× 该系数 = 显存估算的"权重部分"
#   vram_overhead_gb  : 额外固定开销(GB)。视频远大于图像(多帧 latent 激活/中间张量)。
PROFILES = {
    "video": {
        "match_class_types": {
            "SaveVideo", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG",
            "VHS_VideoCombine", "CreateVideo",
        },
        "worker_timeout_s": 1200,   # 20 分钟。原生画布 + 15s 实测:采样 743s、端到端 802s,
                                    # 而这个预算还要覆盖冷启和产物回传 —— 900s 曾在回传阶段被杀。
        "vram_base_factor": 1.3,
        "vram_overhead_gb": 8.0,    # 多帧激活的粗略额外开销;宁可偏高,目的是 OOM 前预警
    },
    "image": {                       # 兜底默认类别
        "match_class_types": set(),
        "worker_timeout_s": 900,
        "vram_base_factor": 1.15,
        "vram_overhead_gb": 0.0,
    },
}

DEFAULT_CATEGORY = "image"

# 识别顺序:非默认类优先匹配,default 兜底。
_ORDER = [k for k in PROFILES if k != DEFAULT_CATEGORY] + [DEFAULT_CATEGORY]


def classify(prompt: dict) -> str:
    """按工作流里出现的 class_type 判类别(命中任一类别的 match 集即归该类)。"""
    cts = {n.get("class_type") for n in (prompt or {}).values()
           if isinstance(n, dict) and n.get("class_type")}
    for cat in _ORDER:
        match = PROFILES[cat]["match_class_types"]
        if match and (cts & match):
            return cat
    return DEFAULT_CATEGORY


def profile(category: str) -> dict:
    """取某类别的 profile(未知类别回退默认)。"""
    return PROFILES.get(category, PROFILES[DEFAULT_CATEGORY])


def estimate_vram_gb(model_gb: float, category: str) -> float:
    """按类别估算显存需求(GB)= 权重×系数 + 固定开销。供显存预警对比所选显卡。
    视频类这是**兜底公式**(从工作流抠不出分辨率×帧数时用):它把 TE/VAE 也算成常驻,
    对 MiniMax H3 高估约 50%(估 60G vs 实测峰值 38-40G)。抠得出时走 estimate_vram_video_gb。"""
    p = profile(category)
    return model_gb * p["vram_base_factor"] + p["vram_overhead_gb"]


# ── 视频显存估算 v2:常驻权重 + 激活(∝ 像素×帧数)──
# 视频显存的真实结构是「主模型常驻 + 多帧激活」,不是「全部权重 × 系数」:
# TE 编码完 prompt 即被模型管理换出、VAE 到 decode 才上,采样期只有主扩散模型常驻。
# 激活与 token 数成正比,token 数 ∝ W×H×帧数(DiT 的 patch 化把空间/时间都线性切)。
# 锚点(MiniMax H3, H100/L40S 双卡实测一致):1280×736×362帧 = 0.341 G像素帧 →
# 激活 ≈18GB → 52.8 GB/G像素帧。余量 1.3 使三个实测锚点同时成立:
# 0.9MP@48G 放行(est 45.4)、1344×768@48G 放行(47.7)、2K@80G 报警(80.5,实测确实 offload)。
VIDEO_ACT_GB_PER_GPIXFRAME = 52.8
VIDEO_ACT_MARGIN = 1.3
VIDEO_FIXED_GB = 2.0  # CUDA context + 显存碎片


def estimate_vram_video_gb(largest_model_gb: float, pixels: float, frames: int) -> float:
    """视频显存估算(GB)= 最大单模型(常驻的主扩散模型)+ 激活项 + 固定开销。"""
    act = VIDEO_ACT_GB_PER_GPIXFRAME * (pixels * frames) / 1e9
    return largest_model_gb + act * VIDEO_ACT_MARGIN + VIDEO_FIXED_GB


_FRAME_KEYS = ("length", "num_frames", "frames", "video_frames", "frame_count")


def extract_pixels_frames(prompt: dict) -> tuple[float, int]:
    """从 API prompt 抠(像素数 W×H, 帧数)。抠不到返回 (0, 0) → 调用方回退兜底公式。
    只认**字面量**,不做节点求值:
      1) 首选某节点同时带 width/height/帧数字面量(H3 的 EmptyMiniMaxH3LatentAV 等),
         多个取像素×帧数最大者;
      2) W/H 经连线拿不到时,退而找图里的 megapixels 字面量(ResolutionSelector 类节点)——
         激活只关心 W×H 乘积,不关心宽高比,MP 值即乘积;帧数取全图最大帧字面量。"""
    best_px, best_f, best_prod = 0.0, 0, 0.0
    mp_px, max_f = 0.0, 0
    for n in (prompt or {}).values():
        if not isinstance(n, dict):
            continue
        ins = n.get("inputs") or {}
        f = 0
        for k in _FRAME_KEYS:
            v = ins.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 5:
                f = int(v)
                break
        if f:
            max_f = max(max_f, f)
            w, h = ins.get("width"), ins.get("height")
            if (isinstance(w, (int, float)) and isinstance(h, (int, float))
                    and w >= 64 and h >= 64 and w * h * f > best_prod):
                best_px, best_f, best_prod = float(w * h), f, w * h * f
        v = ins.get("megapixels")
        if isinstance(v, (int, float)) and 0.05 <= v <= 16:
            mp_px = max(mp_px, float(v) * 1e6)
    if best_prod:
        return best_px, best_f
    if mp_px and max_f:
        return mp_px, max_f
    return 0.0, 0


def worker_timeout_s(category: str) -> int:
    """某类别的运行时长上限(s)。"""
    return int(profile(category)["worker_timeout_s"])


def max_worker_timeout_s() -> int:
    """所有类别里最长的时长上限 —— 部署时用作 worker(Modal)超时上限,覆盖最慢类别。"""
    return max(int(p["worker_timeout_s"]) for p in PROFILES.values())
