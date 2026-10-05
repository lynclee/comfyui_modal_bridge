"""
ComfyUI 通信(WebSocket 监听 + 产物回传)
desktop 交付:小文件 base64、大文件写 Volume 直连取回;aigc-r2 交付只「发现」产物引用,
由 aigc_delivery 流式直传 R2(见 discover_outputs / materialize_desktop_outputs)。
"""
import base64
import json
import os
import socket
import time
import urllib.parse
import uuid
from pathlib import Path
from io import BytesIO

import requests
import websocket


COMFY_HOST = "127.0.0.1:8188"
WS_RECONNECT_ATTEMPTS = 5
WS_RECONNECT_DELAY_S = 3
# WS 静默这么久就查一次 /history 兜底(见 _history_settled)。60s 远长于任何正常消息间隔,
# 又远短于 worker 超时,探测开销(一次本地 HTTP)可忽略。
WS_IDLE_PROBE_S = 60

# 产物文件扩展名(图 / 视频 / 3D):用于从异构 history 输出里识别"这是个要回传的产物文件",
# 并给每个产物打 asset_type(image/video/model3d,aigc-r2 交付的 intake 契约要)。
# dict 形态({filename,subfolder,type},如 images/gifs/videos)保持原行为、不按扩展名过滤;
# 只有裸文件名字符串(如 Preview3D 的 result=[文件名, camera_info, bg])才按扩展名筛,
# 避免把 camera_info 之类非文件串也抓进来。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".apng"}
_MODEL3D_EXTS = {".glb", ".gltf", ".obj", ".fbx", ".stl", ".ply", ".splat", ".spz", ".ksplat"}
# 音频(SaveAudio / SaveAudioMP3 / SaveAudioOpus 等)。以前不认识,一律兜底成 image,
# aigc-r2 拿 "image" 去 intake,对端按图片类别校验 content-type,错得没有线索(2026-10-05 深度 review)。
# ⚠ 刻意不并进 _OUTPUT_EXTS:那个集合只用于从「裸字符串」里认文件名,音频节点都是 dict 形态,
#   并进去只会让文本类输出里恰好以 .mp3 结尾的字符串被误当成产物、取不到再整单失败。
_AUDIO_EXTS = {".flac", ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac"}
_OUTPUT_EXTS = _IMAGE_EXTS | _VIDEO_EXTS | _MODEL3D_EXTS


def classify_asset_type(filename: str, out_key: str = "") -> str:
    """按扩展名归类产物:image / video / model3d / audio。
    扩展名不认识时用输出键兜底(gifs/videos 这类键都是视频容器,audio 键是音频),再兜底 image。"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _MODEL3D_EXTS:
        return "model3d"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _IMAGE_EXTS:
        return "image"
    if out_key in ("gifs", "videos"):
        return "video"
    if out_key == "audio":
        return "audio"
    return "image"

# 大于此字节数的产物走 Volume 直连取回(本地 SDK 读),小的仍 base64。0 = 关(全 base64)。
# 阈值由部署烤进镜像 env(MODAL_BRIDGE_VOLUME_THRESHOLD_MB,默认 8MB)。
_VOL_THRESHOLD = int(os.environ.get("MODAL_BRIDGE_VOLUME_THRESHOLD_MB", "8")) * 1024 * 1024
# 单个任务内联(base64 进 job_state)的**总量**上限。上面那个阈值是逐文件判的,没有总量:
# 批量出 16 张每张 7MB 的 4K 图,每张都低于 8MB 走 base64,合计 ~150MB,写 job_state 时
# modal.Dict 抛 RequestSizeError —— 状态卡在 running,GPU 钱花了、产物只在容器里(2026-09-23
# review)。超出预算的后续产物一律改走 Volume,和大文件同一条取回路径。
_INLINE_TOTAL_BUDGET = int(os.environ.get("MODAL_BRIDGE_INLINE_TOTAL_MB", "24")) * 1024 * 1024


def wait_comfy_ready(timeout_s: int = 180, proc=None) -> None:
    """轮询 /system_stats 直到 ComfyUI HTTP 起来,超时 raise。

    proc:ComfyUI 子进程句柄。传了就每轮先看它还活着没有 —— 子进程启动即崩(依赖坏 / CUDA 初始化失败)
    时立刻失败,而不是对着一个死进程空等满 timeout_s(2026-10-05 深度 review:每次白烧 180s,
    而 enter 阶段同样按容器时长计费)。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc = proc.poll() if proc is not None else None
        if rc is not None:
            raise RuntimeError(f"ComfyUI 进程启动后就退出了(returncode={rc}),"
                               f"看容器日志里它最后几行输出")
        try:
            r = requests.get(f"http://{COMFY_HOST}/system_stats", timeout=2)
            if r.ok:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"ComfyUI didn't come up within {timeout_s}s")


