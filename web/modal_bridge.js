// comfyui_modal_bridge — P1+P2 增强版
//
// 包含:
//   - actionBarButtons API 顶部按钮
//   - 异步轮询(submit → poll → fetch_result)
//   - 进度浮窗(可拖动 + 取消 + 错误展开 + history 持久化)
//   - custom_node 预警
//   - 批量提交(Run × N,自动改 seed)
//   - 注册到 ComfyUI Settings 面板
//   - 结果回填 SaveImage / PreviewImage 节点

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const log = (...a) => console.log("[modal_bridge]", ...a);
const err = (...a) => console.error("[modal_bridge]", ...a);

// =====================================================================
// i18n —— 跟随 ComfyUI 的 Comfy.Locale 设置(zh/en),实时切换
// t("key", {vars}) 取当前语言文案;字典 I18N 在文件末尾集中定义。
// 注:ComfyUI Settings 项注册后无法实时切换(架构限制),取 _locale() 在注册时定语言,
//     切语言需刷新页面;其余(对话框/弹窗/通知/进度)每次调 t() 实时跟随。
// =====================================================================
function _locale() {
  try {
    const v = app.ui?.settings?.getSettingValue?.("Comfy.Locale");
    if (typeof v === "string" && v) return v.toLowerCase().startsWith("zh") ? "zh" : "en";
    return (navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en";
  } catch (e) { return "en"; }
}
// =====================================================================
// i18n 字典 —— 所有用户可见文案的 zh/en。新增文案在此加一条,代码里用 t("key")。
// =====================================================================
const I18N = {
  // —— Setup 部署对话框 ——
  "dlg.title":        { zh: "☁️ 部署到 Modal", en: "☁️ Deploy to Modal" },
  "dlg.intro":        { zh: "全程在 ComfyUI 里完成,不用开终端。需要 Modal token(免费注册,每月送 $30):",
                        en: "All inside ComfyUI, no terminal. Needs a Modal token (free signup, $30/month free):" },
  "dlg.ver.local":    { zh: "插件(本地):", en: "Plugin (local): " },
  "dlg.ver.deployed": { zh: "云端部署:", en: "Deployed: " },
  "dlg.ver.aligned":  { zh: "✓ 已对齐", en: "✓ aligned" },
  "dlg.ver.mismatch": { zh: "⚠ 不一致,请点「部署」更新云端", en: "⚠ mismatch — click Deploy to update" },
  "dlg.ver.unreach":  { zh: "云端未部署 / 连不上", en: "not deployed / unreachable" },
  "dlg.ver.notconn":  { zh: "未连接", en: "not connected" },
  "dlg.ws.hint":      { zh: "(modal.com 个人主页 URL 那段,如 your-workspace)",
                        en: "(the segment in your modal.com profile URL, e.g. your-workspace)" },
  "dlg.secret.saved": { zh: ";已保存,留空=沿用", en: "; saved, leave blank to reuse" },
  "dlg.secret.ph_saved": { zh: "••••••••(已保存,留空沿用)", en: "•••••••• (saved, blank=reuse)" },
  "dlg.gpu.label":    { zh: "(Auto 按工作流显存自动选卡,更省钱;首次/升级后需部署一次)",
                        en: "(Auto picks the GPU by VRAM to save cost; deploy once after first use/upgrade)" },
  "dlg.gpu.opt_auto": { zh: "Auto — 更省钱(按显存自动选 L40S/H100/B200)",
                        en: "Auto — cheaper (auto L40S/H100/B200 by VRAM)" },
  "dlg.gpu.opt_h100": { zh: "H100(固定)", en: "H100 (fixed)" },
  "dlg.gpu.opt_b200": { zh: "B200(固定 · 最快最强)", en: "B200 (fixed · fastest)" },
  "dlg.gpu.note":     { zh: "Auto(更省钱):小图走 L40S、常规走 H100、超 80G 自动上 B200(183G,最强),按工作流显存自动选,最省。H100(固定):一律 H100。B200(固定):一律 B200,显存最大、速度最快,适合大图/视频/赶时间(最贵)。选择后点「部署」生效。点 RunModal 前会按类别估算显存预警(视频含多帧激活开销)。",
                        en: "Auto (cheaper): small→L40S, normal→H100, >80G→B200 (183G, top), chosen automatically per workflow VRAM. H100 (fixed): always H100. B200 (fixed): always B200, biggest VRAM & fastest, for large images/video/rush jobs (most expensive). Click Deploy to apply. Before running, VRAM is estimated per category (video includes multi-frame activations)." },
  "vram.warn.title":  { zh: "⚠ 显存可能不够", en: "⚠ VRAM may be tight" },
  "vram.warn.body":   { zh: "预估需 ~{est}GB(模型 {model}GB),超过所选 {gpu}({cap}GB)。可能 offload 变慢甚至 OOM。",
                        en: "Est. ~{est}GB ({model}GB models) exceeds the selected {gpu} ({cap}GB). May offload (slow) or OOM." },
  "vram.warn.unknown":{ zh: "(另有 {n} 个模型本地没找到,实际可能更高)", en: " ({n} models not found locally; real usage may be higher)" },
  "vram.warn.video":  { zh: "(视频类:显存还要算多帧激活,估算偏粗,务必留余量)", en: " (video: multi-frame activations add VRAM; estimate is rough, leave headroom)" },
  "vram.warn.run":    { zh: "仍要跑", en: "Run anyway" },
  "vram.warn.switch": { zh: "去 Setup 换显卡", en: "Switch GPU in Setup" },
  "dlg.comfy.hint":   { zh: "(可选,工作流含 API 节点才需要)", en: "(optional, only if your workflow uses API nodes)" },
  "dlg.comfy.ph":     { zh: "platform.comfy.org 生成的 API key", en: "API key from platform.comfy.org" },
  "dlg.comfy.ph_saved":{ zh: "已保存(留空=沿用)", en: "saved (blank = keep)" },
  "dlg.comfy.note":   { zh: "⚠ 存进云端 Secret,worker 用它跑 API 节点 —— 账单走你的 comfy.org 额度。",
                        en: "⚠ Stored in the cloud Secret; the worker uses it to run API nodes — billed to your comfy.org credits." },
  "api.warn.title":   { zh: "⚠ 工作流含 API 节点", en: "⚠ Workflow uses API nodes" },
  "api.warn.body":    { zh: "这个工作流用了 ComfyUI API 节点(Kling/Luma/OpenAI 等),在云端跑需要 comfy.org API key,但你还没配。\n\n现在跑这些 API 节点会 401 失败。\n\n建议先去 Setup 填 comfy.org API key。",
                        en: "This workflow uses ComfyUI API nodes (Kling/Luma/OpenAI, etc.). Running them in the cloud needs a comfy.org API key, which isn't configured.\n\nThose API nodes will fail (401) if you run now.\n\nAdd your comfy.org API key in Setup first." },
  "api.warn.run":     { zh: "仍要跑", en: "Run anyway" },
  "api.warn.setup":   { zh: "去 Setup 配置", en: "Configure in Setup" },
  "dlg.btn.deploy":   { zh: "部署", en: "Deploy" },
  "dlg.btn.test":     { zh: "测试连接", en: "Test connection" },
  "dlg.btn.close":    { zh: "关闭", en: "Close" },
  "dlg.nodes.title":  { zh: "管理云端节点", en: "Manage cloud nodes" },
  "dlg.nodes.load":   { zh: "加载镜像节点", en: "Load image nodes" },
  "dlg.nodes.warn":   { zh: "勾选 = 从云端镜像移除该节点 + 重部署。⚠ 别的电脑若用到会失败、需重新加(多机各装一部分时慎删)。",
                        en: "Checked = remove from cloud image + redeploy. ⚠ other machines using it will fail and need re-add." },
  "dlg.nodes.prune":  { zh: "移除勾选项并重部署", en: "Remove selected & redeploy" },
  // —— 测试连接 ——
  "test.running":     { zh: "测试中(冷启动时可能要等几秒)...", en: "Testing (cold start may take a few seconds)..." },
  "test.ok":          { zh: "✓ 连接正常(云端 {ver}, warm={warm}, 已装节点={nodes})",
                        en: "✓ Connected (cloud {ver}, warm={warm}, nodes={nodes})" },
  "test.fail":        { zh: "✗ 连不上:{why} — app 可能没部署/被删,请点「部署」",
                        en: "✗ Unreachable: {why} — app may be undeployed/deleted, click Deploy" },
  "test.err":         { zh: "✗ 测试失败:{e}", en: "✗ Test failed: {e}" },
  "test.unreach":     { zh: "endpoint 不可达", en: "endpoint unreachable" },
  // —— 部署按钮 / 节点管理动作 ——
  "dep.fill_saved":   { zh: "请填对 workspace + ak- token(secret 可留空沿用)",
                        en: "Fill workspace + ak- token (secret may be left blank)" },
  "dep.fill_all":     { zh: "请填对 workspace + ak-/as- token", en: "Fill workspace + ak-/as- token" },
  "dep.running":      { zh: "部署中(首次拉镜像约 3-5 分钟,别关窗口)...",
                        en: "Deploying (first image pull ~3-5 min, keep window open)..." },
  "dep.ok":           { zh: "✓ 部署成功!可以关掉这个窗口去出图了",
                        en: "✓ Deployed! Close this window and start generating." },
  "dep.ok.toast":     { zh: "✓ Modal 部署成功", en: "✓ Modal deployed" },
  "dep.fail":         { zh: "✗ 部署失败 rc={rc}(看上面日志)", en: "✗ Deploy failed rc={rc} (see log above)" },
  "dep.fail.toast":   { zh: "Modal 部署失败 rc={rc}", en: "Modal deploy failed rc={rc}" },
  "nodes.redeploying":{ zh: "重部署中(约 1-3 分钟,别关窗口)...", en: "Redeploying (~1-3 min, keep window open)..." },
  // —— 节点同步 ——
  "node.scan":        { zh: "扫描工作流 custom nodes...", en: "Scanning workflow custom nodes..." },
  "node.nogit":       { zh: "这些 custom_node 没有 git 信息,无法自动补:\n{list}",
                        en: "These custom_nodes have no git info, cannot auto-add:\n{list}" },
  "node.ok":          { zh: "nodes ok ({baked} custom + {builtin} builtin)", en: "nodes ok ({baked} custom + {builtin} builtin)" },
  "node.confirm_skip":{ zh: "部分 custom_node 无法自动补,仍然提交?(很可能失败)",
                        en: "Some custom_nodes can't be auto-added. Submit anyway? (likely to fail)" },
  "node.sync_title":  { zh: "工作流需要的 custom_node 要同步到 Modal(镜像来源:{src}):\n\n{parts}\n\n点「确定」更新 Modal 镜像并重新部署(约 1-3 分钟,只这一次,之后秒进)。\n点「取消」则跳过同步,直接提交。",
                        en: "Custom_nodes to sync to Modal (image source: {src}):\n\n{parts}\n\nOK = update Modal image & redeploy (~1-3 min, one-time, instant after).\nCancel = skip sync and submit." },
  "node.add_head":    { zh: "➕ 新增(Modal 还没有):", en: "➕ Add (not on Modal yet):" },
  "node.upd_head":    { zh: "🔄 更新(本地 commit 变了):", en: "🔄 Update (local commit changed):" },
  "node.nodes_n":     { zh: "{n} 节点", en: "{n} nodes" },
  "node.skip_confirm":{ zh: "不同步节点,直接提交?(可能失败)", en: "Submit without syncing nodes? (may fail)" },
  "node.redeploy":    { zh: "Redeploying Modal image...", en: "Redeploying Modal image..." },
  "node.updated":     { zh: "✓ image updated", en: "✓ image updated" },
  "node.deploy_fail": { zh: "Modal 重部署失败(看进度窗 deploy 日志 / ComfyUI 控制台)",
                        en: "Modal redeploy failed (see progress log / ComfyUI console)" },
  // —— 模型同步 ——
  "mdl.on_volume":    { zh: "{present}/{total} 已在 Volume", en: "{present}/{total} on Volume" },
  "mdl.downloading":  { zh: "这些模型本地还在下载中,现在传会传成残缺文件:\n\n{list}\n\n建议:等本地下完再点 [☁️ Modal]。\n\n仍然继续提交?(会缺这些模型,大概率失败)",
                        en: "These models are still downloading locally; uploading now would push partial files:\n\n{list}\n\nTip: wait until done, then click [☁️ Modal].\n\nSubmit anyway? (missing these, likely to fail)" },
  "mdl.cancel_dl":    { zh: "Cancelled — 本地模型还在下载中", en: "Cancelled — local models still downloading" },
  "mdl.no_source":    { zh: "下面这些模型 Modal Volume 没有,本地也找不到,无法自动同步:\n\n{list}\n\n解决:先在本地 ComfyUI 里把这些模型下到对应 models/<类型>/ 目录,再跑。\n\n仍然继续提交?(大概率失败)",
                        en: "These models are on neither the Volume nor locally, can't auto-sync:\n\n{list}\n\nFix: download them locally into models/<type>/ first, then run.\n\nSubmit anyway? (likely to fail)" },
  "mdl.cancel_miss":  { zh: "Cancelled — 缺本地模型", en: "Cancelled — missing local models" },
  "mdl.all_present":  { zh: "模型齐全({present}/{total})✓", en: "All models present ({present}/{total}) ✓" },
  "mdl.upload_confirm":{ zh: "这些模型本地有、Modal Volume 还没有,需要上传一次(共 ~{mb} MB):\n\n{list}\n\n点「确定」上传到 Volume(块级去重:网上通用大模型秒过,只有新内容真正占上行带宽;只这一次,之后秒进)。\n点「取消」则不传,直接提交。",
                        en: "These models exist locally but not on the Volume, need a one-time upload (~{mb} MB):\n\n{list}\n\nOK = upload to Volume (block dedup: common big models are instant; one-time, instant after).\nCancel = skip and submit." },
  "mdl.uploading":    { zh: "上传 {n} 个模型到 Volume...", en: "Uploading {n} models to Volume..." },
  "mdl.upload_fail":  { zh: "模型上传失败(看进度窗日志 / ComfyUI 控制台)", en: "Model upload failed (see progress log / ComfyUI console)" },
  "mdl.synced":       { zh: "{n} 个模型已同步 ✓", en: "{n} models synced ✓" },
  // —— 通知 / 版本契约 / 恢复 ——
  "toast.saved_no_node":{ zh: "图已存到 output/{sf}/(没找到对应输出节点)", en: "Image saved to output/{sf}/ (no matching output node)" },
  "toast.saved_3d":   { zh: "🧊 3D 模型已存到 output/{sf}/", en: "🧊 3D model saved to output/{sf}/" },
  "toast.bg_done":    { zh: "✓ 后台工作流出图完成,切到该 tab 即显示(也已存 output/{sf}/)",
                        en: "✓ Background workflow done; switch to its tab to view (also saved to output/{sf}/)" },
  "toast.not_deployed":{ zh: "还没部署到 Modal。先点右上角 [⚙️ Modal Setup] 填 token 一键部署(不用开终端)",
                        en: "Not deployed yet. Click [⚙️ Modal Setup] top-right to deploy with your token (no terminal)." },
  "toast.fail":       { zh: "✗ {wf} 失败:{msg}", en: "✗ {wf} failed: {msg}" },
  "toast.recovered":  { zh: "✓ {wf} 恢复完成 → output/{sf}/", en: "✓ {wf} recovered → output/{sf}/" },
  "toast.still":      { zh: "Job {id} 仍在 {status};重新点 [☁️ Modal] 监控", en: "Job {id} still {status}; click [☁️ Modal] again to monitor" },
  "stage.still":      { zh: "still {status};重新点 [☁️ Modal] 监控", en: "still {status}; click [☁️ Modal] again" },
  "ver.unreach_toast":{ zh: "云端连不上(app 可能没部署/被删)。点 [⚙️ Modal Setup] 重新部署",
                        en: "Cloud unreachable (app maybe undeployed/deleted). Click [⚙️ Modal Setup] to redeploy." },
  "ver.mismatch_toast":{ zh: "插件版本 {local} 与云端部署的 {deployed} 不一致,需重新部署。",
                        en: "Plugin {local} differs from deployed {deployed}; redeploy needed." },
  "ver.unreach_msg":  { zh: "云端 Modal 连不上(没部署 / app 被删)。\n\n点「确定」打开部署窗口。",
                        en: "Cloud Modal unreachable (not deployed / app deleted).\n\nOK to open the deploy dialog." },
  "ver.mismatch_msg": { zh: "⚠ 版本不一致:\n  插件(本地):{local}\n  云端部署:{deployed}\n\n你升级了插件但还没重新部署,云端跑的是旧代码,会出问题。\n\n点「确定」打开部署窗口重新部署。",
                        en: "⚠ Version mismatch:\n  Plugin (local): {local}\n  Deployed: {deployed}\n\nYou upgraded the plugin but haven't redeployed; the cloud runs old code.\n\nOK to open the deploy dialog." },
  "export.done":      { zh: "已导出 {name}_modal.py —— 给别人:让他装 requests、填 KEY、python 跑即可(模型/节点需已同步过)。",
                        en: "Exported {name}_modal.py — share it: recipient installs requests, fills KEY, runs python (models/nodes must be already synced)." },
  "export.fail":      { zh: "导出失败:取当前工作流出错", en: "Export failed: couldn't read the current workflow" },
  "export.key.title": { zh: "要把你的 API KEY 写进导出文件吗?", en: "Write your API KEY into the exported file?" },
  "export.key.body":  { zh: "这把 key = 你的 Modal 账单:谁拿到都能花你的钱,泄露只能整把换 key。\n\n【嵌入 KEY】文件直接能跑,但 key 明文写在里面 —— 只发可信的人 / 自己后端。\n【用占位符(推荐)】文件不含 key,对方用你私下给的 key 自己填 —— 对外分享选这个。", en: "This key = your Modal billing: anyone who has it can spend your money; a leak means rotating the key.\n\n[Embed KEY] Runs as-is, but the key sits in the file in plaintext — trusted people / your own backend only.\n[Use placeholder (recommended)] No key in the file; the recipient fills in the key you give them privately — pick this for sharing." },
  "export.key.embed": { zh: "嵌入 KEY", en: "Embed KEY" },
  "export.key.placeholder":{ zh: "用占位符(推荐)", en: "Use placeholder (recommended)" },
  "export.key.fail":  { zh: "取 KEY 失败,已改用占位符(需重启 ComfyUI 加载新后端)", en: "Couldn't fetch KEY; fell back to placeholder (restart ComfyUI to load the new backend)" },
  "ver.gpu_mismatch_toast":{ zh: "显卡已改为 {local},但云端部署的是 {deployed},必须重新部署才生效。",
                        en: "GPU changed to {local}, but cloud is deployed on {deployed}; redeploy required." },
  "ver.gpu_mismatch_msg": { zh: "⚠ 显卡不一致:\n  你选的:{local}\n  云端实际在跑:{deployed}\n\nModal 的显卡是部署时固定的,换卡必须重新部署才生效——否则会继续在旧卡 {deployed} 上跑。\n\n点「确定」打开部署窗口重新部署。",
                        en: "⚠ GPU mismatch:\n  Selected: {local}\n  Actually running on cloud: {deployed}\n\nModal's GPU is fixed at deploy time; switching GPU needs a redeploy — otherwise it keeps running on the old {deployed}.\n\nOK to open the deploy dialog to redeploy." },
  "ver.comfyui_changed_toast":{ zh: "本机 ComfyUI 已升级({local}),云端还是部署时的 {deployed} —— 建议重新部署让云端跟上(不影响本次出图)。",
                        en: "Local ComfyUI upgraded ({local}); cloud still on {deployed} from last deploy — redeploy to sync (this run still proceeds)." },
  "ver.checking":     { zh: "检查云端中…", en: "Checking cloud…" },
  "ver.platform_startup":{ zh: "⚠ Modal 平台当前异常(status.modal.com),出图可能失败,等平台恢复",
                           en: "⚠ Modal platform is currently degraded (status.modal.com); jobs may fail until it recovers" },
  "ver.platform_toast":{ zh: "⚠ 连不上 Modal,可能是平台故障", en: "⚠ Can't reach Modal — possible platform outage" },
  "ver.platform_msg": { zh: "连不上 Modal 云端(超时)。\n\n这很可能是 Modal 平台故障,不是你的问题——重新部署也会失败。\n\n点「确定」打开 status.modal.com 查看平台状态;若显示故障,等恢复后再试即可。",
                        en: "Can't reach Modal cloud (timeout).\n\nThis is likely a Modal platform outage, not your fault — redeploying would also fail.\n\nOK to open status.modal.com; if it shows an outage, just wait for recovery." },
  "ver.notdeployed_toast":{ zh: "云端 app 未部署。点 [⚙️ Modal Setup] 部署", en: "Cloud app not deployed. Click [⚙️ Modal Setup]" },
  "ver.notdeployed_msg":{ zh: "云端 Modal app 不存在(没部署 / 被删)。\n\n点「确定」打开部署窗口。",
                          en: "Cloud Modal app not found (undeployed / deleted).\n\nOK to open the deploy dialog." },
  // —— 管理云端节点 动作 ——
  "mn.loading":       { zh: "加载中...", en: "Loading..." },
  "mn.empty":         { zh: "镜像里没有 custom_node", en: "No custom_nodes on the image" },
  "mn.installed":     { zh: "镜像实装 {n} 个(来源:{src})", en: "{n} installed on image (source: {src})" },
  "mn.nogit_tag":     { zh: "(本地无 git 信息)", en: "(no local git info)" },
  "mn.load_fail":     { zh: "✗ 加载失败:{e}", en: "✗ Load failed: {e}" },
  "mn.none_checked":  { zh: "没勾选任何节点", en: "Nothing selected" },
  "mn.confirm":       { zh: "确定从云端镜像移除这 {n} 个节点并重部署?\n\n{list}\n\n⚠ 别的电脑若用到这些节点会失败,需要时重新加。",
                        en: "Remove these {n} nodes from the cloud image & redeploy?\n\n{list}\n\n⚠ Other machines using them will fail and need re-add." },
  "mn.removed":       { zh: "✓ 已移除 {n} 个,镜像现 {keep} 个", en: "✓ Removed {n}, image now has {keep}" },
  "mn.removed_toast": { zh: "✓ 已从云端移除 {n} 个 custom_node", en: "✓ Removed {n} custom_nodes from cloud" },
  "mn.redeploy_fail": { zh: "✗ 重部署失败 rc={rc}(看日志)", en: "✗ Redeploy failed rc={rc} (see log)" },
  // —— 成功 toast / Settings tooltip ——
  "toast.done":       { zh: "✓ {wf} 完成 (job {id})", en: "✓ {wf} done (job {id})" },
  "toast.done_n":     { zh: "✓ {wf} {n} 张完成", en: "✓ {wf} {n} done" },
  "set.batch":        { zh: "一次点击跑几次(自动改 seed)", en: "How many runs per click (auto-reseed)" },
  "set.poll":         { zh: "查询状态频率", en: "Status polling interval" },
  "set.timeout":      { zh: "前端等出图的最长时间(秒),默认 900=15分钟,和 worker 单任务上限一致——worker 最多跑多久前端就等多久。出图后立刻返回,不会真等满;设大只是给冷启动+大模型留足空间。",
                        en: "Max seconds the frontend waits for a result. Default 900=15min, matching the worker job limit. Returns instantly when done; large values just allow cold start + big models." },
  "set.incognito":    { zh: "关闭后图会上传到 R2(需要 modal_app R2 凭据)", en: "If off, images upload to R2 (needs modal_app R2 creds)" },
  "set.autosync_models":{ zh: "提交前检查 Modal Volume,工作流要、Volume 没、但本地有的模型自动上传(块级去重,通用大模型秒过)",
                          en: "Before submit, auto-upload models the workflow needs that are missing on the Volume but present locally (block dedup, common big models instant)" },
  "set.autosync_nodes": { zh: "提交前把工作流用到的 custom_node 与本地双向同步到 Modal:缺的加、commit 变的更新、本地已卸载的移除,再重部署",
                          en: "Before submit, sync the workflow's custom_nodes with local: add missing, update changed commits, prune uninstalled, then redeploy" },
  "set.snapshot":     { zh: "实验:Modal 容器内存快照(CPU+GPU),冷启 ~30s→~5s。改后需在 Setup 重新部署生效;按 GPU 档需验证(挂了自动退化为普通冷启,不会更差)。",
                        en: "Experimental: Modal container memory snapshot (CPU+GPU), cold start ~30s→~5s. Redeploy in Setup to take effect; verify per GPU tier (self-heals to a normal cold start if unsupported)." },
  "set.snapshot.on":  { zh: "已开启内存快照 —— 去 Setup 重新部署才生效(实验,前 2-3 次冷启偏慢=制作快照)", en: "Snapshot ON — redeploy in Setup to take effect (experimental; first 2-3 cold starts slower while snapshotting)" },
  "set.snapshot.off": { zh: "已关闭内存快照 —— 去 Setup 重新部署生效", en: "Snapshot OFF — redeploy in Setup to take effect" },
};

function t(key, vars) {
  const lang = _locale();
  const tbl = (typeof I18N !== "undefined") ? I18N[key] : null;
  let s = (tbl && (tbl[lang] ?? tbl.en ?? tbl.zh)) ?? key;
  if (vars) for (const k of Object.keys(vars)) s = String(s).replaceAll(`{${k}}`, vars[k]);
  return s;
}

// =====================================================================
// 工具
// =====================================================================
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 上报 job 客户端侧结局(超时/取消/失败)给后端记日志——否则这些只在浏览器,
// ComfyUI 后端 log 看不到(用户反馈"报错没进 log")。fire-and-forget,失败无所谓。
function reportJobEvent(jobId, event, detail) {
  try {
    api.fetchApi("/modal_bridge/job_event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, event, detail: detail || "" }),
    }).catch(() => {});
  } catch (e) {}
}

