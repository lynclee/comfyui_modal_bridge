"""fix1005 云端运行时修复的回归测试(2026-10-05 深度 review 的 K1–K19 + C12)。

modal_app.py 模块级就 `modal.Dict.from_name()`、`modal.Volume.from_name()`,CI 也没装 modal / requests /
websocket。这里用假的 modal / fastapi / modal_image 把 **真的** modal_app.py 整个 import 进来(独立模块名,
import 完立刻恢复 sys.modules,不影响别的测试),再按用例替换 job_state / models_vol / 子进程这些外部依赖。
这些 bug 大多是执行顺序问题,源码字符串断言看不出来,必须真跑。
"""
import copy
import importlib
import importlib.util
import json
import os
import sys
import time
import types
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MA_DIR = ROOT / "modal_app"
if str(MA_DIR) not in sys.path:
    sys.path.insert(0, str(MA_DIR))


# ── 依赖桩 ──────────────────────────────────────────────────────────────────
def _ensure_net_stubs():
    """CI 没装 requests / websocket:放空模块桩(装了就用真的)。和 test_core._comfy_ws 同一个套路,
    另外补上 _comfy_ws 的 except 子句会引用到的异常类,桩上没有的话异常路径会变成 AttributeError。"""
    for name in ("requests", "websocket"):
        if name not in sys.modules:
            try:
                importlib.import_module(name)
            except ImportError:
                sys.modules[name] = types.ModuleType(name)
    ws = sys.modules["websocket"]
    for exc in ("WebSocketTimeoutException", "WebSocketConnectionClosedException", "WebSocketException"):
        if not hasattr(ws, exc):
            setattr(ws, exc, type(exc, (Exception,), {}))


_ensure_net_stubs()
import _comfy_ws as cw            # noqa: E402
import _local_nodes_boot as lnb   # noqa: E402
import aigc_delivery as ad        # noqa: E402


class FakeDict:
    """近似 modal.Dict:读写都过一遍深拷贝(序列化语义),支持 put(skip_if_exists)。"""

    def __init__(self, init=None):
        self.d = copy.deepcopy(init or {})

    def get(self, k, default=None):
        return copy.deepcopy(self.d.get(k, default))

    def put(self, k, v, skip_if_exists=False):
        if skip_if_exists and k in self.d:
            return False
        self.d[k] = copy.deepcopy(v)
        return True

    def __setitem__(self, k, v):
        self.put(k, v)

    def __getitem__(self, k):
        return copy.deepcopy(self.d[k])

    def __delitem__(self, k):
        del self.d[k]

    def __contains__(self, k):
        return k in self.d

    def items(self):
        return iter([(k, copy.deepcopy(v)) for k, v in self.d.items()])


class FakeVolume:
    def __init__(self):
        self.calls = []
        self.removed = []
        self.reload_hook = None
        self.entries = {}

    def reload(self):
        self.calls.append("reload")
        if self.reload_hook:
            self.reload_hook()

    def commit(self):
        self.calls.append("commit")

    def remove_file(self, p, recursive=False):
        self.removed.append(p)

    def listdir(self, path, recursive=False):
        self.calls.append(("listdir", path))
        return list(self.entries.get(path, []))


def _fake_modal():
    m = types.ModuleType("modal")

    class App:
        def __init__(self, name):
            self.name = name

        def cls(self, **kw):
            return lambda c: c

        def function(self, **kw):
            return lambda f: f

        def local_entrypoint(self):
            return lambda f: f

    m.App = App
    m.Volume = types.SimpleNamespace(from_name=lambda *a, **k: FakeVolume())
    m.Secret = types.SimpleNamespace(from_name=lambda *a, **k: object(), from_dict=lambda d: object())
    m.Dict = types.SimpleNamespace(from_name=lambda *a, **k: FakeDict())
    m.concurrent = lambda **kw: (lambda c: c)
    m.enter = lambda snap=False: (lambda f: f)
    m.exit = lambda: (lambda f: f)
    m.method = lambda: (lambda f: f)
    m.fastapi_endpoint = lambda **kw: (lambda f: f)
    m.FunctionCall = types.SimpleNamespace(from_id=lambda cid: types.SimpleNamespace(cancel=lambda: None))
    m.current_function_call_id = lambda: "fc-self"
    m.Cls = types.SimpleNamespace(from_name=lambda *a, **k: None)
    return m


_LOADED = {}


def _load_modal_app():
    """import 真的 modal_app.py(假 modal 只在 import 期间挂在 sys.modules 上,随后原样恢复)。"""
    if "ma" in _LOADED:
        return _LOADED["ma"]
    keys = ("modal", "modal.experimental", "fastapi", "fastapi.responses", "modal_image")
    saved = {k: sys.modules.get(k) for k in keys}
    try:
        sys.modules["modal"] = _fake_modal()
        sys.modules.pop("modal.experimental", None)
        fa = types.ModuleType("fastapi")
        fa.Header = lambda default=None, **kw: default
        sys.modules["fastapi"] = fa
        sys.modules.pop("fastapi.responses", None)
        mi = types.ModuleType("modal_image")
        mi.cuda_image = None
        sys.modules["modal_image"] = mi
        spec = importlib.util.spec_from_file_location("_modal_app_fix_cloud", MA_DIR / "modal_app.py")
        ma = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ma)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    _LOADED["ma"] = ma
    return ma


