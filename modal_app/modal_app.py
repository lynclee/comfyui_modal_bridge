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

from aigc_delivery import normalize_delivery, public_delivery
from modal_image import cuda_image


# ============================================================================
# Modal 资源
# ============================================================================
APP_NAME = os.environ.get("MODAL_BRIDGE_APP_NAME", "comfyui-bridge")
VOLUME_NAME = os.environ.get("MODAL_BRIDGE_VOLUME", "comfyui-bridge-models")
SECRET_NAME = os.environ.get("MODAL_BRIDGE_SECRET", "comfyui-bridge-secrets")
SCALEDOWN = int(os.environ.get("MODAL_BRIDGE_SCALEDOWN", "40"))
# worker 单任务超时上限(s)。部署时由 config.worker_timeout_sec 决定(覆盖最慢类别,如视频)。
# ⚠ Modal 的 timeout 是部署期固定的,运行时不可变 —— 换值需重新部署。
WORKER_TIMEOUT = int(os.environ.get("MODAL_BRIDGE_TIMEOUT", "1800"))
# 内存快照(可选,默认关):冷启 ~30s→~5s。开关 = config.enable_snapshot → MODAL_BRIDGE_SNAPSHOT。
# 必须连 GPU 快照一起开(ComfyUI boot 探 CUDA;只 CPU 快照会以 CPU 模式初始化、恢复后切不回卡)。
# experimental,按 GPU 档需各自 bench;失败兜底见 ComfyWorker.ensure_comfy_alive(退化为普通冷启,不更差)。
_SNAPSHOT = os.environ.get("MODAL_BRIDGE_SNAPSHOT", "0") == "1"
DEPLOYED_VERSION = os.environ.get("MODAL_BRIDGE_VERSION", "unknown")  # 部署时烤进,health 回传
DEPLOYED_COMFYUI_TAG = os.environ.get("MODAL_BRIDGE_COMFYUI_TAG", "v0.22.0")  # 云端 clone 的 ComfyUI tag

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
    def _drop(jid):
        for k in (jid, f"{jid}:call"):  # 连带删独立的 call_id key,不留孤儿
            try:
                del job_state[k]
            except Exception:
                pass
    # 1) 过期删
    for jid, done_at in finished:
        if done_at and now - done_at > JOB_TTL_S:
            _drop(jid)
    # 2) 数量兜底:仍超上限就删最旧的终态条目
    try:
        remaining = [(j, s.get("completed_at") or 0) for j, s in job_state.items()
                     if isinstance(s, dict) and s.get("status") in terminal]
        if len(remaining) > JOB_MAX:
            remaining.sort(key=lambda x: x[1])
            for jid, _ in remaining[: len(remaining) - JOB_MAX]:
                _drop(jid)
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
    timeout=WORKER_TIMEOUT,
    min_containers=0,
    max_containers=10,
)


def _worker_boot(self, cpu: bool = False):
    # cpu=True:CPU-only 容器(无 GPU)→ 给 ComfyUI 传 --cpu 强制 CPU 模式,根本不碰 CUDA 初始化,
    # 避免 CUDA 版 torch 在无驱动机器上自动探测的边角风险。GPU 容器走默认(自动用 CUDA)。
    self._cpu = cpu
    models_vol.reload()  # 启动前同步 Volume(ComfyUI 还没打开文件,不冲突)
    cmd = [
        "python", "/comfyui/main.py",
        "--listen", "127.0.0.1", "--port", "8188",
        "--extra-model-paths-config", "/comfyui/extra_model_paths.yaml",
    ]
    if cpu:
        cmd.append("--cpu")
    self.proc = subprocess.Popen(cmd)
    from _comfy_ws import wait_comfy_ready
    wait_comfy_ready(timeout_s=180)
    print(f"[bridge] ComfyUI ready ({'CPU' if cpu else 'GPU'})")


def _worker_shutdown(self):
    try:
        self.proc.terminate()
    except Exception:
        pass


def _worker_ensure_alive(self):
    """快照恢复路径上的正确性闸门 + 自愈(GPU/CPU worker 共用):快照关时直接返回;开时探活失败
    就原地重启子进程(退化为一次普通 boot,不比无快照更糟),而非 raise 杀容器进重试循环。"""
    if not _SNAPSHOT:
        return
    import requests
    try:
        if requests.get("http://127.0.0.1:8188/system_stats", timeout=5).ok:
            return
    except Exception:
        pass
    print("[bridge] restore 探活失败,重启 ComfyUI 子进程(退化为普通冷启)")
    _worker_shutdown(self)
    _worker_boot(self, cpu=getattr(self, "_cpu", False))  # 沿用本 worker 的 CPU/GPU 模式


