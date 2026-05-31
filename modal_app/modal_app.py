"""
Modal app — comfyui_modal_bridge 自带的独立 worker

部署:
    modal deploy modal_app/modal_app.py

部署后会拿到这些长期 URL(<workspace> 由你的 modal 账号决定):
    https://<ws>--comfyui-bridge-run.modal.run          (POST,跑 workflow)
    https://<ws>--comfyui-bridge-status.modal.run       (GET,查状态)
    https://<ws>--comfyui-bridge-cancel.modal.run       (POST,取消)
    https://<ws>--comfyui-bridge-health.modal.run       (GET,健康)
    https://<ws>--comfyui-bridge-list-models.modal.run  (GET,列 Volume 模型)
    https://<ws>--comfyui-bridge-check-models.modal.run (POST,批量检查模型)
    https://<ws>--comfyui-bridge-seed-model.modal.run   (POST,下载模型到 Volume)
    https://<ws>--comfyui-bridge-seed-status.modal.run  (GET,查模型下载进度)
    (health 顺带返回镜像已装 custom_nodes,省一个 endpoint;Starter plan 限 8 个)

需要:
- Modal Secret  `comfyui-bridge-secrets`(包含 HF_TOKEN,可选)
- Modal Volume  `comfyui-bridge-models`(自动创建)
"""
import os
import subprocess
import time
import uuid
from pathlib import Path

import modal

from modal_image import cuda_image


# ============================================================================
# Modal 资源
# ============================================================================
APP_NAME = os.environ.get("MODAL_BRIDGE_APP_NAME", "comfyui-bridge")
VOLUME_NAME = os.environ.get("MODAL_BRIDGE_VOLUME", "comfyui-bridge-models")
SECRET_NAME = os.environ.get("MODAL_BRIDGE_SECRET", "comfyui-bridge-secrets")
DEFAULT_GPU = os.environ.get("MODAL_BRIDGE_DEFAULT_GPU", "H100")
SCALEDOWN = int(os.environ.get("MODAL_BRIDGE_SCALEDOWN", "120"))

app = modal.App(APP_NAME)
models_vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# secrets 包含:HF_TOKEN(可选,下私有模型)+ CIVITAI_TOKEN(预留)
# 如果用户没创建,from_name 会报错 — 提示去 deploy.py
try:
    bridge_secret = modal.Secret.from_name(SECRET_NAME)
except Exception:
    bridge_secret = modal.Secret.from_dict({})  # 空 secret,不报错

job_state = modal.Dict.from_name(f"{APP_NAME}-jobs", create_if_missing=True)
seed_state = modal.Dict.from_name(f"{APP_NAME}-seeds", create_if_missing=True)  # 下载任务状态

_ALLOWED_GPU = {"A10G", "L40S", "A100", "A100-80GB", "H100"}


# ============================================================================
# ComfyUI worker
# ============================================================================
# 两档 worker:gpu 用 list(@app.cls 定义时支持 Modal 原生 fallback——按顺序排不到就降级)。
# boot/run 逻辑提取为模块函数,两个 class 共享,只 GPU 档不同。
_WORKER_KW = dict(
    image=cuda_image,
    volumes={"/comfy-volume": models_vol},
    secrets=[bridge_secret],
    scaledown_window=SCALEDOWN,
    timeout=900,
    min_containers=0,
    max_containers=10,
)


def _worker_boot(self):
    models_vol.reload()  # 启动前同步 Volume(此时 ComfyUI 还没打开文件,不冲突)
    self.proc = subprocess.Popen([
        "python", "/comfyui/main.py",
        "--listen", "127.0.0.1", "--port", "8188",
        "--extra-model-paths-config", "/comfyui/extra_model_paths.yaml",
    ])
    from _comfy_ws import wait_comfy_ready
    wait_comfy_ready(timeout_s=180)
    print("[bridge] ComfyUI ready")


def _worker_shutdown(self):
    try:
        self.proc.terminate()
    except Exception:
        pass


