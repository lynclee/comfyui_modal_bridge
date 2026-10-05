"""fix1005 本机路由层 / 配置的回归测试(2026-10-05 深度 review)。

全部走真实 aiohttp 路由(harness 复用 test_routes.py:假 folder_paths / server,config 落临时目录)。
外部依赖一律 monkeypatch:modal deploy / secret create(_run_streamed)、云端 /health、Modal Volume、
git、GitHub tag 列表 —— 下面的 autouse fixture 先把它们全部换成「碰了就记一笔」的假实现,
单条测试再按需覆盖。**任何一条都不应真的起子进程、连 *.modal.run 或 GitHub。**
"""
import ast
import asyncio
import json
import os
import threading
import time
import types
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import test_routes as harness
from comfyui_modal_bridge import config as cfg_mod
from comfyui_modal_bridge import contract
from comfyui_modal_bridge import health_client
from comfyui_modal_bridge import local_nodes
from comfyui_modal_bridge import modal_client
from comfyui_modal_bridge import modal_volume
from comfyui_modal_bridge import node_sync
from comfyui_modal_bridge import private_json
from comfyui_modal_bridge import routes as rt
from comfyui_modal_bridge.result_receipts import ResultReceipts

ROOT = Path(__file__).resolve().parent.parent
MAIN_THREAD = threading.main_thread().ident


class _FakeVolumeUnavailable(RuntimeError):
    """契约 C13 的 local_nodes.VolumeUnavailable(build 分支新增;合并前这里补一个同名替身)。"""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """默认把所有外部副作用换成假的。返回一个记录本,测试可以往里看。"""
    rec = types.SimpleNamespace(cmds=[], secret_key_in_config=[], health_calls=0)
    monkeypatch.setattr(local_nodes, "VolumeUnavailable",
                        getattr(local_nodes, "VolumeUnavailable", _FakeVolumeUnavailable), raising=False)
    monkeypatch.setattr(node_sync, "DATA_FILE", tmp_path / "_custom_nodes_data.py")
    monkeypatch.setattr(node_sync, "LOCAL_REQS_FILE", tmp_path / "_local_nodes_data.py")
    monkeypatch.setattr(node_sync, "EXTRA_MODEL_PATHS_YAML", tmp_path / "extra.yaml")
    monkeypatch.setattr(node_sync, "list_comfyui_tags", lambda *a, **k: [])
    monkeypatch.setattr(node_sync, "detect_local_comfyui_version", lambda: "")
    monkeypatch.setattr(node_sync, "modal_available", lambda: True)
    monkeypatch.setattr(node_sync, "write_extra_model_paths", lambda *a, **k: [])
    monkeypatch.setattr(node_sync, "folder_git_info", lambda name: {"has_git": False})

    def no_cloud(cfg, timeout=20):
        raise health_client.HealthUnavailable("not_deployed", "测试:没有云端")
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", no_cloud)
    monkeypatch.setattr(node_sync, "reconcile_baked_with_cloud", lambda cfg: node_sync.Reconciled([], []))

    async def fake_health(session, cfg):
        rec.health_calls += 1
        return {"healthy": True, "custom_nodes": ["n"], "custom_nodes_manifest": [{"name": "n"}]}
    monkeypatch.setattr(modal_client, "health", fake_health)

    async def fake_list_nodes(session, cfg):
        raise RuntimeError("测试:/health 不可达")
    monkeypatch.setattr(modal_client, "list_nodes", fake_list_nodes)

    async def fake_run(resp, cmd, cwd, env):
        kind = "secret" if "secret" in cmd else "deploy" if "deploy" in cmd else \
            "compat" if "node_compat_check.py" in cmd else "?"
        rec.cmds.append((kind, env.get("MODAL_BRIDGE_APP_NAME")))
        if kind == "secret":
            key = next((a.split("=", 1)[1] for a in cmd if a.startswith("BRIDGE_API_KEY=")), "")
            rec.secret_key_in_config.append((key, cfg_mod.load_config().get("bridge_api_key")))
        return 0
    monkeypatch.setattr(rt, "_run_streamed", fake_run)
    monkeypatch.setattr(local_nodes, "list_volume_local_nodes", lambda cfg, max_age=60: [])
    monkeypatch.setattr(modal_volume, "record_deployed_reqs", lambda *a: True)
    monkeypatch.setattr(modal_volume, "modal_importable", lambda: True)
    monkeypatch.setattr(rt, "_HEALTH_404_RETRY_S", 0)
    monkeypatch.setattr(rt, "_output_dir", lambda: tmp_path / "output")
    # asyncio.Lock 一旦发生过争用就绑死在那个事件循环上;每条测试各跑各的 loop,换新锁
    monkeypatch.setattr(rt, "_DEPLOY_LOCK", asyncio.Lock())
    monkeypatch.setattr(rt, "_UPLOAD_LOCK", asyncio.Lock())
    rt._LAST_HEALTH["healthy"] = None
    harness._set_cfg()
    yield rec
    p = cfg_mod._config_path()
    if p.exists():
        p.unlink()        # 损坏的 config 会让 _set_cfg 拒绝覆盖 —— 先删再恢复
    harness._set_cfg()


