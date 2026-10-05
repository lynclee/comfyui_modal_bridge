"""fix1005 第二轮(2026-10-05 深度 review 复核):本机后端、构建链与客户端。

条目编号对应第二轮修复清单:
  1 secret-upsert 在隔离模式的 Python(Windows 便携版 ._pth = -P / -I)下也能 import
  2 _compute_local_node_reqs 读 manifest 走 strict
  3 契约 D2:调用方自带 job_id;MCP 本地模式自己定 id、超时放宽、超时 / 断线交还 job_id
  4 结果不确定之后的确定拒收也算「结果未知」
  5 连接阶段的失败(一个字节都没发出去)全部如此 → 确定没提交
  6 /run 的确定性 4xx 消息带 `/run:` 前缀(comfyagent 按前缀认)
  7 受限视图的 healthy 带时刻与 endpoint,过期 / 换 endpoint 回 null;匿名 /version 不碰云端
  8 AIGC 只清这台机器知道有过的;地址只收 https://
  9 /sync_nodes 读不到云端且本机清单为空 → 409
  10 补不出来源的说明要准(带凭据的私有仓库 vs 本机清单没有);要删的节点不挡同步
  11 Registry 节点按 .tracking 判 dirty
  12 bridge_cli:cli.json 损坏即中止;endpoint 未知时问 Modal API

约束同其它测试文件:不联网、不部署、不碰真实 Modal(SDK 一律用桩);会写文件的都落 tmp_path。
"""
import asyncio
import errno
import http.client
import inspect
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 两套模块对象:routes 测试走包内(comfyui_modal_bridge.*),CLI / 客户端测试走顶层模块 —— 打桩要打对对象
import bridge_client as bc  # noqa: E402
import modal_client as mc  # noqa: E402
import node_sync as ns_top  # noqa: E402
import test_fix_build as _tfb  # noqa: E402
import test_fix_client as _tfc  # noqa: E402
import test_fix_routes as _tfr  # noqa: E402
import test_routes as harness  # noqa: E402
from test_fix_client import WF, _calls, _client, _load_mcp  # noqa: E402
from test_fix_routes import DEPLOY_BODY, _last_marked, _rc  # noqa: E402

# 复用其它文件的 fixture(赋值而不是 from-import:测试函数的同名参数不算重定义)
baked, cli_env, deploy_env = _tfb.baked, _tfb.cli_env, _tfb.deploy_env
cloud = _tfc.cloud
_isolate = _tfr._isolate      # autouse:routes 的外部副作用全部换成假的

from comfyui_modal_bridge import contract  # noqa: E402
from comfyui_modal_bridge import config as cfg_mod  # noqa: E402
from comfyui_modal_bridge import health_client as hc_pkg  # noqa: E402
from comfyui_modal_bridge import modal_client as mc_pkg  # noqa: E402
from comfyui_modal_bridge import node_sync as ns_pkg  # noqa: E402
from comfyui_modal_bridge import routes as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_health_cache():
    rt._LAST_HEALTH.update(healthy=None, checked_at=None, endpoint=None)
    yield
    rt._LAST_HEALTH.update(healthy=None, checked_at=None, endpoint=None)


def _refused_url(label="run") -> str:
    """一个本机上确定没人监听的端口(先占再放)—— 连上去必然 ECONNREFUSED,不出本机。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/{label}"


# ============================================================================
# 1 secret-upsert:隔离模式的 Python 下也能跑
# ============================================================================
_FAKE_MODAL = """
import json
class _NF(Exception):
    pass
class exception:
    NotFoundError = _NF
class _Ref:
    def __init__(self, name):
        self.name = name
    def update(self, d):
        open({log!r}, "w").write(json.dumps(["update", self.name, d]))
class Secret:
    update = _Ref.update
    @staticmethod
    def from_name(name):
        return _Ref(name)
