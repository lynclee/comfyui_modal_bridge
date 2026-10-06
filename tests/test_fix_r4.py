"""codex review 0.8.59 的两条(2026-10-05)回归测试。

P1 MCP 本地提交:只凭 /submit 的来源头认「插件自己的答复」。以前任何 JSON 对象都算,MCP 与 ComfyUI 之间隔着
   反向代理时,代理回的 504 {"error": …} 被当成插件的确定答复原样返回 —— job_id 与 outcome:unknown 丢了,
   agent 重交就是双跑双计费。
P2 Registry 节点判 dirty:第三轮的「列出的 .py mtime 中位数」基准在单文件节点、两个文件改一个、多数文件被改时漏判。
   现在:列出的 .py 都不晚于 .tracking = 没动过;有晚于它的就比 Registry 原包内容,拿不到原包判改过。

约束同其它测试文件:不联网、不部署、不碰真实 Modal / Registry;会写文件的都落 tmp_path。
"""
import asyncio
import hashlib
import io
import json
import os
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from aiohttp import web

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import node_sync  # noqa: E402
import test_fix_client as _tfc  # noqa: E402
import test_fix_routes as _tfr  # noqa: E402
import test_routes as harness  # noqa: E402
from test_fix_client import WF, _load_mcp  # noqa: E402

from comfyui_modal_bridge import modal_client as mc_pkg  # noqa: E402
from comfyui_modal_bridge import routes as rt  # noqa: E402

cloud = _tfc.cloud
_isolate = _tfr._isolate      # autouse:routes 的外部副作用全部换成假的

_PLUGIN = {"X-Modal-Bridge-Origin": "plugin-submit"}


# ============================================================================
# P1 MCP 本地提交:按来源分类,不按正文格式
# ============================================================================
def test_origin_header_constants_match_between_routes_and_mcp(monkeypatch):
    m = _load_mcp(monkeypatch, {})
    assert (m._SUBMIT_ORIGIN_HEADER, m._SUBMIT_ORIGIN) == (rt._SUBMIT_ORIGIN_HEADER, rt._SUBMIT_ORIGIN)
    assert (rt._SUBMIT_ORIGIN_HEADER, rt._SUBMIT_ORIGIN) == tuple(_PLUGIN.items())[0]


def _mcp_submit(monkeypatch, cloud, behaviour):
    seen = []

    def submit(h, q, b):
        seen.append(b)
        return behaviour(h, b)
    cloud.behav["modal_bridge/submit"] = submit
    m = _load_mcp(monkeypatch, {"MODAL_BRIDGE_URL": cloud.base})
    return m.submit_workflow(json.dumps(WF)), seen


@pytest.mark.parametrize("code", [502, 503, 504, 500, 408, 429])
def test_gateway_error_with_json_body_is_unknown_and_keeps_the_job_id(cloud, monkeypatch, code):
    """codex 的复现:反向代理回 504 {"error": "upstream timeout"} —— 请求可能已经到了插件、任务已交上云端。"""
    r, seen = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(code, {"error": "upstream timeout"}))
    assert r["outcome"] == "unknown" and r["ok"] is False, r
    assert r["job_id"] == seen[0]["job_id"], "交还的必须是发出去的那个 id"
    assert "别重新提交" in r["error"]


@pytest.mark.parametrize("code,body", [(413, {"message": "request entity too large"}),
                                       (401, {"error": "proxy auth required"}),
                                       (403, b"<html>forbidden</html>")])
def test_gateway_4xx_is_a_definite_rejection_without_job_id(cloud, monkeypatch, code, body):
    """代理 / 网关的 4xx:请求没到插件,确定没提交 —— 不带 job_id(带了 agent 会去 poll 一个不存在的任务)。"""
    r, _ = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(
        code, body, "application/json" if isinstance(body, dict) else "text/html"))
    assert r["ok"] is False and "job_id" not in r and "outcome" not in r, r
    assert "没有提交" in r["error"] and f"HTTP {code}" in r["error"]
    if isinstance(body, dict):
        assert next(iter(body.values())) in r["error"], "代理给的原因要带上"


def test_redirect_is_unknown_not_a_definite_rejection(cloud, monkeypatch):
    """POST 遇到 307:urllib 不跟随、抛 HTTPError(307)。跳转说明不了请求有没有被处理,按未知。"""
    r, seen = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(
        307, {"error": "moved"}, extra={"Location": f"{cloud.base}/elsewhere"}))
    assert r["outcome"] == "unknown" and r["job_id"] == seen[0]["job_id"], r


