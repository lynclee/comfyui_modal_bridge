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
import model_deps  # noqa: E402
import contract  # noqa: E402
import categories  # noqa: E402
import config  # noqa: E402


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


def test_plan_prune_default_keeps():
    """默认 allow_prune=False(多机并集):本地没有的列为 prune 候选,但不真删、不触发部署。"""
    _stub_analyze({})  # 工作流没用任何 custom node
    _stub_env({}, exists_set=set())  # gone-node 本地不存在了
    try:
        p = node_sync.plan_node_sync({}, baked=[{"name": "gone-node", "url": "u", "commit": "c"}])
        assert [x["name"] for x in p["prune"]] == ["gone-node"]  # 列为候选
        assert [n["name"] for n in p["new_baked"]] == ["gone-node"]  # 但仍保留
        assert p["needs_deploy"] is False  # 不因 prune 触发部署
    finally:
        _restore()


def test_plan_prune_when_allowed():
    """allow_prune=True(手动清理面板):本地没有的真从 new_baked 移除并触发部署。"""
    _stub_analyze({})
    _stub_env({}, exists_set=set())
    try:
        p = node_sync.plan_node_sync({}, baked=[{"name": "gone-node", "url": "u", "commit": "c"}],
                                     allow_prune=True)
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


def test_ensure_baked_file_creates_when_absent():
    """_custom_nodes_data.py 是 gitignore 的本地状态:缺失时 ensure 写空清单(供部署/打包用)。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    node_sync.DATA_FILE = d / "_custom_nodes_data.py"
    try:
        assert not node_sync.DATA_FILE.exists()
        node_sync.ensure_baked_file()
        assert node_sync.DATA_FILE.exists()
        assert node_sync.read_baked_nodes() == []  # 空清单且可被正常解析
    finally:
        _restore()


def test_ensure_baked_file_keeps_existing():
    """已存在则不覆盖(不能把同步好的清单清空)。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    node_sync.DATA_FILE = d / "_custom_nodes_data.py"
    try:
        nodes = [{"name": "X", "url": "https://x.git", "commit": "c"}]
        node_sync.write_baked_nodes(nodes)
        node_sync.ensure_baked_file()
        assert node_sync.read_baked_nodes() == nodes
    finally:
        _restore()


# ============================================================================
# node_sync.folder_git_info — .git 主路径 + pyproject 兜底(CNR / 压缩包装的节点)
# ============================================================================
def test_pyproject_repo_url_extracts_github():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "pyproject.toml").write_text(
        '[project]\nname = "ComfyUI-GGUF"\n\n[project.urls]\n'
        'Repository = "https://github.com/city96/ComfyUI-GGUF"\n', encoding="utf-8")
    assert node_sync._pyproject_repo_url(d) == "https://github.com/city96/ComfyUI-GGUF"