def _worker_run(workflow: dict, job_id: str, input_images: list | None = None,
                delivery: dict | None = None) -> dict:
    # delivery:结果交付方式(见 aigc_delivery.normalize_delivery)。desktop = 现状(回本地);
    # aigc-r2 = 直传 R2 + 回调 AIGC Studio。⚠ delivery 里的 token 是敏感的:不进 job_state、不打日志。
    # call_id 现在存独立 key(见 run_endpoint);等它出现仅为让 cancel 可用,等不到也继续。
    for _ in range(50):  # 最多 ~5s
        if job_state.get(f"{job_id}:call"):
            break
        time.sleep(0.1)
    mode = (delivery or {}).get("mode", "desktop")
    job_state[job_id] = {**job_state.get(job_id, {}), "status": "running", "started_at": time.time()}
    try:
        # ⚠ 不在这里 free/reload!曾经"每 job 跑前 free+reload"会把 warm 容器显存里的模型卸掉,
        # 导致每个 job 都得重新从 Volume 加载 flux2(~163s),彻底毁掉 warm 复用。
        # 正确策略:正常直接跑(模型在显存,秒级);只有验证失败(模型不在列表)时,queue_workflow
        # 内部才按需 free→reload→重试(只有删 Volume/缺模型的极端场景才付这个代价)。
        from _comfy_ws import run_workflow
        # aigc-r2:只「发现」产物不读进内存(materialize=False),下面流式直传 R2。
        result = run_workflow(workflow=workflow, job_id=job_id, input_images=input_images,
                              materialize=(mode != "aigc-r2"))
        if mode == "aigc-r2":
            # 状态机:running → delivering → completed。出图后直传 R2 + 回调 AIGC Studio;
            # 全部上传且回调成功才 completed。回调失败但文件已在 R2 → delivery.status =
            # callback_failed + 保留 manifest,AIGC Studio 轮询 /status 兜底落库(计划 §7)。
            job_state[job_id] = {**job_state.get(job_id, {}), "status": "delivering"}
            from aigc_delivery import deliver_outputs
            dres = deliver_outputs(
                job_id=job_id, output_refs=result.get("output_refs") or [], delivery=delivery,
                provider_job_id=str(job_state.get(f"{job_id}:call") or ""))
            # manifest 只有 r2_key/etag/size 等元数据(无 base64、无 token),job_state 不膨胀。
            job_state[job_id] = {**job_state.get(job_id, {}), "status": "completed",
                                 "delivery": {"mode": "aigc-r2", **dres},
                                 "completed_at": time.time()}
            return {"delivered": dres["status"], "assets": len(dres["assets"])}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        job_state[job_id] = {**job_state.get(job_id, {}), "status": "failed",
                             "error": str(e), "trace": tb[-2000:], "completed_at": time.time()}
        raise
    # 大文件走了 Volume(item 带 volume_path)→ commit 一次,本地 SDK 才看得到刚写进 _outputs 的文件
    if any(i.get("volume_path") for i in (result.get("images") or [])):
        try:
            models_vol.commit()
        except Exception as e:
            print(f"[bridge] volume commit 失败: {e}")
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


# GPU 在部署时由 config.default_gpu 决定(deploy_env 传 MODAL_BRIDGE_DEFAULT_GPU)。
# ⚠ Modal 的 gpu 是部署时固定的,运行时不可变 —— 换显卡需重新部署。
# 每档带 Modal 原生 fallback(排不到主卡自动降级到链里下一个)。
_PRIMARY_GPU = os.environ.get("MODAL_BRIDGE_DEFAULT_GPU", "H100")
_GPU_CHAIN = {
    "B200":      ["B200", "H200", "H100"], # 183G Blackwell 最强档,排不到降 H200/H100
    "H100":      ["H100", "A100-80GB"],   # 主卡排不到降 A100-80G
    "H200":      ["H200", "H100"],         # 141G 大卡,降级到 H100
    "A100-80GB": ["A100-80GB"],
    "L40S":      ["L40S"],                  # 选 L40S 是为省钱,不 fallback 到贵卡
}
_GPU_LIST = _GPU_CHAIN.get(_PRIMARY_GPU, [_PRIMARY_GPU])

