"""
Modal app — comfyui_modal_bridge 自带的独立 worker(精简版,对齐 art_ai 的 4-endpoint 形态)

部署:
    modal deploy modal_app/modal_app.py

部署后拿到 4 个长期 URL(<ws> 由你的 modal 账号决定):
    https://<ws>--comfyui-bridge-run.modal.run     (POST,跑 workflow)
    https://<ws>--comfyui-bridge-status.modal.run  (GET,查状态)
    https://<ws>--comfyui-bridge-cancel.modal.run  (POST,取消)
    https://<ws>--comfyui-bridge-health.modal.run  (GET,健康 + 已装 custom_nodes)

模型不在这里下载。模型都在本地 ComfyUI Desktop 下好,由本地 `modal_volume.py`(SDK
batch_upload)直接传到 Volume(CAS 去重,通用模型秒过)。Volume 查询也走本地 SDK,
所以这里不再需要 list-models / check-models / seed-model / seed-status 这些 endpoint。

需要:
- Modal Secret  `comfyui-bridge-secrets`(BRIDGE_API_KEY 私有鉴权 + 可选 HF_TOKEN)
- Modal Volume  `comfyui-bridge-models`(自动创建,本地脚本往里传模型)
"""
import hmac
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import modal

from aigc_delivery import normalize_delivery, public_delivery
from modal_image import cuda_image


# ============================================================================
# Modal 资源
# ============================================================================
APP_NAME = os.environ.get("MODAL_BRIDGE_APP_NAME", "comfyui-bridge")
VOLUME_NAME = os.environ.get("MODAL_BRIDGE_VOLUME", "comfyui-bridge-models")
SECRET_NAME = os.environ.get("MODAL_BRIDGE_SECRET", "comfyui-bridge-secrets")
SCALEDOWN = int(os.environ.get("MODAL_BRIDGE_SCALEDOWN", "12"))
# worker 单任务超时上限(s)。部署时由 config.worker_timeout_sec 决定(覆盖最慢类别,如视频)。
# ⚠ Modal 的 timeout 是部署期固定的,运行时不可变 —— 换值需重新部署。
WORKER_TIMEOUT = int(os.environ.get("MODAL_BRIDGE_TIMEOUT", "1200"))
# 内存快照(实验,默认关):实测对 GPU worker 基本无效(ComfyUI 子进程盖不住,7 启动 0 复用,
# 反添 ~5s/次创建开销)。开关 = config.enable_snapshot → MODAL_BRIDGE_SNAPSHOT。
# 必须连 GPU 快照一起开(ComfyUI boot 探 CUDA;只 CPU 快照会以 CPU 模式初始化、恢复后切不回卡)。
# experimental,按 GPU 档需各自 bench;失败兜底见 ComfyWorker.ensure_comfy_alive(退化为普通冷启,不更差)。
_SNAPSHOT = os.environ.get("MODAL_BRIDGE_SNAPSHOT", "0") == "1"
# 关掉云端 ComfyUI 的动态 VRAM(见 _worker_boot)。开关 = config.disable_dynamic_vram。
# 默认不关:关掉后显存不够会直接 OOM,而非降速兜底 —— 是否划算取决于工作流,交给用户判断。
_DISABLE_DYNAMIC_VRAM = os.environ.get("MODAL_BRIDGE_DISABLE_DYNAMIC_VRAM", "0") == "1"
# 用 SageAttention 顶替 ComfyUI 默认的 PyTorch SDPA。开关 = config.use_sage_attention。
# 默认关:QK 走 INT8 是有损的(论文在 CogVideoX 上端到端 ~0.2%,但 H3 是音视频联合生成、
# 且权重本身已剪枝+INT8,误差叠加没有先例数据),需要自己看片验证后再常开。
_SAGE_ATTENTION = os.environ.get("MODAL_BRIDGE_SAGE_ATTENTION", "0") == "1"
DEPLOYED_VERSION = os.environ.get("MODAL_BRIDGE_VERSION", "unknown")  # 部署时烤进,health 回传
DEPLOYED_COMFYUI_TAG = os.environ.get("MODAL_BRIDGE_COMFYUI_TAG", "v0.22.0")  # 云端 clone 的 ComfyUI tag

app = modal.App(APP_NAME)
models_vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# secrets 含 BRIDGE_API_KEY(私有鉴权)+ 可选 HF_TOKEN。没建过则空 secret 兜底。
try:
    bridge_secret = modal.Secret.from_name(SECRET_NAME)
except Exception:
    bridge_secret = modal.Secret.from_dict({})

job_state = modal.Dict.from_name(f"{APP_NAME}-jobs", create_if_missing=True)

# job_state 清理:每条 completed 含整张图 base64,不清会让 Dict 无限膨胀。
# 策略:终态(completed/failed/cancelled)条目超过 JOB_TTL_S 就删;再按数量上限兜底。
JOB_TTL_S = int(os.environ.get("MODAL_BRIDGE_JOB_TTL", "3600"))   # 终态保留 1 小时(够客户端取回)
JOB_MAX = int(os.environ.get("MODAL_BRIDGE_JOB_MAX", "200"))       # 最多保留多少条
_VOL_GC_PER_SWEEP = 10  # 一次 sweep 最多删多少个 Volume 上的 _outputs/<job> 目录(见 _drop)
# cancel 对「查无此 job」的确认窗口(秒)。一次读就回 not_found 会漏掉跨容器还没同步到的任务,
# 见 cancel_endpoint。对真不存在的 job 只是晚几秒回复,对刚提交的 job 是「取消生不生效」的区别。
_CANCEL_NOT_FOUND_WAIT_S = 3.0


# `<job_id>:call` 的占位值:run_endpoint 在 spawn *之前* 写它,拿到真实 call_id 再覆盖。
# 目的是让这段窗口对 cancel 可见(见 run_endpoint / cancel_endpoint 的注释)。
_CALL_PENDING = "pending"

# job_id 会拼进 Volume 路径(_outputs/<job_id>/),并被 _sweep_job_state 用
# remove_file(recursive=True) 递归删除 —— 一个 "../../" 就能写到、删到 Volume 任意位置。
# /run 有 bridge_key 鉴权,但 job_id 由调用方(desktop / AIGC Studio 的任务 UUID)自带,
# 鉴权只证明"是我们的客户端",不证明"这个 id 干净"。
# ⚠ 刻意内联而不 import contract.is_safe_job_id:contract 不在镜像的
# add_local_python_source 名单里,容器内根本没有这个模块(和 modal_image.py 内联
# 目录哈希是同一个理由)。两处规则必须保持一致,改一边记得改另一边。
# ⚠ 首字符必须是字母数字(契约 C1,2026-10-05 深度 review):以前 "." 能过,GC 就会发出
#   remove_file("_outputs/.", recursive=True) —— 递归删的是整个 _outputs/,所有人没取回的产物一起没。
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _safe_job_id(job_id) -> bool:
    # fullmatch 而不是 match:`$` 会放过结尾的换行("abc\n")
    return (isinstance(job_id, str)
            and bool(_SAFE_JOB_ID.fullmatch(job_id))
            and ".." not in job_id)


# worker 被 Modal 按 timeout 强杀(或 OOM / 容器崩溃)时,容器里什么都执行不了 —— 没人写终态,
# 状态永远停在 running;_sweep_job_state 又只清终态,于是条目和 _outputs 目录也永不回收。
# 不能靠轮询时问 Modal:FunctionCall.get() 会把结果(可能几十 MB base64)整个拉回来,
# get_call_graph 官方文档明说「不实时、尽力而为、别用在关键路径」。
# 所以用确定性规则:非终态条目超过部署时的 worker 超时上限,Modal 必然已经把它杀了。
# 代价:OOM / 崩溃要到超时才被判定 —— 有上界,远好过永远 running。
_STALE_GRACE_S = 120
# queued 的判死线。排队不计费、也可能只是在等 GPU(B200 之类可能排很久),所以给得很宽 ——
# 这条只为兜住「容器在 @modal.enter 就失败(镜像 / 依赖 / ComfyUI 起不来)」:run() 根本不会执行,
# 状态永远 queued,不回收、Dict 一直涨,CLI / MCP 一直等(2026-09-24 review #12)。
_QUEUE_STALE_S = 6 * 3600


def _stale_reason(s, now: float) -> str:
    """非终态条目若 worker 必然已死 / 从没起来,返回失败说明;否则空串。"""
    if not isinstance(s, dict):
        return ""
    if s.get("status") == "queued":
        t0 = s.get("queued_at") or 0
        if t0 and now - t0 > _QUEUE_STALE_S:
            # ⚠ 别写「排队期间不计费」:正是这种「启动阶段就失败」的情形最费钱 —— Modal 按容器时长计费,
            #   @modal.enter 也算,容器反复拉起、反复失败,每一次都在计费(2026-10-05 深度 review)。
            return (f"排队超过 {_QUEUE_STALE_S // 3600} 小时仍未开始执行 —— worker 多半在启动阶段就失败了"
                    f"(镜像 / 依赖 / ComfyUI 起不来)。单纯排队等 GPU 不计费,但每次容器启动(含失败的启动)"
                    f"都按容器时长计费。可在 Modal 控制台看该调用的日志。")
        return ""
    if s.get("status") not in ("running", "delivering"):
        return ""
    t0 = s.get("started_at") or 0
    # ⚠ 用任务**起跑时**记下的超时,不能用当前部署的 WORKER_TIMEOUT:调小超时并重部署时,
    #   老部署上还在跑的长任务会被新部署的 status 按新上限误判死亡 —— 前端停止轮询,
    #   任务随后写回 completed 也没人取,付过钱的产物被 GC 清掉(2026-09-24 review)。
    limit = s.get("timeout_s") or WORKER_TIMEOUT
    if t0 and now - t0 > limit + _STALE_GRACE_S:
        return (f"worker 超过部署时的超时上限 {limit}s 仍未写回结果 —— 已被 Modal 强杀"
                f"(超时 / OOM / 容器崩溃),这段时间已计费。可在 Modal 控制台看该调用的日志。")
    return ""


def _effective(s, now: float):
    """**所有**读取方共用的实际状态:非终态但 worker 必然已死 / 从没起来 → 视为 failed(附原因)。
    没变返回原对象,变了返回新 dict —— 调用方用 `is` 判断要不要写回。

    ⚠ 以前这个判断只在 status 和 GC 两处:/run 仍把死任务当活的(对重复提交回 queued 而不是真实结局),
      /cancel 去取消一个早已结束的调用,报「取消失败」、前端据此提示「可能仍在计费」,正好说反
      (2026-09-24 review #12)。"""
    reason = _stale_reason(s, now)
    if not reason:
        return s
    # stale_from:判死前的状态。判死只是推断(见 _release_stale_call),下游据此在放掉记录前先取消原调用。
    return {**s, "status": "failed", "error": reason, "completed_at": now, "stale_from": s.get("status")}


def _call_id(job_id: str) -> str:
    """取真实 call_id;还在占位(spawn 中)或没有则返回空串。"""
    cid = job_state.get(f"{job_id}:call")
    return "" if not cid or cid == _CALL_PENDING else str(cid)