def _comfy_server_status() -> dict:
    try:
        r = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {"reachable": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        return {"reachable": False, "error": str(e)}


def _attempt_ws_reconnect(ws_url, max_attempts, delay_s, initial_error):
    print(f"[bridge] WS dropped: {initial_error}. Reconnecting...")
    last_err = initial_error
    for i in range(max_attempts):
        srv = _comfy_server_status()
        if not srv["reachable"]:
            raise websocket.WebSocketConnectionClosedException(
                f"ComfyUI HTTP unreachable: {srv.get('error', srv.get('status_code'))}"
            )
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print("[bridge] WS reconnected")
            return new_ws
        except (websocket.WebSocketException, ConnectionRefusedError, socket.timeout, OSError) as e:
            last_err = e
            if i < max_attempts - 1:
                time.sleep(delay_s)
    raise websocket.WebSocketConnectionClosedException(f"reconnect failed: {last_err}")


def _history_settled(prompt_id: str) -> tuple[bool, list[str]]:
    """查 /history 判断这个 prompt 是否已经终结(跑完或跑挂),返回 (已终结, 错误列表)。

    ⚠ 为什么需要这个:ComfyUI 的 WS 是广播且不重放 —— 断线窗口里推的 `executing:node=None`
    完成事件永远补不回来。而主循环的退出条件只有「收到完成/错误事件」,没有超时分支
    (recv 超时是 continue)。于是丢一次完成事件 = 循环空转到 Modal function timeout 才被杀:
    烧满 worker_timeout 的钱、零产出、报一个跟真实原因无关的错。history 是 ComfyUI 的权威
    终态,拿它兜底。

    这里故意不加「主循环绝对超时」:那会引入第二个与 worker_timeout_sec 手动同步的旋钮
    (前端 timeoutSec 已经因此踩过坑),而空转的根因是丢事件,堵住它就够;真正的绝对上限
    由 Modal function timeout 承担。

    查不到 / 查失败一律返回 (False, []) —— 兜底绝不能制造假终态。"""
    try:
        h = get_history(prompt_id).get(prompt_id)
    except Exception as e:
        print(f"[bridge] history probe failed: {e}")
        return False, []
    return _history_verdict(h)


def _history_verdict(h) -> tuple[bool, list[str]]:
    """history 里**一条** prompt 记录的终态判定:(已终结, 错误列表)。纯函数。

    ⚠ WS 收尾(executing node=None)和 history 兜底两条路径都必须经过这里,结论才一致。
    以前 WS 那条只认「收到完成事件」:被 interrupt 的 prompt 照样会推 executing node=None
    (v0.37.2 main.py:prompt_worker 无论成败都发),中断前落盘的半成品就被当 completed 返回、
    照常计费;同一个 prompt 走 history 却判 error —— 两条路径对同一件事给出相反结论
    (2026-10-05 深度 review)。
    v0.37.2 的 status:status_str 只有 success / error,completed == (status_str == success)。
    另外 execution.py 在「节点排程阶段出错」那条分支只发 execution_error、不把 success 置 False,
    所以 messages 里出现 execution_error / execution_interrupted 一律按失败算,不只看 status_str。"""
    if not isinstance(h, dict):
        return False, []
    st = h.get("status") if isinstance(h.get("status"), dict) else None
    if st is None:
        # 老版 ComfyUI 没有 status 字段:有 outputs 就算跑完
        return (True, []) if h.get("outputs") else (False, [])
    detail = ""
    for m in st.get("messages") or []:
        if not (isinstance(m, (list, tuple)) and len(m) >= 2):
            continue
        d = m[1] if isinstance(m[1], dict) else {}
        if m[0] == "execution_error":
            detail = (f"Node {d.get('node_id')} ({d.get('node_type')}): "
                      f"{d.get('exception_message')}")
            break
        if m[0] == "execution_interrupted":
            detail = _interrupted_text(d)
            break
    if st.get("status_str") == "error" or detail:
        return True, [f"(from history) {detail or str(st.get('messages'))[:200]}"]
    if st.get("completed"):
        return True, []
    return False, []


def _interrupted_text(d: dict) -> str:
    return (f"执行被中断 —— Node {d.get('node_id')} ({d.get('node_type')}) 处停下,"
            f"产物不完整,不算成功")


def upload_images(images: list[dict]) -> dict:
    """把 base64 input images 上传到 ComfyUI(image-to-image 用)"""
    if not images:
        return {"status": "success"}
    errors = []
    for image in images:
        # 形态是契约:本地 routes / bridge_client 都发 {name, image: data URI}。以前缺键只抛
        # KeyError('image'),到用户手里整条报错就剩一个 'image',看不出是形态不对。
        # ⚠ 别写成"comfyagent 接 videos/audios 就会撞到"(旧注释的错,2026-09-08 seedance 实测更正):
        # 视频/音频在调用方也走同一个 image 键,这里照收、并以 image/png 传给 ComfyUI。
        # 真正会走到缺键这条路的是 {name, url, sha256} —— 素材超预算被调用方转存对象存储后的形态。
        # 任务本来就会失败(run_workflow 检查返回值后 raise),这里只是把原因说清。
        if not isinstance(image, dict):
            errors.append(f"upload failed: 期望 {{name, image}} 对象,收到 {type(image).__name__}")
            continue
        name = image.get("name")
        try:
            data_uri = image.get("image")
            if not isinstance(name, str) or not name or not isinstance(data_uri, str) or not data_uri:
                raise ValueError(
                    f"需要 {{name, image: data URI}} 形态(只支持图片参考);收到的键: {sorted(image)}")
            b64 = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
            blob = base64.b64decode(b64)
            # ⚠ 子目录必须拆成 subfolder 字段单独发,不能整串塞进 filename。
            #   ComfyUI 的 image_upload 是 open(join(input_dir, normpath(subfolder), filename)),
            #   而 makedirs 只建到 subfolder 那一层 —— filename 里带 "refs/" 时
            #   input/refs/ 根本没被创建,open() 直接 FileNotFoundError → HTTP 500。
            #   (2026-09-20 codex review 抓到;对着 ComfyUI v0.34.6 server.py 复现。)
            # ⚠ 越界自己也要挡一道:ComfyUI 有 commonpath 兜底,但 name 来自调用方提交的
            #   工作流,不该把唯一的边界检查外包给对端。
            safe = str(name).replace("\\", "/")
            if safe.startswith("/") or ".." in safe.split("/"):
                raise ValueError(f"输入素材路径非法(绝对路径或含 ..): {name}")
            sub, _, base_name = safe.rpartition("/")
            files = {
                "image": (base_name, BytesIO(blob), "image/png"),
                "overwrite": (None, "true"),
            }
            if sub:
                files["subfolder"] = (None, sub)
            r = requests.post(f"http://{COMFY_HOST}/upload/image", files=files, timeout=30)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"upload {name or '?'} failed: {e}")
    if errors:
        return {"status": "error", "details": errors}
    return {"status": "success"}


