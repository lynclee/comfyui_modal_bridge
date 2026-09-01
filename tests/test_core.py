"""
核心纯函数单测 —— 防回归(节点加/改/删规划、模型分类、下载中判定、VRAM 估算、ETA 格式)。

跑法(插件根目录):  python -m pytest tests/ -q
或不装 pytest:        python tests/test_core.py

只测不碰真实环境的纯逻辑;对依赖 ComfyUI(`import nodes`)/ 文件系统 / Modal 的点,用桩替换。
"""
import sys
import subprocess
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
import workflow_check  # noqa: E402


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
        assert p["expect_baked"] == ["ComfyUI-KJNodes"]
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
        assert p["expect_baked"] == ["rgthree-comfy"]
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
        assert p["expect_baked"] == ["ComfyUI_essentials"]
    finally:
        _restore()


def test_plan_no_git_goes_local_pack():
    """无 git 信息但目录在本地 → 走本地打包通道(local_pack),不算 add、不触发部署。
    (0.7.5 前这里判 missing_no_git「补不了」;现在有 Volume 打包通道,能补了。)"""
    _stub_analyze({"weird-node": ["W1"]})
    _stub_env({"weird-node": {"has_git": False, "url": None, "commit": None}},
              exists_set={"weird-node"})
    try:
        p = node_sync.plan_node_sync({}, baked=[])
        assert p["add"] == []
        assert [x["folder"] for x in p["local_pack"]] == ["weird-node"]
        assert p["missing_no_git"] == []
        assert p["needs_deploy"] is False, "本地通道是运行时挂载,不该重 build 镜像"
        assert p["needs_local_upload"] is True
    finally:
        _restore()


def test_plan_missing_no_git_only_when_not_a_dir():
    """目录都不在(单文件节点 / 解析异常)→ 才是真的补不了,进 missing_no_git。"""
    _stub_analyze({"single_file.py": ["S1"]})
    _stub_env({"single_file.py": {"has_git": False, "url": None, "commit": None}},
              exists_set=set())  # folder_exists_locally → False
    try:
        p = node_sync.plan_node_sync({}, baked=[])
        assert p["local_pack"] == []
        assert [x["folder"] for x in p["missing_no_git"]] == ["single_file.py"]
        assert p["missing_no_git"][0]["reason"] == "not_a_directory"
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
# workflow_check.find_missing_required_inputs
# ============================================================================
def _req_getter(mapping):
    """把 {class_type: {必填名}} 包成 required_getter;未知类返回 None(跳过)。"""
    return lambda ct: mapping.get(ct)


def test_missing_required_catches_absent_widget():
    """老图节点缺了新版必填 widget(generate_type)→ 命中。"""
    prompt = {
        "2": {"class_type": "TencentImageToModelNode",
              "inputs": {"model": "3.0", "image": ["1", 0], "face_count": 500000, "seed": 0}},
    }
    req = {"TencentImageToModelNode": {"model", "image", "face_count", "generate_type", "seed"}}
    out = workflow_check.find_missing_required_inputs(prompt, _req_getter(req))
    assert len(out) == 1
    assert out[0]["node_id"] == "2"
    assert out[0]["class_type"] == "TencentImageToModelNode"
    assert out[0]["missing"] == ["generate_type"]


def test_missing_required_none_when_all_present():
    """必填项都在(widget 值或连线都算已提供)→ 不报。"""
    prompt = {
        "2": {"class_type": "TencentImageToModelNode",
              "inputs": {"model": "3.0", "image": ["1", 0], "face_count": 500000,
                         "generate_type": "Normal", "seed": 0}},
    }
    req = {"TencentImageToModelNode": {"model", "image", "face_count", "generate_type", "seed"}}
    assert workflow_check.find_missing_required_inputs(prompt, _req_getter(req)) == []


def test_missing_required_skips_unknown_class():
    """拿不到定义的节点(getter 返回 None)→ 跳过,不误报。"""
    prompt = {"5": {"class_type": "SomeUnknownNode", "inputs": {}}}
    assert workflow_check.find_missing_required_inputs(prompt, _req_getter({})) == []


def test_missing_required_ignores_autogrow_expanded_inputs():
    """V3 Autogrow 动态输入组:INPUT_TYPES() 的 required 里是模板名(values),
    prompt 里却是展开名(values.a / values.b)→ 已接上就不该报缺。
    真实案例:内置 ComfyMathExpression(comfy_extras/nodes_math.py)。"""
    prompt = {
        "105:107": {"class_type": "ComfyMathExpression",
                    "inputs": {"values.a": ["105:111", 0], "values.b": ["105:120", 0],
                               "expression": "max(5, round(a * 24))"}},
    }
    req = {"ComfyMathExpression": {"expression", "values"}}
    assert workflow_check.find_missing_required_inputs(prompt, _req_getter(req)) == []


def test_missing_required_still_catches_empty_autogrow():
    """Autogrow 组一个展开项都没有(min=1 要求至少一项)→ 仍要报缺。"""
    prompt = {"7": {"class_type": "ComfyMathExpression",
                    "inputs": {"expression": "a + b"}}}
    req = {"ComfyMathExpression": {"expression", "values"}}
    out = workflow_check.find_missing_required_inputs(prompt, _req_getter(req))
    assert len(out) == 1
    assert out[0]["missing"] == ["values"]


def test_missing_required_prefix_match_is_not_substring_match():
    """前缀豁免必须以 `名字.` 为界,不能被同前缀的无关输入(valuesX)顶掉。"""
    prompt = {"8": {"class_type": "N", "inputs": {"valuesX": 1, "values_b": 2}}}
    req = {"N": {"values"}}
    out = workflow_check.find_missing_required_inputs(prompt, _req_getter(req))
    assert len(out) == 1
    assert out[0]["missing"] == ["values"]


def _out_getter(output_types):
    """把 {会被当作 OUTPUT_NODE 的 class_type} 包成 is_output_getter。"""
    return lambda ct: (ct in output_types) if ct else None


def test_missing_required_skips_dangling_node():
    """输出悬空的节点不参与执行(ComfyUI 只跑 OUTPUT_NODE 的依赖闭包)→ 不该拦。
    真实案例:画布上顺手拖进来还没接线的 ImageScaleToTotalPixels,云端跑得好好的,
    预检却报它缺 image。"""
    prompt = {
        "92": {"class_type": "SaveVideo", "inputs": {"video": ["91", 0]}},
        "91": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
        "11": {"class_type": "VAELoader", "inputs": {"vae_name": "x.safetensors"}},
        # 缺 image,但输出没接到任何地方 → ComfyUI 根本不执行它
        "119": {"class_type": "ImageScaleToTotalPixels",
                "inputs": {"upscale_method": "lanczos", "megapixels": 1.0}},
    }
    req = {"ImageScaleToTotalPixels": {"image", "upscale_method", "megapixels"},
           "SaveVideo": {"video"}}
    out = workflow_check.find_missing_required_inputs(
        prompt, _req_getter(req), _out_getter({"SaveVideo"}))
    assert out == []


def test_missing_required_still_catches_reachable_node():
    """同样缺 image,但接进了输出链 → 必须照报。"""
    prompt = {
        "92": {"class_type": "SaveVideo", "inputs": {"video": ["119", 0]}},
        "119": {"class_type": "ImageScaleToTotalPixels",
                "inputs": {"upscale_method": "lanczos", "megapixels": 1.0}},
    }
    req = {"ImageScaleToTotalPixels": {"image", "upscale_method", "megapixels"},
           "SaveVideo": {"video"}}
    out = workflow_check.find_missing_required_inputs(
        prompt, _req_getter(req), _out_getter({"SaveVideo"}))
    assert len(out) == 1
    assert out[0]["node_id"] == "119"
    assert out[0]["missing"] == ["image"]


def test_missing_required_falls_back_when_no_output_node():
    """一个 OUTPUT_NODE 都识别不出来(拿不到定义等)→ 退回全量检查,不因优化而漏报。"""
    prompt = {"119": {"class_type": "ImageScaleToTotalPixels", "inputs": {"megapixels": 1.0}}}
    req = {"ImageScaleToTotalPixels": {"image", "megapixels"}}
    out = workflow_check.find_missing_required_inputs(
        prompt, _req_getter(req), _out_getter(set()))
    assert len(out) == 1
    assert out[0]["missing"] == ["image"]


def test_reachable_follows_widget_values_safely():
    """inputs 里的标量 widget 值不能被当成连线(否则遍历会串到不相干的节点)。"""
    prompt = {
        "1": {"class_type": "SaveVideo", "inputs": {"video": ["2", 0], "fps": 24}},
        "2": {"class_type": "CreateVideo", "inputs": {"images": ["3", 0]}},
        "3": {"class_type": "VAEDecode", "inputs": {}},
        "24": {"class_type": "Dangling", "inputs": {}},  # id 恰好等于上面的 fps 值
    }
    got = workflow_check.reachable_from_outputs(prompt, _out_getter({"SaveVideo"}))
    assert got == {"1", "2", "3"}          # "24" 不该因为 fps=24 被拉进来


def test_missing_required_sorted_by_node_id():
    """多个缺失节点按 node_id 排序返回。"""
    prompt = {
        "10": {"class_type": "N", "inputs": {}},
        "3": {"class_type": "N", "inputs": {}},
    }
    req = {"N": {"a"}}
    out = workflow_check.find_missing_required_inputs(prompt, _req_getter(req))
    assert [r["node_id"] for r in out] == ["10", "3"]  # 字符串排序,稳定即可
    assert all(r["missing"] == ["a"] for r in out)


# ============================================================================
# node_sync.secret_create_cmd — comfy.org API key(API 节点鉴权)进 secret
# ============================================================================
def test_secret_cmd_includes_comfy_api_key():
    """传了 comfy.org API key → secret 命令带 COMFY_API_KEY_COMFY_ORG;没传则不出现(不污染普通部署)。"""
    cmd = node_sync.secret_create_cmd({"modal_app_name": "comfyui-bridge"},
                                      bridge_key="bk-x", comfy_api_key="comfy-KEY")
    assert any("COMFY_API_KEY_COMFY_ORG=comfy-KEY" in a for a in cmd)
    cmd2 = node_sync.secret_create_cmd({"modal_app_name": "comfyui-bridge"}, bridge_key="bk-x")
    assert not any("COMFY_API_KEY" in a for a in cmd2)


def test_secret_cmd_includes_aigc_studio():
    """aigc-r2 交付配置进 secret;没配则不出现;R2 长期密钥任何情况下都不该出现。"""
    cmd = node_sync.secret_create_cmd({"modal_app_name": "comfyui-bridge"}, bridge_key="bk-x",
                                      aigc_base_url="https://studio.example",
                                      aigc_bypass_secret="byp-1")
    assert any("AIGC_STUDIO_BASE_URL=https://studio.example" in a for a in cmd)
    assert any("AIGC_STUDIO_BYPASS_SECRET=byp-1" in a for a in cmd)
    assert not any("R2_ACCESS_KEY" in a or "R2_SECRET" in a for a in cmd)
    cmd2 = node_sync.secret_create_cmd({"modal_app_name": "comfyui-bridge"}, bridge_key="bk-x")
    assert not any("AIGC_STUDIO" in a for a in cmd2)


