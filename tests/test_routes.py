"""真实 aiohttp 路由测试 —— 补上"只验证源码长什么样"测不到的那一层。

## 为什么需要这个文件

test_core.py 里有 30 多条是**源码字符串检查**(断言某个调用存在、某两句的先后顺序)。
它们能挡住"某人把这段删了"这类回归,但验证不了"跑起来会怎样"。2026-08-31 连续两轮
codex review 抓到的两个 P1 恰好都从这个缺口漏过去:

  - AIGC 密钥搬进 Settings 后,前端提交的字段不在 /config 的 allowlist 里 → 每次都 400。
    源码检查全绿:函数在、调用在、设置项也注册了 —— 就是跑起来不工作。
  - /bridge_key 的本机限制:字符串检查只能确认"代码里有 request.remote",
    确认不了"非本机请求真的会被拒"。

## 怎么跑起来的(不引入任何新依赖)

routes.py 只依赖两个 ComfyUI 全局:`server.PromptServer.instance.routes` 用来注册路由,
`folder_paths.get_user_directory()` 用来定位 config。两个都用假模块注入 ——
后者尤其关键:**不注入的话 import 会读到用户的真实 config.json**。

没有 pytest-asyncio,所以异步用例包在 asyncio.run 里跑;CI 只装 ruff,不该为测试加依赖
(新依赖还会进 Registry 包)。
"""
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# ── 在 import 插件之前注入两个 ComfyUI 全局 ────────────────────────────────
_TMP_USER = Path(tempfile.mkdtemp(prefix="mb-routes-test-"))

if "folder_paths" not in sys.modules:
    _fp = types.ModuleType("folder_paths")
    _fp.get_user_directory = lambda: str(_TMP_USER)          # ← config 落到临时目录
    _fp.get_input_directory = lambda: str(_TMP_USER / "input")
    sys.modules["folder_paths"] = _fp

if "server" not in sys.modules:
    _srv = types.ModuleType("server")

    class _PromptServer:
        instance = None

    _PromptServer.instance = types.SimpleNamespace(routes=web.RouteTableDef())
    _srv.PromptServer = _PromptServer
    sys.modules["server"] = _srv

sys.path.insert(0, str(ROOT.parent))
import comfyui_modal_bridge.config as cfg_mod      # noqa: E402
import comfyui_modal_bridge.routes                 # noqa: E402,F401  (import 即注册路由)

_ROUTES = sys.modules["server"].PromptServer.instance.routes


def _set_cfg(**over):
    """写一份隔离的 config(绝不碰用户真实文件 —— 路径已被 folder_paths 假模块改到临时目录)。"""
    base = {
        "modal_endpoint_base": "https://ws--app",
        "bridge_api_key": "bk-secret-value",
        "local_api_capability": "cap-secret-value",
        "modal_token_secret": "as-secret",
        "comfy_api_key": "comfy-secret",
        "aigc_bypass_secret": "aigc-secret",
        "gpu_tier": "auto",
    }
    base.update(over)
    cfg_mod.save_config(base)
    return base


async def _client():
    app = web.Application()
    app.add_routes(_ROUTES)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def _run(coro_fn):
    async def main():
        c = await _client()
        try:
            return await coro_fn(c)
        finally:
            await c.close()
    return asyncio.run(main())


# ── 用例 ──────────────────────────────────────────────────────────────────
def test_get_config_never_leaks_credentials():
    """GET /config 不许回吐任何凭据 —— 这条只有真发一次请求才验得了。"""
    _set_cfg()

    async def body(c):
        r = await c.get("/modal_bridge/config")
        assert r.status == 200
        return await r.json()

    data = _run(body)
    raw = json.dumps(data, ensure_ascii=False)
    for leaked in ("bk-secret-value", "cap-secret-value", "as-secret",
                   "comfy-secret", "aigc-secret"):
        assert leaked not in raw, f"/config 回吐了凭据: {leaked}"
    # 但要给出"有没有配过"的布尔标志，否则前端没法显示占位符
    assert data.get("has_token_secret") is True
    assert data.get("has_aigc_bypass_secret") is True


def test_post_config_rejects_credential_fields():
    """POST /config 的 allowlist 必须挡住凭据字段。

    ⚠ 这正是 0.8.19 那个 P1 的验证:当时把 aigc_bypass_secret 也当普通设置提交,
    源码检查全绿,实际每次都 400、config 从没更新。反过来 URL 必须能写进去。
    """
    _set_cfg()

    async def body(c):
        out = {}
        for field, val in (("aigc_bypass_secret", "x"),
                           ("bridge_api_key", "x"),
                           ("local_api_capability", "x")):
            r = await c.post("/modal_bridge/config", json={field: val})
            out[field] = r.status
        r = await c.post("/modal_bridge/config",
                         json={"aigc_studio_base_url": "https://site.app/"})
        out["aigc_studio_base_url"] = r.status
        return out

    st = _run(body)
    for field in ("aigc_bypass_secret", "bridge_api_key", "local_api_capability"):
        assert st[field] == 400, f"{field} 竟然可以经 /config 写入(status={st[field]})"
    assert st["aigc_studio_base_url"] == 200, "URL 应当可写,否则设置页改了也不生效"
    assert cfg_mod.load_config()["aigc_studio_base_url"] == "https://site.app", "URL 没有真正落盘"


def test_bridge_key_rejects_non_loopback():
    """/bridge_key 只能本机取。TestClient 走的是 127.0.0.1,所以正常应放行;
    伪造成外部 Host 时必须拒 —— 反向代理后面 TCP peer 恒为 loopback,只看它会漏。"""
    _set_cfg()

    async def body(c):
        ok = await c.get("/modal_bridge/bridge_key")
        spoofed = await c.get("/modal_bridge/bridge_key",
                              headers={"Host": "bridge.example.com"})
        return ok.status, (await ok.json()).get("key"), spoofed.status

    ok_status, key, spoofed_status = _run(body)
    assert ok_status == 200 and key == "bk-secret-value", "本机取不到 key"
    assert spoofed_status == 403, "外部 Host 竟然能取走 bridge key"


if __name__ == "__main__":
    # 与 test_core.py 同样的极简自跑(CI 里没有 pytest,直接 python tests/test_routes.py)
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
    raise SystemExit(1 if failed else 0)
