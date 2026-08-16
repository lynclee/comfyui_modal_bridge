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
        assert boot.needs_refresh({}) == [] and boot.needs_refresh(None) == []
    finally:
        boot.DEST_DIR = orig


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