def test_plugin_answers_are_trusted_as_is(cloud, monkeypatch):
    # 插件判定的确定失败(5xx 也一样):原样给出,不能被改成 unknown
    r, _ = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(
        500, {"error": "prepare images failed: x"}, extra=_PLUGIN))
    assert r == {"error": "prepare images failed: x"}
    # 插件的 403 带鉴权提示
    r, _ = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(
        403, {"error": "admin capability required"},
        extra={**_PLUGIN, "X-Modal-Bridge-Auth": "capability-required"}))
    assert r["error"] == "admin capability required" and "MODAL_BRIDGE_LOCAL_CONFIG" in r["hint"]


def test_success_needs_the_header_or_our_job_id(cloud, monkeypatch):
    ok = lambda h, b: h.send_json(200, {"ok": True, "job_id": b["job_id"], "gpu": "H100"})  # noqa: E731
    r, seen = _mcp_submit(monkeypatch, cloud, ok)
    assert r["ok"] is True and r["job_id"] == seen[0]["job_id"], "头被代理剥掉:job_id 对得上就认"
    # 不带头、job_id 对不上的 200 JSON(网关 / 登录页之类):不能当提交成功
    r, seen = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(200, {"status": "ok"}))
    assert r["outcome"] == "unknown" and r["job_id"] == seen[0]["job_id"], r
    # 带头的照常
    r, _ = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(
        200, {"ok": True, "job_id": b["job_id"]}, extra=_PLUGIN))
    assert r["ok"] is True


# ── 插件这一侧:/submit 的每个答复都带来源头 ──
def _post_submit(body=None, raw=None, headers=None):
    async def go(c):
        harness._set_cfg(gpu_tier="primary")
        kw = {"data": raw} if raw is not None else {"json": body}
        r = await c.post("/modal_bridge/submit", headers=headers or {}, **kw)
        return r.status, r.headers.get("X-Modal-Bridge-Origin")
    return harness._run(go)


def test_submit_route_tags_every_answer(monkeypatch):
    async def ok(session, cfg, **kw):
        return {"id": kw["job_id"], "gpu": "H100"}
    monkeypatch.setattr(mc_pkg, "submit_job", ok)
    assert _post_submit({"prompt": {}, "local_nodes": {}, "job_id": "j-1"}) == (200, "plugin-submit")
    # 处理函数自己的 400、_admin_only 收口的 400(不是 JSON)、跨站 403、submit_job 的 502
    assert _post_submit({"prompt": "x"}) == (400, "plugin-submit")
    assert _post_submit(raw=b"{not json") == (400, "plugin-submit")
    assert _post_submit({"prompt": {}}, headers={"Origin": "https://evil.example"}) == (403, "plugin-submit")

    async def boom(session, cfg, **kw):
        raise RuntimeError("Modal /run 401")
    monkeypatch.setattr(mc_pkg, "submit_job", boom)
    assert _post_submit({"prompt": {}, "local_nodes": {}}) == (502, "plugin-submit")


def test_mcp_against_the_real_submit_route(monkeypatch):
    """端到端:真的 routes 应用跑在后台线程,MCP 用 urllib 打它。插件判定的 502(确定失败)要原样到 agent 手里。"""
    seen = []

    async def boom(session, cfg, **kw):
        seen.append(kw["job_id"])
        raise RuntimeError("Modal /run 401 — bridge key 不对")
    monkeypatch.setattr(mc_pkg, "submit_job", boom)
    harness._set_cfg(gpu_tier="primary")
    monkeypatch.setattr(rt.node_sync, "plan_node_sync", lambda p: {})

    loop = asyncio.new_event_loop()
    started, port = threading.Event(), []

    async def start():
        app = web.Application()
        app.add_routes(harness._ROUTES)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port.append(site._server.sockets[0].getsockname()[1])
        started.set()
        return runner
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    runner = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    try:
        assert started.wait(5)
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        m = _load_mcp(monkeypatch, {"MODAL_BRIDGE_URL": f"http://127.0.0.1:{port[0]}"})
        r = m.submit_workflow(json.dumps(WF))
        assert r == {"error": "Modal /run 401 — bridge key 不对"}, r
        assert seen and bool(m._safe_job_id(seen[0]))
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)


