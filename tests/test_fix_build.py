"""
fix1005 构建与部署链的回归测试(2026-10-05 深度 review:B2–B11、C13、C14、B-P3a/b/c、CLI 三条)。

约束(同 test_core / test_routes):
  - 凡是会写清单文件 / config / cli.json 的函数,一律 monkeypatch 到 tmp_path;
  - 不联网、不部署、不碰真实 Modal(Modal SDK 用桩);
  - 不用 exec/eval(Registry 扫描会标记),需要单独执行源码片段时写临时模块再 importlib 加载。
"""
import ast
import http.server
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import health_client  # noqa: E402
import local_nodes  # noqa: E402
import modal_volume  # noqa: E402
import model_deps  # noqa: E402
import node_sync  # noqa: E402

IMAGE_SRC = ROOT / "modal_app" / "modal_image.py"


# ============================================================================
# 公共桩
# ============================================================================
@pytest.fixture
def baked(tmp_path, monkeypatch):
    """清单文件 / 私有依赖文件都落在 tmp_path。"""
    monkeypatch.setattr(node_sync, "DATA_FILE", tmp_path / "_custom_nodes_data.py")
    monkeypatch.setattr(node_sync, "LOCAL_REQS_FILE", tmp_path / "_local_nodes_data.py")
    return tmp_path


def _cnr_node(root: Path, folder: str, name: str, version: str, tracking: bool = True,
              comment: str = "") -> Path:
    d = root / "custom_nodes" / folder
    d.mkdir(parents=True)
    (d / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"{comment}\n\n'
        f'[project.urls]\nRepository = "https://github.com/someone/{name}"\n', encoding="utf-8")
    if tracking:
        (d / ".tracking").write_text("__init__.py\n", encoding="utf-8")
    (d / "__init__.py").write_text("", encoding="utf-8")
    return d


