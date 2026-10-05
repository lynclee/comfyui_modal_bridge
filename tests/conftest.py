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
    `except Exception` 吞不掉。需要真 SDK 行为的测试自己 monkeypatch 覆盖即可。"""
    try:
        import modal.client as _mc
    except Exception:
        return

    async def _refuse(*a, **k):
        pytest.fail("测试里出现了未桩的 Modal 控制面调用(会用真实凭据出网)—— 请 monkeypatch 掉它")
    monkeypatch.setattr(_mc._Client, "from_env", classmethod(_refuse), raising=False)
