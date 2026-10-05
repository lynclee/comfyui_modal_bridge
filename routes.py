"""
routes.py — 本地 ComfyUI 服务器上的 HTTP 路由
所有路由前缀 /modal_bridge/...
"""
import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import mimetypes
import re
import secrets
import subprocess
import traceback
from pathlib import Path

import aiohttp
from aiohttp import web

from . import categories
from . import config as cfg_mod
from . import contract
from . import local_nodes
from . import modal_client
from . import modal_volume
from . import model_deps
from . import node_sync
from . import workflow_check
from .result_receipts import RECEIPT_MAX_AGE_S, ResultReceipts


# folder_paths 是 ComfyUI 全局模块
try:
    import folder_paths  # type: ignore
except Exception:
    folder_paths = None


# ComfyUI 里互为别名(同一池子)的模型目录:同一个文件可能放在任一目录。
# 历史命名:UNET 旧叫 unet、新叫 diffusion_models;CLIP 旧叫 clip、新叫 text_encoders。
# 不同机器(Mac/Win)、不同下载器默认目录不同,所以两个都得搜,否则会误报"本地没有"。
_TYPE_ALIASES = {
    "diffusion_models": ["unet"],
    "unet": ["diffusion_models"],
    "text_encoders": ["clip"],
    "clip": ["text_encoders"],
}


def _local_model_resolver():
    """返回 (type_, filename) -> Path|None,用 ComfyUI folder_paths 在本地定位模型文件。
    模型都在本地 ComfyUI Desktop 下好,这里把工作流里的文件名映射到磁盘路径,供上传 Volume。"""
    def resolve(type_: str, filename: str):
        search_types = [type_, *_TYPE_ALIASES.get(type_, [])]
        roots = []
        if folder_paths is not None:
            # 先收齐所有合法根。get_full_path 会规范化绝对路径/..，但会跟随根内 symlink；
            # 命中后仍必须 resolve 再确认没有借 symlink 跳到配置根之外。
            for t in search_types:
                try:
                    roots += folder_paths.get_folder_paths(t) or []
                except Exception:
                    pass
            # 1) ComfyUI 官方解析(认 extra_model_paths.yaml 的所有根,最权威);别名类型逐个试
            for t in search_types:
                try:
                    full = folder_paths.get_full_path(t, filename)
                    if full and modal_volume.is_path_within_roots(full, roots):
                        return Path(full)
                except Exception:
                    pass
        # 2) 兜底:默认 models/<type>(含别名目录)里找
        if not roots:
            base = Path(__file__).resolve().parents[2] / "models"
            roots = [str(base / t) for t in search_types]
        return modal_volume.find_local_model(type_, filename, roots)
    return resolve


# ── 从 workflow prompt 解析需要的模型 ──
# 纯解析(LOADER_MAP 命中 + 通用扩展名兜底)在 model_deps.py(可单测)。这里只补需要
# 文件系统的那一步:通用兜底拿到的文件名不知道 type,按本地命中位置反推 type。
def _resolve_model_anywhere(filename: str) -> str | None:
    """在本地所有模型 folder 类型里按文件名定位 → 返回命中的 type。
    供通用兜底(LOADER_MAP 外的 loader)反推模型属于哪个 models/<type>/。找不到返回 None。

    ⚠ filename 是工作流里的**相对路径**(如 flux/x.gguf),按原样交给 get_full_path(契约 C15,
    2026-10-05 深度 review)。以前先取 basename 再查:放在子目录里的模型永远查不到 →
    通用兜底漏掉它 → 不同步、不计入显存估算。get_full_path 自己会规范化并囚在各模型根内。"""
    if folder_paths is None:
        return None
    try:
        types_ = list(folder_paths.folder_names_and_paths.keys())
    except Exception:
        return None
    for t in types_:
        try:
            if folder_paths.get_full_path(t, filename):
                return t
        except Exception:
            pass
    return None


def extract_required_models(prompt: dict) -> list[dict]:
    """返回 [{type, filename}, ...] 去重。
    = LOADER_MAP 已知 type 的模型 + 通用兜底(扫到的模型文件名,按本地位置反推 type)。
    通用兜底只收"本地能定位到、因而能推出 type"的:本地都没有的模型反正传不上去,
    维持原行为(不强行入列),由云端验证阶段报缺。

    ⚠ 会对每个通用兜底文件名查一遍所有模型目录(外置盘 / 网络盘上不便宜):async 路由里
    一律经 asyncio.to_thread 调,别在事件循环里直接跑(深度 review)。"""
    loader_models = model_deps.extract_loader_models(prompt)
    out = list(loader_models)
    seen = {m["filename"] for m in loader_models}
    seen_base = {Path(f).name for f in seen}
    for fn in sorted(model_deps.extract_generic_filenames(prompt)):
        # 通用兜底现在保留相对路径(C15):与 LOADER_MAP 命中的同一个文件去重要按全路径比;
        # 只有裸文件名(老的形态)才退回按 basename 比,别把 a/x.gguf 和 b/x.gguf 当成同一个。
        if fn in seen or ("/" not in fn and "\\" not in fn and fn in seen_base):
            continue
        t = _resolve_model_anywhere(fn)
        if t:
            out.append({"type": t, "filename": fn})
            seen.add(fn)
            seen_base.add(Path(fn).name)
    return out


# 各 GPU 显存(GB)。用于"按工作流估算显存自动选便宜档"。与前端 GPU_VRAM 保持一致。
_GPU_VRAM_GB = {"L40S": 48, "A100-80GB": 80, "H100": 80, "H200": 141, "B200": 180, "A10G": 24, "L4": 24}
_CHEAP_MARGIN_GB = 6  # 余量:估算 + 激活波动,est_vram 要比便宜卡显存低这么多才敢降档(防 OOM)

# 云端各档排不到卡时的 fallback 链。**这是 modal_app/modal_app.py 里 _GPU_CHAIN 的副本**(云端不能
# import 本模块,本模块也不能 import 那个 `import modal` 的文件),由 test_fix_routes 用 ast 抽两边比对。
# 为什么要它(契约 C11,2026-10-05 深度 review):主档选 H200 时,云端排不到 H200 会落到 H100。
# 以前按 H200 的 141G 判「放得下」,估算 110G 的工作流不升档 → 落到 H100 就 OOM。
_GPU_FALLBACK_CHAIN = {
    "B200":      ["B200", "H200", "H100"],
    "H100":      ["H100", "A100-80GB"],
    "H200":      ["H200", "H100"],
    "A100-80GB": ["A100-80GB"],
    "L40S":      ["L40S"],
}


def _tier_capacity_gb(gpu: str, default: int) -> int:
    """某档实际能保证的显存 = fallback 链里显存最小的那张卡(链外的卡就按它自己)。"""
    chain = _GPU_FALLBACK_CHAIN.get(gpu, [gpu])
    return min(_GPU_VRAM_GB.get(g, default) for g in chain)


def _estimate_workflow_vram(prompt: dict, required: list | None = None) -> tuple[float, str, int]:
    """估工作流显存需求(GB)+ 类别 + 本地查不到大小的模型数。供自动选档 / 预警端点复用。
    视频类优先走激活公式(最大模型常驻 + W×H×帧数 激活项,H3 双卡实测校准),
    工作流里抠不出分辨率/帧数字面量时回退旧的「权重总和×系数」保守公式。
    required:调用方已经算过的 extract_required_models 结果(/submit 只算一次往下传)。
    同步、会碰文件系统,async 路由里经 asyncio.to_thread 调。"""
    resolver = _local_model_resolver()
    total_bytes, largest_bytes, unknown = 0, 0, 0
    if required is None:
        required = extract_required_models(prompt)
    for m in required:
        p = resolver(m["type"], m["filename"])
        try:
            if p and Path(p).exists():
                sz = Path(p).stat().st_size
                total_bytes += sz
                largest_bytes = max(largest_bytes, sz)
            else:
                unknown += 1
        except OSError:
            unknown += 1
    category = categories.classify(prompt)
    if category == "video" and largest_bytes:
        pixels, frames = categories.extract_pixels_frames(prompt)
        if pixels and frames:
            est = categories.estimate_vram_video_gb(largest_bytes / (1024 ** 3), pixels, frames)
            return est, category, unknown
    est = categories.estimate_vram_gb(total_bytes / (1024 ** 3), category)
    return est, category, unknown


_TIER_GPU_KEY = {"cheap": "cheap_gpu", "primary": "default_gpu", "top": "top_gpu"}


def _local_queue_busy() -> bool:
    """ComfyUI 本地是否有正在跑/排队的任务。拿不到就返回 False(不做推断)。

    为什么需要它:ComfyUI 是单进程 aiohttp + 同步执行图,KSampler 的 PyTorch 采样是
    同步阻塞调用,采样期间 event loop 基本调度不到。于是我们自己的 /version 里那个
    6 秒**挂钟**超时,在一个 3 s/it 的工作流上两个迭代就吃满了 —— 请求还没轮到处理
    就 TimeoutError。以前这会被前端当成「Modal 平台故障」,而云端其实完全正常
    (2026-08-31 实测:本地队列空闲后同一接口 1.4 s 返回、各项全匹配)。

    ⚠ 用户在本地忙的时候点 RunModal,恰恰是这个插件最该工作的场景(把活推到云端)。
    所以这个判定的目的不是"拦住他",而是把超时如实归因成 local_busy、别再拦。
    """
    try:
        from server import PromptServer  # type: ignore
        q = PromptServer.instance.prompt_queue
        running, pending = q.get_current_queue()
        return bool(running) or bool(pending)
    except Exception:
        return False


def resolve_gpu_tier(cfg: dict) -> str:
    """config → 生效的 GPU 档位。'auto' 表示按显存自动选,其余为固定档。
    新配置用 gpu_tier;为空则回落到旧的 auto_downgrade 语义(关=固定 primary)。"""
    tier = (cfg.get("gpu_tier") or "").strip().lower()
    if tier in ("auto", "cheap", "primary", "top"):
        return tier
    return "auto" if cfg.get("auto_downgrade", True) else "primary"


def _pick_gpu_class(prompt: dict, cfg: dict, required: list | None = None) -> tuple[str, str]:
    """按估算显存在 GPU 档梯子上选档,返回 (gpu_class, reason)。gpu_class ∈ {'cheap','primary','top'}。

    gpu_tier 固定某档时直接返回该档 —— 四档 worker 是一次部署全建好的,选哪档纯粹是
    运行时路由,**换档不必重新部署**(换某档具体是哪张卡才要)。
    gpu_tier=auto 时走梯子(成本低→高 L40S→H100→B200):
      1) 升档(防 OOM):估算 > 主卡容量 → top(B200 180G)。
      2) 降档(省钱):cheap≠主卡 + 非视频 + 大小已知 + 放得下便宜卡 → cheap(L40S)。
      3) 否则 → primary(H100)。
    本地查不到大小(unknown>0)时估算不可信:不升不降,留 primary(稳妥)。
    主档 / 省钱档的容量按各自 fallback 链里**最小**的卡算(C11):H200 档排不到会落到 H100。
    同步、会碰文件系统,async 路由里经 asyncio.to_thread 调。"""
    tier = resolve_gpu_tier(cfg)
    if tier != "auto":
        gpu_name = (cfg.get(_TIER_GPU_KEY[tier]) or "").strip() or "?"
        return tier, f"固定 {tier} 档({gpu_name})"
    cheap_gpu = (cfg.get("cheap_gpu") or "L40S").strip()
    primary_gpu = (cfg.get("default_gpu") or "H100").strip()
    top_gpu = (cfg.get("top_gpu") or "").strip()
    est, category, unknown = _estimate_workflow_vram(prompt, required)
    primary_vram = _tier_capacity_gb(primary_gpu, 80)
    primary_label = primary_gpu
    if len(_GPU_FALLBACK_CHAIN.get(primary_gpu, [primary_gpu])) > 1 \
            and primary_vram < _GPU_VRAM_GB.get(primary_gpu, primary_vram):
        primary_label = f"{primary_gpu} 档(排不到会落到 {primary_vram}G 的卡)"

    # 1) 升档:估算超过主卡「裸显存」才升(防 OOM)。⚠ 这里不减 margin ——
    #    est 已含系数余量(图像×1.15 / 视频×1.3+8),再减 margin 会双重保守:
    #    例 FLUX.2-dev est≈76G,实际在 H100/A100 80G 上跑得动,不该误升 H200。
    #    需有可信估算(unknown==0)。
    if (top_gpu and top_gpu != primary_gpu and unknown == 0
            and est > primary_vram):
        return "top", f"估算 {est:.1f}G > 主卡 {primary_label}({primary_vram}G) → 升档 {top_gpu}"

    # 2) 降档:省钱档放得下 → 便宜卡。
    if (cfg.get("auto_downgrade", True) and cheap_gpu != primary_gpu
            and category != "video" and unknown == 0):
        cap = _tier_capacity_gb(cheap_gpu, 48) - _CHEAP_MARGIN_GB
        if est <= cap:
            return "cheap", f"估算 {est:.1f}G ≤ {cap}G → 降档 {cheap_gpu}"

    # 3) 主卡兜底。
    if unknown:
        return "primary", f"{unknown} 个模型本地查不到大小,估算不可信 → 稳妥用 {primary_gpu}"
    if category == "video":
        return "primary", f"视频类 → {primary_gpu}"
    return "primary", f"估算 {est:.1f}G → {primary_gpu}"


def _input_dir() -> Path:
    if folder_paths:
        return Path(folder_paths.get_input_directory())
    return Path(__file__).resolve().parents[2] / "input"


def _output_dir() -> Path:
    if folder_paths:
        return Path(folder_paths.get_output_directory())
    return Path(__file__).resolve().parents[2] / "output"


# 取回进度(给 /fetch_result 那一次阻塞 POST 提供可观测性)。
# 2026-09-03 用户反馈:8K 全景图工作流"卡在 Downloading result"一小时。实际没卡 ——
# 大产物走 Volume 直连下载,而 modal 的 read_file_into_fileobj 是一次阻塞调用、没有进度
# 回调,前端那句文案又是**无条件**写死的「Decoding base64...」,于是几十分钟的下载被显示成
# 一句静态的、还说错了路径的提示。一小时静态文案与真卡住无法区分,用户只能猜。
# 这里靠采样 .part 文件大小报进度;分母来自 modal_volume.volume_file_size(拿不到就只报已下载量)。
_FETCH_PROGRESS: dict = {}
_FETCH_PROGRESS_MAX = 32
_FETCH_TASKS: dict = {}


