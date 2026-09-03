"""
核心纯函数单测 —— 防回归(节点加/改/删规划、模型分类、下载中判定、VRAM 估算、ETA 格式)。

跑法(插件根目录):  python -m pytest tests/ -q
或不装 pytest:        python tests/test_core.py

只测不碰真实环境的纯逻辑;对依赖 ComfyUI(`import nodes`)/ 文件系统 / Modal 的点,用桩替换。
"""
import contextlib
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


def code_only(text: str) -> str:
    """把 Python 注释挖成空格,**保持字符偏移不变**。

    为什么需要:源码字符串断言里,如果同一个串在注释里也出现过,**删掉真代码测试照样绿**
    —— 注释兜住了它。2026-09-02 实测:把真代码里的串改名、注释原样保留,本文件有 4 条
    测试仍然全绿(cpu_tier_when_no_model / plan_local_uploads ×2 / setuptools<81)。
    静态扫描发现不了这类:真代码在的时候,串在代码里也在注释里,两边都成立。

    偏移不变是刻意的 —— 于是可以**用原文 index 出来的位置去切挖空后的文本**:
    定位仍可用注释当锚点(改注释会响,是 fail-loud),断言只看真代码。
    """
    import io
    import tokenize

    off, acc = [], 0
    for line in text.splitlines(True):
        off.append(acc)
        acc += len(line)
    buf = list(text)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                a = off[tok.start[0] - 1] + tok.start[1]
                b = off[tok.end[0] - 1] + tok.end[1]
                for i in range(a, min(b, len(buf))):
                    if buf[i] != "\n":
                        buf[i] = " "
    except Exception:
        return text          # 剥不动就退回原文(宁可弱一点,不要假失败)
    return "".join(buf)


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


def test_parse_import_failures_survives_ansi_log_prefix():
    """ComfyUI 新版给每行加了带 ANSI 色码的 [INFO] 前缀,解析器必须扛得住。

    2026-09-02 在 ComfyUI v0.34.2 上实测:真实字节是
        "\x1b[32m[INFO]\x1b[0m    0.0 seconds: /comfyui/custom_nodes/foo"
    而 _TIME_LINE 从行首锚定,前缀一来第一行就失配 → 循环里那个"块结束就 break"
    立刻退出 → 返回 {"ok": [], "failed": []}。

    ⚠ **失配的表现不是报错,是静默返回空**,于是:
      · node_compat_check 打「全部导入成功 ✓」—— 假绿;
      · import_failure_hint() 永远返回空串 —— 那条专门用来止住用户"反复点同步"
        的提示直接哑掉,而它存在的全部理由就是 ComfyUI 对"包 import 失败"和
        "包根本没装"给的是同一句话。
    上面两条老测试全用无前缀的旧格式,所以一个都没红。
    """
    cl = _comfy_log()
    log = (
        "\x1b[1m\x1b[33m[WARNING]\x1b[0m Cannot import /comfyui/custom_nodes/ComfyUI-Broken"
        " module for custom nodes: No module named 'foo'\n"
        "Import times for custom nodes:\n"
        "\x1b[32m[INFO]\x1b[0m    0.0 seconds: /comfyui/custom_nodes/websocket_image_save.py\n"
        "\x1b[32m[INFO]\x1b[0m    0.1 seconds: /comfyui/custom_nodes/rgthree-comfy\n"
        "\x1b[32m[INFO]\x1b[0m    0.5 seconds (IMPORT FAILED): /comfyui/custom_nodes/ComfyUI-Broken\n"
        "\x1b[32m[INFO]\x1b[0m \n"
        "\x1b[32m[INFO]\x1b[0m Starting server\n"
    )
    r = cl.parse_import_failures(log)
    assert r["ok"] == ["websocket_image_save", "rgthree-comfy"], r["ok"]
    assert [f["name"] for f in r["failed"]] == ["ComfyUI-Broken"]
    assert r["failed"][0]["error"] == "No module named 'foo'", "带前缀的 Cannot import 行没认出来"


def test_node_compat_check_does_not_call_zero_nodes_all_green():
    """一个节点都没解析到 ≠ 全绿 —— 那是解析器坏了,不是没问题。

    这条正是上面那个 bug 的"第二道门":即便解析器又被 ComfyUI 的日志改动打瘸,
    检测结果也必须显式说"没解析到",而不是打「全部导入成功 ✓」把人骗过去。
    """
    src = (ROOT / "modal_app" / "node_compat_check.py").read_text(encoding="utf-8")
    i = src.index('print("全部导入成功 ✓")')
    branch = src[max(0, i - 200):i]
    assert "elif ok:" in branch, \
        "「全部导入成功 ✓」没有以「确实解析到了节点」为前提 —— 解析器一坏就报假绿"
    assert "没有从启动日志里解析到任何节点" in src, "缺少 ok 与 failed 都为空时的显式告警分支"


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


def test_selfrun_entrypoint_is_last_so_no_test_is_skipped():
    """`if __name__ == "__main__":` 必须是文件里**最后一个**顶层语句。

    2026-09-02 codex 抓到:它当时在 2957 行文件的第 1685 行。`python tests/test_core.py`
    从上往下执行,跑到那儿就 `sys.exit()` —— 后面 1272 行的 **36 条测试连定义都没执行到**。
    而 CI 用的正是这条路径,于是这些从来没在 CI 里跑过:
      路径囚笼(test_output_path_jail ×3)、admin 守卫、`test_no_api_key_in_query_string`、
      `test_no_runtime_pip_install_anywhere`(Registry 合规)、全部 sage 与 basicsr 补丁测试。
    **CI 一直是绿的** —— 漏跑和全过在报告里长得一模一样。

    所以这条不测行为,测的是"门禁自己有没有被绕过"。
    """
    import ast

    src = (HERE / "test_core.py").read_text(encoding="utf-8")
    body = ast.parse(src).body
    idx = [i for i, n in enumerate(body)
           if isinstance(n, ast.If) and ast.unparse(n.test) == "__name__ == '__main__'"]
    assert len(idx) == 1, f"期望恰好一个 __main__ 块,实际 {len(idx)} 个"
    after = [n for n in body[idx[0] + 1:]]
    names = [getattr(n, "name", type(n).__name__) for n in after]
    assert not after, (
        f"__main__ 块后面还有 {len(after)} 个顶层语句:{names[:8]} —— "
        f"`python tests/test_core.py` 执行到 sys.exit() 就停,它们永远不会被定义、更不会被跑。"
        f"把 __main__ 块移到文件最末尾。")


def test_config_written_without_any_0644_window(tmp_path, monkeypatch):
    """写 config 必须**直接以 0600 创建**,不能"先落地再 chmod"。

    2026-09-02 codex 抓到:save_config 的 docstring 明写"不能先 write_text 再 chmod"
    (会有 0644 窗口、崩在 chmod 前会留下明文凭据、还吞掉 chmod 失败),而实现正是那三条。
    这条不看源码字符串 —— 钩住 os.open 抓它实际传的创建模式,再核最终文件权限。
    """
    import os
    import stat

    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kw):
        if flags & os.O_CREAT:
            modes.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "config.json")

    config.save_config({"endpoint": "https://example.invalid", "bridge_api_key": "bk-x"})

    assert modes, "没有用 os.open 创建 —— 说明还是 write_text 那条路径,存在 0644 窗口"
    for m in modes:
        assert m & 0o077 == 0, f"创建模式 {oct(m)} 带了 group/other 位,窗口期同机可读凭据"

    got = stat.S_IMODE((tmp_path / "config.json").stat().st_mode)
    assert got == 0o600, f"最终权限 {oct(got)},应为 0600"