def interrupt_comfy() -> None:
    """让 ComfyUI 停下当前 prompt 并清空排队。best-effort,失败不抛(调用方正在处理异常)。

    ⚠ 顺序必须是**先清队列、再中断**:反过来的话,中断的那一刻下一个排队的 prompt 会立刻开跑。
    ⚠ 取消只会中断 worker 的 Python 线程(Modal 的 InputCancellation),**不会**碰容器里的
      ComfyUI 子进程 —— 它会继续跑被取消的 prompt。暖容器的下一单排在它后面等它跑完,
      用户为已取消的任务付了全部剩余 GPU 时间,而界面显示「✕ Cancelled」(2026-09-23 review)。
    接口依据 ComfyUI server.py:POST /queue {"clear": true}、POST /interrupt(v0.34.6 与 v0.37.2 都核过)。
    ⚠ 必须查状态码:ComfyUI 回 500 时请求「发出去了」,但它没停。以前只接网络异常,500 也记成
      「已停下」,线上日志会说反(2026-09-26 review)。"""
    ok = True
    for path, body in (("/queue", {"clear": True}), ("/interrupt", {})):
        try:
            requests.post(f"http://{COMFY_HOST}{path}", json=body, timeout=5).raise_for_status()
        except Exception as e:
            ok = False
            print(f"[bridge] ⚠ ComfyUI {path} 失败(prompt 可能仍在跑): {e}")
    if ok:
        # 留痕:没有这行的话,线上根本看不出取消后 ComfyUI 有没有被叫停(modal app logs 可查)
        print("[bridge] 已让 ComfyUI 停下:清空排队 + 中断当前 prompt")


