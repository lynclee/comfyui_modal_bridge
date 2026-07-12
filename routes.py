"""
routes.py — 本地 ComfyUI 服务器上的 HTTP 路由
所有路由前缀 /modal_bridge/...
"""
import asyncio
import base64
import os
import subprocess
from pathlib import Path

import aiohttp
from aiohttp import web

from . import categories
from . import config as cfg_mod
from . import contract
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
            # 1) ComfyUI 官方解析(认 extra_model_paths.yaml 的所有根,最权威);别名类型逐个试
            for t in search_types:
                try:
                    full = folder_paths.get_full_path(t, filename)
                    if full:
                        return Path(full)
                except Exception:
                    pass
            for t in search_types:
                try:
                    roots += folder_paths.get_folder_paths(t) or []
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
    """估工作流显存需求(GB)+ 类别 + 本地查不到大小的模型数。供自动选档 / 预警端点复用。"""
    resolver = _local_model_resolver()
    total_bytes, unknown = 0, 0
    for m in extract_required_models(prompt):
        p = resolver(m["type"], m["filename"])
        try:
            if p and Path(p).exists():
                total_bytes += Path(p).stat().st_size
            else:
                unknown += 1
        except OSError:
            unknown += 1
    category = categories.classify(prompt)
    est = categories.estimate_vram_gb(total_bytes / (1024 ** 3), category)
    return est, category, unknown


def _pick_gpu_class(prompt: dict, cfg: dict) -> tuple[str, str]:
    """按估算显存在 GPU 档梯子上选档,返回 (gpu_class, reason)。gpu_class ∈ {'cheap','primary','top'}。
    auto_downgrade 关 = 「H100/B200 固定」模式:一律 primary,不降也不升(>主卡容量会在前端预警)。
    auto_downgrade 开 = 「Auto(更省钱)」模式,走梯子(成本低→高 L40S→H100→B200):
      1) 升档(防 OOM):估算 > 主卡容量 → top(B200 183G)。
      2) 降档(省钱):cheap≠主卡 + 非视频 + 大小已知 + 放得下便宜卡 → cheap(L40S)。
      3) 否则 → primary(H100)。
    本地查不到大小(unknown>0)时估算不可信:不升不降,留 primary(稳妥)。"""
    if not cfg.get("auto_downgrade", True):
        return "primary", f"{(cfg.get('default_gpu') or 'H100').strip()} 固定模式"
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


async def _write_results(final: dict, job_id: str, subfolder: str, cfg: dict) -> list:
    """把 Modal 返回的产物写到 output/<subfolder>/<job_id>/,返回 outputs 列表。
    每个产物二选一:小文件 data_base64(解码落盘);大文件 volume_path(从 Volume 直连下载落盘)。
    否则回退单图 data_base64 / image_url。写失败 raise(由调用方转 502)。"""
    out_dir = _output_dir() / subfolder / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs, seen = [], set()

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
                # 大文件:从 Volume 直连下载(不走 base64/Dict),下完删 Volume 上的副本
                try:
                    size = await asyncio.to_thread(modal_volume.download_volume_file, cfg, vp, str(local))
                except Exception as e:
                    raise RuntimeError(f"volume download {vp} failed: {e}")
                await asyncio.to_thread(modal_volume.remove_volume_path, cfg, vp)
            else:
                data = base64.b64decode(b64)
                local.write_bytes(data)
                size = len(data)
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
        data = base64.b64decode(b64)
        (out_dir / fn).write_bytes(data)
        outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                        "type": "output", "size_bytes": len(data)})
    elif image_url:
        async with aiohttp.ClientSession() as s:
            async with s.get(image_url) as r:
                if r.status >= 400:
                    raise RuntimeError(f"download {image_url} failed: {r.status}")
                data = await r.read()
        (out_dir / fn).write_bytes(data)
        outputs.append({"filename": fn, "subfolder": f"{subfolder}/{job_id}",
                        "type": "output", "size_bytes": len(data), "source_url": image_url})
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
    """读 input/<name>,返回 Modal 期望的 {name, image (data uri)} 格式。"""
    p = _input_dir() / name
    if not p.exists():
        raise FileNotFoundError(f"Input image not found locally: {p}")
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


