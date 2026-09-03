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


def test_fetch_progress_reports_rate_eta_and_stall():
    """取回进度必须给出**速率、ETA 和停滞时长** —— 这三个才回答得了"到底卡没卡"。

    2026-09-03 用户反馈:8K 全景图工作流"卡在 Downloading result"一小时,后来成功了。
    实际没卡 —— 大产物走 Volume 直连下载,而 modal 的 read_file_into_fileobj 是一次
    阻塞调用没有进度回调,前端那句文案还**无条件**写死「Decoding base64...」(走 Volume
    时根本不解码)。一句说错了路径的静态文案挂一小时,和真挂住无法区分。
    追问也很直接:"download 很慢感觉也像卡死一样,能加个下载速度么"。

    速率在后端算而不是前端:后端固定 0.5s 采样,而前端 setInterval 在后台标签页会被
    节流(≥1s、甚至暂停),用它的时间差算速率会跳得没法看。
    """
    import comfyui_modal_bridge.routes as rt

    tmp = Path(tempfile.mkdtemp(prefix="mb-fetchprog-"))
    part = tmp / "big.png.part"
    total = 4 * 1024 * 1024                      # 分母:4 MB

    async def body(c):
        # ① 没有记录时不能报错,前端据此保留静态文案
        r = await c.get("/modal_bridge/fetch_progress?job_id=nosuchjob")
        assert r.status == 200
        assert (await r.json())["ok"] is False

        # ② 边长边采:间隔压到 20ms,窗口 0.4s(生产是 0.5s / 10s)
        task = asyncio.create_task(
            rt._sample_part_size("j-prog", part, total, "big.png",
                                 interval=0.02, window_s=0.4))
        try:
            n = 0
            # 增长期 0.4s、步长 40ms,采样 20ms → 每步至少两次采样,慢 CI 上也够算出速率
            for _ in range(10):                  # 涨到 1 MB
                n += 104858
                part.write_bytes(b"\0" * n)
                await asyncio.sleep(0.04)
            await asyncio.sleep(0.05)
            r = await c.get("/modal_bridge/fetch_progress?job_id=j-prog")
            moving = await r.json()

            # ③ 停止增长 → 停滞时长必须爬起来。
            #    stalled_s 是**整秒**(生产阈值 20s,整秒粒度足够),所以这里必须等过 1s ——
            #    等 0.5s 的话 int(0.5)==0,会误判成"没实现"。
            await asyncio.sleep(1.2)
            r = await c.get("/modal_bridge/fetch_progress?job_id=j-prog")
            stalled = await r.json()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return moving, stalled

    moving, stalled = _run(body)

    assert moving["ok"] is True and moving["stage"] == "volume"
    assert moving["label"] == "big.png"
    assert moving["done"] > 0, f"没读到已下载字节: {moving}"
    assert moving["total"] == total
    assert moving["bps"] > 0, f"没算出速率 —— 用户问的就是这个: {moving}"
    assert moving["eta_s"] > 0, f"分母已知且在动,必须给 ETA: {moving}"
    assert moving["stalled_s"] == 0, f"正在涨却报停滞: {moving}"

    assert stalled["stalled_s"] >= 1, \
        f"停止增长后 stalled_s 仍是 0 —— 那就无法区分「慢」和「挂住」: {stalled}"
    assert stalled["done"] == moving["done"] or stalled["done"] >= moving["done"]

    rt._FETCH_PROGRESS.pop("j-prog", None)


def test_local_nodes_diff_reports_pending_image_rebuild():
    """内容一致但依赖镜像还欠一次重建时,预检必须如实回报 —— 否则前端会说"无需推送"。

    2026-09-02 codex 抓到:/local_nodes_diff 当时只比 Volume 内容。上次依赖重建失败后
    `local_node_reqs_deployed_hash` 没有推进,而 zip 是一致的 → 预检报"全部一致" →
    前端不弹确认 → 随后 sync_local_nodes 仍满足 target != deployed,**无确认地触发
    几分钟镜像重建 + 云账单**。确认框存在的全部理由就是"别让一次点击悄悄变成几分钟"。

    这条真发请求。只验源码里有那个字段是不够的:字段在、值算错照样过。
    """
    import comfyui_modal_bridge.node_sync as ns
    import comfyui_modal_bridge.routes as rt

    reqs = ["pandas", "kornia"]
    matching = ns.local_node_reqs_hash(reqs)

    rt.local_nodes.plan_local_uploads = lambda cfg, folders, root: {
        "upload": [], "uptodate": [{"folder": f} for f in folders], "failed": [],
    }
    rt._compute_local_node_reqs = lambda cfg: reqs

    async def ask(c):
        r = await c.post("/modal_bridge/local_nodes_diff", json={"folders": ["my_node"]})
        return r.status, await r.json()

    # ① 指纹一致 = 镜像是最新的 → 不该打扰用户
    _set_cfg(local_node_reqs_deployed_hash=matching)
    st, body = _run(ask)
    assert st == 200, body
    assert body["upload"] == [] and body["uptodate"] == ["my_node"]
    assert body["reqs_redeploy_pending"] is False, \
        f"指纹一致却说还要重建,会每次都多问一次: {body}"

    # ② 指纹不一致(典型:上次重建失败)= 内容虽一致,但点下去仍会重建几分钟
    _set_cfg(local_node_reqs_deployed_hash="stale-hash")
    st, body = _run(ask)
    assert st == 200, body
    assert body["uptodate"] == ["my_node"], "内容明明一致,不该报成有改动"
    assert body["reqs_redeploy_pending"] is True, \
        f"依赖镜像欠重建却没回报 —— 前端会说「无需推送」然后静默卡几分钟: {body}"


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