// 查 Modal 官方状态页(status.modal.com)的整体状态,判断是否平台故障。
// 返回 true=平台异常(downtime/degraded/maintenance)。查询失败=false(不误报)。
async function isModalOutage() {
  try {
    const r = await api.fetchApi("/modal_bridge/platform_status");
    const d = await r.json();
    return d.state && d.state !== "operational" && d.state !== "unknown";
  } catch (e) { return false; }
}

const LS_KEYS = {
  progressPos: "modal_bridge.progress_pos",
  activeJob: "modal_bridge.active_job",   // 持久化未完成 job
};

function loadLS(k, def = null) {
  try {
    const v = localStorage.getItem(k);
    return v ? JSON.parse(v) : def;
  } catch (e) { return def; }
}
function saveLS(k, v) {
  try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {}
}
function clearLS(k) { try { localStorage.removeItem(k); } catch (e) {} }

// ComfyUI Settings 读取(优先,失败回退到 config.json)
function getSetting(id, def) {
  try {
    const v = app.ui?.settings?.getSettingValue?.(id);
    return v !== undefined && v !== null ? v : def;
  } catch (e) { return def; }
}

// =====================================================================
// 通知封装(右上角 toast)
// =====================================================================
function notify(message, severity = "info") {
  try {
    app.extensionManager?.toast?.add?.({
      severity,
      summary: "Modal Bridge",
      detail: message,
      life: severity === "error" ? 8000 : 4000,
    });
    return;
  } catch (e) {}
  log(`[${severity}]`, message);
}

