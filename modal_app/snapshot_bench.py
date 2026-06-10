"""
snapshot_bench.py — 隔离 bench:验证内存快照(CPU+GPU)对 ComfyUI 冷启的加速。
**不碰生产 modal_app**(独立 app 名 comfyui-bridge-snapbench),用同一个生产镜像 cuda_image。

用法(在插件目录、本机能 import modal 的 Python 下):
    cd custom_nodes/comfyui_modal_bridge/modal_app
    MODAL_BRIDGE_BENCH_GPU=L40S      python -m modal run snapshot_bench.py
    MODAL_BRIDGE_BENCH_GPU=A100-80GB python -m modal run snapshot_bench.py
    MODAL_BRIDGE_BENCH_GPU=H100      python -m modal run snapshot_bench.py
    MODAL_BRIDGE_BENCH_GPU=H200      python -m modal run snapshot_bench.py

对照:BaselineWorker(无快照) vs SnapWorker(开快照)。每轮 ping() 返回:
  - boot_s:本轮 boot 耗时。**restore 命中会"冻结"为快照里的值**(boot 没重跑)→ 判别 restore 的硬指标
  - alive :对 127.0.0.1:8188/system_stats 探活 —— 子进程恢复后还能服务?
  - marker:snap 阶段生成的随机值。值不变 = restore 命中;每轮都变 = 又重 boot 了

判读:
  - snap 模式前 2-3 轮是"快照制作 run"(可能比 baseline 还慢 ~6s,且 Modal 会给不同硬件变体各做一份)
  - 之后 boot_s/marker 冻住、wall 掉到个位数 = restore 命中,成功
  - 每轮之间等 ~25s 让容器缩容(scaledown_window=2),保证下一轮是真冷启
"""
import os
import subprocess
import time
import uuid

import modal

from modal_image import cuda_image

_GPU = os.environ.get("MODAL_BRIDGE_BENCH_GPU", "L40S")
app = modal.App("comfyui-bridge-snapbench")

_CMD = ["python", "/comfyui/main.py", "--listen", "127.0.0.1", "--port", "8188",
        "--extra-model-paths-config", "/comfyui/extra_model_paths.yaml"]


def _boot(self):
    from _comfy_ws import wait_comfy_ready
    t0 = time.time()
    self.proc = subprocess.Popen(_CMD)
    wait_comfy_ready(timeout_s=180)
    self.boot_s = round(time.time() - t0, 2)
    self.marker = uuid.uuid4().hex[:8]
    print(f"[bench] boot done in {self.boot_s}s marker={self.marker}")


def _alive() -> bool:
    import requests
    try:
        return requests.get("http://127.0.0.1:8188/system_stats", timeout=5).ok
    except Exception:
        return False


# scaledown_window 调小 → 容器空闲后秒级缩容,冷启实验更快。timeout 给足首次拉镜像。
_KW = dict(image=cuda_image, gpu=_GPU, min_containers=0, scaledown_window=2,
           timeout=600, max_containers=1)


@app.cls(**_KW)
@modal.concurrent(max_inputs=1)
class BaselineWorker:
    @modal.enter()
    def boot(self):
        _boot(self)

    @modal.method()
    def ping(self) -> dict:
        return {"mode": "baseline", "gpu": _GPU, "boot_s": self.boot_s,
                "alive": _alive(), "marker": self.marker}


@app.cls(enable_memory_snapshot=True,
         experimental_options={"enable_gpu_snapshot": True}, **_KW)
@modal.concurrent(max_inputs=1)
class SnapWorker:
    @modal.enter(snap=True)
    def boot(self):
        _boot(self)

    @modal.enter(snap=False)
    def ensure(self):
        # restore 后的正确性闸门:子进程没活就原地重启(退化为普通冷启)。
        if _alive():
            return
        print("[bench] restore 探活失败 → 重启子进程")
        try:
            self.proc.terminate()
        except Exception:
            pass
        _boot(self)

    @modal.method()
    def ping(self) -> dict:
        return {"mode": "snap", "gpu": _GPU, "boot_s": self.boot_s,
                "alive": _alive(), "marker": self.marker}


@app.local_entrypoint()
def main():
    rounds = int(os.environ.get("MODAL_BRIDGE_BENCH_ROUNDS", "4"))
    print(f"=== snapshot bench  GPU={_GPU}  rounds={rounds} ===")
    print("(snap 前 2-3 轮是快照制作 run,可能更慢;之后 boot_s/marker 冻住 + wall 掉到个位数 = restore 命中)\n")
    for label, cls in [("baseline", BaselineWorker), ("snap", SnapWorker)]:
        print(f"--- {label} ---")
        for i in range(rounds):
            t0 = time.time()
            r = cls().ping.remote()
            wall = round(time.time() - t0, 2)
            print(f"  run {i}: wall={wall:>6}s  boot_s={r['boot_s']:>6}  "
                  f"alive={r['alive']}  marker={r['marker']}")
            if i < rounds - 1:
                time.sleep(25)  # 等容器缩容,保证下轮真冷启
        print()