def _fetch_progress_set(job_id: str, **kw) -> None:
    if job_id not in _FETCH_PROGRESS and len(_FETCH_PROGRESS) >= _FETCH_PROGRESS_MAX:
        for _old in list(_FETCH_PROGRESS)[: _FETCH_PROGRESS_MAX // 4]:  # dict 有序,清最早的
            _FETCH_PROGRESS.pop(_old, None)
    _FETCH_PROGRESS.setdefault(job_id, {}).update(kw)


async def _sample_part_size(job_id: str, part: Path, total: int, label: str,
                            interval: float = 0.5, window_s: float = 10.0):
    """每 0.5s 采一次 .part 大小,算出速率与停滞时长,写进 _FETCH_PROGRESS。被 cancel 即停。

    ⚠ 速率在这里算、不在前端算:这边采样间隔固定 0.5s,前端轮询会被标签页节流
    (后台 tab 的 setInterval 被压到 ≥1s、甚至暂停),用它的时间差算速率会跳得没法看。

    stalled_s 是这里最要紧的一个数 —— 用户问的其实不是"多快",是"到底卡没卡"。
    速度慢和真挂住在一句静态文案下完全一样;而"已 45s 没有任何增长"是个能直接回答
    那个问题的观测值(2026-09-03 用户反馈:download 很慢感觉也像卡死)。
    """
    # interval / window_s 是留给测试压缩时间的口子(真跑 0.5s / 10s;测试用 0.02s / 0.4s,
    # 否则一条测试要 5 秒)。生产调用不传这两个参数。
    window: list[tuple[float, int]] = []          # (时刻, 已下载) 滑动窗口
    last_grow = asyncio.get_running_loop().time()
    last_size = -1
    try:
        while True:
            now = asyncio.get_running_loop().time()
            try:
                done = part.stat().st_size
            except OSError:
                done = 0                          # 文件还没建 / 已 rename 成正式名
            if done > last_size:
                last_grow, last_size = now, done
            window.append((now, done))
            while len(window) > 1 and now - window[0][0] > window_s:
                window.pop(0)
            bps = 0
            if len(window) > 1:
                dt = window[-1][0] - window[0][0]
                db = window[-1][1] - window[0][1]
                if dt > 0 and db > 0:
                    bps = int(db / dt)
            _fetch_progress_set(
                job_id, stage="volume", label=label, done=done, total=total,
                bps=bps, stalled_s=int(now - last_grow),
                # 分母已知且在动才给 ETA;不给"∞"这种没用的显示
                # max(1, ...):不足 1 秒的 ETA 被 int() 截成 0,而 0 在前端表示"未知" ——
                # 于是"马上就好"会显示成"算不出来"(测试抓到)。
                eta_s=max(1, int((total - done) / bps)) if (bps and total and total > done) else 0,
            )
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise


async def _write_results(final: dict, job_id: str, subfolder: str, cfg: dict) -> list:
    """把 Modal 返回的产物写到 output/<subfolder>/<job_id>/,返回 outputs 列表。
    每个产物二选一:小文件 data_base64(解码落盘);大文件 volume_path(从 Volume 直连下载落盘)。
    否则回退单图 data_base64 / image_url。写失败 raise(由调用方转 502)。"""
    # 囚笼:job_id / subfolder 都参与拼路径,必须确认结果仍在 output/ 内。
    # filename 早就做了 basename 防逃逸,job_id 这条以前是漏的(它来自 HTTP body,
    # {"job_id": "../../x"} 就能写到 output 之外)。入口有正则,这里再兜一层:
    # 路由虽有 admin capability,路径边界仍须独立成立,不能把鉴权当囚笼。
    out_root = _output_dir().resolve()
    out_dir = (out_root / subfolder / job_id).resolve()
    try:
        out_dir.relative_to(out_root)
    except ValueError:
        raise ValueError(f"unsafe output path: subfolder={subfolder!r} job_id={job_id!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs, seen = [], set()
    pending_cleanup = set()
    receipts = ResultReceipts(cfg_mod._config_path().parent / "download_receipts", [
        cfg.get("modal_endpoint_base"), cfg.get("modal_volume_name"),
        job_id, final.get("completed_at"),
    ])
    # 回执目录只增不删(每个取回过的产物一个文件),取回时顺带按 mtime 清掉 30 天前的
    # (2026-10-05 深度 review)。尽力而为,清不掉不影响这次取回。
    try:
        await asyncio.to_thread(receipts.prune, RECEIPT_MAX_AGE_S)
    except Exception as e:
        print(f"[modal_bridge] download_receipts 清理跳过: {e}")

    def _atomic_write(dst: Path, data: bytes) -> int:
        """先写 .part 再 rename —— 半截文件不能以正式名出现在 output/ 里。
        ComfyUI 的画廊/前端会直接读这个目录,写到一半被读到就是一张坏图;
        Volume 下载那条路径(bridge_client / modal_volume)早就是 .part+rename 了,这边补齐。"""
        tmp = dst.with_suffix(dst.suffix + ".part")
        tmp.resolve().relative_to(out_root)
        tmp.write_bytes(data)
        tmp.replace(dst)
        return len(data)

    def _dedup(fn: str) -> str:
        """与 bridge_client.download_outputs._name 行为逐字一致(那份是独立可 vendor 的单文件,
        不能 import 这里),test_dedup_identical_routes_and_bridge_client 钉死两边。"""
        if fn not in seen:
            seen.add(fn)
            return fn
        # ⚠ 撞名必须循环到真正空出来为止,不能只改一次名。曾经是「撞名就取 {stem}_{len(seen)}」,
        #   而那个候选名可能**本来就在输入里**:a.png / a_2.png / a.png 三份不同内容,第三份
        #   算出的 a_2.png 正好是第二份已占的名字 —— 第二份被静默覆盖,而三条都报成功、
        #   返回列表长度还是 3。只有去数磁盘上的文件才看得出少了一份。
        #   (2026-09-09 seedance 侧复现;H3 一个任务出视频+音频+预览,ComfyUI 的产物名又是
        #   每实例独立递增,跨 job 撞名是常态,不是边角。)
        # ⚠ rpartition 找不到 "." 时返回 ("", "", 整串) —— ext 反而是整个文件名。只判 ext
        #   非空会把 "noext" 变成 "_1.noext"。要判分隔符,不能判 stem/ext 本身。
        stem, sep, ext = fn.rpartition(".")
        if not sep:
            stem, ext = fn, ""
        n = len(seen)
        while True:
            cand = f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}"
            if cand not in seen:
                seen.add(cand)
                return cand
            n += 1

    images = final.get("images")
    if isinstance(images, list) and images:
        for img in images:
            vp = img.get("volume_path")
            b64 = img.get("data_base64")
            if not vp and not b64:
                continue
            fn = _dedup(Path(img.get("filename") or "output.png").name)  # basename 防路径逃逸
            local = out_dir / fn
            local.resolve().relative_to(out_root)
            local.with_name(local.name + ".part").resolve().relative_to(out_root)
            if vp:
                # ⚠ vp 整个来自浏览器提交的 modal_state,没人替我们验过 —— 而这条路
                # **绕过云端 fetch_endpoint、直连 Volume SDK**,云端那道囚笼管不到。
                # 伪造成 models/... 就能把模型下载走并删掉(取回后即删是既定行为),
                # 删除不可逆。所以本地必须自己囚一次(规则与云端逐字相同)。
                if not contract.is_safe_output_path(job_id, vp):
                    raise RuntimeError(f"volume_path 越界(必须在 _outputs/{job_id}/ 内): {vp!r}")
                # 云端报的原始字节数(契约 C12)。没有就退回 SDK 报的大小;两个都拿不到 = 校验不了。
                expected = img.get("size_bytes")
                if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                    expected = None
                verified = True
                size = receipts.completed_size(vp, local)
                if size is None:
                    total = expected if expected is not None else \
                        await asyncio.to_thread(modal_volume.volume_file_size, cfg, vp)
                    want = expected if expected is not None else (total or None)
                    sampler = asyncio.create_task(
                        _sample_part_size(job_id, local.with_name(local.name + ".part"), total, fn))
                    try:
                        # 期望大小交给下载函数在 .part 上校验(契约 C14),不对就不会 rename 成正式名。
                        await asyncio.to_thread(modal_volume.download_volume_file, cfg, vp,
                                                str(local), expected_size=expected)
                        size = actual = local.stat().st_size
                        if want is not None and actual != want:
                            # 纵深:万一下载函数没校验就 rename 了,半截文件也不能以正式名留在
                            # output/ 里 —— ComfyUI 画廊会直接读它,下次取回还会被同名去重当成已有产物。
                            local.unlink(missing_ok=True)
                            raise RuntimeError(f"incomplete output: {actual}/{want} bytes")
                        if want is None:
                            # 拿不到期望大小(老云端没报 size_bytes、SDK 也没报大小):不记回执、
                            # 不删云端副本,交给云端 TTL —— 万一是半截,还能重取(契约 C12)。
                            verified = False
                            print(f"[modal_bridge] ⚠ {vp} 拿不到期望大小,无法校验完整性;"
                                  f"云端副本保留到 TTL 回收")
                        else:
                            receipts.record(vp, local)
                    except Exception as e:
                        raise RuntimeError(f"volume download {vp} failed: {e}")
                    finally:
                        sampler.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await sampler
                if verified:
                    pending_cleanup.add(vp)
            else:
                _fetch_progress_set(job_id, stage="decode", label=fn,
                                    done=0, total=len(b64) * 3 // 4)
                # 解码也放进线程:以前 b64decode 作为参数先在事件循环里跑完才进线程,
                # 几十 MB 的内联产物照样卡住整个 ComfyUI(深度 review)。
                size = await asyncio.to_thread(
                    lambda _p=local, _b=b64: _atomic_write(_p, base64.b64decode(_b)))
                _fetch_progress_set(job_id, stage="decode", label=fn, done=size, total=size)
            outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                            "type": "output", "size_bytes": size,
                            "node_id": img.get("node_id"),  # 来源节点 → 前端按节点回填
                            "key": img.get("key")})          # 原始输出键 → 前端按键派发渲染
        # 全部落盘后才删除；回执保留，以便成功响应丢失／进程重启后再次取回。
        for vp in pending_cleanup:
            try:
                await asyncio.to_thread(modal_volume.remove_volume_path, cfg, vp)
            except Exception as e:
                print(f"[modal_bridge] output cleanup deferred: {e}")
        return outputs

    # 单图回退
    fn = Path(final.get("filename") or "output.png").name  # basename 防路径逃逸
    b64 = final.get("data_base64")
    image_url = final.get("image_url")
    if b64:
        size = await asyncio.to_thread(lambda: _atomic_write(out_dir / fn, base64.b64decode(b64)))
        outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                        "type": "output", "size_bytes": size})
    elif image_url:
        async with aiohttp.ClientSession() as s:
            async with s.get(image_url) as r:
                if r.status >= 400:
                    raise RuntimeError(f"download {image_url} failed: {r.status}")
                data = await r.read()
        size = await asyncio.to_thread(_atomic_write, out_dir / fn, data)
        outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                        "type": "output", "size_bytes": size, "source_url": image_url})
    return outputs


async def _fetch_job(final: dict, job_id: str, subfolder: str, cfg: dict) -> list:
    try:
        return await _write_results(final, job_id, subfolder, cfg)
    finally:
        _FETCH_PROGRESS.pop(job_id, None)


def _fetch_finished(job_id: str, task: asyncio.Task) -> None:
    if _FETCH_TASKS.get(job_id, (None, None))[1] is task:
        _FETCH_TASKS.pop(job_id, None)
    if not task.cancelled():
        task.exception()  # 请求断开时也收走异常，避免无人消费的 Task 警告


# 引用 input/ 下本地文件的节点 → 各自的输入键。**键名不统一,不能一律取 "image"**:
# LoadVideo 是 "file"、LoadAudio 是 "audio"(ComfyUI v0.34.6 源码核实)。
# 漏一个键 = 那个文件根本不进 payload,云端 ComfyUI 找不到它,报错长得像工作流参数错
# 而不是"少传了素材"。2026-09-19 之前这里只有三个 LoadImage*,所以面板送不了视频/音频参考。
# ⚠ 这张表在 routes.py 和 bridge_client.py 各有一份(后者是零依赖、可被下游整份 vendor 的
#   独立客户端,不能 import 前者),由 test_input_file_nodes_identical_routes_and_client 钉死。
_INPUT_FILE_NODES = {
    "LoadImage": ("image",),
    "LoadImageMask": ("image",),
    "LoadImageOutput": ("image",),
    "LoadVideo": ("file",),
    "LoadAudio": ("audio",),
}


async def _deployed_reqs_hash(cfg: dict) -> str:
    """云端镜像**实际装的**私有节点依赖的指纹。

    ⚠ 以前用本机 config 里的 local_node_reqs_deployed_hash,而镜像是多台机器共享的:机器 A 改了依赖
      并部署,机器 B 的 config 还是旧的,每次都误判「欠一次重建」(2026-09-23 review)。
      0.8.50 改成读云端 /health,又引入两个问题(2026-09-24 review):/health 跑在大镜像上,冷启动常超
      8s 超时 → 静默退回本机旧指纹,多机误判原样回来,而且 /sync_local_nodes 会据此**无确认地**自动
      重建 3-5 分钟;每次 RunModal 预检还要多等最多 8s;依赖行里的 git+https://TOKEN@ 也经 /health 外泄。
    现在由每次成功部署把依赖清单写进 <app>-meta 这个 modal.Dict(任何机器部署都写),这里用 SDK 直读:
    确定、共享、没有冷启动,也不再经 /health。拿不到(老部署还没写过)才退回本机记录。"""
    reqs = await asyncio.to_thread(modal_volume.deployed_reqs, cfg)
    if reqs is not None:
        return node_sync.local_node_reqs_hash(reqs)
    return cfg.get("local_node_reqs_deployed_hash", "")


def _extract_input_file_name(cls: str, ins: dict) -> str | None:
    """按节点类型取它引用的本地文件名。取不到(或那一位接的是连线而非字面量)返回 None。"""
    keys = _INPUT_FILE_NODES.get(cls)
    if not keys:
        return None
    # "filename" 是给自定义节点的兜底;连线形态是 ["3", 0] 这样的 list,必须判 str 跳过。
    for k in (*keys, "filename"):
        v = ins.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _extract_input_image_names(prompt: dict) -> list[str]:
    """遍历 prompt 找所有会引用 input/ 下本地文件的节点(图 / 视频 / 音频),返回去重文件名。"""
    names: list[str] = []
    seen: set[str] = set()
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type", "")
        name = _extract_input_file_name(cls, node.get("inputs", {}) or {})
        if not name or name in seen:
            continue
        # 子目录形式("clipspace/xxx"、"refs/clip.mp4")照收 —— 它们是 input/ 下的真实文件。
        # ⚠ 曾经是「打一行 WARN 然后 continue」,而提交流程照常往下走:用户看到的是提交成功,
        #   实际 input_image_count=0、云端找不到素材。**漏传后继续提交**是最糟的形态 ——
        #   控制台那行 WARN 没人看,失败原因显示在云端、看起来像工作流参数错。
        #   (2026-09-20 codex review 抓到。)现在越界的会在 _read_input_as_b64 里抛
        #   FileNotFoundError → /submit 回 400,失败在本地、当场可见。
        seen.add(name)
        names.append(name)
    return names


def _read_input_as_b64(name: str) -> dict:
    """读 input/<name>,返回 Modal 期望的 {name, image (data uri)} 格式。

    ⚠ name 来自工作流 JSON,**可以带子目录**("refs/clip.mp4"),所以这里是唯一的边界检查:
    input 目录里放一个指向目录外的**符号链接**,exists() 照样为真、read_bytes() 就把目录外
    内容读出来上传了;"../" 同理。必须 resolve 后确认仍在 input 目录内 —— 与模型查找用的是
    同一份囚笼(modal_volume.is_path_within_roots)。抛错即 /submit 400,不会漏传后继续提交。
    """
    root = _input_dir()
    p = root / name
    if not p.exists():
        raise FileNotFoundError(f"Input image not found locally: {p}")
    if not modal_volume.is_path_within_roots(p, [root]):
        raise FileNotFoundError(f"输入图越界(解析后不在 input 目录内): {name}")
    blob = p.read_bytes()
    # ⚠ 别硬拼 data:image/<ext> —— 视频/音频会拼出 "data:image/mp4" 这种假话。云端
    # upload_images 只按逗号切 base64、不读 MIME,所以不会炸,但数据里不该写假的。
    # 与 bridge_client.pack_input_images 用同一套判定(mimetypes,按扩展名)。
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    b64 = base64.b64encode(blob).decode("ascii")
    return {"name": name, "image": f"data:{mime};base64,{b64}"}


async def _emit(resp: web.StreamResponse, text: str) -> None:
    try:
        await resp.write(text.encode("utf-8"))
    except Exception:
        pass


async def _cloud_error_text(r) -> str:
    """云端非 2xx 响应 → 一句可读的错误(优先 JSON 里的 error 字段)。读不到返回空串,不抛。"""
    try:
        text = await r.text()
    except Exception:
        return ""
    try:
        j = json.loads(text)
        if isinstance(j, dict) and j.get("error"):
            return str(j["error"])[:300]
    except ValueError:
        pass
    return " ".join(text.split())[:200]


# 失败时回灌到 ComfyUI 控制台的尾部行数。够看清一个 pip / 镜像构建的报错,又不至于刷屏。
_TAIL_ON_FAIL = 40