def test_config_write_does_not_swallow_permission_failure(tmp_path, monkeypatch):
    """收权限失败必须抛出来 —— 吞掉等于"权限没设上却无人知晓"。"""
    import os

    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "config.json")

    def boom(*a, **kw):
        raise OSError("fchmod denied")

    monkeypatch.setattr(os, "fchmod", boom)
    with raises(OSError):
        config.save_config({"endpoint": "x"})


@contextlib.contextmanager
def raises(exc):
    """`pytest.raises` 的最小替身。

    ⚠ 不能在测试里直接 `import pytest`:README 给了"不装 pytest 就 `python tests/test_core.py`"
    这条备选跑法,而那条路径下 import 会直接挂。2026-09-02 把自跑入口挪到文件末尾后,
    原本落在被跳过区段里的 `import pytest` 才第一次真正生效 —— 在装了 pytest 的机器上
    看不出问题,正是"漏跑和全过长得一样"的又一例。
    """
    try:
        yield
    except exc:
        return
    except Exception as e:                       # noqa: BLE001
        raise AssertionError(f"期望 {exc.__name__},实际 {type(e).__name__}: {e}") from e
    raise AssertionError(f"期望抛 {exc.__name__},但什么都没抛")


def test_aigc_secret_can_actually_be_cleared():
    """停用 AIGC 集成必须能把旁路密钥**清掉**,不能只允许更新。

    2026-09-02 codex 抓到:密码框留空 = 沿用已存(密码框的标准语义),而后端也是
    `new or stored` —— 于是密钥**只能被更新、无法被删除**。用户清掉 URL 停用集成后,
    密钥仍躺在本地 config.json 里,并被烤进下一次创建的 Modal Secret。

    密钥不在 PUBLIC_CONFIG_WRITE_FIELDS 里(那道闸挡的是"改配置再取密钥"的两步绕过),
    所以清除入口只能挂在"显式把 URL 置空"这个动作上 —— 只清、不读、不回吐,不构成绕过。
    """
    cur = {"aigc_studio_base_url": "https://x.example", "aigc_bypass_secret": "byp-1",
           "gpu_tier": "auto"}

    # ① 显式停用 → 密钥必须跟着没
    out = contract.merge_public_config(cur, {"aigc_studio_base_url": ""})
    assert out["aigc_studio_base_url"] == ""
    assert out["aigc_bypass_secret"] == "", "停用了集成,密钥却还留在 config 里"

    # ② 改别的字段不能有副作用 —— 用户可能先填了密钥还没填 URL
    cur2 = {"aigc_studio_base_url": "", "aigc_bypass_secret": "byp-1", "gpu_tier": "auto"}
    out2 = contract.merge_public_config(cur2, {"gpu_tier": "top"})
    assert out2["aigc_bypass_secret"] == "byp-1", "改无关字段把密钥无声抹掉了"

    # ③ 密钥本身仍然不能经这个 API 写入(原有的两步绕过闸不许被这次改动打开)
    try:
        contract.merge_public_config(cur, {"aigc_bypass_secret": "byp-2"})
    except ValueError:
        pass
    else:
        raise AssertionError("aigc_bypass_secret 竟然能经通用 config API 写入")


def test_deploy_aigc_secret_three_states():
    """/setup 里旁路密钥必须是三态,且顺序固定:显式输入 > URL 存在则沿用 > 否则清掉。

    review 抓到第一版写成"URL 为空就清":用户在部署面板刚输入的密钥也被静默丢掉 ——
    而 /config 那条路径对"先填密钥、URL 稍后填"这个场景是特意保护过的,两边自相矛盾。
    这里取三个 guard 行整行逐字比对(子串包含会被 else-if 包一层骗过,本轮已踩四次)。
    """
    src = code_only((ROOT / "routes.py").read_text(encoding="utf-8"))
    i = src.index('_typed = (body.get("aigc_bypass_secret") or "").strip()')
    seg = src[i:i + 500]
    lines = [ln.strip() for ln in seg.splitlines() if ln.strip()]
    assert lines[:6] == [
        '_typed = (body.get("aigc_bypass_secret") or "").strip()',
        "if _typed:",
        "aigc_bypass = _typed",
        "elif aigc_base_url:",
        'aigc_bypass = cfg.get("aigc_bypass_secret", "")',
        "else:",
    ], f"密钥三态或顺序被改了。实际读到:{lines[:6]}"
    assert lines[6] == 'aigc_bypass = ""', f"URL 为空时没有真的清掉:{lines[6]!r}"


def test_fetch_stage_label_tells_the_truth_about_which_path():
    """取回阶段的文案必须按**实际走的通道**分支,不能写死「Decoding base64」。

    2026-09-03 用户反馈:8K 全景图工作流"卡在 Downloading result",下面一行是
    「Decoding base64...」,前后一小时。实际没卡 —— 大产物 >volume_threshold_mb(默认 8MB)
    走 Volume 直连下载,**根本不解码**。那句文案是无条件写死的,于是几十分钟的下载被
    显示成一句既静态、又说错了路径的提示;而"慢"和"挂住"在静态文案下完全一样。

    分流规则在云端 _comfy_ws.materialize_*:>阈值 → rec["volume_path"],否则 rec["data_base64"]。
    所以前端在调 /fetch_result 之前就能从 final.images 判断走哪条。
    """
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")

    # ⚠ 断言必须盯**代码形态**,不能搜那句散文。写成 `"Decoding base64..." not in js`
    #   会撞上本次修复在源码里留下的解释性注释 —— 这轮已经第三次踩到"断言匹配到自己
    #   写的注释",而这次是反方向(否定式断言被注释触发,永远为假)。
    #   这里锚的是那个模板字面量本身,散文里不会出现。
    assert "${batchSuffix}Decoding base64" not in js, \
        "又出现了写死的解码文案 —— 走 Volume 时它是错的,而且一挂一小时"

    i = js.index('ctx.stage("downloading"')
    seg = js[i - 900:i + 400]
    assert "volume_path" in seg, "取回文案没有按 volume_path 分支,又变回一句写死的了"
    assert 't("run.fetch_volume"' in seg and 't("run.fetch_decode"' in seg, \
        "两条通道必须各有自己的文案"

    # 进度轮询必须存在:/fetch_result 是一次阻塞 POST,没有它大文件下载期间前端零信号
    assert "/modal_bridge/fetch_progress" in js, "没有取回进度轮询"
    # 三个数缺一不可 —— 用户明确要的是速度,而停滞时长才回答"到底卡没卡"
    for key in ("run.fetch_progress", "run.fetch_stalled"):
        assert f'"{key}"' in js, f"缺文案 {key}"
    # ⚠ 不能写 `"j.stalled_s" in js` —— 那只证明**字符串存在**,不证明分支会执行。
    #   实测:把 `if (j.stalled_s >= 20) {` 改成 `if (false) {`,该串仍留在下面那句
    #   t("run.fetch_stalled", { secs: j.stalled_s }) 里,断言照样绿(2026-09-03 当场踩到,
    #   本轮第四次同类)。和 codex 演示的"把校验包进 if false 仍能骗过字符串断言"同源。
    #   所以取整行逐字比对。
    guards = [ln.strip() for ln in js.splitlines() if "stalled_s >=" in ln]
    assert guards == ["if (j.stalled_s >= 20) {"], \
        f"停滞判据被改写或去掉了 —— 那就无法区分「慢」和「挂住」。实际读到:{guards}"
    rate = [ln.strip() for ln in js.splitlines() if "fmtRate(" in ln and "function" not in ln]
    assert rate == ["const spd = j.bps ? fmtRate(j.bps) : \"\";"], \
        f"下载速度的显示被改了(用户明确要的就是这个)。实际读到:{rate}"


