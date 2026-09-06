"""真实路由、HTTP 重定向及部分下载失败的隔离回归。只使用测试凭据。"""
import asyncio
import io
import threading
import urllib.request
import urllib.response
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import test_routes as harness
from comfyui_modal_bridge import routes as rt
from bridge_client import BridgeClient, BridgeError
from bridge_client import _SameOriginRedirect
from comfyui_modal_bridge.result_receipts import ResultReceipts


def test_loopback_requires_capability_before_parsing_request():
    harness._set_cfg()

    async def body(c):
        for route in harness._ROUTES:
            if not hasattr(route.handler, "__wrapped__"):
                continue
            for token in ("", "wrong-capability", "错误-token"):
                r = await c.request(route.method, route.path, headers={
                    "X-Modal-Bridge-Capability": token, "Content-Type": "text/plain",
                }, data="invalid-json")
                assert r.status == 403, (route.path, token, r.status)
                assert r.headers.get("X-Modal-Bridge-Auth") == "capability-required"
        assert harness.cfg_mod.load_config()["gpu_tier"] == "auto"

    harness._run(body)


def test_first_pairing_generates_private_capability_without_returning_it():
    harness._set_cfg(local_api_capability="")

    async def body(c):
        headers = {"X-Modal-Bridge-Capability": ""}
        public = await c.get("/modal_bridge/config", headers=headers)
        assert public.status == 200
        assert not (await public.json())["has_local_api_capability"]
        denied = await c.post("/modal_bridge/deploy", headers=headers, data="invalid-json")
        assert denied.status == 403
        cap = harness.cfg_mod.load_config()["local_api_capability"]
        assert cap.startswith("lc-")
        assert cap not in await denied.text()
        public = await c.get("/modal_bridge/config", headers=headers)
        assert cap not in await public.text()
        assert "local_api_capability" not in await public.json()
        # 只在测试内读取假配置；相同 token 在 localhost 和正确反代 Host 下均可用。
        for host in (str(c.make_url("/")).split("//", 1)[1].rstrip("/"), "bridge.example"):
            allowed = await c.post("/modal_bridge/config", headers={
                "Host": host, "X-Modal-Bridge-Capability": cap,
            }, json={"gpu_tier": "cheap"})
            assert allowed.status == 200

    harness._run(body)


def test_loopback_same_origin_also_requires_capability():
    harness._set_cfg()

    async def body(c):
        headers = {"Origin": str(c.make_url("/")).rstrip("/"),
                   "X-Modal-Bridge-Capability": ""}
        r = await c.post("/modal_bridge/config", headers=headers, json={"gpu_tier": "cheap"})
        assert r.status == 403
        assert harness.cfg_mod.load_config()["gpu_tier"] == "auto"
        headers["X-Modal-Bridge-Capability"] = "cap-secret-value"
        r = await c.post("/modal_bridge/config", headers=headers, json={"gpu_tier": "cheap"})
        assert r.status == 200

    harness._run(body)


def test_local_browser_origin_guard():
    harness._set_cfg()

    async def body(c):
        for origin in ("https://foreign.example", "null", "http://localhost:1",
                       "http://127.0.0.1:invalid", "http://user@127.0.0.1"):
            for route in harness._ROUTES:
                if not hasattr(route.handler, "__wrapped__"):
                    continue
                r = await c.request(route.method, route.path,
                                    headers={"Origin": origin, "Content-Type": "text/plain"},
                                    data="invalid-json")
                assert r.status == 403, (route.path, origin)
        r = await c.post("/modal_bridge/config", headers={"Sec-Fetch-Site": "cross-site"},
                         json={"gpu_tier": "cheap"})
        assert r.status == 403
        assert harness.cfg_mod.load_config()["gpu_tier"] == "auto"
        for headers in ({}, {"Origin": str(c.make_url("/")).rstrip("/")},
                        {"Host": "bridge.example", "Origin": "https://bridge.example",
                         "X-Modal-Bridge-Capability": "cap-secret-value"}):
            r = await c.post("/modal_bridge/config", headers=headers, json={"gpu_tier": "cheap"})
            assert r.status == 200

    harness._run(body)


