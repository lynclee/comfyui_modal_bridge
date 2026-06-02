"""
核心纯函数单测 —— 防回归(节点加/改/删规划、模型分类、下载中判定、VRAM 估算、ETA 格式)。

跑法(插件根目录):  python -m pytest tests/ -q
或不装 pytest:        python tests/test_core.py

只测不碰真实环境的纯逻辑;对依赖 ComfyUI(`import nodes`)/ 文件系统 / Modal 的点,用桩替换。
"""
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import node_sync  # noqa: E402
import modal_volume  # noqa: E402


# ============================================================================
# node_sync.plan_node_sync — 双向同步(加/改/删)规划
# ============================================================================
def _stub_analyze(monkey_by_folder, builtin=None, unresolved=None):
    """替换 analyze_workflow,直接给定工作流解析结果(绕开 import nodes)。"""
    def fake(prompt):
        return {"builtin": builtin or [], "by_folder": monkey_by_folder,
                "unresolved": unresolved or []}
    node_sync.analyze_workflow = fake


def _stub_env(git_map, exists_set):
    """git_map: folder->{has_git,url,commit};  exists_set: 本地仍存在的 folder 集合。"""
    node_sync.folder_git_info = lambda f: {"folder": f, **git_map.get(
        f, {"has_git": False, "url": None, "commit": None})}
    node_sync.folder_exists_locally = lambda f: f in exists_set


def _restore():
    import importlib
    importlib.reload(node_sync)


def test_plan_add_missing_node():
    """工作流用到、本地有 git、baked 没有 → add。"""
    _stub_analyze({"ComfyUI-KJNodes": ["KSamplerX"]})
    _stub_env({"ComfyUI-KJNodes": {"has_git": True, "url": "https://x/kj.git", "commit": "abc123"}},
              exists_set={"ComfyUI-KJNodes"})
    try:
        p = node_sync.plan_node_sync({}, baked=[])
        assert len(p["add"]) == 1 and p["add"][0]["folder"] == "ComfyUI-KJNodes"
        assert p["update"] == [] and p["prune"] == []
        assert p["needs_deploy"] is True
        names = [n["name"] for n in p["new_baked"]]
        assert names == ["ComfyUI-KJNodes"]
    finally:
        _restore()


def test_plan_update_on_commit_change():
    """baked 有但本地 commit 变了 → update,new_baked 用新 commit。"""
    _stub_analyze({"rgthree-comfy": ["NodeA"]})
    _stub_env({"rgthree-comfy": {"has_git": True, "url": "https://x/rg.git", "commit": "NEW"}},
              exists_set={"rgthree-comfy"})
    try:
        p = node_sync.plan_node_sync({}, baked=[{"name": "rgthree-comfy", "url": "https://x/rg.git", "commit": "OLD"}])
        assert len(p["update"]) == 1
        assert p["update"][0]["old_commit"] == "OLD" and p["update"][0]["commit"] == "NEW"
        assert p["add"] == [] and p["prune"] == []
        assert p["new_baked"][0]["commit"] == "NEW"
        assert p["needs_deploy"] is True
    finally:
        _restore()


def test_plan_prune_uninstalled():
    """baked 有、本地已卸载(且工作流没用到)→ prune,从 new_baked 移除。"""
    _stub_analyze({})  # 工作流没用任何 custom node
    _stub_env({}, exists_set=set())  # gone-node 本地不存在了
    try:
        p = node_sync.plan_node_sync({}, baked=[{"name": "gone-node", "url": "u", "commit": "c"}])
        assert [x["name"] for x in p["prune"]] == ["gone-node"]
        assert p["new_baked"] == []
        assert p["needs_deploy"] is True
    finally:
        _restore()


def test_plan_noop_when_in_sync():
    """工作流用到的节点 baked 已有、commit 一致、本地都在 → 无需部署。"""
    _stub_analyze({"ComfyUI_essentials": ["E1"]})
    _stub_env({"ComfyUI_essentials": {"has_git": True, "url": "u", "commit": "same"}},
              exists_set={"ComfyUI_essentials"})
    try:
        p = node_sync.plan_node_sync({}, baked=[{"name": "ComfyUI_essentials", "url": "u", "commit": "same"}])
        assert p["add"] == [] and p["update"] == [] and p["prune"] == []
        assert p["needs_deploy"] is False
        assert p["ok_baked"] == 1
    finally:
        _restore()