def _last_marked(text: str, mark: str = "✗") -> str:
    lines = [ln for ln in text.splitlines() if mark in ln]
    return lines[-1] if lines else ""


def _rc(text: str) -> int:
    last = text.strip().splitlines()[-1]
    assert last.startswith("__DEPLOY_DONE__ rc="), f"流没有以 __DEPLOY_DONE__ 收尾: {text[-300:]!r}"
    return int(last.split("rc=")[1])


DEPLOY_BODY = {"token_id": "ak-test", "token_secret": "as-test", "workspace": "ws"}


# ── R1:config.json 损坏 ──────────────────────────────────────────────────
def _corrupt_config() -> bytes:
    p = cfg_mod._config_path()
    raw = p.read_text(encoding="utf-8")
    bad = raw.rstrip().rstrip("}").rstrip() + ',\n  "comfyui_tag_pin": "v0.3.60",\n}\n'   # 手改的尾逗号
    p.write_text(bad, encoding="utf-8")
    return p.read_bytes()


def test_corrupt_config_raises_with_line_number_instead_of_defaults():
    _corrupt_config()
    with pytest.raises(cfg_mod.ConfigCorrupt) as ei:
        cfg_mod.load_config()
    msg = str(ei.value)
    assert ei.value.lineno and f"第 {ei.value.lineno} 行解析失败" in msg and "修好或删除" in msg, msg
    assert "bk-secret-value" not in msg, "错误信息不能带出文件内容(里面是凭据)"
    for top in ("[1, 2]", '"x"', "null"):
        cfg_mod._config_path().write_text(top, encoding="utf-8")
        with pytest.raises(cfg_mod.ConfigCorrupt, match="不是 JSON 对象"):
            cfg_mod.load_config()


def test_missing_config_still_falls_back_to_defaults():
    cfg_mod._config_path().unlink()
    cfg = cfg_mod.load_config()
    assert cfg["modal_app_name"] == "comfyui-bridge" and cfg_mod._config_path().exists()


def test_corrupt_config_is_never_overwritten_by_any_writer():
    before = _corrupt_config()
    with pytest.raises(cfg_mod.ConfigCorrupt):
        cfg_mod.save_config({"bridge_api_key": ""})
    with pytest.raises(cfg_mod.ConfigCorrupt):
        cfg_mod.ensure_local_api_capability()
    assert cfg_mod._config_path().read_bytes() == before, "损坏的 config 被覆盖了 —— 凭据会全丢"


def test_routes_answer_500_with_the_line_and_leave_the_file_alone():
    before = _corrupt_config()

    async def go(c):
        out = {}
        r = await c.post("/modal_bridge/config", json={"gpu_tier": "primary"})
        out["post_config"] = (r.status, await r.json())
        r = await c.get("/modal_bridge/config")
        out["get_config"] = r.status
        # 非 loopback:以前 _admin_denial 会「默认值 + 新 capability」整份写回
        r = await c.get("/modal_bridge/list_nodes", headers={"Host": "bridge.example"})
        out["remote"] = r.status
        r = await c.post("/modal_bridge/deploy", json=DEPLOY_BODY)
        out["deploy"] = (r.status, r.content_type)
        r = await c.get("/modal_bridge/version")
        out["version"] = r.status
        return out

    out = harness._run(go)
    st, body = out["post_config"]
    assert st == 500 and "解析失败" in body["error"] and body.get("code") == "config_corrupt", body
    assert out["get_config"] == 500 and out["remote"] == 500 and out["version"] == 500, out
    assert out["deploy"] == (500, "application/json"), "config 读不了时要在开流之前回 JSON 500"
    assert cfg_mod._config_path().read_bytes() == before


def test_read_local_capability_file_warns_on_corrupt(tmp_path, capsys):
    f = tmp_path / "config.json"
    f.write_text('{"local_api_capability": "cap",}', encoding="utf-8")
    assert cfg_mod.read_local_capability_file(str(f)) == ""
    assert "解析失败" in capsys.readouterr().err, "MCP 读不到 capability 时要说明是 config 坏了"