@pytest.fixture
def ma(monkeypatch):
    """每个用例一份干净的外部状态:新的 job_state / Volume / modal 句柄,print 静音。"""
    m = _load_modal_app()
    js, vol = FakeDict(), FakeVolume()
    cancels = []
    fake_modal = types.SimpleNamespace(
        FunctionCall=types.SimpleNamespace(
            from_id=lambda cid: types.SimpleNamespace(cancel=lambda: cancels.append(cid))),
        current_function_call_id=lambda: "fc-self")
    monkeypatch.setattr(m, "job_state", js)
    monkeypatch.setattr(m, "models_vol", vol)
    monkeypatch.setattr(m, "modal", fake_modal)
    monkeypatch.setattr(m, "_check", lambda key: None)
    monkeypatch.setattr(m, "_LOADED_LOCAL_DIGESTS", None)
    monkeypatch.setattr(m, "_last_sweep", [0.0])
    monkeypatch.setattr(m, "_last_orphan_sweep", [0.0])
    monkeypatch.setattr(m, "_gpu_name", lambda: "NVIDIA H100")
    monkeypatch.setattr(m, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    m._t = types.SimpleNamespace(js=js, vol=vol, cancels=cancels)
    return m


class Proc:
    """假 ComfyUI 子进程。"""

    def __init__(self, rc=None):
        self.rc = rc

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = 0

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        self.rc = -9


def _worker(ma, proc=None):
    inst = ma.ComfyWorker()
    inst.proc = proc if proc is not None else Proc()
    inst._cpu = False
    return inst


# ============================================================================
# K1 — /prompt 回 2xx 但带 node_errors(部分分支被剔除)
# ============================================================================
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _partial(pid, typ="value_not_in_list"):
    return {"prompt_id": pid, "number": 3, "node_errors": {
        "31": {"errors": [{"type": typ, "message": "Value not in list",
                           "details": "lora_name: 'new.safetensors' not in [...]", "extra_info": {}}],
               "dependent_outputs": ["40"], "class_type": "LoraLoader"}}}


def _patch_comfy_http(monkeypatch, prompt_responses):
    """接管 _comfy_ws 的 HTTP:/prompt 依次回 prompt_responses,其余 POST 记录下来回 200。"""
    calls = []

    def post(url, data=None, json=None, headers=None, timeout=None, **kw):
        path = url.split("8188", 1)[-1]
        calls.append((path, json))
        if path == "/prompt":
            return prompt_responses.pop(0)
        return _Resp(200, {})
    monkeypatch.setattr(cw, "requests", types.SimpleNamespace(post=post, get=None))
    monkeypatch.setattr(cw, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    reloads = []
    monkeypatch.setattr(cw, "_reload_volume_in_worker", lambda: reloads.append(1))
    return calls, reloads


def test_k1_partial_2xx_is_withdrawn_and_retried_like_400(monkeypatch):
    """v0.37.2 只要还有一个输出合法就回 200、只把合法分支入队,被剔除的写在 node_errors 里。
    以前只看状态码 → 缺一个分支照样 completed;value_not_in_list 的 reload 重试也被绕过。"""
    ok = {"prompt_id": "p-2", "number": 4, "node_errors": {}}
    calls, reloads = _patch_comfy_http(monkeypatch, [_Resp(200, _partial("p-1")), _Resp(200, ok)])
    out = cw.queue_workflow({"9": {}}, "cid")
    assert out == ok
    assert reloads == [1], "value_not_in_list 必须走 reload 重试"
    # 撤回残缺的 p-1:先从队列删,再按 prompt_id 定向中断;都在重新提交之前
    assert calls[1:3] == [("/queue", {"delete": ["p-1"]}), ("/interrupt", {"prompt_id": "p-1"})], calls
    assert [c[0] for c in calls] == ["/prompt", "/queue", "/interrupt", "/prompt"]


def test_k1_partial_2xx_other_errors_run_the_rest_without_retry(monkeypatch):
    """2026-10-05 复核 r2 改了行为:非缺模型类的部分校验错误(悬空的输出节点等)不撤回、不失败,
    其余分支照常跑,被剔除的输出走契约 D1 的 warnings(详见 test_fix_r2_cloud.py)。"""
    resp = _partial("p-1", "required_input_missing")
    calls, reloads = _patch_comfy_http(monkeypatch, [_Resp(200, resp)])
    assert cw.queue_workflow({"9": {}}, "cid") == resp
    assert reloads == [], "非模型缺失类错误不重试"
    assert [c[0] for c in calls] == ["/prompt"], "不能撤回合法分支"


def test_k1_partial_2xx_retries_exhausted_raises(monkeypatch):
    resps = [_Resp(200, _partial(f"p-{i}")) for i in range(cw._RETRY_MAX + 1)]
    calls, reloads = _patch_comfy_http(monkeypatch, resps)
    with pytest.raises(cw.ValidationError):
        cw.queue_workflow({"9": {}}, "cid")
    assert len(reloads) == cw._RETRY_MAX
    withdrawn = [b["delete"][0] for p, b in calls if p == "/queue"]
    assert withdrawn == [f"p-{i}" for i in range(cw._RETRY_MAX + 1)], "每个残缺 prompt 都要撤回"


def test_k1_clean_2xx_and_400_unchanged(monkeypatch):
    ok = {"prompt_id": "p-1", "number": 1, "node_errors": {}}
    calls, _ = _patch_comfy_http(monkeypatch, [_Resp(200, ok)])
    assert cw.queue_workflow({}, "c") == ok and [c[0] for c in calls] == ["/prompt"], "干净的 2xx 不能多发请求"
    bad = {"error": {"type": "prompt_outputs_failed_validation"},
           "node_errors": {"5": {"errors": [{"type": "required_input_missing", "details": "x"}]}}}
    _patch_comfy_http(monkeypatch, [_Resp(400, bad)])
    with pytest.raises(ValueError) as ei:
        cw.queue_workflow({}, "c")
    assert str(ei.value).startswith("Workflow validation: "), "400 的文案保持原样"


# ============================================================================
# K2 — execution_interrupted 不能被当成完成;WS 与 history 结论一致
# ============================================================================
PID = "p-ws"


def _run_with_ws(monkeypatch, msgs, history):
    class WS:
        connected = True

        def connect(self, url, timeout=None):
            pass

        def recv(self):
            return json.dumps(msgs.pop(0))

        def close(self):
            pass
    monkeypatch.setattr(cw.websocket, "WebSocket", WS, raising=False)
    monkeypatch.setattr(cw, "queue_workflow", lambda wf, cid: {"prompt_id": PID})
    monkeypatch.setattr(cw, "get_history", lambda pid: history)
    monkeypatch.setattr(cw, "get_image_data", lambda *a: b"\x89PNG partial")
    return cw.run_workflow({"x": 1}, job_id="job1")


_PARTIAL_OUT = {"9": {"images": [{"filename": "a_00001_.png", "subfolder": "", "type": "output"}]}}
_INTERRUPTED_HIST = {PID: {"outputs": _PARTIAL_OUT, "status": {
    "status_str": "error", "completed": False,
    "messages": [["execution_start", {}],
                 ["execution_interrupted", {"node_id": "12", "node_type": "VAEDecodeTiled"}]]}}}


def test_k2_ws_interrupted_is_a_failure(monkeypatch):
    """v0.37.2:interrupt 时先广播 execution_interrupted,随后照样推 executing node=None。"""
    msgs = [{"type": "executing", "data": {"node": "3", "prompt_id": PID}},
            {"type": "execution_interrupted",
             "data": {"prompt_id": PID, "node_id": "12", "node_type": "VAEDecodeTiled"}},
            {"type": "executing", "data": {"node": None, "prompt_id": PID}}]
    with pytest.raises(RuntimeError, match="中断") as ei:
        _run_with_ws(monkeypatch, msgs, _INTERRUPTED_HIST)
    assert "(from history)" not in str(ei.value), "WS 分支自己就该认出中断,不能只靠 history 兜底"


def test_k2_ws_done_but_history_says_error_is_a_failure(monkeypatch):
    """WS 上只看到完成事件(中断事件丢了 / 被别的分支吞了),终态以 history 为准。"""
    msgs = [{"type": "executing", "data": {"node": None, "prompt_id": PID}}]
    with pytest.raises(RuntimeError, match="中断"):
        _run_with_ws(monkeypatch, msgs, _INTERRUPTED_HIST)


def test_k2_history_success_with_error_message_is_a_failure(monkeypatch):
    """execution.py 排程阶段出错只发 execution_error、不把 success 置 False —— 两条路径都要判失败。"""
    hist = {PID: {"outputs": _PARTIAL_OUT, "status": {
        "status_str": "success", "completed": True,
        "messages": [["execution_error", {"node_id": "7", "node_type": "X", "exception_message": "boom"}]]}}}
    msgs = [{"type": "executing", "data": {"node": None, "prompt_id": PID}}]
    with pytest.raises(RuntimeError, match="boom"):
        _run_with_ws(monkeypatch, msgs, hist)
    monkeypatch.setattr(cw, "get_history", lambda pid: hist)
    done, errs = cw._history_settled(PID)
    assert done and errs and "boom" in errs[0]


def test_k2_ws_and_history_paths_agree_on_interrupt(monkeypatch):
    monkeypatch.setattr(cw, "get_history", lambda pid: _INTERRUPTED_HIST)
    done, errs = cw._history_settled(PID)
    assert done and errs and "中断" in errs[0]
    assert cw._history_verdict(_INTERRUPTED_HIST[PID]) == (done, errs)


def test_k2_normal_success_still_returns_outputs_with_size_bytes(monkeypatch):
    hist = {PID: {"outputs": _PARTIAL_OUT, "status": {"status_str": "success", "completed": True,
                                                      "messages": [["execution_success", {}]]}}}
    msgs = [{"type": "executing", "data": {"node": None, "prompt_id": PID}}]
    r = _run_with_ws(monkeypatch, msgs, hist)
    assert [i["filename"] for i in r["images"]] == ["a_00001_.png"]
    assert r["images"][0]["size_bytes"] == len(b"\x89PNG partial")   # C12


# ============================================================================
# C12 — images[] 每项都带 size_bytes(base64 与 volume_path 两种)
# ============================================================================
def test_c12_every_image_record_has_raw_size(monkeypatch):
    blobs = {"small.png": b"s" * 10, "big.mp4": b"b" * 100}
    monkeypatch.setattr(cw, "get_image_data", lambda fn, sub, typ: blobs[fn])
    monkeypatch.setattr(cw, "_VOL_THRESHOLD", 50)
    written = {}

    class _F:
        def __init__(self, p):
            self.p = p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            written[self.p] = b
    # 大文件会写 /comfy-volume(测试机上没有):只替换这个模块里的 open / os,不碰全局
    monkeypatch.setattr(cw, "open", lambda p, mode="r": _F(p), raising=False)
    monkeypatch.setattr(cw, "os", types.SimpleNamespace(makedirs=lambda *a, **k: None, path=os.path,
                                                        environ=os.environ))
    refs = [{"filename": f, "subfolder": "", "type": "output", "node_id": "9", "key": "images"}
            for f in blobs]
    images, errs = cw.materialize_desktop_outputs(refs, "job1")
    assert not errs
    by = {i["filename"]: i for i in images}
    assert "data_base64" in by["small.png"] and "volume_path" in by["big.mp4"]
    assert by["small.png"]["size_bytes"] == 10 and by["big.mp4"]["size_bytes"] == 100
    assert len(written) == 1


# ============================================================================
# K3 — 暖容器探活 / 原地重启 / 退役
# ============================================================================
def _healthy_http(monkeypatch, ok=True):
    def get(url, timeout=None):
        if not ok:
            raise ConnectionError("refused")
        return types.SimpleNamespace(ok=True, status_code=200)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=get))