def _release_stale_call(job_id: str, s, call_id: str | None = None) -> str:
    """被判死(_effective 标了 stale_from)的任务,在放掉它的记录之前先 cancel 原调用。

    call_id:要取消的句柄。GC 必须传快照里的那个(绑定旧的这一次),不能在这里现读 —— 现读拿到的
    可能已经不是它核对过的那次提交(2026-09-27 review:核对后、取消前被换掉,取消的就是新调用)。
    None = 现读,只给 cancel_endpoint 用(用户取消的就是这个 id 现在的那次)。

    ⚠ 判死只是推断,原调用可能还会开跑:
      · 排队判死:Modal 的 timeout 不含排队等待,它可能还在队列里;
      · 执行超时判死:容器被抢占 / 内存错误时 Modal 会自动重试该 input,timeout 按每次 attempt 重新计,
        重试排着队时记录就可能已经判死(2026-09-26 review)。
      它开跑时读到的若是 GC 删空的记录,起跑检查拦不住,照样执行、照样计费(2026-09-26 review 隔离复现)。
      记录还是 failed 时起跑检查会拦下它,所以「记录要被删掉 / 回给取消方」之前调这里。
    返回错误说明;不需要处理或已释放返回空串。**从不抛异常**:GC 在 /run 里跑,抛了就让用户这次提交 500。
    2026-09-26 实测:对已完成 / 已失败的调用 cancel() 不报错;对过了保留期的旧调用(7 天前)和不存在的
    id 抛 NotFoundError —— 查不到的调用不可能再开跑,当作已释放,否则 GC 永远删不掉、取消报假的「仍在计费」。"""
    try:
        if not (isinstance(s, dict) and s.get("stale_from")):
            return ""
        if call_id is None:
            call_id = _call_id(job_id) or s.get("call_id")
        if not call_id:
            return ""
        modal.FunctionCall.from_id(call_id).cancel()
    except Exception as e:
        if type(e).__name__ == "NotFoundError":
            return ""
        return f"{type(e).__name__}: {e}"
    print(f"[bridge] job {job_id}: 判死的任务,已取消原调用 {call_id}(防止它之后开跑)")
    return ""


def _is_already_gone(e: BaseException) -> bool:
    """remove_file 失败是不是只因为「路径本来就不存在」(= 产物已取回 / 本来就走 base64,常态)。

    ⚠ 不能只认 FileNotFoundError。SDK 的本意是把服务端的 NotFoundError 转成 FileNotFoundError,
      但 v2 Volume 的服务端对不存在的路径实际回的是 InvalidError("No such file or directory."),
      SDK 那层映射接不住(2026-09-23 线上日志实证)。0.8.42 起 _drop 把「其它异常」当真失败、
      保留索引下次重试 —— 于是这些目录早就不在的条目**永远删不掉**,每次 sweep 都重试,
      每个还吃掉一格预算:10 个僵尸就把每轮的预算吃光,真正过期的任务一个都回收不了。
      当时的测试用 FileNotFoundError 模拟「已经没了」,和 SDK 的意图一致、和服务端的真实行为
      不一致,所以一直是绿的。"""
    return (isinstance(e, FileNotFoundError)
            or type(e).__name__ == "NotFoundError"
            or "no such file or directory" in str(e).lower())


# GC 节流:每个 run_endpoint 容器最多每 _SWEEP_EVERY_S 秒扫一次。
# ⚠ job_state.items() 会把**所有条目的完整值**拉回来 —— 包括最多 JOB_MAX 个已完成任务里的
#   base64 产物,动辄几百 MB。以前每次 /run 都拉(而且拉两遍),一串连续提交时每次都要先
#   传输并反序列化整个 Dict,run_endpoint 的 60s 超时很容易被吃掉(2026-09-23 review)。
#   GC 本来就是 best-effort,晚一分钟回收无所谓。节流状态放模块级(每容器),不放 Dict —— 放 Dict
#   里就成了一个非 job 的条目,得在所有遍历处特判。
_SWEEP_EVERY_S = 60
_last_sweep = [0.0]


def _sweep_job_state():
    """best-effort 清理过期/超量的终态 job。任何异常都不影响主流程。"""
    now = time.time()
    if now - _last_sweep[0] < _SWEEP_EVERY_S:
        return
    _last_sweep[0] = now
    try:
        items = list(job_state.items())
    except Exception:
        return
    terminal = {"completed", "failed", "cancelled"}
    finished = []
    records = dict(items)
    for jid, s in items:
        if not isinstance(s, dict):
            continue
        if s.get("status") in terminal:
            finished.append((jid, s.get("completed_at") or 0))
        elif _stale_reason(s, now):
            # worker 早被杀了 / 从没起来、没人写终态:按开始(或入队)时间当作已结束参与过期回收,否则永不 GC。
            finished.append((jid, s.get("started_at") or s.get("queued_at") or 0))
    vol_gc_budget = _VOL_GC_PER_SWEEP
    # 判死条目的重读 + 取消 RPC 另有一份额度:它们取消持续失败时,不许占掉正常回收的 Volume 预算
    # (判死的恰恰最旧,count-trim 也会先轮到它们)(2026-09-26 review)。
    rpc_budget = _VOL_GC_PER_SWEEP
    dropped = set()
    skip = set()   # 本轮已失败过的,第 2 阶段别再试(免得同一条重复发 RPC)

    def _drop(jid):
        """清一个终态 job。返回 False = 这次没动它(预算用完 / 快照之后变了 / 原调用没取消掉 / Volume 删失败),
        **什么都没删**(索引留着,下次 sweep 再来)。

        ⚠ 铁律:Volume 目录和 job_state 索引必须同生共死 —— 要么一起删,要么都不删。
        曾经是「先无条件删索引,再判预算」,于是一次 sweep 撞上 11 个过期 job 时,
        第 11 个索引没了、`_outputs/` 目录还在:下次 sweep 遍历 job_state 时再也看不到
        这个 jid,那个目录从此无人认领。限流本意是「这次先清 10 个」,实际成了
        「超出的部分永久泄漏」(2026-09-09 seedance 侧隔离执行复现,本文件同名回归测试钉死)。
        """
        nonlocal vol_gc_budget, rpc_budget
        safe = _safe_job_id(jid)
        # 限量:一次 sweep 最多删这么多个,避免某次提交撞上大批过期 job 时被一串 RPC 拖慢。
        # 脏 id 不占预算:它不碰 Volume,索引留着只会让 Dict 白涨。
        if (safe and vol_gc_budget <= 0) or jid in skip:
            return False
        snap = records.get(jid)
        # 判死 = 快照里是非终态但 worker 必然已死(_stale_reason),或 /status 已把判死结果写回(stale_from)
        stale = isinstance(snap, dict) and bool(snap.get("stale_from") or _stale_reason(snap, now))
        if stale and rpc_budget <= 0:
            return False
        live = snap
        try:
            if stale and not snap.get("stale_from"):
                # 快照里是「非终态、判死」:Modal 的自动重试 / 迟到的原调用可能已让它活过来(写回 running),
                # 甚至已经跑完。只有现在仍判死才回收 —— 否则就是删掉一个正在跑、正在计费的任务的记录和产物。
                # 判死的记录没有内联产物,重读很轻。
                rpc_budget -= 1
                live = job_state.get(jid)
                if live is not None and not _stale_reason(live, now):
                    return False
        except Exception:
            return False
        if stale:
            # 判死的任务:原调用可能还会开跑。记录一删,它开跑时起跑检查就拦不住了(见 _release_stale_call)。
            # 取消失败就整条留着(记录是 failed,起跑检查仍会拦),下次 sweep 再试。
            rpc_budget -= 1
            # 取消快照里的句柄,不现读(见 _release_stale_call 的 call_id)。占位值 = spawn 没成,没有可取消的。
            handle = records.get(f"{jid}:call")
            handle = "" if not handle or handle == _CALL_PENDING else str(handle)
            err = _release_stale_call(jid, _effective(live if live is not None else snap, now),
                                      call_id=handle or (snap.get("call_id") or ""))
            if err:
                skip.add(jid)
                print(f"[bridge] ⚠ GC {jid}: 原调用取消失败,记录保留到下次: {err}")
                return False
        # 顺带清 Volume 上的 _outputs/<job_id>/:成功取回会即删(见 modal_volume.download_volume_file
        # 和 fetch_endpoint 的 delete=1),但**失败/取消/客户端放弃**的大文件以前永久留在 Volume 上,
        # 谁也不会去删。job_state 条目都过期了,产物更没人要。
        # 二道闸:/run 已经挡住脏 id,但 Dict 里可能还留着旧版本写入的条目 ——
        # remove_file 是 recursive 删除,宁可漏删一个孤儿目录,也不能拿不可信的 id 去删 Volume。
        # 脏 id 不占预算也不阻塞索引清理:它压根不碰 Volume,索引留着只会让 Dict 白涨。
        if safe:
            vol_gc_budget -= 1
            try:
                models_vol.remove_file(f"_outputs/{jid}", recursive=True)
            except Exception as e:
                if not _is_already_gone(e):
                    # 别的异常要出声:静默失败 = GC 从来没生效过,而日志上看不出来。
                    # 索引也保留 —— 删失败还把索引丢掉,就又变成上面那种无人认领的孤儿目录。
                    print(f"[bridge] ⚠ Volume GC _outputs/{jid} 失败: {type(e).__name__}: {e}")
                    skip.add(jid)
                    return False
                # 目录不存在是常态(产物已取回 / 本来就是小文件走 base64)→ 照常删索引
        for k in (jid, f"{jid}:call", f"{jid}:progress"):  # 连带删独立键,不留孤儿
            try:
                del job_state[k]
            except Exception:
                pass
        dropped.add(jid)
        return True
    # 1) 过期删
    for jid, done_at in finished:
        if done_at and now - done_at > JOB_TTL_S:
            _drop(jid)
    # 2) 数量兜底:仍超上限就删最旧的终态条目。用同一份快照算,**不再拉第二遍**(每拉一遍都是全量值)。
    try:
        remaining = [(j, t) for j, t in finished if j not in dropped]
        if len(remaining) > JOB_MAX:
            remaining.sort(key=lambda x: x[1])
            for jid, _ in remaining[: len(remaining) - JOB_MAX]:
                _drop(jid)
    except Exception:
        pass
    # 3) 没有索引的 _outputs 目录(见 _sweep_orphan_outputs)。复用本轮快照,不再拉一遍 Dict。
    try:
        _sweep_orphan_outputs(records, now)
    except Exception as e:
        print(f"[bridge] ⚠ 孤儿产物扫描失败(忽略): {type(e).__name__}: {e}")


# 孤儿产物兜底扫描:最多每 _ORPHAN_SWEEP_EVERY_S 秒一次(每个 run_endpoint 容器各自计),一次最多
# _VOL_GC_PER_SWEEP 个 Volume RPC。模块级节流,理由同 _last_sweep。
_ORPHAN_SWEEP_EVERY_S = 3600
_last_orphan_sweep = [0.0]


def _dir_entry_is_dir(ent) -> bool:
    t = getattr(ent, "type", None)
    return getattr(t, "name", None) == "DIRECTORY" or t == 2   # modal.volume.FileEntryType.DIRECTORY


def _sweep_orphan_outputs(records: dict, now: float) -> None:
    """删掉 job_state 里已经没有索引的 `_outputs/<jid>/` 目录。

    为什么需要:_drop 让目录和索引同生共死,前提是索引还在。Modal Dict 的条目 7 天没有读写就会过期,
    如果最后一批任务之后超过 7 天不再有人提交(GC 只在 /run 里跑),没取回的大文件目录的索引先过期了,
    之后再也没人认领它们,Volume 一直付存储钱(2026-10-05 深度 review)。

    判据(全部满足才删):
      · Volume 上的目录名是合法 job_id(脏名字一律不碰,remove_file 是递归删);
      · 本轮 Dict 快照里 `jid` / `jid:call` / `jid:progress` 都不存在;
      · 最后修改时间早于 2 × JOB_TTL_S。正常任务写完产物、commit 时索引早已存在,哪怕跨容器最终一致
        有延迟也是秒级;目录 2 小时没动过还查不到索引,只能是索引过期 / 被删后留下的。
    mtime 取目录项自己的(FileEntry.mtime,秒);目录项报 0 时退一步列它里面的文件取最大 mtime,
    仍拿不到就不删 —— 宁可漏删,不可错删。"""
    if now - _last_orphan_sweep[0] < _ORPHAN_SWEEP_EVERY_S:
        return
    _last_orphan_sweep[0] = now
    try:
        entries = models_vol.listdir("_outputs", recursive=False)
    except Exception as e:
        if not _is_already_gone(e):        # 还没有任何大文件产物时 _outputs 本来就不存在
            print(f"[bridge] ⚠ 孤儿产物扫描:列 _outputs 失败: {type(e).__name__}: {e}")
        return
    budget = _VOL_GC_PER_SWEEP
    horizon = 2 * JOB_TTL_S
    for ent in entries:
        if budget <= 0:
            break
        jid = str(getattr(ent, "path", "") or "").rstrip("/").rsplit("/", 1)[-1]
        if not _dir_entry_is_dir(ent) or not _safe_job_id(jid):
            continue
        if any(k in records for k in (jid, f"{jid}:call", f"{jid}:progress")):
            continue
        mtime = getattr(ent, "mtime", 0) or 0
        try:
            if not mtime:
                budget -= 1
                inner = models_vol.listdir(f"_outputs/{jid}", recursive=True)
                mtime = max((getattr(x, "mtime", 0) or 0 for x in inner), default=0)
            if not mtime or now - mtime <= horizon:
                continue
            budget -= 1
            models_vol.remove_file(f"_outputs/{jid}", recursive=True)
            print(f"[bridge] GC 孤儿产物 _outputs/{jid}(无索引,{int((now - mtime) // 3600)}h 未改动)")
        except Exception as e:
            if not _is_already_gone(e):
                print(f"[bridge] ⚠ 孤儿产物 _outputs/{jid} 清理失败: {type(e).__name__}: {e}")


