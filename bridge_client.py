"""
bridge_client.py — Modal Bridge 云端协议的独立客户端(零依赖:纯 Python 标准库)。

不需要 ComfyUI、不需要 modal SDK、不需要本插件的其它模块 —— 单文件即可
submit / status / wait / cancel / health / 产物落盘。是「脱离本地 ComfyUI 使用」
的共用引擎:mcp_server.py 的 cloud 模式和 bridge_cli.py 都基于它。

前提:有人(通常是部署者)已经用完整插件部署过云端 app、模型已在 Volume、
所需 custom_node 已在镜像 —— 本客户端只消费能力,不搬运模型/节点。

鉴权:bridge_api_key(部署时生成,存部署者的 config.json;GET ?key= / POST body auth_key)。
大文件:worker 把 >阈值 的产物写 Volume,状态里给 volume_path;本客户端经云端
/fetch 端点(0.7.3+)流式下载,不需要 modal token。

网络:云端是外网地址,walk 系统代理(env http_proxy/https_proxy)—— 与 mcp_server 的
localhost 直连策略相反,勿混用。
"""
import base64
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class BridgeError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, endpoint_base: str, key: str, timeout: int = 60):
        """endpoint_base 形如 https://<workspace>--comfyui-bridge(与 config.modal_endpoint_base 同)。"""
        if not endpoint_base or "--" not in endpoint_base:
            raise BridgeError("endpoint_base 形如 https://<workspace>--comfyui-bridge")
        self.base = endpoint_base.rstrip("/")
        self.key = key or ""
        self.timeout = timeout

    # ── 底层 ──
    def _url(self, label: str) -> str:
        return f"{self.base}-{label}.modal.run"

    def _get(self, label: str, params: dict, timeout: int | None = None) -> dict:
        qs = urllib.parse.urlencode({**params, "key": self.key})
        return self._req(f"{self._url(label)}?{qs}", None, timeout)

    def _post(self, label: str, body: dict, timeout: int | None = None) -> dict:
        return self._req(self._url(label), {**body, "auth_key": self.key}, timeout)

    def _req(self, url: str, body: dict | None, timeout: int | None, retries: int = 1) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        last: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"} if data else {})
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise BridgeError("401 unauthorized — bridge key 不对/缺失") from None
                if e.code in (502, 503, 504) and attempt < retries:
                    last = BridgeError(f"transient {e.code}")
                    time.sleep(1.5)
                    continue
                try:
                    return json.loads(e.read().decode())
                except Exception:
                    raise BridgeError(f"HTTP {e.code}: {url}") from None
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5)
        raise BridgeError(f"request failed: {last}") from last

    # ── 协议 ──
    def submit(self, workflow: dict, *, input_images: list | None = None,
               gpu_class: str = "primary", needs_gpu: bool = True,
               tier: str = "40g", job_id: str | None = None) -> dict:
        """提交工作流(API prompt 格式)。返回 {id, status, gpu}。
        gpu_class ∈ primary/cheap/top(部署时绑的卡型);needs_gpu=False → CPU worker(纯 API 工作流)。"""
        payload = {"workflow": workflow, "tier": tier, "needs_gpu": bool(needs_gpu),
                   "gpu_class": gpu_class, "delivery": {"mode": "desktop"},
                   "user_id": "bridge-client"}
        if input_images:
            payload["images"] = input_images
        # 幂等键:客户端定 job_id,重试用同一个 —— _req 对 502/503/504 和网络错会重试,
        # 而 spawn 可能在第一次就已经成功(响应丢在网关)。不带 id 的话服务端每次新建 uuid,
        # 重试 = 再开一个一模一样的 GPU 任务,双跑双计费,调用方只看得到第二个。
        payload["job_id"] = job_id or str(uuid.uuid4())
        d = self._post("run", payload)
        if "error" in d:
            raise BridgeError(f"/run: {d['error']}")
        if "id" not in d:
            raise BridgeError(f"/run 响应缺 id: {d}")
        return d

    def status(self, job_id: str) -> dict:
        """status ∈ queued/running/delivering/completed/failed/cancelled;
        running 带 progress:{step,total,s_it,elapsed}。"""
        return self._get("status", {"job_id": job_id}, timeout=20)

    def wait(self, job_id: str, timeout_s: int = 3600, poll_s: float = 2.0,
             on_update=None, max_consecutive_errors: int = 5) -> dict:
        """轮询到终态。on_update(state) 每次状态/进度变化时回调(打印进度用)。

        ⚠ 单次查询失败只意味着"这一拍没看到",不是终态 —— 任务在云端照常跑。
        以前任何 BridgeError 都直接打死整个 wait,而 _req 只重试一次:两次连续的
        瞬态网络错(某些网络环境一天能撞好几回)就足以让一个跑了 20 分钟的任务
        失去接管者,产物再也取不回、还继续计费。连续失败到上限才放弃,成功即清零。
        """
        deadline = time.time() + timeout_s
        last_sig = None
        errs = 0
        while time.time() < deadline:
            try:
                s = self.status(job_id)
                errs = 0
            except BridgeError as e:
                errs += 1
                if errs >= max_consecutive_errors:
                    raise BridgeError(
                        f"连续 {errs} 次查询失败,放弃等待 —— 任务可能仍在云端跑,"
                        f"可用 status/cancel 接管: {e}") from None
                time.sleep(poll_s)
                continue
            sig = (s.get("status"), json.dumps(s.get("progress") or {}, sort_keys=True))
            if sig != last_sig:
                last_sig = sig
                if on_update:
                    on_update(s)
            if s.get("status") in ("completed", "failed", "cancelled"):
                return s
            time.sleep(poll_s)
        raise BridgeError(f"wait timeout ({timeout_s}s) — 任务仍在云端跑,可 status/cancel")

    def cancel(self, job_id: str) -> dict:
        """⚠ 检查返回:带 error 表示取消失败、云端仍在计费。"""
        return self._post("cancel", {"job_id": job_id}, timeout=20)

    def health(self) -> dict:
        return self._get("health", {}, timeout=15)

    # ── 产物落盘 ──
    def download_outputs(self, state: dict, out_dir: str,
                         delete_remote: bool = True) -> list[dict]:
        """把 completed 状态里的产物写到 out_dir。小文件解 base64;大文件经云端 /fetch
        流式下载(delete_remote=True 下载后删 Volume 副本,同官方插件行为)。
        返回 [{filename, path, size_bytes}]。"""
        if state.get("status") != "completed":
            raise BridgeError(f"job 未完成: {state.get('status')}")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        job_id = state.get("id") or ""
        results, seen = [], set()

        def _name(fn: str) -> str:
            fn = Path(fn or "output.bin").name
            if fn in seen:
                stem, _, ext = fn.rpartition(".")
                fn = f"{stem}_{len(seen)}.{ext}" if ext else f"{fn}_{len(seen)}"
            seen.add(fn)
            return fn

        images = state.get("images")
        items = images if isinstance(images, list) and images else (
            [{"filename": state.get("filename"), "data_base64": state.get("data_base64")}]
            if state.get("data_base64") else [])
        for img in items:
            fn = _name(img.get("filename"))
            local = out / fn
            vp = img.get("volume_path")
            if vp:
                size = self._download_volume(job_id, vp, local, delete_remote)
            elif img.get("data_base64"):
                blob = base64.b64decode(img["data_base64"])
                local.write_bytes(blob)
                size = len(blob)
            else:
                continue
            results.append({"filename": fn, "path": str(local), "size_bytes": size})
        if not results:
            raise BridgeError("状态里没有可落盘的产物(images 为空)")
        return results

    def _download_volume(self, job_id: str, vol_path: str, local: Path,
                         delete_remote: bool) -> int:
        qs = urllib.parse.urlencode({"job_id": job_id, "path": vol_path,
                                     "key": self.key, "delete": int(delete_remote)})
        url = f"{self._url('fetch')}?{qs}"
        # 先写 .part、校验后原子 rename:delete_remote 时远端边传边清,
        # 中断若直接写终名会留下"看起来完整"的残缺文件。
        part = local.with_name(local.name + ".part")
        try:
            with urllib.request.urlopen(url, timeout=600) as r, open(part, "wb") as f:
                expected = int(r.headers.get("Content-Length") or 0)
                size = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
            if expected and size != expected:
                raise BridgeError(
                    f"/fetch 下载不完整: {size}/{expected} bytes({vol_path})")
            part.replace(local)
            return size
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise BridgeError(
                    f"/fetch 404:{vol_path} 不在 Volume 上(已被取过并删除?)") from None
            raise BridgeError(f"/fetch HTTP {e.code}(云端是 0.7.3+ 吗?老部署没有该端点)") from None
        finally:
            part.unlink(missing_ok=True)

    # ── 输入图打包(LoadImage 类节点 → data uri,协议与官方插件一致)──
    @staticmethod
    def pack_input_images(workflow: dict, search_dirs: list[str]) -> list[dict]:
        """扫 workflow 里 LoadImage/LoadImageMask/LoadImageOutput 引用的文件名,
        在 search_dirs 里找到并编成 [{name, image: data uri}]。找不到的抛错(与云端报错等价但更早)。"""
        names, out = [], []
        for node in (workflow or {}).values():
            if isinstance(node, dict) and node.get("class_type") in (
                    "LoadImage", "LoadImageMask", "LoadImageOutput"):
                ins = node.get("inputs") or {}
                n = ins.get("image") or ins.get("filename")
                if isinstance(n, str) and n not in names:
                    names.append(n)
        for n in names:
            # 工作流内容不可信:绝对路径 / ".." 会让 Path(d) / n 落到 search_dirs 之外,
            # 变成任意本地文件读取并上传。子目录相对路径(如 "sub/a.png")合法。
            pn = Path(n)
            if pn.is_absolute() or ".." in pn.parts:
                raise BridgeError(f"输入图路径非法(绝对路径或含 ..): {n}")
            p = next((Path(d) / n for d in search_dirs if (Path(d) / n).is_file()), None)
            if p is None:
                raise BridgeError(f"输入图找不到: {n}(搜索目录: {search_dirs})")
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            out.append({"name": n, "image": f"data:{mime};base64,{b64}"})
        return out