# ============================================================================
# node_sync.redact_cmd — 命令行回显打码(部署面板会把命令流给浏览器)
# ============================================================================
def test_redact_cmd_masks_credentials():
    """secret create 的 argv 里是明文凭据,回显必须打码 —— 用户复制部署日志求助就泄漏了。"""
    cmd = node_sync.secret_create_cmd(
        {"modal_app_name": "comfyui-bridge"}, hf_token="hf_SECRETVALUE123",
        civitai_token="civ_abcdef", bridge_key="bk-0123456789abcdef",
        comfy_api_key="comfy-XYZ", aigc_base_url="https://studio.example",
        aigc_bypass_secret="byp-topsecret")
    out = node_sync.redact_cmd(cmd)
    # 凭据原文一个都不许出现
    for leak in ("hf_SECRETVALUE123", "civ_abcdef", "bk-0123456789abcdef",
                 "comfy-XYZ", "byp-topsecret"):
        assert leak not in out, f"泄漏了 {leak}: {out}"
    # key 名要留着(不然日志失去排查价值),够长的露前 4 位 + 长度
    assert "BRIDGE_API_KEY=bk-0***(len=19)" in out
    assert "HF_TOKEN=hf_S***(len=17)" in out
    # 短值一位都不露(8 位的东西露 4 位等于露一半)
    assert "COMFY_API_KEY_COMFY_ORG=***(len=9)" in out
    assert "CIVITAI_TOKEN=***(len=10)" in out
    # 非凭据项保持明文:URL 是排查部署问题的关键信息,打码反而添乱
    assert "AIGC_STUDIO_BASE_URL=https://studio.example" in out
    # 命令本体不受影响
    assert "secret" in out and "create" in out


def test_redact_cmd_leaves_plain_args():
    """普通命令(deploy)没有 KEY=VALUE,应原样输出;命中敏感词则宁滥勿缺。"""
    assert node_sync.redact_cmd(node_sync.deploy_command()).endswith("deploy modal_app.py")
    assert node_sync.redact_cmd(["a", "MODE=fast", "TOKEN="]) == "a MODE=fast TOKEN=(empty)"
    # 名字里带 SECRET 就打码,哪怕它其实不是密钥 —— 漏打一个才是事故
    assert node_sync.redact_cmd(["NOT_SECRET=1"]) == "NOT_SECRET=***(len=1)"


# ============================================================================
# contract.is_safe_job_id — job_id 拼本地路径前的白名单
# ============================================================================
def test_is_safe_job_id():
    """job_id 会拼进 output/<subfolder>/<job_id>/,路径穿越必须挡住。"""
    import uuid
    assert contract.is_safe_job_id(str(uuid.uuid4()))       # 云端真实形态
    assert contract.is_safe_job_id("job_1.2-3")
    for evil in ("../../x", "..", "a/b", "a\\b", "", "x" * 65, None, 123,
                 "a\x00b", "a b", "a/../b"):
        assert not contract.is_safe_job_id(evil), f"必须拒绝: {evil!r}"


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


def test_extract_pixels_frames_literal_whl():
    """节点同时带 width/height/length 字面量(H3 的 EmptyMiniMaxH3LatentAV 形态)→ 直接取。"""
    p = {"1": {"class_type": "EmptyMiniMaxH3LatentAV",
               "inputs": {"width": 1280, "height": 736, "length": 362}}}
    px, f = categories.extract_pixels_frames(p)
    assert px == 1280 * 736 and f == 362


def test_extract_pixels_frames_linked_wh_megapixels_fallback():
    """W/H 是连线(引用列表)拿不到字面量 → 退到图里 megapixels 字面量 ×1e6,帧数取最大帧字面量。"""
    p = {
        "1": {"class_type": "ResolutionSelector",
              "inputs": {"aspect_ratio": "16:9 (Widescreen)", "megapixels": 0.9, "multiple": 32}},
        "2": {"class_type": "MiniMaxH3ImageToVideo",
              "inputs": {"width": ["1", 0], "height": ["1", 1], "length": 362}},
    }
    px, f = categories.extract_pixels_frames(p)
    assert px == 0.9e6 and f == 362


def test_extract_pixels_frames_none():
    """全图抠不出尺寸/帧数字面量 → (0, 0),调用方回退兜底公式。"""
    assert categories.extract_pixels_frames(
        {"1": {"class_type": "SaveVideo", "inputs": {}}}) == (0.0, 0)
    assert categories.extract_pixels_frames({}) == (0.0, 0)


def test_bridge_client_endpoint_validation_and_urls():
    """独立客户端:endpoint 校验 + URL 拼装约定(与 modal_client._endpoint 同一约定)。"""
    import bridge_client
    try:
        bridge_client.BridgeClient("https://no-dashes.example", "k")
        assert False, "缺 -- 的 endpoint 应当拒绝"
    except bridge_client.BridgeError:
        pass
    c = bridge_client.BridgeClient("https://ws--comfyui-bridge", "k")
    assert c._url("run") == "https://ws--comfyui-bridge-run.modal.run"
    assert c._url("fetch") == "https://ws--comfyui-bridge-fetch.modal.run"


def test_bridge_client_pack_input_images(tmp_path):
    """输入图打包:LoadImage 类节点 → {name, image: data uri};找不到抛错。"""
    import base64
    import bridge_client
    (tmp_path / "a.png").write_bytes(b"\x89PNG-fake")
    wf = {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
          "2": {"class_type": "KSampler", "inputs": {}}}
    out = bridge_client.BridgeClient.pack_input_images(wf, [str(tmp_path)])
    assert out[0]["name"] == "a.png"
    assert out[0]["image"].startswith("data:image/png;base64,")
    assert base64.b64decode(out[0]["image"].split(",", 1)[1]) == b"\x89PNG-fake"
    try:
        bridge_client.BridgeClient.pack_input_images(
            {"1": {"class_type": "LoadImage", "inputs": {"image": "missing.png"}}}, [str(tmp_path)])
        assert False, "缺输入图应当抛错"
    except bridge_client.BridgeError:
        pass


def test_bridge_client_pack_input_images_rejects_escape(tmp_path):
    """工作流不可信:绝对路径 / .. 逃逸必须拒绝;子目录相对路径合法。"""
    import bridge_client
    (tmp_path / "secret.txt").write_bytes(b"leak")
    (tmp_path / "in" / "sub").mkdir(parents=True)
    (tmp_path / "in" / "sub" / "b.png").write_bytes(b"ok")
    for evil in (str(tmp_path / "secret.txt"),          # 绝对路径
                 "../secret.txt",                        # 上跳
                 "sub/../../secret.txt"):                # 藏在中段的上跳
        try:
            bridge_client.BridgeClient.pack_input_images(
                {"1": {"class_type": "LoadImage", "inputs": {"image": evil}}},
                [str(tmp_path / "in")])
            assert False, f"应当拒绝: {evil}"
        except bridge_client.BridgeError as e:
            assert "非法" in str(e), f"逃逸路径要报'非法'而不是'找不到': {evil} -> {e}"
    out = bridge_client.BridgeClient.pack_input_images(
        {"1": {"class_type": "LoadImage", "inputs": {"image": "sub/b.png"}}},
        [str(tmp_path / "in")])
    assert out[0]["name"] == "sub/b.png"


def test_local_nodes_skip_rules():
    """打包排除规则:代码留下,.git/缓存/权重/素材剔除。"""
    import local_nodes as ln
    for keep in ("__init__.py", "nodes/my.py", "requirements.txt", "web/ui.js", "README.md"):
        assert not ln.should_skip(keep), f"不该排除: {keep}"
    for drop in (".git/config", "__pycache__/x.pyc", "a/__pycache__/b.py", "x.pyc",
                 ".DS_Store", "model.safetensors", "w/ckpt.pt", "node_modules/x/y.js",
                 "demo.mp4", "venv/lib/x.py"):
        assert ln.should_skip(drop), f"应当排除: {drop}"


def test_local_nodes_pack_and_digest(tmp_path):
    """打包:内容一致 → 指纹一致;改一个字节 → 指纹变;排除项不进包;超限抛错。"""
    import io
    import zipfile
    import local_nodes as ln
    d = tmp_path / "my_node"
    (d / "__pycache__").mkdir(parents=True)
    (d / "__init__.py").write_text("NODE_CLASS_MAPPINGS={}")
    (d / "helper.py").write_text("x = 1")
    (d / "__pycache__" / "c.pyc").write_bytes(b"junk")
    (d / "big.safetensors").write_bytes(b"0" * 1000)

    blob, digest, count, raw = ln.pack_node_dir(d)
    assert count == 2, "只该打包两个 .py"
    names = set(zipfile.ZipFile(io.BytesIO(blob)).namelist())
    assert names == {"__init__.py", "helper.py"}
    assert raw < 100, "权重/缓存不该计入体积"

    blob2, digest2, _, _ = ln.pack_node_dir(d)
    assert digest2 == digest and blob2 == blob, "同内容必须打出一致的包与指纹(幂等重传)"
    (d / "helper.py").write_text("x = 2")
    assert ln.pack_node_dir(d)[1] != digest, "内容变了指纹必须变"

    ln.MAX_PACK_BYTES, keep = 10, ln.MAX_PACK_BYTES
    try:
        ln.pack_node_dir(d)
        assert False, "超限应抛错"
    except ValueError as e:
        assert "上限" in str(e)
    finally:
        ln.MAX_PACK_BYTES = keep


def test_local_nodes_pack_reads_each_source_once(tmp_path):
    """digest 和 zip 必须使用同一次读取的 bytes,避免编辑器保存造成包/指纹分裂。
    ⚠ 手动 patch + finally 还原,不用 pytest 的 monkeypatch —— CI 跑的是本文件自带的
      裸 runner(python tests/test_core.py),它只供给 tmp_path,没有 monkeypatch。"""
    import local_nodes as ln
    d = tmp_path / "one-read"
    d.mkdir()
    src = d / "node.py"
    src.write_text("x = 1")
    original = Path.read_bytes
    reads = {}

    def tracked(path):
        reads[path] = reads.get(path, 0) + 1
        return original(path)

    Path.read_bytes = tracked
    try:
        ln.pack_node_dir(d)
    finally:
        Path.read_bytes = original
    assert reads[src] == 1, f"每个源文件应只读一次,实际 {reads.get(src)} 次"


def test_local_nodes_zip_slip_guard():
    """云端解压的路径囚笼:绝对路径 / .. 穿越必须被挡在目标目录外。"""
    sys.path.insert(0, str(ROOT / "modal_app"))
    import _local_nodes_boot as boot
    dest = Path("/comfyui/custom_nodes/my_node")
    ok, bad = boot.safe_members(
        ["__init__.py", "sub/a.py",
         "../../../etc/passwd", "/etc/shadow", "sub/../../out.py", "a/../b.py"], dest)
    assert ok == ["__init__.py", "sub/a.py", "a/../b.py"] or "a/../b.py" in bad
    for evil in ("../../../etc/passwd", "/etc/shadow", "sub/../../out.py"):
        assert evil in bad, f"必须拦截: {evil}"
    for good in ("__init__.py", "sub/a.py"):
        assert good in ok, f"正常条目不该被拦: {good}"


def test_plan_node_sync_routes_local_and_unpushed(tmp_path, monkeypatch=None):
    """分流:有 git 且已推 → add(重部署);无 git / 未推送 → local_pack(不重部署)。"""
    import node_sync as ns
    root = tmp_path
    (root / "custom_nodes" / "gitnode").mkdir(parents=True)
    (root / "custom_nodes" / "mynode").mkdir(parents=True)
    orig_root, orig_info = ns._comfyui_root, ns.folder_git_info
    ns._comfyui_root = lambda: root
    ns.folder_git_info = lambda f: {
        "gitnode": {"folder": f, "has_git": True, "url": "https://github.com/a/b",
                    "commit": "c" * 40, "pushed": True},
        "mynode": {"folder": f, "has_git": False, "url": None, "commit": None, "pushed": True},
        "unpushed": {"folder": f, "has_git": True, "url": "https://github.com/a/b",
                     "commit": "d" * 40, "pushed": False},
    }[f]
    try:
        ns.analyze_workflow = lambda p: {
            "builtin": [], "unresolved": [],
            "by_folder": {"gitnode": ["G"], "mynode": ["M"]}}
        plan = ns.plan_node_sync({}, baked=[])
        assert [a["folder"] for a in plan["add"]] == ["gitnode"]
        assert [p["folder"] for p in plan["local_pack"]] == ["mynode"]
        assert plan["local_pack"][0]["reason"] == "no_git"
        assert plan["needs_deploy"] is True and plan["needs_local_upload"] is True

        # 未推送的 commit 绝不能进清单 —— 云端 checkout 不到会让整个镜像 build 崩
        (root / "custom_nodes" / "unpushed").mkdir()
        ns.analyze_workflow = lambda p: {
            "builtin": [], "unresolved": [], "by_folder": {"unpushed": ["U"]}}
        plan2 = ns.plan_node_sync({}, baked=[])
        assert plan2["add"] == [], "未推送的不该走 git 路线"
        assert plan2["local_pack"][0]["reason"] == "unpushed"
        assert plan2["needs_deploy"] is False, "本地通道不该触发重新部署"
    finally:
        ns._comfyui_root, ns.folder_git_info = orig_root, orig_info