def test_atomic_write_fsyncs_before_replace(monkeypatch, tmp_path):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(private_json.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(private_json.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    private_json.atomic_write_json(tmp_path / "c.json", {"a": 1})
    assert events and events[0] == "fsync" and "replace" in events, events
    assert events.index("fsync") < events.index("replace")
    assert json.loads((tmp_path / "c.json").read_text()) == {"a": 1}


# ── R2 / C6:节点同步的删除必须显式 ────────────────────────────────────
def test_check_nodes_and_list_nodes_flag_unchecked_cloud(monkeypatch):
    monkeypatch.setattr(node_sync, "plan_node_sync", lambda prompt, baked=None: {"expect_baked": []})

    async def go(c):
        r1 = await c.post("/modal_bridge/check_nodes", json={"prompt": {}})
        r2 = await c.get("/modal_bridge/list_nodes")
        return await r1.json(), await r2.json()

    chk, lst = harness._run(go)
    assert chk["source"] == "local" and "不可达" in chk.get("cloud_unchecked", ""), chk
    assert lst["source"] == "local" and lst.get("cloud_unchecked"), lst

    async def ok_list(session, cfg):
        return {"custom_nodes": [], "custom_nodes_manifest": []}
    monkeypatch.setattr(modal_client, "list_nodes", ok_list)
    chk, lst = harness._run(go)
    assert chk["source"] == "modal" and "cloud_unchecked" not in chk, chk
    assert "cloud_unchecked" not in lst


def _deployed_baked(rec_list):
    async def fake_run(resp, cmd, cwd, env):
        if "deploy" in cmd:
            rec_list.append(sorted(n["name"] for n in node_sync.read_baked_nodes()))
        return 0
    return fake_run


def test_sync_nodes_keeps_cloud_nodes_unless_explicitly_pruned(monkeypatch):
    manifest = [{"name": "A", "url": "https://x/A", "commit": "1"},
                {"name": "B", "url": "https://x/B", "commit": "2"}]
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", lambda cfg, timeout=20: (["A", "B"], manifest))
    deployed = []
    monkeypatch.setattr(rt, "_run_streamed", _deployed_baked(deployed))
    new = [{"name": "New", "url": "https://x/New", "commit": "3"}]

    async def go(c, body):
        r = await c.post("/modal_bridge/sync_nodes", json=body)
        return r.status, await r.text()

    st, text = harness._run(lambda c: go(c, {"new_baked": new}))
    assert st == 200 and _rc(text) == 0, text
    assert deployed[-1] == ["A", "B", "New"], f"云端独有的节点被这次同步删了: {deployed}"
    assert "已并回" in text
    st, text = harness._run(lambda c: go(c, {"new_baked": new, "prune": ["A"]}))
    assert _rc(text) == 0 and deployed[-1] == ["B", "New"], deployed


def test_sync_nodes_refuses_when_cloud_unreadable_and_local_nodes_would_vanish(monkeypatch):
    def unreadable(cfg, timeout=20):
        raise health_client.HealthUnavailable("unreachable", "网络断了")
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", unreadable)
    node_sync.write_baked_nodes([{"name": "X", "url": "https://x/X", "commit": "1"}])
    deployed = []
    monkeypatch.setattr(rt, "_run_streamed", _deployed_baked(deployed))
    new = [{"name": "New", "url": "https://x/New", "commit": "3"}]

    async def go(c, body):
        r = await c.post("/modal_bridge/sync_nodes", json=body)
        return r.status, r.content_type, await r.text()

    st, ctype, text = harness._run(lambda c: go(c, {"new_baked": new}))
    assert st == 409 and ctype == "application/json" and "X" in json.loads(text)["error"], text
    assert deployed == [] and [n["name"] for n in node_sync.read_baked_nodes()] == ["X"]
    st, _, text = harness._run(lambda c: go(c, {"new_baked": new, "prune": ["X"]}))
    assert st == 200 and _rc(text) == 0 and deployed == [["New"]], (text, deployed)


def test_sync_nodes_409_before_streaming_when_cloud_node_has_no_source(monkeypatch):
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes",
                        lambda cfg, timeout=20: (["Ghost"], [{"name": "Ghost", "url": "", "commit": ""}]))

    async def go(c):
        r = await c.post("/modal_bridge/sync_nodes", json={"new_baked": []})
        return r.status, await r.json()

    st, body = harness._run(go)
    assert st == 409 and "Ghost" in body["error"], body


def test_sync_nodes_not_deployed_is_a_fresh_deploy(_isolate):
    async def go(c):
        r = await c.post("/modal_bridge/sync_nodes", json={"new_baked": [
            {"name": "N", "url": "https://x/N", "commit": ""}]})
        return await r.text()

    assert _rc(harness._run(go)) == 0 and ("deploy", "comfyui-bridge") in _isolate.cmds


# ── R3:/submit 不在事件循环里扫模型 ──────────────────────────────────
def test_submit_scans_models_off_loop_and_only_once(monkeypatch):
    seen = {"extract": [], "pick": []}

    def extract(prompt):
        seen["extract"].append(threading.get_ident())
        return [{"type": "checkpoints", "filename": "m.safetensors"}]

    def pick(prompt, cfg, required=None):
        seen["pick"].append((threading.get_ident(), required))
        return "primary", "test"
    monkeypatch.setattr(rt, "extract_required_models", extract)
    monkeypatch.setattr(rt, "_pick_gpu_class", pick)
    sent = {}

    async def submit(session, cfg, **kw):
        sent.update(kw)
        return {"id": "job-1", "gpu": "H100"}
    monkeypatch.setattr(modal_client, "submit_job", submit)

    async def go(c, tier):
        harness._set_cfg(gpu_tier=tier)
        r = await c.post("/modal_bridge/submit", json={"prompt": {}, "local_nodes": {}})
        return r.status, await r.json()

    st, body = harness._run(lambda c: go(c, "auto"))
    assert st == 200 and body["job_id"] == "job-1", body
    assert len(seen["extract"]) == 1, "required 应只算一次"
    assert seen["extract"][0] != MAIN_THREAD and seen["pick"][0][0] != MAIN_THREAD, "扫描跑在事件循环线程里"
    assert seen["pick"][0][1] == [{"type": "checkpoints", "filename": "m.safetensors"}], "required 没往下传"
    seen["extract"].clear()
    harness._run(lambda c: go(c, "top"))
    assert seen["extract"] == [], "显式选档时不该扫描模型"


# ── C3:提交结果未知 ───────────────────────────────────────────────────
def test_submit_unknown_returns_job_id_for_verification(monkeypatch):
    class SubmitUnknown(RuntimeError):
        def __init__(self, msg, job_id):
            super().__init__(msg)
            self.job_id = job_id

    async def submit(session, cfg, **kw):
        raise SubmitUnknown("提交结果未知 job_id=j-u", "j-u")
    monkeypatch.setattr(modal_client, "submit_job", submit)

    async def go(c):
        harness._set_cfg(gpu_tier="primary")
        r = await c.post("/modal_bridge/submit", json={"prompt": {}, "local_nodes": {}})
        return r.status, await r.json()

    st, body = harness._run(go)
    assert st == 502 and body["job_id"] == "j-u" and body["outcome"] == "unknown", body


# ── R4 / C11:主档容量取 fallback 链里最小的卡 ─────────────────────────
def test_primary_capacity_uses_the_smallest_card_in_its_fallback_chain(monkeypatch):
    monkeypatch.setattr(rt, "_estimate_workflow_vram", lambda prompt, required=None: (110.0, "image", 0))
    cfg = {"gpu_tier": "auto", "default_gpu": "H200", "top_gpu": "B200", "cheap_gpu": "L40S"}
    cls, why = rt._pick_gpu_class({}, cfg)
    assert cls == "top", f"H200 档排不到会落到 H100(80G),110G 的工作流必须升档: {why}"
    monkeypatch.setattr(rt, "_estimate_workflow_vram", lambda prompt, required=None: (70.0, "image", 0))
    assert rt._pick_gpu_class({}, cfg)[0] == "primary"


def test_gpu_fallback_chain_matches_the_cloud():
    src = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_GPU_CHAIN" for t in n.targets))
    assert ast.literal_eval(node.value) == rt._GPU_FALLBACK_CHAIN, \
        "routes._GPU_FALLBACK_CHAIN 与 modal_app._GPU_CHAIN 不一致 —— 改了一边忘了另一边"


# ── R5 / C13:Volume 读不到 ≠ 没有 ──────────────────────────────────────
def _vol_down(cfg, max_age=60):
    raise local_nodes.VolumeUnavailable("Token invalid / network down")


def test_volume_unavailable_is_reported_not_treated_as_empty(monkeypatch):
    monkeypatch.setattr(local_nodes, "list_volume_local_nodes", _vol_down)
    monkeypatch.setattr(node_sync, "plan_node_sync",
                        lambda prompt, baked=None: {"expect_baked": ["priv"]})
    node_sync.write_local_node_reqs(["stale-dep==1"])

    async def go(c):
        r1 = await c.get("/modal_bridge/list_local_nodes")
        r2 = await c.post("/modal_bridge/check_nodes", json={"prompt": {}})
        return await r1.json(), await r2.json()

    lst, chk = harness._run(go)
    assert lst["ok"] is False and "Volume" in lst["error"], lst
    assert chk["local_remove"] == [] and "network down" in chk["volume_unchecked"], chk
    with pytest.raises(local_nodes.VolumeUnavailable):
        rt._compute_local_node_reqs({})      # 不再退回本机那份(可能是空的)清单
    monkeypatch.setattr(local_nodes, "list_volume_local_nodes", lambda cfg, max_age=60: [])
    assert rt._compute_local_node_reqs({}) == [], "确认 Volume 上没有私有节点 → 没有依赖"


def test_deploy_aborts_when_volume_unreadable(monkeypatch, _isolate):
    monkeypatch.setattr(local_nodes, "list_volume_local_nodes", _vol_down)

    async def go(c):
        r = await c.post("/modal_bridge/deploy", json=DEPLOY_BODY)
        return await r.text()

    text = harness._run(go)
    assert _rc(text) == 1 and "部署已中止" in _last_marked(text), text[-400:]
    assert _isolate.cmds == [], f"读不到 Volume 还是去写 Secret / 部署了: {_isolate.cmds}"
    assert "私有节点:云端没有" not in text, "读不到 Volume 被当成了「云端没有私有节点」"


# ── R6 / C14:产物大小校验 ────────────────────────────────────────────
def _vol_state(job, size=None):
    img = {"filename": "clip.mp4", "volume_path": f"_outputs/{job}/clip.mp4"}
    if size is not None:
        img["size_bytes"] = size
    return {"completed_at": 1.0, "images": [img]}


def test_short_download_never_leaves_a_formal_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_mod, "_config_path", lambda: tmp_path / "user/config.json")
    passed, removed = [], []

    def download(cfg, vp, local, expected_size=None):   # 不校验就 rename 的「老」实现
        passed.append(expected_size)
        Path(local).write_bytes(b"x" * 50)
        return 50
    monkeypatch.setattr(modal_volume, "download_volume_file", download)
    monkeypatch.setattr(modal_volume, "volume_file_size", lambda cfg, vp: 0)
    monkeypatch.setattr(modal_volume, "remove_volume_path", lambda cfg, vp: removed.append(vp))

    with pytest.raises(RuntimeError, match="incomplete"):
        asyncio.run(rt._write_results(_vol_state("job-s", 100), "job-s", "r", {}))
    out = tmp_path / "output/r/job-s"
    assert passed == [100], "云端给的 size_bytes 没传给下载函数"
    assert not (out / "clip.mp4").exists(), "半截文件以正式名留在 output/ 里"
    assert removed == []