def test_k3_dead_comfy_is_restarted_before_the_job(ma, monkeypatch):
    _healthy_http(monkeypatch)
    order = []
    monkeypatch.setattr(ma, "_worker_shutdown", lambda self: order.append("shutdown"))
    monkeypatch.setattr(ma, "_worker_boot", lambda self, **kw: (order.append("boot"),
                                                                setattr(self, "proc", Proc())))
    monkeypatch.setattr(ma, "_worker_run", lambda *a: order.append("run") or {"ok": 1})
    inst = _worker(ma, Proc(rc=-9))      # 上一单里被 OOM kill
    ma._t.js["j"] = {"status": "queued"}
    assert inst.run({"1": {}}, "j") == {"ok": 1}
    assert order == ["shutdown", "boot", "run"], order


def test_k3_healthy_comfy_is_not_restarted(ma, monkeypatch):
    _healthy_http(monkeypatch)
    order = []
    monkeypatch.setattr(ma, "_worker_boot", lambda self, **kw: order.append("boot"))
    monkeypatch.setattr(ma, "_worker_run", lambda *a: order.append("run") or {})
    _worker(ma).run({"1": {}}, "j")
    assert order == ["run"], "健康的暖容器不能重启(会丢掉显存里的模型)"


def test_k3_unreachable_http_with_live_process_is_restarted(ma, monkeypatch):
    _healthy_http(monkeypatch, ok=False)
    order = []
    monkeypatch.setattr(ma, "_worker_shutdown", lambda self: order.append("shutdown"))
    monkeypatch.setattr(ma, "_worker_boot", lambda self, **kw: order.append("boot"))
    monkeypatch.setattr(ma, "_worker_run", lambda *a: order.append("run") or {})
    _worker(ma).run({"1": {}}, "j")
    assert order[:2] == ["shutdown", "boot"]