def test_config_save_works_where_fchmod_is_missing(tmp_path, monkeypatch):
    """没有 os.fchmod 的平台(Windows + Python ≤3.12)上保存配置必须照常成功。

    os.fchmod 在 Windows 上 **3.13 才有**(python/cpython#113191)。ComfyUI Desktop 大量跑在
    Windows + 3.12,而 review 抓到第一版无条件调用它 —— 等于让 Windows 上每一次保存配置
    都 AttributeError,插件直接不可用。这条模拟"属性不存在",不是"调用失败":两者语义不同,
    前者要跳过,后者仍须抛(见下一条)。
    """
    import os

    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "config.json")
    monkeypatch.delattr(os, "fchmod", raising=True)
    config.save_config({"endpoint": "https://example.invalid", "bridge_api_key": "bk-x"})
    assert (tmp_path / "config.json").exists(), "无 fchmod 的平台上保存失败了"
    import json
    assert json.loads((tmp_path / "config.json").read_text())["bridge_api_key"] == "bk-x"


def test_download_stall_is_shown_even_at_zero_bytes():
    """一个字节都没到就挂住,是最坏的那种挂法 —— 停滞提示必须先于"没字节就返回"。

    review 抓到第一版 `if (!j.ok || !j.done) return;` 排在停滞判断之前:恰好让 0 字节的
    挂住永远显示不出来,而那正是用户最需要被告知的情况。整行按顺序比对。
    """
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")
    i = js.index("/modal_bridge/fetch_progress?job_id=")
    seg = js[i:i + 1800]
    order = [ln.strip() for ln in seg.splitlines()
             if ln.strip() in ("if (!j.ok) return;",
                               "if (j.stalled_s >= 20) {",
                               "if (!j.done) return;   // 还没开始写、且没停滞 → 保持静态文案")]
    assert order == [
        "if (!j.ok) return;",
        "if (j.stalled_s >= 20) {",
        "if (!j.done) return;   // 还没开始写、且没停滞 → 保持静态文案",
    ], f"停滞判断与「无字节即返回」的顺序不对,0 字节挂住会显示不出来。实际:{order}"


