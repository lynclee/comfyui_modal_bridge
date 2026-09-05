"""
routes.py — 本地 ComfyUI 服务器上的 HTTP 路由
所有路由前缀 /modal_bridge/...
"""
import asyncio
import base64
import contextlib
import functools
import secrets
import subprocess
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
    供通用兜底(LOADER_MAP 外的 loader)反推模型属于哪个 models/<type>/。找不到返回 None。"""
    base = Path(filename).name
    if folder_paths is None:
        return None
    try:
        types_ = list(folder_paths.folder_names_and_paths.keys())
    except Exception:
        return None
    for t in types_:
        try:
            if folder_paths.get_full_path(t, base):
                return t
        except Exception:
            pass
    return None


def extract_required_models(prompt: dict) -> list[dict]:
    """返回 [{type, filename}, ...] 去重。
    = LOADER_MAP 已知 type 的模型 + 通用兜底(扫到的模型文件名,按本地位置反推 type)。
    通用兜底只收"本地能定位到、因而能推出 type"的:本地都没有的模型反正传不上去,
    维持原行为(不强行入列),由云端验证阶段报缺。"""
    loader_models = model_deps.extract_loader_models(prompt)
    out = list(loader_models)
    seen_base = {Path(m["filename"]).name for m in loader_models}
    for fn in sorted(model_deps.extract_generic_filenames(prompt)):
        if fn in seen_base:
            continue
        t = _resolve_model_anywhere(fn)
        if t:
            out.append({"type": t, "filename": fn})
            seen_base.add(fn)
    return out


# 各 GPU 显存(GB)。用于"按工作流估算显存自动选便宜档"。与前端 GPU_VRAM 保持一致。
_GPU_VRAM_GB = {"L40S": 48, "A100-80GB": 80, "H100": 80, "H200": 141, "B200": 180, "A10G": 24, "L4": 24}
_CHEAP_MARGIN_GB = 6  # 余量:估算 + 激活波动,est_vram 要比便宜卡显存低这么多才敢降档(防 OOM)


def _estimate_workflow_vram(prompt: dict) -> tuple[float, str, int]:
    """估工作流显存需求(GB)+ 类别 + 本地查不到大小的模型数。供自动选档 / 预警端点复用。
    视频类优先走激活公式(最大模型常驻 + W×H×帧数 激活项,H3 双卡实测校准),
    工作流里抠不出分辨率/帧数字面量时回退旧的「权重总和×系数」保守公式。"""
    resolver = _local_model_resolver()
    total_bytes, largest_bytes, unknown = 0, 0, 0
    for m in extract_required_models(prompt):
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


def _pick_gpu_class(prompt: dict, cfg: dict) -> tuple[str, str]:
    """按估算显存在 GPU 档梯子上选档,返回 (gpu_class, reason)。gpu_class ∈ {'cheap','primary','top'}。

    gpu_tier 固定某档时直接返回该档 —— 四档 worker 是一次部署全建好的,选哪档纯粹是
    运行时路由,**换档不必重新部署**(换某档具体是哪张卡才要)。
    gpu_tier=auto 时走梯子(成本低→高 L40S→H100→B200):
      1) 升档(防 OOM):估算 > 主卡容量 → top(B200 180G)。
      2) 降档(省钱):cheap≠主卡 + 非视频 + 大小已知 + 放得下便宜卡 → cheap(L40S)。
      3) 否则 → primary(H100)。
    本地查不到大小(unknown>0)时估算不可信:不升不降,留 primary(稳妥)。"""
    tier = resolve_gpu_tier(cfg)
    if tier != "auto":
        gpu_name = (cfg.get(_TIER_GPU_KEY[tier]) or "").strip() or "?"
        return tier, f"固定 {tier} 档({gpu_name})"
    cheap_gpu = (cfg.get("cheap_gpu") or "L40S").strip()
    primary_gpu = (cfg.get("default_gpu") or "H100").strip()
    top_gpu = (cfg.get("top_gpu") or "").strip()
    est, category, unknown = _estimate_workflow_vram(prompt)
    primary_vram = _GPU_VRAM_GB.get(primary_gpu, 80)

    # 1) 升档:估算超过主卡「裸显存」才升(防 OOM)。⚠ 这里不减 margin ——
    #    est 已含系数余量(图像×1.15 / 视频×1.3+8),再减 margin 会双重保守:
    #    例 FLUX.2-dev est≈76G,实际在 H100/A100 80G 上跑得动,不该误升 H200。
    #    需有可信估算(unknown==0)。
    if (top_gpu and top_gpu != primary_gpu and unknown == 0
            and est > primary_vram):
        return "top", f"估算 {est:.1f}G > 主卡 {primary_gpu}({primary_vram}G) → 升档 {top_gpu}"

    # 2) 降档:省钱档放得下 → 便宜卡。
    if (cfg.get("auto_downgrade", True) and cheap_gpu != primary_gpu
            and category != "video" and unknown == 0):
        cap = _GPU_VRAM_GB.get(cheap_gpu, 48) - _CHEAP_MARGIN_GB
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

    def _atomic_write(dst: Path, data: bytes) -> int:
        """先写 .part 再 rename —— 半截文件不能以正式名出现在 output/ 里。
        ComfyUI 的画廊/前端会直接读这个目录,写到一半被读到就是一张坏图;
        Volume 下载那条路径(bridge_client / modal_volume)早就是 .part+rename 了,这边补齐。"""
        tmp = dst.with_suffix(dst.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dst)
        return len(data)

    def _dedup(fn: str) -> str:
        if fn not in seen:
            seen.add(fn)
            return fn
        stem, _, ext = fn.rpartition(".")
        fn2 = f"{stem}_{len(seen)}.{ext}" if ext else f"{fn}_{len(seen)}"
        seen.add(fn2)
        return fn2

    images = final.get("images")
    if isinstance(images, list) and images:
        for img in images:
            vp = img.get("volume_path")
            b64 = img.get("data_base64")
            if not vp and not b64:
                continue
            fn = _dedup(Path(img.get("filename") or "output.png").name)  # basename 防路径逃逸
            local = out_dir / fn
            if vp:
                # ⚠ vp 整个来自浏览器提交的 modal_state,没人替我们验过 —— 而这条路
                # **绕过云端 fetch_endpoint、直连 Volume SDK**,云端那道囚笼管不到。
                # 伪造成 models/... 就能把模型下载走并删掉(取回后即删是既定行为),
                # 删除不可逆。所以本地必须自己囚一次(规则与云端逐字相同)。
                if not contract.is_safe_output_path(job_id, vp):
                    raise RuntimeError(f"volume_path 越界(必须在 _outputs/{job_id}/ 内): {vp!r}")
                # 大文件:从 Volume 直连下载(不走 base64/Dict),下完删 Volume 上的副本
                total = await asyncio.to_thread(modal_volume.volume_file_size, cfg, vp)
                sampler = asyncio.create_task(
                    _sample_part_size(job_id, local.with_name(local.name + ".part"), total, fn))
                try:
                    size = await asyncio.to_thread(modal_volume.download_volume_file, cfg, vp, str(local))
                except Exception as e:
                    raise RuntimeError(f"volume download {vp} failed: {e}")
                finally:
                    sampler.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sampler
                await asyncio.to_thread(modal_volume.remove_volume_path, cfg, vp)
            else:
                _fetch_progress_set(job_id, stage="decode", label=fn,
                                    done=0, total=len(b64) * 3 // 4)
                size = await asyncio.to_thread(_atomic_write, local, base64.b64decode(b64))
                _fetch_progress_set(job_id, stage="decode", label=fn, done=size, total=size)
            outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                            "type": "output", "size_bytes": size,
                            "node_id": img.get("node_id"),  # 来源节点 → 前端按节点回填
                            "key": img.get("key")})          # 原始输出键 → 前端按键派发渲染
        return outputs

    # 单图回退
    fn = Path(final.get("filename") or "output.png").name  # basename 防路径逃逸
    b64 = final.get("data_base64")
    image_url = final.get("image_url")
    if b64:
        size = _atomic_write(out_dir / fn, base64.b64decode(b64))
        outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                        "type": "output", "size_bytes": size})
    elif image_url:
        async with aiohttp.ClientSession() as s:
            async with s.get(image_url) as r:
                if r.status >= 400:
                    raise RuntimeError(f"download {image_url} failed: {r.status}")
                data = await r.read()
        size = _atomic_write(out_dir / fn, data)
        outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                        "type": "output", "size_bytes": size, "source_url": image_url})
    return outputs


def _extract_input_image_names(prompt: dict) -> list[str]:
    """遍历 prompt 找所有 LoadImage 类节点引用的本地文件名(去重)。"""
    names: list[str] = []
    seen: set[str] = set()
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type", "")
        # 常见会引用 input/ 里图片的节点类型
        if cls in ("LoadImage", "LoadImageMask", "LoadImageOutput"):
            ins = node.get("inputs", {}) or {}
            name = ins.get("image") or ins.get("filename")
            if isinstance(name, str) and name not in seen:
                # 跳过子目录形式 "clipspace/xxx"(ComfyUI 自动 cache 那种)— 第一版只支持 input 根
                if "/" in name or "\\" in name:
                    print(f"[modal_bridge] WARN: subpath input ignored: {name}")
                    continue
                seen.add(name)
                names.append(name)
    return names


def _read_input_as_b64(name: str) -> dict:
    """读 input/<name>,返回 Modal 期望的 {name, image (data uri)} 格式。

    ⚠ name 来自工作流 JSON。上游 _extract_input_image_names 已挡掉子路径形态,但那只是
    字符串检查:input 目录里放一个指向目录外的**符号链接**,exists() 照样为真、
    read_bytes() 就把目录外内容读出来上传了。必须 resolve 后确认仍在 input 目录内
    —— 与模型查找用的是同一份囚笼(modal_volume.is_path_within_roots)。
    """
    root = _input_dir()
    p = root / name
    if not p.exists():
        raise FileNotFoundError(f"Input image not found locally: {p}")
    if not modal_volume.is_path_within_roots(p, [root]):
        raise FileNotFoundError(f"输入图越界(解析后不在 input 目录内): {name}")
    blob = p.read_bytes()
    ext = p.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpe": "jpeg"}.get(ext, ext)
    b64 = base64.b64encode(blob).decode("ascii")
    return {"name": name, "image": f"data:image/{mime};base64,{b64}"}


async def _emit(resp: web.StreamResponse, text: str) -> None:
    try:
        await resp.write(text.encode("utf-8"))
    except Exception:
        pass


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


def _admin_denial(request: web.Request) -> web.Response | None:
    """本机直连免配置；其它访问必须带持久 capability。

    peer+Host 双判避免反向代理把远程访客伪装成 127.0.0.1。capability 只存在本机
    0600 config 和调用方浏览器 localStorage 中,绝不从匿名端点回吐。
    """
    try:
        host = request.host
    except Exception:
        host = ""
    forwarded = ",".join(x for x in (
        request.headers.get("X-Forwarded-For", ""),
        request.headers.get("X-Real-IP", ""),
    ) if x)
    if contract.is_direct_loopback_request(request.remote, host, forwarded):
        return None
    expected = cfg_mod.ensure_local_api_capability()
    supplied = (request.headers.get(_ADMIN_HEADER) or "").strip()
    if supplied and secrets.compare_digest(supplied, expected):
        return None
    return web.json_response(
        {"error": "admin capability required",
         "code": "modal_bridge_admin_capability_required"},
        status=403,
        headers={_ADMIN_REQUIRED_HEADER: "capability-required"},
    )


def _admin_only(handler):
    @functools.wraps(handler)
    async def guarded(request: web.Request):
        denial = _admin_denial(request)
        if denial is not None:
            return denial
        return await handler(request)
    return guarded


def _compute_local_node_reqs(cfg: dict) -> list[str]:
    """从 Volume 每个私有节点的 manifest 算出镜像依赖清单。**纯读,不落盘。**

    多机环境不能只扫当前机器目录。旧 zip 没有 manifest 时保留已有 flat 清单；该节点
    下次参与同步会被强制重传并完成迁移。

    与 _refresh_local_node_reqs 拆开,是因为 /local_nodes_diff 这种只读预检也要算这个
    指纹(判断"依赖镜像是否还欠一次重建"),而它不该写文件、也不该去抢 _DEPLOY_LOCK。
    """
    folders = local_nodes.list_volume_local_nodes(cfg, max_age=0)
    if not folders:
        # 空可能是真空,也可能是 Modal/网络瞬断；保留旧依赖只会多装几个包，清空却会
        # 让仍在 Volume 的私有节点全部 import 失败。取安全的一侧。
        return node_sync.read_local_node_reqs()
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
    if node_sync.modal_available():
        await _emit(resp, "== modal 包已就绪 ==\n")
        return 0
    await _emit(resp, "== ✗ 未检测到 modal 包 ==\n")
    await _emit(resp, "   本插件的依赖由 ComfyUI Manager 在安装时装。手动 clone 进 custom_nodes 的话,\n")
    await _emit(resp, "   在 ComfyUI 用的那个 Python 环境里装一次即可:\n\n")
    await _emit(resp, "       <ComfyUI 的 python> -m pip install -U modal\n\n")
    await _emit(resp, "   装完重启 ComfyUI 再点部署。(也可以在 Manager 里卸载后重装本插件,依赖会自动装上)\n")
    return 1


def _setup_routes():
    # 这个函数被 module 末尾立即调用,而不是 import-time(避免循环)
    from server import PromptServer  # type: ignore

    routes = PromptServer.instance.routes

    # -------- 配置读写 --------
    @routes.get("/modal_bridge/config")
    async def _get_config(request: web.Request):
        # 不把密钥送到浏览器:抹掉 token_secret 和 bridge_api_key,只给前端要的非敏感字段
        # + 一个 has_token_secret 标志(部署框据此显示"已保存,留空=沿用")。
        cfg = dict(cfg_mod.load_config())
        cfg["has_token_secret"] = bool(cfg.get("modal_token_secret"))
        cfg["has_comfy_api_key"] = bool(cfg.get("comfy_api_key"))
        cfg["has_aigc_bypass_secret"] = bool(cfg.get("aigc_bypass_secret"))
        cfg["has_local_api_capability"] = bool(cfg.get("local_api_capability"))
        cfg.pop("modal_token_secret", None)
        cfg.pop("bridge_api_key", None)
        cfg.pop("comfy_api_key", None)  # 账单凭据,不回吐浏览器(同 bridge_api_key)
        cfg.pop("aigc_bypass_secret", None)  # Vercel 旁路密钥,同上
        cfg.pop("local_api_capability", None)  # 本地管理 capability,永不匿名回吐
        cfg.pop("local_node_reqs_deployed_hash", None)  # 内部部署状态
        return web.json_response(cfg)

    @routes.get("/modal_bridge/bridge_key")
    @_admin_only
    async def _bridge_key(request: web.Request):
        """导出脚本「嵌入 KEY」时取回自己的 bridge_api_key。

        localhost 直连可用；远程访问必须通过统一 admin capability。不存在可由匿名
        /config 打开的逃生开关,反向代理也要同时满足外部 Host 校验。
        """
        cfg = cfg_mod.load_config()
        return web.json_response({"key": cfg.get("bridge_api_key", "")})

    @routes.post("/modal_bridge/config")
    @_admin_only
    async def _set_config(request: web.Request):
        body = await request.json()
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        try:
            cur = contract.merge_public_config(cfg_mod.load_config(), body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        cfg_mod.save_config(cur)
        # 不回吐密钥(和 GET /config 一致):抹掉 token_secret / bridge_api_key
        safe = dict(cur)
        safe["has_token_secret"] = bool(safe.get("modal_token_secret"))
        safe["has_comfy_api_key"] = bool(safe.get("comfy_api_key"))
        safe["has_aigc_bypass_secret"] = bool(safe.get("aigc_bypass_secret"))
        safe["has_local_api_capability"] = bool(safe.get("local_api_capability"))
        safe.pop("modal_token_secret", None)
        safe.pop("bridge_api_key", None)
        safe.pop("comfy_api_key", None)
        safe.pop("aigc_bypass_secret", None)
        safe.pop("local_api_capability", None)
        safe.pop("local_node_reqs_deployed_hash", None)
        return web.json_response(safe)

    # -------- 异步提交(返回 job_id,不阻塞)--------
    @routes.post("/modal_bridge/submit")
    @_admin_only
    async def _submit(request: web.Request):
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt (object) required"}, status=400)

        cfg = cfg_mod.load_config()
        tier = (body.get("tier") or "40g").lower()
        # 是否需要 GPU。三条判据,任一成立就给 GPU:
        #   1) 用户**显式选了档位**(非 auto)—— 那是明确表达"我要这张卡",不该再被
        #      "扫不到模型"这种负向推断推翻。以前只要扫不到模型,选了 H100 也照样进 CPU worker。
        #   2) 工作流里扫到了本地模型。
        #   3) 关掉了 cpu_tier_when_no_model —— 默认 True(维持既有账单),但那条推断不可靠:
        #      节点内部下载权重、无模型文件的 CUDA/Triton 图像处理与 3D/光流节点、
        #      模型参数不是文件名字符串的节点,都扫不到却真要 GPU。误判代价不对称:
        #      给错 CPU = 跑不动耗到超时、白烧钱零产出。
        _tier_sel = resolve_gpu_tier(cfg)
        needs_gpu = (
            _tier_sel != "auto"
            or bool(extract_required_models(prompt))
            or not cfg.get("cpu_tier_when_no_model", True)
        )
        # 需要 GPU 时再按估算显存自动选档:放得下便宜卡 → cheap(L40S),否则 primary(H100)。
        gpu_class = "primary"
        if needs_gpu:
            gpu_class, gpu_reason = _pick_gpu_class(prompt, cfg)
            print(f"[modal_bridge] GPU 路由: {gpu_class}  ({gpu_reason})")

        try:
            image_names = _extract_input_image_names(prompt)
            input_images = [_read_input_as_b64(n) for n in image_names]
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
                plan = node_sync.plan_node_sync(prompt)
                # 没走前端预检的调用方也必须声明 baked 期望,否则历史本地覆盖包会在暖容器里
                # 永久存活。digest 与 sentinel 共用一个 map,worker 能统一做版本闸门。
                local_digests = {
                    folder: local_nodes.BAKED_SENTINEL
                    for folder in plan.get("expect_baked", [])
                }
                folders = [p["folder"] for p in plan.get("local_pack", [])]
                if folders:
                    local_digests.update(local_nodes.expected_digests(
                        folders, Path(node_sync._comfyui_root()) / "custom_nodes"))
            except Exception as e:
                print(f"[modal_bridge] 本地节点指纹计算跳过: {e}")

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                submit_result = await modal_client.submit_job(
                    session, cfg, workflow=prompt,
                    input_images=input_images or None, tier=tier, needs_gpu=needs_gpu,
                    gpu_class=gpu_class, local_nodes=local_digests or None,
                )
        except Exception as e:
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
                async with session.get(url, params={"job_id": job_id},
                                       headers={"X-Bridge-Key": modal_client._key(cfg)}) as r:
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
        job_id = body.get("job_id") or "?"
        event = body.get("event") or "unknown"
        detail = (body.get("detail") or "")[:300]
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
        body = await request.json()
        job_id = body.get("job_id")
        final = body.get("modal_state")  # 前端 poll 拿到的最终状态对象
        if not job_id or not isinstance(final, dict):
            return web.json_response({"error": "job_id + modal_state required"}, status=400)
        if not contract.is_safe_job_id(job_id):   # 见 contract.is_safe_job_id 的注释
            return web.json_response({"error": "bad job_id"}, status=400)

        cfg = cfg_mod.load_config()
        subfolder = cfg.get("output_subfolder", "modal_results")
        try:
            outputs = await _write_results(final, job_id, subfolder, cfg)
        except Exception as e:
            _FETCH_PROGRESS.pop(job_id, None)
            return web.json_response({"error": f"write result failed: {e}"}, status=502)
        if not outputs:
            _FETCH_PROGRESS.pop(job_id, None)   # 三条出口都要清,否则残留会让同 id 下次读到旧数
            return web.json_response({"error": "no image in modal_state"}, status=502)

        _FETCH_PROGRESS.pop(job_id, None)
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
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)

        required = extract_required_models(prompt)
        if not required:
            return web.json_response(
                {"required": [], "present": [], "missing_local": [],
                 "downloading": [], "missing_no_source": []})

        if not modal_volume.modal_importable():
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
        body = await request.json()
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
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)
        required = extract_required_models(prompt)
        resolver = _local_model_resolver()
        total_bytes, largest_bytes, known, unknown = 0, 0, 0, []
        for m in required:
            p = resolver(m["type"], m["filename"])
            try:
                if p and Path(p).exists():
                    sz = Path(p).stat().st_size
                    total_bytes += sz
                    largest_bytes = max(largest_bytes, sz)
                    known += 1
                else:
                    unknown.append(f"{m['type']}/{m['filename']}")
            except OSError:
                unknown.append(f"{m['type']}/{m['filename']}")
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
        最后一行: __DEPLOY_DONE__ rc=<code>
        """
        body = await request.json()
        items = body.get("items")
        if not isinstance(items, list) or not items:
            return web.json_response({"error": "items (non-empty list) required"}, status=400)

        cfg = cfg_mod.load_config()
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)

        if not modal_volume.modal_importable():
            await _emit(resp, "✗ 本地没装 modal,无法上传\n\n__DEPLOY_DONE__ rc=1\n")
            await resp.write_eof()
            return resp

        total_mb = sum(int(it.get("size_mb") or 0) for it in items)
        await _emit(resp, f"== 上传 {len(items)} 个模型到 Volume(共 ~{total_mb} MB)==\n")
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
            return modal_volume.upload_models(cfg, items, on_progress=on_progress)

        # 串行化:有别的上传在跑就排队等(上传前会复查 Volume,等到时多半已有、直接跳过)
        if _UPLOAD_LOCK.locked():
            await _emit(resp, "== 另有模型上传进行中,排队等待(同一时刻只传一个,避免并发撞车)…\n\n")
        try:
            async with _UPLOAD_LOCK:
                result = await _run_blocking_streamed(resp, do_upload)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[modal_bridge] sync_models 上传失败: {e}\n{tb}")  # 进 ComfyUI 控制台日志
            await _emit(resp, f"\n✗ 上传失败: {e}\n{tb[-800:]}\n\n__DEPLOY_DONE__ rc=1\n")
            await resp.write_eof()
            return resp

        await _emit(resp, f"\n== ✓ 上传完成:{len(result['uploaded'])} 个,共 ~{result['total_mb']} MB ==\n")
        if result["skipped"]:
            await _emit(resp, f"== ⚠ 跳过 {len(result['skipped'])} 个(本地文件丢失)==\n")
        await _emit(resp, "\n__DEPLOY_DONE__ rc=0\n")
        await resp.write_eof()
        return resp

    @routes.post("/modal_bridge/sync_local_nodes")
    @_admin_only
    async def _sync_local_nodes(request: web.Request):
        """
        本地自写 custom_node(无 git remote / commit 未推送)打包上传 Volume。stream 回传进度。
        worker 启动时会解压进 /comfyui/custom_nodes/；纯代码变化不重部署，依赖变化自动部署。
        body: {folders: ["my_node", ...]}  (前端从 check_nodes 的 local_pack 拿)
        最后一行: __DEPLOY_DONE__ rc=<code>
        """
        body = await request.json()
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
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)

        if not modal_volume.modal_importable():
            await _emit(resp, "✗ 本地没装 modal,无法上传\n\n__DEPLOY_DONE__ rc=1\n")
            await resp.write_eof()
            return resp

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
            import traceback
            tb = traceback.format_exc()
            print(f"[modal_bridge] sync_local_nodes 失败: {e}\n{tb}")
            await _emit(resp, f"\n✗ 上传失败: {e}\n{tb[-800:]}\n\n__DEPLOY_DONE__ rc=1\n")
            await resp.write_eof()
            return resp

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
                    latest_cfg = cfg_mod.load_config()
                    reqs = await asyncio.to_thread(_refresh_local_node_reqs, latest_cfg)
                    target_hash = node_sync.local_node_reqs_hash(reqs)
                    deployed_hash = latest_cfg.get("local_node_reqs_deployed_hash", "")
                    needs_redeploy = target_hash != deployed_hash and bool(reqs or deployed_hash)
                    if needs_redeploy:
                        await _emit(resp, f"== 私有节点依赖已变化({len(reqs)} 条),自动重新部署 ==\n")
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
                            await _emit(resp, "== ✓ 私有节点依赖镜像已更新 ==\n")
                        else:
                            await _emit(resp, "== ✗ 私有节点依赖部署失败,停止本次提交 ==\n")
                except Exception as e:
                    rc = 1
                    await _emit(resp, f"== ✗ 私有节点依赖同步失败:{e} ==\n")
        # 只有全部成功才给可提交的版本契约。失败时发空/部分 map 会让前端漏掉失败节点,
        # 暖容器反而可能继续跑它的旧版本。
        if rc == 0:
            import json as _json
            await _emit(resp, f"__LOCAL_DIGESTS__ {_json.dumps(result.get('digests') or {})}\n")
        await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
        await resp.write_eof()
        return resp

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
        body = await request.json()
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
            _deployed = cfg.get("local_node_reqs_deployed_hash", "")
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
        if not modal_volume.modal_importable():
            return web.json_response({"ok": False, "nodes": [], "error": "modal 未安装"})
        return web.json_response({"ok": True, "nodes": local_nodes.list_volume_local_nodes(cfg)})

    @routes.post("/modal_bridge/remove_local_node")
    @_admin_only
    async def _remove_local_node(request: web.Request):
        """从 Volume 删掉某个本地节点包。body: {folder}"""
        body = await request.json()
        folder = (body.get("folder") or "").strip()
        if not folder or "/" in folder or ".." in folder:
            return web.json_response({"error": "folder 非法"}, status=400)
        cfg = cfg_mod.load_config()
        if not modal_volume.modal_importable():
            return web.json_response({"ok": False, "error": "modal 未安装,无法操作 Volume"},
                                     status=503)
        r = local_nodes.remove_volume_local_node(cfg, folder)
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
        names, source = None, "local"
        try:
            async with aiohttp.ClientSession() as session:
                info = await modal_client.list_nodes(session, cfg)
            if isinstance(info, dict) and isinstance(info.get("custom_nodes"), list):
                names = info["custom_nodes"]
                source = "modal"
        except Exception as e:
            print(f"[modal_bridge] list_nodes: /health 不可达,回退本地 ({e})")
        if names is None:
            names = list(local_baked.keys())
        nodes = []
        for name in sorted(names):
            b = local_baked.get(name, {})
            nodes.append({"name": name, "url": b.get("url", ""), "commit": b.get("commit", ""),
                          "in_local_baked": name in local_baked})
        return web.json_response({"ok": True, "source": source, "nodes": nodes})

    @routes.post("/modal_bridge/check_nodes")
    @_admin_only
    async def _check_nodes(request: web.Request):
        """
        双向同步规划:对比工作流用到的 custom_node 与 Modal 镜像,算出加/改/删。全本地解析,瞬时。
        baked 清单优先用 Modal /health 的 custom_nodes(权威,反映真实已部署镜像),不可达回退本地数据文件。
        body: {prompt}
        返回: node_sync.plan_node_sync(...) + {ok, source}
        """
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)

        cfg = cfg_mod.load_config()
        baked = None
        source = "local"
        try:
            async with aiohttp.ClientSession() as session:
                nodes_info = await modal_client.list_nodes(session, cfg)
            if isinstance(nodes_info, dict) and isinstance(nodes_info.get("custom_nodes"), list):
                # Modal 只给名字;url/commit 用本地清单补全(prune 只看名字,add/update 用本地 git)
                local_baked = {n["name"]: n for n in node_sync.read_baked_nodes()}
                baked = [local_baked.get(name, {"name": name, "url": "", "commit": ""})
                         for name in nodes_info["custom_nodes"]]
                source = "modal"
        except Exception as e:
            print(f"[modal_bridge] check_nodes: /health 不可达,回退本地清单 ({e})")

        result = node_sync.plan_node_sync(prompt, baked=baked)
        # 只对 Volume 中实际存在的旧覆盖包发删除请求；expect_baked 仍保留全部应跑镜像版
        # 的节点,用于修复已解压旧包的暖容器以及列表查询暂时失败的情况。
        volume_local = set(local_nodes.list_volume_local_nodes(cfg))
        result["local_remove"] = sorted(set(result.get("expect_baked", [])) & volume_local)
        result["ok"] = True
        result["source"] = source
        return web.json_response(result)

    @routes.post("/modal_bridge/sync_nodes")
    @_admin_only
    async def _sync_nodes(request: web.Request):
        """
        按 plan 的 new_baked 重写镜像清单(增/改/删)并重部署。stream 回传 modal deploy 日志。
        body: {new_baked: [{name,url,commit}], summary?: {add,update,prune}}
        最后一行: __DEPLOY_DONE__ rc=<code>
        """
        body = await request.json()
        new_baked = body.get("new_baked")
        if not isinstance(new_baked, list):
            return web.json_response({"error": "new_baked (list) required"}, status=400)

        # 校验并规整每条
        clean = []
        for e in new_baked:
            name = e.get("name")
            if not name:
                continue
            clean.append({"name": name, "url": e.get("url", ""), "commit": e.get("commit", "")})

        summary = body.get("summary") or {}
        cfg = cfg_mod.load_config()
        cwd = str(node_sync.MODAL_APP_DIR)

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)

        if _DEPLOY_LOCK.locked():
            await _emit(resp, "== 另有部署/节点同步进行中,排队等待(避免并发写清单 + deploy 撞车)…\n\n")
        # 写清单 + deploy 整段独占:并发请求会互相覆盖 _custom_nodes_data.py、两个 deploy 也冲突
        async with _DEPLOY_LOCK:
            node_sync.write_baked_nodes(clean)
            reqs = await asyncio.to_thread(_refresh_local_node_reqs, cfg)
            reqs_hash = node_sync.local_node_reqs_hash(reqs)
            print(f"[modal_bridge] sync_nodes: baked → {len(clean)} 条 (add={summary.get('add')} "
                  f"update={summary.get('update')} prune={summary.get('prune')})")

            await _emit(resp, f"== 同步 custom_nodes:加 {summary.get('add', '?')} / 改 "
                              f"{summary.get('update', '?')} / 删 {summary.get('prune', '?')} ==\n")
            await _emit(resp, f"== 镜像清单现 {len(clean)} 条,重新部署(clone + 装依赖约 1-3 分钟,别关窗口)==\n\n")

            rc = await _ensure_modal(resp)
            if rc != 0:
                await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
                await resp.write_eof()
                return resp

            rc = await _run_streamed(resp, node_sync.deploy_command(), cwd=cwd, env=node_sync.deploy_env(cfg))
            if rc == 0:
                final_cfg = cfg_mod.load_config()
                final_cfg["local_node_reqs_deployed_hash"] = reqs_hash
                cfg_mod.save_config(final_cfg)
        await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
        await resp.write_eof()
        return resp

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
        body = await request.json()
        # token_secret 现在不回显到前端(/config 已抹掉),留空 = 沿用已存的;token_id 同理
        _stored = cfg_mod.load_config()
        token_id = (body.get("token_id") or "").strip() or (_stored.get("modal_token_id") or "")
        token_secret = (body.get("token_secret") or "").strip() or (_stored.get("modal_token_secret") or "")
        workspace = (body.get("workspace") or "").strip()

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)

        # 校验
        errs = []
        if not token_id.startswith("ak-"):
            errs.append("token_id 应以 ak- 开头(modal.com/settings/tokens 创建)")
        if not token_secret.startswith("as-"):
            errs.append("token_secret 应以 as- 开头(首次部署必填;之后留空=沿用已存的)")
        if not workspace:
            errs.append("workspace 不能空(modal.com 个人主页 URL 那一段)")
        if errs:
            for e in errs:
                await _emit(resp, f"✗ {e}\n")
            await _emit(resp, "\n__DEPLOY_DONE__ rc=2\n")
            await resp.write_eof()
            return resp

        # 缺省值优先沿用已有 config(重新部署时不重置用户之前的选择)
        cfg = cfg_mod.load_config()
        app_name = (body.get("app_name") or cfg.get("modal_app_name") or "comfyui-bridge").strip()
        volume_name = (body.get("volume_name") or cfg.get("modal_volume_name") or "comfyui-bridge-models").strip()
        default_gpu = (body.get("default_gpu") or cfg.get("default_gpu") or "H100").strip()
        scaledown = int(body.get("scaledown_window") or cfg.get("scaledown_window") or 12)
        hf_token = (body.get("hf_token") or "").strip()
        civitai_token = (body.get("civitai_token") or "").strip()
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
        _typed = (body.get("aigc_bypass_secret") or "").strip()
        if _typed:
            aigc_bypass = _typed
        elif aigc_base_url:
            aigc_bypass = cfg.get("aigc_bypass_secret", "")
        else:
            aigc_bypass = ""
        endpoint_base = f"https://{workspace}--{app_name}"
        # 私有鉴权 key:已有就复用(不让旧 config 失效),否则新生成
        bridge_key = cfg.get("bridge_api_key") or node_sync.gen_bridge_key()

        # ComfyUI 版本跟随本机:检测本机版本 → 解析云端 clone tag(无对应取最接近,只警告不中止)
        comfyui_version = node_sync.detect_local_comfyui_version()
        _tags = await asyncio.to_thread(node_sync.list_comfyui_tags)
        comfyui_tag, _tag_note = node_sync.resolve_comfyui_tag(comfyui_version, _tags)

        # 合并出完整 config(用于 deploy_env + 最终落盘)
        cfg.update({
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
            "aigc_studio_base_url": aigc_base_url,
            "aigc_bypass_secret": aigc_bypass,
        })
        env = node_sync.deploy_env(cfg)
        cwd = str(node_sync.MODAL_APP_DIR)

        await _emit(resp, "== Modal 一键部署 ==\n")
        await _emit(resp, f"   workspace={workspace}  app={app_name}\n")
        if _tag_note:
            await _emit(resp, f"   ⚠ {_tag_note}\n")
        await _emit(resp, f"   ComfyUI: 本机={comfyui_version or '未知'} → 云端 clone {comfyui_tag}\n")
        await _emit(resp, f"   plugin_version={node_sync.plugin_version()}  (会烤进云端 deployed_version)\n")
        await _emit(resp, f"   endpoint={endpoint_base}\n\n")

        # 1) modal 包
        rc = await _ensure_modal(resp)
        if rc != 0:
            await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
            await resp.write_eof()
            return resp

        if _DEPLOY_LOCK.locked():
            await _emit(resp, "\n== 另有部署/节点同步进行中,排队等待…\n")
        # secret + deploy + 写 config 整段独占(与 /sync_nodes 共用锁,避免并发 deploy 冲突)
        async with _DEPLOY_LOCK:
            # 2) 建 / 更新 secret(放 HF / Civitai token)
            await _emit(resp, "\n== 创建 Modal Secret ==\n")
            rc = await _run_streamed(
                resp, node_sync.secret_create_cmd(cfg, hf_token, civitai_token, bridge_key,
                                                  comfy_api_key, aigc_base_url, aigc_bypass),
                cwd=cwd, env=env,
            )
            if rc != 0:
                await _emit(resp, "== ✗ secret 创建失败(token 可能无效)==\n")
                await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
                await resp.write_eof()
                return resp

            # 3) 部署 app(首次拉镜像 3-5 分钟)
            node_sync.ensure_baked_file()  # 本地清单是 .gitignore 状态,缺则建空,免得 modal_image 打包炸
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
                await _emit(resp, "\n__DEPLOY_DONE__ rc=1\n")
                await resp.write_eof()
                return resp

            # 私有节点依赖来自 Volume 中每个包的 manifest,而不是只扫当前机器；这样多机
            # 上传的私有节点在任意一台机器重部署时都不会掉依赖。
            _local_reqs = await asyncio.to_thread(_refresh_local_node_reqs, cfg)
            _local_reqs_hash = node_sync.local_node_reqs_hash(_local_reqs)
            if _local_reqs:
                await _emit(resp, f"   私有节点依赖:{len(_local_reqs)} 条(镜像 build 期安装)\n")
            # 云端模型目录跟随本机:生成 extra_model_paths.yaml(覆盖自定义类别如 geometry_estimation)
            _mtypes = node_sync.write_extra_model_paths()
            _custom_mtypes = [t for t in _mtypes if t not in node_sync.STANDARD_MODEL_TYPES]
            await _emit(resp, f"   云端模型目录类型:{len(_mtypes)} 个"
                              f"(自定义 {len(_custom_mtypes)}:{', '.join(_custom_mtypes) or '无'})\n")
            await _emit(resp, "\n== modal deploy(首次拉镜像约 3-5 分钟,别关窗口)==\n")
            rc = await _run_streamed(resp, node_sync.deploy_command(), cwd=cwd, env=env)
            if rc != 0:
                await _emit(resp, "== ✗ modal deploy 失败 ==\n")
                await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
                await resp.write_eof()
                return resp

            # 4) 写本地 config(在 ComfyUI 进程里,路径用 folder_paths,必对)
            cfg["local_node_reqs_deployed_hash"] = _local_reqs_hash
            cfg_mod.save_config(cfg)
            await _emit(resp, f"\n== ✓ config 已写入(endpoint={endpoint_base})==\n")

        # 5) 验证 health(锁外即可)
        try:
            async with aiohttp.ClientSession() as s:
                h = await modal_client.health(s, cfg)
            await _emit(resp, f"== ✓ /health: {h} ==\n")
        except Exception as e:
            await _emit(resp, f"== ⚠ /health 暂不可达(endpoint 可能还在初始化,稍后重试):{e} ==\n")

        # 6) 自定义节点兼容性检测(隔离 app,同镜像 boot 一次 ComfyUI,报每个节点导入成功/失败)。
        #    只警告不阻断:坏节点不影响其它工作流,部署照样 rc=0。
        await _emit(resp, "\n== 自定义节点兼容性检测(云端同镜像 boot 一次 ComfyUI,约 1 分钟)==\n")
        try:
            crc = await _run_streamed(resp, node_sync.node_compat_check_command(), cwd=cwd, env=env)
            if crc != 0:
                await _emit(resp, "== ⚠ 兼容性检测未跑完(不影响部署);可稍后手动 `modal run node_compat_check.py` ==\n")
        except Exception as e:
            await _emit(resp, f"== ⚠ 兼容性检测启动失败(忽略):{e} ==\n")

        await _emit(resp, "\n__DEPLOY_DONE__ rc=0\n")
        await resp.write_eof()
        return resp

    # -------- 取消(代理 Modal /cancel)--------
    @routes.post("/modal_bridge/cancel")
    @_admin_only
    async def _cancel(request: web.Request):
        body = await request.json()
        job_id = body.get("job_id")
        if not job_id:
            return web.json_response({"error": "job_id required"}, status=400)
        cfg = cfg_mod.load_config()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                result = await modal_client.cancel(session, cfg, job_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        # 云端取消失败(如 Modal 拒绝该请求)必须原样透出:此时任务仍在跑、仍在计费,
        # 不能因为 HTTP 200 就当成功 —— 前端据 ok/error 提示用户去 Modal 控制台确认。
        if isinstance(result, dict) and result.get("error"):
            print(f"[modal_bridge] cancel job {job_id} FAILED: {result['error']}")
            return web.json_response({"ok": False, **result})
        print(f"[modal_bridge] cancelled job {job_id}: {result}")
        return web.json_response({"ok": True, **result})

    # -------- 健康检查(代理一下 Modal 那边的)--------
    @routes.get("/modal_bridge/health")
    async def _health(request: web.Request):
        cfg = cfg_mod.load_config()
        async with aiohttp.ClientSession() as s:
            try:
                h = await modal_client.health(s, cfg)
                return web.json_response({"ok": True, "modal": h})  # 不回传 config(含 token)
            except Exception as e:
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
        cfg = cfg_mod.load_config()
        local_gpu = (cfg.get("default_gpu") or "H100")
        local_comfyui = node_sync.detect_local_comfyui_version()   # 当前本机 ComfyUI 版本
        deploy_comfyui = cfg.get("comfyui_version") or None         # 上次部署时检测到的版本
        deployed, deployed_gpu, reachable, err_kind = None, None, False, None
        # 快速单次直查(不走 health 的 3×10s 重试,避免点 Modal 卡 30s 无反应)。
        url = modal_client._endpoint(cfg["modal_endpoint_base"], "health")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
                async with s.get(url, headers={"X-Bridge-Key": modal_client._key(cfg)}) as r:
                    if r.status == 200:
                        h = await r.json(content_type=None)
                        if isinstance(h, dict):
                            deployed = h.get("deployed_version")
                            deployed_gpu = h.get("deployed_gpu")
                            reachable = True
                    elif r.status == 404:
                        err_kind = "not_deployed"  # endpoint 不存在 = app 没部署
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