def test_unverifiable_download_keeps_the_cloud_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_mod, "_config_path", lambda: tmp_path / "user/config.json")
    removed = []
    monkeypatch.setattr(modal_volume, "download_volume_file",
                        lambda cfg, vp, local, expected_size=None: Path(local).write_bytes(b"ok") and 2)
    monkeypatch.setattr(modal_volume, "volume_file_size", lambda cfg, vp: 0)
    monkeypatch.setattr(modal_volume, "remove_volume_path", lambda cfg, vp: removed.append(vp))
    outs = asyncio.run(rt._write_results(_vol_state("job-u"), "job-u", "r", {}))
    assert outs[0]["size_bytes"] == 2 and removed == [], "校验不了大小还删云端副本(契约 C12)"
    outs = asyncio.run(rt._write_results(_vol_state("job-v", 2), "job-v", "r", {}))
    assert removed == ["_outputs/job-v/clip.mp4"], "校验通过后应删除云端副本"


# ── R7:回执按 30 天清理 ────────────────────────────────────────────────
def test_old_receipts_are_pruned_on_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg_mod, "_config_path", lambda: tmp_path / "user/config.json")
    root = tmp_path / "user/download_receipts"
    root.mkdir(parents=True)
    old, fresh, other = root / "a.json", root / "b.json", root / "keep.txt"
    for f in (old, fresh, other):
        f.write_text("[]")
    t = time.time() - 31 * 86400
    os.utime(old, (t, t))
    os.utime(other, (t, t))
    asyncio.run(rt._write_results({"data_base64": "aGk=", "filename": "a.png"}, "job-r", "r", {}))
    assert not old.exists() and fresh.exists() and other.exists()
    assert ResultReceipts(root, []).prune() == 0