def _fake_experimental(monkeypatch, has_api=True):
    stops = []
    mx = types.ModuleType("modal.experimental")
    if has_api:
        mx.stop_fetching_inputs = lambda: stops.append(1)
    monkeypatch.setitem(sys.modules, "modal.experimental", mx)
    return stops


def test_k3_cuda_sticky_error_retires_the_container(ma, monkeypatch):
    _healthy_http(monkeypatch)
    stops = _fake_experimental(monkeypatch)

    def boom(**kw):
        raise RuntimeError("工作流执行出错: Node 3 (KSampler): CUDA error: an illegal memory access "
                           "was encountered")
    monkeypatch.setattr(cw, "run_workflow", boom)
    monkeypatch.setattr(cw, "interrupt_comfy", lambda: None)
    ma._t.js["j"] = {"status": "queued"}
    ma._t.js["j:call"] = "fc-1"
    inst = _worker(ma)
    with pytest.raises(RuntimeError):
        inst.run({"1": {}}, "j")
    assert ma._t.js.get("j")["status"] == "failed"
    assert stops == [1], "CUDA 黏性错误后必须让容器不再接单"
    assert inst._restart_before_next is True, "兜底:万一还有下一单进来,先原地重启"


def test_k3_retire_degrades_to_restart_when_sdk_lacks_api(ma, monkeypatch):
    _fake_experimental(monkeypatch, has_api=False)
    inst = _worker(ma)
    ma._retire_if_broken(inst, RuntimeError("CUDA error: unspecified launch failure"))
    assert inst._restart_before_next is True
    order = []
    _healthy_http(monkeypatch)
    monkeypatch.setattr(ma, "_worker_shutdown", lambda self: order.append("shutdown"))
    monkeypatch.setattr(ma, "_worker_boot", lambda self, **kw: order.append("boot"))
    monkeypatch.setattr(ma, "_worker_run", lambda *a: order.append("run") or {})
    inst.run({"1": {}}, "j2")
    assert order == ["shutdown", "boot", "run"], "下一单开跑前必须先重启"
    assert inst._restart_before_next is False


def test_k3_comfy_died_during_job_retires(ma, monkeypatch):
    stops = _fake_experimental(monkeypatch)
    inst = _worker(ma)

    def die(**kw):
        inst.proc.rc = -11            # segfault
        raise RuntimeError("ComfyUI HTTP unreachable")
    monkeypatch.setattr(cw, "run_workflow", die)
    monkeypatch.setattr(cw, "interrupt_comfy", lambda: None)
    _healthy_http(monkeypatch)
    ma._t.js["j:call"] = "fc-1"
    with pytest.raises(RuntimeError):
        inst.run({"1": {}}, "j")
    assert stops == [1]


def test_k3_ordinary_failure_keeps_the_container(ma, monkeypatch):
    stops = _fake_experimental(monkeypatch)
    _healthy_http(monkeypatch)

    def bad(**kw):
        raise ValueError("Workflow validation: Node 5: required input missing")
    monkeypatch.setattr(cw, "run_workflow", bad)
    monkeypatch.setattr(cw, "interrupt_comfy", lambda: None)
    ma._t.js["j:call"] = "fc-1"
    inst = _worker(ma)
    with pytest.raises(ValueError):
        inst.run({"1": {}}, "j")
    assert stops == [] and not getattr(inst, "_restart_before_next", False), "普通失败不能退役容器"


def test_k3_boot_failure_before_run_is_recorded_and_retires(ma, monkeypatch):
    stops = _fake_experimental(monkeypatch)
    _healthy_http(monkeypatch)
    monkeypatch.setattr(ma, "_worker_shutdown", lambda self: None)

    def boot_fail(self, **kw):
        self.proc = Proc(rc=1)
        raise RuntimeError("ComfyUI 进程启动后就退出了(returncode=1)")
    monkeypatch.setattr(ma, "_worker_boot", boot_fail)
    ma._t.js["j"] = {"status": "queued"}
    with pytest.raises(RuntimeError):
        _worker(ma, Proc(rc=-9)).run({"1": {}}, "j")
    assert ma._t.js.get("j")["status"] == "failed" and "退出" in ma._t.js.get("j")["error"]
    assert stops == [1]