def test_local_nodes_safe_folder(tmp_path):
    """入口路径囚笼:folders 来自 HTTP body,`../x` 必须挡住,不能打包 custom_nodes 之外的东西。"""
    import local_nodes as ln
    root = tmp_path / "custom_nodes"
    (root / "good").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    assert ln.safe_folder(root, "good") == (root / "good").resolve()
    for evil in ("../secret", "..", ".", "", "a/b", "/etc", "..\\x"):
        try:
            ln.safe_folder(root, evil)
            assert False, f"应当拒绝: {evil!r}"
        except ValueError:
            pass


def test_local_nodes_needs_refresh(tmp_path):
    """暖容器纠偏判定:容器内指纹 ≠ 提交方期望 → 该节点要重装。"""
    sys.path.insert(0, str(ROOT / "modal_app"))
    import _local_nodes_boot as boot
    dest = tmp_path / "custom_nodes"
    (dest / "a").mkdir(parents=True)
    (dest / "b").mkdir()
    (dest / "a" / ".mb_local_digest").write_text("aaa")
    (dest / "b" / ".mb_local_digest").write_text("bbb")
    orig = boot.DEST_DIR
    boot.DEST_DIR = dest
    try:
        assert boot.current_digests() == {"a": "aaa", "b": "bbb"}
        assert boot.needs_refresh({"a": "aaa", "b": "bbb"}) == []        # 都最新
        assert boot.needs_refresh({"a": "NEW", "b": "bbb"}) == ["a"]      # a 改过
        assert boot.needs_refresh({"c": "ccc"}) == ["c"]                  # 容器里根本没装
        assert boot.needs_refresh({"a": boot.BAKED_SENTINEL}) == ["a"]    # 要 baked,却仍是本地覆盖
        assert boot.needs_refresh({}) == [] and boot.needs_refresh(None) == []
    finally:
        boot.DEST_DIR = orig


def test_local_nodes_restore_baked_and_remove_local_only(tmp_path):
    """本地覆盖退场:同名镜像节点恢复备份,纯本地节点则删除目录。"""
    sys.path.insert(0, str(ROOT / "modal_app"))
    import _local_nodes_boot as boot
    dest = tmp_path / "custom_nodes"
    backup = tmp_path / "backups"
    baked = dest / "baked-node"
    baked.mkdir(parents=True)
    (baked / "node.py").write_text("baked")
    orig_dest, orig_backup = boot.DEST_DIR, boot.BACKUP_DIR
    boot.DEST_DIR, boot.BACKUP_DIR = dest, backup
    try:
        boot._remember_baked(baked, "baked-node")
        (baked / "node.py").write_text("local")
        (baked / ".mb_local_digest").write_text("d")
        local_only = dest / "local-only"
        local_only.mkdir()
        (local_only / ".mb_local_digest").write_text("x")
        assert boot.restore_baked(["baked-node", "local-only"]) == ["baked-node", "local-only"]
        assert (baked / "node.py").read_text() == "baked"
        assert not (baked / ".mb_local_digest").exists()
        assert not local_only.exists()
    finally:
        boot.DEST_DIR, boot.BACKUP_DIR = orig_dest, orig_backup


def test_folder_git_info_does_not_inherit_comfyui_parent_repo(tmp_path):
    """无 .git 节点不能向上继承 ComfyUI 主仓库的 remote/HEAD。"""
    root = tmp_path / "ComfyUI"
    node = root / "custom_nodes" / "plain-node"
    node.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                    "https://github.com/comfyanonymous/ComfyUI.git"], check=True)
    (node / "pyproject.toml").write_text(
        '[project.urls]\nRepository = "https://github.com/acme/plain-node"\n')
    original = node_sync._comfyui_root
    node_sync._comfyui_root = lambda: root
    try:
        info = node_sync.folder_git_info("plain-node")
        assert info["url"] == "https://github.com/acme/plain-node"
        assert info["commit"] == ""
        assert info["dirty"] is False
    finally:
        node_sync._comfyui_root = original


def test_cloud_stale_reason_matrix():
    """云端那份是不是旧的 —— 四条分支各一例(纯函数)。
    dirty 优先级最高:HEAD 没变但文件改了,云端按 commit clone 永远拿不到这些改动。"""
    f = node_sync._cloud_stale_reason
    same = "a" * 40
    assert f({"has_git": True, "pushed": True, "dirty": False}, same, same) is None
    assert f({"has_git": True, "pushed": True, "dirty": True}, same, same) == "dirty"
    assert f({"has_git": True, "pushed": True, "dirty": False}, "b" * 40, same) == "commit"
    assert f({"has_git": True, "pushed": False, "dirty": False}, "b" * 40, same) == "unpushed"
    assert f({"has_git": False, "pushed": True, "dirty": False}, "", same) == "no_git"
    # .git 丢了但 baked 里还留着旧记录 → 无从校验,按可能不一致处理(而不是默认「一致」)
    assert f({"has_git": True, "pushed": True, "dirty": False}, "", same) == "no_git"


def test_plan_baked_node_dirty_worktree():
    """已烤进镜像、HEAD 与 baked 相同、但工作树有未提交改动 → 必须进 local_pack。
    这是自写/调试节点最常见的状态(改一行试一下,不会先 commit),
    漏掉它 = 用户改完节点跑一遍、结果和没改一样,且没有任何提示。"""
    same = "s" * 40
    _stub_analyze({"baked-node": ["B1"]})
    _stub_env({"baked-node": {"has_git": True, "url": "https://github.com/a/b",
                              "commit": same, "pushed": True, "dirty": True}},
              exists_set={"baked-node"})
    try:
        baked = [{"name": "baked-node", "url": "https://github.com/a/b", "commit": same}]
        p = node_sync.plan_node_sync({}, baked=baked)
        assert p["update"] == []
        assert [x["folder"] for x in p["local_pack"]] == ["baked-node"]
        assert p["local_pack"][0]["reason"] == "dirty"
        assert p["needs_local_upload"] is True and p["needs_deploy"] is False
        assert p["expect_baked"] == []
    finally:
        _restore()


def test_plan_dirty_new_node_not_git_route():
    """还没进镜像、有 git 也推过、但工作树是脏的 → 不能走 git 路线(云端 clone 到的是干净版),
    必须走本地打包通道。"""
    _stub_analyze({"newnode": ["N1"]})
    _stub_env({"newnode": {"has_git": True, "url": "https://github.com/a/b",
                           "commit": "c" * 40, "pushed": True, "dirty": True}},
              exists_set={"newnode"})
    try:
        p = node_sync.plan_node_sync({}, baked=[])
        assert p["add"] == [], "脏工作树不该走 git 路线"
        assert [x["folder"] for x in p["local_pack"]] == ["newnode"]
    finally:
        _restore()


def test_plan_baked_node_with_unpushed_changes():
    """已烤进镜像的节点,本地改动没推 → 必须进 local_pack 盖掉旧版,
    绝不能静默 continue(那样云端跑旧代码,用户改完毫无变化且无任何线索)。"""
    _stub_analyze({"baked-node": ["B1"]})
    _stub_env({"baked-node": {"has_git": True, "url": "https://github.com/a/b",
                              "commit": "n" * 40, "pushed": False}},
              exists_set={"baked-node"})
    try:
        baked = [{"name": "baked-node", "url": "https://github.com/a/b", "commit": "o" * 40}]
        p = node_sync.plan_node_sync({}, baked=baked)
        assert p["update"] == [], "未推送的 commit 不该进清单(云端 checkout 不到会崩 build)"
        assert [x["folder"] for x in p["local_pack"]] == ["baked-node"]
        assert p["needs_local_upload"] is True
        assert p["needs_deploy"] is False
    finally:
        _restore()


def test_bridge_client_download_outputs_base64(tmp_path):
    """产物落盘(base64 路径,无网络):写文件 + 重名去重 + 未完成拒绝。"""
    import base64
    import bridge_client
    c = bridge_client.BridgeClient("https://ws--comfyui-bridge", "k")
    b64 = base64.b64encode(b"vid").decode()
    state = {"status": "completed", "id": "j1",
             "images": [{"filename": "out.mp4", "data_base64": b64},
                        {"filename": "out.mp4", "data_base64": b64}]}
    outs = c.download_outputs(state, str(tmp_path / "r"))
    assert [o["filename"] for o in outs] == ["out.mp4", "out_1.mp4"]
    assert (tmp_path / "r" / "out.mp4").read_bytes() == b"vid"
    try:
        c.download_outputs({"status": "running"}, str(tmp_path))
        assert False
    except bridge_client.BridgeError:
        pass


def test_estimate_vram_video_v2_anchors():
    """激活公式的三个实测锚点(MiniMax H3,主模型 20GB):
    0.9MP×362 帧应放行 48G 卡(实测峰值 38-40G 无 offload);2K×362 应对 80G 卡报警(实测 offload)。
    旧公式对同一工作流估 ~60G,在 48G 卡上纯误报 —— 这组断言防止公式回退。"""
    e_09 = categories.estimate_vram_video_gb(20.0, 1280 * 736, 362)
    assert e_09 <= 48.0, e_09                 # 0.9MP 在 L40S/L20 上必须放行
    e_native = categories.estimate_vram_video_gb(20.0, 1344 * 768, 362)
    assert e_native <= 48.0, e_native         # 原生画布 1344×768 也应放行
    e_2k = categories.estimate_vram_video_gb(20.0, 2048 * 1152, 362)
    assert e_2k > 80.0, e_2k                  # 2K 必须对 80G 卡报警


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
# node_sync.resolve_comfyui_tag — ComfyUI 版本跟随(纯函数)
# ============================================================================
def test_resolve_comfyui_tag_exact():
    tag, note = node_sync.resolve_comfyui_tag("0.22.0", ["v0.21.0", "v0.22.0", "v0.23.0"])
    assert tag == "v0.22.0" and note == ""


def test_resolve_comfyui_tag_closest_prefers_older():
    # 0.22.3 无对应 tag → 最接近(平手/更近取 ≤ 本机的 v0.22.0,不让云端比本地新)
    tag, note = node_sync.resolve_comfyui_tag("0.22.3", ["v0.22.0", "v0.23.0"])
    assert tag == "v0.22.0" and note != ""


def test_resolve_comfyui_tag_unknown_version():
    tag, note = node_sync.resolve_comfyui_tag("", ["v0.22.0"])
    assert tag == node_sync.DEFAULT_COMFYUI_TAG and note != ""


def test_resolve_comfyui_tag_no_tags():
    tag, note = node_sync.resolve_comfyui_tag("0.22.0", [])
    assert tag == node_sync.DEFAULT_COMFYUI_TAG and note != ""


# ============================================================================
# comfy_log.parse_import_failures — 节点导入结果解析(纯函数)
# ============================================================================
def _comfy_log():
    sys.path.insert(0, str(ROOT / "modal_app"))
    import comfy_log
    return comfy_log


