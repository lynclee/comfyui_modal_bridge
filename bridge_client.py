"""
bridge_client.py — Modal Bridge 云端协议的独立客户端(零依赖:纯 Python 标准库)。

不需要 ComfyUI、不需要 modal SDK、不需要本插件的其它模块 —— 单文件即可
submit / status / wait / cancel / health / 产物落盘。是「脱离本地 ComfyUI 使用」
的共用引擎:mcp_server.py 的 cloud 模式和 bridge_cli.py 都基于它。

前提:有人(通常是部署者)已经用完整插件部署过云端 app、模型已在 Volume、
所需 custom_node 已在镜像 —— 本客户端只消费能力,不搬运模型/节点。

鉴权:bridge_api_key(部署时生成,存部署者的 config.json)。GET 走 X-Bridge-Key 头、
POST 走 body 的 auth_key;云端仍兼容旧的 ?key=,但本客户端不再往 query 里放 key。
大文件:worker 把 >阈值 的产物写 Volume,状态里给 volume_path;本客户端经云端
/fetch 端点(0.7.3+)流式下载,不需要 modal token。

网络:云端是外网地址,walk 系统代理(env http_proxy/https_proxy)—— 与 mcp_server 的
localhost 直连策略相反,勿混用。
"""
import base64
import errno
import json
import mimetypes
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class BridgeError(RuntimeError):
    pass


# 报错文本里的 URL userinfo(user:pass@)一律打码:urllib 的网络错有时会带出代理地址,而本机代理 env
# 常是 http://USER:PASS@host:port。comfyagent 的 vendor 副本先加了这一条(同名、同规则),2026-10-05
# 深度 review 第二轮收回上游,免得两边 vendor 时互相覆盖。
_CREDS_IN_URL = re.compile(r"([a-zA-Z][\w+.-]*://)[^/@\s]*:[^/@\s]*@")


def _scrub_credentials(text) -> str:
    return _CREDS_IN_URL.sub(r"\1***:***@", str(text))


class BridgeHTTPError(BridgeError):
    """云端回了非 2xx。.status = HTTP 状态码,.body = 正文(能解析成 JSON 就是解析结果,否则是截断的文本)。

    ⚠ 以前 _req 把 HTTP 错误的 JSON 正文当正常响应返回:/status 回 500 + {"error"} 时 wait
      拿到一份没有 status 的 dict,一路空转到 timeout_s(默认 1 小时);/cancel 回 500 时 CLI
      退出码是 0(2026-10-05 深度 review)。正常的业务答复(/run 的 duplicate、/status 与
      /cancel 的 not_found)云端都是 HTTP 200,所以「≥ 400 一律是错」不会误伤它们。"""

    def __init__(self, msg: str, status: int, body=None):
        super().__init__(msg)
        self.status = status
        self.body = body


class SubmitUnknown(BridgeError):
    """/run 的重试全部失败,而**至少有一次请求可能已经到达云端** —— 任务也许已在排队 / 运行、在计费。

    .job_id 是这次提交用的幂等键。调用方必须拿它去 status / cancel 核实,**绝不能换新 id 重新提交**
    (那是双跑双计费)。以前重试用尽只抛一个不带 id 的 BridgeError,调用方手里没有任何能查的东西,
    云端那个任务就成了谁也看不见的孤儿(2026-10-05 深度 review,契约 C3)。
    「确定没提交」(401、其它 4xx、HTTP 200 + {"error"}、每次都失败在连接阶段)不抛它,照旧抛普通
    BridgeError —— 但前面有过结果不确定的尝试时,之后的拒收也抛它(分类规则见 BridgeClient.submit)。"""

    def __init__(self, msg: str, job_id: str):
        super().__init__(msg)
        self.job_id = job_id


# 与云端 modal_app._SAFE_JOB_ID / 本机 contract.is_safe_job_id 同一条规则(契约 C1)。本模块是零依赖、
# 可整份 vendor 的单文件,不能 import 它们,所以自带一份,由 tests/test_fix_client.py 钉住字面量。
# 用在回执文件名里:job_id 会拼进 out_dir 下的路径,不合规的就不写回执。
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


# wait 认得的全部状态(终态 + 非终态 + not_found)。不在其中的视为这一拍没看到,计入错误次数。
_KNOWN_STATUSES = ("queued", "running", "delivering", "completed", "failed", "cancelled", "not_found")


def _safe_job_id(job_id) -> bool:
    # fullmatch 而不是 match:`$` 会放过结尾的 "\n"
    return (isinstance(job_id, str) and bool(_SAFE_JOB_ID.fullmatch(job_id))
            and ".." not in job_id)