async def _run_streamed(resp: web.StreamResponse, cmd: list[str], cwd: str, env: dict) -> int:
    """跑一个命令,stdout/stderr 实时流式回前端,返回 returncode(找不到可执行文件返回 127)。
    用线程 + subprocess.Popen(不走 asyncio 子进程)——避免 Windows 上事件循环不支持
    子进程(SelectorEventLoop → NotImplementedError)的坑,Mac/Linux/Win 一致。"""
    await _emit(resp, f"$ {' '.join(cmd)}\n")

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
            emit(line)
        proc.wait()
        return proc.returncode

    return await _run_blocking_streamed(resp, work)


_STREAM_SENTINEL = object()

# 模型上传串行化:同一时刻只允许一个 /sync_models 真正上传,避免并发工作流同时往
# Volume 写同一个大模型撞车(用户实测 35GB flux2 dev 并发上传会失败)。
_UPLOAD_LOCK = asyncio.Lock()

# 部署串行化:写 _custom_nodes_data.py + modal deploy 这段必须独占——两个并发请求
# (/sync_nodes 之间、或 /sync_nodes 与 /deploy)同时写清单会互相覆盖、两个 modal deploy
# 打同一个 app 也会冲突。整段(写文件 + deploy)包进同一把锁。
_DEPLOY_LOCK = asyncio.Lock()

# poll 记日志用:job_id → 上次见到的 status(只在变化时打日志,避免高频 poll 刷屏)
_LAST_POLL_STATUS: dict = {}