def _image_helpers():
    """把 modal_image 里与 modal SDK 无关的纯函数 / 常量抠出来,写成临时模块再加载。
    (modal_image 顶层 import modal,CI 不保证装了;也不 exec —— 见模块 docstring。)"""
    src = IMAGE_SRC.read_text(encoding="utf-8")
    want_fn = {"_cnr_ref", "_split_creds", "_clone_one", "_install_reqs_one"}
    want_var = {"_CNR_ID_RE", "_CNR_VER_RE", "_CNR_URL_RE", "_CNR_FETCH_PY",
                "_TORCH_CONSTRAINTS", "_TORCH_PIN_CMD"}
    parts = ["import re as _re", "from shlex import quote as _q",
             "from urllib.parse import unquote as _unquote"]
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name in want_fn:
            parts.append(ast.get_source_segment(src, n))
        elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") in want_var for t in n.targets):
            parts.append(ast.get_source_segment(src, n))
    fd, tmp = tempfile.mkstemp(suffix=".py", prefix="image_helpers_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n\n".join(parts) + "\n")
        spec = importlib.util.spec_from_file_location("_image_helpers", tmp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.unlink(tmp)
    return mod


def _no_proxy_env(**extra):
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.update(extra)
    return env


# ============================================================================
# B2 云端独有、url 被脱敏的私有节点:只借本机地址,commit 用云端的
# ============================================================================
def _redacted_setup(monkeypatch, local_url, local_commit="LOCAL_OLD"):
    node_sync.write_baked_nodes([{"name": "pub", "url": "https://github.com/x/pub", "commit": "p1"}])
    manifest = [{"name": "pub", "url": "https://github.com/x/pub", "commit": "p1"},
                {"name": "priv", "url": "https://github.com/me/priv", "commit": "CLOUD_NEW",
                 "url_redacted": True}]
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", lambda cfg, timeout=20: (["priv", "pub"], manifest))
    monkeypatch.setattr(node_sync, "folder_git_info", lambda name: {
        "has_git": True, "url": local_url, "commit": local_commit, "pushed": True, "dirty": False}
        if name == "priv" else {"has_git": True, "url": "https://github.com/x/pub", "commit": "p1"})


def test_redacted_cloud_only_node_keeps_cloud_commit_and_is_drift(baked, monkeypatch):
    _redacted_setup(monkeypatch, "https://ghp_tok@github.com/me/priv")
    rec = node_sync.reconcile_baked_with_cloud({})
    out = {n["name"]: n for n in node_sync.read_baked_nodes()}
    assert out["priv"]["commit"] == "CLOUD_NEW", "并回时用了本机的 commit —— 云端被静默换版本"
    assert out["priv"]["url"] == "https://ghp_tok@github.com/me/priv", "要借本机带凭据的地址才克隆得到"
    assert rec.added == ["priv"]
    assert any(d.startswith("priv:") for d in rec.drift), rec.drift
    assert "ghp_tok" not in node_sync.drift_message(rec)
    stop = node_sync.auto_deploy_blocker(rec)
    assert stop and "priv" in stop and "\n" not in stop

    # 本机与云端同一个 commit:照常并回,不算差异
    _redacted_setup(monkeypatch, "https://ghp_tok@github.com/me/priv", local_commit="CLOUD_NEW")
    rec = node_sync.reconcile_baked_with_cloud({})
    assert rec.added == ["priv"] and rec.drift == [], rec


@pytest.mark.parametrize("local_url", [
    "https://github.com/me/priv",            # 本机地址不带凭据:换上去也克隆不到
    "https://ghp_tok@github.com/me/other",   # 带凭据但是另一个仓库
    "",
])
def test_redacted_cloud_only_node_without_usable_local_source_blocks(baked, monkeypatch, local_url):
    _redacted_setup(monkeypatch, local_url)
    with pytest.raises(node_sync.DeployBlocked) as e:
        node_sync.reconcile_baked_with_cloud({})
    assert "priv" in str(e.value)
    assert [n["name"] for n in node_sync.read_baked_nodes()] == ["pub"], "中止时不能写清单"


def test_check_nodes_completion_uses_cloud_commit_for_redacted_entries(monkeypatch):
    """/check_nodes 的补全(routes 调 complete_baked_entries):本机清单是旧 commit 时不能拿它去部署。"""
    manifest = [{"name": "priv", "url": "https://github.com/me/priv", "commit": "CLOUD_NEW", "url_redacted": True}]
    local = {"priv": {"name": "priv", "url": "https://tok@github.com/me/priv", "commit": "LOCAL_STALE"}}
    monkeypatch.setattr(node_sync, "folder_git_info", lambda f: {"has_git": False})
    entries, drift = node_sync.complete_baked_entries_ex(["priv"], local, manifest)
    assert entries == [{"name": "priv", "url": "https://tok@github.com/me/priv", "commit": "CLOUD_NEW"}]
    assert drift and "LOCAL_STA" in drift[0] and "tok" not in drift[0]
    assert node_sync.complete_baked_entries(["priv"], local, manifest) == entries
    # 本机清单的地址指向别的仓库 → 补不出来源
    local = {"priv": {"name": "priv", "url": "https://tok@github.com/me/fork", "commit": "x"}}
    assert node_sync.complete_baked_entries(["priv"], local, manifest)[0]["url"] == ""


# ============================================================================
# B4 Comfy Registry(CNR)节点:当公共节点,钉到本机的 Registry 版本
# ============================================================================
def test_cnr_node_is_detected_and_pinned_to_the_registry_version(baked, tmp_path, monkeypatch):
    root = tmp_path / "ComfyUI"
    # ComfyUI-GGUF 的真实写法:版本号后面跟着行尾注释
    _cnr_node(root, "ComfyUI-GGUF", "ComfyUI-GGUF", "1.1.10",
              comment="  # 2.0.0 = GitHub main, 1.X.X = ComfyUI Registry")
    monkeypatch.setattr(node_sync, "_comfyui_root", lambda: root)
    monkeypatch.setattr(node_sync, "_is_own_git_repo", lambda p: False)
    g = node_sync.folder_git_info("ComfyUI-GGUF")
    assert (g["cnr_id"], g["version"]) == ("comfyui-gguf", "1.1.10"), g
    assert g["url"] == "https://api.comfy.org/nodes/comfyui-gguf/versions/1.1.10"
    assert g["has_git"] and not g["dirty"]

    monkeypatch.setattr(node_sync, "analyze_workflow",
                        lambda p: {"builtin": [], "by_folder": {"ComfyUI-GGUF": ["UnetLoaderGGUF"]},
                                   "unresolved": []})
    first = node_sync.plan_node_sync({}, baked=[])
    assert [a["folder"] for a in first["add"]] == ["ComfyUI-GGUF"]
    assert first["add"][0]["version"] == "1.1.10"
    assert first["new_baked"][0]["cnr_id"] == "comfyui-gguf"

    # 第二次:同一版本 → 跑镜像版,不能落到 no_git 走私有节点通道(也就不会触发依赖重建)
    second = node_sync.plan_node_sync({}, baked=first["new_baked"])
    assert second["local_pack"] == [] and not second["needs_deploy"], second
    assert second["expect_baked"] == ["ComfyUI-GGUF"]

    # routes 的 /sync_nodes 只保留 name/url/commit、云端 manifest 也只回这三个:版本在 url 里,照样认得
    stripped = [{k: e[k] for k in ("name", "url", "commit")} for e in first["new_baked"]]
    third = node_sync.plan_node_sync({}, baked=stripped)
    assert third["local_pack"] == [] and not third["needs_deploy"]
    node_sync.write_baked_nodes(stripped)
    assert node_sync.read_baked_nodes()[0]["version"] == "1.1.10", "写清单时要从 url 补回 cnr 字段"

    # 本机换了版本 → 按 Registry 版本更新(update),不是 local_pack
    older = [{**first["new_baked"][0], "url": node_sync.cnr_url("comfyui-gguf", "1.1.9"),
              "version": "1.1.9"}]
    upd = node_sync.plan_node_sync({}, baked=older)
    assert [u["folder"] for u in upd["update"]] == ["ComfyUI-GGUF"] and upd["needs_deploy"]
    assert upd["update"][0]["old_version"] == "1.1.9" and upd["update"][0]["version"] == "1.1.10"
    assert upd["new_baked"][0]["url"].endswith("/versions/1.1.10")

    # 以前按 pyproject 仓库地址加进去的(GitHub HEAD)→ 也换成本机的 Registry 版本
    legacy = [{"name": "ComfyUI-GGUF", "url": "https://github.com/city96/ComfyUI-GGUF", "commit": ""}]
    upd = node_sync.plan_node_sync({}, baked=legacy)
    assert upd["update"] and upd["new_baked"][0]["version"] == "1.1.10" and not upd["local_pack"]


def test_cnr_detection_requires_tracking_and_survives_missing_tomllib(tmp_path, monkeypatch):
    root = tmp_path / "ComfyUI"
    _cnr_node(root, "no-tracking", "no-tracking", "1.0.0", tracking=False)
    _cnr_node(root, "kj", "comfyui-kjnodes", "1.4.0")
    monkeypatch.setattr(node_sync, "_comfyui_root", lambda: root)
    monkeypatch.setattr(node_sync, "_is_own_git_repo", lambda p: False)
    g = node_sync.folder_git_info("no-tracking")
    assert "cnr_id" not in g and g["url"] == "https://github.com/someone/no-tracking", \
        "没有 .tracking 不是 Registry 安装,仍按 pyproject 仓库地址兜底"
    monkeypatch.setitem(sys.modules, "tomllib", None)        # 模拟 Python 3.10:import tomllib 失败
    assert node_sync._read_cnr_info(root / "custom_nodes" / "kj") == ("comfyui-kjnodes", "1.4.0")


def test_cnr_drift_compares_registry_versions(baked, monkeypatch):
    url = node_sync.cnr_url
    node_sync.write_baked_nodes([{"name": "gguf", "url": url("comfyui-gguf", "1.1.9"), "commit": ""},
                                 {"name": "kj", "url": url("comfyui-kjnodes", "1.4.0"), "commit": ""}])
    # 云端 manifest 只有 name/url/commit(当前云端代码就是这样报的)
    manifest = [{"name": "gguf", "url": url("comfyui-gguf", "1.1.10"), "commit": ""},
                {"name": "kj", "url": url("comfyui-kjnodes", "1.4.0"), "commit": ""}]
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", lambda cfg, timeout=20: (["gguf", "kj"], manifest))
    installed = {"gguf": "1.1.10", "kj": "1.4.0"}
    monkeypatch.setattr(node_sync, "folder_git_info", lambda n: {
        "has_git": True, "url": url(n if n != "gguf" else "comfyui-gguf", installed[n]), "commit": "",
        "cnr_id": "comfyui-gguf" if n == "gguf" else "comfyui-kjnodes", "version": installed[n]})
    rec = node_sync.reconcile_baked_with_cloud({})
    assert rec.drift == [] and [c.split(":")[0] for c in rec.corrected] == ["gguf"], rec
    assert "Registry 1.1.10" in rec.corrected[0]
    assert {n["name"]: n["version"] for n in node_sync.read_baked_nodes()} == {"gguf": "1.1.10", "kj": "1.4.0"}

    # 本机装的就是清单里那版(与云端不同)→ 判断不了谁对,列为差异
    node_sync.write_baked_nodes([{"name": "gguf", "url": url("comfyui-gguf", "1.1.9"), "commit": ""},
                                 {"name": "kj", "url": url("comfyui-kjnodes", "1.4.0"), "commit": ""}])
    installed["gguf"] = "1.1.9"
    rec = node_sync.reconcile_baked_with_cloud({})
    assert [d.split(":")[0] for d in rec.drift] == ["gguf"] and "Registry 1.1.9" in rec.drift[0], rec
    assert node_sync.auto_deploy_blocker(rec)
    # 一边 Registry、一边 git:不是同一来源
    assert not node_sync._same_source({"url": url("comfyui-gguf", "1.1.10")},
                                      {"url": "https://github.com/city96/ComfyUI-GGUF", "commit": ""})


def test_image_downloads_cnr_nodes_instead_of_cloning():
    h = _image_helpers()
    cmd, env = h._clone_one({"name": "ComfyUI-GGUF", "commit": "",
                             "url": "https://api.comfy.org/nodes/comfyui-gguf/versions/1.1.10"})
    assert env == {} and "git clone" not in cmd
    argv = shlex.split(cmd)
    assert argv[:2] == ["python", "-c"] and argv[3:] == ["comfyui-gguf", "1.1.10",
                                                          "/comfyui/custom_nodes/ComfyUI-GGUF"]
    assert "unzip" not in cmd, "镜像里不保证有 unzip"
    # 只有 cnr_id / version 字段(规格里的形态)也认
    cmd2, _ = h._clone_one({"name": "kj", "url": "https://github.com/kijai/ComfyUI-KJNodes",
                            "cnr_id": "comfyui-kjnodes", "version": "1.4.0"})
    assert shlex.split(cmd2)[3:5] == ["comfyui-kjnodes", "1.4.0"]
    # 普通 git 条目的命令一个字节都不能变(变了就让所有用户的节点层缓存失效)
    cmd3, env3 = h._clone_one({"name": "pub", "url": "https://github.com/x/pub", "commit": "abc"})
    assert cmd3 == ("git clone https://github.com/x/pub /comfyui/custom_nodes/pub && "
                    "cd /comfyui/custom_nodes/pub && git checkout abc") and env3 == {}


def test_cnr_fetch_script_downloads_and_extracts_without_unzip(tmp_path):
    """真跑镜像里那段下载脚本(只把 api.comfy.org 换成本地服务):JSON → downloadUrl → zip → 解压。"""
    h = _image_helpers()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__init__.py", "X = 1\n")
        z.writestr("sub/nodes.py", "Y = 2\n")
        z.writestr("../escape.txt", "nope")           # extractall 必须把它关在目标目录里
    blob = buf.getvalue()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/nodes/comfyui-x/versions/1.2.3":
                body = json.dumps({"downloadUrl": f"http://127.0.0.1:{srv.server_port}/node.zip"}).encode()
            elif self.path == "/node.zip":
                body = blob
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        script = h._CNR_FETCH_PY.replace("https://api.comfy.org", f"http://127.0.0.1:{srv.server_port}")
        assert script != h._CNR_FETCH_PY
        dest = tmp_path / "out" / "node"
        r = subprocess.run([sys.executable, "-c", script, "comfyui-x", "1.2.3", str(dest)],
                           capture_output=True, text=True, timeout=60, env=_no_proxy_env())
        assert r.returncode == 0, r.stderr
        assert (dest / "__init__.py").read_text() == "X = 1\n" and (dest / "sub" / "nodes.py").is_file()
        assert not (tmp_path / "out" / "escape.txt").exists()
        r = subprocess.run([sys.executable, "-c", script, "comfyui-x", "9.9.9", str(tmp_path / "n2")],
                           capture_output=True, text=True, timeout=60, env=_no_proxy_env())
        assert r.returncode != 0, "版本不存在必须让构建失败,而不是装个空目录"
    finally:
        srv.shutdown()


# ============================================================================
# B5 本机清单有、云端没有的节点
# ============================================================================
def test_local_only_node_is_listed_and_blocks_auto_deploy(baked, monkeypatch):
    node_sync.write_baked_nodes([{"name": "a", "url": "https://github.com/x/a", "commit": "a1"},
                                 {"name": "pruned-elsewhere", "url": "https://github.com/x/p", "commit": "p0"}])
    manifest = [{"name": "a", "url": "https://github.com/x/a", "commit": "a1"}]
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", lambda cfg, timeout=20: (["a"], manifest))
    monkeypatch.setattr(node_sync, "folder_git_info",
                        lambda n: {"has_git": True, "url": f"https://github.com/x/{n}", "commit": "a1"})
    rec = node_sync.reconcile_baked_with_cloud({})
    assert [d.split(":")[0] for d in rec.drift] == ["pruned-elsewhere"], rec
    assert "pruned-elsewhere" in node_sync.drift_message(rec)
    stop = node_sync.auto_deploy_blocker(rec)
    assert "pruned-elsewhere" in stop and "\n" not in stop

    # 全新部署 / 读不到云端:不能把「云端什么都没有」当成本机独有
    def not_deployed(cfg, timeout=20):
        raise health_client.HealthUnavailable("not_deployed", "404")
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", not_deployed)
    assert node_sync.reconcile_baked_with_cloud({}).drift == []

    def unreachable(cfg, timeout=20):
        raise health_client.HealthUnavailable("unreachable", "timeout")
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", unreachable)
    rec = node_sync.reconcile_baked_with_cloud({})
    assert rec.drift == [] and rec.unchecked


# ============================================================================
# B8 ssh 写法:能转 https 的转,转不了的在入口拒绝
# ============================================================================
@pytest.mark.parametrize("raw,want", [
    ("git@github.com:o/r.git", "https://github.com/o/r.git"),
    ("ssh://git@github.com/o/r.git", "https://github.com/o/r.git"),
    ("ssh://git@github.com:22/o/r", "https://github.com/o/r"),
    ("git@gitlab.com:g/sub/r.git", "https://gitlab.com/g/sub/r.git"),
    ("git@bitbucket.org:t/r.git", "https://bitbucket.org/t/r.git"),
    ("git://github.com/o/r.git", "https://github.com/o/r.git"),
    ("https://github.com/o/r", "https://github.com/o/r"),
])
def test_ssh_urls_on_public_hosts_become_https(raw, want):
    assert node_sync._normalize_git_url(raw) == want
    assert node_sync.clone_url_problem(want) == ""


@pytest.mark.parametrize("raw", [
    "ssh://git@github.com:2222/o/r.git",
    "git@git.company.internal:team/r.git",
    "ssh://git@git.company.internal/team/r.git",
    "/Users/me/repos/r",
    "file:///Users/me/repos/r",
])
def test_unclonable_urls_are_rejected_with_a_reason(raw):
    u = node_sync._normalize_git_url(raw)
    assert node_sync.clone_url_problem(u), f"{raw} 云端克隆不了,入口必须拒绝"


def test_unclonable_remote_goes_to_volume_and_never_into_the_image(baked, tmp_path, monkeypatch):
    root = tmp_path / "ComfyUI"
    (root / "custom_nodes" / "corp-node").mkdir(parents=True)
    monkeypatch.setattr(node_sync, "_comfyui_root", lambda: root)
    monkeypatch.setattr(node_sync, "_is_own_git_repo", lambda p: True)
    monkeypatch.setattr(node_sync, "_git", lambda args, cwd: {
        "config": "git@git.corp.internal:team/corp-node.git", "rev-parse": "c" * 40}.get(args[0]))
    monkeypatch.setattr(node_sync, "commit_on_remote", lambda p, c: True)
    monkeypatch.setattr(node_sync, "worktree_dirty", lambda p: False)
    g = node_sync.folder_git_info("corp-node")
    assert g["has_git"] is False and "ssh" in g["url_problem"]
    monkeypatch.setattr(node_sync, "analyze_workflow",
                        lambda p: {"builtin": [], "by_folder": {"corp-node": ["Corp"]}, "unresolved": []})
    plan = node_sync.plan_node_sync({}, baked=[])
    assert plan["add"] == [] and plan["local_pack"][0]["reason"] == "unclonable"
    assert plan["local_pack"][0]["detail"]

    # 出口:能转的转,转不了的丢弃并出声(进镜像只会让整个 RUN 失败)
    node_sync.write_baked_nodes([{"name": "gh", "url": "ssh://git@github.com/o/gh.git", "commit": "1"},
                                 {"name": "corp", "url": "git@git.corp.internal:t/c.git", "commit": "2"}])
    assert node_sync.read_baked_nodes() == [{"name": "gh", "url": "https://github.com/o/gh.git", "commit": "1"}]


# ============================================================================
# B11 带凭据的克隆地址不进 RUN 行
# ============================================================================
def test_credentialed_clone_keeps_token_out_of_the_run_line():
    h = _image_helpers()
    cmd, env = h._clone_one({"name": "priv", "url": "https://me:ghp_S3CRET%40x@github.com/me/priv",
                             "commit": "abc"}, 3)
    assert "ghp_S3CRET" not in cmd and "me:" not in cmd
    assert "https://github.com/me/priv" in cmd and "git checkout abc" in cmd
    assert env == {"MB_GIT_USER_3": "me", "MB_GIT_PASS_3": "ghp_S3CRET@x"}, "百分号要按 git 的规则解码"
    # 只有 token 的写法(https://TOKEN@host/…)
    _, env2 = h._clone_one({"name": "p2", "url": "https://ghp_ONLY@github.com/me/p2"}, 4)
    assert env2 == {"MB_GIT_USER_4": "ghp_ONLY", "MB_GIT_PASS_4": ""}

    # 真跑一遍 git 的凭据协议:RUN 行里的 helper 在 sh 下能把 env 里的值原样交给 git
    if not shutil.which("git"):
        pytest.skip("没有 git")
    argv = shlex.split(cmd.split(" && ")[0])
    cfgs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert cfgs[0] == "credential.helper=", "先清掉镜像里可能配置的其它 helper"
    gitc = ["git"] + [x for c in cfgs for x in ("-c", c)]
    r = subprocess.run(gitc + ["credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, **env, "GIT_TERMINAL_PROMPT": "0"})
    assert r.returncode == 0, r.stderr
    assert "username=me\n" in r.stdout and "password=ghp_S3CRET@x\n" in r.stdout


# ============================================================================
# B-P3a / B-P3c:坏条目不让模块 import 失败;节点依赖不许换掉镜像里的 torch
# ============================================================================
def test_install_reqs_tolerates_entries_without_name():
    h = _image_helpers()
    assert h._install_reqs_one({"url": "https://github.com/x/y"}) == ""
    assert h._install_reqs_one("garbage") == ""
    assert "pip install -r /comfyui/custom_nodes/ok/requirements.txt" in h._install_reqs_one({"name": "ok"})


def test_torch_constraints_are_generated_from_what_is_installed(tmp_path):
    """真跑约束文件那段 python:用假的 dist-info 冒充镜像里装的 torch 三件套。"""
    h = _image_helpers()
    site = tmp_path / "site"
    for name, ver in (("torch", "2.13.0+cu130"), ("torchvision", "0.28.0+cu130"), ("torchaudio", "2.13.0+cu130")):
        d = site / f"{name}-{ver}.dist-info"
        d.mkdir(parents=True)
        (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n", encoding="utf-8")
    seg = h._TORCH_PIN_CMD.split(" > ")[0].removeprefix("mkdir -p /opt/bridge && ")
    argv = shlex.split(seg)
    assert argv[:2] == ["python", "-c"]
    r = subprocess.run([sys.executable, "-c", argv[2]], capture_output=True, text=True, timeout=30,
                       env={**os.environ, "PYTHONPATH": str(site)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["torch==2.13.0+cu130", "torchvision==0.28.0+cu130", "torchaudio==2.13.0+cu130"]
    assert h._TORCH_PIN_CMD.endswith(f"export PIP_CONSTRAINT={h._TORCH_CONSTRAINTS}")


def test_private_node_requirements_layer_is_constrained_too():
    """不装 modal SDK 也要钉住:私有节点依赖那层同样带 -c 约束文件(用 extra_options,不用 env=)。"""
    tree = ast.parse(IMAGE_SRC.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "pip_install"
             and any(isinstance(a, ast.Starred) and getattr(a.value, "id", "") == "LOCAL_NODE_REQS" for a in n.args)]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "env" not in kw and "extra_options" in kw
    assert "_TORCH_CONSTRAINTS" in ast.unparse(kw["extra_options"]) and "-c" in ast.unparse(kw["extra_options"])


def _dump_image(tmp_path, nodes, local_reqs=()):
    """用本机 modal SDK 构建 Image 对象(不联网、不部署),取每一层的 Dockerfile 命令与挂的 secret 数。"""
    if importlib.util.find_spec("modal") is None:
        pytest.skip("本机没装 modal SDK")
    app = tmp_path / "app"
    app.mkdir(parents=True)
    for f in (ROOT / "modal_app").glob("*.py"):
        if not f.name.startswith("_custom_nodes_data") and not f.name.startswith("_local_nodes_data"):
            shutil.copy(f, app / f.name)
    (app / "_custom_nodes_data.py").write_text(f"CUSTOM_NODES = {nodes!r}\n", encoding="utf-8")
    (app / "_local_nodes_data.py").write_text(f"LOCAL_NODE_REQS = {list(local_reqs)!r}\n", encoding="utf-8")
    harness = app / "_dump.py"
    harness.write_text(textwrap.dedent("""
        import json, sys
        try:
            import modal._image as mi
        except ImportError:
            import modal.image as mi
        layers = []
        orig = mi._Image._from_args
        def rec(*a, **k):
            fn = k.get("dockerfile_function")
            if fn is not None:
                layers.append({"cmds": list(fn("2025.06").commands), "secrets": len(k.get("secrets") or [])})
            return orig(*a, **k)
        mi._Image._from_args = staticmethod(rec)
        sys.path.insert(0, sys.argv[1])
        import modal_image
        print(json.dumps({"layers": layers, "env_keys": sorted(modal_image._CLONE_ENV)}))
    """), encoding="utf-8")
    env = _no_proxy_env(MODAL_BRIDGE_COMFYUI_TAG="v0.37.2", MODAL_BRIDGE_VERSION="0.0.0",
                        MODAL_BRIDGE_APP_NAME="comfyui-bridge")
    r = subprocess.run([sys.executable, str(harness), str(app)], capture_output=True, text=True,
                       timeout=120, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stdout


def test_image_dockerfile_end_to_end(tmp_path):
    """整份镜像定义:坏条目不让 import 失败,CNR 走下载,凭据只进 secret,约束文件先于节点依赖。"""
    nodes = [{"name": "pub", "url": "https://github.com/x/pub", "commit": "abc123"},
             {"name": "ComfyUI-GGUF", "url": "https://api.comfy.org/nodes/comfyui-gguf/versions/1.1.10",
              "commit": "", "cnr_id": "comfyui-gguf", "version": "1.1.10"},
             {"name": "priv", "url": "https://u:ghp_SECRET123@github.com/me/priv", "commit": "d"},
             {"url": "https://github.com/x/noname"}]
    out, raw = _dump_image(tmp_path, nodes, local_reqs=["foo==1"])
    assert "ghp_SECRET123" not in raw
    runs = [(c, layer["secrets"]) for layer in out["layers"] for c in layer["cmds"] if c.startswith("RUN ")]
    clone = [(c, s) for c, s in runs if "mkdir -p /comfyui/custom_nodes" in c]
    assert len(clone) == 1 and clone[0][1] == 1, "凭据要经 secret 注入到克隆那一层"
    assert "comfyui-gguf 1.1.10 /comfyui/custom_nodes/ComfyUI-GGUF" in clone[0][0]
    assert out["env_keys"] == ["MB_GIT_PASS_2", "MB_GIT_USER_2"]
    reqs = [c for c, _ in runs if "requirements.txt; else" in c]
    assert len(reqs) == 1 and reqs[0].index("export PIP_CONSTRAINT=") < reqs[0].index("pip install -r")
    assert "noname" not in reqs[0]
    assert any("foo==1" in c and "-c /opt/bridge/torch-constraints.txt" in c for c, _ in runs)
    # 没有带凭据的条目:克隆层不挂 secret(否则公共节点用户的缓存可能每次都失效)
    out2, _ = _dump_image(tmp_path / "b", [{"name": "pub", "url": "https://github.com/x/pub", "commit": "a"}])
    clone2 = [layer for layer in out2["layers"] if any("mkdir -p /comfyui/custom_nodes" in c for c in layer["cmds"])]
    assert clone2[0]["secrets"] == 0


# ============================================================================
# B9 / C15 通用兜底保留相对路径
# ============================================================================
def test_generic_model_scan_keeps_subdirectories():
    prompt = {"1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux/flux1-dev-Q8_0.gguf"}},
              "2": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flat.gguf"}}}
    assert model_deps.extract_generic_filenames(prompt) == {"flux/flux1-dev-Q8_0.gguf", "flat.gguf"}


# ============================================================================
# B10 子目录不按文件名兜底;存在性检查连同大小一起比
# ============================================================================
def test_find_local_model_never_substitutes_a_same_named_file_from_another_subdir(tmp_path):
    root = tmp_path / "unet"
    (root / "other").mkdir(parents=True)
    (root / "other" / "x.gguf").write_bytes(b"wrong")
    assert modal_volume.find_local_model("unet", "flux/x.gguf", [root]) is None, \
        "别的子目录里的同名文件被当成了请求的那个"
    (root / "flux").mkdir()
    (root / "flux" / "x.gguf").write_bytes(b"right")
    assert modal_volume.find_local_model("unet", "flux/x.gguf", [root]) == root / "flux" / "x.gguf"
    assert modal_volume.find_local_model("unet", "flux\\x.gguf", [root]) == root / "flux" / "x.gguf"
    # 不带子目录的名字照旧递归兜底(Desktop 有人按子目录归类)
    assert modal_volume.find_local_model("unet", "x.gguf", [root]) is not None


class _Entry:
    def __init__(self, path, size, kind="FILE"):
        self.path, self.size, self.type = path, size, types.SimpleNamespace(name=kind)


def test_existence_check_compares_sizes(tmp_path, monkeypatch):
    local = tmp_path / "x.safetensors"
    local.write_bytes(b"0123456789")
    vol = types.SimpleNamespace(
        reload=lambda: None,
        listdir=lambda p, recursive=False: [_Entry("models/checkpoints/SDXL", 0, "DIRECTORY"),
                                            _Entry("models/checkpoints/SDXL/x.safetensors", 7),
                                            _Entry("models/checkpoints/ok.safetensors", 10)]
        if p == "models/checkpoints" else [])
    monkeypatch.setattr(modal_volume, "get_volume", lambda cfg: vol)
    have = modal_volume.volume_files_by_type({}, ["checkpoints"])
    assert have["checkpoints"] == {"SDXL/x.safetensors": 7, "ok.safetensors": 10}
    monkeypatch.setattr(modal_volume, "file_in_progress", lambda p, **k: False)
    r = modal_volume.check_models({}, [{"type": "checkpoints", "filename": "SDXL/x.safetensors"},
                                       {"type": "checkpoints", "filename": "ok.safetensors"}],
                                  lambda t, fn: local)
    assert [m["filename"] for m in r["missing_local"]] == ["SDXL/x.safetensors"], "大小不对的要重传"
    assert r["missing_local"][0]["replace"] is True
    assert [m["filename"] for m in r["present"]] == ["ok.safetensors"]

    # 上传:大小不对的那个用 force=True 覆盖,大小相同的跳过
    batches = []

    class Batch:
        def __init__(self, force):
            self.force, self.puts = force, []
            batches.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put_file(self, src, dst):
            self.puts.append(dst)
    vol.batch_upload = lambda force=False: Batch(force)
    res = modal_volume.upload_models({}, [
        {"type": "checkpoints", "filename": "SDXL/x.safetensors", "local_path": str(local)},
        {"type": "checkpoints", "filename": "ok.safetensors", "local_path": str(local)}])
    assert [(b.force, b.puts) for b in batches] == [(True, ["models/checkpoints/SDXL/x.safetensors"])]
    assert [s["reason"] for s in res["skipped"]] == ["already in volume"]


# ============================================================================
# C13 Volume 私有节点名单:读失败抛 VolumeUnavailable,不缓存
# ============================================================================
def test_list_volume_local_nodes_distinguishes_unreadable_from_empty(monkeypatch):
    local_nodes.invalidate_list_cache()
    state = {"fail": True}

    class NotFoundError(Exception):
        pass

    def listdir(path):
        if state["fail"] == "missing":
            raise NotFoundError("no such dir")
        if state["fail"]:
            raise RuntimeError("grpc deadline exceeded")
        return [_Entry("_local_nodes/a.zip", 1), _Entry("_local_nodes/a.digest", 1)]
    vol = types.SimpleNamespace(reload=lambda: None, listdir=listdir)
    monkeypatch.setattr(modal_volume, "get_volume", lambda cfg: vol)
    with pytest.raises(local_nodes.VolumeUnavailable):
        local_nodes.list_volume_local_nodes({})
    state["fail"] = False
    assert local_nodes.list_volume_local_nodes({}) == ["a"], "失败结果被缓存了"
    state["fail"] = "missing"
    assert local_nodes.list_volume_local_nodes({}, max_age=0) == [], "_local_nodes 目录不存在 = 确认没有"

    def no_volume(cfg):
        raise RuntimeError("modal token invalid")
    monkeypatch.setattr(modal_volume, "get_volume", no_volume)
    with pytest.raises(local_nodes.VolumeUnavailable):
        local_nodes.list_volume_local_nodes({}, max_age=0)
    local_nodes.invalidate_list_cache()


def test_volume_node_requirements_for_deploys(monkeypatch):
    monkeypatch.setattr(local_nodes, "list_volume_local_nodes", lambda cfg, max_age=60: ["a", "b", "legacy"])
    files = {"_local_nodes/a.requirements.json": b'["numpy==1", "x"]',
             "_local_nodes/b.requirements.json": b'["x", "y"]'}

    def read(path, buf):
        if path not in files:
            raise FileNotFoundError(path)
        buf.write(files[path])
    vol = types.SimpleNamespace(reload=lambda: None, read_file_into_fileobj=read)
    monkeypatch.setattr(modal_volume, "get_volume", lambda cfg: vol)
    assert local_nodes.volume_node_requirements({}, ["old==1", "x"]) == ["numpy==1", "x", "y", "old==1"]

    def flaky(path, buf):
        raise RuntimeError("connection reset")
    vol.read_file_into_fileobj = flaky
    with pytest.raises(local_nodes.VolumeUnavailable):
        local_nodes.volume_node_requirements({}, [])
    monkeypatch.setattr(local_nodes, "list_volume_local_nodes", lambda cfg, max_age=60: [])
    assert local_nodes.volume_node_requirements({}, ["stale==1"]) == [], "Volume 上确认没有私有节点 → 空"


# ============================================================================
# C14 Volume 下载:在 .part 上校验大小,通过才 rename
# ============================================================================
def _dl_volume(monkeypatch, payload: bytes, sdk_says=None, listed=None):
    def read(path, f):
        f.write(payload)
        return len(payload) if sdk_says is None else sdk_says
    vol = types.SimpleNamespace(reload=lambda: None, read_file_into_fileobj=read,
                                listdir=lambda p: [_Entry(p, listed)] if listed is not None else [])
    monkeypatch.setattr(modal_volume, "get_volume", lambda cfg: vol)


def test_download_volume_file_checks_size_before_rename(tmp_path, monkeypatch):
    dst = tmp_path / "out" / "v.mp4"
    _dl_volume(monkeypatch, b"x" * 5)
    with pytest.raises(modal_volume.DownloadIncomplete):
        modal_volume.download_volume_file({}, "_outputs/j/v.mp4", str(dst), expected_size=8)
    assert not dst.exists() and not dst.with_name("v.mp4.part").exists()
    assert modal_volume.download_volume_file({}, "_outputs/j/v.mp4", str(dst), expected_size=5) == 5
    assert dst.read_bytes() == b"x" * 5

    dst2 = tmp_path / "out" / "w.mp4"
    _dl_volume(monkeypatch, b"x" * 5, listed=9)          # 没给 expected_size:比 Volume 列出的大小
    with pytest.raises(modal_volume.DownloadIncomplete):
        modal_volume.download_volume_file({}, "_outputs/j/w.mp4", str(dst2))
    assert not dst2.exists()
    _dl_volume(monkeypatch, b"x" * 5, sdk_says=6, listed=0)   # 列不到:比 SDK 报的读取字节数
    with pytest.raises(modal_volume.DownloadIncomplete):
        modal_volume.download_volume_file({}, "_outputs/j/w.mp4", str(dst2))
    _dl_volume(monkeypatch, b"x" * 5, listed=5)
    assert modal_volume.download_volume_file({}, "_outputs/j/w.mp4", str(dst2)) == 5


# ============================================================================
# B3 / C16 Secret:合并语义;先落盘再写 Secret;Secret 在所有中止检查之后
# ============================================================================
def test_secret_upsert_cmd_only_touches_given_keys():
    cmd = node_sync.secret_upsert_cmd({"modal_app_name": "my-app"}, "hf_SECRETVALUE123", "",
                                      "bk-0123456789abcdef", "", "", "",
                                      clear=("aigc_base_url", "aigc_bypass_secret"))
    assert cmd[1].endswith("node_sync.py") and cmd[2:4] == ["secret-upsert", "my-app-secrets"]
    assert "--force" not in cmd
    assert sorted(a.split("=")[0] for a in cmd[4:] if not a.startswith("--")) == \
        ["BRIDGE_API_KEY", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"]
    assert "--clear=AIGC_STUDIO_BASE_URL" in cmd and "--clear=AIGC_STUDIO_BYPASS_SECRET" in cmd
    assert not any("CIVITAI" in a or "COMFY_API" in a for a in cmd), "没给的键不能出现(出现就会被覆盖)"
    shown = node_sync.redact_cmd(cmd)
    assert "hf_SECRETVALUE123" not in shown and "bk-0123456789abcdef" not in shown
    with pytest.raises(ValueError):
        node_sync.secret_upsert_cmd({}, bridge_key="bk-x", clear=("bridge_key",))


class _FakeSecretModal:
    def __init__(self, existing=True, has_update=True):
        self.calls = []
        outer = self

        class NotFoundError(Exception):
            pass

        class Ref:
            def __init__(self, name):
                self.name = name

            def update(self, d):
                if not existing:
                    raise NotFoundError(self.name)
                outer.calls.append(("update", self.name, dict(d)))

        class Secret:
            objects = types.SimpleNamespace(
                create=lambda name, d: outer.calls.append(("create", name, dict(d))))
            update = Ref.update             # 真 SDK 里 update 是 Secret 的方法(≥ 1.3.5)

            @staticmethod
            def from_name(name):
                return Ref(name)
        if has_update:
            self.Secret = Secret
        else:
            self.Secret = type("OldSecret", (), {"from_name": staticmethod(lambda n: Ref(n))})
        self.exception = types.SimpleNamespace(NotFoundError=NotFoundError)


def test_secret_upsert_main_merges_creates_and_falls_back(monkeypatch):
    fake = _FakeSecretModal()
    assert node_sync._secret_upsert_main(["s", "BRIDGE_API_KEY=bk-1", "--clear=AIGC_STUDIO_BASE_URL"],
                                         fake) == 0
    assert fake.calls == [("update", "s", {"BRIDGE_API_KEY": "bk-1", "AIGC_STUDIO_BASE_URL": ""})]
    fake = _FakeSecretModal(existing=False)
    assert node_sync._secret_upsert_main(["s", "BRIDGE_API_KEY=bk-1", "--clear=X"], fake) == 0
    assert fake.calls == [("create", "s", {"BRIDGE_API_KEY": "bk-1"})]
    # SDK < 1.3.5 没有 Secret.update:退回旧的整份重建(并明说)
    seen = []
    monkeypatch.setattr(node_sync.subprocess, "call", lambda argv: seen.append(argv) or 0)
    assert node_sync._secret_upsert_main(["s", "BRIDGE_API_KEY=bk-1"], _FakeSecretModal(has_update=False)) == 0
    assert seen and seen[0][-3:] == ["--force", "s", "BRIDGE_API_KEY=bk-1"]


def test_secret_upsert_runs_as_a_script(tmp_path):
    """部署流程以子进程跑 `python node_sync.py secret-upsert …`:脚本形态下的 import 也要通。"""
    pkg = tmp_path / "fake" / "modal"
    pkg.mkdir(parents=True)
    log = tmp_path / "calls.json"
    (pkg / "__init__.py").write_text(textwrap.dedent(f"""
        import json
        from . import exception
        class _Ref:
            def __init__(self, name): self.name = name
            def update(self, d):
                open({str(log)!r}, "w").write(json.dumps(["update", self.name, d]))
        class Secret:
            objects = None
            update = _Ref.update
            @staticmethod
            def from_name(name): return _Ref(name)
    """), encoding="utf-8")
    (pkg / "exception.py").write_text("class NotFoundError(Exception):\n    pass\n", encoding="utf-8")
    cmd = node_sync.secret_upsert_cmd({"modal_app_name": "a"}, bridge_key="bk-zz")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
                       env={**os.environ, "PYTHONPATH": str(tmp_path / "fake")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(log.read_text()) == ["update", "a-secrets", {"BRIDGE_API_KEY": "bk-zz"}]
    assert "bk-zz" not in r.stdout


# ── deploy.py ────────────────────────────────────────────────────────────
@pytest.fixture
def deploy_env(baked, tmp_path, monkeypatch):
    """deploy.main() 的全套桩:config 到 tmp、modal 是假的、子进程只记录不执行。"""
    import config as cfg_mod
    import deploy
    monkeypatch.setattr(cfg_mod, "_config_path", lambda: tmp_path / "cfg" / "config.json")
    monkeypatch.setitem(sys.modules, "modal", types.SimpleNamespace(__version__="0.0-test"))
    monkeypatch.setattr(node_sync, "detect_local_comfyui_version", lambda: "")
    monkeypatch.setattr(node_sync, "list_comfyui_tags", lambda *a, **k: [])
    events = {"cmds": [], "recorded": [], "rc": {"deploy": 0, "secret": 0},
              "reqs": ["dep==1"], "fetch": None}

    def fake_run(cmd, **kw):
        kind = "secret" if "secret-upsert" in cmd else "deploy" if "deploy" in cmd else "other"
        # 写 Secret 的那一刻,config 里必须已经有这把 key(契约 C16)
        events["cmds"].append((kind, list(cmd), dict(cfg_mod.load_config())))
        return types.SimpleNamespace(returncode=events["rc"].get(kind, 0))
    monkeypatch.setattr(deploy.subprocess, "run", fake_run)

    def fetch(cfg, timeout=20):
        events["fetch_cfg"] = dict(cfg)
        if events["fetch"]:
            return events["fetch"]
        raise health_client.HealthUnavailable("not_deployed", "404")
    monkeypatch.setattr(node_sync, "fetch_cloud_nodes", fetch)

    def reqs(cfg, legacy=None):
        if isinstance(events["reqs"], Exception):
            raise events["reqs"]
        return list(events["reqs"])
    monkeypatch.setattr(local_nodes, "volume_node_requirements", reqs)
    monkeypatch.setattr(modal_volume, "record_deployed_reqs", lambda cfg, r: events["recorded"].append(list(r)))

    def go(*argv):
        monkeypatch.setattr(sys, "argv", ["deploy.py", "--workspace", "ws", "--token-id", "ak-x",
                                          "--token-secret", "as-x", *argv])
        try:
            deploy.main()
            return 0
        except SystemExit as e:
            return e.code
    events["go"] = go
    events["cfg_mod"] = cfg_mod
    return events


def test_deploy_py_order_and_records(deploy_env):
    ev, cfg_mod = deploy_env, deploy_env["cfg_mod"]
    assert ev["go"]("--comfyui-tag", "v0.37.2", "--hf-token", "hf_TOKEN_xyz") == 0
    kinds = [k for k, _, _ in ev["cmds"]]
    assert kinds == ["secret", "deploy"]
    _, secret_cmd, cfg_at_secret = ev["cmds"][0]
    key = [a for a in secret_cmd if a.startswith("BRIDGE_API_KEY=")][0].split("=", 1)[1]
    assert cfg_at_secret["bridge_api_key"] == key, "新 key 必须先写进 config 再写 Secret"
    assert cfg_at_secret["hf_token"] == "hf_TOKEN_xyz"
    assert "--force" not in secret_cmd
    final = cfg_mod.load_config()
    assert final["comfyui_tag"] == "v0.37.2" and final["modal_endpoint_base"] == "https://ws--comfyui-bridge"
    assert final["local_node_reqs_deployed_hash"] == node_sync.local_node_reqs_hash(["dep==1"])
    assert ev["recorded"] == [["dep==1"]], "部署成功后要记下镜像里的私有节点依赖(<app>-meta)"
    assert node_sync.read_local_node_reqs() == ["dep==1"], "部署前要按 Volume 刷新依赖文件"


def test_deploy_py_failure_keeps_key_and_token_in_config(deploy_env):
    ev, cfg_mod = deploy_env, deploy_env["cfg_mod"]
    ev["rc"]["deploy"] = 1
    assert ev["go"]("--comfyui-tag", "v0.37.2", "--hf-token", "hf_TOKEN_xyz") == 1
    cfg = cfg_mod.load_config()
    assert cfg["hf_token"] == "hf_TOKEN_xyz", "部署失败时 token 不能只留在 Secret 里"
    assert cfg["bridge_api_key"].startswith("bk-")
    assert ev["recorded"] == []


def test_deploy_py_aborts_before_touching_the_secret(deploy_env, monkeypatch):
    ev = deploy_env

    def blocked(cfg):
        raise node_sync.DeployBlocked("会删掉云端节点")
    monkeypatch.setattr(node_sync, "reconcile_baked_with_cloud", blocked)
    assert ev["go"]("--comfyui-tag", "v0.37.2") not in (0, None)
    assert ev["cmds"] == [], "DeployBlocked 时不能先写了 Secret"


def test_deploy_py_aborts_when_volume_is_unreadable(deploy_env):
    ev = deploy_env
    ev["reqs"] = local_nodes.VolumeUnavailable("grpc down")
    code = ev["go"]("--comfyui-tag", "v0.37.2")
    assert code not in (0, None) and "Volume" in str(code)
    assert ev["cmds"] == [] and ev["recorded"] == []


def test_deploy_py_respects_app_name_and_pin_and_refuses_unknown_tag(deploy_env):
    ev, cfg_mod = deploy_env, deploy_env["cfg_mod"]
    cfg_mod.save_config({**cfg_mod.DEFAULT_CONFIG, "modal_app_name": "my-bridge",
                         "comfyui_tag": "v0.34.6", "comfyui_tag_pin": "v0.37.2", "bridge_api_key": "bk-old"})
    assert ev["go"]() == 0
    final = cfg_mod.load_config()
    assert final["modal_app_name"] == "my-bridge", "自定义 app 名被改回 comfyui-bridge(会部署出第二个 app)"
    assert final["modal_endpoint_base"] == "https://ws--my-bridge"
    assert final["comfyui_tag"] == "v0.37.2", "没认 comfyui_tag_pin"
    assert ev["cmds"][0][1][3] == "my-bridge-secrets"
    assert final["bridge_api_key"] == "bk-old"

    # 什么版本线索都没有:拒绝,而不是落到 v0.22.0
    cfg_mod.save_config({**cfg_mod.DEFAULT_CONFIG, "bridge_api_key": "bk-old"})
    ev["cmds"].clear()
    code = ev["go"]()
    assert code not in (0, None) and "--comfyui-tag" in str(code) and ev["cmds"] == []


# ── bridge_cli deploy ────────────────────────────────────────────────────
@pytest.fixture
def cli_env(baked, tmp_path, monkeypatch):
    import bridge_cli
    import config as cfg_mod
    plugin_cfg = tmp_path / "user" / "default" / "modal_bridge" / "config.json"
    monkeypatch.setattr(cfg_mod, "_config_path", lambda: plugin_cfg)
    monkeypatch.setattr(bridge_cli, "CLI_CFG", tmp_path / "cli.json")
    ev = {"urls": [], "runs": [], "recorded": [], "reqs": ["d==1"], "plugin_cfg": plugin_cfg,
          "deploy_out": "https://ws--comfyui-bridge-run.modal.run\n", "deploy_rc": 0}

    def fetch(cfg, timeout=20):
        ev["urls"].append(health_client.url(cfg) if cfg.get("modal_endpoint_base") else "")
        raise health_client.HealthUnavailable("not_deployed", "404")
    monkeypatch.setattr(health_client, "fetch", fetch)
    monkeypatch.setattr(local_nodes, "volume_node_requirements", lambda cfg, legacy=None: list(ev["reqs"]))
    monkeypatch.setattr(modal_volume, "record_deployed_reqs", lambda cfg, r: ev["recorded"].append(list(r)))

    def run(cmd, **kw):
        cli = json.loads((tmp_path / "cli.json").read_text()) if (tmp_path / "cli.json").exists() else {}
        ev["runs"].append((list(cmd), kw.get("env"), cli))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)

    class P:
        def __init__(self, cmd, **kw):
            ev["deploy_env"] = kw.get("env")
            self.stdout = io.StringIO(ev["deploy_out"])

        def wait(self):
            return ev["deploy_rc"]
    monkeypatch.setattr(subprocess, "Popen", P)

    def go(**over):
        args = types.SimpleNamespace(app_name="comfyui-bridge", comfyui_tag=None, gpu=None, cheap_gpu=None,
                                     top_gpu=None, timeout_s=None, sage=None)
        for k, v in over.items():
            setattr(args, k, v)
        try:
            bridge_cli.cmd_deploy(args)
            return 0
        except SystemExit as e:
            return e.code
    ev["go"] = go
    ev["cfg_mod"] = cfg_mod
    ev["cli"] = lambda: json.loads((tmp_path / "cli.json").read_text())
    ev["cli_path"] = tmp_path / "cli.json"
    return ev


def test_cli_deploy_fresh_self_hoster(cli_env):
    ev = cli_env
    code = ev["go"]()
    assert code not in (0, None) and "--comfyui-tag" in str(code), "tag 空时必须拒绝,不能落到 v0.22.0"
    assert ev["runs"] == []
    assert not ev["plugin_cfg"].exists(), "在 ComfyUI 之外跑不该凭空建一个插件 config"

    assert ev["go"](comfyui_tag="v0.37.2") == 0
    assert ev["urls"] == [""], "占位 endpoint(YOUR_WORKSPACE)不能拿去查 /health —— 当未知 = 全新部署"
    assert ev["deploy_env"]["MODAL_BRIDGE_COMFYUI_TAG"] == "v0.37.2"
    secret_cmd, _, cli_at_secret = ev["runs"][0]
    key = [a for a in secret_cmd if a.startswith("BRIDGE_API_KEY=")][0].split("=", 1)[1]
    assert cli_at_secret["keys"]["comfyui-bridge"] == key, "新 key 必须先写进 cli.json 再写 Secret"
    cli = ev["cli"]()
    assert cli["endpoint"] == "https://ws--comfyui-bridge" and cli["key"] == key and cli["comfyui_tag"] == "v0.37.2"
    assert ev["recorded"] == [["d==1"]] and node_sync.read_local_node_reqs() == ["d==1"]
    assert not ev["plugin_cfg"].exists()

    # 第二次:沿用上次 CLI 部署的 tag、endpoint、key
    ev["urls"].clear()
    ev["runs"].clear()
    assert ev["go"]() == 0
    assert ev["urls"] == ["https://ws--comfyui-bridge-health.modal.run"]
    assert ev["deploy_env"]["MODAL_BRIDGE_COMFYUI_TAG"] == "v0.37.2"
    assert [a for a in ev["runs"][0][0] if a.startswith("BRIDGE_API_KEY=")] == [f"BRIDGE_API_KEY={key}"]


def test_cli_deploy_on_a_plugin_machine_respects_pin_and_never_creates_config(cli_env):
    ev, cfg_mod = cli_env, cli_env["cfg_mod"]
    cfg_mod.save_config({**cfg_mod.DEFAULT_CONFIG, "modal_endpoint_base": "https://ws--comfyui-bridge",
                         "comfyui_tag": "v0.34.6", "comfyui_tag_pin": "v0.37.2", "bridge_api_key": "bk-plugin"})
    assert ev["go"]() == 0
    assert ev["deploy_env"]["MODAL_BRIDGE_COMFYUI_TAG"] == "v0.37.2", "没认 comfyui_tag_pin"
    assert "BRIDGE_API_KEY=bk-plugin" in ev["runs"][0][0], "同一个 app 时插件的 key 优先"

    # 插件装了、ComfyUI 跑过(默认 config,占位 endpoint)、但没用 GUI 部署过
    cfg_mod.save_config(dict(cfg_mod.DEFAULT_CONFIG))
    ev["urls"].clear()
    ev["cli_path"].unlink()
    assert ev["go"](comfyui_tag="v0.37.2") == 0
    assert ev["urls"] == [""], "占位 endpoint 必须当未知"


def test_cli_deploy_secret_failure_and_blocked_leave_no_half_state(cli_env, monkeypatch):
    ev = cli_env

    def blocked(cfg):
        raise node_sync.DeployBlocked("x")
    monkeypatch.setattr(node_sync, "reconcile_baked_with_cloud", blocked)
    assert ev["go"](comfyui_tag="v0.37.2") not in (0, None)
    assert ev["runs"] == [], "DeployBlocked 时不能先写 Secret"
    assert ev["cli"]()["keys"]["comfyui-bridge"].startswith("bk-"), "生成的 key 已落盘,下次复用同一把"


# ============================================================================
# CLI:cancel 退出码、打印不带 base64、非 BridgeError 不甩 traceback、fetch 说清原因
# ============================================================================
def _cli_with(monkeypatch, **methods):
    import bridge_cli
    monkeypatch.setattr(bridge_cli, "_client", lambda a: types.SimpleNamespace(**methods))
    return bridge_cli


@pytest.mark.parametrize("resp,code", [
    ({"id": "j", "status": "cancelled", "was_running": True}, 0),
    ({"id": "j", "status": "failed", "error": "OOM", "cancel_noop": True}, 0),
    ({"id": "j", "status": "not_found", "error": "job not found"}, 3),
    ({"id": "j", "status": "running", "error": "cancel failed: rpc"}, 4),
])
def test_cli_cancel_exit_codes(monkeypatch, resp, code):
    cli = _cli_with(monkeypatch, cancel=lambda jid: resp)
    try:
        cli.cmd_cancel(types.SimpleNamespace(job_id="j"))
        got = 0
    except SystemExit as e:
        got = e.code
    assert got == code


@pytest.mark.parametrize("exc", [lambda: __import__("bridge_client").BridgeError("HTTP 502"),
                                 lambda: __import__("urllib.error").error.URLError("dns"),
                                 lambda: ValueError("bad json")])
def test_cli_cancel_unknown_outcome(monkeypatch, exc):
    def boom(jid):
        raise exc()
    cli = _cli_with(monkeypatch, cancel=boom)
    with pytest.raises(SystemExit) as e:
        cli.cmd_cancel(types.SimpleNamespace(job_id="j"))
    assert e.value.code == 5


def test_cli_status_strips_base64(monkeypatch, capsys):
    import base64
    blob = base64.b64encode(b"x" * 3000).decode()
    state = {"id": "j", "status": "completed", "images": [
        {"filename": "a.png", "data_base64": blob},
        {"filename": "v.mp4", "volume_path": "_outputs/j/v.mp4", "size_bytes": 9000}],
        "data_base64": blob}
    cli = _cli_with(monkeypatch, status=lambda jid: state, cancel=lambda jid: {**state, "cancel_noop": True})
    cli.cmd_status(types.SimpleNamespace(job_id="j"))
    out = capsys.readouterr().out
    assert blob not in out and "data_base64" not in out
    summ = json.loads(out)["outputs_summary"]
    assert summ["files"] == 3 and summ["bytes"] == 3000 + 9000 + 3000
    assert state["images"][0]["data_base64"] == blob, "不能改动调用方手里的原状态"
    cli.cmd_cancel(types.SimpleNamespace(job_id="j"))
    assert blob not in capsys.readouterr().out


def test_cli_fetch_explains_failed_and_not_found(monkeypatch):
    for st, word in (({"status": "failed", "error": "CUDA OOM"}, "CUDA OOM"),
                     ({"status": "not_found"}, "查无此任务"),
                     ({"status": "running"}, "还没完成")):
        cli = _cli_with(monkeypatch, status=lambda jid, st=st: st)
        with pytest.raises(SystemExit) as e:
            cli.cmd_fetch(types.SimpleNamespace(job_id="j", out="."))
        assert word in str(e.value.code)


def test_cli_main_turns_non_bridge_errors_into_one_line(monkeypatch, tmp_path):
    import http.client
    import bridge_cli

    for err in (http.client.IncompleteRead(b"x", 10), OSError("disk full"), ValueError("bad")):
        def boom(args, err=err):
            raise err
        monkeypatch.setattr(bridge_cli, "cmd_status", boom)
        monkeypatch.setattr(sys, "argv", ["bridge_cli.py", "status", "j"])
        with pytest.raises(SystemExit) as e:
            bridge_cli.main()
        assert str(e.value.code).startswith("✗ status 失败:"), e.value.code


# ============================================================================
# 插件 config 损坏 / 读不了:不能当成「没有插件配置」,否则新 key 覆盖 Secret、所有调用方 401
# (2026-10-05 routes 一路协调,R1)
# ============================================================================
def test_cli_deploy_refuses_a_corrupt_plugin_config(cli_env):
    ev = cli_env
    ev["plugin_cfg"].parent.mkdir(parents=True)
    ev["plugin_cfg"].write_text('{"bridge_api_key": "bk-plugin", ', encoding="utf-8")   # 写了一半
    code = ev["go"](comfyui_tag="v0.37.2")
    assert code not in (0, None) and "✗" in str(code) and "401" in str(code)
    assert ev["runs"] == [], "坏 config 被当成没有 → 生成新 key 写进 Secret"
    assert not ev["cli_path"].exists(), "连新 key 都不该生成"


def test_cli_deploy_lets_config_errors_through(cli_env, monkeypatch):
    """只有 ImportError(插件不在旁边)和文件不存在才算「没有插件配置」,别的异常照常抛。"""
    ev, cfg_mod = cli_env, cli_env["cfg_mod"]

    def broken():
        raise ValueError("config path unresolvable")
    monkeypatch.setattr(cfg_mod, "_config_path", broken)
    monkeypatch.setattr(cfg_mod, "load_config", broken)
    import bridge_cli
    args = types.SimpleNamespace(app_name="comfyui-bridge", comfyui_tag="v0.37.2", gpu=None, cheap_gpu=None,
                                 top_gpu=None, timeout_s=None, sage=None)
    with pytest.raises(ValueError):
        bridge_cli.cmd_deploy(args)
    assert ev["runs"] == []
    # 走 main() 时是一行 ✗,不是 traceback
    monkeypatch.setattr(sys, "argv", ["bridge_cli.py", "deploy", "--comfyui-tag", "v0.37.2"])
    with pytest.raises(SystemExit) as e:
        bridge_cli.main()
    assert str(e.value.code).startswith("✗ deploy 失败:ValueError")


def test_deploy_py_refuses_a_corrupt_or_unreadable_config(deploy_env, monkeypatch):
    ev, cfg_mod = deploy_env, deploy_env["cfg_mod"]
    p = cfg_mod._config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"bridge_api_key": "bk-plug', encoding="utf-8")
    code = ev["go"]("--comfyui-tag", "v0.37.2")
    assert code not in (0, None) and "已损坏" in str(code) and ev["cmds"] == []
    assert p.read_text(encoding="utf-8") == '{"bridge_api_key": "bk-plug', "坏 config 被默认值覆盖了"

    # 新版 config.load_config 对损坏文件抛错(ConfigCorrupt):同样停下、不吞
    p.unlink()

    def corrupt():
        raise ValueError("config.json corrupt")
    monkeypatch.setattr(cfg_mod, "load_config", corrupt)
    code = ev["go"]("--comfyui-tag", "v0.37.2")
    assert code not in (0, None) and "ValueError" in str(code) and ev["cmds"] == []
