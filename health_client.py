"""
health_client.py — 读云端 /health 的**唯一一份**规则:URL、鉴权头、状态码语义。同步 / 异步共用。

⚠ 以前有好几份各写各的:modal_client.health 遇 401 抛清楚的错,node_sync.fetch_cloud_nodes 却把
  401、超时、未部署统统当成「拿不到」返回空 —— 部署前的节点清单保护因此在 key 不对 / 网络抖动时
  **静默跳过**,若恰好本机清单丢了,就是清空云端全部节点(2026-09-24 review #14)。
  这里把「为什么拿不到」分清楚(HealthUnavailable.kind),调用方才能决定该继续还是该停。

只用标准库:CLI(bridge_cli / deploy.py)的环境不一定装了 aiohttp。
"""
import json
import urllib.error
import urllib.request

try:
    from .bridge_client import _open_http
except ImportError:   # CLI / 测试把插件目录直接放进 sys.path
    from bridge_client import _open_http


class HealthUnavailable(RuntimeError):
    """kind:
    not_deployed  404:app 还没部署 / 已被删除(或还没有 endpoint)
    unauthorized  401:bridge key 不对 / 缺失
    http          其它 4xx / 5xx
    unreachable   网络错 / 超时 / 响应不是 JSON 对象
    """

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


def url(cfg: dict) -> str:
    return f"{(cfg.get('modal_endpoint_base') or '').rstrip('/')}-health.modal.run"


def headers(cfg: dict) -> dict:
    # GET 走 X-Bridge-Key 头,不进 query(免得落进反代 / CDN 日志),同 modal_client._key
    return {"X-Bridge-Key": cfg.get("bridge_api_key", "")}


def interpret(status: int, body: str) -> dict:
    """状态码 + 响应体 → info dict;不可用时抛带 kind 的 HealthUnavailable。

    ⚠ 必须查状态码:401 的 body 也是合法 JSON({"error": ...}),无脑解析会把「key 错了」
      包装成「健康检查通过」—— 最误导的一类假阳性。"""
    if status == 401:
        raise HealthUnavailable("unauthorized", "Modal /health 401 — bridge key 不对/缺失。"
                                                "点 [Modal Setup] 重新部署会刷新 key")
    if status == 404:
        raise HealthUnavailable("not_deployed", "Modal /health 404 — app 还没部署或已被删除")
    if 300 <= status < 400:
        # 异步版(aiohttp)不跟随重定向,3xx 原样到这里。别落进下面的 JSON 解析报成「不是 JSON」。
        raise HealthUnavailable("http", f"Modal /health 返回重定向 {status},没有跟随(会把 bridge key 带过去)")
    if status >= 400:
        raise HealthUnavailable("http", f"Modal /health {status}: {body[:200]}")
    try:
        info = json.loads(body)
    except ValueError:
        raise HealthUnavailable("unreachable", f"Modal /health 返回的不是 JSON: {body[:120]}") from None
    if not isinstance(info, dict):
        raise HealthUnavailable("unreachable", "Modal /health 返回的不是 JSON 对象")
    return info


def fetch(cfg: dict, timeout: float = 20) -> dict:
    """同步取 /health(单次,不重试)。拿不到抛 HealthUnavailable。"""
    if not cfg.get("modal_endpoint_base"):
        raise HealthUnavailable("not_deployed", "还没有 endpoint(从未部署过)")
    req = urllib.request.Request(url(cfg), headers=headers(cfg))
    try:
        # ⚠ 不能用裸 urlopen:默认的重定向处理会把 X-Bridge-Key 原样带去跳转目标,跨域也照带
        #   (2026-09-26 review 用两个本地服务实测)。_open_http 只跟同源重定向,与 bridge_client /
        #   mcp_server 同一道守卫。
        with _open_http(req, timeout=timeout) as r:
            return interpret(r.status, r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return interpret(e.code, e.read().decode("utf-8", "replace") if e.fp else "")
    except HealthUnavailable:
        raise
    except Exception as e:
        raise HealthUnavailable("unreachable", f"Modal /health 不可达: {type(e).__name__}: {e}") from None