// =====================================================================
// 输出节点 & 回填
// =====================================================================
function findOutputNodes(prompt) {
  const types = new Set([
    "SaveImage", "PreviewImage", "SaveImageWebsocket",
    "SaveImageAdvanced", "Image Save", "VHS_VideoCombine",
    // 原生视频/动图输出节点(ComfyUI core):它们的产出在 history 里也走 images 输出键
    // (PreviewVideo.as_dict = {images, animated}),所以同样能被回填/保存。
    "SaveVideo", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG",
    // 3D:SaveGLB 走 "3d" 键(saveMesh.ts);Preview3D 走 "result" 键(load3d.ts,从 output 加载)。
    // 两者都能画板内 3D 预览(buildOutput 按键分别拼对应结构)。
    "SaveGLB", "Preview3D", "Preview3DAdvanced",
  ]);
  const ids = [];
  for (const [id, n] of Object.entries(prompt || {})) {
    if (types.has(n?.class_type)) ids.push(id);
  }
  return ids;
}

// 工作流是否含 ComfyUI API 节点(comfy_api_nodes,如 Kling/Luma/OpenAI):这些节点要 comfy.org
// 账号鉴权才能跑。ComfyUI 给 API 节点的节点定义标了 api_node:true,据此检测(拿不到定义则视为无)。
function workflowHasApiNodes() {
  try {
    for (const n of (app.graph?._nodes || [])) {
      const nd = n?.constructor?.nodeData || n?.nodeData;
      if (nd && nd.api_node) return true;
    }
  } catch (e) { log("api node detect skipped:", e); }
  return false;
}

// 当前活动工作流的标识(切 tab 会变)。用于判断结果该不该回填到当前画板。
function activeWorkflowKey() {
  try {
    const w = app.extensionManager?.workflow?.activeWorkflow
           ?? app.workflowManager?.activeWorkflow;
    if (!w) return null;
    return w.path ?? w.key ?? w.filename ?? w.id ?? (typeof w === "string" ? w : null);
  } catch (e) { return null; }
}

// 当前工作流的可读短名(用于进度卡片标题)。取文件名去掉 .json,拿不到则 null。
function activeWorkflowName() {
  try {
    const w = app.extensionManager?.workflow?.activeWorkflow
           ?? app.workflowManager?.activeWorkflow;
    if (!w) return null;
    let n = w.filename ?? w.name ?? (typeof w.path === "string" ? w.path : null)
         ?? w.key ?? (typeof w === "string" ? w : null);
    if (!n) return null;
    n = String(n).split(/[\\/]/).pop().replace(/\.json$/i, "");
    return n || null;
  } catch (e) { return null; }
}

// 结果回填:仅当仍停在提交时那个工作流(tab)才按 id 回填,切了就不填(避免填到别的工作流)。
// guard = {graph, wfKey}(提交时抓)。返回回填节点数(0 = 没填,调用方提示存盘路径)。
// 回填到「当前前台」工作流的节点。ComfyUI 单 graph,只能渲染当前前台;调用方负责
// 确保这是该结果对应的工作流。走原生 executed 事件(ComfyUI 自己写 nodeOutputStore/
// nodeOutputs + 调 onExecuted,跨版本可靠)。返回成功回填的节点数。
// 视频 / 动图扩展名:这些产出在 ComfyUI 里也走 images 输出键,但要带 animated 标记
// 前端才会用 <video>/动图方式预览(原生 PreviewVideo.as_dict 即 {images, animated:(True,)})。
const VIDEO_EXT_RE = /\.(mp4|webm|mov|mkv|avi|flv|m4v|gif|webp|apng)$/i;
// 3D 产物扩展名:也用于 Preview3D 的 result 键里挑出"3D 模型那个文件"(result 里可能还含 bg 图)。
const MODEL3D_EXT_RE = /\.(glb|gltf|obj|fbx|stl|ply|splat|spz|ksplat)$/i;
// dict 形态({filename,subfolder,type})能直接派发渲染的输出键:
// images(SaveImage/SaveVideo)、gifs(VHS)、videos、3d(SaveGLB)。
// result(Preview3D)单独处理:它要的是 {result:[路径字符串,...]},loadFolder 前端写死 output。
const DISPATCHABLE_KEYS = new Set(["images", "gifs", "videos", "3d"]);

function displayInGraph(outputNodeIds, modalOutputs) {
  if (!outputNodeIds?.length || !modalOutputs?.length) return 0;
  // 按来源节点分发(node_id 由新 worker 提供;老 worker 没有 → 回退广播,保持旧行为不破坏)。
  const hasNodeId = modalOutputs.some((o) => o.node_id != null);
  const byNode = {};
  for (const o of modalOutputs) (byNode[String(o.node_id)] ??= []).push(o);

  // 按"原始输出键"重建 executed 事件:节点本来发什么键就回填什么键,各自 widget(图/视频/3D)
  // 就会渲染。老 worker 没 key → 当 images(旧行为)。
  const buildOutput = (list) => {
    const out = {};
    let animated = false;
    for (const o of list) {
      // Preview3D:result=[filePath, cameraState?, bg?, ...],result[0] 是路径(load3d.ts 从 output 加载)。
      // 只回填 3D 模型那个文件为 result[0](camera/bg 省略,用默认视角即可渲染)。
      if (o.key === "result") {
        if (MODEL3D_EXT_RE.test(o.filename || "")) {
          out.result = [(o.subfolder ? o.subfolder + "/" : "") + o.filename];
        }
        continue;
      }
      const key = o.key == null ? "images" : (DISPATCHABLE_KEYS.has(o.key) ? o.key : null);
      if (key == null) continue;  // 未知/不可重建的键 → 跳过(文件仍已落盘)
      (out[key] ??= []).push({ filename: o.filename, subfolder: o.subfolder, type: o.type || "output" });
      if (key === "images" && VIDEO_EXT_RE.test(o.filename || "")) animated = true;
    }
    if (animated) out.animated = [true];
    return out;
  };

  let placed = 0;
  for (const nid of outputNodeIds) {
    const node = app.graph.getNodeById(parseInt(nid, 10)) || app.graph.getNodeById(nid);
    if (!node) continue;
    const mine = hasNodeId ? (byNode[String(nid)] || []) : modalOutputs;
    if (!mine.length) continue;
    const out = buildOutput(mine);
    if (!Object.keys(out).length) continue;  // 全是不可重建的键 → 不派发(文件仍已落盘)
    try {
      api.dispatchEvent(new CustomEvent("executed", {
        detail: { node: String(nid), display_node: String(nid), output: out },
      }));
    } catch (e) {}
    try { node.onExecuted?.(out); } catch (e) {}
    placed++;
  }
  try { app.graph.setDirtyCanvas(true, true); } catch (e) {}
  log(`displayInGraph: ids=[${outputNodeIds.join(",")}] placed=${placed} byNode=${hasNodeId}`);
  return placed;
}

// 后台工作流的出图结果暂存,等用户切回那个 tab 再渲染(ComfyUI 单 graph,后台渲染不了)。
const _pendingResults = new Map();  // wfKey -> {outputNodeIds, outputs}
let _pendingWatcher = null;
function storePendingResult(wfKey, outputNodeIds, outputs) {
  _pendingResults.set(wfKey, { outputNodeIds, outputs });
  if (_pendingWatcher) return;
  _pendingWatcher = setInterval(() => {
    if (!_pendingResults.size) { clearInterval(_pendingWatcher); _pendingWatcher = null; return; }
    const k = activeWorkflowKey();
    if (k != null && _pendingResults.has(k)) {
      const r = _pendingResults.get(k);
      _pendingResults.delete(k);
      log("切到该工作流,回填暂存结果:", k);
      displayInGraph(r.outputNodeIds, r.outputs);
    }
  }, 700);
}