def test_plan_missing_no_git():
    """工作流用到、baked 没有、本地也没 git 信息 → 进 missing_no_git,不算 add。"""
    _stub_analyze({"weird-node": ["W1"]})
    _stub_env({"weird-node": {"has_git": False, "url": None, "commit": None}},
              exists_set={"weird-node"})
    try:
        p = node_sync.plan_node_sync({}, baked=[])
        assert p["add"] == []
        assert [x["folder"] for x in p["missing_no_git"]] == ["weird-node"]
        # 没 git 信息进不了 new_baked,也不该触发部署
        assert p["needs_deploy"] is False
    finally:
        _restore()


# ============================================================================
# node_sync.write/read_baked_nodes — 往返
# ============================================================================
def test_baked_roundtrip(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp())
    node_sync.DATA_FILE = d / "_custom_nodes_data.py"
    try:
        nodes = [{"name": "A", "url": "https://a.git", "commit": "111"},
                 {"name": "B", "url": "https://b.git", "commit": ""}]
        node_sync.write_baked_nodes(nodes)
        back = node_sync.read_baked_nodes()
        assert back == nodes
    finally:
        _restore()


# ============================================================================
# modal_volume.check_models — present / missing_local / downloading / missing_no_source
# ============================================================================
def test_check_models_classification(monkeypatch=None):
    cfg = {}
    # Volume 已有:vae/ae.safetensors
    modal_volume.volume_files_by_type = lambda c, types: {
        "vae": {"ae.safetensors"}, "unet": set(), "loras": set()}
    fs = modal_volume.file_in_progress
    modal_volume.file_in_progress = lambda p, settle_check=True: "downloading" in str(p)

    # resolver: unet/present_local 有本地、unet/dl 在下载中、loras/none 本地没有
    def resolver(t, fn):
        if fn == "present_local.safetensors":
            return Path("/local/unet/present_local.safetensors")
        if fn == "dl.safetensors":
            return Path("/local/unet/downloading/dl.safetensors")
        return None
    # find_local 的 stat 会被调用 → 桩掉 size
    class _P:
        def __init__(s, n): s.n = n
        def __str__(s): return s.n      # file_in_progress 桩按 str(path) 判 "downloading"
        def stat(s): return types.SimpleNamespace(st_size=1024 * 1024 * 10)
    orig_resolver = resolver

    required = [
        {"type": "vae", "filename": "ae.safetensors"},           # present
        {"type": "unet", "filename": "present_local.safetensors"},  # missing_local
        {"type": "unet", "filename": "dl.safetensors"},          # downloading
        {"type": "loras", "filename": "none.safetensors"},       # missing_no_source
    ]
    try:
        # 让 missing_local 分支的 .stat() 不真去读盘
        import os
        def fake_resolver(t, fn):
            p = orig_resolver(t, fn)
            return _P(str(p)) if p is not None else None
        r = modal_volume.check_models(cfg, required, fake_resolver)
        assert [x["filename"] for x in r["present"]] == ["ae.safetensors"]
        assert [x["filename"] for x in r["missing_local"]] == ["present_local.safetensors"]
        assert [x["filename"] for x in r["downloading"]] == ["dl.safetensors"]
        assert [x["filename"] for x in r["missing_no_source"]] == ["none.safetensors"]
    finally:
        modal_volume.file_in_progress = fs
        import importlib; importlib.reload(modal_volume)


# ============================================================================
# modal_volume.file_in_progress / _has_inprogress_sibling
# ============================================================================
def test_file_in_progress_zero_byte(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "m.safetensors"; f.write_bytes(b"")
    assert modal_volume.file_in_progress(f, settle_check=False) is True  # 0 字节 = 在下


def test_file_in_progress_with_aria2_sibling():
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "m.safetensors"; f.write_bytes(b"x" * 100)
    (d / "m.safetensors.aria2").write_bytes(b"ctrl")
    assert modal_volume.file_in_progress(f, settle_check=False) is True  # 有 .aria2 控制文件


def test_file_in_progress_complete():
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "m.safetensors"; f.write_bytes(b"x" * 100)
    assert modal_volume.file_in_progress(f, settle_check=False) is False  # 正常完成


# ============================================================================
# modal_volume._fmt_eta
# ============================================================================
def test_fmt_eta():
    assert modal_volume._fmt_eta(45) == "45s"
    assert modal_volume._fmt_eta(90) == "1m30s"
    assert modal_volume._fmt_eta(3725) == "1h02m"
    assert modal_volume._fmt_eta(-5) == "0s"


# ============================================================================
# 无 pytest 时的简易运行器
# ============================================================================
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}  — {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}  — ERROR {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