# ============================================================================
# K14 — wait_comfy_ready 认得死进程;排队判死文案不再说「不计费」
# ============================================================================
def test_k14_wait_ready_fails_fast_on_dead_process(monkeypatch):
    monkeypatch.setattr(cw, "requests", types.SimpleNamespace(
        get=lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused"))))
    clock, sleeps = [0.0], []

    def sleep(s):                       # 假时钟:不认 proc 的实现会走满 180s 后报「没起来」,而不是挂住
        sleeps.append(s)
        clock[0] += s
    monkeypatch.setattr(cw, "time", types.SimpleNamespace(time=lambda: clock[0], sleep=sleep))
    with pytest.raises(RuntimeError, match="returncode=1"):
        cw.wait_comfy_ready(timeout_s=180, proc=Proc(rc=1))
    assert sleeps == [], "进程已退出就不该再等"


def test_k14_boot_passes_the_process_handle():
    import ast
    src = (MA_DIR / "modal_app.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_worker_boot")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "wait_comfy_ready"]
    assert calls and any(k.arg == "proc" for k in calls[0].keywords)


def test_k14_queue_stale_text_does_not_claim_free(ma):
    now = time.time()
    why = ma._stale_reason({"status": "queued", "queued_at": now - 7 * 3600}, now)
    assert why and "排队期间不计费" not in why and "按容器时长计费" in why


# ============================================================================
# K4 / K5 — 暖容器本地节点纠偏:先停后 reload;比「已加载」而不是磁盘
# ============================================================================
@pytest.fixture
def nodes_fs(tmp_path, monkeypatch):
    vol, dest, bak = tmp_path / "vol", tmp_path / "custom_nodes", tmp_path / "bak"
    vol.mkdir()
    dest.mkdir()
    monkeypatch.setattr(lnb, "VOL_DIR", vol)
    monkeypatch.setattr(lnb, "DEST_DIR", dest)
    monkeypatch.setattr(lnb, "BACKUP_DIR", bak)

    def put_zip(folder, ver):
        with zipfile.ZipFile(vol / f"{folder}.zip", "w") as z:
            z.writestr("__init__.py", f"VERSION = {ver!r}\n")
        (vol / f"{folder}.digest").write_text(ver)
    return types.SimpleNamespace(vol=vol, dest=dest, bak=bak, put_zip=put_zip)


def _real_boot_stubs(ma, monkeypatch, order):
    """跑**真的** _worker_boot,只替换掉子进程和就绪等待。"""
    monkeypatch.setattr(ma.subprocess, "Popen",
                        lambda *a, **k: (order.append("popen"), Proc())[1])
    monkeypatch.setattr(ma.threading, "Thread",
                        lambda **k: types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(cw, "wait_comfy_ready", lambda timeout_s=180, proc=None: None)
    monkeypatch.setattr(ma, "_gpu_compute_cap", lambda: "")


def test_k4_shutdown_happens_before_reload(ma, monkeypatch, nodes_fs):
    """ComfyUI 活着时它可能持有卷上模型文件,Volume.reload 会因 open files 失败。"""
    order = []
    _real_boot_stubs(ma, monkeypatch, order)
    nodes_fs.put_zip("MyNode", "v1")
    lnb.extract_all()
    inst = _worker(ma)
    ma._worker_boot(inst, load_local_nodes=False)
    nodes_fs.put_zip("MyNode", "v2")

    def reload_hook():
        order.append("reload")
        if inst.proc.poll() is None:
            raise RuntimeError("there are open files preventing the operation")
    ma._t.vol.reload_hook = reload_hook
    real_shutdown = ma._worker_shutdown
    monkeypatch.setattr(ma, "_worker_shutdown", lambda self: (order.append("shutdown"), real_shutdown(self)))
    order.clear()
    assert ma._refresh_local_nodes_if_stale(inst, {"MyNode": "v2"}) is True
    # boot 自己开头也会 reload 一次(冷启动同一条路径);关键是每次 reload 时 ComfyUI 都已停下
    assert order[:2] == ["shutdown", "reload"] and order[-1] == "popen", order
    assert ma._LOADED_LOCAL_DIGESTS == {"MyNode": "v2"}


def test_k5_boot_records_what_comfy_loaded(ma, monkeypatch, nodes_fs):
    order = []
    _real_boot_stubs(ma, monkeypatch, order)
    nodes_fs.put_zip("A", "a1")
    lnb.extract_all()
    ma._worker_boot(_worker(ma), load_local_nodes=False)
    assert ma._LOADED_LOCAL_DIGESTS == {"A": "a1"}


def test_k5_stale_check_uses_loaded_not_disk(ma, monkeypatch, nodes_fs):
    """磁盘 marker 已是新版、ComfyUI 内存里还是旧代码(纠偏失败留下的分叉)—— 必须判过期。"""
    order = []
    _real_boot_stubs(ma, monkeypatch, order)
    nodes_fs.put_zip("A", "a1")
    lnb.extract_all()
    inst = _worker(ma)
    ma._worker_boot(inst, load_local_nodes=False)          # ComfyUI 加载了 a1
    nodes_fs.put_zip("A", "a2")
    lnb.extract_all()                                      # 磁盘变成 a2,但 ComfyUI 没重启
    assert lnb.needs_refresh({"A": "a2"}) == [], "前提:比磁盘会得出「不过期」"
    order.clear()
    assert ma._refresh_local_nodes_if_stale(inst, {"A": "a2"}) is True, "必须按已加载版本判过期"
    assert "popen" in order


def test_k5_failed_verify_still_restarts_so_memory_matches_disk(ma, monkeypatch, nodes_fs):
    """复核失败也要重启 ComfyUI:不重启,内存旧代码与磁盘新 marker 分叉,下一单静默跑旧代码。"""
    order = []
    _real_boot_stubs(ma, monkeypatch, order)
    nodes_fs.put_zip("A", "a1")
    nodes_fs.put_zip("B", "b1")
    lnb.extract_all()
    inst = _worker(ma)
    ma._worker_boot(inst, load_local_nodes=False)
    nodes_fs.put_zip("A", "a2")                            # B 的新版没传上来
    order.clear()
    with pytest.raises(RuntimeError, match="版本对不上"):
        ma._refresh_local_nodes_if_stale(inst, {"A": "a2", "B": "b2"})
    assert "popen" in order, "复核失败也必须把 ComfyUI 拉起来"
    assert ma._LOADED_LOCAL_DIGESTS == {"A": "a2", "B": "b1"}
    order.clear()
    # 下一单只用 A=a2:内存里确实是 a2 了,不需要再刷新
    assert ma._refresh_local_nodes_if_stale(inst, {"A": "a2"}) is False
    # 声明 B=b2 的单仍然判过期(而不是比磁盘碰巧对上)
    assert lnb.needs_refresh({"B": "b2"}, ma._LOADED_LOCAL_DIGESTS) == ["B"]


# ============================================================================
# K10 — 节点目录名白名单
# ============================================================================
# 2026-10-05 复核 r2:与本机 local_nodes.safe_folder 对齐后,".hidden" / "-x" / "a..b" / "foo\n" 改为放行
# (本机都能打包上传),见 test_fix_r2_cloud.py 的对照测试
@pytest.mark.parametrize("bad", [".", "..", "", " ", " .. ", "a/b", "a\\b", "a\0b", None, 3])
def test_k10_node_target_rejects(nodes_fs, bad):
    with pytest.raises(ValueError):
        lnb.node_target(bad)


@pytest.mark.parametrize("good", ["ComfyUI-KJNodes", "comfyui_controlnet_aux", "中文节点", "Foo Bar (v2)",
                                  "was-node-suite-comfyui", "rgthree-comfy", "x.y"])
def test_k10_node_target_accepts(nodes_fs, good):
    assert lnb.node_target(good) == nodes_fs.dest / good


def test_k10_restore_baked_dot_never_touches_custom_nodes(nodes_fs):
    (nodes_fs.dest / "Keep").mkdir()
    (nodes_fs.bak / "Other").mkdir(parents=True)          # 有备份目录时旧代码会 rmtree(DEST_DIR)
    with pytest.raises(ValueError):
        lnb.restore_baked(["."])
    assert (nodes_fs.dest / "Keep").is_dir()


def test_k10_extract_all_skips_a_dot_package(nodes_fs):
    (nodes_fs.dest / "Keep").mkdir()
    with zipfile.ZipFile(nodes_fs.vol / "..zip", "w") as z:   # stem == "."
        z.writestr("x.py", "x = 1\n")
    assert lnb.extract_all() == []
    assert (nodes_fs.dest / "Keep").is_dir() and not (nodes_fs.dest / "x.py").exists()


def test_k10_refresh_rejects_bad_names_before_restarting(ma, monkeypatch):
    order = []
    monkeypatch.setattr(ma, "_worker_shutdown", lambda self: order.append("shutdown"))
    monkeypatch.setattr(ma, "_worker_boot", lambda self, **kw: order.append("boot"))
    with pytest.raises(ValueError):
        ma._refresh_local_nodes_if_stale(_worker(ma), {".": lnb.BAKED_SENTINEL, "A": "a1"})
    assert order == [], "坏请求不该让 ComfyUI 白重启一次"


# ============================================================================
# K6 / C1 — job_id 白名单
# ============================================================================
@pytest.mark.parametrize("jid,ok", [
    (".", False), ("..", False), ("-x", False), ("_x", False), ("abc\n", False), ("a..b", False),
    ("a/b", False), ("", False), ("a" * 65, False), (None, False),
    ("a" * 64, True), ("a.b-c_1", True), ("3f2b9c1e-0d4a-4c1e-9a7e-1b2c3d4e5f60", True), ("A", True)])
def test_k6_safe_job_id(ma, jid, ok):
    assert ma._safe_job_id(jid) is ok


def test_k6_gc_never_removes_outputs_dot(ma):
    old = time.time() - 10 * 3600
    for jid in (".", "ok1"):
        ma._t.js[jid] = {"status": "completed", "completed_at": old}
    ma._sweep_job_state()
    assert "_outputs/." not in ma._t.vol.removed
    assert "_outputs/ok1" in ma._t.vol.removed


# ============================================================================
# K7 / K15 — /cancel
# ============================================================================
def test_k7_cancel_rejects_side_keys(ma):
    now = time.time()
    js = ma._t.js
    js["abc"] = {"status": "running", "started_at": now, "timeout_s": 1200}
    js["abc:call"] = "fc-9"
    js["abc:progress"] = {"step": 3, "total": 30}
    r = ma.cancel_endpoint({"auth_key": "k", "job_id": "abc:progress"})
    assert r["status"] == "not_found"
    assert js.get("abc:progress") == {"step": 3, "total": 30}, "进度键被当成任务记录改写了"
    r = ma.cancel_endpoint({"auth_key": "k", "job_id": "abc:call"})      # 以前 500
    assert r["status"] == "not_found"
    assert ma._t.cancels == [] and js.get("abc")["status"] == "running"


def test_k15_cancel_noop_returns_a_field_subset(ma):
    big = "A" * 100000
    ma._t.js["done"] = {"status": "completed", "completed_at": 1.0, "gpu": "H100→A100-80GB",
                        "gpu_actual": "NVIDIA H100", "images": [{"filename": "a.png", "data_base64": big}],
                        "data_base64": big, "trace": "x", "delivery": {"mode": "desktop"}}
    ma._t.js["done:call"] = "fc-1"
    r = ma.cancel_endpoint({"auth_key": "k", "job_id": "done"})
    assert r == {"id": "done", "status": "completed", "completed_at": 1.0, "gpu": "H100→A100-80GB",
                 "gpu_actual": "NVIDIA H100", "cancel_noop": True, "was_running": False}, r


# ============================================================================
# K8 — 取消信号落在写 running 与进 try 之间
# ============================================================================
class InputCancellation(BaseException):
    pass


def test_k8_cancel_during_running_write_still_finalises(ma, monkeypatch):
    js = ma._t.js
    js["j"] = {"status": "queued", "queued_at": 1.0}
    js["j:call"] = "fc-x"
    js["j:progress"] = {"step": 1}
    monkeypatch.setattr(cw, "interrupt_comfy", lambda: None)
    orig_get = js.get
    state = {"n": 0}

    def get(k, default=None):
        v = orig_get(k, default)
        if k == "j" and state["n"] == 0:      # 起跑检查读到 queued 之后,cancel_endpoint 落了 cancelled
            state["n"] = 1
            js.d["j"] = {**js.d["j"], "status": "cancelled", "completed_at": 2.0}
        return v
    js.get = get

    def delitem(k):
        if k.endswith(":progress"):
            raise InputCancellation("Input was cancelled by user")
        del js.d[k]
    monkeypatch.setattr(FakeDict, "__delitem__", lambda self, k: delitem(k))
    with pytest.raises(InputCancellation):
        ma._worker_run({"1": {}}, "j")
    assert orig_get("j")["status"] == "cancelled", "取消落在 try 外,终态停在 running"


# ============================================================================
# K9 — 两段同步数的采样:进度不能冻结在第一段末尾
# ============================================================================
def test_k9_progress_resets_when_v_goes_back(ma, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ma, "time", types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None))
    ma._t.js["j"] = {"status": "queued"}
    ma._t.js["j:call"] = "fc-x"
    seen = []

    def fake_run(workflow, job_id, input_images, materialize, on_progress):
        for spd in (2.0, 30.0):
            for v in range(1, 21):
                clock[0] += spd
                on_progress(v, 20)
                p = ma._t.js.get(f"{job_id}:progress")
                seen.append(p and (p["step"], p["s_it"]))
        return {"images": [{"filename": "x.png", "data_base64": "AA==", "size_bytes": 1}], "errors": []}
    monkeypatch.setattr(cw, "run_workflow", fake_run)
    ma._worker_run({"1": {}}, "j")
    assert seen[21] == (2, 30.0), f"第二段第 2 步应写进度,实际 {seen[21]}"
    assert seen[-1] == (20, 30.0)


# ============================================================================
# K18 — :call 停在占位时 worker 自己补上
# ============================================================================
def test_k18_worker_fills_pending_call_id(ma, monkeypatch):
    monkeypatch.setattr(cw, "run_workflow", lambda **kw: {"images": [], "errors": [], "data_base64": "AA=="})
    ma._t.js["j"] = {"status": "queued"}
    ma._t.js["j:call"] = "pending"
    ma._worker_run({"1": {}}, "j")
    assert ma._t.js.get("j:call") == "fc-self"


def test_k18_never_creates_or_overwrites_call(ma):
    assert ma._claim_pending_call_id("gone") is False and "gone:call" not in ma._t.js, "GC 删了就别再造一个孤儿键"
    ma._t.js["k:call"] = "fc-real"
    assert ma._claim_pending_call_id("k") is False and ma._t.js.get("k:call") == "fc-real"


def test_k18_pending_call_is_claimed_before_a_slow_restart(ma, monkeypatch):
    """探活重启可能要几分钟:占位得在这之前补上,/cancel 才拿得到句柄。"""
    seen = []
    monkeypatch.setattr(ma, "_ensure_comfy_healthy", lambda self: seen.append(ma._t.js.get("j:call")))
    monkeypatch.setattr(ma, "_worker_run", lambda *a: {})
    ma._t.js["j:call"] = "pending"
    _worker(ma).run({"1": {}}, "j")
    assert seen == ["fc-self"]


# ============================================================================
# K17 — 索引过期后的孤儿 _outputs 目录
# ============================================================================
def _ent(path, mtime, typ=2):
    return types.SimpleNamespace(path=path, mtime=mtime, type=typ, size=0)


def test_k17_orphan_outputs_are_collected_conservatively(ma):
    now = time.time()
    # 期限 = max(2×TTL, 8 天)(2026-10-05 复核 r2:多 app 共用 Volume 时 2 小时会删掉别人的产物)
    old, young = now - ma._ORPHAN_MIN_AGE_S - 3600, now - 600
    vol = ma._t.vol
    vol.entries["_outputs"] = [
        _ent("_outputs/orphan", old),                 # 删
        _ent("_outputs/indexed", old),                # 有索引:留
        _ent("_outputs/young", young),                # 还新:留
        _ent("_outputs/zeromtime", 0),                # 目录项 mtime=0 → 看里面的文件
        _ent("_outputs/zeroyoung", 0),
        _ent("_outputs/.", old),                      # 脏名字:绝不碰
        _ent("_outputs/afile", old, typ=1),           # 文件:跳过
    ]
    vol.entries["_outputs/zeromtime"] = [_ent("_outputs/zeromtime/a.mp4", old, 1)]
    vol.entries["_outputs/zeroyoung"] = [_ent("_outputs/zeroyoung/a.mp4", young, 1)]
    records = {"indexed:call": "fc-1"}
    ma._sweep_orphan_outputs(records, now)
    assert sorted(vol.removed) == ["_outputs/orphan", "_outputs/zeromtime"], vol.removed
    vol.removed.clear()
    ma._sweep_orphan_outputs(records, now + 60)
    assert vol.removed == [], "一小时内只扫一次"


def test_k17_orphan_sweep_has_a_budget(ma):
    now = time.time()
    ma._t.vol.entries["_outputs"] = [_ent(f"_outputs/o{i}", now - 9 * 86400) for i in range(25)]
    ma._sweep_orphan_outputs({}, now)
    assert len(ma._t.vol.removed) == ma._VOL_GC_PER_SWEEP


def test_k17_runs_from_the_regular_sweep_with_its_snapshot(ma):
    now = time.time()
    ma._t.js["live"] = {"status": "running", "started_at": now, "timeout_s": 1200}
    ma._t.vol.entries["_outputs"] = [_ent("_outputs/live", now - 9 * 86400), _ent("_outputs/lost", now - 9 * 86400)]
    ma._sweep_job_state()
    assert ma._t.vol.removed == ["_outputs/lost"]


def test_k17_listing_failure_is_harmless(ma, monkeypatch):
    def boom(path, recursive=False):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(ma._t.vol, "listdir", boom)
    ma._sweep_orphan_outputs({}, time.time())        # 不抛
    assert ma._t.vol.removed == []


# ============================================================================
# K19 — /free 是异步的,reload 撞上 open files 要退避重试
# ============================================================================
def test_k19_reload_retries_with_backoff(monkeypatch):
    attempts, sleeps = [], []

    class Vol:
        def reload(self):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("there are open files preventing the operation")
    fake = types.ModuleType("modal")
    fake.Volume = types.SimpleNamespace(from_name=lambda name: Vol())
    monkeypatch.setitem(sys.modules, "modal", fake)
    monkeypatch.setattr(cw, "free_comfy_models", lambda: None)
    monkeypatch.setattr(cw, "time", types.SimpleNamespace(time=time.time, sleep=sleeps.append))
    cw._reload_volume_in_worker()
    assert len(attempts) == 3 and sleeps == [1, 2]
    attempts.clear()
    sleeps.clear()
    fake.Volume = types.SimpleNamespace(from_name=lambda name: types.SimpleNamespace(
        reload=lambda: (attempts.append(1), (_ for _ in ()).throw(RuntimeError("open files")))))
    cw._reload_volume_in_worker()                      # 全部失败也不抛
    assert len(attempts) == len(cw._RELOAD_BACKOFF_S) + 1


# ============================================================================
# K11 — 音频产物
# ============================================================================
def test_k11_audio_is_classified_as_audio():
    for fn in ("a.flac", "b.MP3", "c.wav", "d.ogg", "e.opus", "f.m4a", "g.aac"):
        assert cw.classify_asset_type(fn) == "audio", fn
    assert cw.classify_asset_type("noext", "audio") == "audio"
    assert cw.classify_asset_type("clip.mp4") == "video" and cw.classify_asset_type("x.png") == "image"
    refs = cw.discover_outputs({"5": {"audio": [{"filename": "ComfyUI_00001_.flac", "subfolder": "audio",
                                                 "type": "output"}]},
                                "6": {"text": ["note.mp3"]}})   # 裸字符串照旧不按音频扩展名收
    assert [(r["filename"], r["asset_type"]) for r in refs] == [("ComfyUI_00001_.flac", "audio")]


# ============================================================================
# K12 / K13 / K16 — aigc_delivery
# ============================================================================
_SIGNED = ("https://acct.r2.cloudflarestorage.com/bkt/aigc/u/j/.staging/image-0.png"
           "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA%2F20261005&X-Amz-Signature=deadbeefcafe")


def test_k12_redact_query_in_both_url_shapes():
    full = f"ConnectionError: {_SIGNED} failed"
    path_only = ("HTTPSConnectionPool(host='acct.r2.cloudflarestorage.com', port=443): Max retries exceeded "
                 "with url: /bkt/aigc/u/j/.staging/image-0.png?X-Amz-Algorithm=AWS4&X-Amz-Signature=deadbeef "
                 "(Caused by NewConnectionError('x'))")
    for text in (full, path_only):
        out = ad._redact_query(text)
        assert "deadbeef" not in out and "X-Amz" not in out, out
        assert "image-0.png?<redacted>" in out, "host / path 要留着,排错要用"
    assert ad._redact_query("上传没成功?请重试") == "上传没成功?请重试", "别误伤中文问号"


def test_k12_putter_network_error_log_has_no_signature(monkeypatch, tmp_path, capsys):
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")

    def put(url, data=None, headers=None, timeout=None, **kw):
        raise ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(put=put))
    assert ad._default_putter(_SIGNED, str(f), {}, 5) == (None, {})
    out = capsys.readouterr().out
    assert "deadbeefcafe" not in out and "R2 PUT failed" in out