"""


@pytest.mark.parametrize("flag", ["-P", "-I"])
def test_secret_upsert_imports_under_isolated_python(tmp_path, monkeypatch, flag):
    """Windows 便携版的 python_embeded/._pth 让解释器进隔离模式:脚本目录不进 sys.path。
    以前的 `python node_sync.py secret-upsert` 在那里 import health_client 就炸,GUI 部署全卡在写 Secret。
    -I 连 PYTHONPATH 都不认,所以假 modal 放在插件目录里(引导代码把插件目录插到 sys.path 最前)。"""
    plug = tmp_path / "plugin"
    (plug / "modal").mkdir(parents=True)
    for f in ("node_sync.py", "health_client.py", "bridge_client.py"):
        shutil.copy(ROOT / f, plug / f)
    log = tmp_path / "calls.json"
    (plug / "modal" / "__init__.py").write_text(_FAKE_MODAL.format(log=str(log)), encoding="utf-8")
    monkeypatch.setattr(ns_top, "_HERE", plug)
    cmd = ns_top.secret_upsert_cmd({"modal_app_name": "a"}, "hf_SECRETVALUE123", bridge_key="bk-zz")
    r = subprocess.run([cmd[0], flag, *cmd[1:]], capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(log.read_text()) == ["update", "a-secrets", {
        "BRIDGE_API_KEY": "bk-zz", "HF_TOKEN": "hf_SECRETVALUE123", "HUGGING_FACE_HUB_TOKEN": "hf_SECRETVALUE123"}]
    assert "hf_SECRETVALUE123" not in r.stdout + r.stderr
    # 对照:旧的脚本形态在同样的模式下确实 import 不到(测试测到了点子上)
    old = [cmd[0], flag, str(plug / "node_sync.py"), "secret-upsert", "a-secrets", "BRIDGE_API_KEY=x"]
    r = subprocess.run(old, capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert r.returncode != 0 and "health_client" in r.stderr, r.stderr


def test_redact_cmd_still_covers_the_bootstrap_argv():
    cmd = ns_top.secret_upsert_cmd({"modal_app_name": "a"}, "hf_SECRETVALUE123", "civ_TOKEN_456789",
                                   "bk-0123456789abcdef", "comfy-KEY-000111222", "https://site.app",
                                   "byp-SECRET-99887766")
    shown = ns_top.redact_cmd(cmd)
    for secret in ("hf_SECRETVALUE123", "civ_TOKEN_456789", "bk-0123456789abcdef",
                   "comfy-KEY-000111222", "byp-SECRET-99887766"):
        assert secret not in shown, secret
    assert ns_top._SECRET_UPSERT_BOOT in shown, "引导代码不该被当成 KEY=VALUE 打码"
    assert "AIGC_STUDIO_BASE_URL=https://site.app" in shown, "非凭据照旧明文,排查要看得见"
    assert cmd[cmd.index("secret-upsert") + 1] == "a-secrets"


# ============================================================================
# 2 _compute_local_node_reqs:读 manifest 的瞬时失败不能当成「没有 manifest」
# ============================================================================
def _flaky_volume(monkeypatch, fail_for="b"):
    files = {"_local_nodes/a.requirements.json": b'["numpy==1"]',
             "_local_nodes/b.requirements.json": b'["opencv-python==4"]'}

    def read(path, buf):
        if fail_for and path.endswith(f"{fail_for}.requirements.json"):
            raise ConnectionError("grpc: connection reset")       # 瞬时故障,不是「不存在」
        if path not in files:
            raise FileNotFoundError(path)
        buf.write(files[path])
    vol = types.SimpleNamespace(reload=lambda: None, read_file_into_fileobj=read)
    monkeypatch.setattr(rt.local_nodes, "list_volume_local_nodes", lambda cfg, max_age=60: ["a", "b"])
    monkeypatch.setattr(rt.modal_volume, "get_volume", lambda cfg: vol)


def test_transient_manifest_failure_raises_instead_of_dropping_deps(monkeypatch):
    _flaky_volume(monkeypatch)
    with pytest.raises(rt.local_nodes.VolumeUnavailable):
        rt._compute_local_node_reqs({})
    _flaky_volume(monkeypatch, fail_for="")
    assert rt._compute_local_node_reqs({}) == ["numpy==1", "opencv-python==4"]


def test_auto_redeploy_aborts_on_transient_manifest_failure(monkeypatch):
    """复现 reviewer 探针:新机器(扁平依赖文件为空)+ 读 b 的 manifest 抖一下 → 以前照样部署,b 的依赖没了。"""
    _flaky_volume(monkeypatch)
    builds = []

    async def deploy(resp, cmd, **kw):
        builds.append(cmd)
        return 0
    monkeypatch.setattr(rt, "_run_streamed", deploy)

    class R:
        def __init__(self):
            self.out = []

        async def write(self, b):
            self.out.append(b.decode())

    resp = R()
    rc = asyncio.run(rt._auto_redeploy_for_local_reqs(resp))
    assert rc == 1 and builds == [], "瞬时失败时不能部署"
    assert "已中止" in "".join(resp.out)


# ============================================================================
# 3 契约 D2:调用方自带 job_id
# ============================================================================
def test_submit_route_passes_caller_job_id_and_validates_it(monkeypatch):
    sent = []

    async def submit(session, cfg, **kw):
        sent.append(kw.get("job_id"))
        return {"id": kw.get("job_id") or "gen-1", "gpu": "H100"}
    monkeypatch.setattr(mc_pkg, "submit_job", submit)

    async def go(c, body):
        harness._set_cfg(gpu_tier="primary")
        r = await c.post("/modal_bridge/submit", json={"prompt": {}, "local_nodes": {}, **body})
        return r.status, await r.json()

    st, body = harness._run(lambda c: go(c, {"job_id": "mine-1"}))
    assert st == 200 and body["job_id"] == "mine-1" and sent == ["mine-1"], (st, body, sent)
    for bad in ("../x", "-x", "a" * 65, 5, "a..b"):
        st, body = harness._run(lambda c, bad=bad: go(c, {"job_id": bad}))
        assert st == 400 and "job_id" in body["error"], (bad, body)
    assert sent == ["mine-1"], "不合法的 job_id 不能交给云端"
    harness._run(lambda c: go(c, {}))
    assert sent[-1] is None, "不带就照旧由 submit_job 生成"


class _Resp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    async def read(self):
        return self._body


class _Ctx:
    def __init__(self, act):
        self.act = act

    async def __aenter__(self):
        if isinstance(self.act, BaseException):
            raise self.act
        return self.act

    async def __aexit__(self, *a):
        return False


class _Session:
    """modal_client.submit_job 用的假 aiohttp session:按顺序吐出预设的结局。"""

    def __init__(self, *acts):
        self.acts = list(acts)
        self.payloads = []

    def post(self, url, json=None, **kw):
        self.payloads.append(json)
        return _Ctx(self.acts.pop(0))


def _connector_error():
    key = types.SimpleNamespace(host="ws--x-run.modal.run", port=443, ssl=True)
    return aiohttp.ClientConnectorError(key, OSError(errno.ECONNREFUSED, "Connection refused"))


def _proxy_error():
    import yarl
    u = yarl.URL("https://ws--x-run.modal.run")
    return aiohttp.ClientHttpProxyError(request_info=aiohttp.RequestInfo(u, "POST", {}, u), history=(),
                                        status=407, message="Proxy Authentication Required")


def _mc_run(monkeypatch, session, **kw):
    async def nosleep(*a, **k):
        pass
    monkeypatch.setattr(mc.asyncio, "sleep", nosleep)

    async def go():
        try:
            return await mc.submit_job(session, {"modal_endpoint_base": "https://ws--x", "bridge_api_key": "k"},
                                       WF, **kw)
        except Exception as e:      # noqa: BLE001 —— 由调用方断言类型
            return e
    return asyncio.run(go())


def test_modal_client_submit_uses_caller_job_id(monkeypatch):
    s = _Session(_Resp(200, {"id": "fixed-1", "status": "queued"}))
    assert _mc_run(monkeypatch, s, job_id="fixed-1")["id"] == "fixed-1"
    assert s.payloads[0]["job_id"] == "fixed-1"
    s = _Session(asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError())
    e = _mc_run(monkeypatch, s, job_id="fixed-2")
    assert isinstance(e, mc.SubmitUnknown) and e.job_id == "fixed-2"
    assert {p["job_id"] for p in s.payloads} == {"fixed-2"}


def test_mcp_submit_timeout_covers_submit_job_worst_case():
    """/submit 的最坏耗时 = (max_retries+1) × 每次 60s + 退避;MCP 等得比它久,才不会在本机还在重试时先放弃。"""
    retries = inspect.signature(mc.submit_job).parameters["max_retries"].default
    assert "ClientTimeout(total=60)" in inspect.getsource(mc.submit_job)
    worst = (retries + 1) * 60 + sum(1.5 * 2 ** i for i in range(retries))
    src = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
    val = int(src.split("_SUBMIT_TIMEOUT_S = ", 1)[1].split()[0])
    assert val >= 330 and val >= worst + 60, (val, worst)


def _mcp_with_submit(monkeypatch, cloud, behaviour, timeout_s=None):
    seen = []

    def submit(h, q, b):
        seen.append(b)
        return behaviour(h, b)
    cloud.behav["modal_bridge/submit"] = submit
    m = _load_mcp(monkeypatch, {"MODAL_BRIDGE_URL": cloud.base})
    if timeout_s is not None:
        monkeypatch.setattr(m, "_SUBMIT_TIMEOUT_S", timeout_s)
    return m, seen


def test_mcp_local_submit_sends_its_own_job_id(cloud, monkeypatch):
    m, seen = _mcp_with_submit(monkeypatch, cloud,
                               lambda h, b: h.send_json(200, {"ok": True, "job_id": b["job_id"], "gpu": "H100"}))
    r = m.submit_workflow(json.dumps(WF))
    assert r["ok"] and seen and r["job_id"] == seen[0]["job_id"]
    assert bc._safe_job_id(seen[0]["job_id"]), "MCP 定的 id 也得过 C1"


def test_mcp_local_submit_timeout_returns_outcome_unknown_with_job_id(cloud, monkeypatch):
    def slow(h, b):
        time.sleep(1.5)
        return h.send_json(200, {"ok": True, "job_id": b["job_id"]})
    m, seen = _mcp_with_submit(monkeypatch, cloud, slow, timeout_s=0.5)
    r = m.submit_workflow(json.dumps(WF))
    assert r["outcome"] == "unknown" and r["ok"] is False, r
    assert r["job_id"] == seen[0]["job_id"], "交还的必须是发出去的那个 id,agent 拿它去 poll"
    assert "别重新提交" in r["error"]


def test_mcp_local_submit_dropped_connection_is_unknown(cloud, monkeypatch):
    m, seen = _mcp_with_submit(monkeypatch, cloud, lambda h, b: h.drop())
    r = m.submit_workflow(json.dumps(WF))
    assert r["outcome"] == "unknown" and r["job_id"] == seen[0]["job_id"], r


def test_mcp_local_submit_passes_server_answers_through(cloud, monkeypatch):
    m, seen = _mcp_with_submit(monkeypatch, cloud, lambda h, b: h.send_json(
        502, {"error": "提交结果未知", "job_id": b["job_id"], "outcome": "unknown"}))
    r = m.submit_workflow(json.dumps(WF))
    assert r == {"error": "提交结果未知", "job_id": seen[0]["job_id"], "outcome": "unknown"}
    m, _ = _mcp_with_submit(monkeypatch, cloud, lambda h, b: h.send_json(502, {"error": "Modal /run 401"}))
    r = m.submit_workflow(json.dumps(WF))
    assert r == {"error": "Modal /run 401"}, "插件判定的确定失败原样给出,不能被改成 unknown"
    m, seen = _mcp_with_submit(monkeypatch, cloud, lambda h, b: h.send_json(500, b"Internal Server Error",
                                                                           "text/plain"))
    r = m.submit_workflow(json.dumps(WF))
    assert r["outcome"] == "unknown" and r["job_id"] == seen[0]["job_id"], "不是插件答复的 5xx = 结果未知"


def test_mcp_local_submit_refused_is_a_definite_error(monkeypatch):
    m = _load_mcp(monkeypatch, {"MODAL_BRIDGE_URL": _refused_url("").rstrip("/")})
    r = m.submit_workflow(json.dumps(WF))
    assert "outcome" not in r and "job_id" not in r and "没有提交" in r["error"], r


# ============================================================================
# 4 / 5 / 6 提交结局的分类
# ============================================================================
@pytest.mark.parametrize("first", ["timeout", "504", "non_json"])
@pytest.mark.parametrize("then", [(401, {"error": "unauthorized"}), (404, b"app not found"),
                                  (200, {"error": "invalid workflow"})])
def test_modal_client_reject_after_uncertain_is_unknown(monkeypatch, first, then):
    a = {"timeout": asyncio.TimeoutError(), "504": _Resp(504, b"gateway"), "non_json": _Resp(200, b"<html>")}[first]
    s = _Session(a, _Resp(*then))
    e = _mc_run(monkeypatch, s)
    assert isinstance(e, mc.SubmitUnknown), e
    assert e.job_id == s.payloads[0]["job_id"] == s.payloads[1]["job_id"]
    assert len(s.payloads) == 2, "确定拒收之后不再重试"
    marker = {401: "401", 404: "app not found", 200: "invalid workflow"}[then[0]]
    assert marker in str(e), "要附上拒收原文"


def test_modal_client_reject_on_first_attempt_is_still_definite(monkeypatch):
    e = _mc_run(monkeypatch, _Session(_Resp(404, b"app not found")))
    assert isinstance(e, RuntimeError) and not isinstance(e, mc.SubmitUnknown), e
    # 连接阶段失败之后的拒收:前一次没发出去,拒收照样是确定的
    e = _mc_run(monkeypatch, _Session(_connector_error(), _Resp(401, {"error": "x"})))
    assert isinstance(e, RuntimeError) and not isinstance(e, mc.SubmitUnknown), e


def test_modal_client_connect_phase_failures_are_definite(monkeypatch):
    s = _Session(_connector_error(), _proxy_error(), _connector_error(), _connector_error())
    e = _mc_run(monkeypatch, s)
    assert isinstance(e, RuntimeError) and not isinstance(e, mc.SubmitUnknown), e
    assert len(s.payloads) == 4, "连接阶段失败可以放心重试"
    assert "没有发出去" in str(e) and "没有提交" in str(e)
    # 只要有一次可能到了云端,就是结果未知
    e = _mc_run(monkeypatch, _Session(_connector_error(), asyncio.TimeoutError(), _connector_error(),
                                      _connector_error()))
    assert isinstance(e, mc.SubmitUnknown), e


def test_modal_client_real_refused_connection_is_definite(monkeypatch):
    monkeypatch.setattr(mc, "_endpoint", lambda base, label: _refused_url(label))

    async def nosleep(*a, **k):
        pass
    monkeypatch.setattr(mc.asyncio, "sleep", nosleep)

    async def go():
        async with aiohttp.ClientSession() as s:
            try:
                await mc.submit_job(s, {"modal_endpoint_base": "x", "bridge_api_key": "k"}, WF)
            except Exception as e:      # noqa: BLE001
                return e
    e = asyncio.run(go())
    assert isinstance(e, RuntimeError) and not isinstance(e, mc.SubmitUnknown), e


@pytest.mark.parametrize("reason, sent", [
    (socket.gaierror(-2, "Name or service not known"), False),
    (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), False),
    (ssl.SSLCertVerificationError(1, "certificate verify failed"), False),
    (OSError("Tunnel connection failed: 407 Proxy Authentication Required"), False),
    (OSError(errno.EHOSTUNREACH, "No route to host"), False),
    (TimeoutError("timed out"), True),           # 分不清连接超时还是发到一半超时 → 不确定
    ("some string reason", True),
])
def test_never_sent_classification(reason, sent):
    assert bc.never_sent(urllib.error.URLError(reason)) is (not sent)


def test_never_sent_rejects_read_phase_and_http_errors():
    assert not bc.never_sent(TimeoutError("timed out"))
    assert not bc.never_sent(http.client.RemoteDisconnected("closed"))
    assert not bc.never_sent(ConnectionRefusedError())        # 没被 URLError 包着:不是 h.request 里抛的
    assert not bc.never_sent(urllib.error.HTTPError("http://x", 502, "bad", {}, None))
    assert not bc.never_sent(None)


@pytest.mark.parametrize("first", ["drop", "504", "no_id"])
@pytest.mark.parametrize("then", [(401, {"error": "unauthorized"}), (404, b"app not found"),
                                  (422, {"detail": "bad"}), (200, {"error": "invalid job_id"})])
def test_bridge_client_reject_after_uncertain_is_unknown(cloud, first, then):
    n = []

    def run(h, q, b):
        n.append(b["job_id"])
        if len(n) == 1:
            if first == "drop":
                return h.drop()
            if first == "no_id":
                return h.send_json(200, {"status": "queued"})
            return h.send_json(504, b"gateway", "text/plain")
        code, body = then
        return h.send_json(code, body, "application/json" if isinstance(body, dict) else "text/plain")
    cloud.behav["run"] = run
    with pytest.raises(bc.SubmitUnknown) as ei:
        _client(cloud).submit(WF)
    assert ei.value.job_id == n[0] == n[1] and len(n) == 2
    assert "拒收" in str(ei.value)


def test_bridge_client_connect_phase_only_is_definite(cloud):
    c = _client(cloud)
    c._url = lambda label: _refused_url(label)
    with pytest.raises(bc.BridgeError) as ei:
        c.submit(WF)
    assert not isinstance(ei.value, bc.SubmitUnknown)
    assert str(ei.value).startswith("/run:") and "没有发出去" in str(ei.value)


def test_bridge_client_refused_then_uncertain_is_unknown(cloud):
    cloud.behav["run"] = lambda h, q, b: h.send_json(504, b"gateway", "text/plain")
    c = _client(cloud)
    urls = iter([_refused_url(), f"{cloud.base}/run", _refused_url(), _refused_url()])
    c._url = lambda label: next(urls)
    with pytest.raises(bc.SubmitUnknown):
        c.submit(WF)


class _ConnectRefusingProxy(BaseHTTPRequestHandler):
    """只会对 CONNECT 回 407 的代理:请求停在隧道建立那一步,一个字节都到不了目标。"""

    def log_message(self, *_a):
        pass

    def do_CONNECT(self):
        self.server.seen.append(self.path)
        self.send_response(407, "Proxy Authentication Required")
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_bridge_client_proxy_connect_failure_is_definite(monkeypatch):
    """真走一遍 urllib 的代理隧道:CONNECT 被拒 → http.client 抛 OSError("Tunnel connection failed …")。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ConnectRefusingProxy)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        for k in ("https_proxy", "HTTPS_PROXY"):
            monkeypatch.setenv(k, f"http://127.0.0.1:{srv.server_port}")
        for k in ("no_proxy", "NO_PROXY"):
            monkeypatch.setenv(k, "127.0.0.1,localhost")
        c = bc.BridgeClient("https://ws--comfyui-bridge", "k", timeout=5)
        c._SUBMIT_RETRY_DELAYS = (0, 0, 0)
        with pytest.raises(bc.BridgeError) as ei:
            c.submit(WF)
        assert not isinstance(ei.value, bc.SubmitUnknown), ei.value
        assert str(ei.value).startswith("/run:")
        assert srv.seen == ["ws--comfyui-bridge-run.modal.run:443"] * 4
    finally:
        srv.shutdown()
        srv.server_close()