@pytest.mark.parametrize("method", ["request", "download"])
def test_redirects_never_leak_key_or_change_scheme(method, monkeypatch, tmp_path):
    seen = []

    def ftp_open(_self, req):
        seen.append("ftp")
        return urllib.response.addinfourl(io.BytesIO(b'{"ok":true}'), Message(), req.full_url, 200)

    monkeypatch.setattr(urllib.request.FTPHandler, "ftp_open", ftp_open)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/final":
                seen.append(self.headers.get("X-Bridge-Key"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            self.send_response(302)
            target = {"/ftp": "ftp://127.0.0.1/test-only",
                      "/cross": f"http://127.0.0.1:{other.server_port}/final",
                      "/same": "/final"}[path]
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *_args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    server, other = servers
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    try:
        client = BridgeClient("https://test--bridge", "test-redirect-key")
        for target in ("ftp", "cross", "same"):
            seen.clear()
            url = f"http://127.0.0.1:{server.server_port}/{target}"
            monkeypatch.setattr(client, "_url", lambda _label: url)

            def call():
                if method == "request":
                    return client._req(url, None, 3, retries=0)
                return client._download_volume("job", "_outputs/job/a", tmp_path / "a", False)

            if target == "same":
                call()
                assert seen == ["test-redirect-key"]
            else:
                with pytest.raises((BridgeError, ValueError)):
                    call()
                assert not seen, "重定向目标已被访问／收到 key"
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        for thread in threads:
            thread.join()


def test_partial_download_retry_and_lost_response(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "_output_dir", lambda: tmp_path / "output")
    monkeypatch.setattr(rt.cfg_mod, "_config_path", lambda: tmp_path / "user/config.json")
    store = {"_outputs/job/first": b"first", "_outputs/job/second": b"second"}
    calls, removed = [], []
    fail = [True]

    def download(_cfg, vp, local):
        calls.append(vp)
        if vp.endswith("second") and fail[0]:
            fail[0] = False
            raise OSError("transient second-file failure")
        Path(local).write_bytes(store[vp])
        return len(store[vp])

    def remove(_cfg, vp):
        removed.append(vp)
        store.pop(vp, None)

    monkeypatch.setattr(rt.modal_volume, "volume_file_size", lambda _cfg, vp: len(store.get(vp, b"")))
    monkeypatch.setattr(rt.modal_volume, "download_volume_file", download)
    monkeypatch.setattr(rt.modal_volume, "remove_volume_path", remove)
    state = {"completed_at": 123, "images": [
        {"filename": "first.png", "volume_path": "_outputs/job/first"},
        {"filename": "second.png", "volume_path": "_outputs/job/second"}]}

    async def run():
        with pytest.raises(RuntimeError):
            await rt._write_results(state, "job", "results", {})
        assert not removed, "全部产物落盘前不能删云端副本"
        calls.clear()
        result = await rt._write_results(state, "job", "results", {})
        assert calls == ["_outputs/job/second"], "重试应复用已确认的第一个文件"
        assert len(result) == 2 and not store
        calls.clear()
        assert await rt._write_results(state, "job", "results", {}) == result
        assert not calls, "成功响应丢失后的重试不能重新请求已删的 Volume 文件"
        (tmp_path / "output/results/job/first.png").write_bytes(b"changed")
        with pytest.raises(RuntimeError):
            await rt._write_results(state, "job", "results", {})

    asyncio.run(run())


def test_duplicate_fetch_uses_one_background_task(monkeypatch):
    harness._set_cfg()

    async def run(c):
        gate, started = asyncio.Event(), asyncio.Event()
        calls = []

        async def write(*_args):
            calls.append(1)
            started.set()
            await gate.wait()
            return [{"filename": "a.png"}]

        monkeypatch.setattr(rt, "_write_results", write)

        async def request():
            response = await c.post("/modal_bridge/fetch_result", json={"job_id": "coalesced", "modal_state": {}})
            return await response.json()

        first = asyncio.create_task(request())
        await started.wait()
        second = asyncio.create_task(request())
        try:
            await asyncio.sleep(0.03)
            assert len(calls) == 1
            conflict = await c.post("/modal_bridge/fetch_result", json={
                "job_id": "coalesced", "modal_state": {"completed_at": 2}})
            assert conflict.status == 409
            monkeypatch.setattr(rt, "_FETCH_PROGRESS_MAX", 1)
            overflow = await c.post("/modal_bridge/fetch_result", json={
                "job_id": "another", "modal_state": {}})
            assert overflow.status == 429
        finally:
            gate.set()
            results = await asyncio.gather(first, second)
        assert all(r["ok"] for r in results)
        assert "coalesced" not in rt._FETCH_TASKS
        assert "coalesced" not in rt._FETCH_PROGRESS

    harness._run(run)


@pytest.mark.parametrize("failure", [False, True])
def test_cancelled_http_handler_preserves_download(monkeypatch, failure):
    """直接取消真实 route handler，覆盖启用 handler_cancellation 的服务器。"""
    harness._set_cfg()
    handler = next(r.handler.__wrapped__ for r in harness._ROUTES
                   if r.path == "/modal_bridge/fetch_result")

    class Request:
        async def json(self):
            return {"job_id": "disconnect", "modal_state": {}}

    async def run():
        gate, started = asyncio.Event(), asyncio.Event()
        calls = []

        async def write(*_args):
            calls.append(1)
            rt._fetch_progress_set("disconnect", done=1)
            started.set()
            await gate.wait()
            if failure:
                raise OSError("test failure")
            return [{"filename": "a.png"}]

        monkeypatch.setattr(rt, "_write_results", write)
        first = asyncio.create_task(handler(Request()))
        await started.wait()
        background = rt._FETCH_TASKS["disconnect"][1]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not background.done()
        assert "disconnect" in rt._FETCH_PROGRESS
        second = asyncio.create_task(handler(Request()))
        await asyncio.sleep(0)
        gate.set()
        response = await second
        assert response.status == (502 if failure else 200)
        assert calls == [1]
        assert "disconnect" not in rt._FETCH_TASKS
        assert "disconnect" not in rt._FETCH_PROGRESS

    asyncio.run(run())


def test_receipts_scope_and_symlink_invalidation(tmp_path):
    local = tmp_path / "output.png"
    local.write_bytes(b"test")
    receipts = ResultReceipts(tmp_path / "receipts", ["endpoint", "volume", "job", 1])
    receipts.record("source", local)
    assert receipts.completed_size("source", local) == 4
    assert ResultReceipts(receipts.root, ["endpoint", "volume", "job", 2]).completed_size("source", local) is None
    assert receipts.completed_size("other-source", local) is None
    saved = tmp_path / "saved.png"
    local.rename(saved)
    local.symlink_to(saved)
    assert receipts.completed_size("source", local) is None


def test_redirect_rejects_https_downgrade_and_accepts_default_port():
    handler = _SameOriginRedirect()
    req = urllib.request.Request("https://bridge.example/start")
    with pytest.raises(ValueError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://bridge.example/end")
    redirected = handler.redirect_request(req, None, 302, "Found", {}, "https://bridge.example:443/end")
    assert redirected.full_url == "https://bridge.example:443/end"