def cancel_still_billing(resp) -> bool:
    """/cancel 的响应 → 任务是否**可能仍在跑、仍在计费**(契约 C2)。

    - 带 still_billing 字段(本机 /cancel 0.8.59+)→ 以它为准;
    - status == "not_found":云端没有这个任务,没有东西在跑;
    - cancel_noop:任务在取消之前已经结束(完成 / 失败 / 被判死)—— 这时的 error 是**任务自己的**
      失败原因,不是取消失败;
    - 其余带 error 的(含 "cancel failed: …"、「正在提交中,还拿不到句柄」)→ 取消没成功,可能仍在计费;
    - 都没有 → 取消成功。
    ⚠ 以前文档一律写「error / ok:false = 仍在计费」,对 cancel_noop 和 not_found 正好说反
      (2026-10-05 深度 review)。拿不准(不是 dict)时按「仍在计费」报 —— 失败方向是安全的。"""
    if not isinstance(resp, dict):
        return True
    if "still_billing" in resp:
        return bool(resp["still_billing"])
    if resp.get("status") == "not_found" or resp.get("cancel_noop"):
        return False
    return bool(resp.get("error"))


_NO_ROUTE_ERRNOS = frozenset(x for x in (getattr(errno, "ENETUNREACH", None), getattr(errno, "EHOSTUNREACH", None),
                                          getattr(errno, "EADDRNOTAVAIL", None)) if x is not None)


def never_sent(exc) -> bool:
    """urllib 的异常 → 这次请求是否**确定没有到达服务端**(失败在连接阶段,一个字节的请求都没发出去)。

    只认 URLError 包着的这几种 reason:DNS 解析失败(socket.gaierror)、连接被拒(ConnectionRefusedError)、
    网络 / 主机不可达、TLS 握手失败(ssl.SSLError,含证书校验)、代理 CONNECT 失败(http.client 抛的
    OSError「Tunnel connection failed: …」)。为什么 URLError 可信:urllib 只把 h.request() 里(建连、
    TLS 握手、代理隧道、发请求)抛出的 OSError 包成 URLError;读响应阶段的超时 / 断连
    (TimeoutError、RemoteDisconnected、ConnectionResetError)原样抛出,不会走到这里。
    **超时一律不算**:URLError(TimeoutError) 分不清是连接超时还是请求发到一半超时,按结果未知处理
    (方向安全:多一次 status 核实,而不是双跑双计费)。HTTPError 也是 URLError 的子类,先排除。
    给 submit 分类用,也给 mcp_server 判断本机 ComfyUI 那一跳(2026-10-05 深度 review 第二轮)。"""
    if isinstance(exc, urllib.error.HTTPError) or not isinstance(exc, urllib.error.URLError):
        return False
    r = exc.reason
    if isinstance(r, (socket.gaierror, ConnectionRefusedError, ssl.SSLError)):
        return True
    if isinstance(r, OSError) and getattr(r, "errno", None) in _NO_ROUTE_ERRNOS:
        return True
    return isinstance(r, OSError) and str(r).startswith("Tunnel connection failed")


def _err_body(e: urllib.error.HTTPError):
    """HTTPError 的正文 → (解析结果或截断文本, 用于报错的单行文本)。读不出来就是空串。"""
    try:
        raw = e.read() if e.fp is not None else b""
    except Exception:
        raw = b""
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = text[:500]
    return body, " ".join(text.split())[:300]


