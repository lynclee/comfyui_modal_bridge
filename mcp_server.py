"""
Modal Bridge MCP server — 让 Claude Code / Codex 等 agent 把「云端 GPU 出资产」当标准工具调用。

两种模式(启动时按 env 自动选):

**local 模式(默认)** — 薄封装本地 HTTP API(见仓库根 API.md),ComfyUI(装好本插件)必须在跑。
功能最全:模型/节点自动同步、显存估算、GPU 自动路由都由插件后端完成。
    MODAL_BRIDGE_URL   本地 ComfyUI 地址,默认 http://127.0.0.1:8000(容器内访问宿主机
                       用 http://host.docker.internal:8000)
    MODAL_BRIDGE_LOCAL_CONFIG      推荐:ComfyUI 那份插件 config.json 的路径(0600),MCP 进程
                                   直接从里面读 local_api_capability,token 不进 env / .mcp.json
    MODAL_BRIDGE_LOCAL_CAPABILITY  或直接给值(优先级高于上面)。直连 127.0.0.1 时两者都不用设;
                                   经 host.docker.internal / 局域网访问时必设其一

**cloud 模式** — 经 bridge_client.py 直连 Modal 云端 endpoint,**不需要本地 ComfyUI**。
前提:部署者已用完整插件部署过(模型在 Volume、节点在镜像)。适合拿到 endpoint + key 的
协作者。本地专属工具(estimate_vram / get_config)在此模式下返回说明性错误。
    MODAL_BRIDGE_ENDPOINT   形如 https://<workspace>--comfyui-bridge(设了即进 cloud 模式)
    MODAL_BRIDGE_KEY        bridge_api_key(部署者 config.json 里那把)
    MODAL_BRIDGE_OUT_DIR    产物落盘目录,默认 <启动时的当前目录>/modal_bridge_outputs(启动时即转成绝对路径)
    MODAL_BRIDGE_INPUT_DIRS 输入图搜索目录(按 os.pathsep 分隔:POSIX 是冒号,Windows 是分号),默认当前目录

运行(mcp 包不是插件依赖,单独装):
    pip install mcp && python mcp_server.py

注册示例:
  Claude Code(.mcp.json 或 `claude mcp add`):
    {"mcpServers": {"modal-bridge": {
        "command": "python", "args": ["<repo>/mcp_server.py"],
        "env": {"MODAL_BRIDGE_URL": "http://127.0.0.1:8000",
                "MODAL_BRIDGE_LOCAL_CAPABILITY": "<local_api_capability>"}}}}
  cloud 模式只换 env:
        "env": {"MODAL_BRIDGE_ENDPOINT": "https://<ws>--comfyui-bridge",
                "MODAL_BRIDGE_KEY": "<bridge_api_key>"}
  Codex(~/.codex/config.toml):
    [mcp_servers.modal_bridge]
    command = "python"
    args = ["<repo>/mcp_server.py"]
    env = { MODAL_BRIDGE_URL = "http://127.0.0.1:8000", MODAL_BRIDGE_LOCAL_CAPABILITY = "<local_api_capability>" }

    示例值仅占位；真实 capability 只放私有环境/配置，不能提交到仓库或发送给 agent。

代理:两种模式均继承系统代理；localhost/内网目标通过 no_proxy/NO_PROXY 直连。
远程 BASE 同样保留代理，不以禁用系统代理作为连接失败时的回退。
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from bridge_client import (BridgeClient, BridgeError, SubmitUnknown, _open_http, _safe_job_id,
                           cancel_still_billing, never_sent)

try:                                        # mcp >= 2.0
    from mcp.server import MCPServer as _Server
except ImportError:                         # mcp 1.x(FastMCP 时代)
    from mcp.server.fastmcp import FastMCP as _Server

BASE = os.environ.get("MODAL_BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
_ENDPOINT = os.environ.get("MODAL_BRIDGE_ENDPOINT", "").strip()
MODE = "cloud" if _ENDPOINT else "local"

_client = None
if MODE == "cloud":
    _client = BridgeClient(_ENDPOINT, os.environ.get("MODAL_BRIDGE_KEY", ""))

# ⚠ 启动时就转成绝对路径:返回给 agent 的产物路径要能直接用。相对路径的含义取决于 MCP 进程的 cwd,
#   而 agent 自己的 cwd 往往不是它(2026-10-05 深度 review)。
_OUT_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("MODAL_BRIDGE_OUT_DIR") or "./modal_bridge_outputs"))
# os.pathsep 而不是 ":" —— Windows 路径自带盘符冒号(C:\x),按 ":" 切会切碎
_INPUT_DIRS = [d for d in os.environ.get("MODAL_BRIDGE_INPUT_DIRS", ".").split(os.pathsep) if d]
# cloud 模式手选档位的合法值。云端对不认识的值静默回落 primary —— 写错(如 "B200")不会报错,
# 只会悄悄跑在主卡上,所以在这里先挡(2026-10-05 深度 review)。
_GPU_CLASSES = ("primary", "cheap", "top")
_LOCAL_CAPABILITY = os.environ.get("MODAL_BRIDGE_LOCAL_CAPABILITY", "").strip()
# 更推荐的写法:不把 token 放进 env / .mcp.json,而是让 MCP 进程直接读 ComfyUI 那份
# 0600 的插件 config.json(两者同机)。env 里给路径,不给值。
_LOCAL_CONFIG = os.environ.get("MODAL_BRIDGE_LOCAL_CONFIG", "").strip()
if not _LOCAL_CAPABILITY and _LOCAL_CONFIG:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import read_local_capability_file  # noqa: E402
    _LOCAL_CAPABILITY = read_local_capability_file(_LOCAL_CONFIG)


def _request(path: str, body: dict | None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if _LOCAL_CAPABILITY:
        headers["X-Modal-Bridge-Capability"] = _LOCAL_CAPABILITY
    return urllib.request.Request(f"{BASE}{path}", data=data, headers=headers)


def _http_error_body(e: urllib.error.HTTPError, path: str) -> tuple[dict, bool]:
    """本机 HTTP 错误 → (给 agent 的 dict, 正文是不是插件自己回的 JSON 对象)。"""
    try:
        body = json.loads(e.read().decode())
    except Exception:
        body = None
    ours = isinstance(body, dict)
    if not ours:
        body = {"error": f"HTTP {e.code} {path}"}
    if e.code == 403 and e.headers.get("X-Modal-Bridge-Auth") == "capability-required":
        # 0.8.36 起 localhost 也要 capability。agent 看到裸 403 不知道该配什么,这里把
        # 两种给法都说清楚;值本身不进日志、不进聊天。
        body = dict(body)
        body["hint"] = ("缺少或错误的 X-Modal-Bridge-Capability。给 MCP 进程设 "
                        "MODAL_BRIDGE_LOCAL_CONFIG=<ComfyUI 的插件 config.json 路径>"
                        "(推荐,值不出文件),或 MODAL_BRIDGE_LOCAL_CAPABILITY=<值>。"
                        f"当前:{'已从文件读到值' if _LOCAL_CAPABILITY else '两者都未设置'}。")
    return body, ours


def _call(path: str, body: dict | None = None, timeout: int = 120) -> dict:
    req = _request(path, body)
    try:
        # 与独立客户端共用逐跳同源校验；不能将本地管理 capability 带给重定向目标。
        with _open_http(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
        # 调用方一律按 dict 读(.get);别让一个 JSON 数组变成 AttributeError
        return out if isinstance(out, dict) else {"error": f"{path} 返回的不是 JSON 对象"}
    except urllib.error.HTTPError as e:
        return _http_error_body(e, path)[0]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e} (ComfyUI 在跑吗? BASE={BASE})"}


# 本机 /submit 的超时(契约 D2,2026-10-05 深度 review 第二轮)。/submit 里 modal_client.submit_job 最坏
# 4 次 × 60s + 退避 1.5+3+6s = 250.5s,前面还有模型扫描、每个节点目录跑 git、读输入图转 base64。以前用
# _call 的默认 120s:本机还在重试,MCP 先超时,手里没有 job_id,agent 只能重交 —— 云端多一个孤儿任务。
_SUBMIT_TIMEOUT_S = 330


def _submit_local(prompt: dict) -> dict:
    """local 模式的提交。job_id 由这里先定、随请求带给本机 /submit(契约 D2),这一跳出任何岔子都能交还它。

    结局分三种:
      · 本机 /submit 给了答复(成功,或它自己的 {error[, job_id, outcome]}):原样返回;
      · 请求确定没发出去(连接被拒 = ComfyUI 没开、DNS、TLS,判据同 bridge_client.never_sent):
        普通错误,**没有提交**,ComfyUI 起来后重交即可;
      · 其余(超时、读响应时断连、网关 5xx、回了看不懂的东西):本机可能已经把任务交上云端 ——
        {ok:false, outcome:"unknown", job_id, error},agent 拿 job_id 去 job_status 核实,别重交。"""
    job_id = str(uuid.uuid4())
    path = "/modal_bridge/submit"
    unknown = {"ok": False, "outcome": "unknown", "job_id": job_id}
    try:
        with _open_http(_request(path, {"prompt": prompt, "job_id": job_id}), timeout=_SUBMIT_TIMEOUT_S) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body, ours = _http_error_body(e, path)
        if ours or (e.code < 500 and e.code not in (408, 429)):
            return body           # 插件自己的答复(含它判出的 outcome:unknown),或代理 / 网关的确定拒绝
        return {**unknown, "error": f"本机 /submit 回了 HTTP {e.code}(不是插件的答复),提交结果未知 —— "
                                    f"按 job_id 用 job_status 核实,别重新提交"}
    except Exception as e:
        if never_sent(e):
            return {"ok": False, "error": f"连不上本机 ComfyUI({type(e).__name__}: {e};BASE={BASE}),"
                                          f"请求没有发出去,任务没有提交。ComfyUI 起来后重新提交即可"}
        return {**unknown, "error": f"等本机 /submit 答复时出错({type(e).__name__}: {e}),提交结果未知 —— "
                                    f"任务可能已经交上云端:按 job_id 用 job_status 核实,别重新提交(会双跑双计费)"}
    try:
        out = json.loads(raw.decode())
    except ValueError:
        out = None
    if not isinstance(out, dict):
        return {**unknown, "error": "本机 /submit 回了看不懂的内容,提交结果未知 —— 按 job_id 用 job_status 核实"}
    return out


def _parse_prompt(workflow_json: str) -> dict | None:
    try:
        p = json.loads(workflow_json)
        return p if isinstance(p, dict) else None
    except Exception:
        return None


def _cloud(fn):
    """cloud 模式工具体的统一异常兜底:BridgeError → {ok:false, error}。
    SubmitUnknown 与本机 /submit 同形(契约 C3):{error, job_id, outcome:"unknown"} —— job_id 必须交还,
    任务可能已在云端跑。"""
    try:
        return fn()
    except SubmitUnknown as e:
        return {"ok": False, "error": str(e), "job_id": e.job_id, "outcome": "unknown"}
    except BridgeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _b64_size(b64) -> int:
    """base64 文本 → 原始字节数(不解码)。"""
    if not isinstance(b64, str):
        return 0
    t = "".join(b64.split())
    return max(0, len(t) * 3 // 4 - (len(t) - len(t.rstrip("="))))


def _slim(state):
    """状态响应进 agent 上下文之前,剥掉 base64 产物(images[*].data_base64 与旧版顶层 data_base64),
    换成文件数和大小。⚠ 以前原样透传,一次 job_status 实测灌进 5.3 MB(2026-10-05 深度 review)。
    只改返回给 agent 的那份;本机 /fetch_result 要的 modal_state 仍传完整状态。"""
    if not isinstance(state, dict):
        return state
    out = dict(state)
    count = total = 0
    stripped = False
    imgs = state.get("images")
    if isinstance(imgs, list):
        slim_imgs = []
        for img in imgs:
            if isinstance(img, dict):
                d = {k: v for k, v in img.items() if k != "data_base64"}
                if "data_base64" in img:
                    stripped = True
                    d.setdefault("size_bytes", _b64_size(img["data_base64"]))
                if isinstance(d.get("size_bytes"), int):
                    total += d["size_bytes"]
                count += 1
                slim_imgs.append(d)
            else:
                slim_imgs.append(img)
        out["images"] = slim_imgs
    if "data_base64" in out:
        b64 = out.pop("data_base64")
        stripped = True
        if not (isinstance(imgs, list) and imgs):   # 旧版单产物形态:顶层 filename + data_base64
            count += 1
            total += _b64_size(b64)
    if count:
        out["outputs_summary"] = {"count": count, "total_bytes": total}
    if stripped:
        out["outputs_note"] = "产物内容(base64)已省略,不进上下文;completed 后用 fetch_result 取回落盘"
    return out


def _cancel_view(r):
    """取消结果 → 统一形态:ok = not still_billing(契约 C2)。本机 /cancel 已带 still_billing 时以它为准;
    老版本插件 / cloud 模式没有这个字段,按同一套规则从 status / cancel_noop / error 推出来。"""
    r = _slim(r) if isinstance(r, dict) else {"error": f"取消响应不是 JSON 对象: {str(r)[:200]}"}
    sb = cancel_still_billing(r)
    return {**r, "ok": not sb, "still_billing": sb}


_NOT_READY = ("queued", "running", "delivering")
_TERMINAL_FAIL = ("failed", "cancelled", "not_found")


def _not_fetchable(job_id: str, state) -> dict:
    """fetch_result 遇到非 completed:not_ready 只给还会走到 completed 的状态;终态给 terminal。
    ⚠ 以前 failed / cancelled / not_found 也回 not_ready:true,docstring 还写「方便直接重试」——
      agent 会对一个已失败的任务一直重试下去(2026-10-05 深度 review)。"""
    st = state.get("status") if isinstance(state, dict) else None
    slim = _slim(state) if isinstance(state, dict) else {}
    if st in _NOT_READY:
        return {**slim, "ok": False, "not_ready": True}
    if st in _TERMINAL_FAIL:
        err = slim.get("error") or {"failed": "任务失败(云端没给原因)", "cancelled": "任务已取消"}.get(st)
        if st == "not_found":
            err = (f"云端查无此 job({slim.get('error') or 'job not found'}):job_id 不对,或终态已过保留期被清理。"
                   "刚提交的任务可能短暂查不到 —— 用 job_status 连续确认(见其说明)再下结论")
        return {"ok": False, "terminal": True, "job_id": job_id, "status": st, "error": err}
    return {**slim, "ok": False,
            "error": slim.get("error") or f"查不到可用的状态(status={st!r}),稍后重试 job_status"}


mcp = _Server("modal-bridge")


@mcp.tool()
def submit_workflow(workflow_json: str, gpu_class: str = "") -> dict:
    """提交 ComfyUI 工作流到 Modal 云端 GPU 跑。workflow_json 是 API prompt 的 JSON 字符串
    ({node_id:{class_type,inputs}} 格式,非画布 JSON)。
    local 模式:后端自动做 GPU 档位路由与输入图打包,gpu_class 参数被忽略。
    cloud 模式:gpu_class ∈ primary(默认)/cheap/top 手选档位,其它值直接报错;LoadImage 引用的
    输入图按 MODAL_BRIDGE_INPUT_DIRS 搜索打包。
    返回含 job_id;随后 job_status 轮询,完成后 fetch_result 取产物。
    ⚠ 返回 outcome:"unknown"(带 job_id)= 提交结果不确定,任务**可能已在云端跑、在计费**:
    按这个 job_id 照常 job_status 轮询核实(not_found 连续出现才作数),**不要重新提交**(会双跑双计费)。
    local 模式等本机答复最多约 330s;本机 ComfyUI 没开(连接被拒)时是普通错误,没有提交,可直接重交。
    轮询 deadline:local 模式用返回的 worker_timeout_sec+180s;cloud 模式问部署者(默认按 3600s)。"""
    prompt = _parse_prompt(workflow_json)
    if prompt is None:
        return {"error": "workflow_json 必须是 API prompt 的 JSON 对象字符串"}
    if MODE == "cloud":
        gc = (gpu_class or "primary").strip().lower()
        if gc not in _GPU_CLASSES:
            return {"ok": False, "error": f"gpu_class 只能是 {' / '.join(_GPU_CLASSES)}(收到 {gpu_class!r})。"
                                          "云端对不认识的值会静默按 primary 跑,所以这里直接拒绝"}

        def _do():
            imgs = _client.pack_input_images(prompt, _INPUT_DIRS)
            d = _client.submit(prompt, input_images=imgs or None, gpu_class=gc)
            return {"ok": True, "job_id": d["id"], "gpu": d.get("gpu"), "mode": "cloud"}
        return _cloud(_do)
    return _submit_local(prompt)


def _poll_path(job_id: str) -> str:
    # job_id 来自 agent,原样拼进 query 会被 & / # / 空格改写语义
    return "/modal_bridge/poll?" + urllib.parse.urlencode({"job_id": job_id})


@mcp.tool()
def job_status(job_id: str) -> dict:
    """查任务状态。status ∈ queued / running / delivering(算完了、产物正在交付,还不能取)/
    completed / failed / cancelled / not_found。running 时带 progress:{step,total,s_it,elapsed}
    (s_it 为滑窗中位数,n_samples≥3 才可信)。
    ⚠ not_found 一次不作数:云端记录跨容器最终一致,刚提交的任务可能短暂查不到。要**连续 5 次**
    (间隔 ≥2s)都是 not_found 才能判定任务不存在 / 已过保留期被清理;中间只要出现别的状态就重新计数。
    local 模式还可能有:auth_failed(本机与云端的 bridge key 不一致,重新部署会刷新 key;是终局,别再轮询)、
    unknown(云端这一拍出错,按瞬态处理继续轮询,不计入 not_found)。
    返回 ok:false + error 而没有 status = 这一拍没查到(网络 / 云端出错),不是任务失败,稍后重试。
    返回里不含产物内容(base64 已省略,换成 outputs_summary 的文件数 / 字节数);completed 后用 fetch_result 取回。
    ⚠ 显存不足不报错、只静默降速:s_it 显著高于同配置基线即是信号。"""
    if MODE == "cloud":
        return _cloud(lambda: _slim(_client.status(job_id)))
    return _slim(_call(_poll_path(job_id)))


@mcp.tool()
def fetch_result(job_id: str) -> dict:
    """任务 completed 后取回产物。local 模式写入 ComfyUI/output/<subfolder>/<job_id>/;
    cloud 模式写入 MODAL_BRIDGE_OUT_DIR/<job_id>/(绝对路径;大文件经云端 /fetch 流式下载,不需要 modal token)。
    成功:{ok:true, job_id, outputs:[{filename, path, size_bytes}]}。同一任务重复调用是安全的:
    已经取回过的直接返回本地文件(cloud 模式 already_fetched:true)。
    还没完成(queued / running / delivering):{ok:false, not_ready:true, status, ...},稍后再调。
    已经结束但没有产物(failed / cancelled / not_found):{ok:false, terminal:true, status, error} ——
    重试没有用,别再调;要结果就修正后用新的提交重跑。"""
    if MODE == "cloud":
        # job_id 会拼进本地落盘路径,先按云端同一条规则(契约 C1)校验,挡住 "../x" 这类越界
        if not _safe_job_id(job_id):
            return {"ok": False, "error": f"job_id 不合法: {str(job_id)[:80]!r}"}
        out_dir = os.path.join(_OUT_DIR, job_id)

        def _do():
            # 先看本地回执:取回成功后云端副本已删(ack),终态记录过了保留期也会被清掉 ——
            # 这时再问云端只会得到 404 / not_found,而文件就在本地(2026-10-05 深度 review)
            done = BridgeClient.received_outputs(out_dir, job_id)
            if done:
                return {"ok": True, "job_id": job_id, "outputs": done, "already_fetched": True}
            state = _client.status(job_id)
            if state.get("status") != "completed":
                return _not_fetchable(job_id, state)
            outs = _client.download_outputs(state, out_dir)
            return {"ok": True, "job_id": job_id, "outputs": outs}
        return _cloud(_do)
    state = _call(_poll_path(job_id))
    if state.get("status") != "completed":
        return _not_fetchable(job_id, state)
    # 本机 /fetch_result 要完整状态(base64 产物在里面),这里不能 _slim
    return _call("/modal_bridge/fetch_result", {"job_id": job_id, "modal_state": state},
                 timeout=600)


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """取消云端任务。⚠ 必须检查返回,看 still_billing(ok 恒等于 not still_billing):
    - still_billing:true → 取消没成功,云端**可能仍在跑、仍在计费**,去 Modal 控制台确认;
    - status:"cancelled" → 取消成功;
    - cancel_noop:true → 任务在取消前已经结束(完成 / 失败),不在计费;此时的 error 是**任务自己的**
      失败原因,不是取消失败;completed 的产物照常可用 fetch_result 取;
    - status:"not_found" → 云端没有这个任务(id 不对 / 早已结束并被回收),没有东西在跑。"""
    if MODE == "cloud":
        def _do():
            try:
                r = _client.cancel(job_id)
            except BridgeError as e:
                # 请求本身失败(HTTP 错误 / 网络):不知道取消了没有,只能按仍在计费报
                return {"ok": False, "still_billing": True, "error": f"取消请求失败,结果未知: {e}"}
            return _cancel_view(r)
        return _cloud(_do)
    return _cancel_view(_call("/modal_bridge/cancel", {"job_id": job_id}))


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