# ── R8:锁内重读 config ────────────────────────────────────────────────
def test_sync_nodes_uses_config_read_inside_the_lock(_isolate):
    harness._set_cfg(modal_app_name="old-app")

    async def go(c):
        await rt._DEPLOY_LOCK.acquire()          # 另一次 /deploy 正在跑
        req = asyncio.create_task(c.post("/modal_bridge/sync_nodes", json={
            "new_baked": [{"name": "n", "url": "https://x/n", "commit": ""}]}))
        await asyncio.sleep(0.2)
        cur = cfg_mod.load_config()
        cur["modal_app_name"] = "new-app"       # 那次部署换了 app 并写回 config
        cfg_mod.save_config(cur)
        rt._DEPLOY_LOCK.release()
        return await (await req).text()

    assert _rc(harness._run(go)) == 0
    assert _isolate.cmds == [("deploy", "new-app")], _isolate.cmds


def test_deploy_uses_config_written_while_it_was_queued(_isolate):
    harness._set_cfg(bridge_api_key="", modal_app_name="old-app")

    async def go(c):
        await rt._DEPLOY_LOCK.acquire()
        req = asyncio.create_task(c.post("/modal_bridge/deploy", json=DEPLOY_BODY))
        await asyncio.sleep(0.2)
        cur = cfg_mod.load_config()           # 排队期间另一次部署写回了 key 和 app 名
        cur.update(bridge_api_key="bk-from-the-other-deploy", modal_app_name="new-app")
        cfg_mod.save_config(cur)
        rt._DEPLOY_LOCK.release()
        return await (await req).text()

    assert _rc(harness._run(go)) == 0
    assert _isolate.secret_key_in_config == [("bk-from-the-other-deploy", "bk-from-the-other-deploy")], \
        "排队期间别处写下的 key 被这次部署换掉了 —— 之后所有请求 401"
    assert ("deploy", "new-app") in _isolate.cmds, f"部署用的还是拿锁前的 config: {_isolate.cmds}"
    assert cfg_mod.load_config()["modal_app_name"] == "new-app"


# ── C16:Secret 的位置与新 key 先落 config ─────────────────────────────
def test_secret_is_written_after_reconcile_and_new_key_is_saved_first(monkeypatch, _isolate):
    harness._set_cfg(bridge_api_key="")
    order = []
    monkeypatch.setattr(node_sync, "reconcile_baked_with_cloud",
                        lambda cfg: order.append("reconcile") or node_sync.Reconciled([], []))
    real_refresh = rt._refresh_local_node_reqs
    monkeypatch.setattr(rt, "_refresh_local_node_reqs", lambda cfg: order.append("reqs") or real_refresh(cfg))

    async def go(c):
        r = await c.post("/modal_bridge/deploy", json=DEPLOY_BODY)
        return await r.text()

    assert _rc(harness._run(go)) == 0
    assert order == ["reconcile", "reqs"]
    assert [k for k, _ in _isolate.cmds] == ["secret", "deploy", "compat"], _isolate.cmds
    key, saved = _isolate.secret_key_in_config[0]
    assert key.startswith("bk-") and key == saved, "新 key 没有在写 Secret 之前落 config"
    assert cfg_mod.load_config()["bridge_api_key"] == key


