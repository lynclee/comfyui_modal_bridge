"""
ComfyUI 通信(WebSocket 监听 + 图回传)— 简化版
不上传 R2,直接返回 base64(因为 comfyui_modal_bridge 走 incognito 模式)
"""
import base64
import json
import os
import socket
import time
import urllib.parse
import uuid
from io import BytesIO

import requests
import websocket


COMFY_HOST = "127.0.0.1:8188"
WS_RECONNECT_ATTEMPTS = 5
WS_RECONNECT_DELAY_S = 3


def wait_comfy_ready(timeout_s: int = 180) -> None:
    """轮询 /system_stats 直到 ComfyUI HTTP 起来,超时 raise。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
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


def upload_images(images: list[dict]) -> dict:
    """把 base64 input images 上传到 ComfyUI(image-to-image 用)"""
    if not images:
        return {"status": "success"}
    errors = []
    for image in images:
        try:
            name = image["name"]
            data_uri = image["image"]
            b64 = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
            blob = base64.b64decode(b64)
            files = {
                "image": (name, BytesIO(blob), "image/png"),
                "overwrite": (None, "true"),
            }
            r = requests.post(f"http://{COMFY_HOST}/upload/image", files=files, timeout=30)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"upload {image.get('name','?')} failed: {e}")
    if errors:
        return {"status": "error", "details": errors}
    return {"status": "success"}


def queue_workflow(workflow: dict, client_id: str) -> dict:
    payload = {"prompt": workflow, "client_id": client_id}
    r = requests.post(
        f"http://{COMFY_HOST}/prompt",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code == 400:
        try:
            err = r.json()
            details = []
            if "node_errors" in err:
                for nid, nerr in err["node_errors"].items():
                    if isinstance(nerr, dict):
                        for et, em in nerr.items():
                            details.append(f"Node {nid} ({et}): {em}")
                    else:
                        details.append(f"Node {nid}: {nerr}")
            if details:
                raise ValueError("Workflow validation: " + "; ".join(details))
            raise ValueError(f"ComfyUI 400: {r.text}")
        except (json.JSONDecodeError, KeyError):
            raise ValueError(f"ComfyUI 400: {r.text}")
    r.raise_for_status()
    return r.json()


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


def run_workflow(workflow: dict, job_id: str, input_images: list[dict] | None = None) -> dict:
    """
    跑一个 workflow,返回第一张产出图 base64。
    Returns: {data_base64, filename, errors}
    失败时 raise — 由 caller 转 status="failed"
    """
    wait_comfy_ready()

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
        while True:
            try:
                out = ws.recv()
                if not isinstance(out, str):
                    continue
                msg = json.loads(out)
                t = msg.get("type")
                data = msg.get("data", {})
                if t == "executing":
                    if data.get("node") is None and data.get("prompt_id") == prompt_id:
                        execution_done = True
                        break
                elif t == "execution_error":
                    if data.get("prompt_id") == prompt_id:
                        errors.append(
                            f"Node {data.get('node_id')} ({data.get('node_type')}): "
                            f"{data.get('exception_message')}"
                        )
                        break
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException as e:
                ws = _attempt_ws_reconnect(ws_url, WS_RECONNECT_ATTEMPTS, WS_RECONNECT_DELAY_S, e)
            except json.JSONDecodeError:
                continue

        if not execution_done and not errors:
            raise ValueError("Workflow ended without completion")

        history = get_history(prompt_id)
        if prompt_id not in history:
            raise ValueError(f"Prompt {prompt_id} not in history")

        outputs = history[prompt_id].get("outputs", {})
        for _, node_output in outputs.items():
            for image_info in node_output.get("images", []):
                filename = image_info.get("filename")
                subfolder = image_info.get("subfolder", "")
                img_type = image_info.get("type")
                if img_type == "temp" or not filename:
                    continue
                image_bytes = get_image_data(filename, subfolder, img_type)
                if not image_bytes:
                    errors.append(f"failed to fetch {filename}")
                    continue
                return {
                    "image_url": None,
                    "filename": filename,
                    "data_base64": base64.b64encode(image_bytes).decode("utf-8"),
                    "errors": errors,
                }

        raise ValueError(f"No usable images in output. errors={errors}")
    finally:
        if ws and ws.connected:
            try:
                ws.close()
            except Exception:
                pass