def _sage_patch_cmd() -> str:
    """从 modal_image.py 源码里抽出那条 sage 补丁命令。

    用 ast 而不是 import —— modal_image 顶层 `import modal`,CI 与宿主机都不保证装了。
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
    return patch[0]


def test_modal_image_has_sage_patches():
    """镜像必须带 SageAttention 的两个上游缺陷补丁,且校验是 fail-closed 的。

    ① int32 行偏移溢出(**已致尾帧塌坏**):H3 的 fused QKV(seq-stride=21504)下
       行号 ≥ 2^31/21504 ≈ 99865 即 wrap 成负 → 尾几帧塌灰噪 / 偶发 illegal memory access。
    ② V 走 strided 视图时 kernel 地址算术在 uint32 域回绕(**当前打不着,前瞻防护**):
       TransposePadPermuteKernel 的 stride 与 thread_base_token 全是 uint32_t,
       回绕点 = 2^32/stride_seq = 199,729;还要 kv_len % 128 == 0(否则 core.py:976
       的 cat 把 V 物化成连续、stride 降到 128)。详见 modal_image.py 的长注释。

    两个上游都未修。这层删了不会有任何报错,只会在特定配置下静默出坏帧或崩掉,
    所以这里静态钉死它还在、且保留了全部校验闸。
    """
    import re

    cmd = _sage_patch_cmd()

    # ① int64
    assert "sed -i -E" in cmd and "offs_n" in cmd and "stride_" in cmd
    assert '" = 0' in cmd, "缺少『脆弱写法清零』断言"

    # 必须打**整个 triton/ 目录**,不能退回单文件。同一条正则在 quant_per_block.py 与
    # quant_per_block_varlen.py 也逐字命中,而那两个文件一处 int64 都没有。它们只在
    # sm86 走到,当前被 _worker_boot 的 compute_cap {8.9, 9.0} 门控挡住 —— 但那是
    # 两个独立决定之间的隐式耦合:白名单一放宽到 8.6(A10G)就无声打开同一个 bug。
    assert '"$T"/*.py' in cmd, "① 必须对整个 triton/ 目录打补丁,退回单文件会漏 quant_per_block*"
    assert "'triton'" in cmd, "取的应该是 triton 目录,不是某个具体文件"

    m = re.search(r"-ge (\d+)", cmd)
    assert m, "缺少『int64 转换够数』断言"
    assert int(m.group(1)) >= 16, (
        f"int64 下限只有 {m.group(1)},但全目录实际应命中 16 处"
        "(quant_per_thread 12 + quant_per_block 2 + quant_per_block_varlen 2)——"
        "下限设低了,漏打文件也能过闸")
    # ② v.contiguous()
    assert "v = v.contiguous()" in cmd, "缺少 V strided 补丁"
    assert "transpose_pad_permute_cuda" in cmd, "V 补丁锚点丢失"
    assert "grep -q 'v = v.contiguous()'" in cmd, "V 补丁缺少幂等判断"
    # ③ 语法校验(目录级)
    assert "ast.parse" in cmd, "缺少补丁后语法校验"
    assert "glob.glob" in cmd, "语法校验必须覆盖整个目录,不能只查单个文件"


def _fake_sage_tree(tmp, *, with_per_block=True):
    """造一棵最小的 sageattention 包树,行形态取自 d1a57a5 真源码。

    返回值可直接用 PYTHONPATH 指过去 —— 补丁命令里那两句
    `python -c "import sageattention..."` 就能原样跑,不必改命令。
    """
    import os

    pkg = os.path.join(tmp, "sageattention")
    trt = os.path.join(pkg, "triton")
    os.makedirs(trt, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    open(os.path.join(trt, "__init__.py"), "w").close()

    # ① 量化 kernel:offs_n 跨整个序列(off_blk = program_id(0))→ 真有洞,必须被打
    # 计数照实反映 d1a57a5 真源码:quant_per_thread 共 12 处
    # (plain offs_n ×8、offs_n0 ×2、offs_n1 ×2),per_block 两个文件各 2 处 —— 合计 16。
    # 假树造瘦了会被 `-ge 16` 闸拦下(第一版就是这么炸的,闸本身是对的)。
    per_thread = (
        "import triton.language as tl\n"
        "def kern():\n"
        "    offs_n = off_blk * BLK + tl.arange(0, BLK // 8) * 8 + off_tld\n"
        + "".join(
            f"    p{i} = P + offs_n[:, None] * stride_in + offs_k[None, :]\n"
            f"    q{i} = Q + offs_n[:, None] * stride_on + offs_k[None, :]\n"
            for i in range(4)
        )
        + "    a = A + offs_n0[:, None] * stride_in\n"
        "    b = B + offs_n0[:, None] * stride_on\n"
        "    c = C + offs_n1[:, None] * stride_in\n"
        "    d = D + offs_n1[:, None] * stride_on\n"
    )
    open(os.path.join(trt, "quant_per_thread.py"), "w").write(per_thread)

    if with_per_block:
        per_block = (
            "import triton.language as tl\n"
            "def kern():\n"
            "    offs_n = off_blk * BLK + tl.arange(0, BLK)\n"
            "    input_ptrs = Input + offs_n[:, None] * stride_in + offs_k[None, :]\n"
            "    output_ptrs = Output + offs_n[:, None] * stride_on + offs_k[None, :]\n"
        )
        open(os.path.join(trt, "quant_per_block.py"), "w").write(per_block)
        open(os.path.join(trt, "quant_per_block_varlen.py"), "w").write(per_block)

    # ② attention kernel:offs_n = tl.arange(0, BLOCK_N),BLOCK_N=64 → 最大 63,溢不出。
    #    形态与上面一模一样,只有 stride 名不同 —— **必须原样不动**。
    attn = (
        "import triton.language as tl\n"
        "def kern():\n"
        "    off_z = tl.program_id(2).to(tl.int64)\n"
        "    off_h = tl.program_id(1).to(tl.int64)\n"
        "    offs_n = tl.arange(0, BLOCK_N)\n"
        "    V_ptrs = V + offs_n[:, None] * stride_vn + offs_k[None, :]\n"
    )
    open(os.path.join(trt, "attn_qk_int8_per_block.py"), "w").write(attn)

    open(os.path.join(pkg, "quant.py"), "w").write(
        "def per_channel_fp8(v):\n"
        "    _fused.transpose_pad_permute_cuda(v, v_transposed_permutted, _tensor_layout)\n"
        "    return v\n"
    )
    return pkg


def _run_sage_patch(cmd, tmp):
    """在 tmp 这棵假树上真跑补丁命令,返回 (rc, output)。"""
    import os
    import shutil
    import subprocess
    import tempfile

    binhome = tempfile.mkdtemp(prefix="mb-sage-bin-")
    real_py = shutil.which("python") or shutil.which("python3")
    link = os.path.join(binhome, "python")
    if not os.path.exists(link):
        os.symlink(real_py, link)
    env = dict(os.environ,
               PATH=binhome + os.pathsep + os.environ.get("PATH", ""),
               PYTHONPATH=tmp)
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


def test_sage_patch_actually_runs_and_hits_exactly_the_right_lines():
    """**真跑那条 sage 补丁命令**,把两条边界都锁住 —— 不是检查源码字符串。

    为什么必须真跑:同文件里那条 test_modal_image_has_sage_patches 是字符串检查,
    而 2026-09-02 这一轮它对一个真 bug **毫无反应** —— 当时残留断言被写成宽松的
    `stride_` 任意后缀,把 attn_qk_int8_*.py 里 4 处安全代码算成残留,144 条测试
    全绿,是**镜像构建炸了**才发现。字符串检查看不出范围对不对。

    两条边界(缺一不可):
      · 范围要够宽 —— quant_per_block* 必须被打(它们和 quant_per_thread 同样是
        `offs_n = off_blk * BLK + ...`,off_blk 是 program_id(0),跨整个序列);
      · 范围不能更宽 —— attn_qk_int8_* 里的 `stride_vn` 必须**原样不动**
        (那里 `offs_n = tl.arange(0, BLOCK_N)`,BLOCK_N=64,最大 63,溢不出;
        跨序列部分靠指针累加,而上游已把 off_z/off_h 转成 int64)。
    """
    import os
    import tempfile

    cmd = _sage_patch_cmd()
    tmp = tempfile.mkdtemp(prefix="mb-sage-")
    pkg = _fake_sage_tree(tmp)

    rc, out = _run_sage_patch(cmd, tmp)
    assert rc == 0, f"补丁命令在正常树上失败了: {out}"
    assert "sage patches OK" in out, out

    trt = os.path.join(pkg, "triton")

    def read(name):
        return open(os.path.join(trt, name), encoding="utf-8").read()

    # 范围够宽:三个量化文件都打上,且不留脆弱写法
    for name, want in (("quant_per_thread.py", 12), ("quant_per_block.py", 2),
                       ("quant_per_block_varlen.py", 2)):
        src = read(name)
        assert src.count("to(tl.int64)") == want, f"{name} 期望 {want} 处 int64,实际 {src.count('to(tl.int64)')}"
        assert "offs_n[:, None] * stride_in" not in src, f"{name} 仍有脆弱写法"
        assert "offs_n[:, None] * stride_on" not in src, f"{name} 仍有脆弱写法"

    # 范围不更宽:attention kernel 的 stride_vn 一个字节都不许动
    attn = read("attn_qk_int8_per_block.py")
    assert "offs_n[:, None] * stride_vn" in attn, \
        "stride_vn 被改了 —— 那里 offs_n 最大 63,不该动;改了会让『哪里真有洞』失真"
    assert attn.count("to(tl.int64)") == 2, "attention kernel 里只该有上游自带的两处 int64"

    # ② V contiguous:恰好一处,且在调用之前
    q = open(os.path.join(pkg, "quant.py"), encoding="utf-8").read()
    assert q.count("v = v.contiguous()") == 1, q
    assert q.index("v = v.contiguous()") < q.index("_fused.transpose_pad_permute_cuda"), \
        "contiguous 必须在调用之前"

    # 幂等:再跑一次不重复改、也不报错
    rc2, out2 = _run_sage_patch(cmd, tmp)
    assert rc2 == 0, f"幂等路径失败: {out2}"
    q2 = open(os.path.join(pkg, "quant.py"), encoding="utf-8").read()
    assert q2.count("v = v.contiguous()") == 1, "第二次跑重复插入了"


def test_sage_patch_fails_closed_when_a_file_is_missing():
    """少打一个文件必须让**构建失败**,而不是悄悄少覆盖。

    这正是 2026-09-02 之前的状态:补丁只打 quant_per_thread.py,quant_per_block*
    漏着;而当时的断言是 `to(tl.int64) >= 4` —— **计数只能证明「打过」,证明不了
    「打全了」**,于是漏打一直没被发现。现在的 int64 下限 16 就是为了挡住这个。

    副作用是上游若删掉 quant_per_block* 会让构建失败 —— 这是刻意的:那种结构变更
    必须由人重新核一遍分派,不能默认放行。
    """
    import tempfile

    cmd = _sage_patch_cmd()
    tmp = tempfile.mkdtemp(prefix="mb-sage-short-")
    _fake_sage_tree(tmp, with_per_block=False)   # 只有 quant_per_thread(6 处)

    rc, out = _run_sage_patch(cmd, tmp)
    assert rc != 0, f"少了 quant_per_block* 却通过了闸 —— 漏打会被静默放行: {out}"
    assert "sage patches OK" not in out, out


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
    # AIGC 的两个字段刻意分处两地:URL 在设置页(非凭据、可经 /config 写),
    # 旁路密钥留在面板的专用密码框(凭据,不进 comfy.settings.json)。
    assert 'id="mb-dep-aigc-url"' not in src, "AIGC URL 应该在设置页,不该回到面板"

    # ② sage 开关：注册、默认关、归 Advanced
    i = src.find('id: "ModalBridge.useSageAttention"')
    assert i > 0, "设置项 ModalBridge.useSageAttention 没注册"
    block = src[i:i + 400]
    assert "defaultValue: false" in block, "sage 默认值不是 false"
    assert '"Advanced"' in block, "sage 没归到 Advanced 分组"

    # ③ 只有 URL 进 Settings；**密钥绝不能是 ComfyUI Setting**
    k = src.find('id: "ModalBridge.aigcStudioUrl"')
    assert k > 0, "设置项 ModalBridge.aigcStudioUrl 没注册"
    blk = src[k:k + 400]
    assert '"Advanced"' in blk and 'type: "text"' in blk
    assert "syncAigcFieldToConfig" in blk, "URL 没有把值同步回 config"

    # ⚠ 旁路密钥是凭据。注册成 ComfyUI Setting 会明文落进 comfy.settings.json(0644),
    # 且任何第三方 custom node 的 JS 都读得到;而 /config 的 allowlist 是为挡住
    # 「先改配置再取 key」那类两步绕过设的,把凭据加进去等于自己开口子。
    # 2026-08-31 一度两者都进了 Settings —— 结果是最糟的组合:泄露面扩大,而 allowlist
    # 又拒收,部署根本没拿到新值(codex review 抓到)。密钥只走部署面板的专用密码框。
    assert 'id: "ModalBridge.aigcBypassSecret"' not in src, \
        "旁路密钥不许注册成 ComfyUI Setting(明文落盘 + 第三方插件可读)"
    assert 'id="mb-dep-aigc-bypass"' in src, "部署面板缺少密钥的专用密码框"
    assert 'setSettingValue("ModalBridge.aigcStudioUrl"' in src, "URL 应当用 config 真值回填"

    import contract as _c
    assert "aigc_studio_base_url" in _c.PUBLIC_CONFIG_WRITE_FIELDS, \
        "URL 不在 allowlist 里,设置页改了也写不进 config"
    assert "aigc_bypass_secret" not in _c.PUBLIC_CONFIG_WRITE_FIELDS, \
        "凭据进了通用 config allowlist —— 那道闸就是为挡住这个而设的"


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
    # 断言全部只看真代码:注释里也讲了 cpu_tier_when_no_model,不挖空的话
    # 把判据从代码里删掉、只留注释,这条测试照样绿(2026-09-02 变异实测)。
    src = code_only((ROOT / "routes.py").read_text(encoding="utf-8"))
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
        with raises(ValueError):
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
    真错误(当时是 basicsr 在 Python 3.13 下 setup.py 取版本号失败)。

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


def test_single_push_entry_point():
    """「把本机状态弄到云端」必须只有一个入口,且它自己会分流。

    历史:0.8.15 为了解互锁加过一个独立的「同步本机私有节点」按钮,与「部署」并列。
    但两者职责高度重叠(同步后依赖变了会自动部署;部署前会自动比对 digest 推节点),
    用户却要先想明白"我这次改的是代码还是依赖"才知道点哪个 —— 而这恰恰是系统自己
    完全能判断的事(digest 比对 + 依赖指纹比对)。2026-08-31 用户明确要求合并。

    根源不是命名,是私有节点的代码和依赖走了两条路:代码进 Volume 的 zip(worker 启动
    解压、秒级生效),依赖的**声明**进 Volume 的 manifest、**安装**却发生在镜像 build 期。
    这个分离有它的理由(改代码不必重建镜像),但那是实现约束,不该变成用户的认知负担。
    """
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")

    # 只剩一个主动作，且名字说的是目的而不是手段
    assert "mb-nodes-resync" not in js, "又出现了与主按钮并列的第二个推送入口"
    assert "推送到云端" in js, "主按钮应叫「推送到云端」而不是「部署」"

    # 而它真的会分流：推节点 + 按需重建（后端顺序由 test_deploy_syncs_... 钉死）
    py = (ROOT / "routes.py").read_text(encoding="utf-8")
    i = py.index("# 3) 部署 app")          # 锚点用原文(注释;改了会响,fail-loud)
    seg = code_only(py)[i:i + 3000]        # 断言用挖空版(偏移相同)
    assert "plan_local_uploads" in seg, "主流程没有自动比对本机 digest"
    assert "await asyncio.to_thread(local_nodes.upload_local_nodes" in seg, \
        "主流程没有自动推送有改动的私有节点"

def test_diagnose_build_failure():
    """构建失败要能从 pip 输出里认出包名,给一句可操作的话;认不出就别硬猜。

    2026-08-31 实测:用户点部署 → 白等一轮构建 → 看到的是
    `File "<string>", line 79, in get_version` / `KeyError: '__version__'`,
    要翻 30 行 traceback 才能找到包名叫 basicsr。原始日志留着,但得有一句人话。
    """
    from node_sync import diagnose_build_failure as d

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
    i = src.index("# 3) 部署 app")          # 锚点用原文(注释)
    # 窗口要够大：这一段随着 fail-closed 分支的加入长了不少，切太短会把锚点切掉
    seg = code_only(src)[i:i + 6000]       # 断言用挖空版(偏移相同)

    # ⚠ 锚点要用真实调用而不是裸名字:注释里也提到了 _refresh_local_node_reqs,
    # 用裸名字会匹配到那句注释、把顺序判反(这个坑本会话踩过三次)。
    sync_at = seg.find("await asyncio.to_thread(local_nodes.upload_local_nodes")
    read_at = seg.find("await asyncio.to_thread(_refresh_local_node_reqs")
    assert sync_at > 0, "部署流程里没有把本机私有节点推上 Volume"
    assert read_at > 0, "找不到依赖清单刷新调用,测试锚点需更新"
    assert sync_at < read_at, "同步必须在读 manifest 之前，否则读到的还是旧的"
    assert "plan_local_uploads" in seg, "没有先比对 digest（应只推有改动的）"


def test_import_failure_hint_and_boot_capture():
    """节点整包 import 失败时,必须能把真因带回前端,而不是只报「节点不存在」。

    2026-08-31 实测(art-venture):setuptools≥84 移除了 pkg_resources,整包 IMPORT FAILED。
    worker 日志里写得清清楚楚是 ModuleNotFoundError,但回到前端只剩一句
    "Node 'LLM API Config' not found. The custom node may not be installed."
    —— 用户完全无从推断是依赖缺失,只会以为节点没同步上去、反复去点同步(而同步是好的)。

    ComfyUI 对「包 import 失败」和「包根本没装」给的是同一句话,但处理办法完全相反。
    """
    src = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")

    # ① 启动输出必须被捕获，否则无从得知 IMPORT FAILED
    assert "stdout=subprocess.PIPE" in src, "worker 没有捕获 ComfyUI 启动输出"
    assert "_pump_comfy_output" in src, "缺少输出转发线程"
    # ⚠ stdout=PIPE 而不持续读会让 ComfyUI 阻塞在 write 上，pump 是硬要求
    pump = src[src.index("def _pump_comfy_output"):src.index("def import_failure_hint")]
    assert "print(line" in pump, "转发线程没把输出打回容器日志(可观测性会丢)"
    assert "for line in proc.stdout" in pump, "没有持续读管道"
    # 捕获失败要能退化，不能让 worker 起不来
    boot = src[src.index("_BOOT_LOG[:] = []"):]
    boot = boot[:boot.index("wait_comfy_ready")]
    assert "except Exception" in boot and "subprocess.Popen(cmd)" in boot, \
        "捕获失败时没有退化成不捕获 —— 这会让 worker 起不来"

    # ② 失败路径要调用诊断
    i = src.index("import_failure_hint(m.group(1)")
    seg = src[max(0, i - 700):i + 300]
    assert "not found" in seg, "没有针对「节点不存在」这类错误"
    assert '"status": "failed"' in seg, "诊断没有接在写 failed 状态的路径上"


def test_image_pins_setuptools_for_pkg_resources():
    """镜像必须钉住提供 pkg_resources 的 setuptools。

    setuptools 84.0.0 把 pkg_resources 整个移除了(实测:80.9.0 的 wheel 里 19 个文件、
    84.0.0 里 0 个),而 `import pkg_resources` 是 2023 年前一大批 custom_node 的标配。
    云端装到 ≥84 时这些节点会整包 IMPORT FAILED,而本地 venv 自带旧 setuptools 从不暴露
    —— 和 basicsr 那次同构:本地"缺了也能跑",上云是全有或全无。

    不怕拖累构建:pip 默认开 build isolation,构建别的包用的是隔离环境里的新 setuptools。

    ⚠ 这条原来写的是 `assert "setuptools<81" in src`,**是恒真的**(2026-09-02 变异实测):
      · 把整行钉法删掉 → 仍然绿,因为同文件的注释里也有 "setuptools<81";
      · 把它改成坏钉法 `setuptools<811`(放行 84+,正是本条要防的)→ 也仍然绿,
        因为原串是它的前缀。
      字符串包含关系既证明不了"存在于代码",也证明不了"值是对的"。
      现在改成从 ast 里取 pip_install 的真实参数、并解析版本上界。
    """
    import ast
    import re

    tree = ast.parse((ROOT / "modal_app" / "modal_image.py").read_text(encoding="utf-8"))
    args = [
        a.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "pip_install"
        for a in n.args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    pins = [a for a in args if a.replace(" ", "").lower().startswith("setuptools")]
    assert pins, "镜像的 pip_install 里根本没有 setuptools —— 老节点会因缺 pkg_resources 整包挂掉"

    bounds = []
    for pin in pins:
        m = re.search(r"setuptools\s*<\s*(\d+)", pin.replace(" ", ""))
        assert m, f"setuptools 依赖写成了 {pin!r},没有上界 —— 会装到移除了 pkg_resources 的版本"
        bounds.append(int(m.group(1)))
    assert max(bounds) <= 81, (
        f"setuptools 上界是 {max(bounds)},但 84.0.0 起 pkg_resources 已被整个移除"
        f"(实测 80.9.0 的 wheel 里 19 个文件、84.0.0 里 0 个)—— 上界必须 ≤ 81")


def test_image_python_version_is_consistent_everywhere():
    """镜像的 Python 版本在三处出现,任何一处走散都是**部署直接失败**或**诊断说瞎话**。

      ① modal_image.py 的 add_python=      —— 镜像装哪个解释器
      ② 同文件里 SageAttention wheel 的 ABI tag(cpXY)—— 那个 wheel 带 C 扩展,
         ABI 锁死在某个 CPython;和 ① 对不上时 pip 判 not supported、镜像构建失败。
         这是**成对的**,历史上正是它把 3.13→3.12 卡住了(得先重编一份 cp312 wheel)。
      ③ node_sync.IMAGE_PYTHON_VERSION —— 构建失败时那句"当前 X.Y"提示。写死版本号
         而不是 import modal_image,是因为后者 `import modal`,而宿主机不保证装了 modal。

    ①② 走散会在部署时炸(响);③ 走散只会让提示悄悄说错版本(不响)—— 后者更该测。
    """
    import re

    src = (ROOT / "modal_app" / "modal_image.py").read_text(encoding="utf-8")

    m = re.search(r'add_python="(\d+\.\d+)"', src)
    assert m, "modal_image.py 里找不到 add_python="
    pyver = m.group(1)

    abis = set(re.findall(r"sageattention-[\d.]+-(cp\d+)-cp\d+-", src))
    assert len(abis) == 1, f"wheel 的 ABI tag 不唯一: {abis}"
    want_abi = "cp" + pyver.replace(".", "")
    assert abis == {want_abi}, (
        f"add_python={pyver} 但 sage wheel 是 {abis.pop()} —— pip 会判 not supported,"
        f"镜像构建直接失败。换 Python 版本必须连着换 wheel(两个 ABI 都挂在同一个 Release 下)")

    assert node_sync.IMAGE_PYTHON_VERSION == pyver, (
        f"node_sync.IMAGE_PYTHON_VERSION={node_sync.IMAGE_PYTHON_VERSION} "
        f"与镜像实际的 {pyver} 不一致 —— 构建失败提示会报错版本号,把人往错方向带")


def _basicsr_shim_cmd() -> str:
    """从 modal_image.py 源码里抽出 shim 的 shell 文本。

    用 ast 而不是 import —— modal_image 顶层 `import modal`,而 CI 与宿主机都不保证装了。
    """
    import ast

    src = (ROOT / "modal_app" / "modal_image.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_BASICSR_SHIM_CMD":
            return node.value.value
    raise AssertionError("modal_image.py 里找不到 _BASICSR_SHIM_CMD")


def test_basicsr_shim_three_paths():
    """basicsr 的 functional_tensor shim —— **真跑那段 shell**,不是检查源码字符串。

    torchvision 0.17 删了 transforms.functional_tensor,而 basicsr 1.4.2 的
    data/degradations.py 还从那里取 rgb_to_grayscale。函数本体挪到了公开的
    transforms.functional,签名与数值都没变(2026-09-02 实测最大绝对误差 0.0)。

    三条路径都必须走对,尤其第一条:
      ① basicsr 没装 → 跳过并 exit 0。**这是最重要的一条** —— basicsr 是用户依赖,
         绝大多数用户根本没装;这里要是跟 sage 那层一样 fail-closed,等于让他们
         全都部署不了。
      ② 有旧写法 → 改写、校验、exit 0
      ③ 再跑一次 → 幂等,不重复改也不报错

    ⚠ 第一版在 ② 上炸过:残留检查 `grep -q functional_tensor` 匹配到了 sed 自己
    追加的那句注释,把成功判成失败。所以这条测试必须真执行,源码字符串检查看不出来。
    """
    import ast
    import os
    import shutil
    import subprocess
    import tempfile

    cmd = _basicsr_shim_cmd()

    # ⚠ 必须是单行。Modal 把每条 run_commands 原样变成 Dockerfile 的一行 RUN,
    # 多行 shell 会让 Dockerfile 解析器报 "expected any_breakable" —— 镜像当场构建不出来,
    # 而这段 shell 在 bash 里单独跑是完全正常的,所以只测行为发现不了。
    assert "\n" not in cmd.strip(), "shim 必须写成单行,否则 Dockerfile 解析失败"

    # shim 里写的是 `python`,而不少环境只有 `python3` —— 造一个给它
    binhome = tempfile.mkdtemp(prefix="mb-shim-bin-")
    real_py = shutil.which("python") or shutil.which("python3")
    assert real_py, "环境里既没有 python 也没有 python3"
    os.symlink(real_py, os.path.join(binhome, "python"))
    env = dict(os.environ, PATH=binhome + os.pathsep + os.environ.get("PATH", ""))

    def run(path):
        e = dict(env, MB_BASICSR_DEGRADATIONS=path)
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=e)
        return r.returncode, (r.stdout + r.stderr)

    work = tempfile.mkdtemp(prefix="mb-shim-")

    rc, out = run(os.path.join(work, "does-not-exist.py"))
    assert rc == 0, f"basicsr 没装时不该失败(会让绝大多数用户部署不了): {out}"
    assert "跳过" in out, out

    f = os.path.join(work, "degradations.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("import math\n"
                 "from torchvision.transforms.functional_tensor import rgb_to_grayscale\n"
                 "\n"
                 "def noop():\n"
                 "    return math.pi\n")
    rc, out = run(f)
    assert rc == 0, f"有旧写法时改写失败: {out}"
    assert "applied" in out, out

    txt = open(f, encoding="utf-8").read()
    assert "from torchvision.transforms.functional import rgb_to_grayscale" in txt
    assert "functional_tensor" not in txt, "改完仍有残留(含补丁自己写下的注释)"
    ast.parse(txt)  # 改完必须仍是合法 Python

    rc, out = run(f)
    assert rc == 0, f"幂等路径失败: {out}"
    assert "applied" not in out, "已修过的文件不该再改一次"


def test_no_copy_points_at_removed_ui():
    """面向用户的文案不许指向已经不存在的按钮 —— JS、Python 字符串、文档三处都要查。

    0.8.15 加过「同步本机私有节点」按钮,并在多处引导用户去点它;0.8.19 把它合并进
    「推送到云端」后,那些话就成了指向空气的指路牌 —— 比没有引导更糟,用户会在界面上
    翻找一个不存在的东西。

    ⚠ 这条测试第一版只搜了 JS,于是漏掉 node_sync.py 的构建失败提示和 SETUP.md 的
    配置说明(codex review 抓到)。现在三处都查:
      - JS:直接搜;
      - Python:只看**字符串字面量**,跳过注释与 docstring(那里讲历史是应该的);
      - Markdown:搜正文。
    """
    import ast

    gone_names = ("同步本机私有节点", "Sync local private nodes")
    offenders = []

    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")
    for g in gone_names:
        if g in js:
            offenders.append(f"web/modal_bridge.js: {g}")

    out = subprocess.run(["git", "ls-files", "*.py", "*.md"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    for rel in [x for x in out.stdout.splitlines() if x.strip()]:
        if rel.startswith("tests/"):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8")
        if not any(g in text for g in gone_names):
            continue
        if rel.endswith(".md"):
            offenders += [f"{rel}: {g}" for g in gone_names if g in text]
            continue
        # .py：只查真正会显示给用户的字符串字面量，注释和 docstring 里讲历史是应该的
        try:
            tree = ast.parse(text)
        except Exception:
            continue
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs
                    and any(g in node.value for g in gone_names)):
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, "这些地方仍指向已移除的按钮:\n  " + "\n  ".join(offenders)

    # 主按钮与状态文案口径一致（都说"推送"，不再说"部署"）
    for key in ('"dlg.btn.deploy"', '"dep.ok"', '"dep.ok.toast"', '"dep.running"'):
        i = js.index(key)
        line = js[i:js.index("\n", i)]
        assert "推送" in line, f"{key} 的中文文案没跟主按钮统一口径: {line[:80]}"


def test_push_fails_closed_on_node_upload_failure():
    """推送时私有节点上传失败必须**中止并报错**,不能继续用旧依赖构建还报成功。

    codex review 抓到:部署前那段同步一次性丢掉了三处失败信号 ——
    plan_local_uploads 的 failed 没看、upload_local_nodes 的返回值直接丢弃、
    异常只打一句 warning 就往下走,最终仍 rc=0、前端显示"已推送到云端",
    而云端跑的可能还是旧代码旧依赖。

    /sync_local_nodes 一直是"任意节点失败即失败",统一入口后更该一致。
    这正是本插件反复强调要避免的那类"静默成功"。
    """
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    i = src.index("# 3.0) 先把**本机**的私有节点推上 Volume")
    seg = src[i:i + 4200]

    # ⚠ 不能只查"字符串出现过" —— 把 `if _pfail:` 改成 `if False:` 也照样能过。
    # 要查它真的被用作分支条件，并且分支里会抛。
    assert '_pfail = _plan.get("failed")' in seg, "打包阶段的 failed 没有被取出"
    assert "if _pfail:" in seg, "打包失败没有被用作分支条件"
    assert '_ufail = (_ures or {}).get("failed")' in seg, "上传返回值的 failed 没有被取出"
    assert "if _ufail:" in seg, "上传失败没有被用作分支条件"
    # 两个分支里各要有一个 raise
    assert "raise RuntimeError" in seg[seg.index("if _pfail:"):seg.index("_todo = ")], \
        "打包失败分支没有抛错"
    assert "raise RuntimeError" in seg[seg.index("if _ufail:"):], "上传失败分支没有抛错"
    assert "__DEPLOY_DONE__ rc=1" in seg, "异常分支没有以失败码结束"
    # 并发保护：与 /sync_local_nodes 争同一把锁
    assert "_UPLOAD_LOCK" in seg, "部署路径的上传绕过了 _UPLOAD_LOCK，会与其它上传竞态"


def test_submit_asks_before_pushing_private_nodes():
    """提交前推送私有节点必须先问一句,取消则中止提交(不给"跳过继续")。

    改动前:检测到私有节点有改动就**全自动**推送,而这一步可能顺带重建镜像
    (依赖变了的话几分钟)。于是点一次「运行」会突然卡住,用户不知道在等什么。
    「运行」就该是运行 —— 把重建镜像这种事悄悄塞进去,整个流程就不可预测了。

    ⚠ 取消时**不能**提供"跳过继续跑"的选项:私有节点不同步就提交,云端用的是旧代码,
    结果和用户改的不一样、且不会有任何报错。这种静默偏差比明确失败难查得多
    (本仓库为"残包静默跑旧码"专门修过一轮)。对照:镜像节点那条路径给了跳过选项,
    因为缺节点会**明确报错**,风险性质不同。
    """
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")

    i = js.index("if (local_pack.length) {")
    seg = js[i:i + 3000]

    # ⚠ local_pack 只表示"走 Volume 通道",不等于有改动。必须先问后端真实 digest 差异,
    # 否则只要工作流含自写节点就每次都弹确认(codex review 抓到)。
    diff_at = seg.find("/modal_bridge/local_nodes_diff")
    assert diff_at > 0, "弹确认前没有先比对真实差异,会变成每次运行都打扰用户"
    # ⚠ 这里不能用子串包含,**两个方向的边界都要**(2026-09-02 当场踩到):
    #    · 只写 `_changed.length === 0` → 放宽成 `... || true` 仍含原串,恒真;
    #    · 补上闭括号写成 `if (_changed.length === 0) {` → 仍然恒真!因为
    #      `} else if (_changed.length === 0) {` **原样包含它**。我按这个写法改完之后
    #      把跳过条件的语义改掉了,这条测试一声没吭。
    #    所以改成:把所有相关的 guard 行整行取出来,和预期逐字比对。任何一条被改写、
    #    被 else-if 包一层、或加一个 `|| ...`,都会立刻不等。
    guards = [ln.strip() for ln in seg.splitlines() if "_changed.length === 0" in ln]
    assert guards == [
        "if (_changed.length === 0 && !_reqsPending) {",
        "} else if (_changed.length === 0) {",
    ], (
        "私有节点的跳过判定变了。它必须是两段:内容一致**且**不欠镜像重建才静默跳过;"
        f"内容一致但欠重建要单独确认(那条路径以前完全不问,点一次运行会毫无征兆卡几分钟)。"
        f"实际读到:{guards}")

    confirm_at = seg.find('confirm(t("node.local_push_confirm"')
    assert diff_at < confirm_at, "差异预检必须在确认之前"
    sync_at = seg.find("await syncLocalNodes(")
    assert confirm_at > 0, "推送私有节点前没有确认"
    assert sync_at > 0, "找不到同步调用,测试锚点需更新"
    assert confirm_at < sync_at, "确认必须在推送之前"
    assert "return false" in seg[confirm_at:sync_at], "取消后没有中止提交"
    # 不许出现"跳过继续"那种二次确认
    assert "node.skip_confirm" not in seg[confirm_at:sync_at], \
        "私有节点不同步就跑会让云端静默跑旧代码，不该给跳过选项"

    # 文案要讲清楚代价：可能重建镜像、以及不推的后果
    blk = js[js.index('"node.local_push_confirm"'):][:1200]
    assert "重建镜像" in blk, "确认文案没说明可能要重建镜像"
    assert "旧代码" in blk, "确认文案没说明不推的后果"


def test_partial_output_loss_is_a_failure():
    """多输出任务只取回一部分,必须失败,不能报 completed。

    codex review 抓到:旧写法只在"一个产物都没成功"时抛,于是「2 个输出只拿到 1 个」
    会照常返回,worker 无条件写 completed,连 errors 都丢掉 —— 用户拿到残缺结果却显示
    全成功,而且照常计费。这和之前修过的「执行错误被吞成 completed」是同一类漏洞。

    判据用 len(images) < len(refs) 而不是 not images:materialize 对每个 ref 要么产出
    一条记录、要么记一条 error 后 continue,所以数量对不上就是真丢了东西。
    """
    src = (ROOT / "modal_app" / "_comfy_ws.py").read_text(encoding="utf-8")
    # ⚠ 这里不能用 `"len(images) < len(refs)" in seg` —— 那是**恒真**的:把条件放宽成
    #    `len(images) < len(refs) - 1`(静默容忍丢一个产物,恰是本条要防的)子串原样还在,
    #    测试照样绿(2026-09-02 变异实测)。用 ast 比对**整个条件表达式**才挡得住。
    import ast as _ast

    guard = [
        n for n in _ast.walk(_ast.parse(src))
        if isinstance(n, _ast.If) and _ast.unparse(n.test) == "len(images) < len(refs)"
    ]
    assert guard, "没有按数量严格比对(条件必须恰好是 len(images) < len(refs)),部分丢失会被当成功"
    assert any(isinstance(x, _ast.Raise) for x in _ast.walk(guard[0])), "数量对不上没有抛错"

    # worker 侧即使成功也要留痕，便于事后追溯
    # ⚠ 锚点要精确:文件里有多处 "status": "completed"（aigc-r2 交付分支也有一处），
    # 用裸字符串会命中错的那个。
    worker = (ROOT / "modal_app" / "modal_app.py").read_text(encoding="utf-8")
    j = worker.index('done = {**job_state.get(job_id, {}), "status": "completed"')
    assert "warnings" in worker[j:j + 700], "worker 把 result['errors'] 丢掉了,出问题无从追溯"


if __name__ == "__main__":
    import inspect
    import tempfile

    class _MonkeyPatch:
        """pytest monkeypatch 的最小替身:只实现本文件用到的 setattr + 自动回滚。

        ⚠ 2026-09-02 之前这个自跑入口**放在文件中段**,`sys.exit()` 一执行,后面
        1272 行里的 36 条测试连定义都没跑到 —— 而 CI 用的正是这条路径,于是
        路径囚笼、admin 守卫、Registry 合规、sage 补丁那些测试**从来没在 CI 里跑过**,
        CI 却一直是绿的。挪到文件末尾后才暴露出这三条依赖 monkeypatch 的测试跑不了。
        """

        def __init__(self):
            self._undo = []

        _MISSING = object()

        def setattr(self, target, name, value):
            self._undo.append((target, name, getattr(target, name, self._MISSING)))
            setattr(target, name, value)

        def delattr(self, target, name, raising=True):
            # 模拟"属性不存在"(如 Windows + Python ≤3.12 没有 os.fchmod)。
            # 本文件有测试靠它,而第一版替身只有 setattr —— 于是那条测试在 pytest 下绿、
            # 自跑路径下红:又一个"两条跑法看到的测试集不一样"。
            if not hasattr(target, name):
                if raising:
                    raise AttributeError(name)
                return
            self._undo.append((target, name, getattr(target, name)))
            delattr(target, name)

        def undo(self):
            for target, name, old in reversed(self._undo):
                if old is self._MISSING:
                    if hasattr(target, name):
                        delattr(target, name)
                else:
                    setattr(target, name, old)
            self._undo.clear()

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        mp = _MonkeyPatch()
        try:
            # 极简 fixture:按签名注入 tmp_path / monkeypatch(与 pytest 语义对齐)
            params = inspect.signature(fn).parameters
            kw = {"monkeypatch": mp} if "monkeypatch" in params else {}
            if "tmp_path" in params:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td), **kw)
            else:
                fn(**kw)
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}  — {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}  — ERROR {type(e).__name__}: {e}")
            failed += 1
        finally:
            mp.undo()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