def _worker_run(workflow: dict, job_id: str, input_images: list | None = None) -> dict:
    cur: dict = {}
    for _ in range(100):
        cur = job_state.get(job_id, {})
        if cur.get("call_id"):
            break
        time.sleep(0.1)
    job_state[job_id] = {**cur, "status": "running", "started_at": time.time()}
    try:
        # Volume 已在 boot() reload 过;这里不能再 reload(ComfyUI 已打开文件会冲突)
        from _comfy_ws import run_workflow
        result = run_workflow(workflow=workflow, job_id=job_id, input_images=input_images)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        job_state[job_id] = {**job_state.get(job_id, {}), "status": "failed",
                             "error": str(e), "trace": tb[-2000:], "completed_at": time.time()}
        raise
    job_state[job_id] = {**job_state.get(job_id, {}), "status": "completed",
                         "data_base64": result.get("data_base64"), "image_url": result.get("image_url"),
                         "filename": result.get("filename"), "completed_at": time.time()}
    return result


# 80G 档(flux2 dev 等大模型):H100 优先,排不到降 A100-80GB
@app.cls(gpu=["H100", "A100-80GB"], **_WORKER_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorker80G:
    @modal.enter()
    def boot(self):
        _worker_boot(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None) -> dict:
        return _worker_run(workflow, job_id, input_images)


# 40G 档(z-image / flux2-klein 等):L40S 优先(便宜易分配),排不到升 H100
@app.cls(gpu=["L40S", "H100"], **_WORKER_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorker40G:
    @modal.enter()
    def boot(self):
        _worker_boot(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None) -> dict:
        return _worker_run(workflow, job_id, input_images)


# 显存档 → (worker class, GPU 优先级展示文案)
_TIER_WORKERS = {"80g": ComfyWorker80G, "40g": ComfyWorker40G}
_TIER_GPU_DISPLAY = {"80g": "H100→A100-80G", "40g": "L40S→H100"}


# ============================================================================
# 鉴权 — 自建 API key(private endpoint)
#
# 为什么不用 Modal 的 requires_proxy_auth:那个要单独的 Proxy Auth Token,只能在
# dashboard 手动建,没法程序化 → 破坏「用户只填 account token」。改成自建 key:
# 部署时随机生成 BRIDGE_API_KEY 存进 Modal Secret + 本地 config,每个 endpoint 校验。
# key 经 query(GET)/ body 字段 auth_key(POST)传入(顶层签名不碰 fastapi,
# 否则本地无 fastapi 的环境 `modal deploy` 会挂);拒绝时函数体内 import fastapi 返 401。
# ============================================================================
def _check(key: str):
    """授权返回 None;未授权返回 JSONResponse(401)(函数体内 import,仅容器执行)。"""
    expected = os.environ.get("BRIDGE_API_KEY", "")
    if expected and key == expected:
        return None
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "unauthorized — bad or missing bridge key"}, status_code=401)


# ============================================================================
# REST endpoints
# ============================================================================

@app.function(image=cuda_image, secrets=[bridge_secret], timeout=60)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-run")
def run_endpoint(payload: dict):
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    """提交 workflow。payload: {workflow, gpu?, images?, incognito?}"""
    job_id = payload.get("job_id") or str(uuid.uuid4())
    workflow = payload.get("workflow")
    if not workflow:
        return {"error": "Missing 'workflow' in payload"}
    input_images = payload.get("images")
    # 按显存档选预定义 worker(每档 @app.cls 自带 GPU 原生 fallback list,排不到自动降级)。
    # 前端按工作流总显存需求判断 tier 传入;默认 40g。
    tier = (payload.get("tier") or "40g").lower()
    if tier not in _TIER_WORKERS:
        tier = "40g"
    worker = _TIER_WORKERS[tier]
    gpu_display = _TIER_GPU_DISPLAY[tier]

    job_state[job_id] = {"status": "queued", "queued_at": time.time(), "gpu": gpu_display, "tier": tier}
    call = worker().run.spawn(workflow, job_id, input_images)
    job_state[job_id] = {**job_state.get(job_id, {}), "call_id": call.object_id}
    return {"id": job_id, "status": "queued", "gpu": gpu_display}


