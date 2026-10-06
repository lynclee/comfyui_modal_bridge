"""测试全局护栏:测试进程里的 modal SDK 永远连不到真实的 Modal。

2026-10-05 修复时踩过:给 bridge_cli 新加的「查 app 是否已部署」还没在测试里桩掉,全量一跑,两条现有测试就用
容器里的真实凭据向 Modal 控制面发了查询(只读,但读到了用户的生产 app)。新增代码漏桩是迟早的事,
所以在这里兜底:把控制面地址指向一个必然拒绝连接的本地端口,并换成假凭据 —— 漏桩的调用会立刻失败,
而不是悄悄出网。离线构建 Image 对象、dump Dockerfile 这类不联网的用法不受影响。
用 setdefault:CI 或个人想显式指向别处时可以覆盖。
"""
import os

os.environ.setdefault("MODAL_SERVER_URL", "http://127.0.0.1:9")
os.environ.setdefault("MODAL_TOKEN_ID", "ak-test-no-network")
os.environ.setdefault("MODAL_TOKEN_SECRET", "as-test-no-network")
os.environ.setdefault("MODAL_CONFIG_PATH", os.devnull)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_modal_control_plane(monkeypatch):
    """漏桩的 Modal 控制面调用**立即**失败,而且失败得出声。

    光靠上面的假地址不够:SDK 自带重试,漏桩的调用要 ~56s 才抛 ConnectionError;代码里又常把异常
    转成自己的错误(比如 bridge_cli 的 EndpointLookupFailed),「期望中止」的测试会在一分钟后照样通过,
    谁也不会发现(2026-10-05 第二轮复核)。这里在建客户端的那一步直接 pytest.fail:它是 BaseException,
    `except Exception` 吞不掉。需要真 SDK 行为的测试自己 monkeypatch 覆盖即可。
    ⚠ 在 aiohttp 路由里(asyncio.to_thread → handler)BaseException 会被吞成「连接断开」,而断连在很多测试里
      恰好是预期结果(按「结果未知」处理)—— 所以还要记下来,teardown 时再判一次(第四轮复核)。
    yield 记录列表:护栏自己的测试按名字取到它,确认触发后清空。"""
    hits = []
    try:
        import modal.client as _mc
    except Exception:
        yield hits
        return

    async def _refuse(*a, **k):
        hits.append("modal")
        pytest.fail("测试里出现了未桩的 Modal 控制面调用(会用真实凭据出网)—— 请 monkeypatch 掉它")
    monkeypatch.setattr(_mc._Client, "from_env", classmethod(_refuse), raising=False)
    yield hits
    assert not hits, "测试里出现了未桩的 Modal 控制面调用(被吞掉了,见上方 pytest.fail)—— 请 monkeypatch 掉它"


_REGISTRY_HOSTS = ("comfy.org",)


@pytest.fixture(autouse=True)
def _no_real_registry_download(monkeypatch, tmp_path):
    """Registry 节点判 dirty 时可能下载原包(node_sync.cnr_baseline)。测试里漏桩就失败,而不是悄悄出网
    (2026-10-05 codex review 0.8.59 P2)。
    拦在 urllib 的 OpenerDirector.open 上(urlopen 与 build_opener 都走它),不替换 node_sync 的函数:
    测试中途 import / importlib.reload 出来的模块对象替换不到,拦在这一层都跑不掉。同上,调用记下来、teardown 再判。
    哈希缓存落到本测试的 tmp 目录,内存里的缓存 / 失败记录每个测试清空,不在测试之间串。
    需要「下载成功 / 离线」的测试自己 monkeypatch _download_cnr_py_hashes(或 urllib.request.urlopen)。"""
    import sys
    import urllib.parse
    import urllib.request

    hits = []
    real_open = urllib.request.OpenerDirector.open

    def guarded_open(self, fullurl, *a, **k):
        url = fullurl if isinstance(fullurl, str) else getattr(fullurl, "full_url", "")
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _REGISTRY_HOSTS):
            hits.append(url)
            pytest.fail(f"测试里出现了未桩的 Registry 请求(会出网):{url}")
        return real_open(self, fullurl, *a, **k)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guarded_open)
    for name in ("node_sync", "comfyui_modal_bridge.node_sync"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_cnr_cache_dir"):
            monkeypatch.setattr(mod, "_cnr_cache_dir", lambda: tmp_path / "_cnr_baseline_cache")
            monkeypatch.setattr(mod, "_CNR_BASELINE_FAILED", {})
            monkeypatch.setattr(mod, "_CNR_BASELINE_MEM", {})
    yield hits
    assert not hits, f"测试里出现了未桩的 Registry 请求(被吞掉了):{hits}"