def _comfyagent_definitely_rejected(err) -> bool:
    """comfyagent 的 modal_executor._definitely_rejected(rv_routes/ca 里的原文):按前缀认。"""
    return str(err).startswith(("401", "/run:", "/run 响应缺 id"))


@pytest.mark.parametrize("code", [400, 401, 404, 413, 422])
def test_bridge_client_definite_4xx_keeps_comfyagent_prefix(cloud, code):
    cloud.behav["run"] = lambda h, q, b: h.send_json(code, {"detail": [{"msg": "field required"}]})
    with pytest.raises(bc.BridgeHTTPError) as ei:
        _client(cloud).submit(WF)
    e = ei.value
    assert e.status == code and not isinstance(e, bc.SubmitUnknown)
    assert _comfyagent_definitely_rejected(e), str(e)
    if code != 401:
        assert str(e).startswith(f"/run: HTTP {code}"), str(e)
    assert len(_calls(cloud, "run")) == 1


def test_comfyagent_prefix_never_matches_unknown(cloud):
    cloud.behav["run"] = lambda h, q, b: h.send_json(500, b"boom", "text/plain")
    with pytest.raises(bc.SubmitUnknown) as ei:
        _client(cloud).submit(WF)
    assert not _comfyagent_definitely_rejected(ei.value)