@app.function(image=cuda_image, secrets=[bridge_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-status")
def status_endpoint(job_id: str, key: str = ""):
    deny = _check(key)
    if deny:
        return deny
    s = job_state.get(job_id)
    if not s:
        return {"error": "job not found", "id": job_id}
    return {"id": job_id, **s}


@app.function(image=cuda_image, secrets=[bridge_secret], timeout=15)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-cancel")
def cancel_endpoint(payload: dict):
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    job_id = payload.get("job_id")
    if not job_id:
        return {"error": "Missing 'job_id'"}
    s = job_state.get(job_id) or {}
    was_running = s.get("status") == "running"
    call_id = s.get("call_id")
    if call_id:
        try:
            modal.FunctionCall.from_id(call_id).cancel(terminate_containers=was_running)
        except Exception as e:
            print(f"[bridge] cancel call {call_id}: {e}")
    job_state[job_id] = {**s, "status": "cancelled", "completed_at": time.time()}
    return {"id": job_id, "status": "cancelled", "was_running": was_running}


@app.function(image=cuda_image, secrets=[bridge_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-health")
def health_endpoint(key: str = ""):
    deny = _check(key)
    if deny:
        return deny
    info: dict = {"healthy": True, "app": APP_NAME, "volume": VOLUME_NAME}
    try:
        warm = 0
        for cls_name in ("ComfyWorker80G", "ComfyWorker40G"):
            try:
                stats = modal.Cls.from_name(APP_NAME, cls_name)().run.get_current_stats()
                warm += getattr(stats, "num_total_runners", 0) or 0
            except Exception:
                pass
        info["warm_containers"] = warm
    except Exception as e:
        info["stats_error"] = str(e)
    # 镜像已装的 custom_nodes(并入 health:Starter plan 限 8 个 web endpoint,省一个)
    try:
        cn_dir = Path("/comfyui/custom_nodes")
        info["custom_nodes"] = sorted(
            p.name for p in cn_dir.iterdir()
            if p.is_dir() and not p.name.startswith((".", "__"))
        ) if cn_dir.exists() else []
    except Exception as e:
        info["custom_nodes_error"] = str(e)
    return info


# ============================================================================
# 模型管理 endpoint
# ============================================================================

@app.function(image=cuda_image, volumes={"/comfy-volume": models_vol}, secrets=[bridge_secret], timeout=30)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-list-models")
def list_models_endpoint(type: str = None, key: str = ""):
    """列出 Volume 内的模型,可指定 type。返回 {type: [filename, ...], ...}"""
    deny = _check(key)
    if deny:
        return deny
    models_vol.reload()
    base = Path("/comfy-volume/models")
    base.mkdir(parents=True, exist_ok=True)
    result = {}
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        if type and sub.name != type:
            continue
        result[sub.name] = sorted([p.name for p in sub.iterdir() if p.is_file()])
    return result


@app.function(image=cuda_image, volumes={"/comfy-volume": models_vol}, secrets=[bridge_secret], timeout=30)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-check-models")
def check_models_endpoint(payload: dict):
    """
    批量检查多个模型在 Volume 里是否存在。
    payload: {"required": [{"type": "diffusion_models", "filename": "xxx.safetensors"}, ...]}
    返回:   {"missing": [...], "present": [...]}
    """
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    models_vol.reload()
    base = Path("/comfy-volume/models")
    required = payload.get("required", [])
    missing, present = [], []
    for item in required:
        type_ = item.get("type")
        filename = item.get("filename")
        if not type_ or not filename:
            continue
        p = base / type_ / filename
        if p.exists():
            present.append({"type": type_, "filename": filename, "size_mb": p.stat().st_size // 1024 // 1024})
        else:
            missing.append({"type": type_, "filename": filename})
    return {"missing": missing, "present": present, "total_required": len(required)}


@app.function(
    image=cuda_image,
    volumes={"/comfy-volume": models_vol},
    secrets=[bridge_secret],
    timeout=3600,  # 1 小时,足够下大模型
)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-seed-model")
def seed_model_endpoint(payload: dict):
    """
    下载一个模型到 Volume。同步执行,客户端等返回结果。

    payload: {
      "type": "diffusion_models",
      "filename": "flux2_dev_fp8mixed.safetensors",
      "source": "huggingface" | "url" | "civitai",

      # source=huggingface 时:
      "repo": "Comfy-Org/flux2_dev_repackaged",
      "hf_filename": "flux2_dev_fp8mixed.safetensors",
      "requires_token": true,

      # source=url 时:
      "url": "https://example.com/model.safetensors",

      # source=civitai 时(预留,V1 不实现):
      "civitai_id": 123456,
    }

    返回:
      {"ok": true, "cached": true, "path": "...", "size_mb": 1234}            # 已存在
      {"ok": true, "downloaded": true, "path": "...", "size_mb": 1234, "elapsed_sec": 45}  # 刚下完
      {"ok": false, "error": "..."}
    """
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    type_ = payload.get("type")
    filename = payload.get("filename")
    source = payload.get("source", "huggingface")
    if not type_ or not filename:
        return {"ok": False, "error": "type and filename required"}

    target_dir = Path(f"/comfy-volume/models/{type_}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename

    if target.exists():
        return {
            "ok": True, "cached": True,
            "path": str(target),
            "size_mb": target.stat().st_size // 1024 // 1024,
        }

    t_start = time.time()
    seed_id = f"{type_}/{filename}"
    seed_state[seed_id] = {
        "status": "downloading",
        "started_at": t_start,
        "source": source,
    }

    try:
        if source == "huggingface":
            from huggingface_hub import hf_hub_download
            repo = payload.get("repo")
            hf_filename = payload.get("hf_filename") or filename
            hf_subfolder = payload.get("hf_subfolder")
            if not repo:
                return {"ok": False, "error": "huggingface source needs 'repo'"}
            token = os.environ.get("HF_TOKEN") if payload.get("requires_token") else None
            print(f"[bridge] hf_hub_download {repo}/{hf_filename} → {target}")
            downloaded_path = hf_hub_download(
                repo_id=repo,
                filename=hf_filename,
                subfolder=hf_subfolder,
                local_dir=str(target_dir),
                token=token,
            )
            # hf_hub_download 会保留 repo 内子路径(如 split_files/.../x.safetensors),
            # 实际文件落在 target_dir 的子目录里 → 移到扁平 target(/models/<type>/<filename>)
            actual = Path(downloaded_path)
            if actual.resolve() != target.resolve():
                actual.replace(target)

        elif source == "url":
            url = payload.get("url")
            if not url:
                return {"ok": False, "error": "url source needs 'url'"}
            # aria2c 多线程下载,比 wget 快 4-8 倍
            print(f"[bridge] aria2c {url} → {target}")
            r = subprocess.run([
                "aria2c", "-x16", "-s16", "--summary-interval=10",
                "-d", str(target_dir), "-o", filename, url,
            ], capture_output=True, text=True, timeout=3500)
            if r.returncode != 0:
                # 失败 fallback urllib
                print(f"[bridge] aria2c failed: {r.stderr[-500:]}, fallback urllib")
                import urllib.request
                urllib.request.urlretrieve(url, target)

        elif source == "civitai":
            return {"ok": False, "error": "civitai source not implemented in V1"}

        else:
            return {"ok": False, "error": f"unknown source: {source}"}

        if not target.exists():
            return {"ok": False, "error": "download finished but file not found"}

        models_vol.commit()  # 持久化
        elapsed = time.time() - t_start
        size_mb = target.stat().st_size // 1024 // 1024
        print(f"[bridge] ✓ seeded {filename} ({size_mb} MB) in {elapsed:.1f}s")

        seed_state[seed_id] = {
            "status": "completed",
            "completed_at": time.time(),
            "size_mb": size_mb,
            "elapsed_sec": elapsed,
        }
        return {
            "ok": True, "downloaded": True,
            "path": str(target),
            "size_mb": size_mb,
            "elapsed_sec": round(elapsed, 1),
        }
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[bridge] seed failed: {e}\n{tb[-1000:]}")
        seed_state[seed_id] = {
            "status": "failed",
            "completed_at": time.time(),
            "error": str(e),
        }
        return {"ok": False, "error": str(e), "trace": tb[-1000:]}


@app.function(image=cuda_image, secrets=[bridge_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-seed-status")
def seed_status_endpoint(type: str, filename: str, key: str = ""):
    """查某个模型下载进度。"""
    deny = _check(key)
    if deny:
        return deny
    seed_id = f"{type}/{filename}"
    s = seed_state.get(seed_id)
    if not s:
        return {"status": "unknown"}
    return s


# ============================================================================
# 本地调试
# ============================================================================
@app.local_entrypoint()
def main():
    print(f"App:    {APP_NAME}")
    print(f"Volume: {VOLUME_NAME}")
    print(f"Secret: {SECRET_NAME}")
    print(f"GPU:    {DEFAULT_GPU}")
    print(f"Endpoints will be at:")
    for ep in ["run", "status", "cancel", "health", "list-models", "check-models",
               "seed-model", "seed-status"]:
        print(f"  https://<workspace>--{APP_NAME}-{ep}.modal.run")