// =====================================================================
// custom_node 双向同步
// 后端 /check_nodes 精确解析(本地 NODE_CLASS_MAPPINGS → custom_nodes 文件夹),
// 和 Modal /health 的 custom_nodes 权威比对,算出 加/改/删 plan;确认后走 /sync_nodes
// 写回清单 + 重部署。本地始终是真源。全程在 ComfyUI 里完成,不用开终端。
// =====================================================================
async function checkNodesOnModal(prompt) {
  const res = await api.fetchApi("/modal_bridge/check_nodes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!res.ok) throw new Error(`check_nodes HTTP ${res.status}`);
  return res.json(); // {add, update, prune, missing_no_git, unresolved, new_baked, needs_deploy, ok_*, source}
}

// 流式 POST(/deploy、/add_nodes 共用):逐行回调 onLine,返回 __DEPLOY_DONE__ 的 rc(无则 null)
async function streamPost(path, body, onLine) {
  const res = await api.fetchApi(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`${path} HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", rc = null;
  const feed = (line) => {
    if (!line.length) return;
    const m = line.match(/__DEPLOY_DONE__ rc=(\d+)/);
    if (m) { rc = parseInt(m[1], 10); return; }
    onLine(line);
  };
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop(); // 末尾不完整的留到下一轮
    for (const l of lines) feed(l);
  }
  if (buf) feed(buf);
  return rc;
}

// 调 /sync_nodes,把新清单写回并重部署,流式推进 deploying 进度条,返回是否成功
async function syncNodes(plan, ctx) {
  const [a, b] = STATUS_PROGRESS.deploying;
  let tick = 0;
  const rc = await streamPost(
    "/modal_bridge/sync_nodes",
    {
      new_baked: plan.new_baked,
      summary: { add: plan.add.length, update: plan.update.length, prune: plan.prune.length },
    },
    (line) => {
      log("deploy:", line);
      tick++;
      ctx.bar(a + Math.min(b - a - 1, tick * 0.4));
      ctx.stage("deploying", line.length > 72 ? line.slice(0, 72) + "…" : line);
    },
  );
  return rc === 0;
}

// submit 前调:custom_node 与本地双向同步(加/改/删)。返回 true=可继续 / false=用户取消
async function ensureNodesAvailable(prompt, ctx) {
  ctx.stage("nodes", t("node.scan"));
  let plan;
  try {
    plan = await checkNodesOnModal(prompt);
  } catch (e) {
    log("check_nodes failed, skip:", e);
    return true; // 检查本身失败不阻塞提交
  }

  const { add = [], update = [], prune = [], missing_no_git = [], unresolved = [], source } = plan;
  if (unresolved.length) log("unresolved class_types (本地也没装):", unresolved);

  if (missing_no_git.length) {
    const list = missing_no_git.map((m) => `  ${m.folder} (${m.class_types.join(", ")})`).join("\n");
    notify(t("node.nogit", { list }), "warn");
  }

  if (!plan.needs_deploy) {
    ctx.stage("nodes", t("node.ok", { baked: plan.ok_baked, builtin: plan.ok_builtin }));
    if (missing_no_git.length) {
      return confirm(t("node.confirm_skip"));
    }
    return true;
  }

  // 组装确认文案(只加 / 改,不删 —— 多机并集,删走 Setup 的「管理云端节点」手动做)
  const parts = [];
  if (add.length) {
    parts.push(t("node.add_head") + "\n" + add.map((m) =>
      `   • ${m.folder} (${t("node.nodes_n", { n: m.class_types.length })})` + (m.commit ? ` @ ${m.commit.slice(0, 8)}` : "")
    ).join("\n"));
  }
  if (update.length) {
    parts.push(t("node.upd_head") + "\n" + update.map((m) =>
      `   • ${m.folder}  ${(m.old_commit || "—").slice(0, 8)} → ${m.commit.slice(0, 8)}`
    ).join("\n"));
  }
  const msg = t("node.sync_title", { src: source, parts: parts.join("\n\n") });
  if (!confirm(msg)) {
    return confirm(t("node.skip_confirm"));
  }

  ctx.stage("deploying", t("node.redeploy"), false);
  ctx.bar(STATUS_PROGRESS.deploying[0]);
  const ok = await syncNodes(plan, ctx);
  if (!ok) {
    throw new Error(t("node.deploy_fail"));
  }
  ctx.stage("deploying", t("node.updated"), false);
  return true;
}

// =====================================================================
// 批量提交:扫 prompt 找 KSampler/RandomNoise/RandomNoiseAdv 的 seed 字段
// =====================================================================
function reseedPrompt(prompt, newSeed) {
  // 深拷贝
  const p = JSON.parse(JSON.stringify(prompt));
  for (const n of Object.values(p)) {
    const cls = n?.class_type || "";
    const ins = n?.inputs || {};
    if (
      cls === "KSampler" || cls === "KSamplerAdvanced" ||
      cls === "RandomNoise" || cls === "Seed" ||
      cls === "SamplerCustomAdvanced"
    ) {
      if ("seed" in ins && typeof ins.seed === "number") ins.seed = newSeed;
      if ("noise_seed" in ins && typeof ins.noise_seed === "number") ins.noise_seed = newSeed;
    }
  }
  return p;
}

// =====================================================================
// 进度浮窗 — 每个 job 一张独立卡片(多 workflow 并发互不覆盖)
// =====================================================================
const STAGE_LABELS = {
  preparing: "Preparing", nodes: "Checking nodes", deploying: "Deploying image",
  checking: "Checking models", uploading: "Uploading models", submitting: "Submitting",
  queued: "Queued", running: "Running",
  downloading: "Downloading result", done: "Done", failed: "Failed",
};

const STATUS_PROGRESS = {
  preparing:   [0,  2],    // 序列化 workflow(纯前端,几百 ms)
  nodes:       [2,  5],    // 检查 custom_node Modal 是否齐全
  deploying:   [5, 35],    // (仅需补节点时)重 build 镜像并部署,1-3 分钟
  checking:    [35, 40],   // 检查 Modal Volume 已有哪些模型
  uploading:   [40, 78],   // 上传本地缺失模型到 Volume
  submitting:  [78, 82],
  queued:      [82, 85],
  running:     [85, 96],
  downloading: [96, 99],   // 出图 base64 回流
  done:        [100, 100],
  failed:      [100, 100],
};

// 卡片容器:右上角纵向堆叠,每个并发 job 一张卡。拖任意卡的标题 = 移动整个堆。
let progressStack = null;
let _stackDrag = null;

function _initStackDragListeners() {
  if (_initStackDragListeners._done) return;
  _initStackDragListeners._done = true;
  document.addEventListener("mousemove", (e) => {
    if (!_stackDrag || !progressStack) return;
    const dx = e.clientX - _stackDrag.x, dy = e.clientY - _stackDrag.y;
    const top = Math.max(0, _stackDrag.top + dy);
    const right = Math.max(0, _stackDrag.right - dx);
    progressStack.style.top = `${top}px`;
    progressStack.style.right = `${right}px`;
    progressStack.style.left = "auto";
  });
  document.addEventListener("mouseup", () => {
    if (!_stackDrag || !progressStack) { _stackDrag = null; return; }
    const top = parseInt(progressStack.style.top, 10);
    const right = parseInt(progressStack.style.right, 10);
    saveLS(LS_KEYS.progressPos, { top, right });
    _stackDrag = null;
  });
}

function startStackDrag(e) {
  if (e.target.closest(".mb-cancel")) return; // 别和取消按钮抢点击
  const rect = progressStack.getBoundingClientRect();
  _stackDrag = {
    x: e.clientX, y: e.clientY,
    top: rect.top,
    right: window.innerWidth - rect.right,
  };
  e.preventDefault();
}

function ensureStack() {
  if (progressStack) return progressStack;
  progressStack = document.createElement("div");
  progressStack.id = "modal-bridge-progress-stack";
  const pos = loadLS(LS_KEYS.progressPos) || {};
  const top = pos.top ?? 60;
  const right = pos.right ?? 20;
  progressStack.style.cssText =
    `position:fixed;top:${top}px;right:${right}px;z-index:99999;display:flex;flex-direction:column;gap:8px;align-items:flex-end;pointer-events:none;`;
  document.body.appendChild(progressStack);
  _initStackDragListeners();
  return progressStack;
}

let progressSeq = 0;

// 创建一个 job 的进度卡片,返回带方法的 ctx(stage / bar / setCancel / finish)。
// 每张卡自带计时器和状态,互不干扰 —— 这是多 workflow 并发不互相覆盖的关键。
function newProgress(initialStage = "preparing", wfName = null) {
  ensureStack();
  const card = document.createElement("div");
  card.style.cssText =
    "pointer-events:auto;min-width:280px;max-width:360px;padding:10px 14px;" +
    "background:rgba(28,28,36,0.96);color:#fff;border-radius:10px;font-size:12px;" +
    "font-family:-apple-system,system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,0.5);" +
    "backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.08);";
  card.innerHTML = `
    <div class="mb-drag" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;cursor:move;user-select:none;">
      <div class="mb-label" style="font-weight:600;flex:1;min-width:0;margin-right:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">☁️ Modal</div>
      <div style="display:flex;align-items:center;gap:8px;">
        <div class="mb-time" style="opacity:0.7;font-variant-numeric:tabular-nums;font-size:11px;">0s</div>
        <button class="mb-cancel" title="Cancel" style="all:unset;cursor:pointer;color:#ef4444;font-size:14px;font-weight:bold;padding:0 4px;border-radius:3px;display:none;">✕</button>
      </div>
    </div>
    <div style="height:4px;background:rgba(255,255,255,0.1);border-radius:2px;overflow:hidden;">
      <div class="mb-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#6366f1,#8b5cf6);border-radius:2px;transition:width 0.4s cubic-bezier(0.4,0,0.2,1);"></div>
    </div>
    <div class="mb-detail" style="margin-top:6px;opacity:0.6;font-size:11px;word-break:break-all;"></div>
    <div class="mb-errtoggle" style="display:none;margin-top:4px;font-size:10px;color:#fca5a5;cursor:pointer;text-decoration:underline;">▶ Show error details</div>
    <pre class="mb-errdetail" style="display:none;margin:4px 0 0;padding:6px;background:rgba(0,0,0,0.3);border-radius:4px;font-size:10px;max-height:160px;overflow:auto;white-space:pre-wrap;"></pre>
  `;
  progressStack.appendChild(card);
  card.querySelector(".mb-drag").addEventListener("mousedown", startStackDrag);

  const els = {
    card,
    label: card.querySelector(".mb-label"),
    time: card.querySelector(".mb-time"),
    bar: card.querySelector(".mb-bar"),
    detail: card.querySelector(".mb-detail"),
    cancel: card.querySelector(".mb-cancel"),
    errToggle: card.querySelector(".mb-errtoggle"),
    errDetail: card.querySelector(".mb-errdetail"),
  };

  const ctx = {
    id: ++progressSeq,
    jobId: null,
    wfName: wfName,
    cancelable: false,
    finished: false,
    onCancel: null,
    startMs: Date.now(),
    timer: null,
    runTimer: null,
    els,
  };

  ctx.timer = setInterval(() => {
    els.time.textContent = `${((Date.now() - ctx.startMs) / 1000).toFixed(1)}s`;
  }, 100);

  els.cancel.onclick = async (ev) => {
    ev.stopPropagation();
    if (ctx.finished) { ctx.closeCard(); return; }  // 结束后:✕ = 关闭卡片
    if (!ctx.cancelable || typeof ctx.onCancel !== "function") return;
    await ctx.onCancel();
  };
  els.errToggle.onclick = () => {
    const open = els.errDetail.style.display === "block";
    els.errDetail.style.display = open ? "none" : "block";
    els.errToggle.textContent = open ? "▶ Show error details" : "▼ Hide error details";
  };

  ctx.bar = (pct) => { els.bar.style.width = `${pct}%`; };

  ctx.closeCard = () => {
    if (ctx.timer) { clearInterval(ctx.timer); ctx.timer = null; }
    if (ctx.runTimer) { clearInterval(ctx.runTimer); ctx.runTimer = null; }
    els.card.remove();
  };

  ctx.stage = (stageKey, detail = null, cancelable = null) => {
    els.label.textContent = `☁️ ${ctx.wfName || "Modal"} · ${STAGE_LABELS[stageKey] || stageKey}`;
    if (detail !== null) els.detail.textContent = detail;
    if (cancelable !== null) {
      ctx.cancelable = cancelable;
      els.cancel.style.display = cancelable ? "inline-block" : "none";
    }
    const range = STATUS_PROGRESS[stageKey];
    if (range) ctx.bar((range[0] + range[1]) / 2);
    if (ctx.runTimer) { clearInterval(ctx.runTimer); ctx.runTimer = null; }
    if (stageKey === "running" && range) {
      const [a, b] = range;
      const stageStart = Date.now();
      ctx.runTimer = setInterval(() => {
        const s = (Date.now() - stageStart) / 1000;
        ctx.bar(a + Math.min(b - a, (s / 60) * (b - a)));  // 60s 占满 running 区间
      }, 200);
    }
  };

  ctx.setCancel = (jobId, fn) => { ctx.jobId = jobId; ctx.onCancel = fn; };

  ctx.finish = (success, finalLabel = "✓ Done", errDetail = null) => {
    ctx.finished = true;
    if (ctx.timer) { clearInterval(ctx.timer); ctx.timer = null; }
    if (ctx.runTimer) { clearInterval(ctx.runTimer); ctx.runTimer = null; }
    ctx.bar(100);
    els.bar.style.background = success
      ? "linear-gradient(90deg,#10b981,#22c55e)"
      : "linear-gradient(90deg,#ef4444,#f97316)";
    els.label.textContent = `☁️ ${ctx.wfName || "Modal"} · ${finalLabel}`;
    els.time.textContent = `${((Date.now() - ctx.startMs) / 1000).toFixed(1)}s`;
    // 结束后 ✕ 变成「关闭」按钮(失败/取消的卡留着不自动消失,得能手动关)
    ctx.cancelable = false;
    els.cancel.textContent = "×";
    els.cancel.title = "关闭";
    els.cancel.style.color = "#9aa";
    els.cancel.style.display = "inline-block";
    if (errDetail) {
      els.errToggle.style.display = "block";
      els.errDetail.textContent = errDetail;
    }
    // 成功 4s 后自动移除;失败/取消留着让用户看,点 × 关
    if (success) setTimeout(() => els.card.remove(), 4000);
  };

  ctx.stage(initialStage);
  return ctx;
}

// =====================================================================
// 单次跑(submit + poll + fetch_result)
// =====================================================================
// GPU:统一全部走 H100 → A100-80GB(原生 fallback)。
// 不再按显存分档 —— 之前把小模型(klein 等)丢到 L40S 反而比大模型在 H100 上慢。
// tier 入参保留(后端兼容),但恒为 "80g" 都指向同一个 H100 worker。
// =====================================================================

// 所有工作流统一 H100;tier 恒为 "80g"(后端两档都指向同一个 H100 worker)。
function getVramTier(_prompt) {
  return "80g";
}

// 未完成 job 持久化(支持多个并发):LS 存数组,刷新后逐个尝试恢复
function addActiveJob(j) {
  let a = loadLS(LS_KEYS.activeJob);
  a = Array.isArray(a) ? a : (a ? [a] : []);
  a.push(j);
  saveLS(LS_KEYS.activeJob, a);
}
function removeActiveJob(jobId) {
  let a = loadLS(LS_KEYS.activeJob);
  a = Array.isArray(a) ? a : (a ? [a] : []);
  saveLS(LS_KEYS.activeJob, a.filter((x) => x.jobId !== jobId));
}

async function runOnceOnModal(workflowPrompt, outputNodeIds, ctx, submitGuard, batchInfo = null) {
  // batchInfo: {current, total}
  const batchSuffix = batchInfo ? `[${batchInfo.current}/${batchInfo.total}] ` : "";

  ctx.stage("submitting", batchSuffix + "POST /submit", false);

  const subRes = await api.fetchApi("/modal_bridge/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: workflowPrompt, tier: getVramTier(workflowPrompt) }),
  });
  const sub = await subRes.json();
  if (!subRes.ok || !sub.ok) {
    throw new Error(sub.error || `HTTP ${subRes.status}`);
  }

  const jobId = sub.job_id;
  const gpu = sub.gpu;
  let cancelled = false;  // 点取消后置位:poll 循环据此立即退出,卡片不等后续 poll
  addActiveJob({ jobId, gpu, wfName: ctx.wfName, startedAt: Date.now() });
  // 这张卡的取消只取消这个 job(各 job 互不影响)
  ctx.setCancel(jobId, async () => {
    if (!confirm(`Cancel Modal job ${jobId.slice(0, 8)}?`)) return;
    cancelled = true;
    reportJobEvent(jobId, "user_cancelled", "用户点取消");
    // 立即结束卡片(不依赖后续 poll 拿到 cancelled —— 那可能慢或因竞态拿不到)
    removeActiveJob(jobId);
    ctx.finish(false, "✕ Cancelled");
    // 后台告诉 Modal 取消(不阻塞 UI,失败也无所谓)
    api.fetchApi("/modal_bridge/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    }).catch((e) => err("cancel failed", e));
  });
  log("submitted", jobId, "gpu=" + gpu);

  ctx.stage("queued", `${batchSuffix}job=${jobId.slice(0, 8)} gpu=${gpu}`, true);

  const interval = getSetting("ModalBridge.pollIntervalSec", 1.2) * 1000;
  const timeoutMs = getSetting("ModalBridge.timeoutSec", 1800) * 1000;  // 默认 1800s=30min,覆盖最慢类别(视频),与 worker 超时上限一致
  const deadline = Date.now() + timeoutMs;

  let final = null;
  let lastStatus = "queued";
  try {
    while (Date.now() < deadline) {
      if (cancelled) return { jobId, gpu, cancelled: true };  // 用户已取消,卡片已结束,静默退出
      await sleep(interval);
      if (cancelled) return { jobId, gpu, cancelled: true };
      let pData;
      try {
        const pRes = await api.fetchApi(`/modal_bridge/poll?job_id=${encodeURIComponent(jobId)}`);
        pData = await pRes.json();
      } catch (e) {
        log("poll error (will retry)", e);
        continue;
      }
      // 只有「纯接口错误」(有 error 但没有 status)才当临时错误重试。
      // job 执行失败的响应也带 error,但同时有 status:"failed" —— 不能在这里 continue,
      // 否则漏掉终态、一直 poll 到超时,把真正的失败原因吞掉(报错不回前端的根因)。
      if (pData.error && !pData.status) {
        log("poll resp error (will retry):", pData);
        continue;
      }
      if (pData.status !== lastStatus) {
        lastStatus = pData.status;
        log("status →", pData.status);
        if (pData.status === "running") {
          ctx.stage("running", `${batchSuffix}inference (gpu=${gpu})`, true);
        } else if (pData.status === "queued") {
          ctx.stage("queued", `${batchSuffix}Waiting for worker...`, true);
        }
      }
      if (pData.status === "completed" || pData.status === "failed" || pData.status === "cancelled") {
        final = pData;
        break;
      }
    }
  } finally {
    removeActiveJob(jobId);
  }
  if (!final) {
    reportJobEvent(jobId, "polling_timed_out", `前端等待超时;Modal 可能仍在跑,图若出会在 output/`);
    throw new Error(`[job ${jobId}] Polling timed out(前端等待超时;Modal 可能仍在跑,稍后看 output/modal_results/)`);
  }
  if (final.status === "cancelled") {
    reportJobEvent(jobId, "cancelled", "");
    throw new Error(`[job ${jobId}] Job cancelled`);
  }
  if (final.status === "failed") {
    reportJobEvent(jobId, "worker_failed", final.error || "");
    throw new Error(`[job ${jobId}] ${final.error || "Modal worker failed"}`);
  }

  ctx.stage("downloading", `${batchSuffix}Decoding base64...`, false);
  const fetchRes = await api.fetchApi("/modal_bridge/fetch_result", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId, modal_state: final }),
  });
  const fetched = await fetchRes.json();
  if (!fetchRes.ok || !fetched.ok) {
    throw new Error(`[job ${jobId}] ${fetched.error || `fetch HTTP ${fetchRes.status}`}`);
  }
  // ComfyUI 单 graph:提交时的工作流当前在前台才能直接回填;在后台 tab 的先暂存,
  // 等用户切回该 tab 再渲染(图始终也在 output 里,一张不丢)。
  const sf = fetched.outputs?.[0]?.subfolder || "modal_results";
  const has3d = (fetched.outputs || []).some((o) => MODEL3D_EXT_RE.test(o.filename || ""));
  const wfKey = submitGuard?.wfKey;
  const onFront = wfKey == null || activeWorkflowKey() === wfKey;
  if (onFront) {
    const placed = displayInGraph(outputNodeIds, fetched.outputs);
    if (!placed && fetched.outputs?.length && !has3d) {  // 3D 已有专门提示,不再叠 saved_no_node
      notify(t("toast.saved_no_node", { sf }), "warn");
    }
  } else if (fetched.outputs?.length) {
    storePendingResult(wfKey, outputNodeIds, fetched.outputs);
    notify(t("toast.bg_done", { sf }), "info");
  }
  if (has3d) notify(t("toast.saved_3d", { sf }), "info");  // 3D 产物:画板不渲染,明确提示文件位置
  return { jobId, gpu, outputs: fetched.outputs };
}

// =====================================================================
// 模型自动同步(在 submit 前):本地 → Modal Volume
// 模型都在本地 ComfyUI Desktop 下好。提交前查 Volume 缺哪些:本地有的直接上传(块级
// 去重,通用大模型秒过);Volume 和本地都没有的 → 提示先在本地下好,允许强行提交。
// =====================================================================
async function ensureModelsAvailable(prompt, ctx) {
  ctx.stage("checking", "Scanning workflow + Modal Volume...");

  const checkRes = await api.fetchApi("/modal_bridge/check_models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  const check = await checkRes.json();
  if (check.error) throw new Error(check.error);

  const { required = [], present = [], missing_local = [], downloading = [], missing_no_source = [] } = check;
  ctx.stage("checking", t("mdl.on_volume", { present: present.length, total: required.length }));
  log(`models: required=${required.length} present=${present.length} missing_local=${missing_local.length} ` +
      `downloading=${downloading.length} missing_no_source=${missing_no_source.length}`);

  // 本地还在下载中的模型 → 不能传(会传成残缺),提示等下完
  if (downloading.length) {
    const list = downloading.map((u) => `  ${u.type}/${u.filename}`).join("\n");
    if (!confirm(t("mdl.downloading", { list }))) throw new Error(t("mdl.cancel_dl"));
  }

  // Volume 和本地都没有 → 没法自动补
  if (missing_no_source.length) {
    const list = missing_no_source.map((u) => `  ${u.type}/${u.filename}`).join("\n");
    if (!confirm(t("mdl.no_source", { list }))) throw new Error(t("mdl.cancel_miss"));
  }

  if (!missing_local.length) {
    ctx.stage("checking", t("mdl.all_present", { present: present.length, total: required.length }));
    return;
  }

  // 本地有、Volume 没 → 上传
  const totalMb = missing_local.reduce((s, m) => s + (m.size_mb || 0), 0);
  const list = missing_local.map((m) => `  • ${m.type}/${m.filename} (${m.size_mb} MB)`).join("\n");
  if (!confirm(t("mdl.upload_confirm", { mb: totalMb, list }))) {
    return; // 用户选择不传,直接提交(可能失败,交给 Modal 报错)
  }

  ctx.stage("uploading", t("mdl.uploading", { n: missing_local.length }), false);
  ctx.bar(STATUS_PROGRESS.uploading[0]);
  const [a, b] = STATUS_PROGRESS.uploading;
  let tick = 0;
  const rc = await streamPost(
    "/modal_bridge/sync_models",
    { items: missing_local.map((m) => ({ type: m.type, filename: m.filename, local_path: m.local_path, size_mb: m.size_mb })) },
    (line) => {
      log("upload:", line);
      tick++;
      ctx.bar(a + Math.min(b - a - 1, tick * 0.6));
      ctx.stage("uploading", line.length > 72 ? line.slice(0, 72) + "…" : line);
    },
  );
  if (rc !== 0) throw new Error(t("mdl.upload_fail"));
  ctx.stage("uploading", t("mdl.synced", { n: missing_local.length }), false);
}

// =====================================================================
// 主入口:批量包装
// =====================================================================
// GPU 显存表(GB)。键与 Setup 下拉/后端 default_gpu 一致。
const GPU_VRAM = { "L40S": 48, "A100-80GB": 80, "H100": 80, "H200": 141, "B200": 180 };

// 显存预警:点 Modal 前用「模型总显存 ×1.15」对比所选显卡。超了弹确认。
// 返回 true=继续, false=用户中止(去换显卡)。任何异常都放行 —— 预警是辅助,不该挡正常流程。
async function vramPreflightOrConfirm(prompt, cfgNow) {
  try {
    // Auto(更省钱)模式:大工作流会自动升到顶配卡(B200),所以预警上限按顶配卡;
    // 固定模式(H100/B200):上限就是所选主卡,超了提示用户切到 Auto 或更大的卡。
    const auto = cfgNow.auto_downgrade !== false;
    const gpu = auto ? (cfgNow.top_gpu || "B200") : (cfgNow.default_gpu || "H100");
    const cap = GPU_VRAM[gpu];
    if (!cap) return true;
    const r = await api.fetchApi("/modal_bridge/estimate_vram", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const d = await r.json();
    if (!r.ok || d.error || !d.total_mb) return true;  // 估不出就不挡
    const modelGB = d.total_mb / 1024;
    // 显存估算交后端按类别算(视频=权重×系数+多帧激活开销);拿不到则兜底旧的 ×1.15。
    const estGB = (d.est_vram_gb != null) ? d.est_vram_gb : modelGB * 1.15;
    if (estGB <= cap) return true;  // 没超 → 放行
    let unknownNote = (d.unknown && d.unknown.length)
      ? t("vram.warn.unknown", { n: d.unknown.length }) : "";
    if (d.category === "video") unknownNote += t("vram.warn.video");
    return await confirmDialog(
      t("vram.warn.title"),
      t("vram.warn.body", { est: estGB.toFixed(0), model: modelGB.toFixed(0), gpu, cap }) + unknownNote,
      t("vram.warn.run"), t("vram.warn.switch"),
    );
  } catch (e) { log("vram preflight skipped:", e); return true; }
}

// 轻量 async 确认弹窗,返回 Promise<boolean>(主按钮=true / 次按钮或点遮罩=false)。
function confirmDialog(title, body, okText, cancelText, opts = {}) {
  const cancelBg = opts.dangerCancel ? "#b91c1c" : "#374151";
  const cancelFg = opts.dangerCancel ? "#fff" : "#ddd";
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    Object.assign(ov.style, { position: "fixed", inset: "0", zIndex: "10002",
      background: "rgba(0,0,0,0.55)", display: "flex", alignItems: "center", justifyContent: "center" });
    const box = document.createElement("div");
    Object.assign(box.style, { background: "#1e1e1e", color: "#eee", width: "440px", maxWidth: "92vw",
      borderRadius: "10px", padding: "20px", font: "13px/1.6 system-ui,sans-serif",
      boxShadow: "0 10px 40px rgba(0,0,0,0.5)" });
    box.innerHTML = `
      <div style="font-size:15px;font-weight:600;margin-bottom:8px;color:#fbbf24;">${title}</div>
      <div style="color:#ddd;margin-bottom:16px;white-space:pre-line;">${body}</div>
      <div style="text-align:right;">
        <button id="mb-vram-cancel" style="padding:7px 14px;margin-right:8px;background:${cancelBg};color:${cancelFg};border:none;border-radius:6px;cursor:pointer;">${cancelText}</button>
        <button id="mb-vram-ok" style="padding:7px 16px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">${okText}</button>
      </div>`;
    ov.appendChild(box); document.body.appendChild(ov);
    const done = (v) => { ov.remove(); resolve(v); };
    box.querySelector("#mb-vram-ok").onclick = () => done(true);
    box.querySelector("#mb-vram-cancel").onclick = () => done(false);
    ov.onclick = (e) => { if (e.target === ov) done(false); };
  });
}

async function queueOnModal() {
  // ⭐ 一进来立刻锁定"此刻点的这张图":同步抓工作流身份 + 立即序列化 prompt。
  // 必须在任何 await(fetchConfig / 版本检查)之前——否则那几秒里用户切了 tab,
  // 本任务会懒读到"当前图"=别的工作流,造成并发跨 tab 串台(卡片显示别人的模型上传)。
  const wfName = activeWorkflowName();
  const wfKey = activeWorkflowKey();
  const hasApiNodes = workflowHasApiNodes();
  const p = await app.graphToPrompt();
  const outputNodeIds = findOutputNodes(p.output);

  // 没部署/没配置就直接引导去 Setup,别走后面链路再失败
  const cfg0 = await fetchConfig();
  if (!isConfigured(cfg0)) {
    notify(t("toast.not_deployed"), "warn");
    try { openDeployDialog(); } catch (e) {}
    return;
  }
  // 先立刻出进度卡,再做云端版本检查:checkVersionOrBlock 打 /version→/health(冷启动/超时最长 6s),
  // 放到 UI 之后,避免"点了 RunModal 几秒没反应"像卡死。每次点击 = 一个独立 job 卡片(多 workflow 并发互不覆盖)。
  const ctx = newProgress("preparing", wfName);
  ctx.stage("checking", t("ver.checking"));
  // ⭐ 版本契约:插件版本 vs 云端部署版本必须一致,否则拦截(防"升级了插件没重新部署")
  if (!(await checkVersionOrBlock())) { ctx.finish(false, "✕"); return; }
  ctx.stage("preparing", "Serializing graph...");
  try {
    // p / outputNodeIds / wfKey / hasApiNodes 已在函数开头快照(防跨 tab 串台),这里直接用。
    const submitGuard = { wfKey };  // 结果回填也用这份身份:切了 tab 就暂存,不填到别的工作流
    log("output nodes:", outputNodeIds, "wfKey:", wfKey);

    // API 节点预警:工作流含 ComfyUI API 节点但没配 comfy.org key → 云端会 401,提前提示(早于节点/模型同步)
    if (hasApiNodes && !cfg0.has_comfy_api_key) {
      const proceed = await confirmDialog(
        t("api.warn.title"), t("api.warn.body"), t("api.warn.run"), t("api.warn.setup"));
      if (!proceed) { ctx.finish(false, "✕ Cancelled"); try { openDeployDialog(); } catch (e) {} return; }
    }

    // ⭐ custom_node 自动同步(默认开启,Settings 可关)
    const autoCheckNodes = getSetting("ModalBridge.autoCheckNodes", true);
    if (autoCheckNodes) {
      const proceed = await ensureNodesAvailable(p.output, ctx);
      if (!proceed) {
        ctx.finish(false, "✕ Cancelled");
        return;
      }
    }

    // ⭐ 显存预警:模型总显存×1.15 超所选显卡 → 弹确认(可继续 / 去换卡)
    const okVram = await vramPreflightOrConfirm(p.output, cfg0);
    if (!okVram) {
      ctx.finish(false, "✕ Cancelled");
      try { openDeployDialog(); } catch (e) {}
      return;
    }

    // ⭐ 模型自动同步:本地 → Volume(默认开启,Settings 可关)
    const autoSync = getSetting("ModalBridge.autoSyncModels", true);
    if (autoSync) {
      await ensureModelsAvailable(p.output, ctx);
    }

    const batchCount = Math.max(1, parseInt(getSetting("ModalBridge.batchCount", 1), 10));
    log("batch count:", batchCount);

    const allOutputs = [];
    for (let i = 0; i < batchCount; i++) {
      const seed = Date.now() % 2147483647 + i * 7919;
      const promptWithSeed = batchCount > 1 ? reseedPrompt(p.output, seed) : p.output;
      const result = await runOnceOnModal(
        promptWithSeed, outputNodeIds, ctx, submitGuard,
        batchCount > 1 ? { current: i + 1, total: batchCount } : null,
      );
      if (result?.cancelled) return;  // 用户取消:卡片已 finish,别再 finish(true) 覆盖
      allOutputs.push(result);
    }

    ctx.finish(true, batchCount > 1 ? `✓ ${batchCount} done` : "✓ Done");
    const wf = ctx.wfName ? `「${ctx.wfName}」` : "";
    notify(
      batchCount > 1
        ? t("toast.done_n", { wf, n: batchCount })
        : t("toast.done", { wf, id: allOutputs[0].jobId.slice(0, 8) }),
      "success",
    );
  } catch (e) {
    err(e);
    ctx.finish(false, "✗ " + (e.message || "Error").slice(0, 40), e.stack || e.toString());
    const wf = ctx.wfName ? `「${ctx.wfName}」` : "";
    notify(t("toast.fail", { wf, msg: e.message }), "error");
  }
}

// =====================================================================
// 健康检查 + 启动时检查未完成 job(history 持久化)
// =====================================================================
async function doHealthCheck() {
  try {
    const r = await api.fetchApi("/modal_bridge/health");
    const h = await r.json();
    log("health check", h);
  } catch (e) {
    err("health check failed", e);
  }
}

// RunModal 按钮随部署状态变色:就绪(云端可达 + 本地↔云端版本一致 + 显卡一致)= 白,
// 否则 = 灰(默认外观,提示"没部署好 / 需重新部署")。状态由各处已有的 /version 顺带更新,
// 不新增轮询;一个轻量定时器只按缓存状态重涂,防 ComfyUI 重渲染动作栏后样式丢失。
let _runReady = false;
function applyRunButtonStyle() {
  const el = document.querySelector(
    `button[aria-label="${BUTTON_TOOLTIP}"], button[title="${BUTTON_TOOLTIP}"]`
  );
  if (!el) return;
  if (_runReady) {
    el.style.color = "#ffffff";   // 就绪 → 白
    el.style.opacity = "1";
  } else {
    el.style.color = "";          // 还原默认(灰)
    el.style.opacity = "";
  }
}
function setRunReady(v) {
  // 就绪条件 = checkVersionOrBlock 的放行条件:可达 + 版本一致 + 显卡一致
  _runReady = !!(v && v.reachable && v.match && v.gpu_match !== false);
  applyRunButtonStyle();
}
async function refreshRunReady() {
  try {
    setRunReady(await (await api.fetchApi("/modal_bridge/version")).json());
  } catch (e) { setRunReady(null); }
}

async function recoverPendingJob() {
  let pending = loadLS(LS_KEYS.activeJob);
  pending = Array.isArray(pending) ? pending : (pending ? [pending] : []);
  // 丢弃过期的(>20min),其余各自恢复(并行,每个一张卡)
  const fresh = pending.filter((j) => j?.jobId && (Date.now() - j.startedAt) / 1000 <= 1200);
  saveLS(LS_KEYS.activeJob, fresh);
  for (const j of fresh) recoverOne(j);
}

async function recoverOne(pending) {
  log("recovering pending job:", pending.jobId);
  const ctx = newProgress("queued", pending.wfName || null);
  ctx.stage("queued", `recover job=${pending.jobId.slice(0, 8)}`);
  try {
    const pRes = await api.fetchApi(`/modal_bridge/poll?job_id=${encodeURIComponent(pending.jobId)}`);
    const pData = await pRes.json();
    if (pData.status === "completed") {
      ctx.stage("downloading", "Fetching result of recovered job...");
      const fr = await api.fetchApi("/modal_bridge/fetch_result", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: pending.jobId, modal_state: pData }),
      });
      const fd = await fr.json();
      // 恢复时画板多半已不是当时的工作流 → 不强行回填,提示存盘路径
      if (fd.ok) {
        const sf = fd.outputs?.[0]?.subfolder || "modal_results";
        ctx.finish(true, "✓ Recovered");
        notify(t("toast.recovered", { wf: pending.wfName ? "「" + pending.wfName + "」" : "", sf }), "success");
      } else {
        ctx.finish(false, "✗ Recover fetch failed", JSON.stringify(fd));
      }
    } else if (pData.status === "running" || pData.status === "queued") {
      ctx.stage(pData.status, t("stage.still", { status: pData.status }));
      notify(t("toast.still", { id: pending.jobId.slice(0, 8), status: pData.status }), "warn");
      ctx.finish(true, `· ${pData.status}`);
    } else {
      ctx.finish(false, `✗ ${pData.status}`);
    }
  } catch (e) {
    err("recover failed", e);
    ctx.finish(false, "✗ recover error");
  } finally {
    removeActiveJob(pending.jobId);
  }
}

// =====================================================================
// 注册 ComfyUI Settings
// =====================================================================
// 内存快照开关:Setting 存前端,但部署读后端 config.json → onChange 要同步写回 config。
// _snapReady:ComfyUI 启动期会用存储值触发 onChange,先挡掉,避免在 setup() 用 config 初始化前回写覆盖。
let _snapReady = false;
async function syncSnapshotToConfig(value) {
  if (!_snapReady) return;  // 启动期/初始化触发的 onChange 不回写
  try {
    await api.fetchApi("/modal_bridge/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enable_snapshot: !!value }),
    });
    log("enable_snapshot →", !!value, "(写回 config;重新部署后生效)");
    notify(value ? t("set.snapshot.on") : t("set.snapshot.off"), "info");
  } catch (e) { err("sync snapshot to config failed", e); }
}

const SETTINGS = [
  // 注:GPU 已统一为 H100→A100-80GB(原生 fallback),不再有显存档选项。
  {
    id: "ModalBridge.batchCount",
    name: "Modal Bridge: Batch count",
    type: "number",
    defaultValue: 1,
    attrs: { min: 1, max: 20, step: 1 },
    tooltip: t("set.batch"),
  },
  {
    id: "ModalBridge.pollIntervalSec",
    name: "Modal Bridge: Poll interval (sec)",
    type: "number",
    defaultValue: 1.2,
    attrs: { min: 0.5, max: 10, step: 0.1 },
    tooltip: t("set.poll"),
  },
  {
    id: "ModalBridge.timeoutSec",
    name: "Modal Bridge: Timeout (sec)",
    type: "number",
    defaultValue: 900,
    attrs: { min: 60, max: 7200, step: 60 },
    tooltip: t("set.timeout"),
  },
  {
    id: "ModalBridge.incognito",
    name: "Modal Bridge: Incognito (return base64, skip R2)",
    type: "boolean",
    defaultValue: true,
    tooltip: t("set.incognito"),
  },
  {
    id: "ModalBridge.autoSyncModels",
    name: "Modal Bridge: Auto-sync models (local → Volume)",
    type: "boolean",
    defaultValue: true,
    tooltip: t("set.autosync_models"),
  },
  {
    id: "ModalBridge.autoCheckNodes",
    name: "Modal Bridge: Auto-sync custom nodes",
    type: "boolean",
    defaultValue: true,
    tooltip: t("set.autosync_nodes"),
  },
  {
    id: "ModalBridge.enableSnapshot",
    name: "Modal Bridge: Memory snapshot (experimental, faster cold start)",
    type: "boolean",
    defaultValue: true,
    tooltip: t("set.snapshot"),
    onChange: (v) => syncSnapshotToConfig(v),
  },
];

// =====================================================================
// GUI 一键部署对话框(零终端:后端自动 pip 装 modal → 建 secret → deploy → 写 config)
// =====================================================================
async function fetchConfig() {
  try {
    const r = await api.fetchApi("/modal_bridge/config");
    return await r.json();
  } catch (e) { return {}; }
}

function isConfigured(cfg) {
  return (
    typeof cfg?.modal_token_id === "string" && cfg.modal_token_id.startsWith("ak-") &&
    typeof cfg?.modal_endpoint_base === "string" && !cfg.modal_endpoint_base.includes("YOUR_WORKSPACE")
  );
}

// 版本契约:本地插件版本 vs 云端部署版本不一致 → 拦截提交、引导重新部署。
// 返回 true=放行 / false=拦截。version 检查本身出错(网络等)不拦(放行,别误伤)。
async function checkVersionOrBlock() {
  // 不弹"检查中"提示(正常云端在时 <1s 返回、直接出图,用户无感;弹了反而烦)。
  // 只有连不上/版本不一致才提示(下面)。
  let v;
  try {
    const r = await api.fetchApi("/modal_bridge/version");
    v = await r.json();
  } catch (e) {
    log("version check failed, skip:", e);
    return true;  // 检查本身失败不阻塞
  }
  setRunReady(v);  // 顺便据此把 RunModal 按钮置白(就绪)/灰(需部署)
  if (v.match && v.gpu_match !== false) {
    // 本机 ComfyUI 升级过、云端还是旧版 → 只警告不拦(stale 但能跑),提示重新部署让云端跟上
    if (v.comfyui_match === false) {
      notify(t("ver.comfyui_changed_toast", { local: v.local_comfyui || "?", deployed: v.deploy_comfyui || "?" }), "warn");
    }
    return true;  // 版本 + 显卡都一致,放行
  }

  // 版本一致但显卡改了没重新部署(云端真跑的卡 ≠ 所选)→ 强制重新部署,不让在旧卡上偷偷跑
  if (v.match && v.gpu_match === false) {
    notify(t("ver.gpu_mismatch_toast", { local: v.local_gpu, deployed: v.deployed_gpu }), "warn");
    if (confirm(t("ver.gpu_mismatch_msg", { local: v.local_gpu, deployed: v.deployed_gpu }))) {
      try { openDeployDialog(); } catch (e) {}
    }
    return false;  // 拦截:必须重新部署
  }

  // 连不上:先查 Modal 官方状态页(权威)判断是不是平台故障。
  // 平台故障 → 引导查状态页(部署也会失败,等恢复);否则按 err_kind 当没部署 → 引导部署。
  if (!v.reachable) {
    const outage = await isModalOutage();  // 查 status.modal.com 的 aggregate_state
    const platform = outage || v.err_kind === "timeout" || v.err_kind === "unreachable";
    notify(platform ? t("ver.platform_toast") : t("ver.notdeployed_toast"), "warn");
    if (confirm(platform ? t("ver.platform_msg") : t("ver.notdeployed_msg"))) {
      if (platform) { try { window.open("https://status.modal.com", "_blank"); } catch (e) {} }
      else { try { openDeployDialog(); } catch (e) {} }
    }
  } else {
    // 版本不一致(连得上,但本地↔云端版本不同)→ 引导重新部署
    notify(t("ver.mismatch_toast", { local: v.local, deployed: v.deployed }), "warn");
    if (confirm(t("ver.mismatch_msg", { local: v.local, deployed: v.deployed }))) {
      try { openDeployDialog(); } catch (e) {}
    }
  }
  return false;  // 拦截:不提交
}

let deployDialogEl = null;
let _dialogLang = null;  // 对话框创建时的语言;切语言后重开需重建,否则文案停在旧语言

// 重新拉 /version 并更新 Setup 对话框顶部的版本徽标(部署成功 / 测试连接后调)
async function refreshVerBanner(panel) {
  const el = panel?.querySelector?.("#mb-dep-ver");
  if (!el) return;
  let v;
  try { v = await (await api.fetchApi("/modal_bridge/version")).json(); } catch (e) { return; }
  setRunReady(v);  // Setup 里刷新版本时,顺带更新 RunModal 按钮颜色
  const dep = v.reachable ? (v.deployed || "unknown") : t("dlg.ver.notconn");
  const color = v.match ? "#34d399" : (v.reachable ? "#fbbf24" : "#9aa");
  const hint = v.match ? t("dlg.ver.aligned")
    : (v.reachable ? t("dlg.ver.mismatch") : t("dlg.ver.unreach"));
  el.innerHTML = `${t("dlg.ver.local")}<b style="color:#eee;">${v.local}</b>　·` +
    ` ${t("dlg.ver.deployed")}<b style="color:${color};">${dep}</b> <span style="color:${color};">${hint}</span>`;
}

async function openDeployDialog() {
  // 缓存的对话框:语言没变才复用;语言变了则销毁重建(否则文案停在旧语言)
  if (deployDialogEl && _dialogLang === _locale()) {
    deployDialogEl.style.display = "flex";
    refreshVerBanner(deployDialogEl.querySelector("div"));  // 重开时刷新版本徽标
    return;
  }
  if (deployDialogEl) { deployDialogEl.remove(); deployDialogEl = null; }  // 语言变了,弃旧重建
  _dialogLang = _locale();
  const cfg = await fetchConfig();   // 本地 /config,快
  // 版本徽标改成异步填(见对话框末尾 refreshVerBanner):不再在这里 await /version,
  // 否则首次打开要先等云端 /health(冷启动/超时最长 6s),对话框迟迟不出 → 像卡死。
  const ver = { local: "?", deployed: null, match: false, reachable: false };

  const overlay = document.createElement("div");
  deployDialogEl = overlay;
  Object.assign(overlay.style, {
    position: "fixed", inset: "0", zIndex: "10001",
    background: "rgba(0,0,0,0.5)", display: "flex",
    alignItems: "center", justifyContent: "center",
  });

  const panel = document.createElement("div");
  Object.assign(panel.style, {
    background: "#1e1e1e", color: "#eee", width: "560px", maxWidth: "92vw",
    maxHeight: "88vh", overflow: "auto", borderRadius: "10px", padding: "20px",
    boxShadow: "0 10px 40px rgba(0,0,0,0.5)", font: "13px/1.5 system-ui,sans-serif",
  });
  const inputCss =
    "width:100%;box-sizing:border-box;margin:4px 0 10px;padding:7px 9px;" +
    "background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#eee;font:13px monospace;";
  // 版本对齐徽标:绿=一致 / 黄=不一致(需重新部署)/ 灰=云端连不上
  const vDeployed = ver.reachable ? (ver.deployed || "unknown") : t("dlg.ver.notconn");
  const vColor = ver.match ? "#34d399" : (ver.reachable ? "#fbbf24" : "#9aa");
  const vHint = ver.match ? t("dlg.ver.aligned")
    : (ver.reachable ? t("dlg.ver.mismatch") : t("dlg.ver.unreach"));
  panel.innerHTML = `
    <div style="font-size:16px;font-weight:600;margin-bottom:4px;">${t("dlg.title")}</div>
    <div id="mb-dep-ver" style="margin-bottom:10px;font-size:12px;padding:6px 10px;border-radius:6px;
         background:#222;border:1px solid #383838;">
      ${t("dlg.ver.local")}<b style="color:#eee;">${ver.local}</b>　·
      ${t("dlg.ver.deployed")}<b style="color:${vColor};">${vDeployed}</b>
      <span style="color:${vColor};">${vHint}</span>
    </div>
    <div style="color:#9aa;margin-bottom:14px;">
      ${t("dlg.intro")}
      <a href="https://modal.com/settings/tokens" target="_blank" style="color:#6cf;">modal.com/settings/tokens</a>
    </div>
    <label>Workspace <span style="color:#9aa;">${t("dlg.ws.hint")}</span></label>
    <input id="mb-dep-ws" type="text" style="${inputCss}" value="${cfg.modal_workspace || ""}" placeholder="your-workspace">
    <label>Token ID <span style="color:#9aa;">(ak-...)</span></label>
    <input id="mb-dep-id" type="text" style="${inputCss}" value="${cfg.modal_token_id || ""}" placeholder="ak-xxxxxxxx">
    <label>Token Secret <span style="color:#9aa;">(as-...${cfg.has_token_secret ? t("dlg.secret.saved") : ""})</span></label>
    <input id="mb-dep-secret" type="password" style="${inputCss}" value="" placeholder="${cfg.has_token_secret ? t("dlg.secret.ph_saved") : "as-xxxxxxxx"}">
    <label>GPU <span style="color:#9aa;">${t("dlg.gpu.label")}</span></label>
    <select id="mb-dep-gpumode" style="${inputCss}">
      <option value="auto"${(cfg.auto_downgrade!==false)?" selected":""}>${t("dlg.gpu.opt_auto")}</option>
      <option value="h100"${(cfg.auto_downgrade===false && (cfg.default_gpu||"H100")!=="B200")?" selected":""}>${t("dlg.gpu.opt_h100")}</option>
      <option value="b200"${(cfg.auto_downgrade===false && (cfg.default_gpu||"H100")==="B200")?" selected":""}>${t("dlg.gpu.opt_b200")}</option>
    </select>
    <div style="margin:0 0 10px;color:#9aa;font-size:12px;">${t("dlg.gpu.note")}</div>
    <label>comfy.org API Key <span style="color:#9aa;">${t("dlg.comfy.hint")}</span></label>
    <input id="mb-dep-comfy" type="password" style="${inputCss}" value="" placeholder="${cfg.has_comfy_api_key ? t("dlg.comfy.ph_saved") : t("dlg.comfy.ph")}">
    <div style="margin:0 0 10px;color:#9aa;font-size:12px;">${t("dlg.comfy.note")}</div>
    <div style="margin:10px 0;">
      <button id="mb-dep-go" style="padding:8px 18px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">${t("dlg.btn.deploy")}</button>
      <button id="mb-dep-test" style="padding:8px 14px;margin-left:8px;background:#374151;color:#ddd;border:none;border-radius:6px;cursor:pointer;">${t("dlg.btn.test")}</button>
      <button id="mb-dep-close" style="padding:8px 14px;margin-left:8px;background:#333;color:#ccc;border:none;border-radius:6px;cursor:pointer;">${t("dlg.btn.close")}</button>
      <span id="mb-dep-status" style="margin-left:12px;color:#9aa;"></span>
    </div>
    <pre id="mb-dep-log" style="display:none;background:#111;border:1px solid #333;border-radius:6px;padding:10px;max-height:280px;overflow:auto;white-space:pre-wrap;font:11px/1.4 monospace;color:#bdbdbd;"></pre>

    <div style="margin-top:16px;border-top:1px solid #333;padding-top:12px;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <span style="font-weight:600;">${t("dlg.nodes.title")}</span>
        <button id="mb-nodes-load" style="padding:5px 12px;background:#374151;color:#ddd;border:none;border-radius:6px;cursor:pointer;font-size:12px;">${t("dlg.nodes.load")}</button>
      </div>
      <div style="color:#9aa;margin-top:4px;font-size:12px;">
        ${t("dlg.nodes.warn")}
      </div>
      <div id="mb-nodes-list" style="margin-top:8px;max-height:200px;overflow:auto;"></div>
      <div style="margin-top:8px;">
        <button id="mb-nodes-prune" style="display:none;padding:7px 14px;background:#7f1d1d;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">${t("dlg.nodes.prune")}</button>
        <span id="mb-nodes-status" style="margin-left:10px;color:#9aa;font-size:12px;"></span>
      </div>
      <pre id="mb-nodes-log" style="display:none;background:#111;border:1px solid #333;border-radius:6px;padding:10px;margin-top:8px;max-height:200px;overflow:auto;white-space:pre-wrap;font:11px/1.4 monospace;color:#bdbdbd;"></pre>
    </div>
  `;
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  // 版本徽标异步填:先显示"检查云端中…"立刻出对话框,再异步刷新真实版本(不 await /health)
  const _verEl = panel.querySelector("#mb-dep-ver");
  if (_verEl) _verEl.textContent = t("ver.checking");
  refreshVerBanner(panel);

  const close = () => { overlay.style.display = "none"; };
  panel.querySelector("#mb-dep-close").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  const goBtn = panel.querySelector("#mb-dep-go");
  const testBtn = panel.querySelector("#mb-dep-test");
  const statusEl = panel.querySelector("#mb-dep-status");
  const logEl = panel.querySelector("#mb-dep-log");

  // 测试连接:真打一次 Modal /health,查出"app 被删 / endpoint 不通 / key 不对"——
  // 这些光看本地 config 有没有 token 是查不出的(config 字段在不代表云端 app 还活着)。
  testBtn.onclick = async () => {
    testBtn.disabled = true;
    statusEl.textContent = t("test.running");
    statusEl.style.color = "#9aa";
    logEl.style.display = "block";
    logEl.textContent = "GET /modal_bridge/health …\n";
    try {
      const r = await api.fetchApi("/modal_bridge/health");
      const data = await r.json();
      // 只打 modal/error,不打 data.config(里面含 token)
      logEl.textContent += JSON.stringify(data.modal ?? { error: data.error ?? "unknown" }, null, 2) + "\n";
      logEl.scrollTop = logEl.scrollHeight;
      if (r.ok && data.ok && data.modal?.healthy) {
        const m = data.modal;
        const nodes = Array.isArray(m.custom_nodes) ? m.custom_nodes.length : "?";
        statusEl.textContent = t("test.ok", { ver: m.deployed_version ?? "?", warm: m.warm_containers ?? 0, nodes });
        statusEl.style.color = "#34d399";
        refreshVerBanner(panel);  // 顺带刷新顶部版本徽标
      } else {
        const why = data.error || t("test.unreach");
        statusEl.textContent = t("test.fail", { why: String(why).slice(0, 80) });
        statusEl.style.color = "#f87171";
      }
    } catch (e) {
      statusEl.textContent = t("test.err", { e: e.message || e });
      statusEl.style.color = "#f87171";
    } finally {
      testBtn.disabled = false;
    }
  };

  // ---- 管理云端节点(手动清理 / prune)----
  const nodesLoadBtn = panel.querySelector("#mb-nodes-load");
  const nodesListEl = panel.querySelector("#mb-nodes-list");
  const nodesPruneBtn = panel.querySelector("#mb-nodes-prune");
  const nodesStatusEl = panel.querySelector("#mb-nodes-status");
  const nodesLogEl = panel.querySelector("#mb-nodes-log");
  let loadedNodes = [];  // [{name,url,commit}]

  nodesLoadBtn.onclick = async () => {
    nodesLoadBtn.disabled = true;
    nodesStatusEl.textContent = t("mn.loading");
    nodesStatusEl.style.color = "#9aa";
    try {
      const r = await api.fetchApi("/modal_bridge/list_nodes");
      const d = await r.json();
      loadedNodes = d.nodes || [];
      if (!loadedNodes.length) {
        nodesListEl.innerHTML = `<div style="color:#9aa;font-size:12px;">${t("mn.empty")}</div>`;
        nodesPruneBtn.style.display = "none";
      } else {
        nodesListEl.innerHTML = loadedNodes.map((n, i) =>
          `<label style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px;cursor:pointer;">
             <input type="checkbox" class="mb-node-cb" data-i="${i}">
             <span>${n.name}</span>
             ${n.in_local_baked ? "" : `<span style="color:#fbbf24;font-size:10px;">${t("mn.nogit_tag")}</span>`}
           </label>`).join("");
        nodesPruneBtn.style.display = "inline-block";
      }
      nodesStatusEl.textContent = t("mn.installed", { n: loadedNodes.length, src: d.source });
      nodesStatusEl.style.color = "#9aa";
    } catch (e) {
      nodesStatusEl.textContent = t("mn.load_fail", { e: e.message || e });
      nodesStatusEl.style.color = "#f87171";
    } finally {
      nodesLoadBtn.disabled = false;
    }
  };

  nodesPruneBtn.onclick = async () => {
    const checked = [...panel.querySelectorAll(".mb-node-cb:checked")]
      .map((cb) => loadedNodes[parseInt(cb.dataset.i, 10)]);
    if (!checked.length) { nodesStatusEl.textContent = t("mn.none_checked"); return; }
    const removeNames = new Set(checked.map((n) => n.name));
    const keep = loadedNodes.filter((n) => !removeNames.has(n.name));
    const list = checked.map((n) => "  • " + n.name).join("\n");
    if (!confirm(t("mn.confirm", { n: checked.length, list }))) return;

    nodesPruneBtn.disabled = true;
    nodesStatusEl.textContent = t("nodes.redeploying");
    nodesStatusEl.style.color = "#9aa";
    nodesLogEl.style.display = "block";
    nodesLogEl.textContent = "";
    try {
      const rc = await streamPost("/modal_bridge/sync_nodes", {
        new_baked: keep.map((n) => ({ name: n.name, url: n.url, commit: n.commit })),
        summary: { add: 0, update: 0, prune: checked.length },
      }, (line) => { nodesLogEl.textContent += line + "\n"; nodesLogEl.scrollTop = nodesLogEl.scrollHeight; });
      if (rc === 0) {
        nodesStatusEl.textContent = t("mn.removed", { n: checked.length, keep: keep.length });
        nodesStatusEl.style.color = "#34d399";
        notify(t("mn.removed_toast", { n: checked.length }), "success");
        nodesLoadBtn.onclick();  // 刷新列表
      } else {
        nodesStatusEl.textContent = t("mn.redeploy_fail", { rc });
        nodesStatusEl.style.color = "#f87171";
      }
    } catch (e) {
      nodesStatusEl.textContent = "✗ " + (e.message || e);
      nodesStatusEl.style.color = "#f87171";
    } finally {
      nodesPruneBtn.disabled = false;
    }
  };

  goBtn.onclick = async () => {
    const gpuMode = panel.querySelector("#mb-dep-gpumode").value;  // auto | h100 | b200
    const payload = {
      workspace: panel.querySelector("#mb-dep-ws").value.trim(),
      token_id: panel.querySelector("#mb-dep-id").value.trim(),
      token_secret: panel.querySelector("#mb-dep-secret").value.trim(),
      // Auto/H100 固定:主卡 H100(省钱/升档由 auto_downgrade 控制);B200 固定:主卡直接 B200
      default_gpu: gpuMode === "b200" ? "B200" : "H100",
      auto_downgrade: gpuMode === "auto",
      comfy_api_key: panel.querySelector("#mb-dep-comfy").value.trim(),
    };
    // token_secret 留空 = 沿用已存的(/config 不再回显它);只有填了才校验格式
    const secretOk = payload.token_secret === "" ? cfg.has_token_secret : payload.token_secret.startsWith("as-");
    if (!payload.token_id.startsWith("ak-") || !secretOk || !payload.workspace) {
      statusEl.textContent = cfg.has_token_secret ? t("dep.fill_saved") : t("dep.fill_all");
      statusEl.style.color = "#f87171";
      return;
    }
    goBtn.disabled = true;
    statusEl.textContent = t("dep.running");
    statusEl.style.color = "#9aa";
    logEl.style.display = "block";
    logEl.textContent = "";
    try {
      const rc = await streamPost("/modal_bridge/deploy", payload, (line) => {
        logEl.textContent += line + "\n";
        logEl.scrollTop = logEl.scrollHeight;
      });
      if (rc === 0) {
        statusEl.textContent = t("dep.ok");
        statusEl.style.color = "#34d399";
        notify(t("dep.ok.toast"), "success");
        doHealthCheck();
        refreshVerBanner(panel);  // 部署成功 → 版本徽标翻绿(本地↔云端对齐)
      } else {
        statusEl.textContent = t("dep.fail", { rc });
        statusEl.style.color = "#f87171";
        notify(t("dep.fail.toast", { rc }), "error");
      }
    } catch (e) {
      statusEl.textContent = "✗ " + (e.message || e);
      statusEl.style.color = "#f87171";
    } finally {
      goBtn.disabled = false;
    }
  };
}

// =====================================================================
// actionBarButtons 注册
// =====================================================================
// 浏览器内触发文本文件下载(无需后端)
function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/x-python;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  a.remove(); URL.revokeObjectURL(url);
}

// 把当前画布工作流导成「自包含单文件 Modal API 客户端」——别人 python xxx.py 就能云端出图,
// 不需要 ComfyUI / 本机 GPU / 作者机器开机(直连已部署的 -run / -status)。纯前端,不碰后端。
async function exportModalApi() {
  const cfg = await fetchConfig();
  if (!isConfigured(cfg)) {
    notify(t("toast.not_deployed"), "warn");
    try { openDeployDialog(); } catch (e) {}
    return;
  }
  let p;
  try { p = await app.graphToPrompt(); }
  catch (e) { notify(t("export.fail"), "error"); return; }
  const wf = p.output;
  const base = cfg.modal_endpoint_base;
  const tier = getVramTier(wf);
  const wfname = (activeWorkflowName() || "workflow").replace(/[^\w.-]+/g, "_");

  // 探测可覆盖的提示词/种子节点 + 列出依赖模型(供接收方/作者核对已同步)
  const promptNodes = [], seedNodes = [], prereq = new Set();
  for (const [nid, node] of Object.entries(wf)) {
    const ct = (node && node.class_type) || "";
    const ins = (node && node.inputs) || {};
    if (/TextEncode/i.test(ct) && typeof ins.text === "string") promptNodes.push(nid);
    if (typeof ins.seed === "number" || typeof ins.noise_seed === "number") seedNodes.push(nid);
    for (const v of Object.values(ins)) {
      if (typeof v === "string" && /\.(safetensors|ckpt|pt|pth|gguf|bin|sft|onnx)$/i.test(v))
        prereq.add(`#       - ${ct}: ${v}`);
    }
  }
  const prereqText = prereq.size ? [...prereq].join("\n") : "#       (未检测到模型加载节点)";
  const wfB64 = btoa(unescape(encodeURIComponent(JSON.stringify(wf))));

  // KEY:默认占位符(安全);用户确认「嵌入」才写真 key——从本机路由取(/config 不回吐 key)
  let keyValue = "在此填入作者给的 bridge_api_key(bk-...)";
  let keyNote =
`    3) 把作者给的 API KEY 填到下面 KEY(bk-...)。
       注意:KEY = 作者的 Modal 账单,别公开、别提交到仓库。`;
  // 右(主蓝)= 用占位符(推荐/安全);左(红)= 嵌入 KEY(危险)。返回 true=占位符,false=嵌入
  const safe = await confirmDialog(
    t("export.key.title"), t("export.key.body"),
    t("export.key.placeholder"), t("export.key.embed"),
    { dangerCancel: true },
  );
  if (!safe) {
    try {
      const kd = await (await api.fetchApi("/modal_bridge/bridge_key")).json();
      if (kd && kd.key) {
        keyValue = kd.key;
        keyNote =
`    3) ⚠ 本文件已内嵌作者的 API KEY = 作者的 Modal 账单。
       别公开、别传仓库、别群发;一旦泄露,只能让作者轮换 key 止损。`;
      } else { notify(t("export.key.fail"), "warn"); }
    } catch (e) { notify(t("export.key.fail"), "warn"); }
  }

  const py = `#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
${wfname} — Modal API 单文件客户端(comfyui_modal_bridge 导出)

把这套 ComfyUI 工作流在云端 Modal GPU 上跑、出图存本地。
不需要 ComfyUI、不需要本机 GPU、不用作者的机器开机。

用法:
    pip install requests
    python ${wfname}_modal.py
    python ${wfname}_modal.py --prompt "a cat" --seed 123 --out out.png

前提(作者保证,只需一次):
    1) app 已部署到 Modal:${base}
    2) 以下模型已同步到云端 Volume(否则 worker 找不到),以及工作流用到的自定义节点已在云端镜像:
${prereqText}
${keyNote}
"""
import argparse, base64, json, sys, time
try:
    import requests
except ImportError:
    sys.exit("缺 requests:  pip install requests")

BASE = "${base}"
KEY  = "${keyValue}"
TIER = "${tier}"

_WF_B64 = "${wfB64}"
WORKFLOW = json.loads(base64.b64decode(_WF_B64).decode("utf-8"))

PROMPT_NODES = ${JSON.stringify(promptNodes)}   # --prompt 覆盖这些节点的 text
SEED_NODES   = ${JSON.stringify(seedNodes)}     # --seed 覆盖这些节点的 seed/noise_seed


def _apply(wf, prompt, seed):
    if prompt is not None:
        for nid in PROMPT_NODES:
            wf.get(nid, {}).get("inputs", {})["text"] = prompt
    if seed is not None:
        for nid in SEED_NODES:
            ins = wf.get(nid, {}).get("inputs", {})
            for k in ("seed", "noise_seed"):
                if k in ins:
                    ins[k] = seed
    return wf


def run(wf, timeout=900):
    if not KEY.startswith("bk-"):
        sys.exit("请先在脚本顶部 KEY 填入作者给的 bridge_api_key(bk-...)")
    # 提交(带重试,容忍偶发网络/冷启动抖动)
    jid = None
    for _ in range(5):
        try:
            jid = requests.post(BASE + "-run.modal.run",
                json={"workflow": wf, "tier": TIER, "auth_key": KEY}, timeout=60).json().get("id")
            if jid:
                break
        except Exception as e:
            print("提交重试:", e)
        time.sleep(3)
    if not jid:
        sys.exit("提交失败(网络或鉴权问题)")
    print("job:", jid, "(首次冷启动约 3-5 分钟,别急)")
    # 轮询(单次网络抖动不致命,下一轮自动重试)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = requests.get(BASE + "-status.modal.run",
                             params={"job_id": jid, "key": KEY}, timeout=30).json()
        except Exception as e:
            print("轮询抖动,重试:", e)
            time.sleep(3)
            continue
        st = s.get("status")
        if st == "completed":
            return [base64.b64decode(im["data_base64"]) for im in s.get("images", [])]
        if st == "failed":
            sys.exit("失败: " + str(s.get("error")))
        time.sleep(2)
    sys.exit("超时(>" + str(timeout) + "s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="${wfname} on Modal")
    ap.add_argument("--prompt", help="覆盖正向提示词")
    ap.add_argument("--seed", type=int, help="覆盖随机种子")
    ap.add_argument("--out", default="${wfname}_out.png", help="输出文件名")
    a = ap.parse_args()
    wf = _apply(json.loads(json.dumps(WORKFLOW)), a.prompt, a.seed)
    imgs = run(wf)
    if not imgs:
        sys.exit("没拿到图")
    for i, b in enumerate(imgs):
        fn = a.out if i == 0 else a.out.rsplit(".", 1)[0] + "_" + str(i) + ".png"
        with open(fn, "wb") as f:
            f.write(b)
        print("saved:", fn)
`;
  downloadText(`${wfname}_modal.py`, py);
  notify(t("export.done", { name: wfname }), "success");
}