def test_scrub_credentials_matches_comfyagent_vendor():
    """comfyagent 的 vendor 副本加过同名同规则的函数,它的测试直接 import —— 收回上游后两边 vendor 不再打架。"""
    assert bc._CREDS_IN_URL.pattern == r"([a-zA-Z][\w+.-]*://)[^/@\s]*:[^/@\s]*@"
    assert bc._scrub_credentials("via http://u:p@proxy:1/ x") == "via http://***:***@proxy:1/ x"
    assert bc._scrub_credentials("plain https://h/x") == "plain https://h/x"


def test_request_failed_message_scrubs_proxy_credentials(monkeypatch):
    """本机代理 env 常是 http://USER:PASS@host:port;urllib 的报错里带出来时不能原样进 agent 上下文 / 日志。"""
    def boom(req, timeout):
        raise urllib.error.URLError(OSError("proxy http://USER:PASS@10.0.0.1:3128 refused"))
    monkeypatch.setattr(bc, "_open_http", boom)
    c = bc.BridgeClient("https://ws--comfyui-bridge", "k")
    c._SUBMIT_RETRY_DELAYS = (0, 0, 0)
    with pytest.raises(bc.BridgeError) as ei:
        c.health()
    assert "PASS" not in str(ei.value) and "***:***@10.0.0.1" in str(ei.value)
    with pytest.raises(bc.BridgeError) as ei:
        c.submit(WF)
    assert "PASS" not in str(ei.value)


# ============================================================================
# 7 受限视图:healthy 带时刻与 endpoint;匿名 /version 不碰云端
# ============================================================================
_ANON = {"Host": "bridge.example", "X-Modal-Bridge-Capability": ""}