# ============================================================================
# ComfyUI worker — 两档(按显存),每档 gpu=list 走 Modal 原生 fallback
# boot/run 提取为模块函数,两个 class 共享,只 GPU 档不同。
# ============================================================================
_WORKER_KW = dict(
    image=cuda_image,
    volumes={"/comfy-volume": models_vol},
    secrets=[bridge_secret],
    scaledown_window=SCALEDOWN,
    timeout=WORKER_TIMEOUT,
    min_containers=0,
    max_containers=10,
)


# ComfyUI 启动输出的副本。只留启动阶段那段(足够覆盖 "Import times for custom nodes"),
# 不无限增长 —— worker 可能连续跑几小时。
_BOOT_LOG: list = []
_BOOT_LOG_MAX = 3000


def _pump_comfy_output(proc) -> None:
    """把 ComfyUI 的 stdout 转发到容器日志,顺带留一份启动阶段的副本。

    ⚠ 这个线程不能停:stdout=PIPE 之后没人读,管道满了 ComfyUI 就卡死在 write 上。
    所以整段用 try 包住,出任何问题都只是少了诊断信息,不影响 ComfyUI 本身。
    """
    try:
        for line in proc.stdout:
            print(line, end="", flush=True)     # 容器日志照旧
            if len(_BOOT_LOG) < _BOOT_LOG_MAX:
                _BOOT_LOG.append(line)
    except Exception as e:
        print(f"[bridge] ComfyUI 日志转发停止: {e}")


def import_failure_hint(class_type: str = "") -> str:
    """任务报"节点不存在"时,回一句真因提示;没有导入失败记录就返回空串。

    ComfyUI 对"包 import 失败"和"包根本没装"给的是同一句
    "The custom node may not be installed",而这两者的处理办法完全相反:
    前者要修依赖,后者才是去同步节点。分不清的话用户只会反复点同步。
    """
    try:
        from comfy_log import parse_import_failures
        failed = parse_import_failures("".join(_BOOT_LOG)).get("failed") or []
    except Exception:
        return ""
    if not failed:
        return ""
    lines = [f"⚠ 本 worker 有 {len(failed)} 个 custom_node 包**导入失败**(不是没装):"]
    for f in failed[:6]:
        name = f.get("name") or f.get("path") or "?"
        why = (f.get("error") or "").strip()
        lines.append(f"    - {name}" + (f": {why[:200]}" if why else ""))
    if len(failed) > 6:
        lines.append(f"    …还有 {len(failed) - 6} 个")
    lines.append("  导入失败 = 包在云端装了但 import 时抛错(通常缺依赖或版本不兼容),"
                 "再点几次「同步节点」也不会好 —— 要修的是那个包的依赖。")
    if class_type:
        lines.append(f"  你要的节点 `{class_type}` 很可能就在其中某个包里。")
    return "\n".join(lines)