def _assert_http_url(url: str) -> None:
    """初始 URL 和每次重定向都必须经过校验。"""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError(f"refusing non-http(s) URL: {url[:80]!r}")
    parsed.port  # 同时拒绝非法端口


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        def origin(url):
            _assert_http_url(url)
            u = urllib.parse.urlsplit(url)
            return u.scheme, u.hostname, u.port if u.port is not None else (443 if u.scheme == "https" else 80)

        if origin(req.full_url) != origin(newurl):
            raise ValueError("refusing cross-origin or HTTPS-downgrade redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_http(req, timeout):
    _assert_http_url(req.full_url)
    # 保留默认 ProxyHandler(继承系统代理)，不修改进程全局 opener。
    return urllib.request.build_opener(_SameOriginRedirect()).open(req, timeout=timeout)


class BridgeClient:
    def __init__(self, endpoint_base: str, key: str, timeout: int = 60):
        """endpoint_base 形如 https://<workspace>--comfyui-bridge(与 config.modal_endpoint_base 同)。"""
        if not endpoint_base or "--" not in endpoint_base:
            raise BridgeError("endpoint_base 形如 https://<workspace>--comfyui-bridge")
        self.base = endpoint_base.rstrip("/")
        self.key = key or ""
        self.timeout = timeout

    # ── 底层 ──
    def _url(self, label: str) -> str:
        return f"{self.base}-{label}.modal.run"

    def _get(self, label: str, params: dict, timeout: int | None = None) -> dict:
        # key 走 X-Bridge-Key 头,不进 query —— query string 会落进反代 / CDN 日志、
        # 浏览器历史和 Referer。云端 ≥0.8.3 认这个头(旧版只认 ?key=,会返 401)。
        qs = urllib.parse.urlencode(params)
        return self._req(f"{self._url(label)}?{qs}", None, timeout)

    def _post(self, label: str, body: dict, timeout: int | None = None, retries: int = 1) -> dict:
        return self._req(self._url(label), {**body, "auth_key": self.key}, timeout, retries)

    def _req(self, url: str, body: dict | None, timeout: int | None, retries: int = 1) -> dict:
        """发一次请求(502/503/504 与网络错按 retries 重试),返回解析后的 JSON。
        HTTP 非 2xx 一律抛 BridgeHTTPError(带状态码与正文);网络错 / 超时 / 非 JSON 抛 BridgeError。"""
        data = json.dumps(body).encode() if body is not None else None
        last: Exception | None = None
        for attempt in range(retries + 1):
            headers = {"X-Bridge-Key": self.key}
            if data:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers)
            try:
                with _open_http(req, timeout=timeout or self.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise BridgeHTTPError(
                        "401 unauthorized — bridge key 不对/缺失;"
                        "若 key 没变过,多半是云端版本 < 0.8.3(还不认 X-Bridge-Key 头),"
                        "在 Modal 面板重新部署一次即可", 401, _err_body(e)[0]) from None
                if e.code in (502, 503, 504) and attempt < retries:
                    last = BridgeError(f"transient {e.code}")
                    time.sleep(1.5)
                    continue
                # ⚠ 不再把错误正文当正常响应返回,见 BridgeHTTPError 的说明(2026-10-05 深度 review)
                body_obj, text = _err_body(e)
                raise BridgeHTTPError(f"HTTP {e.code} {url.split('?', 1)[0]}: {text or '(无正文)'}",
                                      e.code, body_obj) from None
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5)
        raise BridgeError(f"request failed: {_scrub_credentials(last)}") from last

    # ── 协议 ──
    # /run 的重试间隔(秒),条数 = 重试次数(共 1 + len 次尝试)。指数退避:网关 504 多半是
    # run_endpoint 冷启动慢,紧接着重试大概率撞同一个冷容器。测试里置 0。
    _SUBMIT_RETRY_DELAYS = (1.5, 3.0, 6.0)

    def submit(self, workflow: dict, *, input_images: list | None = None,
               gpu_class: str = "primary", needs_gpu: bool = True,
               tier: str = "40g", job_id: str | None = None) -> dict:
        """提交工作流(API prompt 格式)。返回 {id, status, gpu}(重复提交同一 job_id 时多一个 duplicate)。
        gpu_class ∈ primary/cheap/top(部署时绑的卡型);needs_gpu=False → CPU worker(纯 API 工作流)。

        失败分两种,调用方必须分开处理:
        - BridgeError:**确定没提交**,改好再提交即可。消息一律以 `401` 或 `/run:` 开头(comfyagent 的
          `_definitely_rejected` 按这两个前缀认,别改):401(key 不对)、其它 4xx(`/run: HTTP 422 …`)、
          云端回 {"error"} 拒收(`/run: …`)、每次尝试都失败在连接阶段(`/run: 连不上云端…`);
        - SubmitUnknown(BridgeError 的子类,带 .job_id):任务**可能已在云端跑**。拿 .job_id 去
          status / cancel 核实,别重新提交。

        每次尝试的结局分三类,决定重不重试、最后抛哪一种(2026-10-05 深度 review 第二轮):
          · 没发出去:连接阶段就失败(DNS / 连接被拒 / 不可达 / TLS 握手 / 代理 CONNECT,判据见 never_sent)。
            请求没到云端,重试是安全的;**全部**尝试都是这一类 → 确定没提交。
          · 结果不确定:超时、读响应时断连、5xx / 408 / 429、非 JSON、缺 id、拒绝跟随的重定向 —— 请求可能
            已经到了云端、spawn 了。重试(同一个 job_id,云端会回 duplicate,不会双跑)。
          · 确定拒收:401 / 其它 4xx / HTTP 200 + {"error"}。不重试。
        只要**之前**有过一次结果不确定的尝试,之后的确定拒收也抛 SubmitUnknown(附拒收原文):前一次
        可能已经 spawn 了,后一次的 401 / 404 说明不了前一次的结局。以前直接抛「确定没提交」,
        job_id 跟着丢了,调用方会换新 id 重交(2026-10-05 深度 review 第二轮)。"""
        payload = {"workflow": workflow, "tier": tier, "needs_gpu": bool(needs_gpu),
                   "gpu_class": gpu_class, "delivery": {"mode": "desktop"},
                   "user_id": "bridge-client"}
        if input_images:
            payload["images"] = input_images
        # 幂等键:客户端定 job_id,重试用同一个 —— spawn 可能在第一次就已经成功(响应丢在网关)。
        # 不带 id 的话服务端每次新建 uuid,重试 = 再开一个一模一样的 GPU 任务,双跑双计费,
        # 调用方只看得到第二个。带了 id,重试撞上已有记录时云端回 duplicate 而不重复 spawn。
        job_id = payload["job_id"] = job_id or str(uuid.uuid4())
        delays = tuple(self._SUBMIT_RETRY_DELAYS)
        last: Exception | None = None
        uncertain = 0            # 结果不确定的尝试次数(可能已经到了云端)
        for attempt in range(len(delays) + 1):
            if attempt:
                time.sleep(delays[attempt - 1])
            try:
                d = self._post("run", payload, retries=0)
            except BridgeHTTPError as e:
                # 4xx = 请求被拒、没有进到 spawn(408 / 429 除外:超时与限流不说明请求被处理了没有)
                if 400 <= e.status < 500 and e.status not in (408, 429):
                    # ⚠ 前缀 `/run:`:comfyagent 按它认「确定拒收」。以前是 "HTTP 422 https://…",
                    #   422 / 413 / 404 在那边全被当成结果未知(2026-10-05 深度 review 第二轮)。401 原样
                    #   (消息以 "401" 开头,那边本来就认)。
                    rejected = e if e.status == 401 else BridgeHTTPError(f"/run: {e}", e.status, e.body)
                    self._raise_rejected(rejected, job_id, uncertain)
                uncertain += 1
                last = e
                continue
            except BridgeError as e:      # 网络错 / 超时 / 非 JSON / 拒绝跟随的重定向
                if not never_sent(e.__cause__):
                    uncertain += 1
                last = e
                continue
            if isinstance(d, dict) and "error" in d:
                # 云端校验没过(job_id / workflow / delivery),确定没提交
                self._raise_rejected(BridgeError(f"/run: {d['error']}"), job_id, uncertain)
            if isinstance(d, dict) and d.get("id"):
                return d
            uncertain += 1
            last = BridgeError(f"/run 响应缺 id: {str(d)[:200]}")
        if not uncertain:
            raise BridgeError(
                f"/run: 连不上云端,请求没有发出去({len(delays) + 1} 次尝试都失败在连接阶段;最后一次:{last})。"
                f"任务没有提交,网络恢复后直接重新提交即可") from last
        raise SubmitUnknown(
            f"提交结果未知 job_id={job_id}:{len(delays) + 1} 次尝试都没拿到确定答复(最后一次:{last})。"
            f"任务可能已在云端排队 / 运行 —— 先用 status/cancel 核实这个 job_id,别重新提交(会双跑双计费)",
            job_id)

    @staticmethod
    def _raise_rejected(err: BridgeError, job_id: str, uncertain: int):
        """确定拒收:之前没有结果不确定的尝试 → 原样抛(确定没提交);有过 → 抛 SubmitUnknown,附拒收原文。"""
        if not uncertain:
            raise err
        raise SubmitUnknown(
            f"提交结果未知 job_id={job_id}:前面 {uncertain} 次尝试结果不确定(请求可能已到云端并开跑),"
            f"之后这次被拒收({err})—— 拒收说明不了前一次的结局。先用 status/cancel 核实这个 job_id,"
            f"别重新提交(会双跑双计费)", job_id) from err

    def status(self, job_id: str) -> dict:
        """status ∈ queued/running/delivering/completed/failed/cancelled/not_found;
        running 带 progress:{step,total,s_it,elapsed}。

        查无此 job(id 不对 / 已过保留期被 GC)时是 HTTP 200 + status="not_found" + error,
        **不是**缺字段 —— 归一状态时别把它兜底成 running。not_found 一次不作数(云端记录跨容器
        最终一致),要连续看到几次,见 wait。

        HTTP 非 2xx、响应不是 JSON 对象、或没有字符串类型的 status 字段,一律抛 BridgeError
        (以前原样返回,wait 拿着一份没有 status 的 dict 空转到超时,2026-10-05 深度 review)。"""
        s = self._get("status", {"job_id": job_id}, timeout=20)
        if not isinstance(s, dict):
            raise BridgeError(f"/status 响应不是 JSON 对象: {str(s)[:200]}")
        if "status" not in s and "not found" in str(s.get("error") or ""):
            # 0.8.40 及更早的云端查无此 job 只回 {"error": "job not found"},没有 status 字段。
            # 归成新形态,别让下面那条把一个确定的「没有」当成查询失败。
            s = {**s, "status": "not_found"}
        if not isinstance(s.get("status"), str):
            raise BridgeError(f"/status 响应缺 status 字段: {str(s)[:200]}")
        return s

    def wait(self, job_id: str, timeout_s: int = 3600, poll_s: float = 2.0,
             on_update=None, max_consecutive_errors: int = 5) -> dict:
        """轮询到终态。on_update(state) 每次状态/进度变化时回调(打印进度用)。

        ⚠ 单次查询失败只意味着"这一拍没看到",不是终态 —— 任务在云端照常跑。
        以前任何 BridgeError 都直接打死整个 wait,而 _req 只重试一次:两次连续的
        瞬态网络错(某些网络环境一天能撞好几回)就足以让一个跑了 20 分钟的任务
        失去接管者,产物再也取不回、还继续计费。连续失败到上限才放弃,成功即清零。
        """
        deadline = time.time() + timeout_s
        last_sig = None
        errs = 0
        gone = 0
        while time.time() < deadline:
            try:
                s = self.status(job_id)
                st = s.get("status")
                if st not in _KNOWN_STATUSES:
                    # ⚠ 不认识的状态也算这一拍「没看到」,计入错误次数而不是清零。以前它会清零计数、
                    #   又不是终态,于是一路轮询到 timeout_s(默认 1 小时)才放弃(2026-10-05 深度 review)。
                    raise BridgeError(f"云端回了不认识的状态 {st!r}")
                errs = 0
            except BridgeError as e:
                errs += 1
                if errs >= max_consecutive_errors:
                    raise BridgeError(
                        f"连续 {errs} 次查询失败,放弃等待 —— 任务可能仍在云端跑,"
                        f"可用 status/cancel 接管: {e}") from None
                time.sleep(poll_s)
                continue
            if st == "not_found":
                # 云端查无此 job:id 不对,或已过 JOB_TTL_S 被 GC 清掉。它永远不会再变成终态,
                # 继续轮询就是白等到 timeout_s(默认 1 小时)才无声返回最后一份状态。
                # ⚠ 但第一次看见不能判死:job_state 是 modal.Dict,跨容器最终一致,/run 刚返回
                #   时另一个容器可能还读不到这条 —— 连续看到才作数,阈值复用错误计数那套。
                gone += 1
                if gone >= max_consecutive_errors:
                    raise BridgeError(
                        f"云端查无此 job({job_id}):job_id 不对,或已超过保留期被清理"
                        f"(连查 {gone} 次都不存在)") from None
                time.sleep(poll_s)
                continue
            gone = 0
            sig = (s.get("status"), json.dumps(s.get("progress") or {}, sort_keys=True))
            if sig != last_sig:
                last_sig = sig
                if on_update:
                    on_update(s)
            if s.get("status") in ("completed", "failed", "cancelled"):
                return s
            time.sleep(poll_s)
        raise BridgeError(f"wait timeout ({timeout_s}s) — 任务仍在云端跑,可 status/cancel")

    def cancel(self, job_id: str) -> dict:
        """取消任务,返回云端 /cancel 的原始响应。⚠ 必须检查,按契约 C2 读(cancel_still_billing 就是这套判定):
        - status == "not_found":云端没有这个任务(id 不对 / 早已结束并被回收),没有东西在跑;
        - 带 cancel_noop:任务在取消前已经结束(完成 / 失败 / 被判死),不在计费。此时的 error
          是**任务自己的**失败原因,不是取消失败;completed 时产物照常可取;
        - 其余带 error 的 → 取消没成功,云端**可能仍在跑、仍在计费**,去 Modal 控制台确认;
        - 都没有(status == "cancelled")→ 取消成功。
        HTTP 非 2xx / 响应不是 JSON 对象时抛 BridgeError(取消结果未知,同样按可能仍在计费处理)。"""
        r = self._post("cancel", {"job_id": job_id}, timeout=20)
        if not isinstance(r, dict):
            raise BridgeError(f"/cancel 响应不是 JSON 对象: {str(r)[:200]}")
        return r

    def health(self) -> dict:
        h = self._get("health", {}, timeout=15)
        if not isinstance(h, dict):
            raise BridgeError(f"/health 响应不是 JSON 对象: {str(h)[:200]}")
        return h

    # ── 产物落盘 ──
    @staticmethod
    def received_outputs(out_dir: str, job_id: str) -> list[dict] | None:
        """这个任务的产物之前已经完整取回到 out_dir(有回执、文件都在、大小都对)→ 返回与
        download_outputs 相同形状的列表;否则 None。不发任何网络请求。

        用途:取回成功后云端副本已经删了(ack),终态记录过了保留期也会被 GC —— 这时再问云端
        只会得到 404 / not_found,而文件其实就在本地。先看回执,别把「已取回」报成「丢了」。"""
        out = Path(out_dir)
        rec = _load_receipt(_receipt_path(out, job_id), job_id)
        if not rec or not rec.get("complete"):
            return None
        return _receipt_outputs(out, rec)

    def download_outputs(self, state: dict, out_dir: str,
                         delete_remote: bool = True) -> list[dict]:
        """把 completed 状态里的产物写到 out_dir。小文件解 base64;大文件经云端 /fetch
        流式下载(delete_remote=True 下载后删 Volume 副本,同官方插件行为)。
        返回 [{filename, path, size_bytes}]。下载 / 落盘的任何失败都抛 BridgeError。

        **建议每个任务用独立的 out_dir**(如 <根目录>/<job_id>,CLI 与 MCP 都这么用)。共用目录时,
        目标文件已存在、又不是本任务写的,就换名加后缀(a.png → a_1.png),不覆盖 —— 以前直接覆盖,
        配合默认的 delete_remote=True,被盖掉的那份再也找不回(2026-10-05 深度 review)。

        有 Volume 产物时在 out_dir 写回执 .bridge_receipt_<job_id>.json(文件名、大小),**先写回执再 ack**。
        同一任务再取一次(调用方超时重试、响应丢了),回执和本地文件对得上就直接返回,不再去云端拿
        一份已经删掉的文件、硬报 404 —— agent 会据此判定产物丢了然后重跑。

        Volume 文件要能校验完整性才 ack:云端给的 size_bytes(images[] 每项,契约 C12)或响应的
        Content-Length,有哪个就按哪个校验;两个都没有时文件照常落盘,但**不发 ack**,远端副本留给云端按 TTL 回收。"""
        if state.get("status") != "completed":
            raise BridgeError(f"job 未完成: {state.get('status')}")
        out = Path(out_dir)
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BridgeError(f"建不了输出目录 {out}: {e}") from e
        job_id = state.get("id") or ""
        results, seen = [], set()

        def _name(fn: str) -> str:
            fn = Path(fn or "output.bin").name
            if fn not in seen:
                seen.add(fn)
                return fn
            # ⚠ 撞名必须循环到真正空出来为止,不能只改一次名。曾经是「撞名就取 {stem}_{len(seen)}」,
            #   而那个候选名可能**本来就在输入里**:a.png / a_2.png / a.png 三份不同内容,第三份
            #   算出的 a_2.png 正好是第二份已占的名字 —— 第二份被静默覆盖,而三条都报成功、
            #   返回列表长度还是 3。只有去数磁盘上的文件才看得出少了一份。
            #   (2026-09-09 seedance 侧复现;H3 一个任务出视频+音频+预览,ComfyUI 的产物名又是
            #   每实例独立递增,跨 job 撞名是常态,不是边角。)
            # ⚠ rpartition 找不到 "." 时返回 ("", "", 整串) —— ext 反而是整个文件名。只判 ext
            #   非空会把 "noext" 变成 "_1.noext"。要判分隔符,不能判 stem/ext 本身。
            stem, sep, ext = fn.rpartition(".")
            if not sep:
                stem, ext = fn, ""
            n = len(seen)
            while True:
                cand = f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}"
                if cand not in seen:
                    seen.add(cand)
                    return cand
                n += 1

        images = state.get("images")
        items = images if isinstance(images, list) and images else (
            [{"filename": state.get("filename"), "data_base64": state.get("data_base64")}]
            if state.get("data_base64") else [])
        items = [i for i in items if isinstance(i, dict) and (i.get("volume_path") or i.get("data_base64"))]
        # 回执只在有 Volume 产物时写:base64 产物随时能从状态里重新解出来,用不着;
        # 也免得往纯 base64 的输出目录里多塞一个文件(下游会数目录里的文件)。
        rpath = _receipt_path(out, job_id) if any(i.get("volume_path") for i in items) else None
        prev = _load_receipt(rpath, job_id)
        if prev and prev.get("complete"):
            done = _receipt_outputs(out, prev)
            if done is not None and len(done) == len(items):
                if delete_remote:   # 上次可能写完回执、还没 ack 就中断了;ack 是幂等的
                    for f in prev["files"]:
                        if f.get("volume_path") and f.get("verified"):
                            self._ack_remote(job_id, f["volume_path"])
                return done
        # 本任务上次(中途失败那次)已经写下的文件:原样覆盖,不算「别人的产物」,不改名
        ours = {f["filename"] for f in prev["files"]} if prev else set()
        acks, written = [], []
        receipt_ok = rpath is not None
        for img in items:
            vp = img.get("volume_path")
            blob = None
            if not vp:
                try:
                    blob = base64.b64decode(img["data_base64"])
                except Exception as e:
                    raise BridgeError(f"产物 {img.get('filename')!r} 的 base64 解不开: {e}") from e
            fn = _name(_safe_basename(img.get("filename")))
            # 目标已存在、又不是本任务写的(内容一样的 base64 产物也算本任务的)→ 换下一个候选名
            while os.path.lexists(out / fn) and fn not in ours and not (
                    blob is not None and _same_content(out / fn, blob)):
                fn = _name(fn)
            local = out / fn
            if vp:
                size, verified = self._fetch_volume(job_id, vp, local, _size_hint(img))
                if verified:
                    acks.append(vp)
            else:
                # 与大文件那条路一致:写 .part、成功后原子 rename。直接写正式名的话,
                # 进程中断会在输出目录里留下一个**看起来完整**的截断文件。
                part = local.with_name(local.name + ".part")
                try:
                    part.write_bytes(blob)
                    os.replace(part, local)
                except Exception as e:
                    try:
                        part.unlink()
                    except Exception:
                        pass
                    raise BridgeError(f"写入 {local} 失败: {type(e).__name__}: {e}") from e
                size, verified = len(blob), True
            results.append({"filename": fn, "path": str(local), "size_bytes": size})
            written.append({"filename": fn, "size_bytes": size,
                            **({"volume_path": vp, "verified": verified} if vp else {})})
            if receipt_ok:   # 逐个记:中途失败后重试时,已经写下的文件认得出是自己的
                receipt_ok = _write_receipt(rpath, job_id, written, complete=False)
        if not results:
            raise BridgeError("状态里没有可落盘的产物(images 为空)")
        if rpath is not None:
            receipt_ok = receipt_ok and _write_receipt(rpath, job_id, written, complete=True)
        # ⚠ 等**这个任务的全部产物**都落盘之后才发 ack。逐个文件 ack 的话,后面某个文件断线失败,
        #   重试时前面那些已被删掉的就 404,整个任务再也取不全(2026-09-24 review)。
        #   routes._write_results 那条路径早就是「全部落盘后统一清理」,这里对齐。
        # ⚠ 回执写不下来就不 ack:没有回执,ack 之后再取一次就是硬 404。远端副本留给云端按 TTL 回收。
        if delete_remote and receipt_ok:
            for vp in acks:
                self._ack_remote(job_id, vp)
        return results

    def _download_volume(self, job_id: str, vol_path: str, local: Path,
                         expected_size: int | None = None) -> int:
        """经云端 /fetch 下载一个 Volume 产物到 local,返回字节数。失败一律抛 BridgeError。"""
        return self._fetch_volume(job_id, vol_path, local, expected_size)[0]

    def _fetch_volume(self, job_id: str, vol_path: str, local: Path,
                      expected_size: int | None = None) -> tuple[int, bool]:
        """→ (字节数, 是否校验过完整性)。expected_size 是云端 images[] 给的 size_bytes(没有就 None)。"""
        # ⚠ 下载时**不**让云端删。以前带 delete=1,云端在响应交给 ingress 之后就删了,
        #   不等这边收完:断线或大小对不上时,远端副本已没、下面 finally 又清掉 .part,
        #   付过钱的产物两头落空。现在先完整落盘并校验,**成功之后**才发 ack 让云端删。
        qs = urllib.parse.urlencode({"job_id": job_id, "path": vol_path})
        url = f"{self._url('fetch')}?{qs}"
        dl_req = urllib.request.Request(url, headers={"X-Bridge-Key": self.key})
        # 先写 .part、校验后原子 rename:中断若直接写终名会留下"看起来完整"的残缺文件。
        part = local.with_name(local.name + ".part")
        try:
            with _open_http(dl_req, timeout=600) as r, open(part, "wb") as f:
                clen = _content_length(r.headers)
                size = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
            # ⚠ 没有 Content-Length 的响应(close 分隔 / 中间层去掉了这个头)断在半路时,读到的就是 EOF。
            #   以前只比 Content-Length,于是截断的文件被当成完整的,随即 ack 删掉远端副本
            #   (2026-10-05 深度 review)。云端 size_bytes 是独立于这次传输的期望值,先比它。
            for want, src in ((expected_size, "云端 size_bytes"), (clen, "Content-Length")):
                if want is not None and size != want:
                    raise BridgeError(f"/fetch 下载不完整: {size}/{want} bytes(按{src},{vol_path})")
            part.replace(local)
            return size, (expected_size is not None or clen is not None)
        except BridgeError:
            raise
        except urllib.error.HTTPError as e:
            raise BridgeError(_fetch_http_error(e, vol_path)) from None
        except Exception as e:
            # URLError / IncompleteRead / 拒绝跨源重定向的 ValueError / 落盘的 OSError:统一包成 BridgeError。
            # 以前直接漏出去,只接 BridgeError 的调用方(bridge_cli)打一串 traceback(2026-10-05 深度 review)。
            raise BridgeError(f"/fetch 下载失败({vol_path}): {type(e).__name__}: {e}") from e
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass

    def _ack_remote(self, job_id: str, vol_path: str) -> None:
        """本地已完整落盘并校验 → 通知云端删 Volume 副本。失败不影响结果:
        云端 _sweep_job_state 会按 TTL 回收,最坏只是多占一会儿存储。"""
        qs = urllib.parse.urlencode({"job_id": job_id, "path": vol_path, "ack": 1})
        try:
            self._req(f"{self._url('fetch')}?{qs}", None, 30)
        except Exception:
            pass

    # ── 输入素材打包(引用 input/ 的节点 → data uri,协议与官方插件一致)──
    # 引用 input/ 下本地文件的节点 → 各自的输入键。**键名不统一,不能一律取 "image"**:
    # LoadVideo 是 "file"、LoadAudio 是 "audio"(ComfyUI v0.34.6 源码核实)。
    # 漏一个键 = 那个文件根本不进 payload,云端 ComfyUI 找不到它,报错长得像工作流参数错
    # 而不是"少传了素材"。2026-09-19 之前这里只有三个 LoadImage*,所以送不了视频/音频参考。
    # ⚠ 这张表在 routes.py 和 bridge_client.py 各有一份(本模块是零依赖、可被下游整份 vendor
    #   的独立客户端,不能 import routes),由 test_input_file_nodes_identical_routes_and_client 钉死。
    _INPUT_FILE_NODES = {
        "LoadImage": ("image",),
        "LoadImageMask": ("image",),
        "LoadImageOutput": ("image",),
        "LoadVideo": ("file",),
        "LoadAudio": ("audio",),
    }

    @staticmethod
    def pack_input_images(workflow: dict, search_dirs: list[str]) -> list[dict]:
        """扫 workflow 里引用 input/ 本地文件的节点(图 / 视频 / 音频),在 search_dirs 里找到
        并编成 [{name, image: data uri}]。找不到的抛错(与云端报错等价但更早)。

        ⚠ 键仍叫 "image" 是既定协议 —— 云端 upload_images 只认这一个键,视频音频也走它
        (ComfyUI 的 /upload/image 不校验类型,按文件名原样落进 input/)。"""
        names, out = [], []
        for node in (workflow or {}).values():
            if not isinstance(node, dict):
                continue
            keys = BridgeClient._INPUT_FILE_NODES.get(node.get("class_type"))
            if not keys:
                continue
            ins = node.get("inputs") or {}
            # "filename" 是给自定义节点的兜底;连线形态是 ["3", 0] 这样的 list,必须判 str 跳过。
            n = next((ins[k] for k in (*keys, "filename")
                      if isinstance(ins.get(k), str) and ins[k]), None)
            if n and n not in names:
                names.append(n)
        for n in names:
            # 工作流内容不可信:绝对路径 / ".." 会让 Path(d) / n 落到 search_dirs 之外,
            # 变成任意本地文件读取并上传。子目录相对路径(如 "sub/a.png")合法。
            pn = Path(n)
            if pn.is_absolute() or ".." in pn.parts:
                raise BridgeError(f"输入图路径非法(绝对路径或含 ..): {n}")
            # ⚠ 只挡字符串形态不够:搜索目录里放一个指向目录外的**符号链接**,
            # is_file() 照样为真、read_bytes() 就把目录外的内容读出来上传了
            # (2026-08-31 codex 实测成功)。必须 resolve 后确认仍在该搜索目录内。
            # 本模块是零依赖的独立客户端,所以这里自带一份,不 import 插件的其它模块。
            def _within(cand: Path, root: Path) -> bool:
                try:
                    cand.resolve().relative_to(Path(root).resolve())
                    return True
                except Exception:
                    return False

            p = next((Path(d) / n for d in search_dirs
                      if (Path(d) / n).is_file() and _within(Path(d) / n, Path(d))), None)
            if p is None:
                raise BridgeError(f"输入图找不到或越界: {n}(搜索目录: {search_dirs})")
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            out.append({"name": n, "image": f"data:{mime};base64,{b64}"})
        return out