# 省钱档 GPU(默认 L40S)。估算显存放得下的工作流自动降到这张卡跑(路由见 run_endpoint)。
# 与主卡相同(用户把 default 设成 L40S 之类)则不启用,所有 GPU 任务仍走主 worker。
_CHEAP_GPU = os.environ.get("MODAL_BRIDGE_CHEAP_GPU", "L40S")
_CHEAP_GPU_LIST = _GPU_CHAIN.get(_CHEAP_GPU, [_CHEAP_GPU])
_CHEAP_ENABLED = _CHEAP_GPU != _PRIMARY_GPU

# 顶配档 GPU(默认 B200 183G):估算显存超过主卡的工作流升到这跑,防 OOM(升档 = 正确性兜底)。
# B200 是 Blackwell 最强档,显存最大、速度最快,大图自动上这张。
# ⚠ 升档档不向下 fallback(对 >80G 的活退到小卡 = OOM),所以固定单卡列表,宁可排队等也不降级。
_TOP_GPU = os.environ.get("MODAL_BRIDGE_TOP_GPU", "B200")
_TOP_GPU_LIST = [_TOP_GPU]
_TOP_ENABLED = _TOP_GPU != _PRIMARY_GPU

# 开快照时追加两个 decorator 参数;关时为空 dict → @app.cls 行为和原来完全一致。
_SNAP_KW = (dict(enable_memory_snapshot=True,
                 experimental_options={"enable_gpu_snapshot": True}) if _SNAPSHOT else {})


@app.cls(gpu=_GPU_LIST, **_WORKER_KW, **_SNAP_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorker:
    @modal.enter(snap=_SNAPSHOT)   # 开快照:这一段进快照(snap 阶段 GPU 可见,正常 boot)
    def boot(self):
        _worker_boot(self)

    @modal.enter(snap=False)       # 恢复路径上的正确性闸门 + 自愈(见 _worker_ensure_alive)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None) -> dict:
        return _worker_run(workflow, job_id, input_images, delivery)


# 省钱档 worker:估算显存放得下便宜卡(默认 L40S)且非视频的 GPU 工作流路由到这。
# 与主 worker 完全同构,只是 gpu 不同;min_containers=0 → 不被路由到时 0 容器 = $0,定义在这儿不花钱。
@app.cls(gpu=_CHEAP_GPU_LIST, **_WORKER_KW, **_SNAP_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorkerCheap:
    @modal.enter(snap=_SNAPSHOT)
    def boot(self):
        _worker_boot(self)

    @modal.enter(snap=False)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None) -> dict:
        return _worker_run(workflow, job_id, input_images, delivery)


# 顶配档 worker:估算显存超过主卡的工作流(如 >80G)升到这(默认 B200 183G),防 OOM。
# 同构,只是 gpu 不同且不向下 fallback;min_containers=0 → 不被路由时 0 容器 = $0。
@app.cls(gpu=_TOP_GPU_LIST, **_WORKER_KW, **_SNAP_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorkerTop:
    @modal.enter(snap=_SNAPSHOT)
    def boot(self):
        _worker_boot(self)

    @modal.enter(snap=False)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None) -> dict:
        return _worker_run(workflow, job_id, input_images, delivery)


# CPU-only worker:无 GPU 需求的工作流(纯 API / 无本地模型节点)走这,GPU 账单≈0。
# 同镜像、无 gpu;CPU 内存快照是 GA(不实验、不需要 gpu_snapshot),所以只 enable_memory_snapshot。
_SNAP_KW_CPU = (dict(enable_memory_snapshot=True) if _SNAPSHOT else {})


@app.cls(**_WORKER_KW, **_SNAP_KW_CPU)
@modal.concurrent(max_inputs=1)
class ComfyWorkerCPU:
    @modal.enter(snap=_SNAPSHOT)
    def boot(self):
        _worker_boot(self, cpu=True)   # 无 GPU 容器 → 强制 ComfyUI CPU 模式

    @modal.enter(snap=False)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None) -> dict:
        return _worker_run(workflow, job_id, input_images, delivery)


