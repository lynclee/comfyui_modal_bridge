"""
routes.py — 本地 ComfyUI 服务器上的 HTTP 路由
所有路由前缀 /modal_bridge/...
"""
import asyncio
import base64
import os
import time
import uuid
from pathlib import Path

import aiohttp
import yaml
from aiohttp import web

from . import config as cfg_mod
from . import modal_client
from . import node_sync


# folder_paths 是 ComfyUI 全局模块
try:
    import folder_paths  # type: ignore
except Exception:
    folder_paths = None


# ── model_registry.yaml 缓存 ──
_REGISTRY_CACHE = None

def _load_registry() -> dict:
    """读 model_registry.yaml,返回 {filename: registry_entry}"""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    p = Path(__file__).resolve().parent / "model_registry.yaml"
    if not p.exists():
        _REGISTRY_CACHE = {}
        return _REGISTRY_CACHE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _REGISTRY_CACHE = data
        print(f"[modal_bridge] loaded {len(data)} models from registry")
    except Exception as e:
        print(f"[modal_bridge] load registry failed: {e}")
        _REGISTRY_CACHE = {}
    return _REGISTRY_CACHE


# ── 从 workflow prompt 解析需要的模型 ──
LOADER_MAP = {
    "CheckpointLoaderSimple":  ("checkpoints",     ["ckpt_name"]),
    "CheckpointLoader":        ("checkpoints",     ["ckpt_name"]),
    "UNETLoader":              ("diffusion_models",["unet_name"]),
    "DiffusionModelLoader":    ("diffusion_models",["model_name"]),
    "VAELoader":               ("vae",             ["vae_name"]),
    "CLIPLoader":              ("text_encoders",   ["clip_name"]),
    "DualCLIPLoader":          ("text_encoders",   ["clip_name1", "clip_name2"]),
    "TripleCLIPLoader":        ("text_encoders",   ["clip_name1", "clip_name2", "clip_name3"]),
    "CLIPVisionLoader":        ("clip_vision",     ["clip_name"]),
    "StyleModelLoader":        ("style_models",    ["style_model_name"]),
    "LoraLoader":              ("loras",           ["lora_name"]),
    "LoraLoaderModelOnly":     ("loras",           ["lora_name"]),
    "ControlNetLoader":        ("controlnet",      ["control_net_name"]),
    "UpscaleModelLoader":      ("upscale_models",  ["model_name"]),
    "GLIGENLoader":            ("gligen",          ["gligen_name"]),
    "PulidFluxModelLoader":    ("pulid",           ["pulid_file"]),
}

def extract_required_models(prompt: dict) -> list[dict]:
    """返回 [{type, filename}, ...] 去重"""
    deps: list[dict] = []
    seen: set[tuple] = set()
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type", "")
        spec = LOADER_MAP.get(cls)
        if not spec:
            continue
        type_, fields = spec
        ins = node.get("inputs") or {}
        for f in fields:
            name = ins.get(f)
            if isinstance(name, str) and name.strip():
                key = (type_, name)
                if key in seen:
                    continue
                seen.add(key)
                deps.append({"type": type_, "filename": name})
    return deps


def _input_dir() -> Path:
    if folder_paths:
        return Path(folder_paths.get_input_directory())
    return Path(__file__).resolve().parents[2] / "input"