async def _run_streamed(resp: web.StreamResponse, cmd: list[str], cwd: str, env: dict) -> int:
    """跑一个命令,stdout/stderr 实时流式回前端,返回 returncode(找不到可执行文件返回 127)。
    用线程 + subprocess.Popen(不走 asyncio 子进程)——避免 Windows 上事件循环不支持
    子进程(SelectorEventLoop → NotImplementedError)的坑,Mac/Linux/Win 一致。

    ⚠ 失败时把尾部若干行**同时 print 到 ComfyUI 控制台**。以前输出只写进 HTTP 流:
    前端拿到了完整内容,却只 console.log 一份、再截断成 72 字符闪过进度窗,最后抛一句
    通用文案。于是镜像构建失败(典型:某个 custom_node 的 requirements 装不上)在
    ComfyUI 日志里**一行痕迹都没有**,用户必须自己去 `modal app logs` 才看得到真错误
    (2026-08-31 由 skybox-ai 会话实测报告)。真实报错留在服务端日志里,是排查的起点。"""
    # ⚠ 用 redact_cmd 而不是 ' '.join —— secret create 的 argv 里是明文凭据,见 node_sync.redact_cmd
    await _emit(resp, f"$ {node_sync.redact_cmd(cmd)}\n")

    tail: list[str] = []

    def work(emit):
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            emit(f"  ✗ 找不到可执行文件: {cmd[0]}\n")
            return 127
        for line in proc.stdout:
            tail.append(line)
            if len(tail) > _TAIL_ON_FAIL:
                tail.pop(0)
            emit(line)
        proc.wait()
        return proc.returncode

    rc = await _run_blocking_streamed(resp, work)
    if rc != 0 and tail:
        print(f"[modal_bridge] ✗ 命令失败 rc={rc}: {node_sync.redact_cmd(cmd)}")
        print(f"[modal_bridge] --- 输出尾部 {len(tail)} 行 ---")
        for line in tail:
            print(f"[modal_bridge] | {line.rstrip()}")
        print("[modal_bridge] --- 完整日志见上方进度窗 / 浏览器控制台 ---")
        # 认得出的失败形态,给一句人话 —— 同时进前端进度窗和 ComfyUI 控制台。
        hint = node_sync.diagnose_build_failure("".join(tail))
        if hint:
            print(f"[modal_bridge] {hint}")
            await _emit(resp, f"\n{hint}\n")
    return rc


_STREAM_SENTINEL = object()

# 模型上传串行化:同一时刻只允许一个 /sync_models 真正上传,避免并发工作流同时往
# Volume 写同一个大模型撞车(用户实测 35GB flux2 dev 并发上传会失败)。
_UPLOAD_LOCK = asyncio.Lock()

# 部署串行化:写 _custom_nodes_data.py + modal deploy 这段必须独占——两个并发请求
# (/sync_nodes 之间、或 /sync_nodes 与 /deploy)同时写清单会互相覆盖、两个 modal deploy
# 打同一个 app 也会冲突。整段(写文件 + deploy)包进同一把锁。
_DEPLOY_LOCK = asyncio.Lock()

# poll 记日志用:job_id → 上次见到的 status(只在变化时打日志,避免高频 poll 刷屏)。
# 走到终态会 pop,但**没走到终态就没人再 poll 的**(关 tab / 断网)会留下来,而 ComfyUI 是
# 长跑进程。条目很小,cap 一下就够,不值得为它上 TTL。
_LAST_POLL_STATUS: dict = {}
_LAST_POLL_MAX = 500

_ADMIN_HEADER = "X-Modal-Bridge-Capability"
_ADMIN_REQUIRED_HEADER = "X-Modal-Bridge-Auth"


def _loopback_host(request: web.Request) -> tuple[bool, str]:
    """(是不是本机直连, Host)。peer + Host + 转发头三者一起判,见 contract.is_direct_loopback_request。"""
    try:
        host = request.host
    except Exception:
        host = ""
    forwarded = ",".join(x for x in (
        request.headers.get("X-Forwarded-For", ""),
        request.headers.get("X-Real-IP", ""),
    ) if x)
    return contract.is_direct_loopback_request(request.remote, host, forwarded), host


def _local_origin_ok(request: web.Request, host: str) -> bool:
    return contract.is_safe_local_origin(
        request.headers.get("Origin"), request.scheme, host,
        request.headers.get("Sec-Fetch-Site", ""))


def _capability_matches(request: web.Request, expected: str) -> bool:
    supplied = (request.headers.get(_ADMIN_HEADER) or "").strip()
    # token_urlsafe 生成 ASCII；误粘中文等输入应返回 403，不让 compare_digest 抛 500。
    return bool(supplied and expected and supplied.isascii() and expected.isascii()
                and secrets.compare_digest(supplied, expected))


def _admin_denial(request: web.Request) -> web.Response | None:
    """本机同源直连免 capability;经局域网 / 反向代理 / 容器过来的一律要。

    peer+Host 双判避免反向代理把远程访客伪装成 127.0.0.1。capability 只存在本机
    0600 config 和调用方浏览器 localStorage 中,绝不从匿名端点回吐。

    ⚠ 0.8.36 曾对本机直连也要求 capability(为回应 Registry 的 policy-v0.2 判定),
    2026-09-08 用户决定撤回。代价与收益不成比例:每个浏览器都要手工粘一次 token,
    而 /submit /poll 也在保护范围内 —— 连正常提交任务都被拦;而对**同源浏览器页面**
    的安全增量近乎为零 —— 能在 ComfyUI 页面里执行 JS 的攻击者本来就能直接排队跑
    工作流,不必绕这些路由。跨站页面由下面的 is_safe_local_origin 挡住,那一层不需要
    用户做任何事。保留的部分:非 loopback 访问(局域网 / 反代 / host.docker.internal
    / MCP)仍须 capability —— 那里没有"同源"可言,而且花的是用户的云端账单。
    """
    is_loopback, host = _loopback_host(request)
    if is_loopback:
        # ComfyUI 开启 CORS 时会替换默认 Origin 中间件；保留独立的本机跨站防护。
        if not _local_origin_ok(request, host):
            return web.json_response({"error": "cross-origin local request rejected"}, status=403)
        return None      # 本机同源(或无 Origin 的本机 CLI)直接放行
    if _capability_matches(request, cfg_mod.ensure_local_api_capability()):
        return None
    return web.json_response(
        {"error": "admin capability required",
         "code": "modal_bridge_admin_capability_required"},
        status=403,
        headers={_ADMIN_REQUIRED_HEADER: "capability-required"},
    )


# /health 匿名(受限)视图用:最近一次完整检查的结论。只是个布尔,不含 manifest。
_LAST_HEALTH: dict = {"healthy": None}


def _health_full_view(request: web.Request) -> bool:
    """/health 给不给完整内容:与管理路由同一套判据(本机同源直连,或带有效 capability),
    但**不生成** capability、也不回 403 —— /health 仍是公开端点。"""
    is_loopback, host = _loopback_host(request)
    if is_loopback:
        return _local_origin_ok(request, host)
    expected = str(cfg_mod.load_config().get("local_api_capability") or "").strip()
    return _capability_matches(request, expected)


class _BadRequest(Exception):
    """请求体 / 字段类型不对。_admin_only 统一转成 400 —— 以前这类输入直接在 handler 里
    AttributeError / TypeError,回 500;流式路由更糟,prepare 之后才炸,流被截断,前端只能
    显示一句笼统的失败(2026-10-05 深度 review)。"""


def _config_corrupt_response(e: Exception) -> web.Response:
    """config.json 损坏(cfg_mod.ConfigCorrupt):500 + 原文说明(第几行、修好或删除)。
    不能像以前那样退回默认值继续 —— 之后任何一次保存都会用默认值覆盖用户的凭据。"""
    print(f"[modal_bridge] ✗ {e}")
    return web.json_response({"error": str(e), "code": "config_corrupt"}, status=500)


def _admin_only(handler):
    @functools.wraps(handler)
    async def guarded(request: web.Request):
        try:
            denial = _admin_denial(request)
            if denial is not None:
                return denial
            return await handler(request)
        # 这两类只会在 prepare 之前抛(流式路由 prepare 之后的异常由 _stream_run 收口)。
        except _BadRequest as e:
            return web.json_response({"error": str(e)}, status=400)
        except cfg_mod.ConfigCorrupt as e:
            return _config_corrupt_response(e)
    return guarded


async def _json_object(request: web.Request) -> dict:
    """读请求体并确认是 JSON 对象;不是就抛 _BadRequest(→ 400)。"""
    try:
        body = await request.json()
    except Exception:
        raise _BadRequest("请求体不是合法的 JSON") from None
    if not isinstance(body, dict):
        raise _BadRequest("请求体必须是 JSON 对象")
    return body


def _opt_str(body: dict, key: str) -> str:
    """可选字符串字段:缺省 / null → "";其它非字符串类型 → 400。"""
    v = body.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise _BadRequest(f"{key} 必须是字符串")
    return v


def _one_line(text) -> str:
    return " ".join(str(text).split())


async def _emit_abort(resp: web.StreamResponse, reason: str, detail: str = "") -> None:
    """部署流中的保护性中止(契约 C5):流里只出**一行** `== ✗ 部署已中止:… ==`,完整多行说明
    (reason + detail)print 到 ComfyUI 控制台。前端弹窗只取最后一行带 ✗ 的内容,多行说明会被切成
    半句;而且必须一眼看出这是「被拦下」,不是「依赖装不上」。auto_deploy_blocker 的消息自带
    「自动部署已中止」,原样用。"""
    print(f"[modal_bridge] ✗ 部署已中止:\n{reason}\n{detail}".rstrip())
    line = _one_line(reason)
    if not line.startswith("自动部署已中止"):
        line = f"部署已中止:{line}"
    await _emit(resp, f"\n== ✗ {line} ==\n")


async def _stream_run(request: web.Request, label: str, body) -> web.StreamResponse:
    """prepare 一个 text/plain 流,跑 body(resp) -> rc,最后**一定**输出 `__DEPLOY_DONE__ rc=…`。

    前端靠这一行判结局;以前 prepare 之后任何一个没接住的异常都会让流直接断掉,前端只能报
    一句「连接中断」,真正的原因只在 aiohttp 的 traceback 里(深度 review)。"""
    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"},
    )
    await resp.prepare(request)
    try:
        rc = await body(resp)
    except Exception as e:
        print(f"[modal_bridge] ✗ {label}出错: {e}\n{traceback.format_exc()}")
        await _emit(resp, f"\n== ✗ {label}出错:{_one_line(e)} ==\n")
        rc = 1
    await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
    with contextlib.suppress(Exception):
        await resp.write_eof()
    return resp


def _compute_local_node_reqs(cfg: dict) -> list[str]:
    """从 Volume 每个私有节点的 manifest 算出镜像依赖清单。**纯读,不落盘。**

    多机环境不能只扫当前机器目录。旧 zip 没有 manifest 时保留已有 flat 清单；该节点
    下次参与同步会被强制重传并完成迁移。

    与 _refresh_local_node_reqs 拆开,是因为 /local_nodes_diff 这种只读预检也要算这个
    指纹(判断"依赖镜像是否还欠一次重建"),而它不该写文件、也不该去抢 _DEPLOY_LOCK。
    """
    # 读不到 Volume 时 list_volume_local_nodes 抛 local_nodes.VolumeUnavailable(契约 C13),
    # 这里**不接**,让调用方中止。以前读失败与「真的没有」都是 [],于是退回本机那份被 gitignore 的
    # 清单 —— 新机器 / 插件重装后它是空的,一次部署就把所有私有节点的依赖从镜像里删了(深度 review)。
    folders = local_nodes.list_volume_local_nodes(cfg, max_age=0)
    if not folders:
        return []   # 确认 Volume 上没有私有节点(C13 起 [] 只表示这个)
    manifests = local_nodes.volume_local_node_requirements(cfg, folders)
    reqs: list[str] = []
    seen: set[str] = set()
    for folder in sorted(manifests):
        for req in manifests[folder]:
            if req not in seen:
                seen.add(req)
                reqs.append(req)
    if set(folders) - set(manifests):
        for req in node_sync.read_local_node_reqs():
            if req not in seen:
                seen.add(req)
                reqs.append(req)
    return reqs


def _refresh_local_node_reqs(cfg: dict) -> list[str]:
    """算依赖清单并落盘。调用方须在 _DEPLOY_LOCK 内调用(它写 _local_nodes_data.py)。"""
    reqs = _compute_local_node_reqs(cfg)
    node_sync.write_local_node_reqs(reqs)
    return reqs


async def _run_blocking_streamed(resp: web.StreamResponse, fn):
    """在线程里跑一个阻塞函数 fn(emit),emit(line) 线程安全地把日志流式写回 resp。
    返回 fn 的返回值。用于 Volume 上传这种阻塞 + 想要实时进度的场景。"""
    loop = asyncio.get_running_loop()  # get_event_loop 在运行中的循环里已 deprecated
    q: asyncio.Queue = asyncio.Queue()

    def emit(line: str):
        loop.call_soon_threadsafe(q.put_nowait, line)

    def runner():
        try:
            return fn(emit)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, _STREAM_SENTINEL)

    task = loop.run_in_executor(None, runner)
    while True:
        line = await q.get()
        if line is _STREAM_SENTINEL:
            break
        await _emit(resp, line)
    return await task


async def _ensure_modal(resp: web.StreamResponse) -> int:
    """确保 ComfyUI 内嵌 Python 里有 modal 包。缺则**报错并给出手动装法**,不自动装。

    以前这里会起子进程装包。移除的原因是 ComfyUI Registry 明令禁止
    「Runtime package installation through subprocess calls」——插件依赖统一由
    ComfyUI Manager 在安装时装(modal 已声明进 pyproject.toml 的 dependencies
    和 requirements.txt)。留着这段会让发布版本被判 Flagged,用户在 Manager 里
    根本装不到新版,代价远大于"少一步自动安装"的便利。

    正常路径下这个分支不会触发:通过 Manager / Registry 装本插件时 modal 已经装好了。
    只有手动 git clone 进 custom_nodes、又没装依赖的用户会走到这里。
    """
    # modal_available 起一个子进程 import modal(冷启动 1~3 秒,上限 20 秒),放进线程,
    # 别让整个 ComfyUI 的事件循环陪它等(2026-10-05 深度 review)。
    if await asyncio.to_thread(node_sync.modal_available):
        await _emit(resp, "== modal 包已就绪 ==\n")
        return 0
    await _emit(resp, "== ✗ 未检测到 modal 包 ==\n")
    await _emit(resp, "   本插件的依赖由 ComfyUI Manager 在安装时装。手动 clone 进 custom_nodes 的话,\n")
    await _emit(resp, "   在 ComfyUI 用的那个 Python 环境里装一次即可:\n\n")
    await _emit(resp, "       <ComfyUI 的 python> -m pip install -U modal\n\n")
    await _emit(resp, "   装完重启 ComfyUI 再点部署。(也可以在 Manager 里卸载后重装本插件,依赖会自动装上)\n")
    return 1


async def _auto_redeploy_for_local_reqs(resp: web.StreamResponse) -> int:
    """/sync_local_nodes 传完之后:私有节点依赖变了就自动重建镜像。调用方持有 _DEPLOY_LOCK。"""
    latest_cfg = cfg_mod.load_config()
    try:
        reqs = await asyncio.to_thread(_refresh_local_node_reqs, latest_cfg)
    except local_nodes.VolumeUnavailable as e:
        # 契约 C13:读不到 Volume 上的私有节点名单就算不出依赖 —— 以前当成「没有私有节点」,
        # 算出空依赖 → 指纹变了 → **无确认地**重建一个不含任何私有节点依赖的镜像。
        await _emit_abort(resp, f"自动部署已中止:读不到 Volume 上的私有节点名单({_one_line(e)}),"
                                f"算不出镜像要装的依赖。稍后重试,或在面板点「推送到云端」")
        return 1
    target_hash = node_sync.local_node_reqs_hash(reqs)
    deployed_hash = await _deployed_reqs_hash(latest_cfg)
    needs_redeploy = target_hash != deployed_hash and bool(reqs or deployed_hash)
    if not needs_redeploy:
        return 0
    await _emit(resp, f"== 私有节点依赖已变化({len(reqs)} 条),自动重新部署 ==\n")
    # 同 /deploy:这里也拿本机清单当全局清单部署,先并回云端独有的节点。
    node_sync.ensure_baked_file()
    try:
        _rec = await asyncio.to_thread(node_sync.reconcile_baked_with_cloud, latest_cfg)
    except node_sync.DeployBlocked as _blk:
        # 契约 C5:流里一行,完整多行说明进控制台(以前这条路径连 print 都没有)
        await _emit_abort(resp, str(_blk))
        return 1
    if _rec.added:
        await _emit(resp, f"   节点清单:并回云端独有的 {len(_rec.added)} 个 —— "
                          f"{', '.join(_rec.added)}\n")
    _drift = node_sync.drift_message(_rec)
    if _drift:
        await _emit(resp, _drift)
    # 自动部署不替用户决定公共节点的版本:有判断不了的差异 / 没读到云端就停,
    # 交给显式的「推送到云端」(2026-09-27 review)。
    # ⚠ 前端弹窗取的是**最后一行**带 ✗ 的内容:阻断说明必须是单行、且之后不能再
    #   落到通用的「依赖部署失败」—— 那句会把用户引去查 requirements。
    _stop = node_sync.auto_deploy_blocker(_rec)
    if _stop:
        await _emit_abort(resp, _stop, _drift)
        return 1
    rc = await _ensure_modal(resp)
    if rc == 0:
        rc = await _run_streamed(
            resp, node_sync.deploy_command(),
            cwd=str(node_sync.MODAL_APP_DIR),
            env=node_sync.deploy_env(latest_cfg),
        )
    if rc == 0:
        final_cfg = cfg_mod.load_config()
        final_cfg["local_node_reqs_deployed_hash"] = target_hash
        cfg_mod.save_config(final_cfg)
        await asyncio.to_thread(modal_volume.record_deployed_reqs, latest_cfg, reqs)
        await _emit(resp, "== ✓ 私有节点依赖镜像已更新 ==\n")
    else:
        await _emit(resp, "== ✗ 私有节点依赖部署失败,停止本次提交 ==\n")
    return rc


