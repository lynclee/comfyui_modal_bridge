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
        "worker_timeout_s": 1800,   # 视频慢,给 30 分钟
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
    """按类别估算显存需求(GB)= 权重×系数 + 固定开销。供显存预警对比所选显卡。"""
    p = profile(category)
    return model_gb * p["vram_base_factor"] + p["vram_overhead_gb"]


def worker_timeout_s(category: str) -> int:
    """某类别的运行时长上限(s)。"""
    return int(profile(category)["worker_timeout_s"])


def max_worker_timeout_s() -> int:
    """所有类别里最长的时长上限 —— 部署时用作 worker(Modal)超时上限,覆盖最慢类别。"""
    return max(int(p["worker_timeout_s"]) for p in PROFILES.values())