def test_limited_views_expire_and_never_touch_the_cloud(monkeypatch):
    hits = []

    async def main():
        fake = web.Application()

        async def health(req):
            hits.append(1)
            return web.json_response({"healthy": True, "deployed_version": "9.9.9"})
        fake.router.add_get("/p-health.modal.run", health)
        fs = TestServer(fake)
        await fs.start_server()
        base = f"http://127.0.0.1:{fs.port}/p"
        harness._set_cfg(modal_endpoint_base=base)
        c = await harness._client()
        out = {}
        try:
            out["local_version"] = await (await c.get("/modal_bridge/version")).json()
            out["hits_after_local"] = len(hits)
            out["anon_version"] = await (await c.get("/modal_bridge/version", headers=_ANON)).json()
            out["anon_health"] = await (await c.get("/modal_bridge/health", headers=_ANON)).json()
            out["hits_after_anon"] = len(hits)
            harness._set_cfg(modal_endpoint_base="https://other--comfyui-bridge")   # 换了 workspace
            out["anon_other"] = await (await c.get("/modal_bridge/health", headers=_ANON)).json()
            out["anon_other_v"] = await (await c.get("/modal_bridge/version", headers=_ANON)).json()
            harness._set_cfg(modal_endpoint_base=base)
            rt._LAST_HEALTH["checked_at"] = time.time() - rt._LAST_HEALTH_MAX_AGE_S - 5
            out["anon_stale"] = await (await c.get("/modal_bridge/health", headers=_ANON)).json()
        finally:
            await c.close()
            await fs.close()
        return out

    out = asyncio.run(main())
    assert out["local_version"]["reachable"] and out["hits_after_local"] == 1, "本机前端行为不变"
    v = out["anon_version"]
    assert v["limited"] and v["local"] == ns_pkg.plugin_version() and v["healthy"] is True, v
    assert isinstance(v["checked_at"], float) and "deployed" not in v and "err_kind" not in v, v
    assert out["anon_health"]["healthy"] is True and out["anon_health"]["checked_at"] == v["checked_at"]
    assert out["hits_after_anon"] == 1, "匿名非本机的 /version、/health 都不能请求云端"
    for k in ("anon_other", "anon_other_v", "anon_stale"):
        assert out[k]["healthy"] is None and out[k]["checked_at"] is None, (k, out[k])


def test_version_failure_and_local_busy_recording(monkeypatch):
    harness._set_cfg(modal_endpoint_base=_refused_url("p").rsplit("/", 1)[0] + "/p")

    async def go(c):
        return await (await c.get("/modal_bridge/version")).json()
    v = harness._run(go)
    assert v["err_kind"] == "unreachable" and rt._LAST_HEALTH["healthy"] is False
    # 本机事件循环被占住导致的超时:云端状态未知,不记
    rt._LAST_HEALTH.update(healthy=None, checked_at=None, endpoint=None)

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(rt.aiohttp, "ClientSession", Boom)
    monkeypatch.setattr(rt, "_local_queue_busy", lambda: True)
    v = asyncio.run(_version_direct())
    assert v["err_kind"] == "local_busy" and rt._LAST_HEALTH["checked_at"] is None


async def _version_direct():
    """不起 TestClient(它自己也要 ClientSession),直接调 handler。"""
    handler = next(r.handler for r in harness._ROUTES if getattr(r, "path", "") == "/modal_bridge/version")
    req = types.SimpleNamespace(remote="127.0.0.1", host="127.0.0.1:8188", headers={}, scheme="http")
    resp = await handler(req)
    return json.loads(resp.body)


def test_deploy_verify_records_health(_isolate):
    class R:
        async def write(self, b):
            pass

    rc = asyncio.run(rt._deploy_verify(R(), {"modal_endpoint_base": "https://ws--app"}, "https://ws--app", ".", {}))
    assert rc == 0 and rt._LAST_HEALTH["healthy"] is True and rt._LAST_HEALTH["endpoint"] == "https://ws--app"


def test_deploy_verify_records_404(monkeypatch, _isolate):
    async def health(session, cfg):
        raise hc_pkg.HealthUnavailable("not_deployed", "404")
    monkeypatch.setattr(mc_pkg, "health", health)

    class R:
        async def write(self, b):
            pass
    rc = asyncio.run(rt._deploy_verify(R(), {"modal_endpoint_base": "https://ws--app"}, "https://ws--app", ".", {}))
    assert rc == 1 and rt._LAST_HEALTH["healthy"] is False


# ============================================================================
# 8 AIGC:只清这台机器知道有过的;地址只收 https://
# ============================================================================
def _capture_runs(monkeypatch):
    cmds = []

    async def fake_run(resp, cmd, cwd, env):
        cmds.append(list(cmd))
        return 0
    monkeypatch.setattr(rt, "_run_streamed", fake_run)
    return cmds


def _deploy(body=None):
    async def go(c):
        r = await c.post("/modal_bridge/deploy", json={**DEPLOY_BODY, **(body or {})})
        return await r.text()
    return harness._run(go)


def _secret(cmds):
    return next(c for c in cmds if "secret-upsert" in c)


def _clears(cmd):
    return sorted(a.split("=", 1)[1] for a in cmd if a.startswith("--clear="))


def test_deploy_without_aigc_never_clears_other_machines_aigc(monkeypatch):
    cmds = _capture_runs(monkeypatch)
    harness._set_cfg(aigc_bypass_secret="")
    text = _deploy()
    assert _rc(text) == 0, text
    assert _clears(_secret(cmds)) == [], "从没配过 AIGC 的机器不能清掉别的机器写进 Secret 的 AIGC"
    assert cfg_mod.load_config()[contract.AIGC_PUSHED_FIELD] == []


def test_clearing_aigc_in_settings_then_deploying_clears_the_secret(monkeypatch):
    cmds = _capture_runs(monkeypatch)
    harness._set_cfg(aigc_studio_base_url="https://www.site.app", aigc_bypass_secret="byp-1")
    assert _rc(_deploy()) == 0
    sec = _secret(cmds)
    assert "AIGC_STUDIO_BASE_URL=https://www.site.app" in sec and _clears(sec) == []
    assert cfg_mod.load_config()[contract.AIGC_PUSHED_FIELD] == ["aigc_base_url", "aigc_bypass_secret"]
    assert contract.AIGC_PUSHED_FIELD not in contract.public_config(cfg_mod.load_config())

    async def clear_url(c):
        return (await c.post("/modal_bridge/config", json={"aigc_studio_base_url": ""})).status
    assert harness._run(clear_url) == 200
    cmds.clear()
    assert _rc(_deploy()) == 0
    assert _clears(_secret(cmds)) == ["AIGC_STUDIO_BASE_URL", "AIGC_STUDIO_BYPASS_SECRET"], \
        "设置页清空地址后部署:config 里地址在部署前就空了,要靠「上次写过」才知道该清"
    cfg = cfg_mod.load_config()
    assert cfg["aigc_bypass_secret"] == "" and cfg[contract.AIGC_PUSHED_FIELD] == []
    cmds.clear()
    assert _rc(_deploy()) == 0
    assert _clears(_secret(cmds)) == [], "清过一次就不再清(下一次部署的空值是「没配」)"