def free_comfy_models() -> None:
    """调 ComfyUI /free 卸载已加载模型 + 释放显存。
    目的:关闭 ComfyUI 对模型文件的句柄,否则随后的 models_vol.reload() 会因
    'open files preventing operation' 失败。下个 job 反正要重新加载模型,卸载无损,
    还顺带清显存。失败不致命。"""
    try:
        r = requests.post(
            f"http://{COMFY_HOST}/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if r.ok:
            print("[bridge] freed ComfyUI models (released file handles)")
    except Exception as e:
        print(f"[bridge] /free 失败(忽略): {e}")


# 注:ComfyUI 0.22 没有"刷新模型列表"的 HTTP 接口。它的 get_filename_list 按目录 mtime
# 自动失效缓存(源码 folder_paths.cached_filename_list_ 比对 os.path.getmtime)——所以只要
# models_vol.reload() 让挂载点目录 mtime 变了,ComfyUI 下次验证 /prompt 会自动重扫看到新模型,
# 不需要也没有"主动 refresh"接口。之前那个 refresh_model_list 试的三个路径都不存在,已删。


# reload 撞上「还有打开的文件」时的退避(秒)。/free 只是在 ComfyUI 的队列上置个标志、立刻回 200,
# 真正的卸载由它的 prompt_worker 线程异步做(v0.37.2 server.py post_free → main.py prompt_worker
# 读 flags 后才 unload_all_models + gc)。所以 free 之后紧跟的第一次 reload 可能正好撞上句柄
# 还没关(2026-10-05 深度 review)。总共最多多等 7s,只发生在「模型不在列表」的重试路径上。
_RELOAD_BACKOFF_S = [1, 2, 4]


def _reload_volume_in_worker() -> None:
    """worker 内 reload Volume(拿最新文件视图 + 更新挂载点 mtime → ComfyUI 自动重扫)。
    先 free 卸载模型关句柄,否则 reload 撞 'open files'。free 是异步的,reload 失败就短暂退避重试。
    全部失败也不致命(本轮重试照样提交,ComfyUI 看到的只是旧视图)。"""
    free_comfy_models()
    try:
        import modal
        vol = modal.Volume.from_name(
            os.environ.get("MODAL_BRIDGE_VOLUME", "comfyui-bridge-models"))
    except Exception as e:
        print(f"[bridge] retry reload 失败(忽略): {e}")
        return
    for i in range(len(_RELOAD_BACKOFF_S) + 1):
        try:
            vol.reload()
            print("[bridge] volume reloaded (retry path)")
            return
        except Exception as e:
            if i >= len(_RELOAD_BACKOFF_S):
                print(f"[bridge] retry reload 失败(忽略): {e}")
                return
            wait = _RELOAD_BACKOFF_S[i]
            print(f"[bridge] reload 失败({e}),可能 /free 还没卸完模型,{wait}s 后再试")
            time.sleep(wait)


# 按需重试:正常 job 不 free/reload(模型留显存,秒级)。只有验证失败(模型不在列表 = 刚上传
# 还没看到 / 删 Volume 全新目录 / 最终一致延迟)时,才 free→reload→等→重试。free 只在这种
# 极端场景付一次代价(会卸显存模型,下次加载慢),日常不碰。等待递增,最多 _RETRY_MAX 次。
_RETRY_MAX = 5
_RETRY_WAITS = [3, 5, 8, 12, 15]  # 秒,逐次拉长


def _parse_validation_error(err: dict):
    """从 ComfyUI 400 响应提取 (details文案, 是否为'模型不在列表'类错误)。"""
    details, is_missing_value = [], False
    node_errors = err.get("node_errors") or {}
    for nid, nerr in node_errors.items():
        if isinstance(nerr, dict):
            for sub in nerr.get("errors", []) or []:
                if isinstance(sub, dict):
                    if sub.get("type") == "value_not_in_list":
                        is_missing_value = True
                    details.append(f"Node {nid}: {sub.get('details', sub)}")
            for et, em in nerr.items():
                if et != "errors":
                    details.append(f"Node {nid} ({et}): {em}")
        else:
            details.append(f"Node {nid}: {nerr}")
    return details, is_missing_value


class ValidationError(ValueError):
    """ComfyUI 拒收(400)或只收了一部分(2xx + node_errors)的工作流。继承 ValueError,老的捕获点不受影响。"""


def _withdraw_prompt(prompt_id: str) -> None:
    """撤掉一个已入队的 prompt:先从队列里删,已经开跑的再按 prompt_id 定向中断。best-effort。

    ⚠ 顺序是先删后断:删在 ComfyUI 的队列锁里做,要么删掉(还在排队),要么它已被取走在跑 / 跑完;
      随后的定向中断只对「正在跑的就是它」生效(v0.37.2 server.py post_interrupt 先查 currently_running),
      不会误伤别的 prompt。中断标志在每个 prompt 开跑时被重置(execution.py execute_async),
      所以即使它在中断前刚好跑完,标志也不会漏到我们接下来重新提交的那个 prompt 上。"""
    for path, body in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {"prompt_id": prompt_id})):
        try:
            requests.post(f"http://{COMFY_HOST}{path}", json=body, timeout=5).raise_for_status()
        except Exception as e:
            print(f"[bridge] ⚠ 撤回 prompt {prompt_id} 时 {path} 失败(它可能仍在跑): {e}")