def test_pyproject_repo_url_sanitizes_subpath():
    """Homepage 指向 /tree/main#readme 之类 → 截回 owner/repo 这一层。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "pyproject.toml").write_text(
        '[project.urls]\nHomepage = "https://github.com/a/b/tree/main#readme"\n', encoding="utf-8")
    assert node_sync._pyproject_repo_url(d) == "https://github.com/a/b"


def test_pyproject_repo_url_none_when_absent():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert node_sync._pyproject_repo_url(d) is None


def test_folder_git_info_fallback_to_pyproject():
    """没有 .git 但 pyproject 有仓库地址 → has_git=True、url 解析出、commit 留空。"""
    import tempfile
    root = Path(tempfile.mkdtemp())
    nd = root / "custom_nodes" / "ComfyUI-GGUF"
    nd.mkdir(parents=True)
    (nd / "pyproject.toml").write_text(
        '[project.urls]\nRepository = "https://github.com/city96/ComfyUI-GGUF"\n', encoding="utf-8")
    node_sync._comfyui_root = lambda: root
    node_sync._git = lambda args, cwd: None  # 模拟无 .git
    try:
        info = node_sync.folder_git_info("ComfyUI-GGUF")
        assert info["has_git"] is True
        assert info["url"] == "https://github.com/city96/ComfyUI-GGUF"
        assert info["commit"] == ""
    finally:
        _restore()


def test_folder_git_info_none_when_no_metadata():
    """没有 .git 也没有 pyproject → has_git=False(仍归 missing_no_git)。"""
    import tempfile
    root = Path(tempfile.mkdtemp())
    (root / "custom_nodes" / "weird-node").mkdir(parents=True)
    node_sync._comfyui_root = lambda: root
    node_sync._git = lambda args, cwd: None
    try:
        info = node_sync.folder_git_info("weird-node")
        assert info["has_git"] is False and info["url"] is None
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
        import importlib
        importlib.reload(modal_volume)


# ============================================================================
# modal_volume.file_in_progress / _has_inprogress_sibling
# ============================================================================
def test_file_in_progress_zero_byte(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "m.safetensors"
    f.write_bytes(b"")
    assert modal_volume.file_in_progress(f, settle_check=False) is True  # 0 字节 = 在下


def test_file_in_progress_with_aria2_sibling():
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "m.safetensors"
    f.write_bytes(b"x" * 100)
    (d / "m.safetensors.aria2").write_bytes(b"ctrl")
    assert modal_volume.file_in_progress(f, settle_check=False) is True  # 有 .aria2 控制文件


def test_file_in_progress_complete():
    import tempfile
    d = Path(tempfile.mkdtemp())
    f = d / "m.safetensors"
    f.write_bytes(b"x" * 100)
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
# model_deps — 模型解析(LOADER_MAP 命中 + 通用扩展名兜底)
# ============================================================================
def test_loader_models_flux2():
    """flux2 风格:UNETLoader / DualCLIPLoader / VAELoader → 正确 type + filename。"""
    prompt = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux2_dev_fp8.safetensors"}},
        "2": {"class_type": "DualCLIPLoader",
              "inputs": {"clip_name1": "a.safetensors", "clip_name2": "b.safetensors"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
    }
    pairs = {(m["type"], m["filename"]) for m in model_deps.extract_loader_models(prompt)}
    assert ("diffusion_models", "flux2_dev_fp8.safetensors") in pairs
    assert ("text_encoders", "a.safetensors") in pairs
    assert ("text_encoders", "b.safetensors") in pairs
    assert ("vae", "ae.safetensors") in pairs


def test_generic_catches_unknown_loader():
    """不在 LOADER_MAP 的节点,但 input 指向模型文件 → 通用兜底捕获(取 basename)。"""
    prompt = {"9": {"class_type": "SomeFutureLoader",
                    "inputs": {"weird_field": "models/sub/cool_model.gguf", "x": 7}}}
    assert model_deps.extract_generic_filenames(prompt) == {"cool_model.gguf"}


def test_generic_ignores_images_and_nonmodel():
    """LoadImage 的 .png / 普通文本 input 不被通用兜底误中(扩展名集合是模型专属)。"""
    prompt = {"1": {"class_type": "LoadImage", "inputs": {"image": "ref.png"}},
              "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}}}
    assert model_deps.extract_generic_filenames(prompt) == set()


# ============================================================================
# categories — 工作流类别画像(显存 / 时长按类别)
# ============================================================================
def test_classify_video_by_savevideo():
    """工作流含 SaveVideo / VHS_VideoCombine → 归 video。"""
    assert categories.classify(
        {"1": {"class_type": "SaveVideo", "inputs": {}}}) == "video"
    assert categories.classify(
        {"9": {"class_type": "VHS_VideoCombine", "inputs": {}}}) == "video"


def test_classify_image_default():
    """没有视频输出节点 → 默认 image。"""
    assert categories.classify(
        {"1": {"class_type": "SaveImage", "inputs": {}},
         "2": {"class_type": "KSampler", "inputs": {}}}) == "image"
    assert categories.classify({}) == "image"


def test_estimate_vram_video_has_overhead():
    """同样权重大小,video 估算应高于 image(多帧激活开销 + 更大系数)。"""
    img = categories.estimate_vram_gb(10.0, "image")
    vid = categories.estimate_vram_gb(10.0, "video")
    assert vid > img
    assert img == 10.0 * 1.15            # image: 纯权重×系数,无额外开销
    assert vid == 10.0 * 1.3 + 8.0       # video: 权重×系数 + 多帧开销


def test_config_default_covers_slowest_category():
    """配置默认的 worker 超时上限必须 ≥ 最慢类别的时长 —— 否则视频会被提前杀。
    加了更慢的新类别却忘了抬高默认值,这条会失败(强制同步)。"""
    assert config.DEFAULT_CONFIG["worker_timeout_sec"] >= categories.max_worker_timeout_s()


# ============================================================================
# contract.compute_contract — 版本 / GPU 契约
# ============================================================================
def test_contract_version_match():
    c = contract.compute_contract("0.2.9", "0.2.9", True, "H100", "H100")
    assert c["match"] is True and c["gpu_match"] is True


def test_contract_version_mismatch():
    c = contract.compute_contract("0.2.9", "0.2.8", True, "H100", "H100")
    assert c["match"] is False


def test_contract_unreachable_not_blocked_on_gpu():
    """不可达 → match=False,但显卡不拦(交版本契约先逼一次重部署)。"""
    c = contract.compute_contract("0.2.9", None, False, "L40S", None)
    assert c["match"] is False and c["reachable"] is False
    assert c["gpu_match"] is True


def test_contract_gpu_mismatch_blocks():
    """版本一致但所选显卡 ≠ 云端在跑 → gpu_match=False(前端据此拦 + 逼重部署)。"""
    c = contract.compute_contract("0.2.9", "0.2.9", True, "L40S", "H100")
    assert c["match"] is True and c["gpu_match"] is False


def test_contract_old_image_gpu_none_not_blocked():
    """老镜像不上报 deployed_gpu(None)→ 不拦显卡。"""
    c = contract.compute_contract("0.2.9", "0.2.9", True, "L40S", None)
    assert c["gpu_match"] is True


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
