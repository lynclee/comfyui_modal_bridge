"""fix1005 第二轮(r2)云端运行时修复的回归测试(2026-10-05 深度 review 复核)。

1. K1 收窄:只有 value_not_in_list 才撤回 + 重试;悬空输出这类部分校验错误不撤回,合法分支跑完,
   被剔除的输出按契约 D1 进 job 记录 / /status 的 warnings。
2. 本地节点目录名与本机 local_nodes.safe_folder 对齐。
3. 孤儿 _outputs 扫描期限 max(2×TTL, 8 天)(多 app 共用 Volume)。
4. 黏性 CUDA 错误只认真正弄坏上下文的那几种。
5. aigc_delivery 只跟随同站的 307/308(同 origin 或同主机 http→https),最多一次。
6. /run 非法 job_id 的提示与 C1 规则一致。

真的 modal_app.py 用 test_fix_cloud 的假 modal 装载(同一套桩,见那个文件的模块说明)。
"""
import json
import sys
import time
import types
import zipfile
from pathlib import Path

import pytest

import test_fix_cloud as fc

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

cw, lnb, ad = fc.cw, fc.lnb, fc.ad


@pytest.fixture
def ma(monkeypatch):
    """同 test_fix_cloud.ma:每个用例一份干净的 job_state / Volume / modal 句柄。"""
    m = fc._load_modal_app()
    js, vol = fc.FakeDict(), fc.FakeVolume()
    cancels = []
    fake_modal = types.SimpleNamespace(
        FunctionCall=types.SimpleNamespace(
            from_id=lambda cid: types.SimpleNamespace(cancel=lambda: cancels.append(cid))),
        current_function_call_id=lambda: "fc-self")
    monkeypatch.setattr(m, "job_state", js)
    monkeypatch.setattr(m, "models_vol", vol)
    monkeypatch.setattr(m, "modal", fake_modal)
    monkeypatch.setattr(m, "_check", lambda key: None)
    monkeypatch.setattr(m, "_last_sweep", [0.0])
    monkeypatch.setattr(m, "_last_orphan_sweep", [0.0])
    monkeypatch.setattr(m, "_gpu_name", lambda: "NVIDIA H100")
    monkeypatch.setattr(m, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    m._t = types.SimpleNamespace(js=js, vol=vol, cancels=cancels)
    return m


# ============================================================================
# 1. K1 收窄 + 契约 D1 warnings
# ============================================================================
def _dangling(pid):
    """reviewer 的复现:PreviewImage(50)的 images 没接线,v0.37.2 只剔除它、主输出 9 照常入队 → 200。"""
    return {"prompt_id": pid, "number": 1, "node_errors": {
        "50": {"errors": [{"type": "required_input_missing", "message": "Required input is missing",
                           "details": "images", "extra_info": {"input_name": "images"}}],
               "dependent_outputs": ["50"], "class_type": "PreviewImage"}}}


_WF = {"9": {"class_type": "SaveImage", "inputs": {}}, "50": {"class_type": "PreviewImage", "inputs": {}},
       "31": {"class_type": "LoraLoader", "inputs": {}}, "40": {"class_type": "SaveImage", "inputs": {}}}


def test_k1r2_dangling_output_is_not_withdrawn(monkeypatch):
    resp = _dangling("p-1")
    calls, reloads = fc._patch_comfy_http(monkeypatch, [fc._Resp(200, resp)])
    assert cw.queue_workflow(_WF, "cid") == resp
    assert [c[0] for c in calls] == ["/prompt"], "合法分支已经入队,不能撤回"
    assert reloads == []


def test_k1r2_dangling_output_job_completes_with_warnings(monkeypatch):
    """端到端:主产物照常返回,被剔除的 PreviewImage 写进 warnings(节点 id + class_type + 原因)。"""
    pid = "p-dangle"
    posts = []

    def post(url, data=None, json=None, headers=None, timeout=None, **kw):
        posts.append(url.rsplit("/", 1)[-1])
        return fc._Resp(200, _dangling(pid))
    monkeypatch.setattr(cw, "requests", types.SimpleNamespace(post=post, get=None))
    msgs = [{"type": "executing", "data": {"node": None, "prompt_id": pid}}]

    class WS:
        connected = True

        def connect(self, *a, **k):
            pass

        def recv(self):
            return json.dumps(msgs.pop(0))

        def close(self):
            pass
    monkeypatch.setattr(cw.websocket, "WebSocket", WS, raising=False)
    monkeypatch.setattr(cw, "get_history", lambda p: {pid: {
        "outputs": {"9": {"images": [{"filename": "main_00001_.png", "subfolder": "", "type": "output"}]}},
        "status": {"status_str": "success", "completed": True, "messages": []}}})
    monkeypatch.setattr(cw, "get_image_data", lambda *a: b"png")
    r = cw.run_workflow(_WF, job_id="j")
    assert [i["filename"] for i in r["images"]] == ["main_00001_.png"]
    assert posts == ["prompt"], "不能有 /queue delete、/interrupt"
    assert len(r["warnings"]) == 1
    w = r["warnings"][0]
    assert "输出节点 50 (PreviewImage)" in w and "required_input_missing: images" in w, w
    assert "\n" not in w


def test_k1r2_missing_model_still_withdraws_and_retries_then_runs_the_rest(monkeypatch):
    """同一个 node_errors 里既有缺模型又有悬空输出:缺模型优先 —— 撤回 + reload 重试;重试后模型看到了,
    只剩悬空输出 → 不再撤回,合法分支跑,悬空的那条进 warnings。"""
    mixed = {"prompt_id": "p-1", "number": 1, "node_errors": {
        **fc._partial("x")["node_errors"], **_dangling("x")["node_errors"]}}
    after = _dangling("p-2")
    calls, reloads = fc._patch_comfy_http(monkeypatch, [fc._Resp(200, mixed), fc._Resp(200, after)])
    assert cw.queue_workflow(_WF, "cid") == after
    assert reloads == [1]
    assert [c[0] for c in calls] == ["/prompt", "/queue", "/interrupt", "/prompt"], calls
    assert calls[1][1] == {"delete": ["p-1"]}
    ws = cw.partial_validation_warnings(after["node_errors"], _WF)
    assert len(ws) == 1 and "输出节点 50 (PreviewImage)" in ws[0]


def test_k1r2_missing_model_exhausted_still_fails(monkeypatch):
    """K1 初衷不变:缺模型的分支重试用尽仍找不到 → 整单失败,绝不静默少产物。"""
    resps = [fc._Resp(200, fc._partial(f"p-{i}")) for i in range(cw._RETRY_MAX + 1)]
    calls, reloads = fc._patch_comfy_http(monkeypatch, resps)
    with pytest.raises(cw.ValidationError, match="找不到"):
        cw.queue_workflow(_WF, "cid")
    assert len(reloads) == cw._RETRY_MAX
    assert len([c for c in calls if c[0] == "/queue"]) == cw._RETRY_MAX + 1


def test_k1r2_warnings_shape_and_no_raw_values():
    """单行、限长、按输出节点归并;会带输入原值的错误类型只写类型 + 输入名(契约 D1:不含凭据)。"""
    ne = {
        "7": {"class_type": "ApiNode", "dependent_outputs": ["40", "9"], "errors": [
            {"type": "invalid_input_type", "message": "Failed to convert an input value to a INT value",
             "details": "seed, sk-SECRET-123, invalid literal", "extra_info": {"input_name": "seed"}},
            {"type": "custom_validation_failed", "message": "Custom validation failed for node",
             "details": "api_key - token ghp_LEAK is invalid", "extra_info": {"input_name": "api_key"}}]},
        "12": {"class_type": "KSampler", "dependent_outputs": ["9"], "errors": [
            {"type": "value_smaller_than_min", "message": "Value 0 smaller than min of 1",
             "details": "steps", "extra_info": {"input_name": "steps"}}]},
        "13": {"class_type": "Loop", "dependent_outputs": [], "errors": [
            {"type": "return_type_mismatch", "message": "m",
             "details": "x, received_type(IMAGE)\nmismatch input_type(LATENT)", "extra_info": {}}]},
    }
    ws = cw.partial_validation_warnings(ne, _WF)
    joined = " ".join(ws)
    assert "SECRET" not in joined and "ghp_LEAK" not in joined, ws
    assert ws[0].startswith("输出节点 9 (SaveImage)") and ws[1].startswith("输出节点 40 (SaveImage)"), ws
    assert "invalid_input_type: seed" in ws[0] and "custom_validation_failed: api_key" in ws[0]
    assert "value_smaller_than_min: steps (Value 0 smaller than min of 1)" in ws[0]
    assert "节点 13 (Loop)" in ws[2] and "mismatch input_type(LATENT)" in ws[2]
    assert all("\n" not in w and len(w) <= cw._WARN_MAX_LEN for w in ws)
    assert cw.partial_validation_warnings({}, _WF) == [] and cw.partial_validation_warnings(None) == []
    assert cw.partial_validation_warnings(["odd"])[0].startswith("ComfyUI 校验时剔除了部分输出分支")


def _fake_run(warnings, errors=()):
    def run(workflow, job_id, input_images, materialize, on_progress):
        out = {"errors": list(errors), "warnings": list(warnings), "output_refs": [{"filename": "a.png"}]}
        if materialize:
            out["images"] = [{"filename": "a.png", "data_base64": "AA==", "size_bytes": 1}]
        return out
    return run


def test_d1_desktop_completed_record_and_status_carry_warnings(ma, monkeypatch):
    monkeypatch.setattr(cw, "run_workflow", _fake_run(["输出节点 50 (PreviewImage) 没有执行"]))
    ma._t.js["j"] = {"status": "queued", "queued_at": time.time()}
    ma._t.js["j:call"] = "fc-1"
    ma._worker_run({"1": {}}, "j")
    rec = ma._t.js.get("j")
    assert rec["status"] == "completed" and rec["warnings"] == ["输出节点 50 (PreviewImage) 没有执行"]
    st = ma.status_endpoint("j", x_bridge_key="k")
    assert st["status"] == "completed" and st["warnings"] == rec["warnings"]


def test_d1_aigc_r2_completed_record_carries_warnings(ma, monkeypatch):
    monkeypatch.setattr(cw, "run_workflow", _fake_run(["输出节点 50 (PreviewImage) 没有执行"]))
    monkeypatch.setattr(ad, "deliver_outputs", lambda **kw: {"status": "completed", "assets": [{"r2_key": "k"}]})
    ma._t.js["j"] = {"status": "queued"}
    ma._t.js["j:call"] = "fc-1"
    ma._worker_run({"1": {}}, "j", delivery={"mode": "aigc-r2", "job_id": "j", "token": "T"})
    rec = ma._t.js.get("j")
    assert rec["status"] == "completed" and rec["warnings"] == ["输出节点 50 (PreviewImage) 没有执行"]
    assert "token" not in json.dumps(rec), "delivery token 不能进记录"


def test_d1_no_warnings_key_when_clean(ma, monkeypatch):
    monkeypatch.setattr(cw, "run_workflow", _fake_run([]))
    ma._t.js["j"] = {"status": "queued"}
    ma._t.js["j:call"] = "fc-1"
    ma._worker_run({"1": {}}, "j")
    assert "warnings" not in ma._t.js.get("j"), "没有警告时别塞空列表(前端按非空判断)"


def test_d1_warnings_are_capped(ma):
    assert len(ma._result_warnings({"warnings": [f"w{i}" for i in range(30)], "errors": ["e"]})) == 20
    assert ma._result_warnings({"warnings": ["w"], "errors": ["e"]}) == ["w", "e"]


# ============================================================================
# 2. 本地节点目录名与本机 safe_folder 对齐
# ============================================================================
_NAMES = ["Bob's Nodes", "nodes&tools", "my-nodes!", "#wip", "~test", "(old) nodes", "[dev] tools",
          "comfy🎨nodes", "🎨nodes", "nodes=v2", "a" * 130, "my_nodes - 副本", "ComfyUI-Foo", ".hidden",
          "-x", "a..b", "...", " sp", "x.y",
          ".", "..", "", " ", " .. ", "a/b", "a\\b", "/etc", "../x"]


def test_k10r2_cloud_accepts_exactly_what_local_accepts(tmp_path, monkeypatch):
    import local_nodes as ln
    root = tmp_path / "custom_nodes"
    root.mkdir()
    monkeypatch.setattr(lnb, "DEST_DIR", root)
    mismatch = []
    for name in _NAMES:
        try:
            ln.safe_folder(root, name)
            local = True
        except ValueError:
            local = False
        try:
            lnb.node_target(name)
            cloud = True
        except ValueError:
            cloud = False
        if local != cloud:
            mismatch.append((name, local, cloud))
    assert mismatch == [], mismatch


@pytest.mark.parametrize("bad", ["a\0b", "\0", None, 3, " . "])
def test_k10r2_still_rejects(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(lnb, "DEST_DIR", tmp_path)
    with pytest.raises(ValueError):
        lnb.node_target(bad)


def test_k10r2_odd_names_extract_and_refresh(fc_nodes_fs, ma, monkeypatch):
    """本机能上传的怪名字:解压、指纹、纠偏都要走得通(以前声明了它的任务一律 ValueError 失败)。"""
    for name in ("Bob's Nodes", "[dev] tools", "🎨nodes"):
        fc_nodes_fs.put_zip(name, "v1")
    assert sorted(lnb.extract_all()) == sorted(["Bob's Nodes", "[dev] tools", "🎨nodes"])
    assert lnb.current_digests() == {"Bob's Nodes": "v1", "[dev] tools": "v1", "🎨nodes": "v1"}
    monkeypatch.setattr(ma, "_LOADED_LOCAL_DIGESTS", lnb.current_digests())
    assert ma._refresh_local_nodes_if_stale(fc._worker(ma), {"Bob's Nodes": "v1", "[dev] tools": "v1"}) is False


def test_k10r2_dot_names_still_cannot_touch_custom_nodes(fc_nodes_fs):
    (fc_nodes_fs.dest / "Keep").mkdir()
    for stem in (".", "..", " "):
        with zipfile.ZipFile(fc_nodes_fs.vol / f"{stem}.zip", "w") as z:
            z.writestr("x.py", "x = 1\n")
    assert lnb.extract_all() == []
    assert (fc_nodes_fs.dest / "Keep").is_dir() and not (fc_nodes_fs.dest / "x.py").exists()


@pytest.fixture
def fc_nodes_fs(tmp_path, monkeypatch):
    vol, dest, bak = tmp_path / "vol", tmp_path / "custom_nodes", tmp_path / "bak"
    vol.mkdir()
    dest.mkdir()
    monkeypatch.setattr(lnb, "VOL_DIR", vol)
    monkeypatch.setattr(lnb, "DEST_DIR", dest)
    monkeypatch.setattr(lnb, "BACKUP_DIR", bak)

    def put_zip(folder, ver):
        with zipfile.ZipFile(vol / f"{folder}.zip", "w") as z:
            z.writestr("__init__.py", f"VERSION = {ver!r}\n")
        (vol / f"{folder}.digest").write_text(ver, encoding="utf-8")
    return types.SimpleNamespace(vol=vol, dest=dest, bak=bak, put_zip=put_zip)


# ============================================================================
# 3. 孤儿 _outputs 扫描期限
# ============================================================================
def test_k17r2_other_apps_outputs_survive_past_two_ttl(ma):
    """reviewer 复现:B 的产物 3 小时没取,A 的扫描(看不到 B 的 Dict)以前就删了。"""
    now = time.time()
    ma._t.js["a-job"] = {"status": "completed", "completed_at": now - 60}
    ma._t.vol.entries["_outputs"] = [fc._ent("_outputs/a-job", now - 3 * 3600),
                                     fc._ent("_outputs/b-job-uuid", now - 3 * 3600),
                                     fc._ent("_outputs/b-week", now - 7 * 86400)]
    ma._sweep_orphan_outputs(dict(ma._t.js.d), now)
    assert ma._t.vol.removed == [], "Dict 条目 7 天才过期:8 天内的目录可能还有主人"


def test_k17r2_horizon_is_max_of_ttl_and_eight_days(ma, monkeypatch):
    now = time.time()
    assert ma._ORPHAN_MIN_AGE_S == 8 * 86400
    ma._t.vol.entries["_outputs"] = [fc._ent("_outputs/nine", now - 9 * 86400),
                                     fc._ent("_outputs/seven", now - 7 * 86400)]
    ma._sweep_orphan_outputs({}, now)
    assert ma._t.vol.removed == ["_outputs/nine"]
    # JOB_TTL_S 配得很大时期限跟着 2×TTL 走
    monkeypatch.setattr(ma, "JOB_TTL_S", 10 * 86400)
    monkeypatch.setattr(ma, "_last_orphan_sweep", [0.0])
    ma._t.vol.removed.clear()
    ma._sweep_orphan_outputs({}, now)
    assert ma._t.vol.removed == []


# ============================================================================
# 4. 黏性 CUDA 错误
# ============================================================================
@pytest.mark.parametrize("text", [
    "CUDA error: an illegal memory access was encountered\nCUDA kernel errors might be asynchronously reported",
    "Node 3 (KSampler): CUDA error: device-side assert triggered",
    "CUDA error: unspecified launch failure",
    "CUDA error: misaligned address",
    "CUDA error: an illegal instruction was encountered",
    "CUDA error: hardware stack error",
    "CUDA error: invalid program counter",
    "CUDA error: operation not supported on global/shared address space",
    "CUDA error: the launch timed out and was terminated",
    "CUDA error: uncorrectable ECC error encountered",
    "Triton Error [CUDA]: an illegal memory access was encountered",
    "cudaErrorIllegalAddress",
    "CUDA_ERROR_LAUNCH_FAILED",
    "CUDA driver error: CUDA_ERROR_ILLEGAL_ADDRESS",
])
def test_k3r2_sticky_cuda_errors_match(ma, text):
    assert ma._STICKY_CUDA.search(text), text


@pytest.mark.parametrize("text", [
    "CUDA error: out of memory",
    "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
    "CUDA error: invalid argument",
    "CUDA error: no kernel image is available for execution on the device",
    "CUDA error: too many resources requested for launch",
    "CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate(handle)",
    "cudaErrorMemoryAllocation",
    "CUDA_ERROR_OUT_OF_MEMORY",
])
def test_k3r2_non_sticky_cuda_errors_do_not_match(ma, text):
    assert not ma._STICKY_CUDA.search(text), text


def test_k3r2_cuda_oom_keeps_the_container(ma, monkeypatch):
    stops = fc._fake_experimental(monkeypatch)
    fc._healthy_http(monkeypatch)
    inst = fc._worker(ma)
    ma._retire_if_broken(inst, RuntimeError("工作流执行出错: Node 3 (KSampler): CUDA error: out of memory"))
    assert stops == [] and not getattr(inst, "_restart_before_next", False), "OOM 不黏,不能丢掉暖容器"
    ma._retire_if_broken(inst, RuntimeError("Node 3: CUDA error: misaligned address"))
    assert stops == [1]


# ============================================================================
# 5. aigc_delivery 同站跳转
# ============================================================================
class _R:
    def __init__(self, status, location=None, body=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.mark.parametrize("url,loc,status,ok", [
    ("http://studio.example/api/internal/x", "https://studio.example/api/internal/x", 308, True),
    ("http://studio.example:80/api/x", "https://studio.example:443/api/x", 307, True),
    ("https://studio.example/api/x", "/api/x/", 308, True),                        # 同 origin(相对)
    ("https://studio.example/api/x", "https://STUDIO.example/api/y", 307, True),   # host 大小写
    ("https://studio.example/api/x", "https://www.studio.example/api/x", 308, False),  # apex→www:跨主机
    ("https://studio.example/api/x", "https://evil.example/api/x", 307, False),
    ("https://studio.example/api/x", "//evil.example/api/x", 307, False),
    ("https://studio.example/api/x", "http://studio.example/api/x", 308, False),   # 降级
    ("https://studio.example/api/x", "https://studio.example:8443/api/x", 308, False),
    ("http://studio.example/api/x", "https://studio.example:8443/api/x", 308, False),
    ("http://studio.example/api/x", "https://studio.example/api/x", 301, False),   # 会改方法
    ("http://studio.example/api/x", "https://studio.example/api/x", 302, False),
    ("https://studio.example/api/x", "/api/y", 303, False),
    ("https://studio.example/api/x", "https://u:p@studio.example/api/x", 307, False),
    ("https://studio.example/api/x", "/\\evil.example/x", 307, False),
    ("https://studio.example/api/x", "", 307, False),
])
def test_k16r2_same_site_redirect_rules(url, loc, status, ok):
    nxt, why = ad._same_site_redirect(url, _R(status, loc))
    assert bool(nxt) is ok, (url, loc, status, nxt, why)
    assert bool(why) is not ok


def _patch_post(monkeypatch, replies):
    seen = []

    def post(url, json=None, headers=None, timeout=None, **kw):
        seen.append({"url": url, "json": json, "headers": dict(headers or {}), **kw})
        return replies.pop(0)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    return seen


def test_k16r2_poster_follows_http_to_https_once_keeping_body_and_headers(monkeypatch):
    seen = _patch_post(monkeypatch, [_R(308, "https://studio.example/api/internal/asset-intake"),
                                     _R(200, body={"put_url": "u", "r2_key": "k"})])
    body, hdr = {"job_id": "j", "token": "T"}, {"x-vercel-protection-bypass": "B"}
    status, resp = ad._default_poster("http://studio.example/api/internal/asset-intake", body, hdr, 5)
    assert status == 200 and resp == {"put_url": "u", "r2_key": "k"}
    assert [s["url"] for s in seen] == ["http://studio.example/api/internal/asset-intake",
                                        "https://studio.example/api/internal/asset-intake"]
    assert seen[1]["json"] == body and seen[1]["headers"] == hdr, "方法与 body 必须原样"
    assert all(s.get("allow_redirects") is False for s in seen), "requests 自己永远不跟"


def test_k16r2_poster_refuses_cross_host_and_explains(monkeypatch):
    seen = _patch_post(monkeypatch, [_R(308, "https://www.studio.example/api/internal/job-complete?sig=s")])
    status, text = ad._default_poster("https://studio.example/api/internal/job-complete", {"token": "T"}, {}, 5)
    assert len(seen) == 1 and status == 308
    assert "redirect refused" in text and "最终地址" in text and "sig=s" not in text


def test_k16r2_poster_follows_at_most_once(monkeypatch):
    seen = _patch_post(monkeypatch, [_R(307, "/api/a/"), _R(307, "/api/a")])
    status, text = ad._default_poster("https://studio.example/api/a", {}, {}, 5)
    assert len(seen) == 2 and status == 307 and "只跟随一次" in text


def test_k16r2_refused_redirect_is_not_retried(monkeypatch):
    """3xx 仍按 4xx 一类处理:不重试(重试也只会再被拒)。"""
    seen = _patch_post(monkeypatch, [_R(301, "https://studio.example/api/internal/x")] * 3)
    monkeypatch.setattr(ad, "_sleep", lambda s: None)
    with pytest.raises(ad.DeliveryError) as ei:
        ad.post_json_with_retry("http://studio.example/api/internal/x", {}, {}, 3)
    assert len(seen) == 1 and ei.value.retryable is False


def test_k16r2_putter_follows_same_origin_and_resends_the_file(monkeypatch, tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"payload-bytes")
    sent = []
    ok = _R(200)
    ok.headers["ETag"] = '"e"'
    replies = [_R(307, "https://acct.r2.example/bkt/x?X-Amz-Signature=new"), ok]

    def put(url, data=None, headers=None, timeout=None, **kw):
        sent.append((url, data.read(), kw.get("allow_redirects")))
        return replies.pop(0)
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(put=put))
    status, headers = ad._default_putter("https://acct.r2.example/bkt/x?X-Amz-Signature=old", str(f), {}, 5)
    assert status == 200 and headers.get("ETag") == '"e"'
    assert [s[1] for s in sent] == [b"payload-bytes", b"payload-bytes"], "跟随后必须整份重传"
    assert all(s[2] is False for s in sent)


def test_k16r2_putter_refuses_cross_host(monkeypatch, tmp_path, capsys):
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    sent = []

    def put(url, data=None, headers=None, timeout=None, **kw):
        sent.append(url)
        return _R(307, "https://evil.example/x?X-Amz-Signature=zzz")
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(put=put))
    status, _ = ad._default_putter("https://acct.r2.example/bkt/x", str(f), {}, 5)
    assert status == 307 and len(sent) == 1
    out = capsys.readouterr().out
    assert "redirect refused" in out and "zzz" not in out


# ============================================================================
# 6. /run 非法 job_id 的提示
# ============================================================================
@pytest.mark.parametrize("jid", [".x", "a..b", "-x"])
def test_run_invalid_job_id_message_matches_c1(ma, jid):
    r = ma.run_endpoint({"auth_key": "k", "workflow": {"1": {}}, "job_id": jid})
    err = r["error"]
    assert err.startswith("invalid job_id") and "首字符必须是字母或数字" in err and "不能含 .." in err, err
    assert ma._t.js.d == {}, "非法 id 不能落任何记录"