def _gpu_compute_cap() -> str:
    """探测容器内 GPU 的 compute capability(如 "9.0"),探测失败返回 ""。
    故意用 nvidia-smi 而非 import torch:boot wrapper 进程不必为这一个数字付 torch 冷 import 的钱。
    探测不到时按「非 sm_90」处理 —— sage 是锦上添花,SDPA 永远正确,宁可慢不可炸。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
        return out[0].strip() if out else ""
    except Exception:
        return ""


# 本容器里正在运行的 ComfyUI 进程**启动时**加载的本地节点指纹({folder: digest})。每次拉起 ComfyUI 前,
# 从磁盘 marker 照一张像存这里 —— ComfyUI 只在启动时扫一次 custom_nodes,所以「启动那一刻的磁盘」
# 就是「内存里在跑的代码」。暖容器纠偏判断过期必须比它,不能比磁盘(见 _refresh_local_nodes_if_stale)。
# None = 还没记过 / 记失败,退回比磁盘(旧行为)。
_LOADED_LOCAL_DIGESTS: dict | None = None


def _worker_boot(self, cpu: bool = False, load_local_nodes: bool = True):
    # cpu=True:CPU-only 容器(无 GPU)→ 给 ComfyUI 传 --cpu 强制 CPU 模式,根本不碰 CUDA 初始化,
    # 避免 CUDA 版 torch 在无驱动机器上自动探测的边角风险。GPU 容器走默认(自动用 CUDA)。
    self._cpu = cpu
    models_vol.reload()  # 启动前同步 Volume(ComfyUI 还没打开文件,不冲突)
    # 本地自写节点(没有 git remote、传不上 GitHub 的那些)从 Volume 解压进 custom_nodes/。
    # 必须在 ComfyUI 起来之前做 —— 它只在启动时扫一次 custom_nodes。失败不阻断启动。
    if load_local_nodes:
        try:
            from _local_nodes_boot import extract_all
            extract_all()
        except Exception as e:
            print(f"[bridge] ⚠ 本地节点装载跳过: {e}")
    # ⚠ 别再做「Triton JIT 缓存落 Volume」:2026-08-06 实测过并回退(git 历史 9fb8efc→此提交)。
    # 全历史逐单对比证明冷容器首步的慢(25~95s 波动)来自**宿主机 Volume 冷缓存**的权重
    # 流式 stall(无缓存的冷容器首步同样只有 25.8s,铁证),Triton 编译本身仅几秒;
    # 三段式同步 + 竞态防护约 40 行复杂度,换来的收益是噪声级 —— 砍。
    cmd = [
        "python", "/comfyui/main.py",
        "--listen", "127.0.0.1", "--port", "8188",
        "--extra-model-paths-config", "/comfyui/extra_model_paths.yaml",
    ]
    if cpu:
        cmd.append("--cpu")  # --cpu 本身就会关掉动态 VRAM,不必再叠加下面那个开关
    elif _DISABLE_DYNAMIC_VRAM:
        # 动态 VRAM 默认开:权重只常驻一小部分,其余按需从 CPU 搬(日志里的 "N MB Staged" +
        # "Force pre-loaded ... KB")。显存装得下时这层搬运是白付的 PCIe 时间,而云上按秒计费。
        # 关掉 = 估算式加载,更快;代价是显存不够时直接 OOM,没有降速兜底。
        cmd.append("--disable-dynamic-vram")
    sage_on = False
    if not cpu and _SAGE_ATTENTION:
        # 只在 wheel 真有 kernel 的架构上开 sage:multiarch wheel 含 sm_89(L40S)+ sm_90a(H100),
        # 双卡冒烟对 SDPA 余弦 0.9992+(含 H3 真实形状 56×90720×96),见 Release
        # sage-2.2.0-d1a57a5-multiarch。门控存在的原因:sm89 分支若装的是错架构代码,launch
        # 失败不报 Python 异常,而是采样时异步 CUDA illegal access 打崩整个 job(2026-08-06
        # 在 L40S 实测,初版 sm90-only wheel);B200(sm100)则因 dispatch 无分支抛 ValueError
        # 被 ComfyUI 兜住回退 SDPA —— 有假 kernel 的架构比没分支的更危险,所以白名单制。
        cap = _gpu_compute_cap()
        if cap in ("8.9", "9.0"):
            sage_on = True
            cmd.append("--use-sage-attention")
        else:
            print(f"[bridge] sage-attention skipped: compute_cap={cap or '?'} "
                  f"(wheel 只有 sm_89/sm_90 kernel) → 回退 PyTorch SDPA")
    # 捕获 ComfyUI 的输出:转发到容器日志(可观测性不能丢),同时留一份启动阶段的副本。
    # 用途:节点整包 import 失败时(依赖缺失/版本不兼容),ComfyUI 只在启动日志里打
    # `(IMPORT FAILED): <path>`,而任务失败回到前端只剩一句
    # "Node 'X' not found. The custom node may not be installed." —— 用户完全无从推断
    # 是依赖缺失导致整包没加载,只会以为节点没同步上去、反复去点同步。
    # 2026-08-31 实测撞上的是 art-venture:setuptools≥84 移除了 pkg_resources,整包挂掉。
    # ⚠ stdout=PIPE 就必须持续读:不读的话管道缓冲区满了,ComfyUI 会阻塞在 write 上。
    #   所以下面那个 pump 线程是硬要求,不是优化。任何一步出问题都退化成不捕获。
    _BOOT_LOG[:] = []
    global _LOADED_LOCAL_DIGESTS
    try:
        from _local_nodes_boot import current_digests
        _LOADED_LOCAL_DIGESTS = current_digests()
    except Exception as e:
        _LOADED_LOCAL_DIGESTS = None
        print(f"[bridge] ⚠ 记录已加载的本地节点版本失败(纠偏退回比磁盘): {e}")
    try:
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, errors="replace")
        threading.Thread(target=_pump_comfy_output, args=(self.proc,), daemon=True).start()
    except Exception as e:
        print(f"[bridge] ⚠ 无法捕获 ComfyUI 输出({e}),退化为直连 stdout(诊断信息会少一些)")
        self.proc = subprocess.Popen(cmd)
    from _comfy_ws import wait_comfy_ready
    # 把进程句柄传进去:启动即崩时立刻失败,不对着死进程空等 180s(enter 阶段同样计费)
    wait_comfy_ready(timeout_s=180, proc=self.proc)
    if cpu:
        mode = "CPU"
    else:
        bits = ["GPU"]
        if _DISABLE_DYNAMIC_VRAM:
            bits.append("dynamic-vram off")
        if sage_on:
            bits.append("sage-attention")
        mode = ", ".join(bits)
    print(f"[bridge] ComfyUI ready ({mode})")


def _worker_shutdown(self, wait_s: float = 20.0):
    """停掉 ComfyUI 子进程,并**等它真的退出**。
    ⚠ 只 terminate() 不 wait() 会有端口竞争:SIGTERM 是异步的,旧进程可能还占着 8188,
      紧接着起的新进程 bind 失败;更坏的是 wait_comfy_ready 可能探到正在退出的旧进程、
      判定 ready,随后任务打向一个已经关掉的服务。所以这里必须等干净再返回。"""
    proc = getattr(self, "proc", None)
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception as e:
        raise RuntimeError(f"无法终止旧 ComfyUI 进程: {e}") from e
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()          # 赖着不走就硬杀
            proc.wait(timeout=10)
        except Exception as e:
            # 不能确认旧进程已退出就绝不能在同一端口启动新的；否则 readiness 可能误命中旧进程。
            raise RuntimeError(f"旧 ComfyUI 进程未能退出: {e}") from e


# ── 暖容器探活 / 退役(2026-10-05 深度 review)───────────────────────────────────
# ComfyUI 子进程死掉(OOM kill / segfault)或 CUDA 进了黏性错误态之后,暖容器以前既不探活也不退役:
# 之后每一单都秒失败(WS 连不上 / 每个 CUDA 调用都报同一个错),直到空闲回收 —— 一串排队的任务
# 全部白白失败。所以:每单开跑前探活,不健康就原地重启;遇到不可达或 CUDA 黏性错误,当前单结束后
# 让容器不再接单(Modal 换一个新容器来跑后面的)。
# 黏性错误的典型文本:"CUDA error: an illegal memory access was encountered"(之后同进程的 CUDA 调用都失败)。
# 普通的显存不足("CUDA out of memory")不黏,不在此列。
_STICKY_CUDA = re.compile(r"CUDA error|illegal memory access", re.IGNORECASE)


def _comfy_unhealthy_reason(self) -> str:
    """ComfyUI 子进程不健康的原因;健康返回空串。进程已退出是确定的;HTTP 探活给两次机会,
    别因为一次偶发超时就把显存里的模型全丢掉重启。"""
    proc = getattr(self, "proc", None)
    rc = proc.poll() if proc is not None else None
    if rc is not None:
        return f"ComfyUI 子进程已退出(returncode={rc})"
    import requests
    why = ""
    for i in range(2):
        try:
            r = requests.get("http://127.0.0.1:8188/system_stats", timeout=10)
            if r.ok:
                return ""
            why = f"/system_stats HTTP {r.status_code}"
        except Exception as e:
            why = f"/system_stats 不可达: {type(e).__name__}: {e}"
        if i == 0:
            time.sleep(1)
    return why


def _ensure_comfy_healthy(self) -> None:
    """每单开跑前调用:不健康(或上一单标记了要重启)就原地重启 ComfyUI。重启失败照常抛,由调用方记 failed。"""
    why = ("上一单遇到不可恢复的错误,已标记重启" if getattr(self, "_restart_before_next", False)
           else _comfy_unhealthy_reason(self))
    if not why:
        return
    print(f"[bridge] ⚠ ComfyUI 不健康({why})→ 原地重启后再跑这一单")
    self._restart_before_next = False
    _worker_shutdown(self)
    # 等价于一次冷启动:重新 reload Volume、重新解压本地节点(并重新记下已加载的版本)
    _worker_boot(self, cpu=getattr(self, "_cpu", False))


def _retire_container(self, why: str) -> None:
    """让本容器跑完当前单后不再接单。

    用的是 modal.experimental.stop_fetching_inputs()(本机 SDK 1.4.3 有;容器里的版本以镜像为准,所以 getattr 防御;
    实现见 modal/_runtime/container_io_manager.py:_generate_inputs —— 置 _fetching_inputs=False,
    max_inputs=1 的同步 worker 在当前 input 返回后不再取下一个,容器优雅退出、照常走 @modal.exit)。
    SDK 没有这个 API 或调用失败时,退化为「下一单开跑前原地重启」(_restart_before_next)。
    两条都设上:万一已经有下一单在途,它开跑前也会先重启。"""
    self._restart_before_next = True
    stop = None
    try:
        import importlib
        stop = getattr(importlib.import_module("modal.experimental"), "stop_fetching_inputs", None)
    except Exception:
        stop = None
    if stop is not None:
        try:
            stop()
            print(f"[bridge] ⚠ 容器 {_container_id()} 跑完当前单后退役、不再接单: {why}")
            return
        except Exception as e:
            print(f"[bridge] ⚠ stop_fetching_inputs 失败({type(e).__name__}: {e})")
    print(f"[bridge] ⚠ {why} —— 无法让容器退役,下一单开跑前原地重启 ComfyUI")


def _retire_if_broken(self, err: BaseException) -> None:
    """任务失败后判断容器还能不能接着用:CUDA 黏性错误 / ComfyUI 不可达 → 退役。
    **从不抛异常**:调用方正在处理原始异常,这里抛了会把真因盖掉。"""
    try:
        if _STICKY_CUDA.search(str(err)):
            _retire_container(self, "CUDA 黏性错误,同一进程里后续的 CUDA 调用都会失败")
            return
        why = _comfy_unhealthy_reason(self)
        if why:
            _retire_container(self, f"ComfyUI 不可达({why})")
    except Exception as e:
        print(f"[bridge] ⚠ 失败后的容器体检出错(忽略): {type(e).__name__}: {e}")


# 容器指纹:同一容器里所有日志行带同一个值 —— 数不同值 = 数容器;几单共享一个值 = 那几单
# 复用了同一个热容器。没有它时 `modal app logs` 既无时间戳也无容器标记,boot 和 job 对不上,
# 「这单到底是冷是热」只能靠耗时猜(2026-09-20 comfyagent 为一单 55.8s 卡在冷热之间查不下去)。
_CONTAINER_ID = ""


def _new_container_id() -> str:
    """给本容器掷一个新指纹。

    ⚠ 绝不能写成模块级常量,也不能在 boot() 里生成 —— 开内存快照时 `@modal.enter(snap=True)`
    跑在**快照之前**,那个值会被烤进快照,所有从同一份快照恢复出来的容器共享同一个指纹:
    日志看着像热复用,其实是不同容器。**假证据比没有证据更糟。**
    所以只在 snap=False 的 enter 钩子里调用(见 _worker_ensure_alive,且必须在它那句
    `if not _SNAPSHOT: return` 之前 —— 快照关着时那句会直接返回)。"""
    global _CONTAINER_ID
    _CONTAINER_ID = uuid.uuid4().hex[:8]
    return _CONTAINER_ID


def _container_id() -> str:
    """本容器指纹;没掷过就惰性补一个(正常路径不会走到,兜底防止日志里出现空值)。"""
    return _CONTAINER_ID or _new_container_id()


# 实际卡型。worker 声明的是 gpu=list(Modal 按顺序 fallback),job 的 gpu 字段只是候选列表
# (如 "H100→A100-80GB"),不是这单跑在哪张卡上。同 seed 对比、性能归因都要它:sm80 与 sm90 走不同的
# attention kernel,comfyagent 同 seed 两单 PSNR 26.8 dB、卡型查不到,就没法归因(2026-09-28)。
# 与容器指纹同理:只在 snap=False 的钩子里探测,别烤进内存快照(恢复到的可能是另一种卡)。
_GPU_NAME = ""


def _detect_gpu_name() -> str:
    """nvidia-smi 读本容器的显卡型号(如 "NVIDIA H100 80GB HBM3"),CPU 容器 / 探测失败返回 ""。
    同 _gpu_compute_cap,不为一个字符串付 torch 冷 import 的钱。"""
    global _GPU_NAME
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        _GPU_NAME = out[0].strip() if out else ""
    except Exception:
        _GPU_NAME = ""
    return _GPU_NAME


def _gpu_name() -> str:
    return _GPU_NAME or _detect_gpu_name()


def _worker_ensure_alive(self):
    """快照恢复路径上的正确性闸门 + 自愈(GPU/CPU worker 共用):快照关时直接返回;开时探活失败
    就原地重启子进程(退化为一次普通 boot,不比无快照更糟),而非 raise 杀容器进重试循环。

    ⚠ 顺带承担「每容器掷一次指纹」——它是 snap=False 的钩子,是唯一每个真实容器都跑一次的
    入口。那一行必须在下面的 _SNAPSHOT 早返回**之前**,否则关快照时永远不会掷。"""
    # 这行是容器级标记:一个真实容器恰好打一次,比 "ComfyUI ready" 可靠
    # (开快照时恢复的容器根本不 boot ComfyUI,那行不会出现)。
    print(f"[bridge] container {_new_container_id()} up gpu={_detect_gpu_name() or '-'} "
          f"(snapshot={'on' if _SNAPSHOT else 'off'})")
    if not _SNAPSHOT:
        return
    import requests
    try:
        if requests.get("http://127.0.0.1:8188/system_stats", timeout=5).ok:
            return
    except Exception:
        pass
    print("[bridge] restore 探活失败,重启 ComfyUI 子进程(退化为普通冷启)")
    _worker_shutdown(self)
    _worker_boot(self, cpu=getattr(self, "_cpu", False))  # 沿用本 worker 的 CPU/GPU 模式


def _refresh_local_nodes_if_stale(self, expected: dict | None) -> bool:
    """暖容器纠偏:本地节点是运行时从 Volume 解压的,而 @modal.enter 只在容器**启动**时跑一次。
    改完节点重传后若命中暖容器,跑的还是上一版代码 —— 静默出旧结果,是最难查的一类失效。
    提交方带上期望指纹,这里比对容器实际装的那版,不一致就 reload Volume + 重解压 + 重启 ComfyUI。

    ⚠ 只在 expected 非空(即这个 job 真的用了本地节点)时才做:reload 有 IO 开销,重启更是
      把显存里的模型也丢了。常规任务 expected 为空 → 整段跳过,warm 复用完全不受影响。
    返回是否真的重装过。"""
    if not expected:
        return False
    from _local_nodes_boot import BAKED_SENTINEL, extract_all, needs_refresh, node_target, restore_baked
    # 名字先校验再动任何东西:非法名(如 ".")直接让这一单失败,别为一个坏请求白白重启 ComfyUI
    for folder in expected:
        node_target(folder)
    # ⚠ 比的是 ComfyUI **启动时加载的**版本,不是磁盘 marker:纠偏失败后磁盘已是新版、内存里还是旧代码,
    #   比磁盘会判「不过期」,下一单静默跑旧节点(2026-10-05 深度 review)。
    stale = needs_refresh(expected, _LOADED_LOCAL_DIGESTS)
    if not stale:
        return False
    print(f"[bridge] 暖容器的本地节点已过期 {stale} → 停 ComfyUI + reload + 重装 + 重启")
    # ⚠ 先停 ComfyUI 再 reload:Modal 文档写明 Volume 上有打开的文件时 reload 会失败,而 ComfyUI 可能
    #   正持有卷上模型文件的句柄(以前是先 reload 后停,2026-10-05 深度 review)。反正后面要重启它。
    _worker_shutdown(self)
    err = None
    try:
        models_vol.reload()   # 别处 commit 的 Volume 变更,运行中容器必须 reload 才看得到
        extract_all()
        # Volume 里可能仍留有历史覆盖包(清理失败/多机最终一致性),所以先统一解压,
        # 再按本次任务声明把应跑 baked 的目录恢复。纯本地节点无备份时会删除覆盖目录。
        restore_baked([f for f, d in expected.items() if d == BAKED_SENTINEL])
        # ⚠ 复核,别只当提示:extract_all 对坏包 / 解压失败 / marker 写失败都是「打日志继续」,
        #   Volume 上的包也可能压根不是这一版(上传失败/最终一致性没追上)。不复核就会
        #   **静默跑旧节点代码** —— 用户改完节点跑一遍,结果和没改一样,零线索。宁可让任务明确失败。
        #   ComfyUI 此刻停着,下一次启动装的就是磁盘这版,所以这里比磁盘(loaded=None)。
        still = needs_refresh(expected)
        if still:
            err = RuntimeError(
                f"本地节点版本对不上,拒绝用旧代码跑: {still}。"
                f"云端拿到的不是本次提交声明的那版(上传没成功?包损坏?)——请重试提交;"
                f"仍不行就在「管理云端节点」里删掉这些包再跑一次。")
    except Exception as e:
        err = e
    # 无论复核成败都要把 ComfyUI 拉起来:失败时磁盘已被改动,不重启的话「内存里的旧代码」和
    # 「磁盘上的新 marker」分叉;重启后两者一致,_LOADED_LOCAL_DIGESTS 也随之更新,下一单判断才对。
    # 上面已经 reload + 解压过。这里若再让 boot 解压一次,另一台机器恰好上传新包
    # 就会在复核之后偷换版本；解压失败也会被 boot 的冷启动容错吞掉。因此只重启进程。
    try:
        _worker_boot(self, cpu=getattr(self, "_cpu", False), load_local_nodes=False)
    except Exception as boot_err:
        if err is not None:   # 两个错误都要让用户看见:真因是前一个,后一个决定了这个容器还能不能用
            raise RuntimeError(f"{err}(随后重启 ComfyUI 也失败了: {boot_err})") from boot_err
        raise
    if err is not None:
        raise err
    return True


def _claim_pending_call_id(job_id: str) -> bool:
    """`<job_id>:call` 还是占位值时,用本次调用自己的 function call id 补上。返回是否补了。

    run_endpoint 在 spawn 之后、写回真实 call_id 之前被杀(60s 超时 / 容器回收),占位值就永远留着:
    /cancel 一直回「提交中、稍等再点」,而任务照常在跑、在计费(2026-10-05 深度 review)。
    worker 自己的 call id 和 spawn 返回的 call.object_id 是同一个值,和 run_endpoint 并发写也无害。
    只在读到占位值时才写:键不存在(GC 已回收)时写进去会留下一个永远没人清的孤儿键。best-effort。"""
    try:
        if job_state.get(f"{job_id}:call") != _CALL_PENDING:
            return False
        cid = modal.current_function_call_id()
        if not cid:
            return False
        job_state[f"{job_id}:call"] = cid
        print(f"[bridge] job {job_id}: :call 仍是占位(提交方没写回),已用本调用的 {cid} 补上")
        return True
    except Exception as e:
        print(f"[bridge] ⚠ job {job_id}: 补写 call_id 失败(忽略): {e}")
        return False


def _worker_run(workflow: dict, job_id: str, input_images: list | None = None,
                delivery: dict | None = None) -> dict:
    # delivery:结果交付方式(见 aigc_delivery.normalize_delivery)。desktop = 现状(回本地);
    # aigc-r2 = 直传 R2 + 回调 AIGC Studio。⚠ delivery 里的 token 是敏感的:不进 job_state、不打日志。
    # call_id 现在存独立 key(见 run_endpoint);等它出现仅为让 cancel 可用,等不到也继续。
    # ⚠ 等的是**真实** call_id:run_endpoint 会先写占位值,认占位就等于没等。
    # 占位值说明 run_endpoint 还没写回(或已经死了写不回了)→ 自己补上,不必干等(见 _claim_pending_call_id)。
    for _ in range(50):  # 最多 ~5s
        if _call_id(job_id) or _claim_pending_call_id(job_id):
            break
        time.sleep(0.1)
    mode = (delivery or {}).get("mode", "desktop")
    # 归属标记:container= 与上面那行 "container <id> up" 对得上就能确定这单跑在哪个容器,
    # 从而直接读出冷/热。call= 是 Modal 的 function call id,把日志行连回提交方的句柄
    # (我们自己存在 job_state 的 "<job_id>:call" 里),便于两边对账。
    gpu_actual = _gpu_name()
    print(f"[bridge] job {job_id} start container={_container_id()} gpu={gpu_actual or '-'} "
          f"call={modal.current_function_call_id() or '?'}")
    cur = job_state.get(job_id) or {}
    if cur.get("status") in ("cancelled", "failed"):
        # cancelled:用户在 /run 之后、worker 起跑之前就取消了(上面还等了最多 5s 的 call_id)。
        # failed:排队太久已被判死(_stale_reason),客户端已被告知失败、停止轮询 —— 再跑就是白付钱、
        #   产物也没人取。本 app 没配自动重试,run() 里写 failed 的路径又是写完就抛、进不到这里,
        #   所以起跑时看到 failed 只可能是被判死的。
        # 别写 running 把它覆盖掉,也别开跑烧 GPU。
        print(f"[bridge] job {job_id}: 起跑前已是 {cur.get('status')},不执行")
        return {"skipped": cur.get("status")}
    from _comfy_ws import interrupt_comfy     # 放在 try 外:下面两个异常分支都要用
    # ⚠ try 必须从写 running **之前**开始:取消信号(SIGUSR1 → InputCancellation)可以落在主线程的任意一行。
    #   以前 try 从写完 running、删完 :progress 之后才开始,信号恰好落在这两次 Dict RPC 上时,
    #   BaseException 分支接不到它,终态停在 running,22 分钟后被 _stale_reason 误报成「被 Modal 强杀、
    #   已计费」(2026-10-05 深度 review 复现)。下面的分支对「还没写 running」也是对的:取消分支只在
    #   非终态时落 cancelled,失败分支照常落 failed。
    try:
        job_state[job_id] = {**cur, "status": "running", "started_at": time.time(),
                             "timeout_s": WORKER_TIMEOUT,   # 按任务记超时,见 _stale_reason
                             # 实际卡型(gpu 字段只是候选列表,见 _GPU_NAME)。之后的写入都是 {**现有记录} 合并,会一路带到终态
                             **({"gpu_actual": gpu_actual} if gpu_actual else {})}
        try:
            del job_state[f"{job_id}:progress"]   # 同 id 被回收后再提交时,别显示上一次的进度
        except Exception:
            pass
        # ⚠ 不在这里 free/reload!曾经"每 job 跑前 free+reload"会把 warm 容器显存里的模型卸掉,
        # 导致每个 job 都得重新从 Volume 加载 flux2(~163s),彻底毁掉 warm 复用。
        # 正确策略:正常直接跑(模型在显存,秒级);只有验证失败(模型不在列表)时,queue_workflow
        # 内部才按需 free→reload→重试(只有删 Volume/缺模型的极端场景才付这个代价)。
        from _comfy_ws import run_workflow

        # 进度上报:ComfyUI 每步推一次 → 算 s/it 写进 job_state.progress,poll 全量透传到前端。
        # 前端据此做「投影式慢速预警」(按当前速度是否会撞 worker 超时),替代老的
        # 按耗时比例预警 —— 后者对 0.9MP 这类合法长任务必然误报(健康任务本来就超 75% 线)。
        # s/it 的参考点取首个事件(首步含 Triton/sage JIT ~70s,差分计算天然把它排除在外)。
        # 每步最多写一次 modal.Dict(20~50 次/任务),开销可忽略;写失败静默,不碰任务本体。
        _t0 = time.time()
        # s/it 用「最近 ≤5 个步间隔的中位数」而非累计平均:冷容器第 2 步仍带 JIT 残余,
        # 累计平均会在早期高估一倍(实测第 3 步评估出 ~48 而稳态 24.6),触发误报;
        # 中位数滚动几步后热身样本自然被洗出窗口。
        _prog = {"v": 0, "m": 0, "t_last": 0.0, "win": []}
        def _on_progress(v: int, m: int) -> None:
            now = time.time()
            if m <= 1 or v <= 0:
                return
            # 新一段进度条 → 全部重置。判据有两个:总步数变了(换了节点),或者步数回退了。
            # ⚠ 只看总步数的话,两段步数相同的采样(base + refiner、两段式视频采样)第二段永远进不来 ——
            #   v 回到 1 后一直 <= 上一段末尾的 v,被下面那句当成重复事件丢掉,:progress 冻结在第一段末尾,
            #   前端的投影式超时预警整段失效(2026-10-05 深度 review 复现)。
            if m != _prog["m"] or v < _prog["v"]:
                _prog.update(m=m, v=v, t_last=now, win=[])
                return
            if v == _prog["v"]:
                return
            itv = (now - _prog["t_last"]) / (v - _prog["v"])
            _prog.update(v=v, t_last=now)
            _prog["win"] = (_prog["win"] + [itv])[-5:]
            w = sorted(_prog["win"])
            s_it = w[len(w) // 2]
            # ⚠ 写独立的 :progress 键,**绝不能读改写整条状态**:那样和 cancel_endpoint 竞态 ——
            #   cancel() 返回后中断信号要异步才到 worker,这个窗口里 worker 读到旧的 running、
            #   cancel 写入 cancelled、worker 再把带 progress 的 running 写回去,取消就被覆盖了,
            #   之后 /status 永远是 running。run_endpoint 那侧的 :call 当年是同一个理由拆出去的。
            try:
                job_state[f"{job_id}:progress"] = {
                    "step": v, "total": m,
                    "s_it": round(s_it, 2),
                    "n_samples": len(w),      # 前端据此决定预警可信度
                    "elapsed": int(now - _t0),
                }
            except Exception:
                pass

        # aigc-r2:只「发现」产物不读进内存(materialize=False),下面流式直传 R2。
        result = run_workflow(workflow=workflow, job_id=job_id, input_images=input_images,
                              materialize=(mode != "aigc-r2"), on_progress=_on_progress)
        if mode == "aigc-r2":
            # 状态机:running → delivering → completed。出图后直传 R2 + 回调 AIGC Studio;
            # 全部上传且回调成功才 completed。回调失败但文件已在 R2 → delivery.status =
            # callback_failed + 保留 manifest,AIGC Studio 轮询 /status 兜底落库(计划 §7)。
            job_state[job_id] = {**job_state.get(job_id, {}), "status": "delivering"}
            from aigc_delivery import deliver_outputs
            dres = deliver_outputs(
                job_id=job_id, output_refs=result.get("output_refs") or [], delivery=delivery,
                provider_job_id=_call_id(job_id))
            # manifest 只有 r2_key/etag/size 等元数据(无 base64、无 token),job_state 不膨胀。
            job_state[job_id] = {**job_state.get(job_id, {}), "status": "completed",
                                 "delivery": {"mode": "aigc-r2", **dres},
                                 "completed_at": time.time()}
            return {"delivered": dres["status"], "assets": len(dres["assets"])}
    except Exception as e:
        interrupt_comfy()   # 失败时 prompt 可能还在 ComfyUI 里跑,别让它接着烧 GPU
        import traceback
        tb = traceback.format_exc()
        msg = str(e)
        # 「节点不存在」时补一句真因:ComfyUI 对"包 import 失败"和"包根本没装"给的是同一句
        # "The custom node may not be installed",而两者的处理办法完全相反 ——
        # 前者要修那个包的依赖,后者才是去同步节点。分不清的话用户只会反复点同步。
        if "not found" in msg or "missing_node_type" in msg:
            m = re.search(r"class_type[\"':\s]+([A-Za-z0-9_.\-]+)", msg)
            hint = import_failure_hint(m.group(1) if m else "")
            if hint:
                msg = f"{msg}\n\n{hint}"
                print(f"[bridge] {hint}")
        job_state[job_id] = {**job_state.get(job_id, {}), "status": "failed",
                             "error": msg, "trace": tb[-2000:], "completed_at": time.time()}
        raise
    except BaseException as e:
        # 取消走的是 Modal 的 InputCancellation —— 它是 BaseException,**不进上面那支**。
        # 以前这里什么都不做,ComfyUI 子进程就接着跑被取消的 prompt(见 interrupt_comfy)。
        interrupt_comfy()
        if type(e).__name__ == "InputCancellation":
            # ⚠ 终态必须在这里再落定一次。cancel_endpoint 写的 cancelled 会被上面 running /
            #   delivering 那两处读改写覆盖(它们读的是取消送达之前的旧值),之后再没人写终态 ——
            #   状态停在 running,22 分钟后被 _stale_reason 误报成「被 Modal 强杀、已计费」,
            #   而那是用户自己取消的任务(2026-09-24 review)。写的是同一个值,与 cancel_endpoint
            #   不冲突;已是终态(worker 抢先写完)就不动。
            try:
                cur = job_state.get(job_id) or {}
                if cur.get("status") not in ("completed", "failed", "cancelled"):
                    job_state[job_id] = {**cur, "status": "cancelled", "completed_at": time.time()}
            except Exception:
                pass
        raise
    # 大文件走了 Volume(item 带 volume_path)→ commit 一次,本地 SDK 才看得到刚写进 _outputs 的文件
    if any(i.get("volume_path") for i in (result.get("images") or [])):
        try:
            models_vol.commit()
        except Exception as e:
            # ⚠ commit 失败 = 本地 SDK 根本看不到刚写进 _outputs 的文件。以前这里只
            # print 一句就继续往下写 completed,用户看到"成功"却怎么也取不回产物,
            # 而且没有任何线索指向真实原因。产物取不到就是失败,如实记。
            import traceback
            job_state[job_id] = {**job_state.get(job_id, {}), "status": "failed",
                                 "error": f"volume commit 失败,产物无法取回: {e}",
                                 "trace": traceback.format_exc()[-2000:],
                                 "completed_at": time.time()}
            print(f"[bridge] volume commit 失败: {e}")
            raise
    # 有 images(多图)就只存 images,不再冗余存 data_base64/filename(那是 images[0] 的重复,
    # 白白让 job_state 体积翻倍);只有极老回退路径(没 images)才退回单图字段。
    done = {**job_state.get(job_id, {}), "status": "completed",
            "image_url": result.get("image_url"), "completed_at": time.time()}
    # 非致命告警也要留痕:走到这里说明产物齐了(数量对不上会在 _comfy_ws 里抛),
    # 但过程中可能有过重试/跳过之类的信息。以前 result["errors"] 直接丢掉,
    # 出了问题事后完全无从追溯。
    if result.get("errors"):
        done["warnings"] = result["errors"][:20]
    if result.get("images"):
        done["images"] = result["images"]
    else:
        done["data_base64"] = result.get("data_base64")
        done["filename"] = result.get("filename")
    try:
        job_state[job_id] = done
    except Exception as e:
        # ⚠ 这句以前在 try 外:Dict 超限(RequestSizeError)或瞬时报错时异常直接冒出去,
        #   没人写终态,状态卡在 running —— GPU 钱花了、产物只在容器里,谁也拿不到。
        #   _comfy_ws 的内联总量预算会让超限基本不发生,这里是最后一道:至少如实落一个 failed。
        why = f"产物已生成,但结果写回失败: {type(e).__name__}: {e}"
        print(f"[bridge] ⚠ job {job_id}: {why}")
        try:
            job_state[job_id] = {**{k: v for k, v in done.items()
                                    if k not in ("images", "data_base64")},
                                 "status": "failed", "error": why}
        except Exception:
            pass
        raise
    return result


def _worker_entry(self, workflow: dict, job_id: str, input_images: list | None,
                  delivery: dict | None, local_nodes: dict | None) -> dict:
    """四个 worker 类的 run() 共用的入口:探活 → 本地节点纠偏 → 跑任务 → 失败后决定容器去留。"""
    # 尽早补上占位的 :call(见 _claim_pending_call_id):下面的探活 / 纠偏可能要重启 ComfyUI、花上几分钟,
    # 这段时间里 /cancel 也得拿得到句柄。
    _claim_pending_call_id(job_id)
    try:
        # 子进程死了 / CUDA 坏了 / 上一单标记要重启 → 原地重启,别让这一单秒失败(见 _STICKY_CUDA 上方注释)
        _ensure_comfy_healthy(self)
        # 暖容器可能装着上一版自写节点。刷不到期望版本会抛错 —— 必须写进 job_state,
        # 否则前端只看到 queued 一直转(spawn 的异常传不回轮询侧)。
        _refresh_local_nodes_if_stale(self, local_nodes)
    except Exception as e:
        cur = job_state.get(job_id) or {}
        # 终态优先:用户在这之前已经取消 / 已被判死的,别拿「重启失败」去覆盖真实结局
        if cur.get("status") not in ("completed", "failed", "cancelled"):
            job_state[job_id] = {**cur, "status": "failed",
                                 "error": str(e), "completed_at": time.time()}
        _retire_if_broken(self, e)
        raise
    try:
        return _worker_run(workflow, job_id, input_images, delivery)
    except Exception as e:
        _retire_if_broken(self, e)
        raise


# GPU 在部署时由 config.default_gpu 决定(deploy_env 传 MODAL_BRIDGE_DEFAULT_GPU)。
# ⚠ Modal 的 gpu 是部署时固定的,运行时不可变 —— 换显卡需重新部署。
# 每档带 Modal 原生 fallback(排不到主卡自动降级到链里下一个)。
_PRIMARY_GPU = os.environ.get("MODAL_BRIDGE_DEFAULT_GPU", "H100")
_GPU_CHAIN = {
    "B200":      ["B200", "H200", "H100"], # 180G Blackwell 最强档,排不到降 H200/H100
    "H100":      ["H100", "A100-80GB"],   # 主卡排不到降 A100-80G
    "H200":      ["H200", "H100"],         # 141G 大卡,降级到 H100
    "A100-80GB": ["A100-80GB"],
    "L40S":      ["L40S"],                  # 选 L40S 是为省钱,不 fallback 到贵卡
}
_GPU_LIST = _GPU_CHAIN.get(_PRIMARY_GPU, [_PRIMARY_GPU])

# 省钱档 GPU(默认 L40S)。估算显存放得下的工作流自动降到这张卡跑(路由见 run_endpoint)。
# 与主卡相同(用户把 default 设成 L40S 之类)则不启用,所有 GPU 任务仍走主 worker。
_CHEAP_GPU = os.environ.get("MODAL_BRIDGE_CHEAP_GPU", "L40S")
_CHEAP_GPU_LIST = _GPU_CHAIN.get(_CHEAP_GPU, [_CHEAP_GPU])
_CHEAP_ENABLED = _CHEAP_GPU != _PRIMARY_GPU

# 顶配档 GPU(默认 B200 180G):估算显存超过主卡的工作流升到这跑,防 OOM(升档 = 正确性兜底)。
# B200 是 Blackwell 最强档,显存最大、速度最快,大图自动上这张。
# ⚠ 升档档不向下 fallback(对 >80G 的活退到小卡 = OOM),所以固定单卡列表,宁可排队等也不降级。
_TOP_GPU = os.environ.get("MODAL_BRIDGE_TOP_GPU", "B200")
_TOP_GPU_LIST = [_TOP_GPU]
_TOP_ENABLED = _TOP_GPU != _PRIMARY_GPU

# 开快照时追加两个 decorator 参数;关时为空 dict → @app.cls 行为和原来完全一致。
_SNAP_KW = (dict(enable_memory_snapshot=True,
                 experimental_options={"enable_gpu_snapshot": True}) if _SNAPSHOT else {})


@app.cls(gpu=_GPU_LIST, **_WORKER_KW, **_SNAP_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorker:
    @modal.enter(snap=_SNAPSHOT)   # 开快照:这一段进快照(snap 阶段 GPU 可见,正常 boot)
    def boot(self):
        _worker_boot(self)

    @modal.enter(snap=False)       # 恢复路径上的正确性闸门 + 自愈(见 _worker_ensure_alive)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None, local_nodes: dict | None = None) -> dict:
        return _worker_entry(self, workflow, job_id, input_images, delivery, local_nodes)


# 省钱档 worker:估算显存放得下便宜卡(默认 L40S)且非视频的 GPU 工作流路由到这。
# 与主 worker 完全同构,只是 gpu 不同;min_containers=0 → 不被路由到时 0 容器 = $0,定义在这儿不花钱。
@app.cls(gpu=_CHEAP_GPU_LIST, **_WORKER_KW, **_SNAP_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorkerCheap:
    @modal.enter(snap=_SNAPSHOT)
    def boot(self):
        _worker_boot(self)

    @modal.enter(snap=False)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None, local_nodes: dict | None = None) -> dict:
        return _worker_entry(self, workflow, job_id, input_images, delivery, local_nodes)


# 顶配档 worker:估算显存超过主卡的工作流(如 >80G)升到这(默认 B200 180G),防 OOM。
# 同构,只是 gpu 不同且不向下 fallback;min_containers=0 → 不被路由时 0 容器 = $0。
@app.cls(gpu=_TOP_GPU_LIST, **_WORKER_KW, **_SNAP_KW)
@modal.concurrent(max_inputs=1)
class ComfyWorkerTop:
    @modal.enter(snap=_SNAPSHOT)
    def boot(self):
        _worker_boot(self)

    @modal.enter(snap=False)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None, local_nodes: dict | None = None) -> dict:
        return _worker_entry(self, workflow, job_id, input_images, delivery, local_nodes)


# CPU-only worker:无 GPU 需求的工作流(纯 API / 无本地模型节点)走这,GPU 账单≈0。
# 同镜像、无 gpu;CPU 内存快照是 GA(不实验、不需要 gpu_snapshot),所以只 enable_memory_snapshot。
_SNAP_KW_CPU = (dict(enable_memory_snapshot=True) if _SNAPSHOT else {})


@app.cls(**_WORKER_KW, **_SNAP_KW_CPU)
@modal.concurrent(max_inputs=1)
class ComfyWorkerCPU:
    @modal.enter(snap=_SNAPSHOT)
    def boot(self):
        _worker_boot(self, cpu=True)   # 无 GPU 容器 → 强制 ComfyUI CPU 模式

    @modal.enter(snap=False)
    def ensure_comfy_alive(self):
        _worker_ensure_alive(self)

    @modal.exit()
    def shutdown(self):
        _worker_shutdown(self)

    @modal.method()
    def run(self, workflow: dict, job_id: str, input_images: list | None = None,
            delivery: dict | None = None, local_nodes: dict | None = None) -> dict:
        return _worker_entry(self, workflow, job_id, input_images, delivery, local_nodes)


# tier 入参保留兼容(前端仍可能传 80g/40g),但 GPU 由部署时的 default_gpu 决定,不再按 tier 分档。
_TIER_WORKERS = {"80g": ComfyWorker, "40g": ComfyWorker}
_GPU_DISPLAY = "→".join(_GPU_LIST)  # 如 "H100→A100-80GB",进度卡/日志显示真实显卡
_TIER_GPU_DISPLAY = {"80g": _GPU_DISPLAY, "40g": _GPU_DISPLAY}
_CHEAP_GPU_DISPLAY = "→".join(_CHEAP_GPU_LIST)  # 省钱档显示(如 "L40S")
_TOP_GPU_DISPLAY = "→".join(_TOP_GPU_LIST)        # 顶配档显示(如 "B200")


# ============================================================================
# 鉴权 — 自建 API key(private endpoint)
# 不用 Modal 的 requires_proxy_auth(要单独 Proxy Auth Token,没法程序化)。改成:
# 部署时随机生成 BRIDGE_API_KEY 存进 Secret + 本地 config,每个 endpoint 校验。
# key 传入方式(按优先级):
#   - GET :  X-Bridge-Key 请求头  →  回退 ?key=(旧客户端兼容)
#   - POST:  body 的 auth_key
# ⚠ query string 会落进反代 / CDN / 浏览器历史 / Referer,是长期暴露面,新客户端一律走 header;
#   ?key= 仅为兼容保留,等旧版插件都升上来后可以摘掉。
# 拒绝时函数体内 import fastapi 返 401。
# ============================================================================

# fastapi 只在容器镜像里有:部署解析期跑的是 ComfyUI 内嵌解释器,那里装不了 fastapi,
# 所以顶层 `from fastapi import Header` 会让 modal deploy 直接炸。
# 而 Modal 在容器内会**重新 import 本模块**(MODAL_BRIDGE_* 那些环境变量也是靠这一点生效的),
# 届时拿到的就是真的 Header,FastAPI 正常按请求头解析。
# 部署期退化成"返回默认值的普通函数",签名变成 x_bridge_key: str = "",合法且不影响 app 解析。
try:
    from fastapi import Header as _Header
except ImportError:  # 部署解析期
    def _Header(default=None, **_kw):
        return default


def _check(key: str):
    expected = os.environ.get("BRIDGE_API_KEY", "")
    # 恒时比较:`==` 在第一个不等的字节就短路,响应时间随「已匹配前缀长度」变化,
    # 理论上可被逐字节爆破出 key。走 bytes 是为了不受非 ASCII 输入影响
    # (compare_digest 对非 ASCII 的 str 会抛 TypeError)。
    if expected and hmac.compare_digest((key or "").encode("utf-8"),
                                        expected.encode("utf-8")):
        return None
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "unauthorized — bad or missing bridge key"}, status_code=401)


# ============================================================================
# REST endpoints(4 个)
# ============================================================================

# ⚠ 这里挂 Volume 不是为了读写挂载点,是为了让 models_vol 在这个容器里被 hydrate ——
# _sweep_job_state 要调 models_vol.remove_file() 清 _outputs/,没被引用的全局 Volume 对象
# 在容器内可能未初始化,那样 GC 会永远静默失败(而日志上看不出来)。挂载本身是 lazy 的,
# 不读文件就没有实际开销。
@app.function(image=cuda_image, secrets=[bridge_secret], timeout=60,
              volumes={"/comfy-volume": models_vol})
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-run")
def run_endpoint(payload: dict):
    """提交 workflow。payload: {workflow, tier?, images?, auth_key, delivery?}
    delivery(可选):{"mode":"desktop"}(缺省,结果回本地)或
    {"mode":"aigc-r2","job_id":"…","token":"…"}(结果直传 R2 + 回调 AIGC Studio)。"""
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    delivery, derr = normalize_delivery(payload)
    if derr:
        return {"error": derr}
    # aigc-r2 用 AIGC Studio 的任务 UUID 作 job_id,双方同一 id 查 /status;desktop 沿用旧逻辑。
    job_id = (delivery.get("job_id") if delivery.get("mode") == "aigc-r2" else None) \
        or payload.get("job_id") or str(uuid.uuid4())
    # 先消毒再用:下面所有分支(job_state key / spawn 参数 / Volume 路径)都吃这个值。
    if not _safe_job_id(job_id):
        return {"error": f"invalid job_id: {str(job_id)[:64]!r} "
                         f"(只允许 [A-Za-z0-9_.-],最长 64)"}
    workflow = payload.get("workflow")
    if not workflow:
        return {"error": "Missing 'workflow' in payload"}
    input_images = payload.get("images")
    # 路由:工作流无本地模型节点(纯 API / 轻节点)= 不需要 GPU → CPU worker(账单≈0);否则 GPU worker。
    # needs_gpu 由后端 /submit 据 extract_required_models 判定后传入(缺省 True,稳妥)。
    # 四档路由(成本从低到高):无 GPU → CPU;放得下便宜卡 → cheap(L40S);超过主卡 → top(B200);否则 → 主卡。
    # gpu_class 由后端 /submit 据 estimate_vram 判定后传入(缺省 primary,稳妥)。
    needs_gpu = bool(payload.get("needs_gpu", True))
    gpu_class = (payload.get("gpu_class") or "primary").lower()
    if not needs_gpu:
        worker = ComfyWorkerCPU
        gpu_display = "CPU"
        tier = "cpu"
    elif gpu_class == "cheap" and _CHEAP_ENABLED:
        worker = ComfyWorkerCheap
        gpu_display = _CHEAP_GPU_DISPLAY
        tier = "cheap"
    elif gpu_class == "top" and _TOP_ENABLED:
        worker = ComfyWorkerTop
        gpu_display = _TOP_GPU_DISPLAY
        tier = "top"
    else:
        tier = (payload.get("tier") or "40g").lower()
        if tier not in _TIER_WORKERS:
            tier = "40g"
        worker = _TIER_WORKERS[tier]
        gpu_display = _TIER_GPU_DISPLAY[tier]

    # 幂等:客户端带自己的 job_id 重试时(/run 对 502/504/超时会重试),这个 id 可能已经
    # spawn 过了 —— 响应丢在网关不代表任务没跑。
    # **契约:job_id 一次性,永不复用** —— 重跑就用新 id 提交。以前有 rerun=1 复用终态 id,但它要删旧句柄、
    # 覆盖记录,和判死任务的原调用、并发的 rerun、GC 各有一个竞态窗口(多轮 review 反复抓到),
    # 而没有任何调用方用过它(2026-09-27 删除;老客户端传了 rerun 也只会拿到 duplicate)。
    # ⚠ 记录被 GC 回收(终态 1 小时后)之后再用同一个 id 提交,这里拦不住(已无记录可查),会被当成新任务;
    #   但另一个容器里拿着旧快照的 GC 可能随后把**新**记录删掉(check-then-delete,Dict 没有原子的
    #   比较后删除)。所以复用 id 不受支持。已知调用方都每次新生成:插件 / CLI / MCP 用 uuid4,
    #   AIGC Studio 用任务行的 gen_random_uuid()。
    prior = job_state.get(job_id)
    if isinstance(prior, dict):
        prior = _effective(prior, time.time())   # 死任务按 failed 回,而不是 queued / running
    prior_status = prior.get("status") if isinstance(prior, dict) else None
    # 非终态:再 spawn 就是第二个同样的 GPU 任务,双跑双计费,而调用方只看得到后一个。
    # 终态:同样回现状 —— 客户端 60s 超时窗内任务完全可能已经跑完(冷启才 ~23s),放行就是静默重跑,
    #   白付一次 GPU 钱还覆盖掉第一次的产物。
    if prior_status:
        print(f"[bridge] /run duplicate job_id {job_id} (status={prior_status}) — 不重复 spawn")
        # ⚠ 只回字段子集,别 **prior:终态条目里带着 images(小产物是 base64),
        # 整个塞进 /run 的响应等于把一份产物白传一遍。要完整状态走 /status。
        return {"id": job_id, "status": prior_status,
                "gpu": prior.get("gpu") or gpu_display, "duplicate": True}

    _sweep_job_state()  # 顺手清理过期/超量的旧 job(防 Dict 无限膨胀)
    # ⚠ 先原子占位 :call,再写 job_state —— 两点都不能反过来:
    # 1) 原子:get→put 之间有窗口,两个并发的同 id 请求会各读到"不存在"、各 spawn 一次。
    #    put(skip_if_exists=True) 返回 False 就是"别人抢先占了",直接回现状。
    # 2) 顺序:cancel 靠 :call 存在与否判断"是否正在提交中"。先写 job_state 的话,
    #    中间窗口里 cancel 看到 status=queued 却查不到 :call,会直接标 cancelled 返回成功,
    #    而函数下一毫秒就开始跑 —— 谎报成功,违反本文件的 cancel 铁律。
    if not job_state.put(f"{job_id}:call", _CALL_PENDING, skip_if_exists=True):
        cur = job_state.get(job_id) if isinstance(job_state.get(job_id), dict) else {}
        print(f"[bridge] /run 并发同 job_id {job_id} — 已有请求在提交中,不重复 spawn")
        return {"id": job_id, "status": cur.get("status") or "queued",
                "gpu": cur.get("gpu") or gpu_display, "duplicate": True}
    # job_state 只存 delivery 的可外泄形态(mode/job_id),token 绝不落 Dict/日志。
    job_state[job_id] = {"status": "queued", "queued_at": time.time(), "gpu": gpu_display,
                         "tier": tier, "delivery": public_delivery(delivery)}
    # local_nodes: {folder: digest} —— 本次工作流用到的自写节点及其期望版本。
    # 暖容器可能装着上一版,worker 侧据此判断要不要 reload+重装+重启(见 _refresh_local_nodes_if_stale)。
    local_nodes = payload.get("local_nodes") if isinstance(payload.get("local_nodes"), dict) else None
    try:
        call = worker().run.spawn(workflow, job_id, input_images, delivery, local_nodes)
    except Exception:
        try:
            del job_state[f"{job_id}:call"]   # 占位不能留成孤儿,否则 cancel 会一直等
        except Exception:
            pass
        job_state[job_id] = {**(job_state.get(job_id) or {}), "status": "failed",
                             "error": "spawn failed", "completed_at": time.time()}
        raise
    # ⚠ call_id 存到独立 key,run_endpoint 不再回写 job_state[job_id]。
    # 原因:job_state[job_id] 同时被 worker 容器写(running/failed/completed)。Modal Dict 跨容器
    # 最终一致、无序,run_endpoint spawn 后 merge 回写可能读到 stale 的 queued、把 worker 刚写的
    # 终态冲掉 → 前端永远 poll 到 queued、卡片一直转。分离 key 后两边各写各的,彻底无竞态。
    job_state[f"{job_id}:call"] = call.object_id
    return {"id": job_id, "status": "queued", "gpu": gpu_display}


@app.function(image=cuda_image, secrets=[bridge_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-status")
def status_endpoint(job_id: str, key: str = "", x_bridge_key: str = _Header("")):
    deny = _check(x_bridge_key or key)
    if deny:
        return deny
    # 先校验 job_id:"<id>:call" / "<id>:progress" 这类独立键不是任务记录。只挡非 dict 不够 ——
    # :progress 的值就是 dict,查它会拿到一条没有 status 的记录,正是客户端会兜底成 running 的形态。
    s = job_state.get(job_id) if _safe_job_id(job_id) else None
    if not isinstance(s, dict) or not s:
        # 非 dict:有人拿 "<id>:call" 这类独立键当 job_id 来查 —— 它不是任务记录,当查无此 job,
        # 别让下面的 {**s} 对字符串展开抛 500。
        # ⚠ 必须带 status 字段。只回 {"error": ...} 的话,客户端归一状态时会落进「未知 → 兜底」,
        #   而最保守的兜底恰好是 running(猜 completed 等于假装有产物,猜 failed 等于把还在
        #   烧钱的任务当结束)—— 于是一条被 GC 清掉的任务被读成「还在跑」,永远不收敛。
        #   本仓自己的 bridge_client.wait 也中招过:它只认 completed/failed/cancelled,
        #   缺 status 就一路轮询到 timeout_s(默认 1 小时)。让「没有这条记录」在数据里显式存在。
        #   (2026-09-10 comfyagent 侧实测:normalize_status(None) → running)
        # ⚠ 刻意不改成 HTTP 404:下游已有 vendor 副本,改状态码是破坏性变更;而且 wait() 把
        #   HTTP 异常当瞬态错重试,反而更难收敛。
        # ⚠ not_found ≠ 立刻可判死:job_state 是 modal.Dict,跨容器最终一致,/run 刚写完的
        #   条目在另一个容器上可能短暂读不到。客户端必须连续看到几次才作数(见 bridge_client.wait)。
        return {"error": "job not found", "id": job_id, "status": "not_found"}
    now = time.time()
    eff = _effective(s, now)
    if eff is not s:
        # worker 必然已死 / 从没起来(见 _stale_reason),没有竞态可言 —— 落成 failed,
        # 调用方拿到真因,而不是空等到自己的超时再报一个错的原因。
        s = eff
        try:
            job_state[job_id] = s
        except Exception:
            pass
    out = {"id": job_id, **s}
    if s.get("status") == "running":
        prog = job_state.get(f"{job_id}:progress")   # 进度在独立键里,见 _worker_run._on_progress
        if isinstance(prog, dict):
            out["progress"] = prog
    return out


@app.function(image=cuda_image, volumes={"/comfy-volume": models_vol},
              secrets=[bridge_secret], timeout=300)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-fetch")
def fetch_endpoint(job_id: str, path: str, key: str = "", delete: int = 0, ack: int = 0,
                   x_bridge_key: str = _Header("")):
    """独立客户端(bridge_client / CLI / cloud 模式 MCP)取大文件:流式返回 Volume 上该 job 的
    产物。本地插件不用它(routes 走 modal SDK 直连);它的存在让外部消费者只凭 bridge_key 就能
    拿到走了 Volume 的视频/网格,不必持有 modal token。
    path 必须是该 job 某个 images[].volume_path(囚笼:仅限 _outputs/<job_id>/ 内,拒绝逃逸);
    ack=1 → **不传文件**,直接删掉该产物并 commit。客户端必须在**确认已完整收到并落盘之后**才调。
    delete=1 → 已停用(见下),保留参数只为兼容老客户端。"""
    deny = _check(x_bridge_key or key)
    if deny:
        return deny
    from fastapi.responses import JSONResponse, FileResponse
    prefix = f"_outputs/{job_id}/"
    if not path.startswith(prefix) or ".." in path or path != os.path.normpath(path):
        return JSONResponse({"error": "path out of job scope"}, status_code=403)
    models_vol.reload()  # worker 完成时 commit 过;reload 确保本容器看得到最新文件
    local = Path("/comfy-volume") / path
    if ack:
        # 客户端已确认完整落盘 → 这时才删。已经不在了也算成功(幂等:重试的 ack 不该报错)。
        try:
            if local.is_file():
                os.remove(local)
                models_vol.commit()
                return {"deleted": path}
            return {"deleted": path, "already_gone": True}
        except Exception as e:
            return JSONResponse({"error": f"delete failed: {e}"}, status_code=500)
    if not local.is_file():
        return JSONResponse({"error": f"not found: {path}"}, status_code=404)
    # ⚠ 下载**永不**删除。以前 delete=1 会在响应交给 Modal ingress 之后就在 BackgroundTask 里删,
    #   不等客户端确认收完 —— 弱网断线、或客户端发现大小对不上,Volume 副本已经没了、客户端的
    #   .part 也被清掉,付过钱的产物彻底丢失(2026-09-23 review)。现在删除只走上面的 ack。
    #   老客户端仍会传 delete=1:对它们等于不删,由 _sweep_job_state 按 TTL 回收 —— 安全的一边。
    return FileResponse(str(local), filename=Path(path).name)


def _cancel_noop_view(job_id: str, rec: dict, was_running: bool) -> dict:
    """cancel_noop(任务已先一步结束)的响应:只回字段子集。

    以前回 {**记录}:completed 记录里带着整份 base64 产物(实测 8 MB),取消一个刚好跑完的任务,
    响应就把产物白传一遍;要完整结果走 /status(2026-10-05 深度 review)。"""
    out = {"id": job_id}
    for k in ("status", "error", "gpu", "gpu_actual", "completed_at"):
        if k in rec:
            out[k] = rec[k]
    out.update(cancel_noop=True, was_running=was_running)
    return out


@app.function(image=cuda_image, secrets=[bridge_secret], timeout=15)
@modal.fastapi_endpoint(method="POST", label=f"{APP_NAME}-cancel")
def cancel_endpoint(payload: dict):
    deny = _check(payload.get("auth_key", ""))
    if deny:
        return deny
    job_id = payload.get("job_id")
    if not job_id:
        return {"error": "Missing 'job_id'"}
    # 和 /status 同一道闸:"<id>:progress" / "<id>:call" 这类独立键不是任务记录。以前不校验,
    # "abc:progress" 的值是个 dict,被当成任务记录、回一个假的 cancelled(还把进度键改写了);
    # "abc:call" 的值是字符串,{**s} 直接 500(2026-10-05 深度 review)。不合法的 id 不可能有任务,回 not_found。
    if not _safe_job_id(job_id):
        return {"id": str(job_id)[:64], "status": "not_found", "error": "job not found"}
    s = job_state.get(job_id) or {}
    # ⚠ 不存在的 job 必须在这里就如实报,不能往下走:底下那句 merge 会**凭空建一条 cancelled
    #   记录**并返回 status: cancelled —— 对调用方是一次假成功(取消了一个根本不存在的任务),
    #   对我们是一条垃圾索引,而且之后 status 查它会回 cancelled 而不是 not found。
    #   (2026-09-10 comfyagent 侧读代码发现)
    # ⚠ 判据必须用**裸 :call 键**,不能用 _call_id():run_endpoint 在 spawn 之前先写占位值,
    #   那段窗口里 job_state[job_id] 还不存在、_call_id() 也返回空串 —— 拿 _call_id() 当判据
    #   会把「正在提交中」误判成「不存在」,正好打掉下面那段专为它写的等待逻辑。
    if not s and not job_state.get(f"{job_id}:call"):
        # ⚠ 一次读不算数:job_state 是 modal.Dict,跨容器最终一致 —— /run 刚在另一个容器里
        #   写完,这里可能还读不到。凭一次读就回 not_found 且**不执行取消**,调用方会据此放弃,
        #   任务照常跑满、照常计费。所以先短暂重读,两个键都始终没有才回 not_found。
        #   这是根因修复:此前只在前端(一个调用方)补了确认,bridge_cli cancel / MCP
        #   cancel_job 直调云端时拿到的仍是单次读的结果(2026-09-23 review 抓到)。
        #   与上面等 :call 占位是同一个套路;status_endpoint 不这么做,因为它被反复轮询、
        #   客户端自带连续确认,而 cancel 是一次性的决定。
        deadline = time.time() + _CANCEL_NOT_FOUND_WAIT_S
        while time.time() < deadline:
            time.sleep(0.1)
            s = job_state.get(job_id) or {}
            if s or job_state.get(f"{job_id}:call"):
                break
        else:
            return {"id": job_id, "status": "not_found", "error": "job not found"}
    eff = _effective(s, time.time()) if s else s
    if eff is not s:
        # 判死的任务:按终态如实回(cancel_noop + 真因),别报「取消失败、可能仍在计费」。
        # 但判死只是推断,原调用可能还在排队 / 等重试(见 _release_stale_call)—— 以前这里取消次数为 0,
        # 随后 GC 删掉记录,旧调用开跑时拦不住,照样跑、照样计费(2026-09-26 review)。所以先真的取消一次。
        # 占位句柄(run_endpoint 写了 pending 后被杀)取不到 call_id,照旧按终态回,不会卡在「提交中」。
        _err = _release_stale_call(job_id, eff)
        if _err:
            return {"id": job_id, "status": s.get("status") or "unknown",
                    "error": f"cancel failed: {_err}", "was_running": s.get("status") == "running"}
        try:
            job_state[job_id] = eff
        except Exception:
            pass
        return _cancel_noop_view(job_id, eff, was_running=False)
    was_running = s.get("status") == "running"
    call_id = _call_id(job_id) or s.get("call_id")  # 新独立 key,兼容旧字段
    # 占位状态 = run_endpoint 正在 spawn,真实 call_id 还没写回来。这时既不能当"没有 call_id"
    # 直接标 cancelled(函数马上就要开始跑,谎报成功),也不该让用户白等 —— 短暂轮询一下。
    if not call_id and job_state.get(f"{job_id}:call") == _CALL_PENDING:
        for _ in range(20):   # 最多 ~2s
            time.sleep(0.1)
            call_id = _call_id(job_id)
            if call_id:
                break
        if not call_id:
            return {"id": job_id, "status": s.get("status") or "queued",
                    "error": "任务正在提交中,还拿不到句柄 —— 稍等一两秒再点取消",
                    "was_running": was_running}
    if call_id:
        try:
            # 不传 terminate_containers —— cancel() 自身就会中断执行并把 input 标记 TERMINATED。
            # 传 True 有两处害:(1)新版 Modal 服务端直接拒绝该请求,而恰恰只有 running 的任务
            # 才会传 True,结果「正在烧钱的任务反而取消不掉」;(2)强杀容器会让同容器上其它
            # 并发任务被重新调度 —— 本插件主打多任务并发,不能误伤邻居。
            modal.FunctionCall.from_id(call_id).cancel()
        except Exception as e:
            # 查不到的调用(过了 Modal 的保留期,实测 7 天前的就查不到)不可能在跑:当作已结束,
            # 往下按记录如实回。报「取消失败、可能仍在计费」正好说反(2026-09-26 review)。
            if type(e).__name__ != "NotFoundError":
                # 取消失败绝不能谎报成功:云端还在跑、还在计费,必须让调用方看见。
                print(f"[bridge] cancel call {call_id} FAILED: {e}")
                return {"id": job_id, "status": s.get("status") or "unknown",
                        "error": f"cancel failed: {e}", "was_running": was_running}
    # ⚠ 重新读一次再 merge:上面等占位 call_id 可能花了两秒、cancel() 本身也是一次 RPC,
    # worker 在这期间会把状态写成 running(带 started_at/progress),甚至直接写完 completed。
    # 拿函数开头那份快照无条件盖成 cancelled 有两种害:轻则冲掉 worker 写的进度,
    # 重则把**已经生成、已经付过钱**的产物判成"用户取消",前端看到 cancelled 就再不会去取。
    # 所以终态优先:worker 抢先写了终态就保留它,如实告诉调用方"取消没赶上"。
    cur = job_state.get(job_id)
    if isinstance(cur, dict) and cur.get("status") in ("completed", "failed"):
        print(f"[bridge] cancel {job_id}: worker 已先写 {cur['status']},保留终态不覆盖")
        return _cancel_noop_view(job_id, cur, was_running=was_running)
    job_state[job_id] = {**(cur if isinstance(cur, dict) else s), "status": "cancelled",
                         "completed_at": time.time()}
    return {"id": job_id, "status": "cancelled", "was_running": was_running}


@app.function(image=cuda_image, secrets=[bridge_secret], timeout=10)
@modal.fastapi_endpoint(method="GET", label=f"{APP_NAME}-health")
def health_endpoint(key: str = "", x_bridge_key: str = _Header("")):
    """健康 + 已装 custom_nodes(权威源:反映真实部署的镜像,供本地双向同步对比)。"""
    deny = _check(x_bridge_key or key)
    if deny:
        return deny
    info: dict = {"healthy": True, "app": APP_NAME, "volume": VOLUME_NAME,
                  "deployed_version": DEPLOYED_VERSION, "deployed_gpu": _PRIMARY_GPU,
                  "deployed_cheap_gpu": (_CHEAP_GPU if _CHEAP_ENABLED else None),
                  "deployed_top_gpu": (_TOP_GPU if _TOP_ENABLED else None),
                  "deployed_comfyui_tag": DEPLOYED_COMFYUI_TAG}
    try:
        warm = 0
        try:
            stats = modal.Cls.from_name(APP_NAME, "ComfyWorker")().run.get_current_stats()
            warm += getattr(stats, "num_total_runners", 0) or 0
        except Exception:
            pass
        info["warm_containers"] = warm
    except Exception as e:
        info["stats_error"] = str(e)
    try:
        cn_dir = Path("/comfyui/custom_nodes")
        info["custom_nodes"] = sorted(
            p.name for p in cn_dir.iterdir()
            if p.is_dir() and not p.name.startswith((".", "__"))
        ) if cn_dir.exists() else []
    except Exception as e:
        info["custom_nodes_error"] = str(e)
    # 镜像实际用的节点清单(带 url/commit)。上面 custom_nodes 只有文件夹名,本地拿它补全 url
    # 时补不出别的机器加的节点 → 被当成空 url 丢掉 → 下次部署从镜像里删掉。_custom_nodes_data
    # 已作为 Python 源随部署挂进容器,原样报回即可,不用改镜像。见 node_sync.complete_baked_entries。
    # ⚠ url 必须脱敏:它来自 `git config remote.origin.url`,私有节点常用
    #   https://user:ghp_xxx@github.com/... 克隆。bridge key 会给 AIGC Studio、MCP、导出的脚本,
    #   以前这些凭据只在私有镜像里,/health 一报就等于发给了所有持 key 的一方(2026-09-24 review)。
    #   被脱敏的条目标 url_redacted —— 本地据此知道这条不能照抄回清单(会丢掉克隆所需的凭据)。
    try:
        from _custom_nodes_data import CUSTOM_NODES as _baked
        manifest = []
        for n in _baked:
            if not isinstance(n, dict):
                continue
            url, red = _redact_url(n.get("url", ""))
            e = {"name": n.get("name", ""), "url": url, "commit": n.get("commit", "")}
            if red:
                e["url_redacted"] = True
            manifest.append(e)
        info["custom_nodes_manifest"] = manifest
    except Exception as e:
        info["custom_nodes_manifest_error"] = str(e)
    return info


def _redact_url(u: str) -> tuple[str, bool]:
    """去掉 URL 里的 userinfo(user:token@)。返回 (脱敏后的 url, 是否动过)。"""
    from urllib.parse import urlsplit, urlunsplit
    try:
        p = urlsplit(u or "")
        if not (p.username or p.password):
            return u or "", False
        host = (p.hostname or "") + (f":{p.port}" if p.port else "")
        return urlunsplit((p.scheme, host, p.path, p.query, p.fragment)), True
    except Exception:
        return "", True    # 解析不了就一个字都不报


# ============================================================================
# 本地调试
# ============================================================================
@app.local_entrypoint()
def main():
    print(f"App:    {APP_NAME}")
    print(f"Volume: {VOLUME_NAME}")
    print(f"Secret: {SECRET_NAME}")
    print("Endpoints:")
    for ep in ["run", "status", "cancel", "health", "fetch"]:
        print(f"  https://<workspace>--{APP_NAME}-{ep}.modal.run")