# ============================================================================
# P2 Registry 节点判 dirty:.tracking 时间 + Registry 原包内容
# ============================================================================
REF = ("comfyui-x", "1.0.0")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _node(tmp_path, files: dict, *, tracking_age_s=0.0) -> Path:
    """模拟 Manager 装好的节点:先解包(文件 mtime = 解包时刻),再写 .tracking。返回目录。"""
    d = tmp_path / "node"
    d.mkdir()
    t = time.time() - 86400
    for rel, text in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        os.utime(f, (t, t))
    (d / ".tracking").write_text("\n".join(files), encoding="utf-8")
    tt = t + 1 - tracking_age_s
    os.utime(d / ".tracking", (tt, tt))
    return d


def _edit(d: Path, rel: str, text: str, *, after_s=3600.0):
    f = d / rel
    f.write_text(text, encoding="utf-8")
    t = (d / ".tracking").stat().st_mtime + after_s
    os.utime(f, (t, t))


class _Registry:
    """假的原包下载:记次数,按给定内容回哈希;offline=True 时抛错。"""
    def __init__(self, files: dict, offline=False):
        self.hashes = {rel: _sha(text) for rel, text in files.items() if rel.endswith(".py")}
        self.offline, self.calls = offline, []

    def __call__(self, cid, ver):
        self.calls.append((cid, ver))
        if self.offline:
            raise OSError("offline (test)")
        return dict(self.hashes)


@pytest.fixture
def registry(monkeypatch):
    def install(files, offline=False):
        reg = _Registry(files, offline)
        monkeypatch.setattr(node_sync, "_download_cnr_py_hashes", reg)
        return reg
    return install


@pytest.mark.parametrize("files,edited", [
    ({"__init__.py": "A = 1\n"}, ["__init__.py"]),                                   # 单文件节点
    ({"__init__.py": "A = 1\n", "nodes.py": "B = 1\n"}, ["nodes.py"]),               # 两个文件改一个
    ({"a.py": "1", "b.py": "2", "c.py": "3", "d.py": "4"}, ["a.py", "b.py", "c.py"]),  # 多数文件被改
    ({"a.py": "1", "b.py": "2", "c.py": "3", "d.py": "4"}, ["a.py", "b.py", "c.py", "d.py"]),  # 全部被改
])
def test_edits_the_median_rule_missed_are_detected(tmp_path, registry, files, edited):
    """codex 的反例:中位数落在改过的时间上。现在比原包内容,改了就是改了。"""
    reg = registry(files)
    d = _node(tmp_path, files)
    for rel in edited:
        _edit(d, rel, files[rel] + "# 本机改过\n")
    assert node_sync.cnr_dirty(d, REF) is True
    assert reg.calls == [REF]


def test_edit_shortly_after_install_is_detected(tmp_path, registry):
    """以前给了 10 分钟容差:装完马上改的代码会被当成解包本身的时间差。容差现在只罩 mtime 精度。"""
    files = {"__init__.py": "A = 1\n", "nodes.py": "B = 1\n"}
    registry(files)
    d = _node(tmp_path, files)
    _edit(d, "nodes.py", "B = 2\n", after_s=30)
    assert node_sync.cnr_dirty(d, REF) is True


def test_untouched_node_is_clean_without_any_download(tmp_path, registry):
    files = {"__init__.py": "A = 1\n", "sub/x.py": "X\n", "conf.json": "{}"}
    reg = registry(files)
    d = _node(tmp_path, files)
    later = time.time()
    os.utime(d / "conf.json", (later, later))       # 运行时改自己的数据文件:不算
    assert node_sync.cnr_dirty(d, REF) is False
    assert reg.calls == [], "列出的 .py 都不晚于 .tracking 时不该联网"


def test_copy_without_timestamps_is_clean_and_the_baseline_is_cached(tmp_path, registry):
    """不保留时间戳的拷贝:.tracking 先落盘,所有文件都比它新 —— 内容与原包一致就是干净的。"""
    files = {"__init__.py": "A = 1\n", "nodes.py": "B = 1\n", "sub/x.py": "X\n"}
    reg = registry(files)
    d = _node(tmp_path, files, tracking_age_s=7200)
    assert node_sync.cnr_dirty(d, REF) is False
    assert node_sync.cnr_dirty(d, REF) is False
    assert reg.calls == [REF], "同一版本只下载一次"
    cached = list((tmp_path / "_cnr_baseline_cache").glob("*.json"))
    assert [p.name for p in cached] == ["comfyui-x@1.0.0.json"]
    # 缓存在磁盘上:换一个进程(这里清掉内存里的一切)也不用再下载
    assert json.loads(cached[0].read_text(encoding="utf-8"))["py"] == reg.hashes