def test_deploy_blocked_writes_no_secret_and_one_abort_line(monkeypatch, _isolate, capsys):
    def blocked(cfg):
        raise node_sync.DeployBlocked("云端镜像装着 X,但拿不到来源,已中止。\n处理:在本机装上 X 后再部署")
    monkeypatch.setattr(node_sync, "reconcile_baked_with_cloud", blocked)

    async def go(c):
        r = await c.post("/modal_bridge/deploy", json=DEPLOY_BODY)
        return await r.text()

    text = harness._run(go)
    last = _last_marked(text)
    assert _rc(text) == 1 and _isolate.cmds == [], (text, _isolate.cmds)
    assert last.startswith("== ✗ 部署已中止:") and "处理:在本机装上 X" in last, last
    assert "处理:在本机装上 X" in capsys.readouterr().out, "完整说明要进 ComfyUI 控制台"


# ── C5:/sync_local_nodes 自动部署被拦也只有一行,且有 print ────────────
def test_auto_deploy_deploy_blocked_is_printed_and_single_line(monkeypatch, _isolate, capsys):
    def blocked(cfg):
        raise node_sync.DeployBlocked("读不到云端,本机清单是空的,已中止。\n处理:检查网络")
    monkeypatch.setattr(node_sync, "reconcile_baked_with_cloud", blocked)

    async def upload(resp, work):
        return {"uploaded": [], "failed": [], "digests": {"p": "d"}}
    monkeypatch.setattr(rt, "_run_blocking_streamed", upload)
    monkeypatch.setattr(rt, "_refresh_local_node_reqs", lambda cfg: ["dep==1"])

    async def deployed(cfg):
        return "old"
    monkeypatch.setattr(rt, "_deployed_reqs_hash", deployed)

    async def go(c):
        r = await c.post("/modal_bridge/sync_local_nodes", json={"folders": ["p"]})
        return await r.text()

    text = harness._run(go)
    assert _rc(text) == 1 and _isolate.cmds == []
    last = _last_marked(text)
    assert last.startswith("== ✗ 部署已中止:") and "处理:检查网络" in last, last
    assert "处理:检查网络" in capsys.readouterr().out


# ── R9:workspace / app 名校验,部署后 404 判失败 ──────────────────────
@pytest.mark.parametrize("override, needle", [
    ({"workspace": "https://modal.com/apps/myws"}, "粘贴了一整段 URL"),
    ({"workspace": "MyWS"}, "小写"),
    ({"app_name": "my_app"}, "app 名"),
    ({"workspace": "w" * 30, "app_name": "a" * 30}, "太长"),
])
def test_deploy_rejects_names_that_cannot_form_an_endpoint(override, needle, _isolate):
    async def go(c):
        r = await c.post("/modal_bridge/deploy", json={**DEPLOY_BODY, **override})
        return await r.text()

    text = harness._run(go)
    assert _rc(text) == 2 and needle in text, text
    assert _isolate.cmds == [] and cfg_mod.load_config()["modal_endpoint_base"] == "https://ws--app"


def test_deploy_fails_when_the_fresh_endpoint_404s(monkeypatch, _isolate):
    async def not_found(session, cfg):
        _isolate.health_calls += 1
        raise health_client.HealthUnavailable("not_deployed", "Modal /health 404")
    monkeypatch.setattr(modal_client, "health", not_found)

    async def go(c):
        r = await c.post("/modal_bridge/deploy", json=DEPLOY_BODY)
        return await r.text()

    text = harness._run(go)
    assert _rc(text) == 1 and "404" in _last_marked(text) and "workspace" in _last_marked(text), text[-500:]
    assert _isolate.health_calls == rt._HEALTH_404_TRIES, "刚部署完的 404 应先重试"
    assert ("compat", "comfyui-bridge") not in _isolate.cmds


# ── C4 / C7:/poll 与 /version 的 401 ─────────────────────────────────
def _with_fake_cloud(handler_map, fn):
    async def main():
        fake = web.Application()
        for path, (status, body) in handler_map.items():
            async def h(req, _s=status, _b=body):
                return web.Response(status=_s, text=_b, content_type="application/json")
            fake.router.add_get(path, h)
        fs = TestServer(fake)
        await fs.start_server()
        harness._set_cfg(modal_endpoint_base=f"http://127.0.0.1:{fs.port}/p")
        c = await harness._client()
        try:
            return await fn(c)
        finally:
            await c.close()
            await fs.close()
    return asyncio.run(main())


