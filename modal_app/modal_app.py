"""
Modal app — comfyui_modal_bridge 自带的独立 worker(精简版,对齐 art_ai 的 4-endpoint 形态)

部署:
    modal deploy modal_app/modal_app.py

部署后拿到 4 个长期 URL(<ws> 由你的 modal 账号决定):
    https://<ws>--comfyui-bridge-run.modal.run     (POST,跑 workflow)
    https://<ws>--comfyui-bridge-status.modal.run  (GET,查状态)
    https://<ws>--comfyui-bridge-cancel.modal.run  (POST,取消)
    https://<ws>--comfyui-bridge-health.modal.run  (GET,健康 + 已装 custom_nodes)

模型不在这里下载。模型都在本地 ComfyUI Desktop 下好,由本地 `modal_volume.py`(SDK
batch_upload)直接传到 Volume(CAS 去重,通用模型秒过)。Volume 查询也走本地 SDK,
所以这里不再需要 list-models / check-models / seed-model / seed-status 这些 endpoint。

需要:
- Modal Secret  `comfyui-bridge-secrets`(BRIDGE_API_KEY 私有鉴权 + 可选 HF_TOKEN)
- Modal Volume  `comfyui-bridge-models`(自动创建,本地脚本往里传模型)
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
SCALEDOWN = int(os.environ.get("MODAL_BRIDGE_SCALEDOWN", "40"))

app = modal.App(APP_NAME)
models_vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# secrets 含 BRIDGE_API_KEY(私有鉴权)+ 可选 HF_TOKEN。没建过则空 secret 兜底。
try:
    bridge_secret = modal.Secret.from_name(SECRET_NAME)
except Exception:
    bridge_secret = modal.Secret.from_dict({})

job_state = modal.Dict.from_name(f"{APP_NAME}-jobs", create_if_missing=True)

# job_state 清理:每条 completed 含整张图 base64,不清会让 Dict 无限膨胀。
# 策略:终态(completed/failed/cancelled)条目超过 JOB_TTL_S 就删;再按数量上限兜底。
JOB_TTL_S = int(os.environ.get("MODAL_BRIDGE_JOB_TTL", "3600"))   # 终态保留 1 小时(够客户端取回)
JOB_MAX = int(os.environ.get("MODAL_BRIDGE_JOB_MAX", "200"))       # 最多保留多少条


def _sweep_job_state():
    """best-effort 清理过期/超量的终态 job。任何异常都不影响主流程。"""
    try:
        now = time.time()
        items = list(job_state.items())
    except Exception:
        return
    terminal = {"completed", "failed", "cancelled"}
    finished = []
    for jid, s in items:
        if not isinstance(s, dict):
            continue
        if s.get("status") in terminal:
            finished.append((jid, s.get("completed_at") or 0))
    # 1) 过期删
    for jid, done_at in finished:
        if done_at and now - done_at > JOB_TTL_S:
            try:
                del job_state[jid]
            except Exception:
                pass
    # 2) 数量兜底:仍超上限就删最旧的终态条目
    try:
        remaining = [(j, s.get("completed_at") or 0) for j, s in job_state.items()
                     if isinstance(s, dict) and s.get("status") in terminal]
        if len(remaining) > JOB_MAX:
            remaining.sort(key=lambda x: x[1])
            for jid, _ in remaining[: len(remaining) - JOB_MAX]:
                try:
                    del job_state[jid]
                except Exception:
                    pass
    except Exception:
        pass


# ============================================================================
# ComfyUI worker — 两档(按显存),每档 gpu=list 走 Modal 原生 fallback
# boot/run 提取为模块函数,两个 class 共享,只 GPU 档不同。
# ============================================================================
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
    models_vol.reload()  # 启动前同步 Volume(ComfyUI 还没打开文件,不冲突)
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
        # Volume 已在 boot() reload;这里不能再 reload(ComfyUI 已打开文件会冲突)
        from _comfy_ws import run_workflow
        result = run_workflow(workflow=workflow, job_id=job_id, input_images=input_images)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        job_state[job_id] = {**job_state.get(job_id, {}), "status": "failed",
                             "error": str(e), "trace": tb[-2000:], "completed_at": time.time()}
        raise
    # 有 images(多图)就只存 images,不再冗余存 data_base64/filename(那是 images[0] 的重复,
    # 白白让 job_state 体积翻倍);只有极老回退路径(没 images)才退回单图字段。
    done = {**job_state.get(job_id, {}), "status": "completed",
            "image_url": result.get("image_url"), "completed_at": time.time()}
    if result.get("images"):
        done["images"] = result["images"]
    else:
        done["data_base64"] = result.get("data_base64")
        done["filename"] = result.get("filename")
    job_state[job_id] = done
    return result


# 统一所有工作流都跑 H100,排不到自动降级 A100-80GB(用户要求:别把小模型丢到 L40S 反而慢)。
# 单 worker class —— 不再按显存分档(那会让 klein 等小模型落到 L40S/弱卡)。
@app.cls(gpu=["H100", "A100-80GB"], **_WORKER_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorker:
    @modal.enter()
    def boot(self):
        _worker_boot(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None) -> dict:
        return _worker_run(workflow, job_id, input_images)


# tier 入参保留兼容(前端仍可能传 80g/40g),但都指向同一个 H100 worker。
_TIER_WORKERS = {"80g": ComfyWorker, "40g": ComfyWorker}
_TIER_GPU_DISPLAY = {"80g": "H100→A100-80G", "40g": "H100→A100-80G"}


# ============================================================================
# 鉴权 — 自建 API key(private endpoint)
# 不用 Modal 的 requires_proxy_auth(要单独 Proxy Auth Token,没法程序化)。改成:
# 部署时随机生成 BRIDGE_API_KEY 存进 Secret + 本地 config,每个 endpoint 校验。
# key 经 query(GET ?key=)/ body(POST auth_key)传入;拒绝时函数体内 import fastapi 返 401。
# ============================================================================
def _check(key: str):
    expected = os.environ.get("BRIDGE_API_KEY", "")
    if expected and key == expected:
        return None
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "unauthorized — bad or missing bridge key"}, status_code=401)


# ============================================================================
# REST endpoints(4 个)
# ============================================================================

@app.function(image=cuda_image, secrets=[bridge_secret], timeout=60)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-run")
def run_endpoint(payload: dict):
    """提交 workflow。payload: {workflow, tier?, images?, auth_key}"""
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    job_id = payload.get("job_id") or str(uuid.uuid4())
    workflow = payload.get("workflow")
    if not workflow:
        return {"error": "Missing 'workflow' in payload"}
    input_images = payload.get("images")
    # 前端按工作流总显存需求判断 tier;默认 40g。每档自带 GPU 原生 fallback。
    tier = (payload.get("tier") or "40g").lower()
    if tier not in _TIER_WORKERS:
        tier = "40g"
    worker = _TIER_WORKERS[tier]
    gpu_display = _TIER_GPU_DISPLAY[tier]

    _sweep_job_state()  # 顺手清理过期/超量的旧 job(防 Dict 无限膨胀)
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
    """健康 + 已装 custom_nodes(权威源:反映真实部署的镜像,供本地双向同步对比)。"""
    deny = _check(key)
    if deny:
        return deny
    info: dict = {"healthy": True, "app": APP_NAME, "volume": VOLUME_NAME}
    try:
        warm = 0
        try:
            stats = modal.Cls.from_name(APP_NAME, "ComfyWorker")().run.get_current_stats()
            warm += getattr(stats, "num_total_runners", 0) or 0
        except Exception:
            pass
        info["warm_containers"] = warm
    except Exception as e:
        info["stats_error"] = str(e)
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
# 本地调试
# ============================================================================
@app.local_entrypoint()
def main():
    print(f"App:    {APP_NAME}")
    print(f"Volume: {VOLUME_NAME}")
    print(f"Secret: {SECRET_NAME}")
    print("Endpoints:")
    for ep in ["run", "status", "cancel", "health"]:
        print(f"  https://<workspace>--{APP_NAME}-{ep}.modal.run")