def test_parse_import_failures_basic():
    cl = _comfy_log()
    log = (
        "Import times for custom nodes:\n"
        "   0.0 seconds: /comfyui/custom_nodes/websocket_image_save.py\n"
        "   0.1 seconds: /comfyui/custom_nodes/rgthree-comfy\n"
        "   0.5 seconds (IMPORT FAILED): /comfyui/custom_nodes/ComfyUI-Broken\n"
        "Starting server\n"
    )
    r = cl.parse_import_failures(log)
    assert "rgthree-comfy" in r["ok"] and "websocket_image_save" in r["ok"]
    assert [f["name"] for f in r["failed"]] == ["ComfyUI-Broken"]


def test_parse_import_failures_with_error():
    cl = _comfy_log()
    log = (
        "Cannot import /comfyui/custom_nodes/ComfyUI-Broken module for custom nodes: No module named 'foo'\n"
        "Import times for custom nodes:\n"
        "   0.5 seconds (IMPORT FAILED): /comfyui/custom_nodes/ComfyUI-Broken\n"
        "Starting server\n"
    )
    r = cl.parse_import_failures(log)
    assert r["failed"][0]["error"] == "No module named 'foo'"


# ============================================================================
# contract.compute_contract — ComfyUI 版本契约
# ============================================================================
def test_contract_comfyui_default_no_info():
    # 无 comfyui 信息(老 config / 没传)→ comfyui_match True,向后兼容不拦
    c = contract.compute_contract("0.5.1", "0.5.1", True, "H100", "H100")
    assert c["comfyui_match"] is True


def test_contract_comfyui_changed_soft():
    c = contract.compute_contract("0.5.1", "0.5.1", True, "H100", "H100",
                                  local_comfyui="0.23.0", deploy_comfyui="0.22.0")
    assert c["comfyui_match"] is False


def test_contract_comfyui_same():
    c = contract.compute_contract("0.5.1", "0.5.1", True, "H100", "H100",
                                  local_comfyui="0.22.0", deploy_comfyui="0.22.0")
    assert c["comfyui_match"] is True


# ============================================================================
# node_sync.render_extra_model_paths_yaml — 云端模型目录映射(纯函数)
# ============================================================================
def test_render_extra_model_paths_custom_category():
    y = node_sync.render_extra_model_paths_yaml(["checkpoints", "geometry_estimation"])
    assert "base_path: /comfy-volume/" in y
    # 自定义类别也映射,且与上传路径 models/<type>/ 一致
    assert "geometry_estimation: models/geometry_estimation/" in y
    assert "checkpoints: models/checkpoints/" in y


def test_local_model_folder_types_includes_standard():
    # folder_paths 不可用时退回标准基线(CI 环境无 ComfyUI)
    types = node_sync.local_model_folder_types()
    assert "checkpoints" in types and "loras" in types
    assert "custom_nodes" not in types  # 黑名单