def test_explicit_empty_url_in_deploy_body_clears_known_values(monkeypatch):
    cmds = _capture_runs(monkeypatch)
    harness._set_cfg(aigc_studio_base_url="https://www.site.app", aigc_bypass_secret="byp-1")
    assert _rc(_deploy({"aigc_studio_base_url": ""})) == 0
    assert _clears(_secret(cmds)) == ["AIGC_STUDIO_BASE_URL", "AIGC_STUDIO_BYPASS_SECRET"]


def test_leftover_bypass_without_url_is_cleared_but_url_left_alone(monkeypatch):
    cmds = _capture_runs(monkeypatch)
    harness._set_cfg(aigc_bypass_secret="byp-old")      # 0.8.30 之前的残留:只有密钥、没有地址
    assert _rc(_deploy()) == 0
    assert _clears(_secret(cmds)) == ["AIGC_STUDIO_BYPASS_SECRET"]


@pytest.mark.parametrize("url, ok", [
    ("", True), ("https://www.site.app/", True), ("HTTPS://Site.app", True),
    ("http://site.app", False), ("ftp://site.app", False), ("https://", False), ("site.app", False),
])
def test_config_accepts_only_https_aigc_url(url, ok):
    harness._set_cfg(aigc_studio_base_url="https://keep.app")

    async def go(c):
        r = await c.post("/modal_bridge/config", json={"aigc_studio_base_url": url})
        return r.status, await r.json()
    st, body = harness._run(go)
    if ok:
        assert st == 200, body
    else:
        assert st == 400 and "https://" in body["error"] and "最终地址" in body["error"], body
        assert cfg_mod.load_config()["aigc_studio_base_url"] == "https://keep.app"
        assert "site.app" not in body["error"], "不回显收到的值(地址里可能带 userinfo)"


def test_deploy_refuses_http_aigc_url(monkeypatch):
    cmds = _capture_runs(monkeypatch)
    text = _deploy({"aigc_studio_base_url": "http://site.app"})
    assert _rc(text) == 2 and "https://" in _last_marked(text) and "最终地址" in _last_marked(text), text
    assert cmds == [], "拦在任何写入之前"
    # 老版本设置页存进 config 的 http:// 也拦(body 没带时用的就是它)
    cfg = cfg_mod.load_config()
    cfg["aigc_studio_base_url"] = "http://legacy.app"
    cfg_mod.save_config(cfg)
    text = _deploy()
    assert _rc(text) == 2 and "当前设置里存的地址" in text and cmds == [], text
    # 显式清空 = 停用,照常部署
    assert _rc(_deploy({"aigc_studio_base_url": ""})) == 0


def test_deploy_py_refuses_http_aigc_url(deploy_env):
    ev, cfg_mod_top = deploy_env, deploy_env["cfg_mod"]
    cfg_mod_top.save_config({**cfg_mod_top.DEFAULT_CONFIG, "aigc_studio_base_url": "http://site.app"})
    code = ev["go"]("--comfyui-tag", "v0.37.2")
    assert code not in (0, None) and "https://" in str(code) and ev["cmds"] == []
    assert not cfg_mod_top.load_config().get("bridge_api_key"), "拦在生成 / 落盘 key 之前"


def test_deploy_py_records_pushed_aigc_fields(deploy_env):
    ev, cfg_mod_top = deploy_env, deploy_env["cfg_mod"]
    cfg_mod_top.save_config({**cfg_mod_top.DEFAULT_CONFIG, "aigc_studio_base_url": "https://www.site.app"})
    assert ev["go"]("--comfyui-tag", "v0.37.2") == 0
    assert cfg_mod_top.load_config()[contract.AIGC_PUSHED_FIELD] == ["aigc_base_url"]


def test_bridge_cli_refuses_http_aigc_url(cli_env):
    ev = cli_env
    ev["plugin_cfg"].parent.mkdir(parents=True, exist_ok=True)
    ev["plugin_cfg"].write_text(json.dumps({"aigc_studio_base_url": "http://site.app"}), encoding="utf-8")
    code = ev["go"](comfyui_tag="v0.37.2")
    assert code not in (0, None) and "https://" in str(code)
    assert ev["runs"] == [] and not ev["cli_path"].exists(), "拦在生成 key、写 Secret 之前"


# ============================================================================
# 9 /sync_nodes:读不到云端 + 本机清单为空 → 409
# ============================================================================
def test_sync_nodes_unreadable_cloud_and_empty_local_list_is_refused(monkeypatch):
    def unreachable(cfg, timeout=20):
        raise hc_pkg.HealthUnavailable("unreachable", "timed out")
    monkeypatch.setattr(ns_pkg, "fetch_cloud_nodes", unreachable)
    cmds = _capture_runs(monkeypatch)

    async def go(c):
        r = await c.post("/modal_bridge/sync_nodes", json={
            "new_baked": [{"name": "New", "url": "https://github.com/x/New", "commit": "n1"}]})
        return r.status, r.content_type, await r.text()
    st, ctype, text = harness._run(go)
    body = json.loads(text)
    assert st == 409 and ctype == "application/json" and body["cloud_unchecked"] == "timed out", text
    assert "本机节点清单是空的" in body["error"] and cmds == []
    assert ns_pkg.read_baked_nodes() == []


# ============================================================================
# 10 补不出来源的说明要准;要删的节点不挡同步
# ============================================================================
_REDACTED = [{"name": "pub", "url": "https://github.com/x/pub", "commit": "p1"},
             {"name": "priv", "url": "https://github.com/me/priv", "commit": "C1", "url_redacted": True}]