# 部署收尾验证 /health 时,404 重试几次、隔多久(测试里压成 0)。刚部署完 endpoint 偶尔要几秒才生效。
_HEALTH_404_TRIES = 3
_HEALTH_404_RETRY_S = 3.0

# workspace / app 名会拼进 endpoint:https://<workspace>--<app>-<label>.modal.run。
# 规则按 Modal 实际行为(2026-10-05 核实 modal.com/docs/guide/webhook-urls 与 SDK 1.4.3 的
# _utils/name_utils.py):URL 的 source / label 只含小写字母、数字、连字符;自定义 label 含非法字符
# 直接被拒(本插件的 label 是 f"{APP_NAME}-run" 这类,所以 app 名同样受限);整个子域名
# 超过 63 字符会被截断并拼上哈希,按名字拼出来的地址就对不上了。
# ⚠ 以前不校验:把 https://modal.com/... 整个粘进 workspace,会拼出 https://https://... 并落盘,
#   之后每个请求都失败,还看不出原因(深度 review)。
_MODAL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LONGEST_LABEL_SUFFIX = max(len("-" + x) for x in ("run", "status", "fetch", "cancel", "health"))


def _deploy_name_errors(workspace: str, app_name: str) -> list[str]:
    errs = []
    if not _MODAL_NAME_RE.match(workspace or ""):
        hint = ""
        if "://" in workspace or "/" in workspace or "." in workspace:
            hint = " —— 看起来粘贴了一整段 URL,只填 workspace 名那一段(如 modal.com/apps/<这一段>/…)"
        elif workspace != workspace.lower():
            hint = " —— 要用小写(Modal 地址里的 workspace 都是小写)"
        errs.append(f"workspace 只能是小写字母、数字和单个连字符,首尾不能是连字符:{workspace!r}{hint}")
    if not _MODAL_NAME_RE.match(app_name or ""):
        errs.append(f"app 名只能是小写字母、数字和单个连字符,首尾不能是连字符(Modal 会拒绝含其它字符的"
                    f" endpoint 名):{app_name!r}")
    if not errs and len(workspace) + 2 + len(app_name) + _LONGEST_LABEL_SUFFIX > 63:
        errs.append(f"workspace + app 名太长:endpoint 子域名 {workspace}--{app_name}-health 超过 63 个字符,"
                    f"Modal 会截断并加哈希,拼出来的地址就对不上了。把 app 名改短一点")
    return errs


async def _fetch_cloud_node_info(cfg: dict) -> tuple[dict | None, str]:
    """读云端 /health 报的节点清单 → (info, "");读不到 → (None, 原因)。原因会原样给前端看
    (cloud_unchecked),所以要是一句能看懂的话,不能是空串。"""
    try:
        async with aiohttp.ClientSession() as session:
            info = await modal_client.list_nodes(session, cfg)
    except Exception as e:
        return None, (str(e) or type(e).__name__)
    if not (isinstance(info, dict) and isinstance(info.get("custom_nodes"), list)):
        return None, "云端 /health 没有报节点清单"
    return info, ""


async def _deploy_locked(resp: web.StreamResponse, body: dict, token_id: str,
                         token_secret: str, workspace: str) -> tuple[int, tuple]:
    """/deploy 持有 _DEPLOY_LOCK 期间的步骤。返回 (rc, 交给 _deploy_verify 的参数)。"""
    # R8:锁内重读(2026-10-05 深度 review)。以前用的是拿锁前读的 cfg:排队期间另一次部署 /
    # 节点同步写回的 key、清单指纹、档位,会被这次部署的收尾当成「部署开始时的值」做三方合并。
    # 缺省值优先沿用已有 config(重新部署时不重置用户之前的选择)
    cfg = cfg_mod.load_config()
    app_name = (body.get("app_name") or cfg.get("modal_app_name") or "comfyui-bridge").strip()
    _name_errs = _deploy_name_errors(workspace, app_name)
    if _name_errs:   # 排队期间别处改了 app 名(拿锁前校验过 body / 旧 config)
        for e in _name_errs:
            await _emit(resp, f"✗ {e}\n")
        return 2, ()
    volume_name = (body.get("volume_name") or cfg.get("modal_volume_name") or "comfyui-bridge-models").strip()
    default_gpu = (body.get("default_gpu") or cfg.get("default_gpu") or "H100").strip()
    scaledown = int(body.get("scaledown_window") or cfg.get("scaledown_window") or 12)
    # ⚠ 下面 secret create 用的是 --force,会**整份替换** Modal Secret。HF / Civitai token
    #   以前只从请求体取、从不持久化,而面板根本不发这两个字段 —— 于是用 deploy.py
    #   --hf-token 配过的 token,点一次「推送到云端」就被抹掉,节点下 gated 权重静默失败。
    #   现在同 comfy_api_key:留空 = 沿用已存,且写回 config(0600)。(2026-09-23 review)
    hf_token = (body.get("hf_token") or "").strip() or cfg.get("hf_token", "")
    civitai_token = (body.get("civitai_token") or "").strip() or cfg.get("civitai_token", "")
    # comfy.org API key(API 节点用):留空 = 沿用已存的(/config 不回显)。持久化进 config,重部署不丢。
    comfy_api_key = (body.get("comfy_api_key") or "").strip() or cfg.get("comfy_api_key", "")
    # AIGC Studio 交付(可选,网站 aigc-r2 模式)。URL 明文回显、输入框预填现值 →
    # 传了空串 = 用户清掉了(停用);没传该字段(老前端)才沿用已存。bypass 密钥不回显,
    # 规则同 comfy_api_key(留空 = 沿用)。都写进 Modal Secret,worker 交付时读。
    if "aigc_studio_base_url" in body:
        aigc_base_url = (body.get("aigc_studio_base_url") or "").strip().rstrip("/")
    else:
        aigc_base_url = cfg.get("aigc_studio_base_url", "")
    # 密钥三态,顺序不能乱(review 抓到第一版把用户刚输入的也丢了):
    #   · 这次显式输入了 → 用它,**不管 URL 有没有**(用户可能先填密钥、URL 稍后在设置页填;
    #     /config 那条路径对同一场景也是这么保护的,两边必须一致);
    #   · 没输入、URL 存在 → 沿用已存(密码框留空 = 沿用,标准语义);
    #   · 没输入、URL 为空 → 清掉。没有 URL 就没有用它的地方,别把它烤进 Modal Secret、
    #     也别继续留在本地 config。这条同时兜住 0.8.30 之前的遗留残留:那时密钥只能更新、
    #     无法清除(codex 抓到),停用集成后它会一直躺在 config.json 里并进入每次新建的 Secret。
    #     2026-10-05 起设置页清空 URL 不再连带清密钥(契约 C10),这里是唯一的清理点。
    _typed = (body.get("aigc_bypass_secret") or "").strip()
    if _typed:
        aigc_bypass = _typed
    elif aigc_base_url:
        aigc_bypass = cfg.get("aigc_bypass_secret", "")
    else:
        aigc_bypass = ""
    endpoint_base = f"https://{workspace}--{app_name}"
    # 私有鉴权 key:已有就复用(不让旧 config 失效),否则新生成(写 Secret 之前先落 config,见下)
    _new_key = not cfg.get("bridge_api_key")
    bridge_key = cfg.get("bridge_api_key") or node_sync.gen_bridge_key()

    # ComfyUI 版本跟随本机:检测本机版本 → 解析云端 clone tag(无对应取最接近,只警告不中止)
    comfyui_version = node_sync.detect_local_comfyui_version()
    _tags = await asyncio.to_thread(node_sync.list_comfyui_tags)
    comfyui_tag, _tag_note = node_sync.resolve_comfyui_tag(
        comfyui_version, _tags, prev_tag=cfg.get("comfyui_tag", ""),
        pin=cfg.get("comfyui_tag_pin", ""))
    # ⚠ 必须在 cfg.update 之前取:那一步会用新值覆盖 comfyui_tag,取晚了永远相等。
    _tag_change = node_sync.comfyui_tag_change_note(cfg.get("comfyui_tag"), comfyui_tag)

    # 合并出完整 config(用于 deploy_env + 最终落盘)
    _base_cfg = dict(cfg)   # 部署开始时的快照,收尾写回时做三方合并(见 contract.merge_after_deploy)
    _deploy_updates = {
        "modal_endpoint_base": endpoint_base,
        "modal_app_name": app_name,
        "modal_workspace": workspace,
        "modal_volume_name": volume_name,
        "scaledown_window": scaledown,
        "default_gpu": default_gpu,
        # GPU 档位是运行时路由(改它本不必部署,前端切换时已即时存过);这里一并收下,
        # 只是让「部署」也能兜住一次,避免即时保存失败时前后端看到的档位不一致。
        "gpu_tier": (body.get("gpu_tier") or cfg.get("gpu_tier") or "auto").strip().lower(),
        "auto_downgrade": bool(body.get("auto_downgrade", cfg.get("auto_downgrade", True))),
        "comfyui_version": comfyui_version,
        "comfyui_tag": comfyui_tag,
        "modal_token_id": token_id,
        "modal_token_secret": token_secret,
        "bridge_api_key": bridge_key,
        "comfy_api_key": comfy_api_key,
        "hf_token": hf_token,
        "civitai_token": civitai_token,
        "aigc_studio_base_url": aigc_base_url,
        "aigc_bypass_secret": aigc_bypass,
    }
    cfg.update(_deploy_updates)
    env = node_sync.deploy_env(cfg)
    cwd = str(node_sync.MODAL_APP_DIR)

    await _emit(resp, "== Modal 一键部署 ==\n")
    await _emit(resp, f"   workspace={workspace}  app={app_name}\n")
    if _tag_note:
        await _emit(resp, f"   ⚠ {_tag_note}\n")
    await _emit(resp, f"   ComfyUI: 本机={comfyui_version or '未知'} → 云端 clone {comfyui_tag}\n")
    if _tag_change:
        await _emit(resp, f"   ⚠ {_tag_change}\n")
    await _emit(resp, f"   plugin_version={node_sync.plugin_version()}  (会烤进云端 deployed_version)\n")
    await _emit(resp, f"   endpoint={endpoint_base}\n\n")

    # 3) 部署 app(首次拉镜像 3-5 分钟):并回节点清单 → 推私有节点 → 算依赖 → Secret → modal deploy。
    #    Secret 挪到了私有节点推送之后、modal deploy 之前,见 3.1。
    node_sync.ensure_baked_file()  # 本地清单是 .gitignore 状态,缺则建空,免得 modal_image 打包炸
    # ⚠ 本机清单是被 gitignore 的本地状态,却会被当成镜像的全局清单去部署。插件被 Manager
    #   重装、清单丢了 → 上面建出一个空清单 → 这次部署清空云端全部节点;多机时另一台加的
    #   节点也会被删。部署前先把云端有、本机没有的并回来(只加不删)。
    try:
        _rec = await asyncio.to_thread(node_sync.reconcile_baked_with_cloud, cfg)
    except node_sync.DeployBlocked as _blk:
        await _emit_abort(resp, str(_blk))
        return 1, ()
    if _rec.added:
        await _emit(resp, f"   节点清单:云端有而本机清单缺的 {len(_rec.added)} 个已并回"
                          f"(不会被这次部署删掉)—— {', '.join(_rec.added)}\n")
    _drift = node_sync.drift_message(_rec)
    if _drift:
        await _emit(resp, _drift)
    await _emit(resp, "\n== 推送到云端:比对本机与云端的差异,只推有变化的部分 ==\n")
    # 3.0) 先把**本机**的私有节点推上 Volume,再去读 manifest。
    #
    # 用户点「部署」的心智模型是"把我现在的状态推上去"。而依赖清单以 Volume 的
    # manifest 为准(多机场景下这是对的),于是「本地改了某个私有节点的
    # requirements → 点部署」会用**旧** manifest 构建、照样失败,而失败信息是
    # 一个 Python 包的 traceback,跟"我该点哪个按钮"看不出任何关系。
    # 2026-08-31 实测:用户就这么白等了一轮构建 —— 0.8.15 加的「同步本机私有节点」
    # 按钮功能是对的,但**入口存在 ≠ 用户知道要用它**。所以这里不做提示、不加勾选框,
    # 直接在部署流程里先同步一次:没变化时 plan_local_uploads 判 uptodate、零开销。
    # 「同步」按钮保留 —— 用于"只想推节点、不想重部署"和版本互锁那两种场景。
    try:
        _vol_folders = await asyncio.to_thread(local_nodes.list_volume_local_nodes, cfg, 0)
    except local_nodes.VolumeUnavailable as _vu:
        # 契约 C13:读不到 ≠ 没有。以前当成「云端没有私有节点」跳过推送,随后又算出空依赖,
        # 部署出一个不含任何私有节点依赖的镜像,rc=0(2026-10-05 深度 review)。
        await _emit_abort(resp, f"读不到 Volume 上的私有节点名单({_one_line(_vu)}),"
                                f"没法确认私有节点和它们的依赖;稍后重试")
        return 1, ()
    try:
        _root = Path(node_sync._comfyui_root()) / "custom_nodes"
        # 只推本机也有的:多机场景下别的机器传的节点,这台机器没有源码,跳过即可
        # (它们的 manifest 已在 Volume 上,_refresh_local_node_reqs 照样读得到)。
        _present = [f for f in _vol_folders if (_root / f).is_dir()]
        if not _vol_folders:
            await _emit(resp, "   私有节点:云端没有,跳过\n")
        elif not _present:
            await _emit(resp, f"   私有节点:云端有 {len(_vol_folders)} 个,但本机都没有对应目录"
                              f"(多机场景,由拥有源码的那台推送)\n")
        else:
            # ⚠ 上传必须与 /sync_local_nodes 争同一把锁:两个 tab、远程客户端、
            # 或 RunModal 的自动同步与这里并发时,会同时覆盖同一个
            # .zip/.digest/.requirements.json,随后读到的 manifest 未必对应
            # 自己刚上传的代码,local_node_reqs_deployed_hash 也会与 Volume 错位。
            if _UPLOAD_LOCK.locked():
                await _emit(resp, "   另有节点上传进行中,排队等待…\n")
            async with _UPLOAD_LOCK:
                _plan = await asyncio.to_thread(local_nodes.plan_local_uploads,
                                                cfg, _present, _root)
                # 打包阶段就失败的(目录空/读不了)必须当场报错。以前只取 upload、
                # 把 failed 整个丢掉,于是坏包被静默跳过、后面照样报"已推送"。
                _pfail = _plan.get("failed") or []
                if _pfail:
                    _d = "; ".join(f"{f.get('folder')}: {f.get('error')}" for f in _pfail)
                    raise RuntimeError(f"私有节点打包失败 —— {_d}")
                _todo = [u["folder"] for u in _plan.get("upload", [])]
                if _todo:
                    await _emit(resp, f"   私有节点:{len(_todo)}/{len(_present)} 个有改动,"
                                      f"正在推送 —— {', '.join(_todo)}\n")
                    _ures = await asyncio.to_thread(local_nodes.upload_local_nodes,
                                                    cfg, _todo, _root)
                    local_nodes.invalidate_list_cache()
                    # 同上:upload 的返回值以前直接丢弃,任何一个包传失败都无人知晓。
                    _ufail = (_ures or {}).get("failed") or []
                    if _ufail:
                        _d = "; ".join(str(f) for f in _ufail)
                        raise RuntimeError(f"私有节点上传失败 —— {_d}")
                    await _emit(resp, f"   ✓ 已推送 {len(_todo)} 个(代码走 Volume 秒级生效;"
                                      f"若其 requirements 也变了,下面会重建依赖层)\n")
                else:
                    # 没改动也要出声 —— 静默会让用户以为"这一步没跑",转头又去找别的按钮
                    await _emit(resp, f"   私有节点:{len(_present)} 个,与云端一致,无需推送\n")
    except Exception as _e:
        # ⚠ fail-closed:同步失败就**停止本次推送**,不能沿用旧 manifest 继续。
        # 以前这里只打一句 warning 就往下走,最终仍返回 rc=0、前端显示"已推送到云端",
        # 而云端跑的可能还是旧代码、旧 requirements —— 这正是本插件反复强调要避免的
        # "静默成功"。/sync_local_nodes 一直是任意节点失败即失败,统一入口后更该一致。
        await _emit(resp, f"\n== ✗ 私有节点推送失败:{_e} ==\n")
        await _emit(resp, "   已停止本次推送 —— 继续下去云端会用旧代码/旧依赖构建,"
                          "那种「成功」比失败更难查。\n")
        return 1, ()

    # 私有节点依赖来自 Volume 中每个包的 manifest,而不是只扫当前机器；这样多机
    # 上传的私有节点在任意一台机器重部署时都不会掉依赖。
    try:
        _local_reqs = await asyncio.to_thread(_refresh_local_node_reqs, cfg)
    except local_nodes.VolumeUnavailable as _vu:
        await _emit_abort(resp, f"读不到 Volume 上的私有节点名单({_one_line(_vu)}),"
                                f"算不出镜像要装的依赖;稍后重试")
        return 1, ()
    _local_reqs_hash = node_sync.local_node_reqs_hash(_local_reqs)
    if _local_reqs:
        await _emit(resp, f"   私有节点依赖:{len(_local_reqs)} 条(镜像 build 期安装)\n")
    # 云端模型目录跟随本机:生成 extra_model_paths.yaml(覆盖自定义类别如 geometry_estimation)
    _mtypes = node_sync.write_extra_model_paths()
    _custom_mtypes = [t for t in _mtypes if t not in node_sync.STANDARD_MODEL_TYPES]
    await _emit(resp, f"   云端模型目录类型:{len(_mtypes)} 个"
                      f"(自定义 {len(_custom_mtypes)}:{', '.join(_custom_mtypes) or '无'})\n")

    # 3.1) 建 / 更新 secret(契约 C16,2026-10-05 深度 review):放在并回 / 私有节点推送**之后**、
    #    modal deploy 之前 —— 以前排在最前面,后面任何一步中止,都会留下一个已经换掉、却没有
    #    对应部署的 Secret。新生成的 bridge key 先落 config 再写 Secret:写 Secret 后才中止的话,
    #    config 里至少有这把 key,下次部署沿用它,不会出现一把谁都没记下的 key。
    if _new_key:
        _cur = cfg_mod.load_config()
        if _cur.get("bridge_api_key"):
            bridge_key = _cur["bridge_api_key"]     # 别处(CLI)刚写了一把:沿用,别换锁
        else:
            _cur["bridge_api_key"] = bridge_key
            cfg_mod.save_config(_cur)
        _deploy_updates["bridge_api_key"] = cfg["bridge_api_key"] = bridge_key
    await _emit(resp, "\n== 创建 Modal Secret ==\n")
    rc = await _run_streamed(
        resp, node_sync.secret_create_cmd(cfg, hf_token, civitai_token, bridge_key,
                                          comfy_api_key, aigc_base_url, aigc_bypass),
        cwd=cwd, env=env,
    )
    if rc != 0:
        await _emit(resp, "== ✗ secret 创建失败(token 可能无效)==\n")
        return rc, ()

    # 3.2) modal deploy
    await _emit(resp, "\n== modal deploy(首次拉镜像约 3-5 分钟,别关窗口)==\n")
    rc = await _run_streamed(resp, node_sync.deploy_command(), cwd=cwd, env=env)
    if rc != 0:
        await _emit(resp, "== ✗ modal deploy 失败 ==\n")
        return rc, ()

    # 4) 写本地 config(在 ComfyUI 进程里,路径用 folder_paths,必对)
    # ⚠ 不能整份写回 cfg:那是几分钟前的快照,会把部署期间别处的改动冲掉。三方合并。
    _final = contract.merge_after_deploy(_base_cfg, _deploy_updates, cfg_mod.load_config())
    _final["local_node_reqs_deployed_hash"] = _local_reqs_hash
    cfg_mod.save_config(_final)
    await asyncio.to_thread(modal_volume.record_deployed_reqs, _final, _local_reqs)
    await _emit(resp, f"\n== ✓ config 已写入(endpoint={endpoint_base})==\n")
    return 0, (_final, endpoint_base, cwd, env)

