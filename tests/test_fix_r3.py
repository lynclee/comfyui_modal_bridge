"""fix1005 第三轮(第二轮复核报出的 P2/P3)回归测试,2026-10-05。"""
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import bridge_cli  # noqa: E402
import contract  # noqa: E402
import node_sync  # noqa: E402


# ── AIGC:设置页把 URL 从有值清空 → 记进 pushed,部署时据此清 Secret(升级用户没有 pushed 记录) ──
def test_clearing_the_aigc_url_in_settings_marks_it_for_clearing_at_deploy():
    cur = {"aigc_studio_base_url": "https://studio.example.com"}
    out = contract.merge_public_config(cur, {"aigc_studio_base_url": ""})
    assert "aigc_base_url" in out[contract.AIGC_PUSHED_FIELD]
    # 从没配过的机器:不记,免得去清别的机器写的配置
    out2 = contract.merge_public_config({}, {"aigc_studio_base_url": ""})
    assert contract.AIGC_PUSHED_FIELD not in out2
    # 已有记录:并集,不重复
    cur3 = {**cur, contract.AIGC_PUSHED_FIELD: ["aigc_bypass_secret"]}
    out3 = contract.merge_public_config(cur3, {"aigc_studio_base_url": ""})
    assert sorted(out3[contract.AIGC_PUSHED_FIELD]) == ["aigc_base_url", "aigc_bypass_secret"]


def test_deploy_py_unions_the_pushed_record_instead_of_overwriting_it():
    src = (ROOT / "deploy.py").read_text(encoding="utf-8")
    i = src.index("contract.AIGC_PUSHED_FIELD: ")
    assert "set(_old) | set(_now)" in src[i:i + 120], "deploy.py 覆盖了 pushed 记录,GUI 部署要清的项会丢"


# ── bridge_cli:自定义 app 名照中止提示 configure --endpoint 后应能继续 ──
def test_saved_endpoint_with_the_app_suffix_counts_as_the_same_app():
    f = bridge_cli._saved_is_same_app
    assert f({"endpoint": "https://ws--my-bridge"}, "my-bridge")
    assert not f({"endpoint": "https://ws--other"}, "my-bridge")
    assert f({}, "comfyui-bridge"), "老文件按默认 app 算"
    assert f({"app_name": "a", "endpoint": "https://ws--b"}, "a")
    assert not f({"app_name": "a"}, "b")
    src = (ROOT / "bridge_cli.py").read_text(encoding="utf-8")
    assert "saved_same = _saved_is_same_app(saved, args.app_name)" in src


# ── cnr_dirty:基准用列出的 .py 的 mtime 中位数,只看 .py ──
def _cnr(tmp_path, files, tracking_age_s=0.0):
    d = tmp_path / "node"
    d.mkdir()
    now = time.time()
    for rel in files:
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        os.utime(f, (now, now))
    t = d / ".tracking"
    t.write_text("\n".join(files))
    os.utime(t, (now - tracking_age_s, now - tracking_age_s))
    return d, now


def test_cnr_copy_without_timestamps_is_not_dirty(tmp_path):
    """不保留时间戳的拷贝:.tracking 比所有文件都早(先落盘),以前全部 Registry 节点被误判改过。"""
    d, _ = _cnr(tmp_path, ["a.py", "b.py", "sub/c.py", "conf.json"], tracking_age_s=3600)
    assert not node_sync.cnr_dirty(d)


def test_cnr_edited_py_is_dirty_but_runtime_data_is_not(tmp_path):
    d, now = _cnr(tmp_path, ["a.py", "b.py", "c.py", "conf.json"])
    later = now + 86400
    os.utime(d / "conf.json", (later, later))   # 节点运行时改自己的配置:不算
    assert not node_sync.cnr_dirty(d)
    os.utime(d / "b.py", (later, later))        # 改了代码:算
    assert node_sync.cnr_dirty(d)


def test_cnr_added_or_removed_py_is_dirty(tmp_path):
    d, _ = _cnr(tmp_path, ["a.py", "b.py"])
    (d / "extra.py").write_text("x")
    assert node_sync.cnr_dirty(d)
    (d / "extra.py").unlink()
    (d / "b.py").unlink()
    assert node_sync.cnr_dirty(d)


# ── AIGC 完成回调带 warnings(契约 D1:AIGC Studio 走的是回调,看不到 /status) ──
def test_aigc_job_complete_callback_carries_the_warnings():
    import test_core as tc
    ad = tc._delivery_env()
    seen = []

    def poster(url, body, headers, timeout):
        if url.endswith("asset-intake"):
            return tc._ok_intake(body)
        seen.append(body)
        return 200, {"ok": True}

    def putter(put_url, path, headers, timeout):
        return 200, {"ETag": '"abc"'}
    refs = [{"filename": "a.png", "asset_type": "image"}]
    ad.deliver_outputs("j", refs, {"mode": "aigc-r2", "job_id": "j", "token": "TOK"},
                       warnings=["输出节点 50 (PreviewImage) 没有执行"],
                       poster=poster, putter=putter, streamer=tc._fake_streamer)
    assert seen[-1]["warnings"] == ["输出节点 50 (PreviewImage) 没有执行"]
    ad.deliver_outputs("j", refs, {"mode": "aigc-r2", "job_id": "j", "token": "TOK"},
                       poster=poster, putter=putter, streamer=tc._fake_streamer)
    assert "warnings" not in seen[-1], "没有 warnings 时不带这个字段"
    src = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    assert "provider_job_id=_call_id(job_id), warnings=_w)" in src


# ── 测试护栏:漏桩的 Modal 控制面调用立即失败、且 except Exception 吞不掉 ──
def test_unstubbed_modal_control_plane_call_fails_loudly():
    modal = pytest.importorskip("modal")
    t = time.time()
    with pytest.raises(pytest.fail.Exception):
        try:
            modal.Function.from_name("x", "y").get_web_url()
        except Exception:
            pass   # 业务代码常这么吞 —— 护栏必须穿透它
    assert time.time() - t < 10, "应当立刻失败,不是等 SDK 重试到超时"