def _output_dir() -> Path:
    if folder_paths:
        return Path(folder_paths.get_output_directory())
    return Path(__file__).resolve().parents[2] / "output"


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
    """跑一个命令,stdout/stderr 实时写进 resp(流式回前端),返回 returncode。
    找不到可执行文件返回 127。"""
    await _emit(resp, f"$ {' '.join(cmd)}\n")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        await _emit(resp, f"  ✗ 找不到可执行文件: {cmd[0]}\n")
        return 127
    assert proc.stdout is not None
    async for raw in proc.stdout:
        await _emit(resp, raw.decode("utf-8", errors="replace"))
    return await proc.wait()


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
        return web.json_response(cfg_mod.load_config())

    @routes.post("/modal_bridge/config")
    async def _set_config(request: web.Request):
        body = await request.json()
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be object"}, status=400)
        cur = cfg_mod.load_config()
        cur.update(body)
        cfg_mod.save_config(cur)
        return web.json_response(cur)

    # -------- 异步提交(返回 job_id,不阻塞)--------
    @routes.post("/modal_bridge/submit")
    async def _submit(request: web.Request):
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt (object) required"}, status=400)

        cfg = cfg_mod.load_config()
        gpu = body.get("gpu") or cfg.get("default_gpu", "H100")

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
                    input_images=input_images or None, gpu=gpu,
                )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

        job_id = submit_result.get("id")
        print(f"[modal_bridge] submitted job {job_id} (gpu={gpu}, refs={len(input_images)})")
        return web.json_response({
            "ok": True,
            "job_id": job_id,
            "gpu": submit_result.get("gpu") or gpu,
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
        return web.json_response(data)

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
        out_dir = _output_dir() / subfolder / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        b64 = final.get("data_base64")
        image_url = final.get("image_url")
        filename = final.get("filename") or "output.png"
        outputs = []

        if b64:
            try:
                data = base64.b64decode(b64)
            except Exception as e:
                return web.json_response({"error": f"decode base64 failed: {e}"}, status=502)
            (out_dir / filename).write_bytes(data)
            outputs.append({
                "filename": filename,
                "subfolder": f"{subfolder}/{job_id}",
                "type": "output",
                "size_bytes": len(data),
            })
        elif image_url:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(image_url) as r:
                        if r.status >= 400:
                            return web.json_response({"error": f"download {image_url} failed: {r.status}"}, status=502)
                        data = await r.read()
            except Exception as e:
                return web.json_response({"error": f"download failed: {e}"}, status=502)
            (out_dir / filename).write_bytes(data)
            outputs.append({
                "filename": filename,
                "subfolder": f"{subfolder}/{job_id}",
                "type": "output",
                "size_bytes": len(data),
                "source_url": image_url,
            })
        else:
            return web.json_response({"error": "no image in modal_state"}, status=502)

        print(f"[modal_bridge] ✓ job {job_id} fetched → {outputs[0]['subfolder']}/{outputs[0]['filename']}")
        return web.json_response({"ok": True, "job_id": job_id, "outputs": outputs})

    # -------- 模型相关 --------

    @routes.post("/modal_bridge/check_models")
    async def _check_models(request: web.Request):
        """
        body: {prompt: {...}}
        返回: {
          required: [{type, filename}],
          missing:  [{type, filename, registry_entry?}],
          present:  [{type, filename, size_mb}],
          unknown:  [{type, filename}],   # 缺失 && 不在 registry,无法自动下
        }
        """
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)

        required = extract_required_models(prompt)
        if not required:
            return web.json_response({"required": [], "missing": [], "present": [], "unknown": []})

        cfg = cfg_mod.load_config()
        async with aiohttp.ClientSession() as session:
            try:
                check_result = await modal_client.check_models(session, cfg, required)
            except Exception as e:
                return web.json_response({"error": f"check_models failed: {e}"}, status=502)

        registry = _load_registry()
        missing = check_result.get("missing", [])
        present = check_result.get("present", [])

        # 给 missing 配上 registry entry
        annotated_missing = []
        unknown = []
        for m in missing:
            entry = registry.get(m["filename"])
            if entry:
                annotated_missing.append({**m, "registry_entry": entry})
            else:
                unknown.append(m)

        return web.json_response({
            "required": required,
            "missing": annotated_missing,
            "present": present,
            "unknown": unknown,
        })

    @routes.post("/modal_bridge/seed_model")
    async def _seed_model(request: web.Request):
        """
        触发 Modal 下载某个模型。
        body: {type, filename, registry_entry}
        返回: Modal 的 {ok, downloaded?, cached?, path, size_mb, elapsed_sec?, error?}
        """
        body = await request.json()
        type_ = body.get("type")
        filename = body.get("filename")
        entry = body.get("registry_entry")
        if not type_ or not filename:
            return web.json_response({"error": "type + filename required"}, status=400)

        # 没传 registry_entry 时,自己查 registry
        if not entry:
            registry = _load_registry()
            entry = registry.get(filename)
            if not entry:
                return web.json_response(
                    {"error": f"{filename} 不在 model_registry.yaml 内,无法自动下"},
                    status=404,
                )

        item = {
            "type": type_,
            "filename": filename,
            "source": entry.get("source", "huggingface"),
            "repo": entry.get("repo"),
            "hf_filename": entry.get("hf_filename"),
            "hf_subfolder": entry.get("hf_subfolder"),
            "url": entry.get("url"),
            "civitai_id": entry.get("civitai_id"),
            "requires_token": entry.get("requires_token", False),
        }

        cfg = cfg_mod.load_config()
        print(f"[modal_bridge] seed_model: {type_}/{filename} (source={entry.get('source')})")
        async with aiohttp.ClientSession() as session:
            try:
                result = await modal_client.seed_model(session, cfg, item)
            except asyncio.TimeoutError:
                return web.json_response({"error": "seed_model timed out"}, status=504)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=502)
        return web.json_response(result)

    @routes.get("/modal_bridge/seed_status")
    async def _seed_status(request: web.Request):
        type_ = request.query.get("type")
        filename = request.query.get("filename")
        if not type_ or not filename:
            return web.json_response({"error": "type + filename required"}, status=400)
        cfg = cfg_mod.load_config()
        async with aiohttp.ClientSession() as session:
            try:
                return web.json_response(await modal_client.seed_status(session, cfg, type_, filename))
            except Exception as e:
                return web.json_response({"error": str(e)}, status=502)

    @routes.get("/modal_bridge/list_models")
    async def _list_models(request: web.Request):
        type_ = request.query.get("type")
        cfg = cfg_mod.load_config()
        async with aiohttp.ClientSession() as session:
            try:
                return web.json_response(await modal_client.list_models(session, cfg, type_))
            except Exception as e:
                return web.json_response({"error": str(e)}, status=502)

    # -------- custom_node 同步 --------

    @routes.post("/modal_bridge/check_nodes")
    async def _check_nodes(request: web.Request):
        """
        检查工作流用到的 custom_node 是否都在 Modal 镜像里。全部本地解析,瞬时。
        baked 清单优先用 Modal /list-nodes(权威,反映真实已部署镜像),不可达时回退本地数据文件。
        body: {prompt}
        返回: {ok, source, missing[], missing_no_git[], unresolved[], ok_builtin, ok_baked}
        """
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt required"}, status=400)

        cfg = cfg_mod.load_config()
        baked_names = None
        source = "local"
        try:
            async with aiohttp.ClientSession() as session:
                nodes_info = await modal_client.list_nodes(session, cfg)
            if isinstance(nodes_info, dict) and isinstance(nodes_info.get("custom_nodes"), list):
                baked_names = set(nodes_info["custom_nodes"])
                source = "modal"
        except Exception as e:
            print(f"[modal_bridge] check_nodes: /list-nodes 不可达,回退本地清单 ({e})")

        result = node_sync.find_missing_nodes(prompt, baked_names=baked_names)
        result["ok"] = True
        result["source"] = source
        return web.json_response(result)

    @routes.post("/modal_bridge/add_nodes")
    async def _add_nodes(request: web.Request):
        """
        把缺失 custom_node 加进 Modal 镜像清单并重部署。stream 回传 modal deploy 日志。
        body: {nodes: [{folder, url, commit}]}
        最后一行: __DEPLOY_DONE__ rc=<code>
        """
        body = await request.json()
        entries = body.get("nodes")
        if not isinstance(entries, list) or not entries:
            return web.json_response({"error": "nodes (non-empty list) required"}, status=400)

        # folder → name(clone 出来的目录名必须和本地一致,ComfyUI 才认)
        new_entries = []
        for e in entries:
            folder = e.get("folder") or e.get("name")
            if not folder or not e.get("url"):
                continue
            new_entries.append({"name": folder, "url": e["url"], "commit": e.get("commit", "")})
        if not new_entries:
            return web.json_response({"error": "no valid {folder,url} entries"}, status=400)

        merge = node_sync.add_baked_nodes(new_entries)
        print(f"[modal_bridge] add_nodes: baked +{merge['added']} (skipped {merge['skipped']})")

        cfg = cfg_mod.load_config()
        cwd = str(node_sync.MODAL_APP_DIR)

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)

        await _emit(resp, f"== adding nodes: {[n['name'] for n in new_entries]} ==\n")
        await _emit(resp, f"== merged into image list: added={merge['added']} skipped={merge['skipped']} ==\n")
        await _emit(resp, "== 首次 clone 新节点 + 装依赖,约 1-3 分钟,别关窗口 ==\n\n")

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
        token_id = (body.get("token_id") or "").strip()
        token_secret = (body.get("token_secret") or "").strip()
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
            errs.append("token_secret 应以 as- 开头")
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
        scaledown = int(body.get("scaledown_window") or cfg.get("scaledown_window") or 120)
        hf_token = (body.get("hf_token") or "").strip()
        civitai_token = (body.get("civitai_token") or "").strip()
        endpoint_base = f"https://{workspace}--{app_name}"
        # 私有鉴权 key:已有就复用(不让旧 config 失效),否则新生成
        bridge_key = cfg.get("bridge_api_key") or node_sync.gen_bridge_key()

        # 合并出完整 config(用于 deploy_env + 最终落盘)
        cfg.update({
            "modal_endpoint_base": endpoint_base,
            "modal_app_name": app_name,
            "modal_workspace": workspace,
            "modal_volume_name": volume_name,
            "scaledown_window": scaledown,
            "default_gpu": default_gpu,
            "modal_token_id": token_id,
            "modal_token_secret": token_secret,
            "bridge_api_key": bridge_key,
        })
        env = node_sync.deploy_env(cfg)
        cwd = str(node_sync.MODAL_APP_DIR)

        await _emit(resp, "== Modal 一键部署 ==\n")
        await _emit(resp, f"   workspace={workspace}  app={app_name}  gpu={default_gpu}\n")
        await _emit(resp, f"   endpoint={endpoint_base}\n")
        await _emit(resp, f"   HF token={'已填' if hf_token else '空(只能下公开模型)'}\n\n")

        # 1) modal 包
        rc = await _ensure_modal(resp)
        if rc != 0:
            await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
            await resp.write_eof()
            return resp

        # 2) 建 / 更新 secret(放 HF / Civitai token)
        await _emit(resp, "\n== 创建 Modal Secret ==\n")
        rc = await _run_streamed(
            resp, node_sync.secret_create_cmd(cfg, hf_token, civitai_token, bridge_key), cwd=cwd, env=env,
        )
        if rc != 0:
            await _emit(resp, "== ✗ secret 创建失败(token 可能无效)==\n")
            await _emit(resp, f"\n__DEPLOY_DONE__ rc={rc}\n")
            await resp.write_eof()
            return resp

        # 3) 部署 app(首次拉镜像 3-5 分钟)
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

        # 5) 验证 health
        try:
            async with aiohttp.ClientSession() as s:
                h = await modal_client.health(s, cfg)
            await _emit(resp, f"== ✓ /health: {h} ==\n")
        except Exception as e:
            await _emit(resp, f"== ⚠ /health 暂不可达(endpoint 可能还在初始化,稍后重试):{e} ==\n")

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
                return web.json_response({"ok": True, "modal": h, "config": cfg})
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e), "config": cfg}, status=502)

    # -------- 主入口:提交工作流 --------
    @routes.post("/modal_bridge/queue")
    async def _queue(request: web.Request):
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return web.json_response({"error": "prompt (object) required"}, status=400)

        gpu_override = body.get("gpu")
        cfg = cfg_mod.load_config()
        gpu = gpu_override or cfg.get("default_gpu", "H100")

        # 1) 解析 prompt 找 LoadImage,准备 base64
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

        # 2) 提交 + 轮询
        t_start = time.time()
        job_id = None
        try:
            timeout = aiohttp.ClientTimeout(total=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                submit_result = await modal_client.submit_job(
                    session, cfg, workflow=prompt,
                    input_images=input_images or None, gpu=gpu,
                )
                job_id = submit_result.get("id")
                print(f"[modal_bridge] submitted job {job_id} (gpu={gpu})")

                def _on_progress(status, data):
                    print(f"[modal_bridge] job {job_id} status={status}")

                final = await modal_client.poll_status(session, cfg, job_id, on_progress=_on_progress)
        except Exception as e:
            return web.json_response(
                {"error": str(e), "job_id": job_id, "elapsed_sec": round(time.time() - t_start, 1)},
                status=502,
            )

        elapsed = round(time.time() - t_start, 1)

        # 3) 处理结果
        status = final.get("status")
        if status != "completed":
            return web.json_response(
                {
                    "error": f"Modal job ended with status={status}",
                    "modal_state": final,
                    "job_id": job_id,
                    "elapsed_sec": elapsed,
                },
                status=502,
            )

        # 4) 写出 base64 → output/modal_results/<job_id>/output.png
        subfolder = cfg.get("output_subfolder", "modal_results")
        out_dir = _output_dir() / subfolder / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs = []

        b64 = final.get("data_base64")
        image_url = final.get("image_url")
        filename = final.get("filename") or "output.png"

        if b64:
            try:
                data = base64.b64decode(b64)
            except Exception as e:
                return web.json_response({"error": f"decode base64 failed: {e}", "modal_state": final}, status=502)
            out_path = out_dir / filename
            out_path.write_bytes(data)
            outputs.append({
                "filename": filename,
                "subfolder": f"{subfolder}/{job_id}",
                "type": "output",
                "size_bytes": len(data),
            })
        elif image_url:
            # 非 incognito 情况下,Modal 会返回 R2 URL — 下载下来
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(image_url) as r:
                        if r.status >= 400:
                            return web.json_response({"error": f"download {image_url} failed: {r.status}"}, status=502)
                        data = await r.read()
            except Exception as e:
                return web.json_response({"error": f"download failed: {e}"}, status=502)
            out_path = out_dir / filename
            out_path.write_bytes(data)
            outputs.append({
                "filename": filename,
                "subfolder": f"{subfolder}/{job_id}",
                "type": "output",
                "size_bytes": len(data),
                "source_url": image_url,
            })
        else:
            return web.json_response({"error": "no image returned", "modal_state": final}, status=502)

        print(f"[modal_bridge] ✓ job {job_id} done in {elapsed}s → {outputs[0]['subfolder']}/{outputs[0]['filename']}")

        return web.json_response({
            "ok": True,
            "job_id": job_id,
            "gpu": final.get("gpu") or gpu,
            "elapsed_sec": elapsed,
            "outputs": outputs,
        })


_setup_routes()