async def _deploy_verify(resp: web.StreamResponse, cfg: dict, endpoint_base: str,
                         cwd: str, env: dict) -> int:
    """/deploy 收尾(锁外即可):验证 /health,再跑节点兼容性检测。返回 rc。"""
    # 5) 验证 health。⚠ 404 = 拼出来的 endpoint 不存在:modal deploy 是按 token 部署到真实
    #    workspace 的,URL 却是按用户填的 workspace 拼的 —— 填错(或多环境少了后缀)时部署
    #    「成功」、之后每个请求都 404。以前这里只给一句「暂不可达」的警告、rc=0,前端报部署成功
    #    (2026-10-05 深度 review)。刚部署完 endpoint 偶尔要几秒才生效,404 先重试两次。
    _not_found = None
    for _attempt in range(_HEALTH_404_TRIES):
        try:
            async with aiohttp.ClientSession() as s:
                h = await modal_client.health(s, cfg)
            await _emit(resp, f"== ✓ /health: {h} ==\n")
            _not_found = None
            break
        except Exception as e:
            if getattr(e, "kind", None) == "not_deployed":
                _not_found = e
                if _attempt + 1 < _HEALTH_404_TRIES:
                    await asyncio.sleep(_HEALTH_404_RETRY_S)
                continue
            await _emit(resp, f"== ⚠ /health 暂不可达(endpoint 可能还在初始化,稍后重试):{e} ==\n")
            break
    if _not_found is not None:
        _url = modal_client._endpoint(endpoint_base, "health")
        print(f"[modal_bridge] ✗ 部署完成但 {_url} 返回 404: {_not_found}")
        await _emit(resp, f"== ✗ 部署完成,但 {_url} 返回 404 —— 按填的 workspace 拼出的地址不存在,"
                          f"workspace 多半填错了(多环境要带环境后缀,如 myws-dev;以上面 modal deploy "
                          f"日志里 web function 的地址为准),改正后重新部署 ==\n")
        return 1

    # 6) 自定义节点兼容性检测(隔离 app,同镜像 boot 一次 ComfyUI,报每个节点导入成功/失败)。
    #    只警告不阻断:坏节点不影响其它工作流,部署照样 rc=0。
    await _emit(resp, "\n== 自定义节点兼容性检测(云端同镜像 boot 一次 ComfyUI,约 1 分钟)==\n")
    try:
        crc = await _run_streamed(resp, node_sync.node_compat_check_command(), cwd=cwd, env=env)
        if crc != 0:
            await _emit(resp, "== ⚠ 兼容性检测未跑完(不影响部署);可稍后手动 `modal run node_compat_check.py` ==\n")
    except Exception as e:
        await _emit(resp, f"== ⚠ 兼容性检测启动失败(忽略):{e} ==\n")
    return 0