# ── 产物落盘的小工具(模块级:download_outputs 与 received_outputs 共用)──
_RECEIPT_PREFIX = ".bridge_receipt_"


def _safe_basename(fn) -> str:
    """产物文件名只取 basename;"" / "." / ".." 换成 output.bin。以前 "." 让 .part 落到 out_dir 外面、
    ".." 让 rename 撞上目录抛 OSError(2026-10-05 深度 review)。与回执同前缀的名字前面加 "_",免得盖掉回执。"""
    name = Path(fn).name if isinstance(fn, str) and fn else ""
    if name in ("", ".", ".."):
        return "output.bin"
    return "_" + name if name.startswith(_RECEIPT_PREFIX) else name


def _size_hint(img: dict):
    """云端 images[] 给的原始字节数(契约 C12);缺失 / 不是非负整数 → None。"""
    v = img.get("size_bytes")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _content_length(headers):
    try:
        v = headers.get("Content-Length")
        n = int(v) if v is not None and str(v).strip() else None
    except (TypeError, ValueError):
        return None
    return n if n is not None and n >= 0 else None


def _same_content(path: Path, blob: bytes) -> bool:
    try:
        return (path.is_file() and not path.is_symlink()
                and path.stat().st_size == len(blob) and path.read_bytes() == blob)
    except OSError:
        return False