# ============================================================================
# _comfy_ws — 产物「发现」(discover_outputs / classify_asset_type,纯函数)
# ============================================================================
def _comfy_ws():
    """CI 无 requests/websocket 依赖 → 注入空模块桩后 import(只测纯函数,不碰网络)。"""
    sys.path.insert(0, str(ROOT / "modal_app"))
    for name in ("requests", "websocket"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    import _comfy_ws
    return _comfy_ws


def test_classify_asset_type():
    cw = _comfy_ws()
    assert cw.classify_asset_type("a.png") == "image"
    assert cw.classify_asset_type("b.MP4") == "video"
    assert cw.classify_asset_type("c.glb") == "model3d"
    assert cw.classify_asset_type("noext", "gifs") == "video"   # 扩展名不认识 → 输出键兜底
    assert cw.classify_asset_type("noext", "images") == "image"  # 再兜底 image


def test_discover_outputs_dict_and_bare_string():
    """dict 形态照收;裸文件名按扩展名筛(camera_info 等非文件串不收);temp 跳过;去重。"""
    cw = _comfy_ws()
    outputs = {
        "9": {"images": [
            {"filename": "img.png", "subfolder": "", "type": "output"},
            {"filename": "img.png", "subfolder": "", "type": "output"},   # 重复 → 去重
            {"filename": "tmp.png", "subfolder": "", "type": "temp"},     # temp → 跳过
        ]},
        "42": {"gifs": [{"filename": "clip.mp4", "subfolder": "v", "type": "output"}]},
        "7": {"result": ["mesh.glb", "camera_info", "bg"]},               # 裸串:只收 .glb
    }
    refs = cw.discover_outputs(outputs)
    by_file = {r["filename"]: r for r in refs}
    assert set(by_file) == {"img.png", "clip.mp4", "mesh.glb"}
    assert by_file["img.png"]["asset_type"] == "image"
    assert by_file["clip.mp4"]["asset_type"] == "video"
    assert by_file["clip.mp4"]["subfolder"] == "v" and by_file["clip.mp4"]["node_id"] == "42"
    assert by_file["mesh.glb"]["asset_type"] == "model3d"
    assert cw.discover_outputs({}) == []


def _with_history(cw, payload):
    """临时替换 _comfy_ws.get_history(不用 monkeypatch —— CI 跑的是裸 runner,不是 pytest)。"""
    original = cw.get_history

    class _Ctx:
        def __enter__(self):
            cw.get_history = lambda pid: payload() if callable(payload) else payload
        def __exit__(self, *a):
            cw.get_history = original
    return _Ctx()


def test_history_settled_completed():
    """WS 丢了完成事件时的兜底:history 说跑完了就该收尾(否则主循环空转到 worker 超时)。"""
    cw = _comfy_ws()
    with _with_history(cw, {"p1": {"status": {"status_str": "success", "completed": True},
                                   "outputs": {"9": {}}}}):
        assert cw._history_settled("p1") == (True, [])


def test_history_settled_error_carries_message():
    """history 报错 → 已终结 + 带错误,让 caller 走正常报错路径而不是"没有完成"。"""
    cw = _comfy_ws()
    msgs = [["execution_start", {}],
            ["execution_error", {"node_id": "7", "node_type": "KSampler",
                                 "exception_message": "OOM"}]]
    with _with_history(cw, {"p1": {"status": {"status_str": "error", "completed": False,
                                              "messages": msgs}}}):
        done, errs = cw._history_settled("p1")
    assert done and len(errs) == 1
    assert "KSampler" in errs[0] and "OOM" in errs[0]


def test_history_settled_not_done_and_never_fakes():
    """还没跑完 / 查不到 / 查炸了 —— 一律「未终结」。兜底绝不能制造假终态。"""
    cw = _comfy_ws()
    with _with_history(cw, {}):                                   # prompt 还不在 history
        assert cw._history_settled("p1") == (False, [])
    with _with_history(cw, {"p1": {"status": {"completed": False}}}):
        assert cw._history_settled("p1") == (False, [])
    def _boom(pid=None):
        raise RuntimeError("connection refused")
    with _with_history(cw, _boom):                                # 查询本身失败
        assert cw._history_settled("p1") == (False, [])


def test_history_settled_legacy_no_status_field():
    """老版 ComfyUI 的 history 没有 status 字段:有 outputs 才算跑完。"""
    cw = _comfy_ws()
    with _with_history(cw, {"p1": {"outputs": {"9": {"images": []}}}}):
        assert cw._history_settled("p1") == (True, [])
    with _with_history(cw, {"p1": {"outputs": {}}}):
        assert cw._history_settled("p1") == (False, [])


# ============================================================================
# aigc_delivery — delivery 契约(desktop / aigc-r2)
# ============================================================================
def _aigc_delivery():
    sys.path.insert(0, str(ROOT / "modal_app"))
    import aigc_delivery
    return aigc_delivery


def test_delivery_default_desktop():
    """没传 delivery(老客户端)→ 默认 desktop,不报错。"""
    ad = _aigc_delivery()
    d, err = ad.normalize_delivery({"workflow": {}})
    assert err is None and d == {"mode": "desktop"}


def test_delivery_desktop_explicit():
    ad = _aigc_delivery()
    d, err = ad.normalize_delivery({"delivery": {"mode": "desktop"}})
    assert err is None and d["mode"] == "desktop"


def test_delivery_unsupported_mode_rejected():
    ad = _aigc_delivery()
    _, err = ad.normalize_delivery({"delivery": {"mode": "ftp"}})
    assert err == "unsupported delivery mode"
    _, err2 = ad.normalize_delivery({"delivery": "aigc-r2"})  # 非 dict 也拒
    assert err2 is not None


def test_delivery_aigc_r2_requires_job_id_and_token():
    ad = _aigc_delivery()
    _, e1 = ad.normalize_delivery({"delivery": {"mode": "aigc-r2", "token": "t"}})
    assert e1 == "aigc-r2 delivery requires 'job_id'"
    _, e2 = ad.normalize_delivery({"delivery": {"mode": "aigc-r2", "job_id": "j"}})
    assert e2 == "aigc-r2 delivery requires 'token'"
    d, e3 = ad.normalize_delivery({"delivery": {"mode": "aigc-r2", "job_id": "j", "token": "t"}})
    assert e3 is None and d["job_id"] == "j"


def test_delivery_public_strips_token():
    """public_delivery 是唯一进 job_state/日志的形态 —— 必须不含 token。"""
    ad = _aigc_delivery()
    pub = ad.public_delivery({"mode": "aigc-r2", "job_id": "j", "token": "SECRET"})
    assert pub == {"mode": "aigc-r2", "job_id": "j"}
    assert "token" not in pub
    assert ad.public_delivery(None) == {"mode": "desktop"}


# ============================================================================
# aigc_delivery — R2 交付引擎(注入假 HTTP,不碰网络;覆盖计划 §6/§7 的重试与恢复矩阵)
# ============================================================================
def _delivery_env():
    """准备可测的 aigc_delivery:配好 base_url、退避 sleep 换成 no-op。"""
    import os
    ad = _aigc_delivery()
    os.environ["AIGC_STUDIO_BASE_URL"] = "https://studio.example"
    ad._sleep = lambda s: None
    return ad


def _fake_streamer(ref):
    """假流式落盘:真写个临时小文件(deliver_one 的 finally 会删它)。"""
    import tempfile
    fd, p = tempfile.mkstemp(prefix="aigc_test_")
    import os as _os
    with _os.fdopen(fd, "wb") as f:
        f.write(b"x" * 10)
    return p, 10, "deadbeef"


def _ok_intake(body):
    return 200, {"r2_key": f"aigc/u/j/{body['asset_type']}-{body['position']}.bin",
                 "put_url": "https://r2/presigned", "asset_type": body["asset_type"],
                 "content_type": body["content_type"],
                 "required_headers": {"Content-Type": body["content_type"]}, "expires_in": 300}


def test_delivery_happy_path_positions_per_type():
    """多产物全成功:position 按 asset_type 各自从 0 计(幂等键),complete 只调一次。"""
    ad = _delivery_env()
    calls = {"intake": 0, "put": 0, "complete": 0}

    def poster(url, body, headers, timeout):
        assert body["token"] == "TOK"
        if url.endswith("asset-intake"):
            calls["intake"] += 1
            return _ok_intake(body)
        calls["complete"] += 1
        assert len(body["assets"]) == 3 and body["provider_job_id"] == "fc-1"
        return 200, {"ok": True}

    def putter(put_url, path, headers, timeout):
        calls["put"] += 1
        return 200, {"ETag": '"abc"'}

    refs = [{"filename": "a.png", "asset_type": "image"},
            {"filename": "b.png", "asset_type": "image"},
            {"filename": "v.mp4", "asset_type": "video"}]
    res = ad.deliver_outputs("j", refs, {"mode": "aigc-r2", "job_id": "j", "token": "TOK"},
                             provider_job_id="fc-1",
                             poster=poster, putter=putter, streamer=_fake_streamer)
    assert res["status"] == "completed"
    assert [a["r2_key"] for a in res["assets"]] == [
        "aigc/u/j/image-0.bin", "aigc/u/j/image-1.bin", "aigc/u/j/video-0.bin"]
    assert calls == {"intake": 3, "put": 3, "complete": 1}
    assert all(a["checksum_sha256"] == "deadbeef" and a["size_bytes"] == 10 for a in res["assets"])


def test_delivery_put_expiry_reintakes():
    """PUT 撞预签名过期(403)→ 重新 intake 换新地址再传,最终成功。"""
    ad = _delivery_env()
    calls = {"intake": 0, "put": 0}

    def poster(url, body, headers, timeout):
        if url.endswith("asset-intake"):
            calls["intake"] += 1
            return _ok_intake(body)
        return 200, {"ok": True}

    def putter(put_url, path, headers, timeout):
        calls["put"] += 1
        return (403, {}) if calls["put"] == 1 else (200, {"ETag": '"e"'})

    rec = ad.deliver_one("j", "TOK", 0, {"filename": "a.png", "asset_type": "image"},
                         poster=poster, putter=putter, streamer=_fake_streamer)
    assert rec["r2_key"] == "aigc/u/j/image-0.bin"
    assert calls == {"intake": 2, "put": 2}  # 过期那轮多一次 intake(幂等,恒返同一 r2_key)


def test_delivery_token_invalid_no_retry():
    """intake 401(token 失效/任务不属己)→ 不重试,立即 DeliveryError(retryable=False)。"""
    ad = _delivery_env()
    calls = {"intake": 0}

    def poster401(url, body, headers, timeout):
        calls["intake"] += 1
        return 401, {"error": "bad token"}

    try:
        ad.deliver_one("j", "TOK", 0, {"filename": "a.png", "asset_type": "image"},
                       poster=poster401, putter=lambda *a: (200, {}), streamer=_fake_streamer)
        raise AssertionError("should have raised")
    except ad.DeliveryError as e:
        assert e.retryable is False and e.status == 401
    assert calls["intake"] == 1  # 4xx 一次都不多试


def test_delivery_5xx_retries_then_gives_up():
    """intake 一直 503 → 重试到 INTAKE_TRIES 用尽,DeliveryError(retryable=True)。"""
    ad = _delivery_env()
    calls = {"n": 0}

    def poster503(url, body, headers, timeout):
        calls["n"] += 1
        return 503, "unavailable"

    try:
        ad.post_json_with_retry("https://studio.example/api/internal/asset-intake",
                                {"token": "TOK"}, {}, ad.INTAKE_TRIES, poster=poster503)
        raise AssertionError("should have raised")
    except ad.DeliveryError as e:
        assert e.retryable is True
    assert calls["n"] == ad.INTAKE_TRIES


def test_delivery_callback_failed_keeps_manifest():
    """文件全部传上 R2 但 job-complete 用尽重试 → 不算失败:status=callback_failed,
    manifest 完整保留(caller 存 job_state,AIGC Studio 轮询 /status 兜底落库)。"""
    ad = _delivery_env()

    def poster(url, body, headers, timeout):
        if url.endswith("asset-intake"):
            return _ok_intake(body)
        return 503, "unavailable"

    res = ad.deliver_outputs("j", [{"filename": "a.png", "asset_type": "image"}],
                             {"mode": "aigc-r2", "job_id": "j", "token": "TOK"},
                             poster=poster, putter=lambda *a: (200, {"ETag": '"e"'}),
                             streamer=_fake_streamer)
    assert res["status"] == "callback_failed"
    assert len(res["assets"]) == 1 and res["assets"][0]["r2_key"] == "aigc/u/j/image-0.bin"


def test_delivery_complete_accepts_empty_2xx_body():
    """job-complete 回 204/空 body 的 200 也算成功 —— 不能误判成 callback_failed。"""
    ad = _delivery_env()

    def poster(url, body, headers, timeout):
        if url.endswith("asset-intake"):
            return _ok_intake(body)
        return 204, ""  # 无 JSON body

    res = ad.deliver_outputs("j", [{"filename": "a.png", "asset_type": "image"}],
                             {"mode": "aigc-r2", "job_id": "j", "token": "TOK"},
                             poster=poster, putter=lambda *a: (200, {"ETag": '"e"'}),
                             streamer=_fake_streamer)
    assert res["status"] == "completed"


def test_delivery_no_outputs_raises():
    ad = _delivery_env()
    try:
        ad.deliver_outputs("j", [], {"mode": "aigc-r2", "job_id": "j", "token": "TOK"})
        raise AssertionError("should have raised")
    except ad.DeliveryError:
        pass


def test_delivery_helpers():
    """safe_filename 防路径注入;content-type 识别含 3D;错误分类 5xx/网络可重试、4xx 不可。"""
    ad = _delivery_env()
    assert ad.safe_filename("../../etc/passwd") == "passwd"
    assert ad.safe_filename("") == "output.bin"
    assert ad.detect_content_type("m.glb") == "model/gltf-binary"
    assert ad.detect_content_type("v.mp4") == "video/mp4"
    assert ad.detect_content_type("weird.zzz") == "application/octet-stream"
    assert ad.is_retryable_status(503) and ad.is_retryable_status(None)
    assert not ad.is_retryable_status(401) and not ad.is_retryable_status(400)


def test_local_nodes_orphan_zip_marks_unknown_digest(tmp_path):
    """残包(有 .zip 无 .digest)解压后必须留下"未知版本"痕迹。

    这是一条完整的静默错误链:Volume 上删 zip 失败、digest 却删成功 → 冷容器照样解压出
    旧本地代码,但因为没有 .digest 而不写 marker → needs_refresh 看不到 marker,把容器
    判成「已经是 baked」→ 早该退场的旧覆盖版一直跑下去,日志全绿、无人察觉。"""
    sys.path.insert(0, str(ROOT / "modal_app"))
    import _local_nodes_boot as boot
    import zipfile
    vol, dest = tmp_path / "_local_nodes", tmp_path / "custom_nodes"
    vol.mkdir(parents=True)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(vol / "mynode.zip", "w") as z:
        z.writestr("__init__.py", "# 旧的本地代码")
    # 故意不写 mynode.digest —— 这就是残包
    orig_vol, orig_dest = boot.VOL_DIR, boot.DEST_DIR
    boot.VOL_DIR, boot.DEST_DIR = vol, dest
    try:
        assert boot.extract_all() == ["mynode"]
        marker = dest / "mynode" / ".mb_local_digest"
        assert marker.exists(), "残包解压后必须留 marker,否则会被误判成 baked"
        assert marker.read_text(encoding="utf-8").strip() == boot.UNKNOWN_DIGEST
        # 两条路都得能自愈:声明 baked → 判定该退场;声明具体指纹 → 判定要重装。
        assert boot.needs_refresh({"mynode": boot.BAKED_SENTINEL}) == ["mynode"]
        assert boot.needs_refresh({"mynode": "realdigest"}) == ["mynode"]
    finally:
        boot.VOL_DIR, boot.DEST_DIR = orig_vol, orig_dest


def test_remove_local_node_keeps_digest_when_zip_delete_fails():
    """zip 没删掉就必须停手,不能接着删 digest —— 那正是上一条测试里那种残包的来源。"""
    import local_nodes as ln
    calls = []

    class _Vol:
        def remove_file(self, path, recursive=False):
            calls.append(path)
            if path.endswith(".zip"):
                raise RuntimeError("boom: volume unavailable")

    orig = ln._mv
    ln._mv = lambda: types.SimpleNamespace(get_volume=lambda cfg: _Vol())
    try:
        res = ln.remove_volume_local_node({}, "mynode")
    finally:
        ln._mv = orig
    assert res["ok"] is False, "删除失败必须如实回报"
    assert any(c.endswith(".zip") for c in calls)
    assert not any(c.endswith(".digest") for c in calls), \
        f"zip 删失败后不该再动 digest,实际调用: {calls}"


def test_job_id_rule_identical_local_and_cloud():
    """job_id 白名单在本地(contract)和云端(modal_app)各有一份 —— 云端不能 import
    contract,那个模块不在镜像的 add_local_python_source 名单里。两份必须逐字一致:
    规则漂移 = 一边挡住的脏 id 另一边照单全收,而这个 id 会被拿去 remove_file(recursive)。"""
    import re
    pat = re.compile(r"_SAFE_JOB_ID = re\.compile\((r?['\"].*?['\"])\)")
    a = pat.search((ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8"))
    b = pat.search((ROOT / "contract.py").read_text(encoding="utf-8"))
    assert a and b, "云端与本地都应有 _SAFE_JOB_ID 定义"
    assert a.group(1) == b.group(1), f"规则漂移: 云端 {a.group(1)} vs 本地 {b.group(1)}"


def test_bridge_client_wait_tolerates_transient_errors():
    """轮询撞上瞬态错误只能重试,不能打死整个 wait —— 任务在云端照常跑、照常计费,
    而 _req 只重试一次,两次连续网络错就足以让长任务失去接管者、产物再也取不回。"""
    import bridge_client as bc
    c = bc.BridgeClient("https://ws--comfyui-bridge", "k")
    calls = {"n": 0}

    def flaky(job_id):
        calls["n"] += 1
        if calls["n"] <= 3:              # 前三拍连续失败
            raise bc.BridgeError("transient")
        return {"status": "completed"}

    c.status = flaky
    assert c.wait("j", timeout_s=30, poll_s=0)["status"] == "completed"
    assert calls["n"] == 4, "失败后要继续轮询,成功即清零"

    # 但连续失败到上限必须如实报错 —— 绝不能假装成终态把任务判死
    c2 = bc.BridgeClient("https://ws--comfyui-bridge", "k")

    def always_down(job_id):
        raise bc.BridgeError("down")

    c2.status = always_down
    try:
        c2.wait("j", timeout_s=30, poll_s=0, max_consecutive_errors=3)
        assert False, "连续失败到上限应当抛错"
    except bc.BridgeError as e:
        assert "连续" in str(e), f"错误信息要说清是连续失败: {e}"


def test_save_config_atomic_and_private(tmp_path):
    """config.json 里有 modal token / bridge key 四种凭据:必须原子写且不可他人读。
    非原子的半个 JSON 会让 load_config 解析失败后**静默回落默认配置** ——
    表现成"插件突然没配置过",没有任何报错指向真实原因。"""
    import json
    import os
    import stat
    import config as cfg_mod
    p = tmp_path / "sub" / "config.json"
    orig = cfg_mod._config_path
    cfg_mod._config_path = lambda: p
    try:
        cfg_mod.save_config({"bridge_api_key": "s3cret", "x": 1})
        assert json.loads(p.read_text(encoding="utf-8"))["bridge_api_key"] == "s3cret"
        assert not list(p.parent.glob("*.tmp")), "临时文件必须被 replace 掉,不能残留"
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode & 0o077 == 0, f"组/其他不该有任何权限,实际 {oct(mode)}"
    finally:
        cfg_mod._config_path = orig


def test_write_baked_nodes_drops_empty_url(tmp_path):
    """空 url/name 的条目不能进 baked 清单:镜像 build 时会生成 `git clone ''`,
    整个 RUN 崩掉而报错和「哪个节点」完全对不上号。"""
    import node_sync as ns
    f = tmp_path / "_custom_nodes_data.py"
    orig = ns.DATA_FILE
    ns.DATA_FILE = f
    try:
        ns.write_baked_nodes([
            {"name": "good", "url": "https://github.com/a/b", "commit": "c" * 40},
            {"name": "no-url", "url": "", "commit": "c" * 40},
            {"name": "", "url": "https://github.com/a/c", "commit": ""},
            {"name": "ws-url", "url": "   ", "commit": ""},
        ])
        names = {n.get("name") for n in ns.read_baked_nodes()}
        assert names == {"good"}, f"只有合法条目该留下,实际 {names}"
    finally:
        ns.DATA_FILE = orig


# ============================================================================
# 无 pytest 时的简易运行器
# ============================================================================
if __name__ == "__main__":
    import inspect
    import tempfile

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            # 极简 fixture:签名带 tmp_path 的给一个独立临时目录(与 pytest 语义对齐)
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
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


def test_modal_image_has_sage_patches():
    """镜像必须带 SageAttention 的两个上游缺陷补丁,且校验是 fail-closed 的。

    ① int32 行偏移溢出:H3 的 fused QKV(seq-stride=21504)下行号 > 2^31/21504 ≈ 99865
       即 wrap 成负 → 尾几帧塌灰噪 / 偶发 illegal memory access。
    ② V strided 进 CUDA 扩展:core.py 只在 kv_len%128!=0 时 cat(顺带变 contiguous),
       整除时 V 以 strided 视图直进 per_channel_fp8 → 越界 crash。

    两个上游都未修。这层删了不会有任何报错,只会在特定配置下静默出坏帧或崩掉,
    所以这里静态钉死它还在、且保留了全部校验闸。
    """
    import ast

    src = (ROOT / "modal_app" / "modal_image.py").read_text(encoding="utf-8")
    cmds = [
        n.args[0].value
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run_commands"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and isinstance(n.args[0].value, str)
    ]
    patch = [c for c in cmds if "tl.int64" in c]
    assert len(patch) == 1, f"期望恰好一条 sage 补丁命令,实际 {len(patch)} 条"
    cmd = patch[0]

    # ① int64
    assert "sed -i -E" in cmd and "offs_n" in cmd and "stride_" in cmd
    assert '" = 0' in cmd, "缺少『脆弱写法清零』断言"
    assert "-ge 4" in cmd, "缺少『int64 转换够数』断言"
    # ② v.contiguous()
    assert "v = v.contiguous()" in cmd, "缺少 V strided 补丁"
    assert "transpose_pad_permute_cuda" in cmd, "V 补丁锚点丢失"
    assert "grep -q 'v = v.contiguous()'" in cmd, "V 补丁缺少幂等判断"
    # ③ 两文件语法校验
    assert "ast.parse" in cmd, "缺少补丁后语法校验"


def test_no_api_key_in_query_string():
    """鉴权 key 不得出现在 query string 里。

    query 会落进反代 / CDN 访问日志、浏览器历史和 Referer,是长期暴露面。
    GET 一律走 X-Bridge-Key 头(云端 modal_app 仍兼容旧的 ?key=,但本仓库自己的
    客户端不许再往 query 里放)。这条测试钉死方向,防止新代码顺手写回 params={"key": ...}。
    """
    import re

    bad = []
    pat = re.compile(r'(?:params|urlencode\()\s*=?\s*\{[^}]*["\']key["\']\s*:')
    for f in ("bridge_client.py", "modal_client.py", "routes.py", "web/modal_bridge.js"):
        p = ROOT / f
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{f}:{i}: {line.strip()}")
    assert not bad, "这些地方把 key 放进了 query:\n" + "\n".join(bad)

    # 反向:客户端确实在用请求头
    assert 'X-Bridge-Key' in (ROOT / "bridge_client.py").read_text(encoding="utf-8")
    assert 'X-Bridge-Key' in (ROOT / "modal_client.py").read_text(encoding="utf-8")


def test_advanced_toggles_not_in_setup_panel():
    """SageAttention 与 AIGC Studio 必须留在设置页的 Advanced,不许回到 Modal Setup 面板。

    两者都是少数人才用的进阶功能,曾经占着部署面板最显眼的位置,逼每个新用户先读懂
    两段免责说明才敢点部署。

    AIGC 的两个字段在 2026-08-31 从「面板输入 + 设置页开关」改成**全部放设置页**
    (用户明确选择):URL 用 text、旁路密钥用 password。已知代价是设置值会明文落进
    comfy.settings.json(0644、前端可读),password 只遮显示不改存储 —— 换来配置集中在一处。
    """
    src = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")

    # ① 面板里不该再有这些控件
    assert 'id="mb-dep-sage"' not in src, "SageAttention 复选框被挪回了 Setup 面板"
    assert "mb-dep-aigc" not in src, "AIGC 输入框被挪回了 Setup 面板"

    # ② sage 开关：注册、默认关、归 Advanced
    i = src.find('id: "ModalBridge.useSageAttention"')
    assert i > 0, "设置项 ModalBridge.useSageAttention 没注册"
    block = src[i:i + 400]
    assert "defaultValue: false" in block, "sage 默认值不是 false"
    assert '"Advanced"' in block, "sage 没归到 Advanced 分组"

    # ③ AIGC 两个字段：注册、归 Advanced、密钥必须是 password 类型
    for sid, want_type in (("ModalBridge.aigcStudioUrl", "text"),
                           ("ModalBridge.aigcBypassSecret", "password")):
        k = src.find(f'id: "{sid}"')
        assert k > 0, f"设置项 {sid} 没注册"
        blk = src[k:k + 400]
        assert '"Advanced"' in blk, f"{sid} 没归到 Advanced 分组"
        assert f'type: "{want_type}"' in blk, f"{sid} 类型应为 {want_type}"
        assert "syncAigcFieldToConfig" in blk, f"{sid} 没有把值同步回 config"

    # ④ 密钥不许被启动回填覆盖 —— /config 不回吐它，用空串回填会把用户填过的值清掉
    assert 'setSettingValue("ModalBridge.aigcStudioUrl"' in src, "URL 应当用 config 真值回填"
    assert 'setSettingValue("ModalBridge.aigcBypassSecret"' not in src,         "密钥不该被回填 —— /config 不回吐它,回填只会用空串清掉用户填过的值"


def test_all_settings_share_one_category():
    """所有 ModalBridge 设置项必须显式写 category,且顶级分类名一致。

    不写 category 时 ComfyUI 按 id 的 "." 拆分归类(ModalBridge.batchCount → 分类
    "ModalBridge"、小节 "batchCount"),于是顶级名只能是无空格的 "ModalBridge",
    且每个设置各占一个小节。只要有一项写了带空格的 "Modal Bridge",就会和没写的
    那些在设置页分裂成两个同名分类 —— 2026-08-30 真踩过。
    """
    import re

    src = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")
    blk = src[src.index("const SETTINGS = ["):]
    blk = blk[:blk.index("\n];")]

    ids = re.findall(r'id:\s*"(ModalBridge\.\w+)"', blk)
    assert len(ids) >= 8, f"只解析到 {len(ids)} 个设置项,正则可能失效"

    cats = re.findall(r'category:\s*\[\s*"([^"]+)"\s*,\s*"([^"]+)"', blk)
    assert len(cats) == len(ids), (
        f"{len(ids)} 个设置项里只有 {len(cats)} 个写了 category —— "
        "漏写的会按 id 前缀单独归类,设置页出现两个 Modal Bridge"
    )
    tops = {c[0] for c in cats}
    assert len(tops) == 1, f"顶级分类名不统一: {sorted(tops)}"
    subs = {c[1] for c in cats}
    assert subs <= {"General", "Advanced"}, f"意外的小节名: {sorted(subs - {'General', 'Advanced'})}"


def test_sage_copy_matches_worker_gate():
    """SageAttention 的用户文案必须和 worker 的实际门控一致。

    worker 用 compute_cap 白名单门控(modal_app.py 的 `cap in ("8.9", "9.0")`),
    即 L40S(sm_89)与 H100(sm_90)都生效、B200(sm_100)回退 SDPA。
    2026-08-30 踩过:把 multiarch wheel 之前的旧文案(「仅 H100 生效」)搬进了设置页,
    会误导用户为了用 sage 去选更贵的 H100。
    """
    gate = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    assert '("8.9", "9.0")' in gate, "worker 门控白名单变了,文案断言需要同步更新"

    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")
    i = js.find('"set.sage":')
    assert i > 0, "set.sage 文案不见了"
    block = js[i:i + 1600]
    assert "L40S" in block, "sage 文案没提 L40S —— 但 worker 门控放行 sm_89"
    assert "仅标准档 H100 生效" not in block, "又抄回了 multiarch wheel 之前的旧文案"
    assert "Standard H100 tier only" not in block, "英文文案仍是旧的 H100-only 说法"


def test_no_runtime_pip_install_anywhere():
    """包里不许有「运行时用 subprocess 装包」——ComfyUI Registry 的硬禁令。

    官方原文:「Runtime package installation through subprocess calls is not permitted.」
    2026-08-30 定位到这是 0.8.x 连续被判 Flagged 的最可能原因(0.7.9 里同样有这段代码,
    只是当时规则还没收紧)。依赖统一由 ComfyUI Manager 装(pyproject.dependencies +
    requirements.txt),自写节点的依赖改在镜像 build 期装(_local_nodes_data.py)。

    这里扫真实代码行(剥掉注释与文档串),避免把说明文字误判成调用。
    """
    import ast

    # 只扫 git 跟踪的文件 —— 那正是 `comfy node publish` 会打进 Registry 包的内容。
    # 不用 rglob:插件目录下有第三方 lib/(已 gitignore、不进包),扫它既慢又会误报。
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    rels = [x for x in out.stdout.splitlines() if x.strip()] if out.returncode == 0 else []
    assert rels, "拿不到 git 跟踪的 py 文件清单(不在 git 仓库?),该测试失去意义"

    offenders = []
    for rel in rels:
        if rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # 取所有字面量字符串实参（含列表里的），拼起来判断是不是 pip 安装命令
            flat = []
            for a in ast.walk(node):
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    flat.append(a.value)
            joined = " ".join(flat)
            if "pip" in joined and "install" in joined:
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", "")
                if name in ("run", "Popen", "call", "check_call", "check_output", "system"):
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "运行时装包(Registry 禁令):\n  " + "\n  ".join(offenders)


def test_local_node_reqs_roundtrip_and_sanitize(tmp_path):
    """自写节点依赖:收集时要剔掉在云端没有意义的行,并能写入/读回。

    -r/-e/-f/--index-url/本地路径在云端没有对应的文件系统上下文,必须剔除；
    远程 wheel 和 git+ VCS 是合法云端依赖,必须保留。
    """
    import local_nodes as ln
    import node_sync as ns

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "requirements.txt").write_text(
        "# c\ntorch>=2.0\n\nnumpy==1.26.4\n-r other.txt\n-e .\nhttps://x/y.whl\n"
        "git+https://github.com/x/y.git@abc#egg=y\n--index-url http://p\n",
        encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "requirements.txt").write_text("numpy==1.26.4\nrich\n", encoding="utf-8")
    (tmp_path / "c").mkdir()          # 没有 requirements

    got = ln.collect_requirements(["a", "b", "c", "../escape"], tmp_path)
    assert got == ["torch>=2.0", "numpy==1.26.4", "https://x/y.whl",
                   "git+https://github.com/x/y.git@abc#egg=y", "rich"], got

    old = ns.LOCAL_REQS_FILE
    try:
        ns.LOCAL_REQS_FILE = tmp_path / "_local_nodes_data.py"
        ns.write_local_node_reqs(got)
        assert ns.read_local_node_reqs() == got
        ns.write_local_node_reqs([])
        assert ns.read_local_node_reqs() == []
    finally:
        ns.LOCAL_REQS_FILE = old


def test_local_node_upload_carries_dependency_manifest(tmp_path, monkeypatch):
    """zip/digest/requirements manifest 必须同批上传,供另一台机器重建依赖层。"""
    import json
    import types
    import local_nodes as ln

    node = tmp_path / "private_node"
    node.mkdir()
    (node / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (node / "requirements.txt").write_text(
        "numpy==1.26.4\ngit+https://github.com/x/y.git\n", encoding="utf-8")

    uploaded = {}

    class Batch:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def put_file(self, src, dst): uploaded[dst] = src.read()

    class Vol:
        def batch_upload(self, force=False):
            assert force is True
            return Batch()

    monkeypatch.setattr(ln, "_mv", lambda: types.SimpleNamespace(get_volume=lambda cfg: Vol()))
    result = ln.upload_local_nodes({}, ["private_node"], tmp_path)
    assert not result["failed"]
    manifest = json.loads(uploaded["_local_nodes/private_node.requirements.json"])
    assert manifest == ["numpy==1.26.4", "git+https://github.com/x/y.git"]


def test_legacy_local_node_without_manifest_forces_migration(tmp_path, monkeypatch):
    """旧 zip digest 即使相同,缺 manifest 也必须重传一次。"""
    import local_nodes as ln

    node = tmp_path / "private_node"
    node.mkdir()
    (node / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    files, _ = ln.scan_node_dir(node)
    digest = ln.compute_digest(files)
    monkeypatch.setattr(ln, "volume_digests", lambda cfg, folders: {"private_node": digest})
    monkeypatch.setattr(ln, "volume_local_node_requirements", lambda cfg, folders: {})
    plan = ln.plan_local_uploads({}, ["private_node"], tmp_path)
    assert [x["folder"] for x in plan["upload"]] == ["private_node"]
    assert not plan["uptodate"]


def test_output_path_jail():
    """volume_path 必须囚在 _outputs/<job_id>/ 内。

    这条路(routes._write_results)**绕过云端 fetch_endpoint、直连 Volume SDK**,
    云端那道校验管不到;而 volume_path 整个来自浏览器提交的 modal_state。
    伪造成 models/... 就能把上传过的模型下载走**并删掉**(取回后即删是既定行为),
    删除不可逆 —— 几十 GB 的模型重传代价极高。
    """
    from contract import is_safe_output_path as ok

    assert ok("job1", "_outputs/job1/a.png")
    assert ok("job1", "_outputs/job1/sub/a.png")
    # 攻击向量
    assert not ok("job1", "models/checkpoints/x.safetensors")
    assert not ok("job1", "_local_nodes/foo.zip")
    assert not ok("job1", "_outputs/job2/a.png")          # 别人的 job
    assert not ok("job1", "_outputs/job1/../../models/x")
    assert not ok("job1", "./_outputs/job1/a.png")
    assert not ok("job1", "_outputs/job1//a.png")
    assert not ok("job1", "")
    assert not ok("job1", None)
    assert not ok("job1", "_outputs/job10/a.png")         # 前缀相近的别的 job


def test_output_path_jail_identical_local_and_cloud():
    """云端 fetch_endpoint 里另有一份同规则实现(云端不能 import contract)。

    ⚠ 两边不能逐字比较:本地必须用 posixpath.normpath —— 插件会跑在 Windows 上,
    而 os.path.normpath 在 Windows 会把 / 转成 \\,把合法路径判成越界。
    云端跑在 Linux 容器里,两者等价。所以这里比对的是「三重判据都在」。
    """
    cloud = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    i = cloud.find('prefix = f"_outputs/{job_id}/"')
    assert i > 0, "云端的输出路径囚笼不见了"
    seg = cloud[i:i + 320]
    assert "startswith(prefix)" in seg, "云端缺前缀校验"
    assert '".." in path' in seg, "云端缺 .. 校验"
    assert "normpath(path)" in seg, "云端缺规范化校验"

    local = (ROOT / "contract.py").read_text(encoding="utf-8")
    assert "posixpath.normpath" in local, "本地必须用 posixpath(Windows 兼容)"
    assert 'f"_outputs/{job_id}/"' in local, "本地缺前缀校验"


def test_write_results_actually_calls_the_jail():
    """routes._write_results 必须在用 vp 之前调用囚笼 —— 光有函数没人调等于没有。

    这是静态检查(比对源码顺序),不是真实调用:routes.py 用相对导入且依赖
    aiohttp/server,单测里 import 不进来。covering 真实路由需要另起一套测试基建
    (codex review 也指出了这个缺口),在那之前先用顺序断言挡住"摘掉调用"这类回归 ——
    上一版测试就漏过了它。
    """
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    i = src.index("async def _write_results(")
    body = src[i:src.index("\n\n\n", i)]

    call = body.find("contract.is_safe_output_path(job_id, vp)")
    dl = body.find("download_volume_file")
    rm = body.find("remove_volume_path")
    assert call > 0, "_write_results 里没调用 is_safe_output_path —— 囚笼被摘了"
    assert dl > 0 and rm > 0, "下载/删除调用不见了,测试锚点需更新"
    assert call < dl, "囚笼必须在 download_volume_file 之前"
    assert call < rm, "囚笼必须在 remove_volume_path 之前"


def test_execution_error_never_becomes_completed():
    """ComfyUI 报 execution_error 时必须让任务失败,不能因为 history 里有前序产物就当成功。

    旧写法 `if not execution_done and not errors: raise` 在 errors 非空时**不抛**,
    于是继续往下从 history 捞产物返回,而 worker 那边无条件写 completed。
    一个 BrokenNode 报错的工作流,只要前面某节点落过一张图,就会被报成"成功"、
    照常计费、还被当完整产物交付出去。

    静态检查:错误分支必须在 discover_outputs 之前 raise。真实的 WS 时序测试需要
    另起基建(codex review 指出的缺口),在那之前先挡住这类回归。
    """
    src = (ROOT / "modal_app" / "_comfy_ws.py").read_text(encoding="utf-8")

    raise_at = src.find('raise RuntimeError("工作流执行出错')
    discover_at = src.find("refs = discover_outputs(")
    assert raise_at > 0, "execution_error 的失败分支不见了 —— 错误会被吞成 completed"
    assert discover_at > 0, "discover_outputs 调用不见了,测试锚点需更新"
    assert raise_at < discover_at, "必须在捞产物之前就失败"

    # 旧的「errors 非空反而不抛」写法不许回来
    assert "if not execution_done and not errors:" not in src, \
        "回退成了 errors 非空时不抛的旧逻辑"

    # worker 侧确实会把异常转成 failed（否则上面的 raise 白抛）
    worker = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    assert '"status": "failed"' in worker, "worker 不再把异常写成 failed"


def test_find_local_model_stays_in_root(tmp_path):
    """模型查找必须囚在 roots 内 —— filename 来自工作流 JSON,找到的文件会被上传进 Volume。

    ⚠ Python 的 `Path("/models") / "/etc/passwd"` **丢弃左边**,直接等于那个绝对路径。
    所以不能只靠"拼一下再判 is_file",必须 resolve 后确认仍在 root 内。
    符号链接也要挡:rglob 的结果天然在 root 下,但链接可以指到外面。
    """
    import os as _os
    import modal_volume as mv

    root = tmp_path / "models"
    root.mkdir()
    (root / "ok.safetensors").write_bytes(b"x")
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s")

    assert mv.find_local_model("ckpt", "ok.safetensors", [root]) is not None
    assert mv.find_local_model("ckpt", str(outside / "secret.txt"), [root]) is None   # 绝对路径
    assert mv.find_local_model("ckpt", "../secrets/secret.txt", [root]) is None       # ..
    try:
        _os.symlink(outside / "secret.txt", root / "link.safetensors")
    except (OSError, NotImplementedError):
        return                                    # Windows 无权限建链接时跳过这一段
    assert mv.find_local_model("ckpt", "link.safetensors", [root]) is None            # 符号链接

    # 真实 resolver 先走 ComfyUI folder_paths.get_full_path；该命中也必须过同一囚笼，
    # 不能只修 fallback 的 find_local_model。
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    i = src.index("def _local_model_resolver()")
    body = src[i:src.index("\n\n\n", i)]
    get_full = body.index("folder_paths.get_full_path")
    cage = body.index("modal_volume.is_path_within_roots")
    returned = body.index("return Path(full)")
    assert get_full < cage < returned


def test_volume_download_is_atomic():
    """Volume 下载必须写 .part 再 rename。

    直接写正式名的话,下载中断会在 ComfyUI 的 output 里留下一个**看起来完整**的
    截断视频/3D 文件,用户点开才发现坏了,同名去重还会把它当成已存在的产物。
    """
    src = (ROOT / "modal_volume.py").read_text(encoding="utf-8")
    i = src.index("def download_volume_file(")
    body = src[i:src.index("\n\ndef ", i)]
    assert '".part"' in body, "下载没写 .part"
    assert "os.replace(" in body, "没有原子 rename"
    assert body.index('".part"') < body.index("os.replace("), "顺序不对"


def test_commit_failure_is_not_completed():
    """Volume commit 失败必须让任务失败,不能照样写 completed。

    commit 失败 = 本地 SDK 看不到刚写进 _outputs 的文件。以前只 print 一句就继续,
    用户看到"成功"却取不回产物,且没有任何线索指向真实原因。
    """
    src = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    i = src.index("models_vol.commit()")
    seg = src[i:i + 700]
    assert '"status": "failed"' in seg, "commit 失败没写 failed"
    assert "raise" in seg, "commit 失败没有中断流程"
    completed_at = seg.find('"status": "completed"')
    assert completed_at == -1 or seg.index('"status": "failed"') < completed_at, \
        "failed 分支必须在写 completed 之前"


def test_explicit_tier_never_routed_to_cpu():
    """用户显式选了 GPU 档位时,不许再用"扫不到模型"把它推翻成 CPU worker。

    以前 needs_gpu 只看 extract_required_models():只要工作流里扫不到本地模型文件名,
    哪怕用户明明选了 H100,也照样被送进强制 --cpu 的 worker。而那条推断本身不可靠 ——
    节点内部下载权重、无模型文件的 CUDA/Triton 图像处理与 3D/光流节点、模型参数不是
    文件名字符串的节点,都扫不到却真要 GPU。给错 CPU = 跑不动耗到超时、白烧钱零产出。
    """
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    i = src.index("needs_gpu = (")
    expr = src[i:src.index(")", src.index("cpu_tier_when_no_model", i))]

    assert '_tier_sel != "auto"' in expr, "显式选档没有短路成 needs_gpu"
    assert "extract_required_models(prompt)" in expr, "丢了原有的模型扫描判据"
    assert "cpu_tier_when_no_model" in expr, "没接策略开关"

    # 开关默认值必须是 True —— 改默认等于改所有既有用户的账单，那是用户的决定
    import config as cfg_mod
    assert cfg_mod.DEFAULT_CONFIG["cpu_tier_when_no_model"] is True, \
        "改这个默认值会改变既有用户的账单模型,应由用户显式决定"


def test_cli_deploy_keeps_secret_fields_and_atomic_config():
    """CLI 部署不能丢 Secret 字段,也不能绕过 config 的原子写 + 0600。

    以前 secret_create_cmd 只传了 hf_token 和 bridge_key,comfy_api_key / aigc_* 吃函数
    默认的空串 —— 用 CLI 部署一次,工作流里的 ComfyUI API 节点鉴权和 aigc-r2 交付就
    静默失效(config 里明明配着)。config 又是直接 write_text:非原子(写一半崩 → 半个
    JSON → 加载静默回落默认值,表现成"配置没了"),权限还是 0644,而里面有四种凭据。
    """
    src = (ROOT / "deploy.py").read_text(encoding="utf-8")

    i = src.index("secret_create_cmd(")
    call = src[i:src.index(")", src.index("aigc_bypass_secret", i))]
    for field in ("comfy_api_key", "aigc_studio_base_url", "aigc_bypass_secret"):
        assert field in call, f"CLI 部署漏传 Secret 字段: {field}"

    assert "save_config(" in src, "config 写入没走 save_config(原子 + 0600)"
    assert "CONFIG_DST.write_text" not in src, "还在直接 write_text 写 config"
    # 别再宣称与 GUI 等价 —— 它确实少做了几步
    assert "不等价于 GUI" in src, "docstring 应如实交代与 GUI 的差距"


def test_admin_capability_closes_bridge_key_and_config_bypass():
    """本机免配置；远程/反代必须 capability，且通用 config 不能改安全字段。"""
    import pytest
    from contract import is_direct_loopback_request, merge_public_config

    assert is_direct_loopback_request("127.0.0.1", "127.0.0.1:8188")
    assert is_direct_loopback_request("::1", "[::1]:8188")
    assert is_direct_loopback_request("::ffff:127.0.0.1", "localhost:8188")
    assert not is_direct_loopback_request("10.0.0.23", "10.0.0.5:8188")
    # 反向代理的 TCP peer 是 loopback,但浏览器访问 Host 是外部域名,不能当本机。
    assert not is_direct_loopback_request("127.0.0.1", "comfy.example.com")
    # 即使代理把 Host 也改成上游 localhost,常见 X-Forwarded-For 仍能识别远端。
    assert not is_direct_loopback_request("127.0.0.1", "127.0.0.1:8188", "10.0.0.23")

    cur = {"gpu_tier": "auto", "local_api_capability": "lc-secret",
           "bridge_api_key": "bk-secret"}
    assert merge_public_config(cur, {"gpu_tier": "top"})["gpu_tier"] == "top"
    for forbidden in ("local_api_capability", "bridge_api_key", "allow_remote_bridge_key"):
        with pytest.raises(ValueError):
            merge_public_config(cur, {forbidden: "attacker"})

    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    i = src.index('@routes.get("/modal_bridge/bridge_key")')
    body = src[i:src.index("@routes.", i + 10)]
    assert "@_admin_only" in body

    i = src.index('@routes.post("/modal_bridge/config")')
    config_body = src[i:src.index("@routes.", i + 10)]
    assert "@_admin_only" in config_body
    assert "contract.merge_public_config" in config_body


def test_local_api_capability_is_persistent_and_private(tmp_path, monkeypatch):
    import stat
    import config as cfg_mod

    path = tmp_path / "config.json"
    monkeypatch.setattr(cfg_mod, "_config_path", lambda: path)
    first = cfg_mod.ensure_local_api_capability()
    second = cfg_mod.ensure_local_api_capability()
    assert first == second and first.startswith("lc-") and len(first) >= 40
    assert cfg_mod.load_config()["local_api_capability"] == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_privileged_local_routes_are_all_admin_guarded():
    """只有脱敏配置/健康/平台状态/版本四个 GET 可匿名；其余路由不能漏装 guard。"""
    import re

    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    public = {
        ("get", "/modal_bridge/config"),
        ("get", "/modal_bridge/health"),
        ("get", "/modal_bridge/platform_status"),
        ("get", "/modal_bridge/version"),
    }
    found = re.findall(r'@routes\.(get|post)\("([^"]+)"\)(\n\s+@_admin_only)?', src)
    assert found
    for method, path, guard in found:
        if (method, path) in public:
            assert not guard, f"公开只读端点意外要求 capability:{method} {path}"
        else:
            assert guard, f"高权限端点漏了 @_admin_only:{method} {path}"


def test_local_busy_is_not_reported_as_modal_outage():
    """本地 ComfyUI 忙导致的版本检查超时,不许被归因成「Modal 平台故障」,更不许拦住提交。

    ComfyUI 是单进程 aiohttp + 同步执行图:KSampler 的 PyTorch 采样是同步阻塞调用,
    采样期间 event loop 调度不到。/version 那个 6 秒是**挂钟**超时,3 s/it 的工作流
    两个迭代就吃满 —— 请求还没轮到处理就 TimeoutError。

    2026-08-31 实测:本地跑图时点 RunModal 弹「Modal 平台故障」,而队列空闲后同一接口
    1.4 s 返回、各项全匹配,云端完全正常。归因是反的。

    ⚠ 而且它是**阻断式**的(checkVersionOrBlock 返回 false 就取消提交)——
    "本地忙的时候把活推到云端"恰恰是 RunModal 存在的理由,拦掉等于废掉核心场景。
    """
    py = (ROOT / "routes.py").read_text(encoding="utf-8")
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")

    # 后端：超时时先问本地队列，忙就归因成 local_busy
    assert "def _local_queue_busy(" in py, "缺少本地队列忙判定"
    i = py.index("except asyncio.TimeoutError:")
    seg = py[i:i + 900]
    assert "_local_queue_busy()" in seg, "超时分支没有区分本地忙"
    assert '"local_busy"' in seg, "没有 local_busy 这个归因"

    # 前端：local_busy 必须放行，且必须在查状态页之前
    k = js.index("if (!v.reachable) {")
    body = js[k:k + 2000]
    busy_at = body.find('v.err_kind === "local_busy"')
    outage_at = body.find("await isModalOutage()")
    assert busy_at > 0, "前端没有处理 local_busy"
    assert busy_at < outage_at, "local_busy 必须先短路，不该还去查状态页"
    assert "return true" in body[busy_at:outage_at], "local_busy 必须放行提交，不能拦"

    # platform 判定只认状态页，不再把 timeout/unreachable 算作平台故障
    assert 'outage || v.err_kind === "timeout"' not in js, \
        "又把 timeout 当成平台故障了 —— 本地一忙就会误报 Modal 挂了"


def test_command_failure_reaches_comfyui_log_and_frontend():
    """命令失败(典型:私有节点依赖装不上导致镜像构建挂掉)必须在两个地方留下真实原因。

    2026-08-31 实测报告:用户点 RunModal 后只看到「本地节点上传失败」,而
    **宿主机 ComfyUI 日志里一行痕迹都没有** —— 必须自己去 `modal app logs` 才看得到
    真错误(basicsr 在 Python 3.13 下 setup.py 取版本号失败)。

    两处缺口:
      1. _run_streamed 的输出只写进 HTTP 流,没有 print 到 ComfyUI 控制台;
      2. 前端拿到了完整错误行,却只 console.log 一份、再截断成 72 字符闪过进度窗,
         最后抛的是通用文案 —— 而且归因还错了:上传其实成功了,挂的是随后的依赖重部署。
    """
    py = (ROOT / "routes.py").read_text(encoding="utf-8")
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")

    # 后端：失败时把输出尾部回灌到服务端日志
    i = py.index("async def _run_streamed(")
    body = py[i:py.index("\n\n\n", i)]
    assert "tail" in body, "没有保留输出尾部"
    assert "if rc != 0 and tail:" in body, "失败时没有回灌日志"
    assert "print(" in body.split("if rc != 0 and tail:")[1], "尾部没有 print 到 ComfyUI 控制台"

    # 前端：失败要带出真实原因，不能只报一句通用文案
    j = js.index("async function syncLocalNodes(")
    fn = js[j:js.index("\n}", j)]
    assert "lastError" in fn, "前端没有留存后端标记的失败原因"
    assert "return { ok: false, message:" in fn, "失败没有把原因带回调用方"
    assert "return { ok: true }" in fn, "成功路径的返回值没有跟着改"
    assert "node.local_fail_detail" in js, "缺少带详情的失败文案"


def test_local_nodes_have_a_push_entry_point():
    """必须有一条「本机 → Volume」方向的手动同步入口,否则会形成互锁。

    2026-08-31 由 skybox-ai 会话报告、用户实际卡住:
    依赖清单以 Volume 的 manifest 为唯一真相源(多机场景下这是对的),而全项目**只有**
    ensureNodesAvailable 那一条路径会刷新它 —— 偏偏那条路径在提交前先过版本检查。
    于是「本地改了私有节点依赖」+「插件版本也变了」同时发生时:
        版本不一致 → 拦提交、让去 Setup 重新部署
          → 部署从 Volume 读到的还是旧 manifest → 构建照样失败 → 版本永远升不上去
          → 而唯一能刷新 manifest 的路径被第一步拦着
    用户从 UI 出不去,只能靠「先移除该节点再重部署」这种绕行。
    """
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")

    # Setup 面板里要有这个按钮，且它真的会调 sync_local_nodes
    assert 'id="mb-nodes-resync"' in js, "Setup 面板缺少「同步本机私有节点」按钮"
    i = js.index("nodesResyncBtn.onclick")
    body = js[i:i + 2200]
    assert "/modal_bridge/list_local_nodes" in body, "同步按钮没有取 Volume 上的节点列表"
    assert "/modal_bridge/sync_local_nodes" in body, "同步按钮没有调用上传端点"
    assert "refreshVerBanner" in body, "同步后没有刷新版本徽标(依赖变化会顺带重部署)"

    # 版本不一致的引导文案要提醒这条死路
    assert "同步本机私有节点" in js.split('"ver.mismatch_msg"')[1][:900], \
        "版本不一致的引导没提「先同步私有节点」—— 依赖也变了的话那条路是死的"


def _load_routes_func(name, extra_ns=None):
    """从 routes.py 里取出一个纯函数来单测。

    routes.py 顶层 import aiohttp / server,单测环境里进不来;而这些函数本身是纯逻辑。
    只 exec 需要的那几个顶层节点,不执行整个模块。
    """
    import ast
    import re as _re

    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns = {"re": _re, **(extra_ns or {})}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.FunctionDef)):
            dump = ast.dump(node)
            if name in dump or "_PIP_" in dump:
                exec(compile(ast.Module([node], []), "<routes>", "exec"), ns)
    return ns[name]


def test_diagnose_build_failure():
    """构建失败要能从 pip 输出里认出包名,给一句可操作的话;认不出就别硬猜。

    2026-08-31 实测:用户点部署 → 白等一轮构建 → 看到的是
    `File "<string>", line 79, in get_version` / `KeyError: '__version__'`,
    要翻 30 行 traceback 才能找到包名叫 basicsr。原始日志留着,但得有一句人话。
    """
    d = _load_routes_func("diagnose_build_failure")

    # ① 真实形态：现代 pip 只打 Collecting，报错紧跟其后，没有 "Failed building wheel" 行
    real = (
        "Collecting py360convert\n"
        "  Downloading py360convert-1.0.3-py3-none-any.whl\n"
        "Collecting basicsr\n"
        "  Downloading basicsr-1.4.2.tar.gz (172 kB)\n"
        "  Preparing metadata (setup.py) ... error\n"
        "  error: subprocess-exited-with-error\n"
        "  × python setup.py egg_info did not run successfully.\n"
        '  File "<string>", line 79, in get_version\n'
        "  KeyError: '__version__'\n"
    )
    out = d(real)
    assert "basicsr" in out, "没认出失败的包名"
    assert "py360convert" not in out, "认成了前一个成功的包"
    assert "requirements.txt" in out, "没给出可操作的处理办法"

    # ② 有显式标记时也要认
    assert "basicsr" in d("Collecting a\nFailed building wheel for basicsr\n")

    # ③ 认不出就返回空 —— 硬猜一个包名比不猜更糟
    assert d("some unrelated error\n") == ""
    assert d("") == ""


def test_deploy_syncs_local_nodes_before_reading_manifest():
    """部署流程必须先把本机私有节点推上 Volume,再去读 manifest 生成依赖清单。

    用户点「部署」的心智模型是"把我现在的状态推上去"。而依赖清单以 Volume 的 manifest
    为准,于是「本地改了私有节点 requirements → 点部署」会用旧 manifest 构建、照样失败。
    2026-08-31 实测:0.8.15 加了「同步本机私有节点」按钮,但用户不知道要先点它,
    白等了一轮构建 —— 入口存在 ≠ 用户知道要用它。
    """
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    i = src.index("# 3) 部署 app")
    seg = src[i:i + 3000]

    # ⚠ 锚点要用真实调用而不是裸名字:注释里也提到了 _refresh_local_node_reqs,
    # 用裸名字会匹配到那句注释、把顺序判反(这个坑本会话踩过三次)。
    sync_at = seg.find("await asyncio.to_thread(local_nodes.upload_local_nodes")
    read_at = seg.find("await asyncio.to_thread(_refresh_local_node_reqs")
    assert sync_at > 0, "部署流程里没有把本机私有节点推上 Volume"
    assert read_at > 0, "找不到依赖清单刷新调用,测试锚点需更新"
    assert sync_at < read_at, "同步必须在读 manifest 之前，否则读到的还是旧的"
    assert "plan_local_uploads" in seg, "没有先比对 digest（应只推有改动的）"