def test_k12_poster_network_error_has_no_query(monkeypatch):
    def post(url, json=None, headers=None, timeout=None, **kw):
        raise ConnectionError(f"bad url: {url}?token=SECRET123")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    status, text = ad._default_poster("https://studio.example/api/internal/asset-intake", {}, {}, 5)
    assert status is None and "SECRET123" not in text


def test_k16_poster_does_not_follow_redirects(monkeypatch):
    seen = {}

    def post(url, json=None, headers=None, timeout=None, **kw):
        seen.update(kw)
        return types.SimpleNamespace(status_code=307, headers={"Location": "https://evil.example/x?k=v"},
                                     json=lambda: {}, text="")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    status, body = ad._default_poster("https://studio.example/api/internal/job-complete",
                                      {"token": "T"}, {"x-vercel-protection-bypass": "B"}, 5)
    assert seen.get("allow_redirects") is False, "跟随 307 会把 bypass 头和 token 转给跳转目标"
    assert status == 307 and "redirect refused" in body and "k=v" not in body
    monkeypatch.setattr(ad, "_sleep", lambda s: None)
    calls = []
    with pytest.raises(ad.DeliveryError) as ei:
        ad.post_json_with_retry("https://s/api/internal/x", {}, {}, 3,
                                poster=lambda *a: (calls.append(1), (307, "redirect refused"))[1])
    assert len(calls) == 1 and ei.value.retryable is False, "3xx 是失败,不重试"


def test_k16_putter_does_not_follow_redirects(monkeypatch, tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    seen = {}

    def put(url, data=None, headers=None, timeout=None, **kw):
        seen.update(kw)
        return types.SimpleNamespace(status_code=302, headers={"Location": "https://evil/x"})
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(put=put))
    status, _ = ad._default_putter("https://r2/x", str(f), {}, 5)
    assert seen.get("allow_redirects") is False and status == 302


def test_k13_view_failure_does_not_leak_the_temp_fd(monkeypatch, tmp_path):
    made = []
    real_mkstemp = ad.tempfile.mkstemp

    def mkstemp(**kw):
        fd, p = real_mkstemp(dir=str(tmp_path), **kw)
        made.append((fd, p))
        return fd, p
    monkeypatch.setattr(ad.tempfile, "mkstemp", mkstemp)

    def get(*a, **k):
        raise ConnectionError("ComfyUI /view refused")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=get))
    with pytest.raises(ConnectionError):
        ad.stream_output_to_temp({"filename": "a.png"})
    fd, path = made[0]
    with pytest.raises(OSError):
        os.fstat(fd)                                   # 已关闭
    assert not os.path.exists(path)
