"""
Modal Bridge MCP server — 让 Claude Code / Codex 等 agent 把「云端 GPU 出资产」当标准工具调用。

两种模式(启动时按 env 自动选):

**local 模式(默认)** — 薄封装本地 HTTP API(见仓库根 API.md),ComfyUI(装好本插件)必须在跑。
功能最全:模型/节点自动同步、显存估算、GPU 自动路由都由插件后端完成。
    MODAL_BRIDGE_URL   本地 ComfyUI 地址,默认 http://127.0.0.1:8000(容器内访问宿主机
                       用 http://host.docker.internal:8000)
    MODAL_BRIDGE_LOCAL_CAPABILITY  BASE 不是 localhost 时必填；值来自服务器 config.json

**cloud 模式** — 经 bridge_client.py 直连 Modal 云端 endpoint,**不需要本地 ComfyUI**。
前提:部署者已用完整插件部署过(模型在 Volume、节点在镜像)。适合拿到 endpoint + key 的
协作者。本地专属工具(estimate_vram / get_config)在此模式下返回说明性错误。
    MODAL_BRIDGE_ENDPOINT   形如 https://<workspace>--comfyui-bridge(设了即进 cloud 模式)
    MODAL_BRIDGE_KEY        bridge_api_key(部署者 config.json 里那把)
    MODAL_BRIDGE_OUT_DIR    产物落盘目录,默认 ./modal_bridge_outputs
    MODAL_BRIDGE_INPUT_DIRS 输入图搜索目录(冒号分隔),默认当前目录

运行(mcp 包不是插件依赖,单独装):
    pip install mcp && python mcp_server.py

注册示例:
  Claude Code(.mcp.json 或 `claude mcp add`):
    {"mcpServers": {"modal-bridge": {
        "command": "python", "args": ["<repo>/mcp_server.py"],
        "env": {"MODAL_BRIDGE_URL": "http://127.0.0.1:8000"}}}}
  cloud 模式只换 env:
        "env": {"MODAL_BRIDGE_ENDPOINT": "https://<ws>--comfyui-bridge",
                "MODAL_BRIDGE_KEY": "<bridge_api_key>"}
  Codex(~/.codex/config.toml):
    [mcp_servers.modal_bridge]
    command = "python"
    args = ["<repo>/mcp_server.py"]
    env = { MODAL_BRIDGE_URL = "http://127.0.0.1:8000" }

代理:local 模式本文件显式绕过系统代理(localhost 流量不该进代理);cloud 模式的外网请求
由 bridge_client 走系统代理 env —— 两条路互不干扰。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

try:                                        # mcp >= 2.0
    from mcp.server import MCPServer as _Server
except ImportError:                         # mcp 1.x(FastMCP 时代)
    from mcp.server.fastmcp import FastMCP as _Server

BASE = os.environ.get("MODAL_BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
_ENDPOINT = os.environ.get("MODAL_BRIDGE_ENDPOINT", "").strip()
MODE = "cloud" if _ENDPOINT else "local"

_client = None
BridgeError = RuntimeError  # local 模式下 _cloud 不会被调,占位防 NameError
if MODE == "cloud":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bridge_client import BridgeClient, BridgeError  # noqa: F811
    _client = BridgeClient(_ENDPOINT, os.environ.get("MODAL_BRIDGE_KEY", ""))

# local 模式全部调用都打本机/宿主机,显式禁用系统代理(容器里 https_proxy 常指向外网 relay,
# 让 localhost 流量进代理是经典事故来源)。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_OUT_DIR = os.environ.get("MODAL_BRIDGE_OUT_DIR", "./modal_bridge_outputs")
_INPUT_DIRS = [d for d in os.environ.get("MODAL_BRIDGE_INPUT_DIRS", ".").split(":") if d]
_LOCAL_CAPABILITY = os.environ.get("MODAL_BRIDGE_LOCAL_CAPABILITY", "").strip()


def _call(path: str, body: dict | None = None, timeout: int = 120) -> dict:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if _LOCAL_CAPABILITY:
        headers["X-Modal-Bridge-Capability"] = _LOCAL_CAPABILITY
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": f"HTTP {e.code} {path}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e} (ComfyUI 在跑吗? BASE={BASE})"}


def _parse_prompt(workflow_json: str) -> dict | None:
    try:
        p = json.loads(workflow_json)
        return p if isinstance(p, dict) else None
    except Exception:
        return None


def _cloud(fn):
    """cloud 模式工具体的统一异常兜底:BridgeError → {error}。"""
    try:
        return fn()
    except BridgeError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


mcp = _Server("modal-bridge")


@mcp.tool()
def submit_workflow(workflow_json: str, gpu_class: str = "") -> dict:
    """提交 ComfyUI 工作流到 Modal 云端 GPU 跑。workflow_json 是 API prompt 的 JSON 字符串
    ({node_id:{class_type,inputs}} 格式,非画布 JSON)。
    local 模式:后端自动做 GPU 档位路由与输入图打包,gpu_class 参数被忽略。
    cloud 模式:gpu_class ∈ primary(默认)/cheap/top 手选档位;LoadImage 引用的输入图按
    MODAL_BRIDGE_INPUT_DIRS 搜索打包。
    返回含 job_id;随后 job_status 轮询,完成后 fetch_result 取产物。
    轮询 deadline:local 模式用返回的 worker_timeout_sec+180s;cloud 模式问部署者(默认按 3600s)。"""
    prompt = _parse_prompt(workflow_json)
    if prompt is None:
        return {"error": "workflow_json 必须是 API prompt 的 JSON 对象字符串"}
    if MODE == "cloud":
        def _do():
            imgs = _client.pack_input_images(prompt, _INPUT_DIRS)
            d = _client.submit(prompt, input_images=imgs or None,
                               gpu_class=(gpu_class or "primary"))
            return {"ok": True, "job_id": d["id"], "gpu": d.get("gpu"), "mode": "cloud"}
        return _cloud(_do)
    return _call("/modal_bridge/submit", {"prompt": prompt})


@mcp.tool()
def job_status(job_id: str) -> dict:
    """查任务状态。status ∈ queued/running/completed/failed/cancelled;running 时带
    progress:{step,total,s_it,elapsed}(s_it 为滑窗中位数,n_samples≥3 才可信)。
    ⚠ 显存不足不报错、只静默降速:s_it 显著高于同配置基线即是信号。"""
    if MODE == "cloud":
        return _cloud(lambda: _client.status(job_id))
    return _call(f"/modal_bridge/poll?job_id={job_id}")


@mcp.tool()
def fetch_result(job_id: str) -> dict:
    """任务 completed 后取回产物。local 模式写入 ComfyUI/output/<subfolder>/<job_id>/;
    cloud 模式写入 MODAL_BRIDGE_OUT_DIR(大文件经云端 /fetch 流式下载,不需要 modal token)。
    未完成时原样返回当前状态(not_ready:true,方便直接重试)。"""
    if MODE == "cloud":
        def _do():
            state = _client.status(job_id)
            if state.get("status") != "completed":
                return {"not_ready": True, **state}
            outs = _client.download_outputs(state, os.path.join(_OUT_DIR, job_id))
            return {"ok": True, "job_id": job_id, "outputs": outs}
        return _cloud(_do)
    state = _call(f"/modal_bridge/poll?job_id={job_id}")
    if state.get("status") != "completed":
        return {"not_ready": True, **state}
    return _call("/modal_bridge/fetch_result", {"job_id": job_id, "modal_state": state},
                 timeout=600)


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """取消云端任务。⚠ 必须检查返回:ok:false / error 表示云端还在跑、还在计费。"""
    if MODE == "cloud":
        return _cloud(lambda: _client.cancel(job_id))
    return _call("/modal_bridge/cancel", {"job_id": job_id})


@mcp.tool()
def estimate_vram(workflow_json: str) -> dict:
    """提交前估算工作流显存(GB)。est_basis="activation" 表示按 分辨率×帧数 激活公式
    (实测校准,视频类可信);"legacy" 表示回退的权重×系数保守公式(可能偏高 ~50%)。
    仅 local 模式可用(估算要读本地模型文件大小)。"""
    if MODE == "cloud":
        return {"error": "cloud 模式无本地模型可估;显存路由请用 submit_workflow 的 gpu_class 手选"}
    prompt = _parse_prompt(workflow_json)
    if prompt is None:
        return {"error": "workflow_json 必须是 API prompt 的 JSON 对象字符串"}
    return _call("/modal_bridge/estimate_vram", {"prompt": prompt})


@mcp.tool()
def bridge_health() -> dict:
    """云端健康 + 版本信息。local 模式还含本地/云端版本契约比对(match=false 应先重新部署)。"""
    if MODE == "cloud":
        return _cloud(lambda: {"mode": "cloud", "health": _client.health()})
    return {
        "mode": "local",
        "health": _call("/modal_bridge/health", timeout=40),
        "version": _call("/modal_bridge/version", timeout=40),
    }


@mcp.tool()
def get_config() -> dict:
    """读插件配置(密钥字段已由后端抹除,只有 has_* 标志)。仅 local 模式可用。
    关注:gpu_tier(档位,改完即生效)、default_gpu/cheap_gpu/top_gpu、use_sage_attention、
    worker_timeout_sec(这些要重新部署生效)。"""
    if MODE == "cloud":
        return {"error": "cloud 模式无本地配置;端点/密钥来自 env,GPU 档位在 submit 时手选"}
    return _call("/modal_bridge/config")


if __name__ == "__main__":
    mcp.run()