def _setup_routes():
    # 这个函数被 module 末尾立即调用,而不是 import-time(避免循环)
    from server import PromptServer  # type: ignore

    routes = PromptServer.instance.routes

    # -------- 配置读写 --------
    @routes.get("/modal_bridge/config")
    async def _get_config(request: web.Request):
        # 不把密钥送到浏览器:抹掉 token_secret 和 bridge_api_key,只给前端要的非敏感字段
        # + 一个 has_token_secret 标志(部署框据此显示"已保存,留空=沿用")。
        try:
            return web.json_response(contract.public_config(cfg_mod.load_config()))
        except cfg_mod.ConfigCorrupt as e:
            return _config_corrupt_response(e)

    # /modal_bridge/bridge_key 已删除(契约 C9,2026-10-05 深度 review):唯一的调用方是早已从界面
    # 摘掉的「导出脚本」,那段脚本本身有多处 P1/P2,整段删掉;留着这个路由只是多一个回吐 key 的入口。

    @routes.post("/modal_bridge/config")
    @_admin_only
    async def _set_config(request: web.Request):
        body = await _json_object(request)
        try:
            cur = contract.merge_public_config(cfg_mod.load_config(), body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        cfg_mod.save_config(cur)
        return web.json_response(contract.public_config(cur))  # 与 GET /config 同一份脱敏

    # -------- 异步提交(返回 job_id,不阻塞)--------
    @routes.post("/modal_bridge/submit")
    @_admin_only
    async def _submit(request: web.Request):
        body = await _json_object(request)
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt (object) required"}, status=400)

        cfg = cfg_mod.load_config()
        tier = (_opt_str(body, "tier") or "40g").lower()
        # 是否需要 GPU。三条判据,任一成立就给 GPU:
        #   1) 用户**显式选了档位**(非 auto)—— 那是明确表达"我要这张卡",不该再被
        #      "扫不到模型"这种负向推断推翻。以前只要扫不到模型,选了 H100 也照样进 CPU worker。
        #   2) 工作流里扫到了本地模型。
        #   3) 关掉了 cpu_tier_when_no_model —— 默认 True(维持既有账单),但那条推断不可靠:
        #      节点内部下载权重、无模型文件的 CUDA/Triton 图像处理与 3D/光流节点、
        #      模型参数不是文件名字符串的节点,都扫不到却真要 GPU。误判代价不对称:
        #      给错 CPU = 跑不动耗到超时、白烧钱零产出。
        _tier_sel = resolve_gpu_tier(cfg)

        def _route_gpu():
            # 模型扫描与自动选档都会碰文件系统(解析不到时 rglob 整个模型目录,外置盘上能到秒级),
            # 以前直接跑在事件循环里,期间整个 ComfyUI 卡住(2026-10-05 深度 review)。放进线程,
            # 且 required 只算一次往下传(以前 needs_gpu 与选档各算一遍)。
            # 显式选档时短路,不扫描 —— 那时 required 用不上。
            required = None
            needs_gpu = (
                _tier_sel != "auto"
                or bool(required := extract_required_models(prompt))
                or not cfg.get("cpu_tier_when_no_model", True)
            )
            # 需要 GPU 时再按估算显存自动选档:放得下便宜卡 → cheap(L40S),否则 primary(H100)。
            if not needs_gpu:
                return False, "primary", ""
            return (True, *_pick_gpu_class(prompt, cfg, required=required))

        needs_gpu, gpu_class, gpu_reason = await asyncio.to_thread(_route_gpu)
        if needs_gpu:
            print(f"[modal_bridge] GPU 路由: {gpu_class}  ({gpu_reason})")

        try:
            image_names = _extract_input_image_names(prompt)
            # 读盘 + base64 放线程里:参考视频动辄几十 MB,在事件循环里做会冻结整个 ComfyUI
            input_images = await asyncio.to_thread(
                lambda: [_read_input_as_b64(n) for n in image_names])
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            return web.json_response({"error": f"prepare images failed: {e}"}, status=500)

        if input_images:
            sizes = sum(len(im["image"]) for im in input_images)
            print(f"[modal_bridge] uploading {len(input_images)} input image(s), ~{sizes//1024} KB total")

        # 自写节点的期望版本随任务发过去:解压只发生在容器启动时,暖容器可能装着上一版
        # (改完节点立刻重跑最容易撞上)→ worker 据此 reload+重装+重启,不会静默出旧结果。
        # 优先用调用方带来的(前端刚同步完、由 sync_local_nodes 回传的**Volume 真实版本**);
        # 没带才现算 —— 现算的风险是同步与提交之间文件又变了,声明一个云端不存在的版本。
        local_digests = body.get("local_nodes") if isinstance(body.get("local_nodes"), dict) else None
        if local_digests is None:
            try:
                plan = await asyncio.to_thread(node_sync.plan_node_sync, prompt)   # 每个节点目录跑 git
                # 没走前端预检的调用方也必须声明 baked 期望,否则历史本地覆盖包会在暖容器里
                # 永久存活。digest 与 sentinel 共用一个 map,worker 能统一做版本闸门。
                local_digests = {
                    folder: local_nodes.BAKED_SENTINEL
                    for folder in plan.get("expect_baked", [])
                }
                folders = [p["folder"] for p in plan.get("local_pack", [])]
                if folders:
                    local_digests.update(await asyncio.to_thread(   # 对整个节点目录做哈希
                        local_nodes.expected_digests,
                        folders, Path(node_sync._comfyui_root()) / "custom_nodes"))
            except Exception as e:
                print(f"[modal_bridge] 本地节点指纹计算跳过: {e}")

        try:
            # ClientTimeout 是**每个请求**的上限(submit_job 自己也给每次 POST 传 60s),不是整个
            # 提交的上限:submit_job 的多次重试 + 退避(最坏几分钟)不会被这里截断(深度 review 核对)。
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                submit_result = await modal_client.submit_job(
                    session, cfg, workflow=prompt,
                    input_images=input_images or None, tier=tier, needs_gpu=needs_gpu,
                    gpu_class=gpu_class, local_nodes=local_digests or None,
                )
        except Exception as e:
            # 契约 C3:重试全部失败时 submit_job 抛 SubmitUnknown(带 .job_id)—— 任务**可能已经在
            # 云端跑了**(响应丢在网关)。回 job_id + outcome=unknown,前端按这个 id 进入正常轮询
            # 核实,而不是当成提交失败让用户再点一次、双跑双计费。
            unknown_id = getattr(e, "job_id", None)
            if unknown_id:
                print(f"[modal_bridge] ⚠ 提交结果未知 job_id={unknown_id}: {e}")
                return web.json_response({"error": str(e), "job_id": unknown_id,
                                          "outcome": "unknown"}, status=502)
            return web.json_response({"error": str(e)}, status=502)

        job_id = submit_result.get("id")
        gpu = submit_result.get("gpu") or tier
        print(f"[modal_bridge] submitted job {job_id} (needs_gpu={needs_gpu}, gpu={gpu}, refs={len(input_images)})")
        return web.json_response({
            "ok": True,
            "job_id": job_id,
            "gpu": gpu,
            "input_image_count": len(input_images),
            # 前端等待窗自动跟上云端超时用(见 modal_bridge.js poll deadline):
            # 云端 worker 上限 = 部署时的 cfg.worker_timeout_sec(node_sync 注入 MODAL_BRIDGE_TIMEOUT)。
            # 若用户改了 cfg 还没重新部署,这里会与云端短暂不一致 —— 偏大无害(worker 先死,
            # poll 拿到失败态提前结束),偏小则被前端设置项的 max() 兜住。
            "worker_timeout_sec": int(cfg.get("worker_timeout_sec", 1200)),
        })

    # -------- 轮询单次状态(前端高频调用,显示进度)--------
    @routes.get("/modal_bridge/poll")
    @_admin_only
    async def _poll(request: web.Request):
        job_id = request.query.get("job_id")
        if not job_id:
            return web.json_response({"error": "job_id required"}, status=400)

        cfg = cfg_mod.load_config()
        url = modal_client._endpoint(cfg["modal_endpoint_base"], "status")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(url, params={"job_id": job_id}, allow_redirects=False,
                                       headers={"X-Bridge-Key": modal_client._key(cfg)}) as r:
                    # 契约 C4(2026-10-05 深度 review):以前不看状态码,云端 401 的 {"error"} 原样
                    # 当状态透传,没有 status 字段,前端只能兜底成「未知」一直轮询到超时;网关 5xx 的
                    # HTML 则直接解析失败。现在:401 = auth_failed(终态,key 不匹配);其它非 2xx =
                    # unknown(瞬态,前端不计入 not_found)。都回 HTTP 200,让前端按 status 分支。
                    if r.status == 401:
                        data = {"status": "auth_failed",
                                "error": await _cloud_error_text(r) or "bridge key 与云端不一致"}
                    elif r.status >= 300:
                        data = {"status": "unknown", "http_status": r.status,
                                "error": await _cloud_error_text(r) or f"云端 /status 返回 HTTP {r.status}"}
                    else:
                        data = await r.json(content_type=None)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        # 只在 status 变化时记日志(poll 高频,避免刷屏);终态 failed 把 error 也记上。
        # 这样即使前端超时/放弃,ComfyUI 日志里也能看到 job 走到了哪一步、为何失败。
        st = data.get("status") if isinstance(data, dict) else None
        if st and _LAST_POLL_STATUS.get(job_id) != st:
            if len(_LAST_POLL_STATUS) >= _LAST_POLL_MAX:
                for _old in list(_LAST_POLL_STATUS)[: _LAST_POLL_MAX // 5]:  # dict 有序,删最早的一批
                    _LAST_POLL_STATUS.pop(_old, None)
            _LAST_POLL_STATUS[job_id] = st
            if st == "failed":
                print(f"[modal_bridge] ⚠ job {job_id} FAILED: {(data.get('error') or '')[:300]}")
            else:
                print(f"[modal_bridge] job {job_id} → {st}")
            if st in ("completed", "failed", "cancelled"):
                _LAST_POLL_STATUS.pop(job_id, None)  # 终态后清掉,不留内存
        return web.json_response(data)

    # -------- 前端上报 job 客户端侧结局(超时/取消/错误)→ 记进后端日志 --------
    @routes.post("/modal_bridge/job_event")
    @_admin_only
    async def _job_event(request: web.Request):
        """前端在 job 出现客户端侧结局(Polling timed out / 用户取消 / 出错)时调,
        让 ComfyUI 后端日志留痕——否则这些只在浏览器,后端无记录(用户反馈'报错没进 log')。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "请求体必须是 JSON 对象"}, status=400)
        # 只是记日志:字段类型不对就转成字符串记下来,不值得为它回 500(深度 review)。
        job_id = str(body.get("job_id") or "?")[:80]
        event = str(body.get("event") or "unknown")[:80]
        detail = str(body.get("detail") or "")[:300]
        print(f"[modal_bridge] ⚠ 前端上报 job {job_id}: {event} {('— ' + detail) if detail else ''}")
        return web.json_response({"ok": True})

    # -------- 拉结果(完成后调,写文件 + 返回 outputs)--------
    @routes.get("/modal_bridge/fetch_progress")
    @_admin_only
    async def _fetch_progress(request: web.Request):
        """取回进度。/fetch_result 是一次阻塞 POST,大产物下载几十分钟期间前端只能靠它
        知道"在动"。没有记录就回 {ok:false} —— 前端据此显示静态文案,不当错误。"""
        job_id = request.query.get("job_id") or ""
        rec = _FETCH_PROGRESS.get(job_id)
        if not rec:
            return web.json_response({"ok": False})
        return web.json_response({"ok": True, **rec})

    @routes.post("/modal_bridge/fetch_result")
    @_admin_only
    async def _fetch_result(request: web.Request):
        body = await _json_object(request)
        job_id = body.get("job_id")
        final = body.get("modal_state")  # 前端 poll 拿到的最终状态对象
        if not job_id or not isinstance(final, dict):
            return web.json_response({"error": "job_id + modal_state required"}, status=400)
        if not contract.is_safe_job_id(job_id):   # 见 contract.is_safe_job_id 的注释
            return web.json_response({"error": "bad job_id"}, status=400)

        cfg = cfg_mod.load_config()
        subfolder = cfg.get("output_subfolder", "modal_results")
        signature = hashlib.sha256(json.dumps([
            final, subfolder, cfg.get("modal_endpoint_base"), cfg.get("modal_volume_name"),
        ], sort_keys=True).encode()).hexdigest()
        existing = _FETCH_TASKS.get(job_id)
        if existing and existing[0] != signature:
            return web.json_response({"error": "job is being fetched with different parameters"}, status=409)
        if existing:
            task = existing[1]
        else:
            if len(_FETCH_TASKS) >= _FETCH_PROGRESS_MAX:
                return web.json_response({"error": "too many active downloads"}, status=429)
            task = asyncio.create_task(_fetch_job(final, job_id, subfolder, cfg))
            _FETCH_TASKS[job_id] = (signature, task)
            task.add_done_callback(functools.partial(_fetch_finished, job_id))
        try:
            # 浏览器刷新不取消底层线程；下一请求复用同一任务，不并发改写 .part。
            outputs = await asyncio.shield(task)
        except Exception as e:
            return web.json_response({"error": f"write result failed: {e}"}, status=502)
        if not outputs:
            return web.json_response({"error": "no image in modal_state"}, status=502)

        print(f"[modal_bridge] ✓ job {job_id} fetched {len(outputs)} img → {subfolder}/{job_id}/")
        return web.json_response({"ok": True, "job_id": job_id, "outputs": outputs})

    # -------- 模型同步(本地 → Volume,全程本地 modal SDK,不经 endpoint)--------

    @routes.post("/modal_bridge/check_models")
    @_admin_only
    async def _check_models(request: web.Request):
        """
        查工作流要的模型 Volume 有没有 / 本地能不能补(本地 SDK 直查 Volume)。
        body: {prompt}
        返回: {required, present, missing_local[], missing_no_source[]}
        """
        body = await _json_object(request)
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)

        required = await asyncio.to_thread(extract_required_models, prompt)   # 会查模型目录
        if not required:
            return web.json_response(
                {"required": [], "present": [], "missing_local": [],
                 "downloading": [], "missing_no_source": []})

        if not await asyncio.to_thread(modal_volume.modal_importable):   # 首次 import modal 要 1 秒级
            return web.json_response(
                {"error": "本地没装 modal。插件依赖由 ComfyUI Manager 在安装时装 —— "
                          "手动 clone 进 custom_nodes 的话,在 ComfyUI 用的那个 Python 里 "
                          "安装 modal 后重启;也可以在 Manager 里卸载后重装本插件。"}, status=400)

        cfg = cfg_mod.load_config()
        resolver = _local_model_resolver()
        try:
            result = await asyncio.to_thread(modal_volume.check_models, cfg, required, resolver)
        except Exception as e:
            return web.json_response({"error": f"check_models(SDK) failed: {e}"}, status=502)
        return web.json_response(result)

    def _node_required_inputs(class_type: str):
        """从 ComfyUI 当前加载的节点定义拿必填输入名集合;拿不到返回 None(跳过,不误报)。
        v3 schema 节点由 ComfyUI 兼容层照样提供经典 INPUT_TYPES()。"""
        try:
            import nodes  # ComfyUI 全局
            cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
            if cls is None:
                return None
            it = cls.INPUT_TYPES()
            if not isinstance(it, dict):
                return None
            req = it.get("required") or {}
            return set(req.keys()) if isinstance(req, dict) else None
        except Exception:
            return None

    def _node_is_output(class_type: str):
        """该节点类是否 OUTPUT_NODE(SaveImage / SaveVideo / PreviewImage …)。
        用于把预检范围收敛到「输出节点的依赖闭包」—— ComfyUI 只执行这部分
        (execution.py 从 OUTPUT_NODE 递归 validate_inputs),画布上输出悬空的节点
        根本不参与执行,不该被预检拦下。拿不到定义返回 None(调用方退回全量检查)。"""
        try:
            import nodes  # ComfyUI 全局
            cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
            if cls is None:
                return None
            return getattr(cls, "OUTPUT_NODE", False) is True
        except Exception:
            return None

    @routes.post("/modal_bridge/check_required_inputs")
    @_admin_only
    async def _check_required_inputs(request: web.Request):
        """提交前预检:按当前本地节点定义,找出 prompt 里「缺必填输入」的节点。
        body: {prompt}  返回: {missing:[{node_id,class_type,missing:[...]}]}
        典型拦截:老工作流缺新版节点新增的必填 widget(如 API 节点 generate_type),
        避免等云端 `execute() missing required argument` 才报错。拿不到定义的节点跳过,不误报。
        只检查输出节点的依赖闭包,与 ComfyUI 的执行范围一致(悬空节点不拦)。"""
        body = await _json_object(request)
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)
        missing = workflow_check.find_missing_required_inputs(
            prompt, _node_required_inputs, _node_is_output)
        return web.json_response({"missing": missing})

    @routes.post("/modal_bridge/estimate_vram")
    @_admin_only
    async def _estimate_vram(request: web.Request):
        """估工作流要加载的模型本地总大小(MB),供前端 ×1.3 对比所选显卡做显存预警。
        body: {prompt}
        返回: {total_mb, known_count, required_count, unknown:[本地查不到的模型]}
        粗估:仅按模型文件大小求和,不含激活/reference;本地缺的模型计 unknown、不入 total
        (前端据此提示"估算可能偏低")。"""
        body = await _json_object(request)
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)
        resolver = _local_model_resolver()

        def _sizes():
            # 放线程里:解析器找不到时会递归 rglob 整个模型目录(Desktop 按子目录归类模型时),
            # 在事件循环里做会冻结整个 ComfyUI。extract_required_models 同理(查各模型目录)。
            required = extract_required_models(prompt)
            total, largest, kn, unk = 0, 0, 0, []
            for m in required:
                p = resolver(m["type"], m["filename"])
                try:
                    if p and Path(p).exists():
                        sz = Path(p).stat().st_size
                        total += sz
                        largest = max(largest, sz)
                        kn += 1
                    else:
                        unk.append(f"{m['type']}/{m['filename']}")
                except OSError:
                    unk.append(f"{m['type']}/{m['filename']}")
            return required, total, largest, kn, unk

        required, total_bytes, largest_bytes, known, unknown = await asyncio.to_thread(_sizes)
        # 按类别估显存。视频优先激活公式(最大模型常驻 + W×H×帧数,实测校准,见 categories.py);
        # 工作流里抠不出尺寸字面量时回退旧的「权重总和×系数」保守公式(basis 标明用的哪个)。
        category = categories.classify(prompt)
        est, basis = None, "legacy"
        if category == "video" and largest_bytes:
            pixels, frames = categories.extract_pixels_frames(prompt)
            if pixels and frames:
                est = categories.estimate_vram_video_gb(largest_bytes / (1024 ** 3), pixels, frames)
                basis = "activation"
        if est is None:
            est = categories.estimate_vram_gb(total_bytes / (1024 ** 3), category)
        return web.json_response({
            "total_mb": total_bytes // 1024 // 1024,
            "known_count": known,
            "required_count": len(required),
            "unknown": unknown,
            "category": category,
            "est_vram_gb": round(est, 1),
            "est_basis": basis,
        })

    @routes.post("/modal_bridge/sync_models")
    @_admin_only
    async def _sync_models(request: web.Request):
        """
        把本地有、Volume 没有的模型上传到 Volume(batch_upload,CAS 去重)。stream 回传进度。
        body: {items: [{type, filename, local_path}]}  (前端从 check_models 的 missing_local 拿)
        最后一行: __DEPLOY_DONE__ rc=<code>。有任何一项被拒(本地解析不到 / 路径不合法 / 还在下载)
        rc 就非 0;Volume 上已存在而跳过的不算失败。倒数第二行是汇总。
        """
        body = await _json_object(request)
        items = body.get("items")
        if not isinstance(items, list) or not items:
            return web.json_response({"error": "items (non-empty list) required"}, status=400)
        cfg = cfg_mod.load_config()   # prepare 之前读:config 损坏时回 500 JSON,而不是半截的流

        async def run(resp: web.StreamResponse) -> int:
            if not await asyncio.to_thread(modal_volume.modal_importable):
                await _emit(resp, "✗ 本地没装 modal,无法上传\n")
                return 1

            # ⚠ 不信请求体里的 local_path —— 用服务端带路径囚笼的解析器重新定位。以前原样交给
            #   upload_models(只查 is_file),发一个 local_path="/Users/me/.ssh/id_ed25519" 就能把
            #   任意本地文件传上 Volume,find_local_model / is_path_within_roots 那道囚笼等于白做
            #   (2026-09-23 review)。正常流程里这个值本来就是 /check_models 用同一个解析器算出来的,
            #   重新解析结果一致;解析不到的一律拒。远端路径那一半由 modal_volume.model_relpath 把关。
            resolver = _local_model_resolver()

            def _reresolve():
                ok, bad = [], []
                for it in items:
                    # 类型不对的项按「被拒」处理,别让 resolver 在 prepare 之后抛 TypeError 截断流
                    # (深度 review:{"type": null, "filename": null} 就能触发)。
                    if not isinstance(it, dict) or not isinstance(it.get("type"), str) \
                            or not isinstance(it.get("filename"), str) or not it["filename"].strip():
                        bad.append((repr(it)[:80], "type / filename 必须是非空字符串"))
                        continue
                    p = resolver(it["type"], it["filename"])
                    if p is None:
                        bad.append((f"{it['type']}/{it['filename']}", "本地找不到(或不在模型目录内)"))
                        continue
                    ok.append({**it, "local_path": str(p)})
                return ok, bad

            todo, rejected = await asyncio.to_thread(_reresolve)
            for name, why in rejected:
                await _emit(resp, f"  ✗ {why},跳过:{name}\n")

            uploaded, existed = [], []
            if todo:
                total_mb = sum(int(it.get("size_mb") or 0) if isinstance(it.get("size_mb"), (int, float))
                               else 0 for it in todo)
                await _emit(resp, f"== 上传 {len(todo)} 个模型到 Volume(共 ~{total_mb} MB)==\n")
                await _emit(resp, "== Modal Volume 块级去重:网上通用大模型秒过,只有新内容真正占上行带宽 ==\n\n")

                def do_upload(emit):
                    def on_progress(ev):
                        if ev["phase"] == "begin":
                            emit(f"  ↑ 开始上传 {ev['count']} 个文件,共 ~{ev['total_mb']} MB(并行传,传完才有结果):\n")
                            for f in ev["files"]:
                                emit(f"      {f['name']} ({f['size_mb']} MB)\n")
                        else:  # end
                            emit(f"  ✓ {ev['count']} 个文件上传完成,共 ~{ev['total_mb']} MB / "
                                 f"{ev['secs']}s(均速 {ev['rate_mbps']} MB/s)\n")
                    return modal_volume.upload_models(cfg, todo, on_progress=on_progress)

                # 串行化:有别的上传在跑就排队等(上传前会复查 Volume,等到时多半已有、直接跳过)
                if _UPLOAD_LOCK.locked():
                    await _emit(resp, "== 另有模型上传进行中,排队等待(同一时刻只传一个,避免并发撞车)…\n\n")
                try:
                    async with _UPLOAD_LOCK:
                        result = await _run_blocking_streamed(resp, do_upload)
                except Exception as e:
                    tb = traceback.format_exc()
                    print(f"[modal_bridge] sync_models 上传失败: {e}\n{tb}")  # 进 ComfyUI 控制台日志
                    await _emit(resp, f"\n✗ 上传失败: {e}\n{tb[-800:]}\n")
                    return 1
                uploaded = result.get("uploaded") or []
                for sk in result.get("skipped") or []:
                    name = f"{sk.get('type')}/{sk.get('filename')}"
                    if sk.get("reason") == "already in volume":
                        existed.append(name)
                    else:
                        rejected.append((name, sk.get("reason") or "未知原因"))
                        await _emit(resp, f"  ✗ {sk.get('reason')},没上传:{name}\n")

            # F-P3-9:以前被拒的项只打一行 ✗、rc 仍是 0,前端当成「模型都齐了」去提交,
            # 到云端才报缺模型。已存在而跳过的不算失败。最后一行汇总写清三类各几个。
            rc = 1 if rejected else 0
            summary = (f"{len(uploaded)} 个已同步、{len(existed)} 个已存在跳过、"
                       f"{len(rejected)} 个被拒")
            if rejected:
                shown = "; ".join(f"{n}({why})" for n, why in rejected[:5])
                summary += f":{shown}" + (f" 等 {len(rejected)} 个" if len(rejected) > 5 else "")
            await _emit(resp, f"\n== {'✓' if rc == 0 else '✗'} 模型同步:{summary} ==\n")
            return rc

        return await _stream_run(request, "模型同步", run)

    @routes.post("/modal_bridge/sync_local_nodes")
    @_admin_only
    async def _sync_local_nodes(request: web.Request):
        """
        本地自写 custom_node(无 git remote / commit 未推送)打包上传 Volume。stream 回传进度。
        worker 启动时会解压进 /comfyui/custom_nodes/；纯代码变化不重部署，依赖变化自动部署。
        body: {folders: ["my_node", ...]}  (前端从 check_nodes 的 local_pack 拿)
        最后一行: __DEPLOY_DONE__ rc=<code>
        """
        body = await _json_object(request)
        folders = body.get("folders")
        if not isinstance(folders, list) or not folders:
            return web.json_response({"error": "folders (non-empty list) required"}, status=400)
        # 入口即校验:folders 直接参与路径拼接,即使已有 admin capability 也不能省掉囚笼 ——
        # 越界的名字必须在这里挡住,别指望下游。local_nodes.safe_folder 还会再囚一次(纵深)。
        bad = [f for f in folders
               if not isinstance(f, str) or not f.strip()
               or "/" in f or "\\" in f or f.strip() in (".", "..")]
        if bad:
            return web.json_response({"error": f"folders 含非法项(须为单个目录名): {bad[:3]}"},
                                     status=400)
        cfg = cfg_mod.load_config()

        async def run(resp: web.StreamResponse) -> int:
            if not await asyncio.to_thread(modal_volume.modal_importable):
                await _emit(resp, "✗ 本地没装 modal,无法上传\n")
                return 1

            root = Path(node_sync._comfyui_root()) / "custom_nodes"
            await _emit(resp, f"== 打包上传 {len(folders)} 个本地节点到 Volume ==\n")
            await _emit(resp, "== 代码走 Volume 秒级生效；requirements 变化时只重建依赖层 ==\n\n")

            def do_upload(emit):
                plan = local_nodes.plan_local_uploads(cfg, folders, root)
                # digests:本次提交应当声明的版本 = Volume 上真实存在的那版
                # (已最新的取现存 digest,新传的取实际打进包的那个 —— 都不是提交时再扫一次目录)
                digests = {u["folder"]: u["digest"] for u in plan["uptodate"]}
                for u in plan["uptodate"]:
                    emit(f"  = {u['folder']} 云端已是最新,跳过\n")
                for f in plan["failed"]:
                    emit(f"  ✗ {f['folder']}: {f['error']}\n")
                todo = [u["folder"] for u in plan["upload"]]
                if not todo:
                    return {"uploaded": [], "failed": plan["failed"], "digests": digests}
                for u in plan["upload"]:
                    emit(f"  ↑ {u['folder']}({u['files']} 个文件,~{u['raw_mb']} MB)\n")
                r = local_nodes.upload_local_nodes(cfg, todo, root)
                r["failed"] = plan["failed"] + r.get("failed", [])
                digests.update({u["folder"]: u["digest"] for u in r.get("uploaded", [])})
                r["digests"] = digests
                return r

            if _UPLOAD_LOCK.locked():
                await _emit(resp, "== 另有上传进行中,排队等待…\n\n")
            try:
                async with _UPLOAD_LOCK:
                    result = await _run_blocking_streamed(resp, do_upload)
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[modal_bridge] sync_local_nodes 失败: {e}\n{tb}")
                await _emit(resp, f"\n✗ 上传失败: {e}\n{tb[-800:]}\n")
                return 1

            for u in result.get("uploaded", []):
                await _emit(resp, f"  ✓ {u['folder']} ({u['zip_kb']} KB, {u['files']} files)\n")
            failed = result.get("failed", [])
            # ⚠ 任何一个失败都算失败(不是"全失败才算"):工作流要的每个节点都是必需品,
            #   少一个云端就跑不起来。rc=0 会让前端当作全成功直接提交 → 白跑一趟云端。
            rc = 1 if failed else 0
            for f in failed:
                await _emit(resp, f"  ✗ {f['folder']}: {f['error']}\n")
            await _emit(resp, f"\n== {'✓' if rc == 0 else '⚠'} 本地节点同步完成:"
                              f"{len(result.get('uploaded', []))} 个上传,{len(failed)} 个失败 ==\n")

            # requirements 不能在 worker 启动时装(Registry 禁令)。每个节点随 zip 上传 manifest，
            # 这里汇总整个 Volume 的依赖；只有与最近成功部署的指纹不同时才重建镜像。
            if rc == 0:
                if _DEPLOY_LOCK.locked():
                    await _emit(resp, "== 另有部署进行中,等待后核对私有节点依赖… ==\n")
                async with _DEPLOY_LOCK:
                    try:
                        rc = await _auto_redeploy_for_local_reqs(resp)
                    except Exception as e:
                        rc = 1
                        print(f"[modal_bridge] ✗ 私有节点依赖同步失败:{e}\n{traceback.format_exc()}")
                        await _emit(resp, f"== ✗ 私有节点依赖同步失败:{_one_line(e)} ==\n")
            # 只有全部成功才给可提交的版本契约。失败时发空/部分 map 会让前端漏掉失败节点,
            # 暖容器反而可能继续跑它的旧版本。
            if rc == 0:
                await _emit(resp, f"__LOCAL_DIGESTS__ {json.dumps(result.get('digests') or {})}\n")
            return rc

        return await _stream_run(request, "本地节点同步", run)

    @routes.post("/modal_bridge/local_nodes_diff")
    @_admin_only
    async def _local_nodes_diff(request: web.Request):
        """这些私有节点里,哪些与 Volume 上的**内容真的不一样**。

        为什么需要:plan_node_sync 的 local_pack 只按"无 git remote / 未推送 / dirty"
        分类 —— 那是**通道选择**(走 Volume 而不是镜像),不是"有改动"。只要工作流含
        自写节点,它每次都非空。提交前若直接拿它去弹确认,用户每跑一次图都要点一次,
        而绝大多数时候云端和本机根本一致(codex review 抓到)。

        真正的差异要比对 digest,那需要读 Volume,所以单独一个端点、只在有私有节点时调。
        """
        body = await _json_object(request)
        folders = body.get("folders")
        if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
            return web.json_response({"error": "folders (string list) required"}, status=400)
        cfg = cfg_mod.load_config()
        root = Path(node_sync._comfyui_root()) / "custom_nodes"
        try:
            plan = await asyncio.to_thread(local_nodes.plan_local_uploads, cfg, folders, root)
        except Exception as e:
            # 查不出来就别拦路:退化成"当作有改动",最坏是多问一次
            return web.json_response({"upload": folders, "uptodate": [], "failed": [],
                                      "degraded": str(e)})
        # ⚠ 光比 Volume 内容不够(2026-09-02 codex 抓到):上次依赖镜像重建失败时,
        #    local_node_reqs_deployed_hash 没有推进,而 zip 内容是一致的 —— 于是这里报
        #    "全部一致"、前端不弹确认,可随后 sync_local_nodes 仍然满足
        #    target_hash != deployed_hash,**无确认地触发几分钟的镜像重建**。
        #    确认框存在的全部理由就是"别让一次点击悄悄变成几分钟",所以这个状态必须回报。
        try:
            _reqs = await asyncio.to_thread(_compute_local_node_reqs, cfg)   # 纯读,不落盘
            _target = node_sync.local_node_reqs_hash(_reqs)
            _deployed = await _deployed_reqs_hash(cfg)
            reqs_pending = _target != _deployed and bool(_reqs or _deployed)
        except Exception as e:
            # 同上:查不出来就别拦路,当作"要重建"多问一次,不会漏
            print(f"[modal_bridge] reqs pending 预检失败,按需重建处理: {e}")
            reqs_pending = True
        return web.json_response({
            "upload": [u["folder"] for u in plan.get("upload", [])],
            "uptodate": [u["folder"] for u in plan.get("uptodate", [])],
            "failed": plan.get("failed", []),
            "reqs_redeploy_pending": reqs_pending,
        })

    @routes.get("/modal_bridge/list_local_nodes")
    @_admin_only
    async def _list_local_nodes(request: web.Request):
        """Volume 上现有的本地节点包名单(「管理云端节点」面板用)。返回 {ok, nodes:[name]}"""
        cfg = cfg_mod.load_config()
        if not await asyncio.to_thread(modal_volume.modal_importable):
            return web.json_response({"ok": False, "nodes": [], "error": "modal 未安装"})
        try:
            nodes = await asyncio.to_thread(local_nodes.list_volume_local_nodes, cfg)   # Modal SDK,同步
        except local_nodes.VolumeUnavailable as e:
            # 契约 C13:读不到 ≠ 没有。以前读失败返回空名单(还缓存 60 秒),面板显示「云端没有
            # 私有节点」,Modal 恢复之后一分钟内也照样是空的(2026-10-05 深度 review)。
            return web.json_response({"ok": False, "nodes": [],
                                      "error": f"读不到 Volume 上的私有节点名单:{e}"})
        return web.json_response({"ok": True, "nodes": nodes})

    @routes.post("/modal_bridge/remove_local_node")
    @_admin_only
    async def _remove_local_node(request: web.Request):
        """从 Volume 删掉某个本地节点包。body: {folder}"""
        body = await _json_object(request)
        folder = _opt_str(body, "folder").strip()
        if not folder or "/" in folder or "\\" in folder or ".." in folder:
            return web.json_response({"error": "folder 非法"}, status=400)
        cfg = cfg_mod.load_config()
        if not await asyncio.to_thread(modal_volume.modal_importable):
            return web.json_response({"ok": False, "error": "modal 未安装,无法操作 Volume"},
                                     status=503)
        r = await asyncio.to_thread(local_nodes.remove_volume_local_node, cfg, folder)   # Modal SDK
        return web.json_response({**r, "folder": folder},
                                 status=200 if r["ok"] else 502)

    # -------- custom_node 双向同步 --------

    @routes.get("/modal_bridge/list_nodes")
    @_admin_only
    async def _list_nodes(request: web.Request):
        """
        列出镜像实装的 custom_nodes 全集(供「管理云端节点」面板手动清理)。
        权威来自 Modal /health 的 custom_nodes(真实部署),url/commit 用本地 baked 补全;
        /health 不可达则回退本地 baked 清单。
        返回: {ok, source, nodes: [{name, url, commit, in_local_baked}]}
        """
        cfg = cfg_mod.load_config()
        local_baked = {n["name"]: n for n in node_sync.read_baked_nodes()}
        names, manifest = None, []
        info, unchecked = await _fetch_cloud_node_info(cfg)
        if info is not None:
            names = info["custom_nodes"]
            manifest = info.get("custom_nodes_manifest") or []
        else:
            print(f"[modal_bridge] list_nodes: 读不到云端节点清单,回退本地 ({unchecked})")
            names = list(local_baked.keys())
        # url/commit 云端 manifest 优先 —— 否则别的机器加的节点在这里 url 为空,面板「移除并重部署」
        # 会把它们连同被移除的那个一起丢掉(见 node_sync.complete_baked_entries)。
        # complete_baked_entries 对补不出来源的节点会跑 git(每个 4~5 个子进程),放进线程(深度 review)。
        entries = await asyncio.to_thread(
            node_sync.complete_baked_entries, sorted(names), local_baked, manifest)
        nodes = [{**e, "in_local_baked": e["name"] in local_baked} for e in entries]
        out = {"ok": True, "source": "modal" if info is not None else "local", "nodes": nodes}
        if info is None:
            # 契约 C6:退回本机清单时如实标明 —— 这份名单可能缺了别的机器加的节点,
            # 面板据此提醒,别让「没查到」看起来像「云端就这些」。
            out["cloud_unchecked"] = unchecked
        return web.json_response(out)

    @routes.post("/modal_bridge/check_nodes")
    @_admin_only
    async def _check_nodes(request: web.Request):
        """
        双向同步规划:对比工作流用到的 custom_node 与 Modal 镜像,算出加/改/删。全本地解析,瞬时。
        baked 清单优先用 Modal /health 的 custom_nodes(权威,反映真实已部署镜像),不可达回退本地数据文件。
        body: {prompt}
        返回: node_sync.plan_node_sync(...) + {ok, source}
        """
        body = await _json_object(request)
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)

        cfg = cfg_mod.load_config()
        baked = None
        nodes_info, unchecked = await _fetch_cloud_node_info(cfg)
        if nodes_info is not None:
            # url/commit:云端 manifest 优先,本机清单兜底。以前只用本机清单,本机没有的节点
            # 被填成空 url → write_baked_nodes 出口丢弃 → 下次部署从镜像里删掉(多机互删)。
            # ⚠ 补不出来源的节点要跑 git(_local_git_entry → 4~5 个子进程),以前直接在事件循环里跑,
            #   每次点 RunModal 都可能卡住整个 ComfyUI(2026-10-05 深度 review)。
            local_baked = {n["name"]: n for n in node_sync.read_baked_nodes()}
            baked = await asyncio.to_thread(
                node_sync.complete_baked_entries, nodes_info["custom_nodes"], local_baked,
                nodes_info.get("custom_nodes_manifest") or [])
        else:
            print(f"[modal_bridge] check_nodes: 读不到云端节点清单,回退本地清单 ({unchecked})")

        # 每次点 RunModal 都会调这里:同步跑的话,每个节点目录 5 次 git 子进程(各 10s 超时)
        # + 未命中缓存的 Modal SDK 查询 0.8~2.4s,期间 websocket 进度、别的请求、版本检查全卡住
        # —— 注释里记过的「/version 6s 超时误判」就是这么来的(2026-09-23 review)。
        result = await asyncio.to_thread(node_sync.plan_node_sync, prompt, baked=baked)
        # 只对 Volume 中实际存在的旧覆盖包发删除请求；expect_baked 仍保留全部应跑镜像版
        # 的节点,用于修复已解压旧包的暖容器以及列表查询暂时失败的情况。
        try:
            volume_local = set(await asyncio.to_thread(local_nodes.list_volume_local_nodes, cfg))
        except local_nodes.VolumeUnavailable as e:
            # 契约 C13:读不到就别发删除请求,如实标明;worker 侧的 baked sentinel 仍会兜住暖容器。
            result["local_remove"] = []
            result["volume_unchecked"] = str(e) or type(e).__name__
        else:
            result["local_remove"] = sorted(set(result.get("expect_baked", [])) & volume_local)
        result["ok"] = True
        result["source"] = "modal" if nodes_info is not None else "local"
        if nodes_info is None:
            # 契约 C6:退回本机清单 = 没核对云端。前端据此**不自动同步**:拿本机清单去部署,
            # 可能删掉别的机器加的节点(2026-10-05 深度 review)。
            result["cloud_unchecked"] = unchecked
        return web.json_response(result)

    @routes.post("/modal_bridge/sync_nodes")
    @_admin_only
    async def _sync_nodes(request: web.Request):
        """
        按 plan 的 new_baked 重写镜像清单(增/改/删)并重部署。stream 回传 modal deploy 日志。
        body: {new_baked: [{name,url,commit}], prune?: [name], summary?: {add,update,prune}}
        最后一行: __DEPLOY_DONE__ rc=<code>

        删除必须显式(契约 C6,2026-10-05 深度 review):云端装着、new_baked 里没有、也不在 prune 里的
        节点,一律从云端 manifest / 本机补回来源并回清单 —— 以前 new_baked 里没有就等于删,而
        /check_nodes 读不到云端时退回本机清单,算出的 new_baked 本来就缺别的机器加的节点,
        一次 RunModal 的自动同步就把它们从镜像里删了。补不出来源、或读不到云端而这次会让本机
        清单里的节点消失,都在 prepare 之前回 409 JSON。
        """
        body = await _json_object(request)
        new_baked = body.get("new_baked")
        if not isinstance(new_baked, list):
            return web.json_response({"error": "new_baked (list) required"}, status=400)
        prune = body.get("prune")
        if prune is None:
            prune = []
        if not isinstance(prune, list) or not all(isinstance(x, str) and x.strip() for x in prune):
            return web.json_response({"error": "prune 必须是节点名(字符串)列表"}, status=400)
        summary = body.get("summary") or {}
        if not isinstance(summary, dict):
            return web.json_response({"error": "summary 必须是对象"}, status=400)

        # 校验并规整每条
        clean = []
        for e in new_baked:
            if not isinstance(e, dict):
                return web.json_response({"error": "new_baked 的每一项必须是 {name,url,commit} 对象"},
                                         status=400)
            name = e.get("name")
            if not name:
                continue
            url, commit = e.get("url") or "", e.get("commit") or ""
            if not all(isinstance(v, str) for v in (name, url, commit)):
                return web.json_response({"error": f"new_baked 里 {name!r} 的 name/url/commit 必须是字符串"},
                                         status=400)
            clean.append({"name": name, "url": url, "commit": commit})

        # ⚠ 空 url 的条目以前会在 write_baked_nodes 出口被静默丢掉,随后的部署就把它从镜像里删了。
        #   这里的 new_baked 来自 /check_nodes 的补全,补不出 url(云端太老 / 来源被脱敏 / 本机没装)
        #   的节点必须拒绝,不能当成「用户要删它」(2026-09-24 review)。
        _lost = [e["name"] for e in clean if not (e.get("url") or "").strip()]
        if _lost:
            return web.json_response({"error": node_sync.unresolved_nodes_message(_lost)}, status=409)

        # 读云端 + 写清单 + deploy 整段独占:并发请求会互相覆盖 _custom_nodes_data.py、两个 deploy 也冲突。
        # ⚠ 锁要在 prepare 之前拿:锁内对账出的 409 必须是 JSON(契约 C6),流一开就回不了状态码了。
        #   代价是排队时前端看不到「排队等待」那行字,只是请求晚一点返回。
        if _DEPLOY_LOCK.locked():
            print("[modal_bridge] sync_nodes: 另有部署/节点同步进行中,排队等待")
        async with _DEPLOY_LOCK:
            # R8:锁内重读 —— 排队期间另一次 /deploy 可能换了 app / token 并写回 config,
            # 拿锁前那份会把清单部署到旧 app、把依赖指纹记到旧 app 上(深度 review)。
            cfg = cfg_mod.load_config()
            wanted = {e["name"] for e in clean}
            dropped = set(prune)
            added_back: list[str] = []
            unchecked = ""
            try:
                cloud_names, manifest = await asyncio.to_thread(node_sync.fetch_cloud_nodes, cfg)
            except Exception as e:
                if getattr(e, "kind", None) != "not_deployed":   # 未部署 = 全新部署,没有可保护的
                    unchecked = str(e) or type(e).__name__
                    local_names = {n.get("name") for n in node_sync.read_baked_nodes() if n.get("name")}
                    vanish = sorted(local_names - wanted - dropped)
                    if vanish:
                        return web.json_response({
                            "error": (f"读不到云端装了哪些节点({_one_line(unchecked)}),而这次同步会让本机清单里的 "
                                      f"{', '.join(vanish)} 消失、又没有明确要求删除 —— 为免误删云端节点,已拒绝。"
                                      f"稍后重试;确实要删请在「管理云端节点」里勾选移除。"),
                            "cloud_unchecked": unchecked, "vanish": vanish}, status=409)
            else:
                missing = [n for n in cloud_names
                           if isinstance(n, str) and n not in wanted and n not in dropped]
                if missing:
                    local_by_name = {n["name"]: n for n in node_sync.read_baked_nodes() if n.get("name")}
                    entries = await asyncio.to_thread(
                        node_sync.complete_baked_entries, missing, local_by_name, manifest)
                    lost = [x["name"] for x in entries if not (x.get("url") or "").strip()]
                    if lost:
                        return web.json_response(
                            {"error": node_sync.unresolved_nodes_message(lost)}, status=409)
                    clean.extend(entries)
                    added_back = [x["name"] for x in entries]

            async def run(resp: web.StreamResponse) -> int:
                cwd = str(node_sync.MODAL_APP_DIR)
                if added_back:
                    await _emit(resp, f"== 节点清单:云端装着、这次计划里没有、也没要求删除的 {len(added_back)} 个"
                                      f"已并回(不会被这次部署删掉)—— {', '.join(added_back)} ==\n")
                if unchecked:
                    await _emit(resp, f"   ⚠ 读不到云端节点清单({_one_line(unchecked)}):别的机器加的节点"
                                      f"没法并回,本次以本机清单为准\n")
                try:
                    reqs = await asyncio.to_thread(_refresh_local_node_reqs, cfg)
                except local_nodes.VolumeUnavailable as e:
                    await _emit_abort(resp, f"读不到 Volume 上的私有节点名单({_one_line(e)}),算不出镜像要装的"
                                            f"依赖;为免把私有节点的依赖从镜像里删掉,这次不部署。稍后重试")
                    return 1
                node_sync.write_baked_nodes(clean)
                reqs_hash = node_sync.local_node_reqs_hash(reqs)
                print(f"[modal_bridge] sync_nodes: baked → {len(clean)} 条 (add={summary.get('add')} "
                      f"update={summary.get('update')} prune={summary.get('prune')} "
                      f"并回={len(added_back)})")

                await _emit(resp, f"== 同步 custom_nodes:加 {summary.get('add', '?')} / 改 "
                                  f"{summary.get('update', '?')} / 删 {summary.get('prune', '?')} ==\n")
                await _emit(resp, f"== 镜像清单现 {len(clean)} 条,重新部署(clone + 装依赖约 1-3 分钟,别关窗口)==\n\n")

                rc = await _ensure_modal(resp)
                if rc != 0:
                    return rc

                rc = await _run_streamed(resp, node_sync.deploy_command(), cwd=cwd,
                                         env=node_sync.deploy_env(cfg))
                if rc == 0:
                    final_cfg = cfg_mod.load_config()
                    final_cfg["local_node_reqs_deployed_hash"] = reqs_hash
                    cfg_mod.save_config(final_cfg)
                    await asyncio.to_thread(modal_volume.record_deployed_reqs, cfg, reqs)
                return rc

            return await _stream_run(request, "节点同步", run)

    @routes.post("/modal_bridge/deploy")
    @_admin_only
    async def _deploy(request: web.Request):
        """
        GUI 一键部署/重新部署:检查 Manager 已装的 modal → 建 secret → modal deploy → 写 config。
        全程在 ComfyUI 进程里,零终端。stream 回传日志,最后 __DEPLOY_DONE__ rc=<code>。
        body: {token_id, token_secret, workspace, hf_token?, civitai_token?,
               app_name?, volume_name?, default_gpu?, scaledown_window?,
               comfy_api_key?, aigc_studio_base_url?, aigc_bypass_secret?}
        """
        # 字段类型在 prepare 之前校验完(深度 review):以前 scaledown_window 传个 "abc"
        # 在 prepare 之后才 int() 炸掉,流被截断,前端只看到一句笼统的失败。
        body = await _json_object(request)
        for _k in ("token_id", "token_secret", "workspace", "hf_token", "civitai_token", "app_name",
                   "volume_name", "default_gpu", "comfy_api_key", "aigc_studio_base_url",
                   "aigc_bypass_secret", "gpu_tier"):
            _opt_str(body, _k)
        _sd = body.get("scaledown_window")
        if _sd not in (None, "") and (isinstance(_sd, bool) or not isinstance(_sd, (int, str))
                                      or not str(_sd).strip().isdigit()):
            raise _BadRequest("scaledown_window 必须是非负整数(秒)")
        if body.get("auto_downgrade") is not None and not isinstance(body.get("auto_downgrade"), bool):
            raise _BadRequest("auto_downgrade 必须是 boolean")
        if (body.get("gpu_tier") or "").strip().lower() not in ("", "auto", "cheap", "primary", "top"):
            raise _BadRequest("gpu_tier 非法")
        # token_secret 现在不回显到前端(/config 已抹掉),留空 = 沿用已存的;token_id 同理
        _stored = cfg_mod.load_config()
        token_id = (body.get("token_id") or "").strip() or (_stored.get("modal_token_id") or "")
        token_secret = (body.get("token_secret") or "").strip() or (_stored.get("modal_token_secret") or "")
        workspace = (body.get("workspace") or "").strip()

        # 校验
        errs = []
        if not token_id.startswith("ak-"):
            errs.append("token_id 应以 ak- 开头(modal.com/settings/tokens 创建)")
        if not token_secret.startswith("as-"):
            errs.append("token_secret 应以 as- 开头(首次部署必填;之后留空=沿用已存的)")
        if not workspace:
            errs.append("workspace 不能空(modal.com 个人主页 URL 那一段)")
        else:
            errs += _deploy_name_errors(
                workspace, (body.get("app_name") or _stored.get("modal_app_name") or "comfyui-bridge").strip())

        async def run(resp: web.StreamResponse) -> int:
            if errs:
                for e in errs:
                    await _emit(resp, f"✗ {e}\n")
                return 2

            # 1) modal 包
            rc = await _ensure_modal(resp)
            if rc != 0:
                return rc

            if _DEPLOY_LOCK.locked():
                await _emit(resp, "\n== 另有部署/节点同步进行中,排队等待…\n")
            # 读 config + secret + deploy + 写 config 整段独占(与 /sync_nodes 共用锁,避免并发 deploy 冲突)
            async with _DEPLOY_LOCK:
                rc, done = await _deploy_locked(resp, body, token_id, token_secret, workspace)
            if rc != 0:
                return rc
            return await _deploy_verify(resp, *done)   # 验证 + 兼容性检测在锁外,别让它们堵住别的同步

        return await _stream_run(request, "部署", run)

    # -------- 取消(代理 Modal /cancel)--------
    @routes.post("/modal_bridge/cancel")
    @_admin_only
    async def _cancel(request: web.Request):
        body = await _json_object(request)
        job_id = body.get("job_id")
        if not job_id or not isinstance(job_id, str):
            return web.json_response({"error": "job_id (string) required"}, status=400)
        cfg = cfg_mod.load_config()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                result = await modal_client.cancel(session, cfg, job_id)
        except Exception as e:
            # 取消请求没送到 / 没回来,或云端回了 401 / 非 2xx / 非 JSON(modal_client.cancel 一律抛错):
            # 取消结果未知,云端可能还在跑、还在计费(契约 C2)。
            return web.json_response({"ok": False, "still_billing": True, "error": str(e)}, status=502)
        if not isinstance(result, dict):
            return web.json_response({"ok": False, "still_billing": True,
                                      "error": f"云端 /cancel 返回了无法识别的内容: {str(result)[:200]}"})
        # 契约 C2(2026-10-05 深度 review):ok 只回答「还会不会继续计费」。以前 ok = 没有 error,
        # 于是「任务早就结束了」(cancel_noop)和「查无此任务」(not_found)也被报成取消失败,前端
        # 提示「可能仍在计费,去 Modal 控制台确认」—— 吓人且是错的。只有取消失败、云端可能还在跑
        # 才 still_billing。云端取消失败(如 Modal 拒绝该请求)仍须原样透出 error。
        still_billing = (bool(result.get("error")) and result.get("status") != "not_found"
                         and not result.get("cancel_noop"))
        if still_billing:
            print(f"[modal_bridge] cancel job {job_id} FAILED: {result['error']}")
        else:
            print(f"[modal_bridge] cancelled job {job_id}: {result}")
        return web.json_response({**result, "ok": not still_billing, "still_billing": still_billing})

    # -------- 健康检查(代理一下 Modal 那边的)--------
    @routes.get("/modal_bridge/health")
    async def _health(request: web.Request):
        """云端健康检查代理。不是管理路由(匿名可调),所以分两档(2026-10-05 深度 review):
        本机同源直连、或带了有效 capability → 完整内容(会真的请求云端);其它(局域网 / 反代 /
        容器里没配 capability 的调用方)→ 只给 {ok, healthy},healthy 取最近一次完整检查的结论,
        **不去碰云端**。以前任何人都能拿到节点 manifest(仓库地址、commit),每调一次还会唤醒
        云端容器。localhost 行为不变(用户明确不要加摩擦);MCP 本地模式经 _call 会带上从
        config.json 读到的 capability,照样拿完整内容。"""
        try:
            full = _health_full_view(request)
            cfg = cfg_mod.load_config() if full else None
        except cfg_mod.ConfigCorrupt as e:
            return _config_corrupt_response(e)
        if not full:
            return web.json_response({
                "ok": True, "healthy": _LAST_HEALTH["healthy"], "limited": True,
                "detail": "非本机访问未带有效 capability:只给最近一次检查的结论,不查询云端"})
        async with aiohttp.ClientSession() as s:
            try:
                h = await modal_client.health(s, cfg)
                _LAST_HEALTH["healthy"] = bool(isinstance(h, dict) and h.get("healthy"))
                return web.json_response({"ok": True, "modal": h})  # 不回传 config(含 token)
            except Exception as e:
                _LAST_HEALTH["healthy"] = False
                return web.json_response({"ok": False, "error": str(e)}, status=502)

    @routes.get("/modal_bridge/platform_status")
    async def _platform_status(request: web.Request):
        """查 Modal 平台官方状态页(status.modal.com,BetterStack)的整体状态。
        用于:连不上云端时区分'Modal 平台故障'还是'你没部署';启动时主动预警。
        返回 {ok, state}  state ∈ operational/degraded/downtime/maintenance/unknown。"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                async with s.get("https://status.modal.com/index.json") as r:
                    data = await r.json(content_type=None)
            state = data.get("data", {}).get("attributes", {}).get("aggregate_state", "unknown")
        except Exception as e:
            print(f"[modal_bridge] platform_status 查询失败: {e}")
            state = "unknown"
        return web.json_response({"ok": True, "state": state})

    @routes.get("/modal_bridge/version")
    async def _version(request: web.Request):
        """版本契约:比对本地插件版本 vs 云端部署的版本。
        返回 {ok, local, deployed, match, reachable}。
          - match=False 且 reachable=True → 插件升级了但没重新部署 → 前端拦截、引导部署
          - reachable=False → 连不上(没部署/app 删了)→ 也要引导部署
        """
        local = node_sync.plugin_version()
        try:
            cfg = cfg_mod.load_config()
        except cfg_mod.ConfigCorrupt as e:
            return _config_corrupt_response(e)
        local_gpu = (cfg.get("default_gpu") or "H100")
        local_comfyui = node_sync.detect_local_comfyui_version()   # 当前本机 ComfyUI 版本
        deploy_comfyui = cfg.get("comfyui_version") or None         # 上次部署时检测到的版本
        deployed, deployed_gpu, reachable, err_kind = None, None, False, None
        # 快速单次直查(不走 health 的 3×10s 重试,避免点 Modal 卡 30s 无反应)。
        url = modal_client._endpoint(cfg["modal_endpoint_base"], "health")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
                async with s.get(url, headers={"X-Bridge-Key": modal_client._key(cfg)},
                                 allow_redirects=False) as r:   # 不带 key 跟随重定向,见 modal_client 顶部
                    if r.status == 200:
                        h = await r.json(content_type=None)
                        if isinstance(h, dict):
                            deployed = h.get("deployed_version")
                            deployed_gpu = h.get("deployed_gpu")
                            reachable = True
                    elif r.status == 404:
                        err_kind = "not_deployed"  # endpoint 不存在 = app 没部署
                    elif r.status == 401:
                        # 契约 C7:key 不对单独归因 —— 以前混在 http_error 里,前端只能说「云端出错」,
                        # 而这时该做的是重新部署刷新 key(2026-10-05 深度 review)。
                        err_kind = "unauthorized"
                    else:
                        err_kind = "http_error"
        except asyncio.TimeoutError:
            # ⚠ 这个 6 秒是**挂钟**超时,而 ComfyUI 是单进程:本地采样(同步的 PyTorch 调用)
            # 期间 event loop 调度不到,3 s/it 的工作流两个迭代就吃满 —— 请求根本没轮到处理。
            # 以前一律记成 timeout,前端据此弹「Modal 平台故障」,而云端完全正常。
            # 先问一句本地队列忙不忙,把这种情况如实归因,前端才能不拦(见 checkVersionOrBlock)。
            if _local_queue_busy():
                err_kind = "local_busy"
                print("[modal_bridge] version check: 本地有任务在跑,event loop 被阻塞导致超时 —— "
                      "与 Modal 无关,放行提交")
            else:
                err_kind = "timeout"
                print("[modal_bridge] version check: health 超时(本机网络慢 / 云端未部署 / 平台故障)")
        except Exception as e:
            err_kind = "unreachable"
            print(f"[modal_bridge] version check: health 不可达 ({e})")
        # 契约计算抽到 contract.compute_contract(纯函数,有单测)。
        c = contract.compute_contract(local, deployed, reachable, local_gpu, deployed_gpu,
                                      local_comfyui=local_comfyui, deploy_comfyui=deploy_comfyui)
        return web.json_response({"ok": True, "err_kind": err_kind, **c})


_setup_routes()
