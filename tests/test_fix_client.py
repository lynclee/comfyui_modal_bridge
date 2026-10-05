"""fix1005 客户端 / MCP 回归(2026-10-05 深度 review):bridge_client、modal_client、mcp_server。

云端 endpoint 一律用本地 http.server 模拟(按 URL 路径 /run /status /cancel /fetch 分派),
不碰真实网络、不需要 mcp 包(测试里桩掉)。
"""
import asyncio
import base64
import importlib.util
import json
import os
import socket
import sys
import threading
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bridge_client as bc  # noqa: E402
import modal_client as mc  # noqa: E402


# ── 模拟云端 ─────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def send_json(self, code, body, ctype="application/json", extra=None):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def send_truncated(self, data: bytes, *, content_length=None):
        """只发一半就断开。content_length=None → 不带 Content-Length(close 分隔的正文)。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def drop(self):
        """收了请求、一个字节都不回就断开(响应丢在网关的形态)。"""
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _dispatch(self, method):
        u = urlsplit(self.path)
        label = u.path.strip("/")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        self.server.log.append({"method": method, "label": label, "q": q, "body": body})
        fn = self.server.behav.get(label)
        if fn is None:
            return self.send_json(404, {"error": f"no behaviour for {label}"})
        return fn(self, q, body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


@pytest.fixture
def cloud(monkeypatch):
    # 本机回环不能走系统代理(CI 没有代理;本机的代理 env 也别把 127.0.0.1 送出去)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.behav, srv.log = {}, []
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    srv.base = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _client(srv, key="k"):
    c = bc.BridgeClient("https://ws--comfyui-bridge", key, timeout=5)
    c._url = lambda label: f"{srv.base}/{label}"
    c._SUBMIT_RETRY_DELAYS = (0,) * len(bc.BridgeClient._SUBMIT_RETRY_DELAYS)   # 次数照旧,不真的睡
    return c


def _calls(srv, label, *, ack=None):
    out = [x for x in srv.log if x["label"] == label]
    if ack is not None:
        out = [x for x in out if bool(x["q"].get("ack")) == ack]
    return out


WF = {"1": {"class_type": "X", "inputs": {}}}


# ── C2:提交结果未知要交还 job_id ────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["504", "500", "drop", "non_json", "429", "no_id"])
def test_submit_unknown_hands_back_job_id(cloud, mode):
    def run(h, q, b):
        if mode == "drop":
            return h.drop()
        if mode == "non_json":
            return h.send_json(200, b"<html>gateway</html>", "text/html")
        if mode == "no_id":
            return h.send_json(200, {"status": "queued"})
        return h.send_json(int(mode), b"upstream", "text/plain")
    cloud.behav["run"] = run
    c = _client(cloud)
    with pytest.raises(bc.SubmitUnknown) as ei:
        c.submit(WF)
    e = ei.value
    assert isinstance(e, bc.BridgeError), "新异常必须继承 BridgeError,已有的 except BridgeError 才兜得住"
    runs = _calls(cloud, "run")
    assert len(runs) == 4, f"应当 1 次 + 重试 3 次,实际 {len(runs)}"
    ids = {r["body"]["job_id"] for r in runs}
    assert ids == {e.job_id}, f"重试必须用同一个幂等键,并把它交还调用方: {ids} vs {e.job_id}"
    assert e.job_id in str(e) and "别重新提交" in str(e)


def test_submit_retry_schedule_is_three_with_backoff():
    d = bc.BridgeClient._SUBMIT_RETRY_DELAYS
    assert len(d) == 3 and all(a < b for a, b in zip(d, d[1:])), d


@pytest.mark.parametrize("code,body", [
    (401, {"error": "unauthorized — bad or missing bridge key"}),
    (400, {"error": "bad request"}),
    (404, b"modal-http: app not found"),
    (200, {"error": "invalid job_id: 'x'"}),
])
def test_submit_definitely_rejected_is_not_unknown(cloud, code, body):
    cloud.behav["run"] = lambda h, q, b: h.send_json(code, body, "application/json" if isinstance(body, dict) else "text/plain")
    c = _client(cloud)
    with pytest.raises(bc.BridgeError) as ei:
        c.submit(WF)
    assert not isinstance(ei.value, bc.SubmitUnknown), "确定没提交的不能报成「结果未知」"
    assert len(_calls(cloud, "run")) == 1, "确定被拒的请求不该重试"


def test_submit_retry_then_duplicate_is_success(cloud):
    seen = []

    def run(h, q, b):
        seen.append(b["job_id"])
        if len(seen) == 1:
            return h.send_json(504, b"upstream timeout", "text/plain")
        return h.send_json(200, {"id": b["job_id"], "status": "running", "gpu": "H100", "duplicate": True})
    cloud.behav["run"] = run
    d = _client(cloud).submit(WF, job_id="fixed-id-1")
    assert d["id"] == "fixed-id-1" and d["duplicate"] is True, "duplicate 是正常业务答复,不是错误"
    assert seen == ["fixed-id-1", "fixed-id-1"]


def _run_async(coro):
    return asyncio.run(coro)


async def _mc_submit(srv, monkeypatch):
    import aiohttp
    monkeypatch.setattr(mc, "_endpoint", lambda base, label: f"{srv.base}/{label}")
    sleeps = []
    real_sleep = asyncio.sleep

    async def no_sleep(s, *a, **k):     # 只记退避间隔;aiohttp 内部的 sleep(0) 照常让出
        if s:
            sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(mc.asyncio, "sleep", no_sleep)
    async with aiohttp.ClientSession() as s:
        try:
            return await mc.submit_job(s, {"modal_endpoint_base": "x", "bridge_api_key": "k"}, WF), sleeps
        except Exception as e:      # noqa: BLE001 — 由调用方断言类型
            return e, sleeps


@pytest.mark.parametrize("mode", ["504", "drop", "non_json", "redirect"])
def test_modal_client_submit_unknown_hands_back_job_id(cloud, monkeypatch, mode):
    def run(h, q, b):
        if mode == "drop":
            return h.drop()
        if mode == "non_json":
            return h.send_json(200, b"<html>gateway</html>", "text/html")
        if mode == "redirect":
            return h.send_json(307, b"", "text/plain", {"Location": f"{cloud.base}/elsewhere"})
        return h.send_json(504, b"upstream", "text/plain")
    cloud.behav["run"] = run
    cloud.behav["elsewhere"] = lambda h, q, b: h.send_json(200, {"id": "stolen"})
    e, sleeps = _run_async(_mc_submit(cloud, monkeypatch))
    assert isinstance(e, mc.SubmitUnknown) and isinstance(e, RuntimeError), e
    runs = _calls(cloud, "run")
    assert {r["body"]["job_id"] for r in runs} == {e.job_id}
    assert e.job_id in str(e)
    assert not _calls(cloud, "elsewhere"), "重定向不能跟随(会把 key 带过去)"
    if mode == "redirect":
        assert len(runs) == 1, "同一个地址还会回同一个跳转,别重试"
    else:
        assert len(runs) == 4, f"默认重试 3 次,实际请求 {len(runs)} 次"
        assert sleeps == [1.5, 3.0, 6.0], f"要带指数退避: {sleeps}"


@pytest.mark.parametrize("code,body", [
    (401, {"error": "unauthorized"}),
    (400, b"bad request"),
    (200, {"error": "Missing 'workflow' in payload"}),
])
def test_modal_client_submit_definitely_rejected(cloud, monkeypatch, code, body):
    cloud.behav["run"] = lambda h, q, b: h.send_json(code, body, "application/json" if isinstance(body, dict) else "text/plain")
    e, _ = _run_async(_mc_submit(cloud, monkeypatch))
    assert isinstance(e, RuntimeError) and not isinstance(e, mc.SubmitUnknown), e
    assert len(_calls(cloud, "run")) == 1


def test_modal_client_cancel_raises_on_http_error_and_non_object(cloud, monkeypatch):
    """云端 /cancel 回 500 + {"detail"}(没有 error 字段)以前被原样返回,本机 /cancel 据此报 ok:true。"""
    import aiohttp
    monkeypatch.setattr(mc, "_endpoint", lambda base, label: f"{cloud.base}/{label}")
    cfg = {"modal_endpoint_base": "x", "bridge_api_key": "k"}

    async def go():
        async with aiohttp.ClientSession() as s:
            return await mc.cancel(s, cfg, "j1")

    for code, body in ((500, {"detail": "Function call failed"}), (200, ["oops"]), (200, b"<html>")):
        cloud.behav["cancel"] = lambda h, q, b, code=code, body=body: h.send_json(code, body)
        with pytest.raises(RuntimeError):
            _run_async(go())
    cloud.behav["cancel"] = lambda h, q, b: h.send_json(200, {"id": "j1", "status": "cancelled"})
    assert _run_async(go()) == {"id": "j1", "status": "cancelled"}


def test_modal_client_health_non_utf8_body_is_health_unavailable(cloud, monkeypatch):
    """正文解不了 UTF-8 时以前抛 UnicodeDecodeError —— 不在 health 的 except 里,直接漏出去。"""
    import aiohttp
    import health_client as hc
    monkeypatch.setattr(hc, "url", lambda cfg: f"{cloud.base}/health")

    real_sleep = asyncio.sleep

    async def no_sleep(_s, *a, **k):
        await real_sleep(0)
    monkeypatch.setattr(mc.asyncio, "sleep", no_sleep)
    cloud.behav["health"] = lambda h, q, b: h.send_json(200, b"\xff\xfe\xfa", "text/plain; charset=utf-8")

    async def go():
        async with aiohttp.ClientSession() as s:
            return await mc.health(s, {"modal_endpoint_base": "x", "bridge_api_key": "k"})
    with pytest.raises(hc.HealthUnavailable):
        _run_async(go())


# ── C8:HTTP 错误 / 畸形状态不能被当成正常响应 ─────────────────────────────
def test_status_http_error_raises_with_body_and_wait_gives_up(cloud):
    cloud.behav["status"] = lambda h, q, b: h.send_json(500, {"error": "internal: job_state unavailable"})
    c = _client(cloud)
    with pytest.raises(bc.BridgeError) as ei:
        c.status("j1")
    assert "500" in str(ei.value) and "job_state unavailable" in str(ei.value), "要附带正文"
    with pytest.raises(bc.BridgeError) as ei:
        c.wait("j1", timeout_s=30, poll_s=0)
    assert "连续" in str(ei.value), "应在连续失败上限处放弃,而不是空转到 timeout"


@pytest.mark.parametrize("code,body", [(500, b'["oops"]'), (200, b'["oops"]'), (200, b'{"progress": {}}'),
                                       (200, b'{"status": 3}')])
def test_status_malformed_raises_bridge_error_not_attribute_error(cloud, code, body):
    cloud.behav["status"] = lambda h, q, b: h.send_json(code, body)
    c = _client(cloud)
    with pytest.raises(bc.BridgeError):
        c.status("j1")
    with pytest.raises(bc.BridgeError):
        c.wait("j1", timeout_s=30, poll_s=0)


def test_status_not_found_is_a_normal_answer_and_legacy_shape_is_normalised(cloud):
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, {"id": q["job_id"], "status": "not_found",
                                                              "error": "job not found"})
    assert _client(cloud).status("j1")["status"] == "not_found"
    # 0.8.40 及更早:只有 error,没有 status
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, {"error": "job not found"})
    assert _client(cloud).status("j1")["status"] == "not_found"


def test_wait_counts_unknown_status_as_error_instead_of_resetting():
    c = bc.BridgeClient("https://ws--comfyui-bridge", "k")
    seq = [{"status": "weird"}] * 4 + [{"status": "completed"}]
    c.status = lambda jid: seq.pop(0)
    assert c.wait("j", timeout_s=30, poll_s=0)["status"] == "completed"

    c.status = lambda jid: {"status": "weird"}
    with pytest.raises(bc.BridgeError) as ei:
        c.wait("j", timeout_s=30, poll_s=0, max_consecutive_errors=5)
    assert "连续 5 次" in str(ei.value) and "weird" in str(ei.value)

    # 网络错与不认识的状态交替出现,也要累计,不能互相清零
    seq2 = [bc.BridgeError("net"), {"status": "weird"}] * 3

    def flaky(jid):
        x = seq2.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    c.status = flaky
    with pytest.raises(bc.BridgeError):
        c.wait("j", timeout_s=30, poll_s=0, max_consecutive_errors=5)
    assert len(seq2) == 1, "第 5 次就该放弃"


@pytest.mark.parametrize("code,body", [(500, {"detail": "Function call failed"}), (502, b"bad gateway"),
                                       (200, b'["x"]')])
def test_cancel_http_error_raises(cloud, code, body):
    cloud.behav["cancel"] = lambda h, q, b: h.send_json(code, body)
    with pytest.raises(bc.BridgeError) as ei:
        _client(cloud).cancel("j1")
    if code == 500:
        assert "Function call failed" in str(ei.value), "要附带正文"


def test_cli_cancel_exits_nonzero_on_http_500(cloud, monkeypatch, capsys):
    """bridge_cli 只接 BridgeError;以前 /cancel 回 500 时 cancel() 返回 {"detail"},CLI 退出码 0。"""
    import bridge_cli
    cloud.behav["cancel"] = lambda h, q, b: h.send_json(500, {"detail": "Function call failed"})
    monkeypatch.setattr(bc.BridgeClient, "_url", lambda self, label: f"{cloud.base}/{label}")
    monkeypatch.setattr(sys, "argv", ["bridge_cli", "cancel", "j1", "--endpoint",
                                      "https://ws--comfyui-bridge", "--key", "k"])
    with pytest.raises(SystemExit) as ei:
        bridge_cli.main()
    assert ei.value.code not in (0, None)


# ── C3 / C4 / C16:Volume 产物下载 ─────────────────────────────────────────
def _vol_state(job="j1", files=None, *, sizes=True):
    files = files or {"clip.mp4": b"V" * 300_000}
    imgs = []
    for fn, data in files.items():
        it = {"filename": fn, "volume_path": f"_outputs/{job}/9__{fn}"}
        if sizes:
            it["size_bytes"] = len(data)
        imgs.append(it)
    return {"id": job, "status": "completed", "images": imgs}


def _volume(srv, store, *, cut=None, no_len=False):
    """store: {volume_path: bytes}。ack 删除;cut=路径 → 只发一半并断开。"""
    def fetch(h, q, b):
        p = q["path"]
        if q.get("ack"):
            store.pop(p, None)
            return h.send_json(200, {"deleted": p})
        if p not in store:
            return h.send_json(404, {"error": f"not found: {p}"})
        data = store[p]
        if cut == p:
            return h.send_truncated(data[: len(data) // 2], content_length=None if no_len else len(data))
        if no_len:
            return h.send_truncated(data, content_length=None)   # 完整正文,但没有 Content-Length
        return h.send_json(200, data, "application/octet-stream")
    srv.behav["fetch"] = fetch


def test_truncated_download_without_content_length_is_caught_by_size_bytes(cloud, tmp_path):
    state = _vol_state()
    vp = state["images"][0]["volume_path"]
    store = {vp: b"V" * 300_000}
    _volume(cloud, store, cut=vp, no_len=True)
    c = _client(cloud)
    with pytest.raises(bc.BridgeError) as ei:
        c.download_outputs(state, str(tmp_path / "out"))
    assert "不完整" in str(ei.value)
    assert not _calls(cloud, "fetch", ack=True), "截断的文件绝不能 ack"
    assert vp in store, "远端副本必须还在"
    assert not (tmp_path / "out" / "clip.mp4").exists() and not list((tmp_path / "out").glob("*.part"))


def test_unverifiable_download_is_saved_but_not_acked(cloud, tmp_path):
    """没有 size_bytes、也没有 Content-Length:照常落盘,但不发 ack(远端留给 TTL)。"""
    state = _vol_state(sizes=False)
    vp = state["images"][0]["volume_path"]
    store = {vp: b"V" * 1000}
    _volume(cloud, store, no_len=True)
    outs = _client(cloud).download_outputs(state, str(tmp_path / "out"))
    assert outs[0]["size_bytes"] == 1000 and Path(outs[0]["path"]).read_bytes() == b"V" * 1000
    assert not _calls(cloud, "fetch", ack=True), "无法校验完整性时不能 ack"
    assert vp in store


def test_content_length_alone_still_verifies_and_acks(cloud, tmp_path):
    state = _vol_state(sizes=False)
    vp = state["images"][0]["volume_path"]
    store = {vp: b"V" * 1000}
    _volume(cloud, store)
    _client(cloud).download_outputs(state, str(tmp_path / "out"))
    assert len(_calls(cloud, "fetch", ack=True)) == 1 and vp not in store


def test_size_bytes_mismatch_is_rejected_even_with_matching_content_length(cloud, tmp_path):
    state = _vol_state()
    state["images"][0]["size_bytes"] = 999_999
    vp = state["images"][0]["volume_path"]
    _volume(cloud, {vp: b"V" * 300_000})
    with pytest.raises(bc.BridgeError):
        _client(cloud).download_outputs(state, str(tmp_path / "out"))
    assert not _calls(cloud, "fetch", ack=True)


def test_refetch_after_ack_returns_local_files_instead_of_404(cloud, tmp_path):
    files = {"clip.mp4": b"V" * 300_000, "audio.wav": b"A" * 1000}
    state = _vol_state(files=files)
    store = {it["volume_path"]: files[it["filename"]] for it in state["images"]}
    _volume(cloud, store)
    c = _client(cloud)
    out = tmp_path / "out"
    first = c.download_outputs(state, str(out))
    assert not store, "全部落盘后才 ack,这里应已全部删除"
    assert (out / ".bridge_receipt_j1.json").is_file(), "ack 之前要写回执"
    cloud.log.clear()
    again = c.download_outputs(state, str(out))
    assert again == first, "重入要直接返回同样的结果"
    assert not _calls(cloud, "fetch", ack=False), "不能再去云端拿一份已经删掉的文件"
    assert bc.BridgeClient.received_outputs(str(out), "j1") == first
    # 本地文件被改动 / 删掉 → 回执对不上,走正常路径(此时云端已删,如实报 404)
    (out / "audio.wav").write_bytes(b"changed")
    assert bc.BridgeClient.received_outputs(str(out), "j1") is None
    with pytest.raises(bc.BridgeError) as ei:
        c.download_outputs(state, str(out))
    assert "404" in str(ei.value)


def test_no_ack_when_receipt_cannot_be_written(cloud, tmp_path):
    """没有回执,ack 之后再取一次就是硬 404 —— 回执写不下来就不 ack,远端留给 TTL。"""
    state = _vol_state()
    vp = state["images"][0]["volume_path"]
    store = {vp: b"V" * 300_000}
    _volume(cloud, store)
    out = tmp_path / "out"
    (out / ".bridge_receipt_j1.json").mkdir(parents=True)     # 回执位置被一个目录占着,写不进去
    outs = _client(cloud).download_outputs(state, str(out))
    assert Path(outs[0]["path"]).read_bytes() == store[vp]
    assert not _calls(cloud, "fetch", ack=True) and vp in store


def test_retry_after_partial_failure_reuses_own_names(cloud, tmp_path):
    files = {"a.mp4": b"A" * 5000, "b.mp4": b"B" * 7000}
    state = _vol_state(files=files)
    vps = [it["volume_path"] for it in state["images"]]
    store = {it["volume_path"]: files[it["filename"]] for it in state["images"]}
    _volume(cloud, store, cut=vps[1])
    c = _client(cloud)
    out = tmp_path / "out"
    with pytest.raises(bc.BridgeError):
        c.download_outputs(state, str(out))
    assert not _calls(cloud, "fetch", ack=True)
    _volume(cloud, store)
    outs = c.download_outputs(state, str(out))
    assert [o["filename"] for o in outs] == ["a.mp4", "b.mp4"], "上次自己写下的文件不能被当成别人的而改名"
    assert sorted(p.name for p in out.iterdir() if not p.name.startswith(".")) == ["a.mp4", "b.mp4"]
    assert not store


def test_shared_out_dir_never_overwrites_another_jobs_output(cloud, tmp_path):
    out = tmp_path / "shared"
    b64 = lambda d: base64.b64encode(d).decode()   # noqa: E731
    c = _client(cloud)
    c.download_outputs({"id": "jobA", "status": "completed",
                        "images": [{"filename": "MiniMax_00001_.png", "data_base64": b64(b"AAAA")}]}, str(out))
    r = c.download_outputs({"id": "jobB", "status": "completed",
                            "images": [{"filename": "MiniMax_00001_.png", "data_base64": b64(b"BBBB")}]}, str(out))
    assert (out / "MiniMax_00001_.png").read_bytes() == b"AAAA", "别的任务的产物被覆盖了"
    assert Path(r[0]["path"]).read_bytes() == b"BBBB" and r[0]["filename"] != "MiniMax_00001_.png"
    # Volume 产物同理
    files = {"MiniMax_00001_.png": b"CCCCCC"}
    state = _vol_state(job="jobC", files=files)
    _volume(cloud, {state["images"][0]["volume_path"]: b"CCCCCC"})
    r = c.download_outputs(state, str(out))
    assert (out / "MiniMax_00001_.png").read_bytes() == b"AAAA"
    assert Path(r[0]["path"]).read_bytes() == b"CCCCCC"
    # 同一任务重复取(base64,没有回执):内容一样就原地复用,不多出一份
    before = sorted(p.name for p in out.iterdir())
    r2 = c.download_outputs({"id": "jobA", "status": "completed",
                             "images": [{"filename": "MiniMax_00001_.png", "data_base64": b64(b"AAAA")}]}, str(out))
    assert r2[0]["filename"] == "MiniMax_00001_.png"
    assert sorted(p.name for p in out.iterdir()) == before


def test_base64_only_download_writes_no_receipt(tmp_path):
    """纯 base64 产物不写回执:下游(comfyagent)会数目录里的文件。"""
    c = bc.BridgeClient.__new__(bc.BridgeClient)   # 不碰网络,连 __init__ 都不需要
    c.download_outputs({"id": "j", "status": "completed",
                        "images": [{"filename": "a.png", "data_base64": base64.b64encode(b"x").decode()}]},
                       str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png"]


@pytest.mark.parametrize("fn", [".", "..", "", "a/..", "/"])
def test_dot_filenames_stay_inside_out_dir(tmp_path, fn):
    out = tmp_path / "job"
    c = bc.BridgeClient("https://ws--comfyui-bridge", "k")
    r = c.download_outputs({"id": "job", "status": "completed",
                            "images": [{"filename": fn, "data_base64": base64.b64encode(b"x").decode()}]},
                           str(out))
    assert Path(r[0]["path"]).parent == out and Path(r[0]["path"]).read_bytes() == b"x"
    assert r[0]["filename"] == "output.bin", r
    assert sorted(p.name for p in tmp_path.iterdir()) == ["job"], "有文件写到了 out_dir 外面"


def test_bad_base64_is_bridge_error(tmp_path):
    c = bc.BridgeClient("https://ws--comfyui-bridge", "k")
    with pytest.raises(bc.BridgeError):
        c.download_outputs({"id": "j", "status": "completed",
                            "images": [{"filename": "a.png", "data_base64": "!!!not-base64"}]}, str(tmp_path))


def test_download_volume_wraps_every_failure_in_bridge_error(cloud, tmp_path, monkeypatch):
    c = _client(cloud)
    # IncompleteRead:声明 1000 字节、只发 500 就断
    cloud.behav["fetch"] = lambda h, q, b: h.send_truncated(b"x" * 500, content_length=1000)
    with pytest.raises(bc.BridgeError):
        c._download_volume("j", "_outputs/j/a", tmp_path / "a")
    # URLError:连接被拒
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    c._url = lambda label: f"http://127.0.0.1:{dead}/{label}"
    with pytest.raises(bc.BridgeError):
        c._download_volume("j", "_outputs/j/a", tmp_path / "a")
    # 跨源重定向:拒绝跟随的 ValueError
    c = _client(cloud)
    cloud.behav["fetch"] = lambda h, q, b: h.send_json(302, b"", "text/plain",
                                                       {"Location": "http://other.invalid/x"})
    with pytest.raises(bc.BridgeError):
        c._download_volume("j", "_outputs/j/a", tmp_path / "a")
    # 落盘失败:目标位置是个目录
    cloud.behav["fetch"] = lambda h, q, b: h.send_json(200, b"data", "application/octet-stream")
    (tmp_path / "isdir").mkdir()
    with pytest.raises(bc.BridgeError):
        c._download_volume("j", "_outputs/j/a", tmp_path / "isdir")
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("code,want,not_want", [
    (401, "key", "0.7.3"),
    (403, "不在该任务的产物目录内", "0.7.3"),
    (404, "不在 Volume 上", "0.7.3"),
])
def test_fetch_http_errors_get_specific_hints(cloud, tmp_path, code, want, not_want):
    body = {401: {"error": "unauthorized"}, 403: {"error": "path out of job scope"},
            404: {"error": "not found: _outputs/j/a"}}[code]
    cloud.behav["fetch"] = lambda h, q, b: h.send_json(code, body)
    with pytest.raises(bc.BridgeError) as ei:
        _client(cloud)._download_volume("j", "_outputs/j/a", tmp_path / "a")
    assert want in str(ei.value) and not_want not in str(ei.value), str(ei.value)


def test_safe_job_id_matches_contract_c1():
    """契约 C1 的字面量;云端 modal_app 与本机 contract 合并后应与之逐字一致。"""
    assert bc._SAFE_JOB_ID.pattern == r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
    assert bc._safe_job_id(str(uuid.uuid4()))
    for bad in ("", ".", "..", "a..b", "_x", "-x", "a/b", "a" * 65, "abc\n", None, 3):
        assert not bc._safe_job_id(bad), bad


# ── C5:取消语义 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("resp,billing", [
    ({"id": "j", "status": "cancelled", "was_running": True}, False),
    ({"id": "j", "status": "failed", "error": "CUDA OOM", "cancel_noop": True}, False),
    ({"id": "j", "status": "completed", "cancel_noop": True, "was_running": True}, False),
    ({"id": "j", "status": "not_found", "error": "job not found"}, False),
    ({"id": "j", "status": "running", "error": "cancel failed: X", "was_running": True}, True),
    ({"id": "j", "status": "queued", "error": "任务正在提交中,还拿不到句柄"}, True),
    ({"ok": True, "still_billing": False, "error": "whatever"}, False),
    ({"ok": False, "still_billing": True}, True),
    (["not", "a", "dict"], True),
])
def test_cancel_still_billing_follows_contract_c2(resp, billing):
    assert bc.cancel_still_billing(resp) is billing


def test_cancel_docstrings_no_longer_say_every_error_is_billing():
    doc = bc.BridgeClient.cancel.__doc__
    assert "cancel_noop" in doc and "not_found" in doc
    assert "带 error 表示取消失败、云端仍在计费" not in doc


# ── MCP ─────────────────────────────────────────────────────────────────────
def _load_mcp(monkeypatch, env: dict, *, pathsep=None):
    class _Server:
        def __init__(self, *_a):
            pass

        def tool(self):
            return lambda fn: fn

        def run(self):
            pass
    for name in ("mcp", "mcp.server"):
        m = types.ModuleType(name)
        m.MCPServer = _Server
        monkeypatch.setitem(sys.modules, name, m)
    base = {"MODAL_BRIDGE_ENDPOINT": "", "MODAL_BRIDGE_KEY": "", "MODAL_BRIDGE_LOCAL_CONFIG": "",
            "MODAL_BRIDGE_LOCAL_CAPABILITY": "test-cap", "MODAL_BRIDGE_URL": "http://127.0.0.1:9",
            "MODAL_BRIDGE_OUT_DIR": "", "MODAL_BRIDGE_INPUT_DIRS": "."}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    if pathsep is not None:
        monkeypatch.setattr(os, "pathsep", pathsep)
    spec = importlib.util.spec_from_file_location(f"mcp_under_test_{uuid.uuid4().hex}", ROOT / "mcp_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cloud_mcp(monkeypatch, srv, tmp_path):
    m = _load_mcp(monkeypatch, {"MODAL_BRIDGE_ENDPOINT": "https://ws--comfyui-bridge",
                                "MODAL_BRIDGE_KEY": "k", "MODAL_BRIDGE_OUT_DIR": str(tmp_path / "mcp_out")})
    m._client._url = lambda label: f"{srv.base}/{label}"
    m._client._SUBMIT_RETRY_DELAYS = (0, 0, 0)
    return m


def _local_mcp(monkeypatch, srv):
    return _load_mcp(monkeypatch, {"MODAL_BRIDGE_URL": srv.base})


def _bridge_routes(srv, poll_state):
    """本机插件的 /modal_bridge/* 路由(local 模式)。"""
    srv.behav["modal_bridge/poll"] = lambda h, q, b: h.send_json(200, poll_state(q["job_id"]))


BIG = base64.b64encode(os.urandom(600_000)).decode()


def test_mcp_job_status_strips_base64_but_client_status_does_not(cloud, monkeypatch, tmp_path):
    st = {"status": "completed", "gpu_actual": "H100",
          "images": [{"filename": "a.png", "data_base64": BIG},
                     {"filename": "v.mp4", "volume_path": "_outputs/j1/9__v.mp4", "size_bytes": 12345}]}
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, {"id": q["job_id"], **st})
    m = _cloud_mcp(monkeypatch, cloud, tmp_path)
    r = m.job_status("j1")
    dumped = json.dumps(r)
    assert "data_base64" not in dumped and len(dumped) < 2000, len(dumped)
    assert r["outputs_summary"] == {"count": 2, "total_bytes": 600_000 + 12345}
    assert "fetch_result" in r["outputs_note"]
    assert r["status"] == "completed" and r["gpu_actual"] == "H100"
    # bridge_client.status 本身的形态不变(comfyagent 拿它直接喂 download_outputs)
    assert m._client.status("j1")["images"][0]["data_base64"] == BIG

    # 旧版顶层单产物
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, {"id": "j1", "status": "completed",
                                                              "filename": "a.png", "data_base64": BIG})
    r = m.job_status("j1")
    assert "data_base64" not in r and r["outputs_summary"] == {"count": 1, "total_bytes": 600_000}

    # local 模式透传 /poll 同样要剥
    lm = _local_mcp(monkeypatch, cloud)
    _bridge_routes(cloud, lambda jid: {"id": jid, **st})
    r = lm.job_status("j1")
    assert "data_base64" not in json.dumps(r) and r["outputs_summary"]["count"] == 2


@pytest.mark.parametrize("status", ["failed", "cancelled", "not_found"])
def test_mcp_fetch_result_terminal_states_are_not_not_ready(cloud, monkeypatch, tmp_path, status):
    st = {"status": status, "error": "CUDA OOM" if status == "failed" else None,
          "images": [{"filename": "a.png", "data_base64": BIG}]}
    st = {k: v for k, v in st.items() if v is not None}
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, {"id": q["job_id"], **st})
    for m in (_cloud_mcp(monkeypatch, cloud, tmp_path), _local_mcp(monkeypatch, cloud)):
        _bridge_routes(cloud, lambda jid: {"id": jid, **st})
        r = m.fetch_result("j1")
        assert r.get("not_ready") is not True, r
        assert r["ok"] is False and r["terminal"] is True and r["status"] == status and r["error"]
        assert "data_base64" not in json.dumps(r)
    assert not _calls(cloud, "modal_bridge/fetch_result")


@pytest.mark.parametrize("status", ["queued", "running", "delivering"])
def test_mcp_fetch_result_not_ready_only_for_live_states(cloud, monkeypatch, tmp_path, status):
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, {"id": q["job_id"], "status": status})
    r = _cloud_mcp(monkeypatch, cloud, tmp_path).fetch_result("j1")
    assert r["not_ready"] is True and r["ok"] is False and r["status"] == status and "terminal" not in r


def test_mcp_tool_docstrings():
    src = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
    import ast
    docs = {n.name: ast.get_docstring(n) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
    assert "方便直接重试" not in docs["fetch_result"] and "terminal" in docs["fetch_result"]
    for word in ("delivering", "not_found", "连续"):
        assert word in docs["job_status"], word
    assert "still_billing" in docs["cancel_job"] and "cancel_noop" in docs["cancel_job"]
    assert "ok:false / error 表示云端还在跑" not in docs["cancel_job"]
    assert "outcome" in docs["submit_workflow"]


def test_mcp_fetch_result_after_success_uses_receipt(cloud, monkeypatch, tmp_path):
    files = {"clip.mp4": b"V" * 3000}
    state = _vol_state(files=files)
    store = {state["images"][0]["volume_path"]: files["clip.mp4"]}
    _volume(cloud, store)
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, state)
    m = _cloud_mcp(monkeypatch, cloud, tmp_path)
    r1 = m.fetch_result("j1")
    assert r1["ok"] is True and os.path.isabs(r1["outputs"][0]["path"])
    # 云端记录已被 GC、副本已删
    cloud.behav["status"] = lambda h, q, b: h.send_json(200, {"id": "j1", "status": "not_found", "error": "job not found"})
    r2 = m.fetch_result("j1")
    assert r2["ok"] is True and r2["outputs"] == r1["outputs"] and r2.get("already_fetched") is True


def test_mcp_cancel_semantics_both_modes(cloud, monkeypatch, tmp_path):
    noop = {"id": "j1", "status": "completed", "cancel_noop": True, "was_running": True,
            "images": [{"filename": "a.png", "data_base64": BIG}]}
    # cloud 模式:cancel_noop → ok:true / still_billing:false,产物 base64 不进上下文
    m = _cloud_mcp(monkeypatch, cloud, tmp_path)
    cloud.behav["cancel"] = lambda h, q, b: h.send_json(200, noop)
    r = m.cancel_job("j1")
    assert r["ok"] is True and r["still_billing"] is False and "data_base64" not in json.dumps(r)
    cloud.behav["cancel"] = lambda h, q, b: h.send_json(200, {"id": "j1", "status": "running",
                                                              "error": "cancel failed: X"})
    r = m.cancel_job("j1")
    assert r["ok"] is False and r["still_billing"] is True
    cloud.behav["cancel"] = lambda h, q, b: h.send_json(500, {"detail": "boom"})
    r = m.cancel_job("j1")
    assert r["ok"] is False and r["still_billing"] is True, "请求失败 = 结果未知,只能按仍在计费报"

    # local 模式:老插件对 cancel_noop 报 ok:false(没有 still_billing)→ 按 C2 纠正
    lm = _local_mcp(monkeypatch, cloud)
    cloud.behav["modal_bridge/cancel"] = lambda h, q, b: h.send_json(
        200, {"ok": False, "id": "j1", "status": "failed", "error": "CUDA OOM", "cancel_noop": True})
    r = lm.cancel_job("j1")
    assert r["ok"] is True and r["still_billing"] is False
    # 新插件带 still_billing → 以它为准
    cloud.behav["modal_bridge/cancel"] = lambda h, q, b: h.send_json(
        200, {"ok": False, "still_billing": True, "id": "j1", "status": "running", "error": "cancel failed"})
    r = lm.cancel_job("j1")
    assert r["ok"] is False and r["still_billing"] is True
    cloud.behav["modal_bridge/cancel"] = lambda h, q, b: h.send_json(502, {"error": "Cannot connect"})
    assert lm.cancel_job("j1")["still_billing"] is True


def test_mcp_submit_unknown_returns_job_id(cloud, monkeypatch, tmp_path):
    cloud.behav["run"] = lambda h, q, b: h.send_json(504, b"upstream", "text/plain")
    r = _cloud_mcp(monkeypatch, cloud, tmp_path).submit_workflow(json.dumps(WF))
    runs = _calls(cloud, "run")
    assert r["outcome"] == "unknown" and r["ok"] is False and r["job_id"] == runs[0]["body"]["job_id"]


def test_mcp_cloud_gpu_class_is_validated(cloud, monkeypatch, tmp_path):
    cloud.behav["run"] = lambda h, q, b: h.send_json(200, {"id": b["job_id"], "status": "queued", "gpu": "x"})
    m = _cloud_mcp(monkeypatch, cloud, tmp_path)
    r = m.submit_workflow(json.dumps(WF), gpu_class="B200")
    assert r["ok"] is False and "gpu_class" in r["error"] and not _calls(cloud, "run"), "写错的档位不能静默按 primary 跑"
    assert m.submit_workflow(json.dumps(WF), gpu_class=" CHEAP ")["ok"] is True
    assert m.submit_workflow(json.dumps(WF))["ok"] is True
    assert [x["body"]["gpu_class"] for x in _calls(cloud, "run")] == ["cheap", "primary"]


def test_mcp_paths_and_env_parsing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    m = _load_mcp(monkeypatch, {"MODAL_BRIDGE_OUT_DIR": "rel/out"})
    assert os.path.isabs(m._OUT_DIR) and m._OUT_DIR == str(tmp_path / "rel" / "out")
    m = _load_mcp(monkeypatch, {})
    assert m._OUT_DIR == str(tmp_path / "modal_bridge_outputs")
    # Windows 风格:分号分隔、路径里带盘符冒号。按 ":" 切会把它切碎。
    m = _load_mcp(monkeypatch, {"MODAL_BRIDGE_INPUT_DIRS": r"C:\in;D:\more"}, pathsep=";")
    assert m._INPUT_DIRS == [r"C:\in", r"D:\more"]


def test_mcp_cloud_fetch_rejects_path_escaping_job_id(cloud, monkeypatch, tmp_path):
    m = _cloud_mcp(monkeypatch, cloud, tmp_path)
    for bad in ("../escape", "a/b", ".."):
        r = m.fetch_result(bad)
        assert r["ok"] is False and "job_id" in r["error"]
    assert not cloud.log


def test_mcp_local_job_id_is_url_encoded(cloud, monkeypatch):
    seen = []

    def poll(h, q, b):
        seen.append(q.get("job_id"))
        return h.send_json(200, {"id": q.get("job_id"), "status": "running"})
    cloud.behav["modal_bridge/poll"] = poll
    lm = _local_mcp(monkeypatch, cloud)
    lm.job_status("a&b=c#d e")
    lm.fetch_result("x&y")
    assert seen == ["a&b=c#d e", "x&y"]