def _fetch_http_error(e: urllib.error.HTTPError, vol_path: str) -> str:
    """/fetch 的 HTTP 错误 → 按实际状态给提示。以前 404 之外一律问「云端是 0.7.3+ 吗?」,
    401(key 不对)和 403(路径越界)都被指错了方向(2026-10-05 深度 review)。"""
    body, text = _err_body(e)
    if e.code == 401:
        return ("/fetch 401:bridge key 不对/缺失(云端早于 0.8.3 时不认 X-Bridge-Key 头,也是 401,"
                "在 Modal 面板重新部署一次)")
    if e.code == 403:
        return f"/fetch 403:云端拒绝了这个路径({vol_path} 不在该任务的产物目录内):{text}"
    if e.code == 404:
        if isinstance(body, dict) and str(body.get("error") or "").startswith("not found"):
            return f"/fetch 404:{vol_path} 不在 Volume 上(已被取过并删除?)"
        return (f"/fetch 404:{text or '(无正文)'} —— 不是云端「文件不存在」的答复;"
                "云端早于 0.7.3 时没有 /fetch 端点,重新部署")
    return f"/fetch HTTP {e.code}:{text or '(无正文)'}"


def _receipt_path(out: Path, job_id):
    return out / f"{_RECEIPT_PREFIX}{job_id}.json" if _safe_job_id(job_id) else None


def _load_receipt(path, job_id):
    """读回执;不存在 / 坏了 / 不是这个任务的 → None。"""
    if path is None:
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = rec.get("files") if isinstance(rec, dict) else None
    if not isinstance(files, list) or rec.get("job_id") != job_id:
        return None
    for f in files:
        if not (isinstance(f, dict) and isinstance(f.get("filename"), str)
                and _safe_basename(f["filename"]) == f["filename"]
                and isinstance(f.get("size_bytes"), int)):
            return None
    return rec


def _receipt_outputs(out: Path, rec: dict):
    """回执里的每个文件都在、大小都对 → download_outputs 形状的结果;否则 None。"""
    res = []
    for f in rec["files"]:
        p = out / f["filename"]
        try:
            if not p.is_file() or p.stat().st_size != f["size_bytes"]:
                return None
        except OSError:
            return None
        res.append({"filename": f["filename"], "path": str(p), "size_bytes": f["size_bytes"]})
    return res or None


def _write_receipt(path: Path, job_id: str, files: list, complete: bool) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"job_id": job_id, "complete": complete, "files": files},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