def queue_workflow(workflow: dict, client_id: str) -> dict:
    """提交 workflow 到 ComfyUI /prompt。
    若验证失败是"模型不在列表"(value_not_in_list)→ reload Volume + 等待 + 重试(最多 _RETRY_MAX 次):
    覆盖"模型刚上传、worker 容器还没看到"的最终一致/全新目录场景,直到 ComfyUI 看到模型或重试用尽。
    其它验证错误(真缺节点/参数错)立即抛,不重试。

    ⚠ 「部分通过」也是验证失败:v0.37.2 只要还有一个输出分支合法,/prompt 就回 200、只把合法分支入队,
      被剔除的分支写在响应的 node_errors 里(server.py post_prompt / execution.py validate_prompt)。
      以前只看状态码:缺一个输出分支照样报 completed、照样计费,专为「模型刚上传、暖容器还没看到」
      写的 value_not_in_list 重试也被绕过 —— 而那恰好是最常见的部分失败(新传的 LoRA 只挂在一条分支上)
      (2026-10-05 深度 review)。所以 2xx 带 node_errors 时先撤回已入队的残缺 prompt,再和 400 走同一条路。"""
    # API 节点(comfy_api_nodes)鉴权:把 comfy.org API key 通过 /prompt 的 extra_data 传进去
    # (ComfyUI 从 extra_data.api_key_comfy_org 取,见 execution.py)。没配 key 就不带,普通工作流不受影响。
    body = {"prompt": workflow, "client_id": client_id}
    _comfy_key = os.environ.get("COMFY_API_KEY_COMFY_ORG")
    if _comfy_key:
        body["extra_data"] = {"api_key_comfy_org": _comfy_key}
    payload = json.dumps(body).encode("utf-8")
    attempt = 0
    while True:
        r = requests.post(
            f"http://{COMFY_HOST}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        partial = False
        if r.status_code != 400:
            r.raise_for_status()
            resp = r.json()
            node_errors = resp.get("node_errors") if isinstance(resp, dict) else None
            if not node_errors:
                return resp
            # 2xx 但有分支被剔除:残缺的那个 prompt 已经入队(甚至已开跑),先撤回,别让它白烧 GPU
            partial = True
            pid = resp.get("prompt_id")
            if pid:
                _withdraw_prompt(pid)
            err = resp
            print(f"[bridge] /prompt 只收了部分输出分支(已撤回 {pid or '?'}),"
                  f"被剔除的节点: {sorted(node_errors) if isinstance(node_errors, dict) else node_errors}")
        else:
            # 400: 解析验证错误
            try:
                err = r.json()
            except json.JSONDecodeError:
                raise ValidationError(f"ComfyUI 400: {r.text}")
        details, is_missing_value = _parse_validation_error(err)
        # 只对"模型不在列表"重试(可能是刚上传没看到);其它错误立即抛
        if is_missing_value and attempt < _RETRY_MAX:
            wait = _RETRY_WAITS[min(attempt, len(_RETRY_WAITS) - 1)]
            attempt += 1
            print(f"[bridge] 模型不在列表,reload+等{wait}s 重试 {attempt}/{_RETRY_MAX} "
                  f"(可能模型刚上传、容器还没看到)...")
            _reload_volume_in_worker()
            time.sleep(wait)
            continue
        head = ("Workflow validation(部分输出分支没通过校验,ComfyUI 只会执行其余分支 —— "
                "已撤回,不当成功): " if partial else "Workflow validation: ")
        if details:
            raise ValidationError(head + "; ".join(details))
        if partial:
            raise ValidationError(head + str(err.get("node_errors"))[:500])
        raise ValidationError(f"ComfyUI 400: {r.text}")


def get_history(prompt_id: str) -> dict:
    r = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    r.raise_for_status()
    return r.json()


def get_image_data(filename: str, subfolder: str, image_type: str) -> bytes | None:
    params = urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder or "", "type": image_type}
    )
    try:
        r = requests.get(f"http://{COMFY_HOST}/view?{params}", timeout=60)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"[bridge] view {filename} failed: {e}")
        return None