def test_offline_is_conservative_and_does_not_retry_every_call(tmp_path, registry):
    files = {"__init__.py": "A = 1\n"}
    reg = registry(files, offline=True)
    d = _node(tmp_path, files, tracking_age_s=7200)   # 时间分不清
    assert node_sync.cnr_dirty(d, REF) is True, "拿不到原包:宁可判改过"
    assert node_sync.cnr_dirty(d, REF) is True
    assert reg.calls == [REF], "失败后 5 分钟内不再重试(一次预检会多次判同一个节点)"
    assert not (tmp_path / "_cnr_baseline_cache").exists(), "失败不能写进磁盘缓存"
    # 恢复联网、过了重试间隔:重新下载并判干净
    reg.offline = False
    node_sync._CNR_BASELINE_FAILED.clear()
    assert node_sync.cnr_dirty(d, REF) is False


def test_file_missing_from_the_baseline_is_dirty(tmp_path, registry):
    files = {"__init__.py": "A = 1\n", "nodes.py": "B = 1\n"}
    reg = registry(files)
    reg.hashes.pop("nodes.py")
    d = _node(tmp_path, files, tracking_age_s=7200)
    assert node_sync.cnr_dirty(d, REF) is True


def test_added_or_deleted_py_needs_no_download(tmp_path, registry):
    files = {"__init__.py": "A = 1\n", "nodes.py": "B = 1\n"}
    reg = registry(files)
    d = _node(tmp_path, files)
    (d / "patch.py").write_text("P\n", encoding="utf-8")
    assert node_sync.cnr_dirty(d, REF) is True
    (d / "patch.py").unlink()
    (d / "nodes.py").unlink()
    assert node_sync.cnr_dirty(d, REF) is True
    assert reg.calls == []


def test_cnr_ref_is_read_from_pyproject_when_not_given(tmp_path, registry):
    files = {"__init__.py": "A = 1\n", "pyproject.toml": '[project]\nname = "ComfyUI-X"\nversion = "1.0.0"\n'}
    reg = registry(files)
    d = _node(tmp_path, files, tracking_age_s=7200)
    assert node_sync.cnr_dirty(d) is False and reg.calls == [REF]


def test_invalid_ref_never_downloads(registry):
    reg = registry({})
    assert node_sync.cnr_baseline("../x", "1.0") is None
    assert node_sync.cnr_baseline("x", "1.0/../../y") is None
    assert reg.calls == []


# ── 下载与解包本身(urlopen 打桩,不出网) ──
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            if name.endswith("/"):
                z.writestr(zipfile.ZipInfo(name), b"")
            else:
                z.writestr(name, data)
    return buf.getvalue()


def _fake_urlopen(monkeypatch, meta, zip_bytes):
    urls = []

    def urlopen(url, timeout=None):
        urls.append(url)
        if url.startswith("https://api.comfy.org/"):
            return _Resp(json.dumps(meta).encode())
        return _Resp(zip_bytes)
    monkeypatch.setattr(node_sync.urllib.request, "urlopen", urlopen)
    return urls


def test_download_hashes_only_the_py_members(monkeypatch):
    z = _zip_bytes({"__init__.py": b"A = 1\n", "sub/": b"", "sub/x.py": b"X\n", "README.md": b"r",
                    "web/app.js": b"js"})
    urls = _fake_urlopen(monkeypatch, {"downloadUrl": "https://cdn.comfy.org/a/x/1.0.0/node.zip"}, z)
    got = node_sync._download_cnr_py_hashes(*REF)
    assert got == {"__init__.py": _sha("A = 1\n"), "sub/x.py": _sha("X\n")}
    assert urls == ["https://api.comfy.org/nodes/comfyui-x/versions/1.0.0",
                    "https://cdn.comfy.org/a/x/1.0.0/node.zip"]


