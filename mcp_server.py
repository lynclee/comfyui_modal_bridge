"""
Modal Bridge MCP server — 把本地 HTTP API(见仓库根 API.md)封装成标准 MCP 工具,
让 Claude Code / Codex 等 agent 把「云端 GPU 出资产」当普通工具调用。

薄封装原则:不实现任何业务逻辑,每个工具 = 一次本地 HTTP 调用;鉴权/路由/模型同步
全部由插件后端完成。ComfyUI(装好本插件)必须在跑,否则所有工具报连接错误。

运行(无需安装进插件环境,mcp 不是插件依赖):
    pip install mcp && python mcp_server.py

注册示例:
  Claude Code(.mcp.json 或 `claude mcp add`):
    {"mcpServers": {"modal-bridge": {
        "command": "python", "args": ["<repo>/mcp_server.py"],
        "env": {"MODAL_BRIDGE_URL": "http://127.0.0.1:8000"}}}}
  Codex(~/.codex/config.toml):
    [mcp_servers.modal_bridge]
    command = "python"
    args = ["<repo>/mcp_server.py"]
    env = { MODAL_BRIDGE_URL = "http://127.0.0.1:8000" }

容器/远程场景把 MODAL_BRIDGE_URL 指向宿主机(如 http://host.docker.internal:8000),
并确保该 host 在 no_proxy 白名单里(本文件自身已显式绕过系统代理,不受 env 影响)。
"""
import json
import os
import urllib.request

try:                                        # mcp >= 2.0
    from mcp.server import MCPServer as _Server
except ImportError:                         # mcp 1.x(FastMCP 时代)
    from mcp.server.fastmcp import FastMCP as _Server

BASE = os.environ.get("MODAL_BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")

# 全部调用都打本机/宿主机,显式禁用系统代理(容器里 https_proxy 常指向外网 relay,
# 让 localhost 流量进代理是经典事故来源)。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(path: str, body: dict | None = None, timeout: int = 120) -> dict:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
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


mcp = _Server("modal-bridge")


@mcp.tool()
def submit_workflow(workflow_json: str) -> dict:
    """提交 ComfyUI 工作流到 Modal 云端 GPU 跑。workflow_json 是 API prompt 的 JSON 字符串
    ({node_id:{class_type,inputs}} 格式,非画布 JSON)。后端自动完成 GPU 档位路由与输入图上传。
    返回 {job_id, gpu, worker_timeout_sec};随后用 job_status 轮询,完成后 fetch_result 取产物。
    轮询 deadline 建议 = worker_timeout_sec + 180s。"""
    prompt = _parse_prompt(workflow_json)
    if prompt is None:
        return {"error": "workflow_json 必须是 API prompt 的 JSON 对象字符串"}
    return _call("/modal_bridge/submit", {"prompt": prompt})


@mcp.tool()
def job_status(job_id: str) -> dict:
    """查任务状态。status ∈ queued/running/completed/failed/cancelled;running 时带
    progress:{step,total,s_it,elapsed}(s_it 为滑窗中位数,n_samples≥3 才可信)。
    ⚠ 显存不足不报错、只静默降速:s_it 显著高于同配置基线即是信号。"""
    return _call(f"/modal_bridge/poll?job_id={job_id}")


@mcp.tool()
def fetch_result(job_id: str) -> dict:
    """任务 completed 后取回产物,写入 ComfyUI/output/<subfolder>/<job_id>/,
    返回 outputs 文件清单。未完成时原样返回当前状态(不报错,方便直接重试)。"""
    state = _call(f"/modal_bridge/poll?job_id={job_id}")
    if state.get("status") != "completed":
        return {"not_ready": True, **state}
    return _call("/modal_bridge/fetch_result", {"job_id": job_id, "modal_state": state},
                 timeout=600)


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """取消云端任务。⚠ 必须检查返回的 ok:false 表示云端还在跑、还在计费。"""
    return _call("/modal_bridge/cancel", {"job_id": job_id})


@mcp.tool()
def estimate_vram(workflow_json: str) -> dict:
    """提交前估算工作流显存(GB)。est_basis="activation" 表示按 分辨率×帧数 激活公式
    (实测校准,视频类可信);"legacy" 表示回退的权重×系数保守公式(可能偏高 ~50%)。"""
    prompt = _parse_prompt(workflow_json)
    if prompt is None:
        return {"error": "workflow_json 必须是 API prompt 的 JSON 对象字符串"}
    return _call("/modal_bridge/estimate_vram", {"prompt": prompt})


@mcp.tool()
def bridge_health() -> dict:
    """云端健康 + 版本契约一次拿全:health(app 可达性)+ version(本地插件 vs 云端部署,
    match=false 应先重新部署)。排障顺序:reachable=false → 查 platform_status 区分平台故障。"""
    return {
        "health": _call("/modal_bridge/health", timeout=40),
        "version": _call("/modal_bridge/version", timeout=40),
    }


@mcp.tool()
def get_config() -> dict:
    """读插件配置(密钥字段已由后端抹除,只有 has_* 标志)。关注:gpu_tier(档位,改完即生效)、
    default_gpu/cheap_gpu/top_gpu、use_sage_attention、worker_timeout_sec(这些要重新部署生效)。"""
    return _call("/modal_bridge/config")


if __name__ == "__main__":
    mcp.run()