def test_poll_maps_cloud_http_errors_to_statuses():
    async def go(c):
        r = await c.get("/modal_bridge/poll?job_id=j1")
        return r.status, await r.json()

    st, body = _with_fake_cloud({"/p-status.modal.run": (401, '{"error": "unauthorized"}')}, go)
    assert st == 200 and body["status"] == "auth_failed" and "unauthorized" in body["error"], body
    st, body = _with_fake_cloud({"/p-status.modal.run": (503, "<html>gateway</html>")}, go)
    assert st == 200 and body["status"] == "unknown" and body["http_status"] == 503, body


def test_version_reports_unauthorized():
    async def go(c):
        r = await c.get("/modal_bridge/version")
        return await r.json()

    body = _with_fake_cloud({"/p-health.modal.run": (401, '{"error": "unauthorized"}')}, go)
    assert body["err_kind"] == "unauthorized", body


# ── R10 已由上面覆盖;R11:同步阻塞挪出事件循环 ─────────────────────────
def test_modal_available_and_git_completion_run_off_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(node_sync, "modal_available", lambda: seen.append(threading.get_ident()) or True)
    real_complete = node_sync.complete_baked_entries
    monkeypatch.setattr(node_sync, "complete_baked_entries",
                        lambda *a: seen.append(threading.get_ident()) or real_complete(*a))
    monkeypatch.setattr(node_sync, "plan_node_sync", lambda prompt, baked=None: {"expect_baked": []})

    async def ok_list(session, cfg):
        return {"custom_nodes": ["x"], "custom_nodes_manifest": []}
    monkeypatch.setattr(modal_client, "list_nodes", ok_list)

    async def go(c):
        await (await c.post("/modal_bridge/check_nodes", json={"prompt": {}})).json()
        await (await c.get("/modal_bridge/list_nodes")).json()
        await (await c.post("/modal_bridge/sync_nodes", json={"new_baked": []})).text()

    harness._run(go)
    assert len(seen) >= 3 and MAIN_THREAD not in seen, "modal_available / git 补全跑在事件循环线程里"


# ── R12:请求体与字段类型 ───────────────────────────────────────────────
def test_bad_bodies_are_400_not_500():
    async def go(c):
        out = {}
        for path, body in (("/modal_bridge/submit", {"prompt": {}, "tier": 5}),
                           ("/modal_bridge/submit", ["x"]),
                           ("/modal_bridge/fetch_result", ["x"]),
                           ("/modal_bridge/check_nodes", "x"),
                           ("/modal_bridge/sync_nodes", {"new_baked": ["x"]}),
                           ("/modal_bridge/sync_nodes", {"new_baked": [], "prune": [1]}),
                           ("/modal_bridge/remove_local_node", {"folder": 3}),
                           ("/modal_bridge/cancel", {"job_id": 3}),
                           ("/modal_bridge/deploy", {**DEPLOY_BODY, "scaledown_window": "abc"}),
                           ("/modal_bridge/deploy", {**DEPLOY_BODY, "app_name": 3})):
            r = await c.post(path, json=body)
            out[f"{path} {json.dumps(body)[:40]}"] = r.status
        r = await c.post("/modal_bridge/config", data="not json",
                         headers={"Content-Type": "application/json"})
        out["config invalid json"] = r.status
        for path, body in (("/modal_bridge/job_event", ["x"]),
                           ("/modal_bridge/job_event", {"job_id": 1, "detail": {"a": 1}})):
            r = await c.post(path, json=body)
            out[f"{path} {json.dumps(body)[:40]}"] = r.status
        return out

    out = harness._run(go)
    for k, st in out.items():
        if "job_event" in k and "detail" in k:
            assert st == 200, (k, st)       # 只是记日志:类型不对就转字符串记下
        else:
            assert st == 400, (k, st)


def test_streams_always_end_with_deploy_done(monkeypatch):
    def boom(cfg):
        raise ValueError("意外的内部错误")
    monkeypatch.setattr(rt, "_refresh_local_node_reqs", boom)

    async def go(c):
        r1 = await c.post("/modal_bridge/sync_models", json={"items": [{"type": None, "filename": None}]})
        r2 = await c.post("/modal_bridge/sync_nodes", json={"new_baked": []})
        return await r1.text(), await r2.text()

    t1, t2 = harness._run(go)
    assert _rc(t1) == 1 and "被拒" in t1, t1
    assert _rc(t2) == 1 and "意外的内部错误" in _last_marked(t2), t2


# ── F-P3-9:/sync_models 的 rc 与汇总 ─────────────────────────────────
def test_sync_models_rc_and_summary(monkeypatch):
    monkeypatch.setattr(rt, "_local_model_resolver", lambda: (
        lambda t, fn: None if fn == "gone.safetensors" else Path(f"/models/{t}/{fn}")))

    def upload(cfg, items, on_progress=None):
        return {"uploaded": [i for i in items if i["filename"] == "new.safetensors"],
                "skipped": [{**i, "reason": "already in volume"} for i in items
                            if i["filename"] == "have.safetensors"],
                "total_mb": 1}
    monkeypatch.setattr(modal_volume, "upload_models", upload)

    async def go(c, names):
        r = await c.post("/modal_bridge/sync_models", json={"items": [
            {"type": "checkpoints", "filename": n} for n in names]})
        return await r.text()

    text = harness._run(lambda c: go(c, ["new.safetensors", "have.safetensors", "gone.safetensors"]))
    assert _rc(text) == 1, text
    assert "1 个已同步、1 个已存在跳过、1 个被拒" in text and "gone.safetensors" in _last_marked(text), text
    text = harness._run(lambda c: go(c, ["have.safetensors"]))
    assert _rc(text) == 0 and "0 个已同步、1 个已存在跳过、0 个被拒" in text, text