def discover_outputs(outputs: dict) -> list[dict]:
    """从 history 的 outputs 里「发现」所有产物(跳过 temp / input)—— 只返回引用,不读文件内容。
    纯函数(可单测)。每条:{filename, subfolder, type, node_id, key, asset_type}。
    扫每个输出节点的每个输出键:
      - dict 形态({filename,...},images/gifs/videos 等):按原样收
      - 裸文件名字符串(Preview3D 的 result=[文件名,camera_info,bg]):按扩展名筛收
    每条带来源 node_id(前端按节点回填,多 SaveImage 不串图)+ 输出键。去重。"""
    refs: list[dict] = []
    seen = set()
    for node_id, node_output in (outputs or {}).items():
        if not isinstance(node_output, dict):
            continue
        for out_key, val in node_output.items():
            if not isinstance(val, list):
                continue
            for item in val:
                if isinstance(item, dict):
                    filename = item.get("filename")
                    subfolder = item.get("subfolder", "")
                    img_type = item.get("type", "output")
                    if not filename:
                        continue
                elif isinstance(item, str):
                    if os.path.splitext(item)[1].lower() not in _OUTPUT_EXTS:
                        continue
                    filename, subfolder, img_type = item, "", "output"
                else:
                    continue
                # temp = 预览图;input = 用户上传的输入素材。v0.37.2 起 LoadVideo 会把输入视频作为 ui 预览
                # 放进 outputs(type="input"),以前只跳 temp,参考视频就被当成产物返回、还排在真产物前面,
                # 取「第一个 mp4」的调用方拿到的是参考视频(2026-09-27 comfyagent 在 h3_r2v 上撞到)。
                if img_type in ("temp", "input"):
                    continue
                dkey = (str(node_id), filename, subfolder)
                if dkey in seen:
                    continue
                seen.add(dkey)
                refs.append({
                    "filename": filename, "subfolder": subfolder, "type": img_type,
                    "node_id": str(node_id), "key": out_key,
                    "asset_type": classify_asset_type(filename, out_key),
                })
    return refs


def materialize_desktop_outputs(refs: list[dict], job_id: str) -> tuple[list[dict], list[str]]:
    """desktop 模式的「读取」:逐个产物取内容 → 小的 base64、大的写 Volume(本地 SDK 直连取回)。
    返回 (images 记录, errors)。"""
    images: list[dict] = []
    errors: list[str] = []
    inline_total = 0
    for ref in refs:
        image_bytes = get_image_data(ref["filename"], ref["subfolder"], ref["type"])
        if not image_bytes:
            errors.append(f"failed to fetch {ref['filename']}")
            continue
        # size_bytes:原始字节数(不是 base64 长度)。客户端取 Volume 产物时拿它校验是否收全;
        # 拿不到期望大小又没有 Content-Length 时不 ack、不删远端(契约 C12,2026-10-05 深度 review)。
        rec = {"filename": ref["filename"], "node_id": ref["node_id"], "key": ref["key"],
               "size_bytes": len(image_bytes)}
        over_file = _VOL_THRESHOLD and len(image_bytes) > _VOL_THRESHOLD
        # 阈值为 0 的约定是「关闭、全部内联」,总量预算也必须跟着关 —— 否则破坏既有契约
        over_total = (_VOL_THRESHOLD and _INLINE_TOTAL_BUDGET
                      and inline_total + len(image_bytes) > _INLINE_TOTAL_BUDGET)
        if over_file or over_total:
            # 大文件:写进挂载的 Volume(_outputs/<job>/<node>__<fn>)→ 本地 SDK 直连取回,不走 base64。
            # commit 由 modal_app._worker_run 在跑完后统一做(这里只写挂载点文件)。
            vp = f"_outputs/{job_id}/{ref['node_id']}__{ref['filename']}"
            dst = "/comfy-volume/" + vp
            # 囚笼:job_id 来自 /run 的入参(已在那边消毒),filename 来自 ComfyUI 的
            # history。两者都不是本函数生成的,而这里是往挂载卷**写文件** —— 逃出
            # _outputs/ 就等于用 bridge_key 换到了任意 Volume 写权限。多一次 resolve 的
            # 成本可以忽略,漏掉一次的代价是整个卷。
            _root = Path("/comfy-volume/_outputs").resolve()
            try:
                Path(dst).resolve().relative_to(_root)
            except ValueError:
                errors.append(f"unsafe output path (job_id={job_id!r}, "
                              f"filename={ref['filename']!r})")
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(image_bytes)
            rec["volume_path"] = vp
        else:
            inline_total += len(image_bytes)
            rec["data_base64"] = base64.b64encode(image_bytes).decode("utf-8")
        images.append(rec)
    return images, errors