def test_explain_unresolved_names_the_credential_problem(monkeypatch):
    monkeypatch.setattr(ns_top, "folder_git_info", lambda name: {"has_git": False})
    local = {"priv": {"name": "priv", "url": "https://github.com/me/priv", "commit": "C1"}}
    assert ns_top.complete_baked_entries(["priv"], local, _REDACTED)[0]["url"] == ""
    why = ns_top.explain_unresolved(["priv"], local, _REDACTED)["priv"]
    assert "带凭据的私有仓库" in why and "同一个仓库" in why and "不带凭据" in why, why
    assert "github.com" not in why, "说明里不打印地址(私有仓库常带凭据)"
    msg = ns_top.unresolved_nodes_message(["priv"], {"priv": why})
    assert "本机清单里没有" not in msg and why in msg
    # 不知道云端 manifest 时按补全规则反推(/sync_nodes 读云端之前那道检查)
    why2 = ns_top.explain_unresolved(["priv"], local, None, cloud_read=False)["priv"]
    assert "不带凭据" in why2 and "本机清单里没有" not in why2, why2
    # 本机真的没有、云端也没报来源
    why3 = ns_top.explain_unresolved(["ghost"], {}, [{"name": "ghost", "url": "", "commit": ""}])["ghost"]
    assert "云端没报它的来源" in why3, why3
    # 不给 reasons 时仍是笼统说法(兼容)
    assert "拿不到它的来源" in ns_top.unresolved_nodes_message(["x"])


def test_reconcile_block_message_is_precise(baked, monkeypatch):
    monkeypatch.setattr(ns_top, "fetch_cloud_nodes", lambda cfg, timeout=20: (["priv", "pub"], _REDACTED))
    ns_top.write_baked_nodes([{"name": "pub", "url": "https://github.com/x/pub", "commit": "p1"}])
    monkeypatch.setattr(ns_top, "folder_git_info", lambda name: {
        "has_git": True, "url": "https://github.com/me/priv", "commit": "C1", "pushed": True, "dirty": False}
        if name == "priv" else {"has_git": True, "url": "https://github.com/x/pub", "commit": "p1"})
    with pytest.raises(ns_top.DeployBlocked) as ei:
        ns_top.reconcile_baked_with_cloud({})
    assert "实际装的" in str(ei.value) and "不带凭据" in str(ei.value), str(ei.value)


def test_sync_nodes_409_messages_are_precise_and_prune_is_not_blocked(monkeypatch):
    monkeypatch.setattr(ns_pkg, "fetch_cloud_nodes", lambda cfg, timeout=20: (["priv", "pub"], _REDACTED))
    ns_pkg.write_baked_nodes([{"name": "pub", "url": "https://github.com/x/pub", "commit": "p1"},
                              {"name": "priv", "url": "https://github.com/me/priv", "commit": "C1"}])
    deployed = []

    async def fake_run(resp, cmd, cwd, env):
        deployed.append([n["name"] for n in ns_pkg.read_baked_nodes()])
        return 0
    monkeypatch.setattr(rt, "_run_streamed", fake_run)
    pub = {"name": "pub", "url": "https://github.com/x/pub", "commit": "p1"}

    async def go(c, body):
        r = await c.post("/modal_bridge/sync_nodes", json=body)
        return r.status, await r.text()

    # a) 计划里没有 priv(云端有)→ 并回补不出来源 → 409,说明要准
    st, text = harness._run(lambda c: go(c, {"new_baked": [pub]}))
    err = json.loads(text)["error"]
    assert st == 409 and "不带凭据" in err and "本机清单里没有" not in err, err
    # b) 计划里带着空地址的 priv(/check_nodes 补不出来)→ 读云端之前就拦,说明同样要准
    st, text = harness._run(lambda c: go(c, {"new_baked": [pub, {"name": "priv", "url": "", "commit": ""}]}))
    err = json.loads(text)["error"]
    assert st == 409 and "不带凭据" in err and "本机清单里没有" not in err, err
    assert deployed == []
    # c) 用户这次就是要删它:不拦
    st, text = harness._run(lambda c: go(c, {"new_baked": [pub, {"name": "priv", "url": "", "commit": ""}],
                                             "prune": ["priv"]}))
    assert st == 200 and _rc(text) == 0 and deployed == [["pub"]], (text, deployed)


# ============================================================================
# 11 Registry 节点按 .tracking 判 dirty
# ============================================================================
def _registry_node(root: Path, folder="ComfyUI-GGUF") -> Path:
    d = root / "custom_nodes" / folder
    (d / "tools").mkdir(parents=True)
    files = {"__init__.py": "X = 1\n", "nodes.py": "Y = 2\n", "tools/convert.py": "Z = 3\n",
             "README.md": "readme\n", "requirements.txt": "gguf\n",
             "pyproject.toml": '[project]\nname = "ComfyUI-GGUF"\nversion = "1.1.10"\n'}
    for rel, text in files.items():
        (d / rel).write_text(text, encoding="utf-8")
    # Manager:先解包,后写 .tracking(namelist,目录项以 / 结尾)
    (d / ".tracking").write_text("\n".join([*files, "tools/"]), encoding="utf-8")
    t = time.time() - 3600
    for rel in files:
        os.utime(d / rel, (t, t))
    os.utime(d / ".tracking", (t + 1, t + 1))
    return d


def test_cnr_dirty_rules(tmp_path):
    d = _registry_node(tmp_path)
    assert ns_top.cnr_dirty(d) is False
    # 运行时写的非代码文件、字节码、节点自带的虚拟环境:都不算
    (d / "settings.json").write_text("{}", encoding="utf-8")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "nodes.cpython-312.py").write_text("", encoding="utf-8")
    (d / ".venv" / "lib").mkdir(parents=True)
    (d / ".venv" / "lib" / "site.py").write_text("", encoding="utf-8")
    (d / "venv").mkdir()
    (d / "venv" / "x.py").write_text("", encoding="utf-8")
    assert ns_top.cnr_dirty(d) is False
    # 列了的非代码文件被删:不算(云端跑的代码不变)
    (d / "README.md").unlink()
    assert ns_top.cnr_dirty(d) is False
    # 改了列着的文件
    os.utime(d / "nodes.py", None)
    assert ns_top.cnr_dirty(d) is True


@pytest.mark.parametrize("change", ["new_py", "deleted_py", "edited_nested"])
def test_cnr_dirty_detects_code_changes(tmp_path, change):
    d = _registry_node(tmp_path)
    if change == "new_py":
        (d / "my_patch.py").write_text("P = 1\n", encoding="utf-8")
    elif change == "deleted_py":
        (d / "tools" / "convert.py").unlink()
    else:
        (d / "tools" / "convert.py").write_text("Z = 4\n", encoding="utf-8")
    assert ns_top.cnr_dirty(d) is True