# ── R14:/health 的匿名视图 ─────────────────────────────────────────────
def test_health_full_only_for_loopback_or_capability(_isolate):
    async def go(c):
        local = await (await c.get("/modal_bridge/health", headers={"X-Modal-Bridge-Capability": ""})).json()
        calls = _isolate.health_calls
        anon = await c.get("/modal_bridge/health",
                           headers={"Host": "bridge.example", "X-Modal-Bridge-Capability": ""})
        anon_body = await anon.json()
        anon_calls = _isolate.health_calls - calls
        remote = await (await c.get("/modal_bridge/health", headers={"Host": "bridge.example"})).json()
        return local, anon.status, anon_body, anon_calls, remote

    local, anon_st, anon, anon_calls, remote = harness._run(go)
    assert local["ok"] and "custom_nodes_manifest" in local["modal"], "本机直连行为不变"
    assert anon_st == 200 and anon_calls == 0, "匿名非本机请求不能唤醒云端"
    assert set(anon) <= {"ok", "healthy", "limited", "detail"} and anon["healthy"] is True, anon
    assert "custom_nodes_manifest" in remote["modal"], "带有效 capability 的应拿完整内容"


# ── C2:/cancel 的 ok / still_billing ───────────────────────────────────
@pytest.mark.parametrize("cloud, ok, billing", [
    ({"id": "j", "status": "cancelled", "was_running": True}, True, False),
    ({"id": "j", "status": "completed", "cancel_noop": True, "was_running": False}, True, False),
    ({"id": "j", "status": "not_found", "error": "job not found"}, True, False),
    ({"id": "j", "status": "running", "error": "cancel failed: boom", "was_running": True}, False, True),
])
def test_cancel_ok_means_not_billing(cloud, ok, billing, monkeypatch):
    async def cancel(session, cfg, job_id):
        return dict(cloud)
    monkeypatch.setattr(modal_client, "cancel", cancel)

    async def go(c):
        r = await c.post("/modal_bridge/cancel", json={"job_id": "j"})
        return r.status, await r.json()

    st, body = harness._run(go)
    assert st == 200 and body["ok"] is ok and body["still_billing"] is billing, body
    assert body["status"] == cloud["status"]


def test_cancel_transport_failure_is_still_billing(monkeypatch):
    async def cancel(session, cfg, job_id):
        raise RuntimeError("Modal /cancel 500")
    monkeypatch.setattr(modal_client, "cancel", cancel)

    async def go(c):
        r = await c.post("/modal_bridge/cancel", json={"job_id": "j"})
        return r.status, await r.json()

    st, body = harness._run(go)
    assert st == 502 and body["ok"] is False and body["still_billing"] is True, body


# ── C9 / C10 / C1 / C15 ────────────────────────────────────────────────
def test_bridge_key_route_is_gone():
    async def go(c):
        return (await c.get("/modal_bridge/bridge_key")).status

    assert harness._run(go) in (404, 405)


def test_clearing_the_url_in_settings_keeps_the_bypass_secret():
    harness._set_cfg(aigc_studio_base_url="https://site.app", aigc_bypass_secret="byp-1")

    async def go(c):
        return (await c.post("/modal_bridge/config", json={"aigc_studio_base_url": ""})).status

    assert harness._run(go) == 200
    cfg = cfg_mod.load_config()
    assert cfg["aigc_studio_base_url"] == "" and cfg["aigc_bypass_secret"] == "byp-1"


def test_job_id_rule_rejects_trailing_newline_and_leading_symbol():
    for bad in ("abc\n", "-x", ".x", "_x", "a" * 65):
        assert not contract.is_safe_job_id(bad), repr(bad)
    assert contract.is_safe_job_id("a" * 64) and contract.is_safe_job_id("0-a_b.c")


def test_generic_models_resolve_by_relative_path(monkeypatch):
    asked = []

    def full_path(t, fn):
        asked.append((t, fn))
        return f"/m/{t}/{fn}" if (t, fn) == ("unet", "flux/x.gguf") else None
    monkeypatch.setattr(rt, "folder_paths", types.SimpleNamespace(
        folder_names_and_paths={"checkpoints": None, "unet": None}, get_full_path=full_path))
    monkeypatch.setattr(rt.model_deps, "extract_generic_filenames", lambda prompt: {"flux/x.gguf"})
    monkeypatch.setattr(rt.model_deps, "extract_loader_models", lambda prompt: [])
    assert rt.extract_required_models({}) == [{"type": "unet", "filename": "flux/x.gguf"}], asked
    assert ("unet", "x.gguf") not in asked, "又按 basename 查了 —— 子目录里的模型永远找不到"