@pytest.mark.parametrize("meta", [{"downloadUrl": "http://cdn.comfy.org/x.zip"}, {}, ["not", "a", "dict"]])
def test_download_refuses_missing_or_plain_http_url(monkeypatch, meta):
    _fake_urlopen(monkeypatch, meta, _zip_bytes({"a.py": b"1"}))
    with pytest.raises(ValueError):
        node_sync._download_cnr_py_hashes(*REF)


def test_download_is_capped(monkeypatch):
    _fake_urlopen(monkeypatch, {"downloadUrl": "https://cdn.comfy.org/x.zip"}, b"\0" * (3 << 20))
    monkeypatch.setattr(node_sync, "_CNR_ZIP_MAX_BYTES", 2 << 20)
    with pytest.raises(ValueError, match="上限"):
        node_sync._download_cnr_py_hashes(*REF)


# ── 测试护栏本身(conftest._no_real_registry_download) ──
def test_registry_guard_blocks_even_modules_it_never_patched(_no_real_registry_download):
    """护栏拦在 urllib 的 OpenerDirector 上:测试中途另行加载 / reload 出来的 node_sync 也出不了网。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("node_sync_fresh_copy", ROOT / "node_sync.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    with pytest.raises(pytest.fail.Exception):
        fresh.cnr_baseline(*REF)            # 内部 except Exception 吞不掉 BaseException
    assert _no_real_registry_download and "api.comfy.org" in _no_real_registry_download[0]
    _no_real_registry_download.clear()      # 故意触发的,清掉;被吞掉的情况由 teardown 判


def test_registry_guard_lets_local_urllib_traffic_through(cloud, _no_real_registry_download):
    import urllib.request
    cloud.behav["ping"] = lambda h, q, b: h.send_json(200, {"ok": True})
    with urllib.request.urlopen(f"{cloud.base}/ping", timeout=5) as r:
        assert json.loads(r.read()) == {"ok": True}
    assert _no_real_registry_download == []


# ============================================================================
# 第四轮复核(独立 reviewer)
# ============================================================================
def test_once_downloaded_every_listed_py_is_compared(tmp_path, registry):
    """慢速、不保留时间戳的拷贝:先拷的改过的文件不晚于 .tracking,后拷的晚于它。以前只比「晚于它的」,改动漏掉。"""
    files = {"__init__.py": "A = 1\n", "nodes.py": "B = 1\n", "z_later.py": "Z\n"}
    reg = registry(files)
    d = _node(tmp_path, files)
    (d / "nodes.py").write_text("B = 2  # 本机改过\n", encoding="utf-8")
    t = (d / ".tracking").stat().st_mtime
    os.utime(d / "nodes.py", (t - 1, t - 1))          # 先拷:不晚于 .tracking
    os.utime(d / "z_later.py", (t + 10, t + 10))      # 后拷:晚于它
    assert node_sync.cnr_dirty(d, REF) is True
    assert reg.calls == [REF]


def test_old_plugin_403_keeps_the_pairing_hint(cloud, monkeypatch):
    """没重启 ComfyUI 的旧插件不带来源头,但 403 配对带 X-Modal-Bridge-Auth:仍是插件的答复,提示要保留。"""
    r, _ = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(
        403, {"error": "admin capability required"}, extra={"X-Modal-Bridge-Auth": "capability-required"}))
    assert r["error"] == "admin capability required" and "MODAL_BRIDGE_LOCAL_CONFIG" in r["hint"], r


def test_headerless_4xx_wording_does_not_blame_only_the_proxy(cloud, monkeypatch):
    r, _ = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(400, {"error": "prompt (object) required"}))
    assert r["ok"] is False and "job_id" not in r
    assert "没重启" in r["error"] and "prompt (object) required" in r["error"], r


def test_a_slow_download_does_not_block_other_nodes(tmp_path, monkeypatch):
    """锁按 id@version 分:一个节点在下载时,别的节点(缓存命中)不用陪着等;同一个节点并发只下一次。"""
    gate, started, calls = threading.Event(), threading.Event(), []

    def slow(cid, ver):
        calls.append((cid, ver))
        started.set()
        assert gate.wait(10)
        return {"a.py": "x"}
    monkeypatch.setattr(node_sync, "_download_cnr_py_hashes", slow)
    cache = tmp_path / "_cnr_baseline_cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "other@2.0.0.json").write_text(json.dumps({"py": {"b.py": "y"}}), encoding="utf-8")
    out = []
    th = [threading.Thread(target=lambda: out.append(node_sync.cnr_baseline("slow", "1.0.0"))) for _ in range(2)]
    th[0].start()
    assert started.wait(5)
    th[1].start()
    t0 = time.monotonic()
    assert node_sync.cnr_baseline("other", "2.0.0") == {"b.py": "y"}
    assert time.monotonic() - t0 < 1.0, "缓存命中被别的节点的下载挡住了"
    gate.set()
    for t in th:
        t.join(10)
    assert out == [{"a.py": "x"}] * 2 and calls == [("slow", "1.0.0")]


def test_download_has_a_total_deadline(monkeypatch):
    class Slow(_Resp):
        left = 30                              # 有限长(约 1.5s):总时限失效时测试失败而不是挂住

        def read(self, n=-1):
            time.sleep(0.05)
            Slow.left -= 1
            return b"\0" * 1024 if Slow.left > 0 else b""
        read1 = read
    meta = json.dumps({"downloadUrl": "https://cdn.comfy.org/x.zip"}).encode()
    monkeypatch.setattr(node_sync.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(meta) if "api.comfy.org" in url else Slow())
    monkeypatch.setattr(node_sync, "_CNR_FETCH_DEADLINE_S", 0.3)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        node_sync._download_cnr_py_hashes(*REF)
    assert time.monotonic() - t0 < 2


def test_baseline_is_kept_in_memory_when_the_disk_cache_is_unwritable(tmp_path, monkeypatch, registry):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(node_sync, "_cnr_cache_dir", lambda: blocker / "cache")   # mkdir 必然失败
    reg = registry({"a.py": "1"})
    assert node_sync.cnr_baseline(*REF) == reg.hashes
    assert node_sync.cnr_baseline(*REF) == reg.hashes
    assert reg.calls == [REF], "写不进磁盘也不能每次重下"


def test_disk_cache_survives_a_fresh_process(tmp_path, registry):
    reg = registry({"a.py": "1"})
    assert node_sync.cnr_baseline(*REF) == reg.hashes
    node_sync._CNR_BASELINE_MEM.clear()                # 模拟重启:内存清空,只剩磁盘缓存
    assert node_sync.cnr_baseline(*REF) == reg.hashes and reg.calls == [REF]


# ============================================================================
# 第五轮复核
# ============================================================================
def test_mcp_docstring_quotes_the_real_submit_timeout(monkeypatch):
    """agent 与配 MCP 的人照 docstring 配客户端超时:写的数字必须与常量一致(改过常量、文档没跟上,复核抓到过)。"""
    m = _load_mcp(monkeypatch, {})
    doc = m.submit_workflow.__doc__
    assert f"{m._SUBMIT_TIMEOUT_S}s" in doc and "330" not in doc, doc


def test_gateway_unknown_tells_the_agent_to_wait_longer(cloud, monkeypatch):
    r, _ = _mcp_submit(monkeypatch, cloud, lambda h, b: h.send_json(504, {"error": "upstream timeout"}))
    assert "9 分钟" in r["error"] and "还在重试" in r["error"], r


class _Trickle(BaseHTTPRequestHandler):
    """声称很大、每 20ms 只吐 512 字节:模拟慢速链路。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(64 << 20))
        self.end_headers()
        try:
            for _ in range(5000):
                self.wfile.write(b"\0" * 512)
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass


def test_download_deadline_holds_on_a_slow_real_socket(monkeypatch):
    """read(1 MiB) 会攒满 1 MiB 才返回,总时限在慢速链路上形同虚设(第五轮复核实测越过近 10 倍)。"""
    import urllib.request as ur
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    real = ur.urlopen
    meta = json.dumps({"downloadUrl": "https://cdn.comfy.org/x.zip"}).encode()
    monkeypatch.setattr(node_sync.urllib.request, "urlopen", lambda url, timeout=None: (
        _Resp(meta) if "api.comfy.org" in url
        else real(f"http://127.0.0.1:{srv.server_port}/x.zip", timeout=timeout)))
    monkeypatch.setattr(node_sync, "_CNR_FETCH_DEADLINE_S", 0.5)
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            node_sync._download_cnr_py_hashes(*REF)
        assert time.monotonic() - t0 < 3, f"越过总时限 {time.monotonic() - t0:.1f}s"
    finally:
        srv.shutdown()
        srv.server_close()