async def _run_blocking_streamed(resp: web.StreamResponse, fn):
    """在线程里跑一个阻塞函数 fn(emit),emit(line) 线程安全地把日志流式写回 resp。
    返回 fn 的返回值。用于 Volume 上传这种阻塞 + 想要实时进度的场景。"""
    loop = asyncio.get_event_loop()
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
    """确保 ComfyUI 内嵌 Python 里有 modal 包,缺则 pip 装。返回 0=可用。"""
    if node_sync.modal_available():
        await _emit(resp, "== modal 包已就绪 ==\n")
        return 0
    await _emit(resp, "== 未检测到 modal 包,正在装到 ComfyUI 内嵌 Python(约 30s)==\n")
    rc = await _run_streamed(resp, node_sync.pip_install_modal_cmd(), cwd=str(node_sync._HERE), env=os.environ.copy())
    if rc != 0:
        await _emit(resp, "== ✗ pip install modal 失败 ==\n")
        return rc
    if not node_sync.modal_available():
        await _emit(resp, "== ✗ 装完仍 import 不到 modal ==\n")
        return 1
    await _emit(resp, "== ✓ modal 安装完成 ==\n")
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
        cfg = dict(cfg_mod.load_config())
        cfg["has_token_secret"] = bool(cfg.get("modal_token_secret"))
        cfg["has_comfy_api_key"] = bool(cfg.get("comfy_api_key"))
        cfg["has_aigc_bypass_secret"] = bool(cfg.get("aigc_bypass_secret"))
        cfg.pop("modal_token_secret", None)
        cfg.pop("bridge_api_key", None)
        cfg.pop("comfy_api_key", None)  # 账单凭据,不回吐浏览器(同 bridge_api_key)
        cfg.pop("aigc_bypass_secret", None)  # Vercel 旁路密钥,同上
        return web.json_response(cfg)

    @routes.get("/modal_bridge/bridge_key")
    async def _bridge_key(request: web.Request):
        """仅本机:导出脚本「嵌入 KEY」时取回自己的 bridge_api_key。
        /config 故意抹掉 key 不回吐浏览器;这里单独给(同机、owner 自己的 key,显式动作才调)。"""
        cfg = cfg_mod.load_config()
        return web.json_response({"key": cfg.get("bridge_api_key", "")})

    @routes.post("/modal_bridge/config")
    async def _set_config(request: web.Request):
        body = await request.json()
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        cur = cfg_mod.load_config()
        cur.update(body)
        cfg_mod.save_config(cur)
        # 不回吐密钥(和 GET /config 一致):抹掉 token_secret / bridge_api_key
        safe = dict(cur)
        safe["has_token_secret"] = bool(safe.get("modal_token_secret"))
        safe["has_comfy_api_key"] = bool(safe.get("comfy_api_key"))
        safe["has_aigc_bypass_secret"] = bool(safe.get("aigc_bypass_secret"))
        safe.pop("modal_token_secret", None)
        safe.pop("bridge_api_key", None)
        safe.pop("comfy_api_key", None)
        safe.pop("aigc_bypass_secret", None)
        return web.json_response(safe)

    # -------- 异步提交(返回 job_id,不阻塞)--------
    @routes.post("/modal_bridge/submit")
    async def _submit(request: web.Request):
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt (object) required"}, status=400)

        cfg = cfg_mod.load_config()
        tier = (body.get("tier") or "40g").lower()
        # 工作流无本地模型节点(纯 API / 轻节点)= 不需要 GPU → 路由到 CPU worker(账单≈0);否则 GPU worker。
        needs_gpu = bool(extract_required_models(prompt))
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

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                submit_result = await modal_client.submit_job(
                    session, cfg, workflow=prompt,
                    input_images=input_images or None, tier=tier, needs_gpu=needs_gpu,
                    gpu_class=gpu_class,
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
        })

    # -------- 轮询单次状态(前端高频调用,显示进度)--------
    @routes.get("/modal_bridge/poll")
    async def _poll(request: web.Request):
        job_id = request.query.get("job_id")
        if not job_id:
            return web.json_response({"error": "job_id required"}, status=400)

        cfg = cfg_mod.load_config()
        url = modal_client._endpoint(cfg["modal_endpoint_base"], "status")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(url, params={"job_id": job_id, "key": modal_client._key(cfg)}) as r:
                    data = await r.json(content_type=None)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        # 只在 status 变化时记日志(poll 高频,避免刷屏);终态 failed 把 error 也记上。
        # 这样即使前端超时/放弃,ComfyUI 日志里也能看到 job 走到了哪一步、为何失败。
        st = data.get("status") if isinstance(data, dict) else None
        if st and _LAST_POLL_STATUS.get(job_id) != st:
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
    @routes.post("/modal_bridge/fetch_result")
    async def _fetch_result(request: web.Request):
        body = await request.json()
        job_id = body.get("job_id")
        final = body.get("modal_state")  # 前端 poll 拿到的最终状态对象
        if not job_id or not isinstance(final, dict):
            return web.json_response({"error": "job_id + modal_state required"}, status=400)

        cfg = cfg_mod.load_config()
        subfolder = cfg.get("output_subfolder", "modal_results")
        try:
            outputs = await _write_results(final, job_id, subfolder, cfg)
        except Exception as e:
            return web.json_response({"error": f"write result failed: {e}"}, status=502)
        if not outputs:
            return web.json_response({"error": "no image in modal_state"}, status=502)

        print(f"[modal_bridge] ✓ job {job_id} fetched {len(outputs)} img → {subfolder}/{job_id}/")
        return web.json_response({"ok": True, "job_id": job_id, "outputs": outputs})

    # -------- 模型同步(本地 → Volume,全程本地 modal SDK,不经 endpoint)--------

    @routes.post("/modal_bridge/check_models")
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
                {"error": "本地没装 modal(先点 [Modal Setup] 部署一次,会自动装 modal)"}, status=400)

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

    @routes.post("/modal_bridge/check_required_inputs")
    async def _check_required_inputs(request: web.Request):
        """提交前预检:按当前本地节点定义,找出 prompt 里「缺必填输入」的节点。
        body: {prompt}  返回: {missing:[{node_id,class_type,missing:[...]}]}
        典型拦截:老工作流缺新版节点新增的必填 widget(如 API 节点 generate_type),
        避免等云端 `execute() missing required argument` 才报错。拿不到定义的节点跳过,不误报。"""
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)
        missing = workflow_check.find_missing_required_inputs(prompt, _node_required_inputs)
        return web.json_response({"missing": missing})

    @routes.post("/modal_bridge/estimate_vram")
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
        total_bytes, known, unknown = 0, 0, []
        for m in required:
            p = resolver(m["type"], m["filename"])
            try:
                if p and Path(p).exists():
                    total_bytes += Path(p).stat().st_size
                    known += 1
                else:
                    unknown.append(f"{m['type']}/{m['filename']}")
            except OSError:
                unknown.append(f"{m['type']}/{m['filename']}")
        # 按类别(image/video/…)估显存:视频权重小但多帧激活大,系数+开销见 categories.py。
        category = categories.classify(prompt)
        model_gb = total_bytes / (1024 ** 3)
        return web.json_response({
            "total_mb": total_bytes // 1024 // 1024,
            "known_count": known,
            "required_count": len(required),
            "unknown": unknown,
            "category": category,
            "est_vram_gb": round(categories.estimate_vram_gb(model_gb, category), 1),
        })

    @routes.post("/modal_bridge/sync_models")
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

    # -------- custom_node 双向同步 --------

    @routes.get("/modal_bridge/list_nodes")
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
        result["ok"] = True
        result["source"] = source
        return web.json_response(result)

    @routes.post("/modal_bridge/sync_nodes")
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
        await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
        await resp.write_eof()
        return resp

    @routes.post("/modal_bridge/deploy")
    async def _deploy(request: web.Request):
        """
        GUI 一键部署/重新部署:pip 装 modal → 建 secret → modal deploy → 写 config(路径必对)。
        全程在 ComfyUI 进程里,零终端。stream 回传日志,最后 __DEPLOY_DONE__ rc=<code>。
        body: {token_id, token_secret, workspace, hf_token?, civitai_token?,
               app_name?, volume_name?, default_gpu?, scaledown_window?}
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
        scaledown = int(body.get("scaledown_window") or cfg.get("scaledown_window") or 40)
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
        aigc_bypass = (body.get("aigc_bypass_secret") or "").strip() or cfg.get("aigc_bypass_secret", "")
        endpoint_base = f"https://{workspace}--{app_name}"
        # 私有鉴权 key:已有就复用(不让旧 config 失效),否则新生成
        bridge_key = cfg.get("bridge_api_key") or node_sync.gen_bridge_key()

        # ComfyUI 版本跟随本机:检测本机版本 → 解析云端 clone tag(无对应取最接近,只警告不中止)
        comfyui_version = node_sync.detect_local_comfyui_version()
        _tags = await asyncio.get_event_loop().run_in_executor(None, node_sync.list_comfyui_tags)
        comfyui_tag, _tag_note = node_sync.resolve_comfyui_tag(comfyui_version, _tags)

        # 合并出完整 config(用于 deploy_env + 最终落盘)
        cfg.update({
            "modal_endpoint_base": endpoint_base,
            "modal_app_name": app_name,
            "modal_workspace": workspace,
            "modal_volume_name": volume_name,
            "scaledown_window": scaledown,
            "default_gpu": default_gpu,
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
                async with s.get(url, params={"key": modal_client._key(cfg)}) as r:
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
            err_kind = "timeout"   # 超时 = 多半 Modal 平台故障 / 冷启动慢
            print("[modal_bridge] version check: health 超时(可能 Modal 平台故障,查 status.modal.com)")
        except Exception as e:
            err_kind = "unreachable"
            print(f"[modal_bridge] version check: health 不可达 ({e})")
        # 契约计算抽到 contract.compute_contract(纯函数,有单测)。
        c = contract.compute_contract(local, deployed, reachable, local_gpu, deployed_gpu,
                                      local_comfyui=local_comfyui, deploy_comfyui=deploy_comfyui)
        return web.json_response({"ok": True, "err_kind": err_kind, **c})


_setup_routes()