def test_cnr_dirty_routes_to_private_channel(tmp_path, monkeypatch):
    d = _registry_node(tmp_path)
    monkeypatch.setattr(ns_top, "_comfyui_root", lambda: tmp_path)
    monkeypatch.setattr(ns_top, "_is_own_git_repo", lambda p: False)
    g = ns_top.folder_git_info("ComfyUI-GGUF")
    assert g["cnr_id"] == "comfyui-gguf" and g["dirty"] is False
    baked_entry = ns_top._baked_entry("ComfyUI-GGUF", g)
    monkeypatch.setattr(ns_top, "analyze_workflow", lambda p: {
        "builtin": [], "by_folder": {"ComfyUI-GGUF": ["UnetLoaderGGUF"]}, "unresolved": []})
    plan = ns_top.plan_node_sync({}, baked=[baked_entry])
    assert plan["expect_baked"] == ["ComfyUI-GGUF"] and not plan["local_pack"]
    (d / "nodes.py").write_text("Y = 3  # 本机改过\n", encoding="utf-8")
    assert ns_top.folder_git_info("ComfyUI-GGUF")["dirty"] is True
    plan = ns_top.plan_node_sync({}, baked=[baked_entry])
    assert [(x["folder"], x["reason"]) for x in plan["local_pack"]] == [("ComfyUI-GGUF", "dirty")], plan
    assert "ComfyUI-GGUF" not in plan["expect_baked"], "改过的不能再声明「应跑镜像版」"


def test_cnr_dirty_unreadable_tracking_is_clean(tmp_path):
    d = _registry_node(tmp_path)
    (d / ".tracking").unlink()
    assert ns_top.cnr_dirty(d) is False


# ============================================================================
# 12 bridge_cli:cli.json 损坏即中止;endpoint 未知时问 Modal API
# ============================================================================
@pytest.mark.parametrize("bad", ['{"endpoint": "https://ws--comfyui-bridge", "key": "bk-1", ', "[]", "\xff\xfe"])
def test_cli_deploy_aborts_on_corrupt_cli_json(cli_env, bad):
    ev = cli_env
    if bad == "\xff\xfe":
        ev["cli_path"].write_bytes(b"\xff\xfe\x00{")
    else:
        ev["cli_path"].write_text(bad, encoding="utf-8")
    before = ev["cli_path"].read_bytes()
    code = ev["go"](comfyui_tag="v0.37.2")
    assert code not in (0, None) and "cli.json" in str(code), code
    assert ev["runs"] == [] and ev["urls"] == [], "不能换 key、不能对账、不能写 Secret"
    assert ev["cli_path"].read_bytes() == before, "坏文件原样留着给用户修"


def test_cli_configure_refuses_to_overwrite_corrupt_cli_json(cli_env):
    import bridge_cli
    ev = cli_env
    ev["cli_path"].write_text('{"keys": {"a": "bk-a"}, ', encoding="utf-8")
    before = ev["cli_path"].read_bytes()
    with pytest.raises(SystemExit):
        bridge_cli.cmd_configure(types.SimpleNamespace(endpoint="https://ws--x", key="bk-new"))
    assert ev["cli_path"].read_bytes() == before
    # flag 给全了就不读 cli.json:坏了也不挡
    c = bridge_cli._client(types.SimpleNamespace(endpoint="https://ws--x", key="bk-1"))
    assert c.base == "https://ws--x"


def test_cli_deploy_checks_modal_when_endpoint_unknown(cli_env):
    """第二台机器(没插件、没 cli.json)对已部署的 app 跑 deploy:以前直接按全新部署,云端节点一个都不核对。"""
    ev = cli_env
    ev["deployed_endpoint"] = "https://ws-dev--comfyui-bridge"
    assert ev["go"](comfyui_tag="v0.37.2") == 0
    assert ev["lookups"] == ["comfyui-bridge"]
    assert ev["urls"] == ["https://ws-dev--comfyui-bridge-health.modal.run"], "查到 endpoint 就要拿它对账"
    # 已知 endpoint 时不再问 Modal
    ev["lookups"].clear()
    assert ev["go"](comfyui_tag="v0.37.2") == 0 and ev["lookups"] == []


def test_cli_deploy_aborts_when_modal_cannot_be_asked(cli_env, monkeypatch):
    import bridge_cli
    ev = cli_env

    def fail(app):
        raise bridge_cli.EndpointLookupFailed("AuthError: token missing")
    monkeypatch.setattr(bridge_cli, "_lookup_deployed_endpoint", fail)
    code = ev["go"](comfyui_tag="v0.37.2")
    assert code not in (0, None) and "AuthError" in str(code) and "configure --endpoint" in str(code)
    assert ev["runs"] == [] and ev["urls"] == []


class _FakeModalLookup:
    def __init__(self, result):
        outer = self
        self.calls = []

        class NotFoundError(Exception):
            pass

        class Fn:
            def __init__(self, app, name):
                outer.calls.append((app, name))

            def get_web_url(self):
                if isinstance(result, BaseException):
                    raise result
                if result == "notfound":
                    raise NotFoundError("Lookup failed for Function")
                return result

        self.Function = types.SimpleNamespace(from_name=lambda app, name: Fn(app, name))
        self.exception = types.SimpleNamespace(NotFoundError=NotFoundError)


@pytest.mark.parametrize("result, want", [
    ("notfound", ""),
    ("https://ws--comfyui-bridge-health.modal.run", "https://ws--comfyui-bridge"),
    ("https://ws-dev--my-app-health.modal.run/", "https://ws-dev--my-app"),
])
def test_lookup_deployed_endpoint(result, want):
    import bridge_cli
    fake = _FakeModalLookup(result)
    assert bridge_cli._lookup_deployed_endpoint("comfyui-bridge", fake) == want
    assert fake.calls == [("comfyui-bridge", "health_endpoint")]


@pytest.mark.parametrize("result", [PermissionError("AuthError"), ConnectionError("grpc"), None,
                                    "https://ws--comfyui-bridge-abc123.modal.run"])
def test_lookup_deployed_endpoint_failures(result):
    import bridge_cli
    with pytest.raises(bridge_cli.EndpointLookupFailed):
        bridge_cli._lookup_deployed_endpoint("comfyui-bridge", _FakeModalLookup(result))


def test_health_function_name_matches_cloud():
    """_lookup_deployed_endpoint 按函数名查;云端改名 / 改 label 时这里要跟着改。"""
    import ast
    src = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "health_endpoint")
    assert any("-health" in ast.unparse(d) for d in fn.decorator_list), "health_endpoint 的 label 不再是 <app>-health"
    import bridge_cli
    assert bridge_cli._HEALTH_FUNCTION == "health_endpoint"


def test_lookup_uses_only_documented_sdk_surface():
    """真 SDK 里确实有这两样(只看源码,不发请求)。"""
    modal = pytest.importorskip("modal")
    assert hasattr(modal.Function, "from_name") and hasattr(modal.exception, "NotFoundError")
    src = inspect.getsource(modal.functions) if hasattr(modal, "functions") else ""
    assert "get_web_url" in src or hasattr(modal.Function, "get_web_url")
