"""Registry 命中对应路径的行为回归；只用临时目录和本地假服务器。"""
import importlib.util
import json
import os
import stat
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import bridge_cli
import config


@pytest.fixture(params=["plugin", "cli"])
def config_writer(request, monkeypatch, tmp_path):
    target = tmp_path / "config.json"
    if request.param == "plugin":
        monkeypatch.setattr(config, "_config_path", lambda: target)
        return config.save_config, target
    monkeypatch.setattr(bridge_cli, "CLI_CFG", target)
    return bridge_cli._save_cli_cfg, target


def test_config_ignores_preexisting_tmp_symlink(config_writer, tmp_path):
    save, target = config_writer
    victim = tmp_path / "unrelated.txt"
    victim.write_text("unchanged")
    legacy = target.with_name(target.name + ".tmp")
    legacy.symlink_to(victim)
    save({"key": "test-only"})
    assert victim.read_text() == "unchanged"
    assert not target.is_symlink()
    assert json.loads(target.read_text()) == {"key": "test-only"}
    assert legacy.is_symlink(), "不得操作不属于本次保存的旧临时文件"


def test_concurrent_config_writes_use_distinct_temps(config_writer, monkeypatch):
    save, target = config_writer
    gate = threading.Barrier(2, timeout=5)
    real_replace = os.replace
    sources = []

    def replace(src, dst):
        sources.append(str(src))
        gate.wait()
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, {"key": f"test-{i}"}) for i in range(2)]
        for f in futures:
            f.result()
    assert len(set(sources)) == 2
    assert json.loads(target.read_text())["key"] in ("test-0", "test-1")
    assert all(not Path(p).exists() for p in sources)


def test_private_creation_and_unrelated_old_temp(config_writer, monkeypatch):
    save, target = config_writer
    legacy = target.with_name(target.name + ".tmp")
    legacy.write_text("unrelated old temp")
    legacy.chmod(0o644)
    real_fdopen = os.fdopen
    modes = []

    def fdopen(fd, *args, **kwargs):
        # 写入第一字节前的实际权限，而非仅检查最终文件。
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", fdopen)
    save({"key": "test-only"})
    assert modes == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert legacy.read_text() == "unrelated old temp"
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o644


@pytest.mark.parametrize("failure", ["fdopen", "replace", "serialization"])
def test_private_write_failure_keeps_old_config_and_cleans_temp(config_writer, monkeypatch, failure):
    save, target = config_writer
    target.write_text('{"key":"old-test-only"}')
    before = set(target.parent.iterdir())
    opened = []

    def fail(*args, **kwargs):
        if failure == "fdopen":
            opened.append(args[0])
        raise OSError("injected failure")

    if failure != "serialization":
        monkeypatch.setattr(os, failure, fail)
    with pytest.raises((OSError, TypeError)):
        save({"key": object() if failure == "serialization" else "new-test-only"})
    assert json.loads(target.read_text()) == {"key": "old-test-only"}
    assert set(target.parent.iterdir()) == before
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def _load_mcp(monkeypatch, base):
    # 不 import 真实 MCP SDK，不读取真实 config 或环境凭据。
    class Server:
        def __init__(self, *_args):
            pass

        def tool(self):
            return lambda fn: fn

    for name in ("mcp", "mcp.server"):
        module = types.ModuleType(name)
        module.MCPServer = Server
        monkeypatch.setitem(sys.modules, name, module)
    for key, value in {
        "MODAL_BRIDGE_ENDPOINT": "", "MODAL_BRIDGE_LOCAL_CONFIG": "",
        "MODAL_BRIDGE_LOCAL_CAPABILITY": "test-local-cap", "MODAL_BRIDGE_URL": base,
    }.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("isolated_mcp_bridge", Path(__file__).parents[1] / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mcp_redirect_does_not_forward_capability(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/final":
                seen.append(self.headers.get("X-Modal-Bridge-Capability"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self.send_response(302)
                location = "/final" if self.path == "/same" else f"http://127.0.0.1:{other.server_port}/final"
                self.send_header("Location", location)
                self.end_headers()

        def log_message(self, *_args):
            pass

    first, other = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (first, other)]
    for thread in threads:
        thread.start()
    try:
        module = _load_mcp(monkeypatch, f"http://127.0.0.1:{first.server_port}")
        result = module._call("/cross", timeout=3)
        assert "error" in result
        assert seen == [], "跨源目标已收到管理 capability"
        assert module._call("/same", timeout=3) == {"ok": True}
        assert seen == ["test-local-cap"]
    finally:
        for server in (first, other):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