// 注:这两个 tooltip 同时用作 DOM 选择器(下方 querySelector button[title=...]),
// 不做 i18n(否则切语言后选择器对不上已注册的按钮)。保持稳定英文。
const BUTTON_TOOLTIP = "Queue on Modal (H100, see Settings)";
const SETUP_TOOLTIP = "Modal Bridge: deploy / settings (start here)";
const EXPORT_TOOLTIP = "Export current workflow as a standalone Modal API client (.py)";

app.registerExtension({
  name: "ModalBridge.QueueButton",

  settings: SETTINGS,

  actionBarButtons: [
    {
      icon: "pi pi-cloud-upload",
      tooltip: BUTTON_TOOLTIP,
      label: "RunModal",
      onClick: queueOnModal,
    },
    // Export API 按钮先从 UI 摘掉(暂不暴露);exportModalApi / EXPORT_TOOLTIP 代码保留,以后可一键恢复。
    // {
    //   icon: "pi pi-file-export",
    //   tooltip: EXPORT_TOOLTIP,
    //   label: "Export API",
    //   onClick: exportModalApi,
    // },
    {
      icon: "pi pi-cog",
      tooltip: SETUP_TOOLTIP,
      label: "Modal Setup",
      onClick: openDeployDialog,
    },
  ],

  async setup() {
    log("setup() running");
    doHealthCheck();

    // RunModal 按钮变色:启动查一次部署状态;定时器只按缓存状态重涂(防动作栏重渲染丢样式)
    refreshRunReady();
    setInterval(applyRunButtonStyle, 2000);

    // 没配置过 → 提示去点 Setup 部署(零终端);配过了则查 Modal 平台状态,故障就主动预警
    fetchConfig().then(async (cfg) => {
      // 用后端 config 的真实值初始化快照开关(UI 对齐部署现实),之后才放行 onChange 回写
      try { app.ui.settings.setSettingValue("ModalBridge.enableSnapshot", !!cfg.enable_snapshot); } catch (e) {}
      _snapReady = true;
      if (!isConfigured(cfg)) {
        notify(t("toast.not_deployed"), "warn");
      } else if (await isModalOutage()) {
        notify(t("ver.platform_startup"), "warn");  // 启动主动预警:Modal 平台正故障
      }
    });

    // 5 秒后检查 actionBarButtons 是否成功渲染
    setTimeout(() => {
      const found = document.querySelector(
        `button[aria-label="${BUTTON_TOOLTIP}"], button[title="${BUTTON_TOOLTIP}"]`
      );
      if (found) {
        log("✓ actionBarButtons rendered");
      } else {
        log("✗ actionBarButtons not rendered, no fallback");
      }
    }, 5000);

    // history 持久化:启动时检查未完成 job
    recoverPendingJob();
  },
});