def run_workflow(workflow: dict, job_id: str, input_images: list[dict] | None = None,
                 materialize: bool = True, on_progress=None) -> dict:
    """
    跑一个 workflow,返回所有产出图 base64。
    Returns: {images: [{filename, data_base64}], filename, data_base64, errors,
              output_refs: [发现步骤的产物引用(含 asset_type),aigc-r2 交付用]}
      - images: 所有非 temp 输出图(支持多 SaveImage / batch 出多图)
      - filename/data_base64: 第一张(向后兼容老回流路径)
      - materialize=False(aigc-r2):只「发现」不「读取」,images 留空 —— 产物不进
        base64/Volume/job_state,由 caller 拿 output_refs 流式直传 R2(大文件不整体进内存)。
    失败时 raise — 由 caller 转 status="failed"
    """
    # 注:boot() 已 wait_comfy_ready 过;这里不再重复等(ComfyUI 若中途崩,下面 ws 连接会快速报错)
    if input_images:
        up = upload_images(input_images)
        if up["status"] == "error":
            raise ValueError(f"Input image upload failed: {up['details']}")

    ws = None
    client_id = str(uuid.uuid4())
    errors: list[str] = []
    prompt_id: str | None = None

    try:
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)

        queued = queue_workflow(workflow, client_id)
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise ValueError(f"Missing prompt_id: {queued}")
        print(f"[bridge] queued workflow {prompt_id}")

        execution_done = False
        last_msg_at = time.time()   # 最近一次收到 WS 消息的时刻(静默探测用)

        def _settle_from_history(why: str) -> bool:
            """查 history,已终结则把结果写进闭包变量并返回 True(由 caller break)。"""
            nonlocal execution_done
            done, herrs = _history_settled(prompt_id)
            if not done:
                return False
            print(f"[bridge] {why} → history 显示已终结{'(有错误)' if herrs else ''},收尾")
            execution_done = not herrs
            errors.extend(herrs)
            return True

        while True:
            try:
                out = ws.recv()
                last_msg_at = time.time()
                if not isinstance(out, str):
                    continue
                msg = json.loads(out)
                t = msg.get("type")
                data = msg.get("data", {})
                if t == "executing":
                    if data.get("node") is None and data.get("prompt_id") == prompt_id:
                        execution_done = True
                        break
                elif t == "progress":
                    # ComfyUI 每完成一步推一条 {value, max}(采样器为主,带进度条的节点都有)。
                    # 这里保持哑管道:原样上抛,s/it 的计算和限频写状态由 caller 决定。
                    if on_progress and data.get("prompt_id") == prompt_id:
                        try:
                            on_progress(int(data.get("value") or 0), int(data.get("max") or 0))
                        except Exception:
                            pass  # 进度上报绝不能影响任务本体
                elif t == "execution_error":
                    if data.get("prompt_id") == prompt_id:
                        errors.append(
                            f"Node {data.get('node_id')} ({data.get('node_type')}): "
                            f"{data.get('exception_message')}"
                        )
                        break
                elif t == "execution_interrupted":
                    # 被中断的 prompt 之后照样会推 executing node=None(见 _history_verdict),
                    # 不在这里截住就会当成完成,把中断前落盘的半成品作为 completed 返回。
                    if data.get("prompt_id") == prompt_id:
                        errors.append(_interrupted_text(data))
                        break
            except websocket.WebSocketTimeoutException:
                # 静默超过阈值 → 查 history 兜底(可能完成事件在某次抖动里丢了)
                if time.time() - last_msg_at >= WS_IDLE_PROBE_S:
                    last_msg_at = time.time()
                    if _settle_from_history(f"WS 静默 {WS_IDLE_PROBE_S}s"):
                        break
                continue
            except websocket.WebSocketConnectionClosedException as e:
                ws = _attempt_ws_reconnect(ws_url, WS_RECONNECT_ATTEMPTS, WS_RECONNECT_DELAY_S, e)
                # ⚠ 断线窗口里推的完成事件不会重发,重连后必须立刻对一次 history,
                # 否则这个 job 会一直等一条永远不会再来的消息。
                if _settle_from_history("WS 重连"):
                    break
                last_msg_at = time.time()
            except json.JSONDecodeError:
                continue

        # ⚠ 执行出错必须直接失败,不能因为 history 里恰好有前序节点的产物就当成功。
        # 旧写法把「没完成」和「没出错」用 and 连起来做唯一的抛出条件 —— 于是出错时反而
        # **不抛**,继续往下从 history 捞产物返回,worker 那边无条件写 completed。
        # 结果:一个 BrokenNode 报错的工作流,只要前面某个节点落过一张图,就会被报成
        # "成功"、照常计费、并被 AIGC Studio 当完整产物发布出去(codex 已复现)。
        # 部分产物对报错的工作流几乎没有价值(视频只生成了前半段),而伪装成功的代价
        # 远大于丢弃它们。真要保留得另立 failed_with_outputs 契约,不是在这里含糊过去。
        if errors:
            raise RuntimeError("工作流执行出错: " + "; ".join(errors))
        if not execution_done:
            raise ValueError("Workflow ended without completion")

        history = get_history(prompt_id)
        if prompt_id not in history:
            raise ValueError(f"Prompt {prompt_id} not in history")
        # 终态以 history 为准(和 _history_settled 走同一个判定):WS 说完成不算数,history 里该 prompt
        # 的 status 必须是成功。WS 的完成事件在 task_done 写完 history 之后才发,这里读到的就是终值。
        # 老版 ComfyUI 没有 status 字段时 verdict 只看 outputs,下面「没有产物」那条照样会挡。
        settled, herrs = _history_verdict(history[prompt_id])
        if herrs:
            raise RuntimeError("工作流执行出错: " + "; ".join(herrs))
        if not settled and isinstance(history[prompt_id].get("status"), dict):
            raise ValueError(f"WS 报告完成,但 history 里 prompt {prompt_id} 没有标记成功: "
                             f"{str(history[prompt_id].get('status'))[:300]}")

        # 拆两步:先「发现」产物引用(不读内容),再按交付模式「读取」。
        # desktop = materialize(base64/Volume);aigc-r2 由 caller 拿 output_refs 走流式直传 R2。
        refs = discover_outputs(history[prompt_id].get("outputs", {}))
        if not refs:
            raise ValueError(f"No usable output (image/video/3d) in result. errors={errors}")
        if not materialize:
            return {"image_url": None, "images": [], "errors": errors, "output_refs": refs}
        images, mat_errors = materialize_desktop_outputs(refs, job_id)
        errors.extend(mat_errors)
        if not images:
            raise ValueError(f"No usable output (image/video/3d) in result. errors={errors}")
        # ⚠ 部分产物取回失败也必须失败。materialize 对每个 ref 要么产出一条记录、要么
        # append 一条 error 后 continue,所以数量对不上就是真丢了东西。
        # 旧写法只在"一个都没成功"时抛,于是「2 个输出只拿到 1 个」会照常返回、worker
        # 写 completed、errors 还被丢掉 —— 用户拿到残缺结果却显示全成功,而且照常计费。
        # 这和之前修过的「执行错误被吞成 completed」是同一类状态机漏洞:宁可明确失败。
        # 真要保留部分产物,得另立 failed_with_outputs / completed_with_warnings 契约,
        # 而不是在这里含糊过去。
        if len(images) < len(refs):
            lost = len(refs) - len(images)
            raise ValueError(
                f"{len(refs)} 个产物只取回 {len(images)} 个,缺 {lost} 个 —— "
                f"拒绝当成功返回(残缺结果比明确失败难查)。原因: {mat_errors}")
        return {
            "image_url": None,
            "images": images,
            "filename": images[0]["filename"],          # 向后兼容
            "data_base64": images[0].get("data_base64"),  # 向后兼容(Volume 项无 base64 → None)
            "errors": errors,
            "output_refs": refs,
        }
    finally:
        if ws and ws.connected:
            try:
                ws.close()
            except Exception:
                pass