# tier 入参保留兼容(前端仍可能传 80g/40g),但 GPU 由部署时的 default_gpu 决定,不再按 tier 分档。
_TIER_WORKERS = {"80g": ComfyWorker, "40g": ComfyWorker}
_GPU_DISPLAY = "→".join(_GPU_LIST)  # 如 "H100→A100-80GB",进度卡/日志显示真实显卡
_TIER_GPU_DISPLAY = {"80g": _GPU_DISPLAY, "40g": _GPU_DISPLAY}
_CHEAP_GPU_DISPLAY = "→".join(_CHEAP_GPU_LIST)  # 省钱档显示(如 "L40S")
_TOP_GPU_DISPLAY = "→".join(_TOP_GPU_LIST)        # 顶配档显示(如 "B200")


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
    """提交 workflow。payload: {workflow, tier?, images?, auth_key, delivery?}
    delivery(可选):{"mode":"desktop"}(缺省,结果回本地)或
    {"mode":"aigc-r2","job_id":"…","token":"…"}(结果直传 R2 + 回调 AIGC Studio)。"""
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    delivery, derr = normalize_delivery(payload)
    if derr:
        return {"error": derr}
    # aigc-r2 用 AIGC Studio 的任务 UUID 作 job_id,双方同一 id 查 /status;desktop 沿用旧逻辑。
    job_id = (delivery.get("job_id") if delivery.get("mode") == "aigc-r2" else None) \
        or payload.get("job_id") or str(uuid.uuid4())
    workflow = payload.get("workflow")
    if not workflow:
        return {"error": "Missing 'workflow' in payload"}
    input_images = payload.get("images")
    # 路由:工作流无本地模型节点(纯 API / 轻节点)= 不需要 GPU → CPU worker(账单≈0);否则 GPU worker。
    # needs_gpu 由后端 /submit 据 extract_required_models 判定后传入(缺省 True,稳妥)。
    # 四档路由(成本从低到高):无 GPU → CPU;放得下便宜卡 → cheap(L40S);超过主卡 → top(B200);否则 → 主卡。
    # gpu_class 由后端 /submit 据 estimate_vram 判定后传入(缺省 primary,稳妥)。
    needs_gpu = bool(payload.get("needs_gpu", True))
    gpu_class = (payload.get("gpu_class") or "primary").lower()
    if not needs_gpu:
        worker = ComfyWorkerCPU
        gpu_display = "CPU"
        tier = "cpu"
    elif gpu_class == "cheap" and _CHEAP_ENABLED:
        worker = ComfyWorkerCheap
        gpu_display = _CHEAP_GPU_DISPLAY
        tier = "cheap"
    elif gpu_class == "top" and _TOP_ENABLED:
        worker = ComfyWorkerTop
        gpu_display = _TOP_GPU_DISPLAY
        tier = "top"
    else:
        tier = (payload.get("tier") or "40g").lower()
        if tier not in _TIER_WORKERS:
            tier = "40g"
        worker = _TIER_WORKERS[tier]
        gpu_display = _TIER_GPU_DISPLAY[tier]

    _sweep_job_state()  # 顺手清理过期/超量的旧 job(防 Dict 无限膨胀)
    # job_state 只存 delivery 的可外泄形态(mode/job_id),token 绝不落 Dict/日志。
    job_state[job_id] = {"status": "queued", "queued_at": time.time(), "gpu": gpu_display,
                         "tier": tier, "delivery": public_delivery(delivery)}
    call = worker().run.spawn(workflow, job_id, input_images, delivery)
    # ⚠ call_id 存到独立 key,run_endpoint 不再回写 job_state[job_id]。
    # 原因:job_state[job_id] 同时被 worker 容器写(running/failed/completed)。Modal Dict 跨容器
    # 最终一致、无序,run_endpoint spawn 后 merge 回写可能读到 stale 的 queued、把 worker 刚写的
    # 终态冲掉 → 前端永远 poll 到 queued、卡片一直转。分离 key 后两边各写各的,彻底无竞态。
    job_state[f"{job_id}:call"] = call.object_id
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
    call_id = job_state.get(f"{job_id}:call") or s.get("call_id")  # 新独立 key,兼容旧字段
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
    info: dict = {"healthy": True, "app": APP_NAME, "volume": VOLUME_NAME,
                  "deployed_version": DEPLOYED_VERSION, "deployed_gpu": _PRIMARY_GPU,
                  "deployed_cheap_gpu": (_CHEAP_GPU if _CHEAP_ENABLED else None),
                  "deployed_top_gpu": (_TOP_GPU if _TOP_ENABLED else None),
                  "deployed_comfyui_tag": DEPLOYED_COMFYUI_TAG}
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
