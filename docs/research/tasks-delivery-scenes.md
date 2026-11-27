# 任务记录、投递与场景接入评估

> 核验日期：2026-11-27  
> 范围：上游只读快照 `reference/wechat-ai-memory`（`v0.3.8`，commit `14d94db8b1affa12a2d7d88f0f830c1e0b8d752a`）与本项目 `src/NativeApp`、`src/PythonWorker`、`docs/protocol.md`。  
> 方法：只读源码和测试，不读取真实微信数据库/聊天内容，不联网，不执行 git 操作。  
> 标记：`【事实】` 来自实际文件；`【建议】` 是接入方案；`【未验证】` 是代码和测试没有覆盖、不能当作已实现的内容。

## 0. 结论摘要

`【事实】` 上游的任务记录、投递和场景是相对独立的 Python 层：`jobs/`、`delivery/`、`scenes/` 主要使用标准库，投递层通过 `ctypes` 调用 Windows API，没有 HTTP、socket、子进程或第三方网络 SDK（`delivery/win32.py:105-166`，`delivery/agent.py:50-86`，`scenes/prompt.py:36-82`）。这三块的代码量约为 2.1k 行（不含 `pipeline.py` 的 370 行），直接相关测试约 0.9–1.2k 行。`【事实】`

`【建议】` 接入现有原生应用的可行性较高，但不能把 `pipeline.py` 原样搬进 worker：`pipeline.py:44-53` 依赖 `service.ExportService`，会牵出 PDF/渲染/ReportLab/Pillow 等依赖；当前 worker 是 stdlib-only，只暴露 Markdown 导出（`src/PythonWorker/worker.py:1-7,37-43,359-365`，`src/PythonWorker/requirements-build.txt` 只有 PyInstaller）。更合适的是在现有 `export_markdown` 外包一层原生任务适配器，复用 `jobs/`、`scenes/`、`delivery/` 的概念和大部分实现。

| 能力 | 上游事实 | 建议优先级 |
|---|---|---|
| 任务记录/manifest/state/哈希 | 完整实现，JSON 文件存储，原子提交 | P0 |
| 场景模板/提示词 | 4 个内置场景，纯文本模板，无模型调用 | P0 |
| 文件夹投递 | 完整实现，纯文件复制 | P0 |
| Obsidian 投递 | 完整实现，写 Markdown + 附件目录 | P1 |
| 剪贴板投递 | 完整实现，Win32 剪贴板 | P1 |
| Agent 投递 | 完整实现，激活窗口并模拟 Ctrl+V | P2 |
| 任务历史/恢复 UI | 上游无 GUI 历史/恢复；仅 CLI 调用 `recover_stale` | P0/P1（本项目新增 UI） |
| 重试/重新投递 | 文档提到可重试，源码没有独立重试入口 | P2（属于新增能力） |

`【建议】` 推荐落地顺序是：P0 任务记录 + 场景文本 + 文件夹投递 + 只读历史；P1 Obsidian + 剪贴板；P2 Agent + 重试/恢复增强。若产品要求严格复刻上游“投递失败自动回落剪贴板”的契约，剪贴板要提前到 P0/P1 交界，因为它是回落的保底目标。

## 1. 任务模型

### 1.1 一次导出的字段

`【事实】` 上游把“不可变的导出描述”和“可变的执行状态”分开：`manifest.json` 对应 `JobManifest`，`state.json` 对应 `JobState`（`reference/wechat-ai-memory/src/wechat_context_exporter/jobs/models.py:214-342`，`:345-399`）。一次导出涉及的字段如下。

| 需求字段 | 上游字段 | 证据 | 说明 |
|---|---|---|---|
| 任务标识 | `job_id` | `jobs/models.py:26-31,85-98` | 形如 `YYYYMMDDTHHMMSS-<6 hex>`，UTC 生成，正则校验 |
| 来源 | `source.kind/label/wechat_version` | `pipeline.py:255-271,313-344` | kind 来自 source 类名推断，label 可能是路径或账户标签；native 适配器应显式赋值 |
| 会话 | `conversation.id/name` | `pipeline.py:258-262` | 会话名会进入 manifest、日志和投递目录名 |
| 时间窗 | `window.start/end/query` | `pipeline.py:279-283`；`scenes/models.py:77-90` | 场景可给出相对天数，最终解析成有效窗口 |
| 首条/末条消息 | `reference.first/last` | `pipeline.py:274-280`；`jobs/models.py:129-166` | 每项含 `id/timestamp/sender/preview` |
| 消息数量 | `message_count` | `pipeline.py:281` | 导出时统计 |
| 场景 | `scene.id/name` | `pipeline.py:267-272` | 无场景时为 `none`/null |
| 产物 | `artifacts[]` | `jobs/models.py:169-211`；`jobs/store.py:394-419` | 名称、类型、字节数、sha256；只收集顶层 `.pdf/.md/.json` |
| 哈希 | `JobArtifact.sha256` | `jobs/models.py:106-112,169-211` | 流式 SHA-256；`*_assets` 目录不逐个列出、不单独哈希 |
| 投递目标/结果 | `state.target/destination/detail/error` | `jobs/models.py:345-374`；`pipeline.py:138-145` | 目标名、目的路径、说明、错误 |
| 投递尝试 | `state.attempts` | `jobs/models.py:360-362`；`jobs/store.py:195-230` | 仅在 delivered/copied/failed 时递增，不是完整审计日志 |
| 状态 | `state.status` | `jobs/models.py:38-57` | `pending/running/ready/delivered/copied/failed/expired` |
| 备注 | `manifest.notes` | `pipeline.py:296-310`；`jobs/models.py:288-291` | 导出阶段写入的说明，不是聊天正文 |
| 兼容版本 | `schema_version` | `jobs/models.py:26,214-227` | 当前为 1 |

`【事实】` 首尾消息的 `preview` 不是完整正文：`message_preview` 会截断到 `PREVIEW_LIMIT = 80`，并折叠为单行（`jobs/models.py:26-27,115-120`）。即便只是“任务记录”，manifest 仍会保存会话名、发送者、首尾消息片段和来源路径/标签；这是本地隐私数据，不是纯元数据。

`【事实】` `JobRecord.summary()` 提供面向 UI 的摘要（job id、状态、创建时间、会话名、消息数、产物、目标、目的、错误等，`jobs/models.py:402-443`），但它不是独立存储；权威数据仍是 manifest + state。

### 1.2 存储位置与格式

`【事实】` 上游默认任务根目录：

- Windows：`%LOCALAPPDATA%\WeChatAIMemory\jobs`
- 其他平台：`~/.wechat-ai-memory/jobs`
- 环境变量覆盖：`WECHAT_AI_MEMORY_JOBS_DIR`

证据：`jobs/store.py:3-19,52-66`。目录布局为：

- `.staging/<job_id>/`：正在执行的 staging，含 `files/`
- `Ready/<job_id>/`：已提交任务，含 `manifest.json`、`state.json`、`files/`
- `Failed/<job_id>/`：中断或失败任务，仍保留现场
- `Logs/<YYYY-MM-DD>.log`：按 UTC 日期分文件的轻量日志

`【事实】` manifest/state 都是 UTF-8、缩进 JSON，带 `schema_version`；任务内部文件位于 `files/`。日志是制表符分隔的文本，记录 UTC 时间、job id 和消息；`begin` 时会写入会话名，`set_state` 时会写入状态和目的路径（`jobs/store.py:176-193,232-240`）。日志可能包含会话名和目的路径，不能按“无内容日志”宣传。

`【事实】` 场景有独立的存储文件：默认 `%LOCALAPPDATA%\WeChatAIMemory\scenes.json`，环境变量 `WECHAT_AI_MEMORY_SCENES_FILE` 可覆盖（`scenes/store.py:21-29`）。任务存储和场景存储都不加密，依赖用户目录 ACL。

`【建议】` 原生应用不要复用上游默认目录，避免和上游应用互相读/写/清理对方的任务。建议使用独立命名空间，例如 `%LOCALAPPDATA%\WechatMemoryNative\jobs`、`%LOCALAPPDATA%\WechatMemoryNative\scenes.json`，并允许 worker 启动参数覆盖。原因是上游的任务目录名、场景文件名和日志格式都是默认约定，不是一个隔离的多实例协议。

### 1.3 生命周期、原子提交与失败恢复

`【事实】` `run_export` 在带 `JobStore` 时执行以下顺序：查找会话/消息 → 组装 manifest → `store.begin` 创建 staging/RUNNING → 导出到 job 的 `files/` → 收集产物/哈希 → 渲染提示词 → 投递 → 提交或标记失败（`pipeline.py:74-120`）。具体提交点如下：

- `begin()` 创建 `.staging/<job_id>/files`，写入 `RUNNING` 的 state，并写日志（`jobs/store.py:176-193`）。
- `commit()` 写 manifest、写 `READY` state，然后把 staging 重命名到 `Ready/<job_id>`；目标已存在会报错（`jobs/store.py:96-122`）。
- `abort()` 把 staging 移到 `Failed/<job_id>`，写入失败状态；上下文管理器在未提交时自动 abort（`jobs/store.py:124-160`）。
- `_atomic_write` 用同目录 `.tmp` + `os.replace` 写 JSON（`jobs/store.py:69-74`）。
- 产物哈希在 `commit` 前计算；`collect_artifacts` 只枚举顶层 `.pdf/.md/.json`，不枚举 `*_assets`（`jobs/store.py:394-419`）。

`【事实】` `READY` 是一个“已提交但未完成投递”的状态，`is_finished` 不包含它，`prune` 默认也保留未完成任务（`jobs/models.py:47-57,414-417`；`jobs/store.py:315-343`）。这是上游有意的设计：没有投递目标的导出任务会停在 READY，不会被历史清理误删。

`【事实】` 失败恢复只有 `recover_stale()`：把遗留的 `.staging` 目录移到 `Failed`，写 `FAILED` 状态和 `interrupted before commit`，并跳过无法加载的 manifest（`jobs/store.py:278-313`）。`recover_stale` 只在 CLI 入口调用（`cli.py:106`）；上游 GUI 没有任务历史/恢复入口，worker 也没有。`prune()` 只删除超过指定天数的已完成任务，默认不删未完成；`Failed` 属于已完成，会被清理（`jobs/store.py:315-343`）。

`【事实】` 上游 `docs/jobs-and-delivery.md:50-61` 描述了同一 job 可重试/继续，但源码中没有独立的“重新投递同一个 job”命令或函数；CLI 参数也没有 `--retry`（`cli.py:25-63,66-118`）。因此“重试”是文档承诺、代码未提供的部分。

`【事实/边界】` 代码没有文件锁、单实例限制或并发写保护；`begin` 的“目标不存在”检查和后续 rename 之间存在竞态。多实例共享同一个任务根目录不安全。代码也没有 `fsync` 或目录同步调用，原子 rename 能保证进程崩溃时的“不会出现半成品 Ready”，但不能据此宣称断电场景的持久性。

`【建议】` 原生应用应把“任务根目录”固定为 worker 启动参数，并在应用层加单实例/命名互斥；启动 worker 后先调用一次 `recover_jobs`，再做列表和导出。不要把 `recover_jobs` 放在 UI 的每个刷新动作里。

### 1.4 失败恢复的限制与 native 适配要点

`【事实】` 失败时不会自动续写同一个 staging；下次启动只能把遗留 staging 归入 Failed，用户需要重新导出。投递失败的任务状态为 `FAILED`，`state.error` 可保存异常文本；非 `DeliveryError` 的意外异常没有被 `_deliver` 捕获，可能让任务停在 `READY` 并把异常抛给上层（`pipeline.py:126-181`）。

`【建议】` native 适配器要明确三层失败语义：

1. 导出失败：staging → Failed，保留错误，允许用户重试（新 job）。
2. 投递失败：任务已是 Ready，不改 manifest；保留 artifacts/prompt，允许重新投递。
3. 客户端投递（剪贴板/Agent）失败或中断：任务仍为 Ready；C# 必须在完成后回调 worker 记录结果，否则状态不会自己变化。

`【建议】` 不要复用上游 `paths.py` 的默认输出根：`paths.py:21-32` 把 Windows 默认根硬编码为 `C:/微信记忆`，恰好是本仓库根目录。若直接使用，文件夹投递和默认导出会写进源码树，甚至被误提交。native 应使用用户明确选择的目录或独立的应用数据目录。

`【建议】` 上游 manifest 的 `created_at` 是 UTC，`as_utc` 对无时区值按 UTC 处理（`jobs/models.py:60-103,238-241`）；文件夹投递的日期目录也按这个 UTC 时间生成（`delivery/folder.py:35`）。本项目现有 worker 固定 UTC+08:00（`worker.py:22-30`，`docs/protocol.md:27-29`）。接入时必须统一：manifest 时间带偏移（例如 `+08:00`），目录日期按用户本地日期或明确说明的 UTC 日期生成，不能两套混用。

## 2. 投递

### 2.1 共同协议

`【事实】` 投递层的公共对象在 `delivery/base.py`：

- `DeliveryRequest`：job、scene、prompt（`:89-107`）。
- `DeliveryResult`：target、status、destination、detail；status 只有 `delivered/copied/failed`（`:109-125`）。
- 目标协议：`DeliveryTarget.deliver(request)`（`:128-131`）。
- 产物顺序固定为 PDF → Markdown → JSON；复制时同时复制同名 `*_assets` 目录（`:47-86`）。
- 文件名清洗 `safe_filename` 会移除 Windows 非法字符、首尾空格/点，并限制长度（`:33-44`）。
- 目标集合：`none/folder/obsidian/clipboard/agent`（`delivery/__init__.py:18`）。

`【事实】` 投递不是独立的网络服务：这些模块没有 HTTP/socket/子进程调用；文件夹和 Obsidian 是本地文件操作，剪贴板和 Agent 是本地 Win32 调用。Agent 会枚举窗口、读取目标进程映像名并发送键盘事件，但不会启动外部程序（`delivery/win32.py:105-166,189-214`）。

### 2.2 各目标对照

| 目标 | 入口/配置 | 依赖 | 外部进程/网络 | 失败方式与回落 |
|---|---|---|---|---|
| 文件夹 | `FolderDelivery(root, organize_by_date=True)`；CLI `--deliver-to`；GUI 目标预设 | 标准库文件操作 | 无 | `OSError` → `DeliveryError`；`pipeline` 捕获后回落剪贴板 |
| Obsidian | `ObsidianDelivery(vault, folder="微信记忆", attachments="_附件")`；CLI/GUI 要求 vault 存在 | 标准库文件操作 | 无；不调用 Obsidian API | vault 不存在/写入失败 → `DeliveryError`；回落剪贴板 |
| 剪贴板 | `ClipboardDelivery(backend=None)`；无需配置；CLI `--deliver-to clipboard` | `ctypes` + Win32 剪贴板；测试可注入 fake backend | 无 | 复制失败 → `DeliveryError`；若它本身是回落目标则最终 `FAILED` |
| Agent | `AgentDelivery(app, backend=None, path_only=True, dry_run=False)`；CLI `--agent`；GUI 要求应用名 | `ctypes` + Win32 窗口/键盘/剪贴板 | 不启动进程、不联网；操作用户已运行的应用 | 复制失败 → `DeliveryError`；找不到窗口/激活失败/粘贴失败通常返回 `copied`，不会抛错 |
| none | 不投递 | 无 | 无 | 任务停在 READY |

### 2.3 文件夹投递

`【事实】` 默认目的目录是 `<root>/<YYYY-MM-DD> <safe_conversation>/`，日期取 `manifest.created_at` 的日期；可用 `organize_by_date=False` 关闭日期层（`delivery/folder.py:27-36`）。`deliver` 会复制有序产物和同名 `*_assets`，写入提示词文件 `给 Agent 的提示词.txt`，返回 `delivered`；任何 `OSError` 转成 `DeliveryError`（`delivery/folder.py:38-55`）。同名文件会被覆盖，没有冲突检测或幂等标记。

`【事实】` CLI 的文件夹目标必须显式给出 `--deliver-to`（`cli.py:183-203`）；GUI 默认用 `output_root()` 作为根目录（`ui/scene_delivery.py:146-170` 附近），因此会继承 `paths.py` 的 `C:/微信记忆` 默认值。native 不应沿用这一默认。

### 2.4 Obsidian 投递

`【事实】` Obsidian 目标要求 vault 目录已经存在；笔记路径为：

`<vault>/微信记忆/YYYY-MM/YYYY-MM-DD <safe_conversation>.md`

附件目录为 `<vault>/微信记忆/_附件/<job_id>/`（`delivery/obsidian.py:31-66`）。笔记包含 YAML frontmatter：来源、会话、会话 id、job id、导出时间、时间窗、消息数、场景、标签；正文包含产物链接、提示词和备注（`delivery/obsidian.py:77-121`）。它只写文件，不调用 Obsidian 可执行文件、Obsidian URI、插件 API 或网络。

`【事实】` 同一天同一会话的笔记路径相同，会覆盖已有笔记；附件目录按 job id 区分，不覆盖其他任务。没有“是否已存在”的交互或合并逻辑（`delivery/obsidian.py:49-66`）。

`【建议】` native 集成时至少增加两种策略之一：默认拒绝覆盖并要求用户确认；或者使用包含 job id 的笔记文件名。前者保留上游行为但更安全，后者改变布局。不要在没有提示的情况下静默覆盖用户笔记。

### 2.5 剪贴板投递

`【事实】` `ClipboardDelivery` 的文本优先使用 `request.prompt`；没有提示词时使用包含会话名、时间窗、消息数和产物路径的保底文本（`delivery/clipboard.py:16-24`）。它通过 `Win32Clipboard` 同时设置 `CF_UNICODETEXT` 和 `CF_HDROP`（文件列表），成功后返回 `copied`；没有可复制内容、backend 拒绝或 `OSError` 会抛 `DeliveryError`（`delivery/clipboard.py:27-55`；`delivery/win32.py:40-101`）。

`【事实】` 剪贴板内容一旦写入就是系统全局的；上游没有自动清空、没有敏感标记、没有“仅本应用可见”的机制。文件列表使用绝对路径，提示词文本含会话名、时间窗、归档路径等。Windows 剪贴板历史/云同步是否保留属于系统策略，代码没有处理。`【未验证】` 未在真实 Windows 剪贴板历史上测试。

### 2.6 Agent 投递与回落

`【事实】` Agent 目标把提示词（或保底文本）和产物复制到剪贴板；`path_only=True` 时只复制提示词，不把文件放入剪贴板（`delivery/agent.py:30-55`）。然后按应用名/窗口标题片段查找已运行窗口，激活并发送 Ctrl+V（`delivery/agent.py:56-86`；`delivery/win32.py:105-166,189-214`）。

`【事实】` 它的失败策略与文件夹/Obsidian 不同：

- 复制到剪贴板失败 → `DeliveryError`，`pipeline` 会尝试剪贴板回落；
- 找不到窗口、激活失败、粘贴失败 → 通常返回 `copied`，detail 说明原因，不抛异常；
- `dry_run=True` 只复制，不激活/粘贴，返回 `copied`；
- 没有目标应用白名单，也没有“只允许某个 exe”的限制；按名称/标题匹配，可能匹配到非预期窗口（`delivery/win32.py:189-214`）。

`【事实】` `pipeline._deliver`/`_fall_back` 只捕获 `DeliveryError`；成功状态为 `delivered` 时写 `DELIVERED`，`copied` 写 `COPIED`，两者都算完成（`pipeline.py:126-181`）。如果目标本身是剪贴板且失败，回落目标仍是剪贴板，通常会再次失败，最终合并错误写 `FAILED`。如果发生非 `DeliveryError` 异常，任务可能停在 READY，异常向上传播。

`【建议】` Agent 不应作为默认目标，也不应在没有用户确认的情况下自动执行。C# 侧应显示目标应用名、提示词摘要和“路径模式/文件模式”，并提供 dry-run。若必须保持上游语义，找不到窗口时记录 `copied` 而不是假装 `delivered`；这与上游一致，也避免误导。

## 3. 场景与提示词

### 3.1 内置场景

`【事实】` 4 个内置场景定义在 `scenes/catalog.py:16-79`：

| id | 名称 | 默认天数 | 默认目标 | 快捷键 |
|---|---|---|---|---|
| `group-digest` | 群聊总结 | 1 | folder | 1 |
| `decision-review` | 项目决策复盘 | 30 | obsidian | 2 |
| `commitments` | 待办与承诺 | 14 | clipboard | 3 |
| `change-trace` | 需求变更追溯 | 60 | folder | 4 |

每个场景的 `prompt` 都是静态中文指令文本；`builtin=True`。它们只描述任务、范围和输出要求，不包含模型调用、API 地址、密钥或网络请求。

### 3.2 提示词文件位置

`【事实】` 内置提示词没有独立文件，直接写在 `scenes/catalog.py`。用户自定义场景保存在 `scenes.json`；`SceneStore` 先返回内置场景，再追加用户场景，同 id 的用户场景覆盖内置场景；删除用户覆盖后恢复内置（`scenes/store.py:39-72`）。场景文件损坏或条目非法时会跳过并给出 warning，不阻断内置场景（`scenes/store.py:76-108`）。写入使用临时文件 + replace（`scenes/store.py:93-108`）。

`【事实】` 渲染后的提示词在上游没有被保存进任务目录：文件夹目标把它写成 `给 Agent 的提示词.txt`（`delivery/folder.py:15,42-43`），Obsidian 目标把它嵌入笔记（`delivery/obsidian.py:113-114`），剪贴板/Agent 直接复制（`delivery/clipboard.py:39-43`，`delivery/agent.py:50-55`）。任务 manifest 只存 scene id/name，不存渲染后的提示词。

`【建议】` native 若要做“同一任务重新投递”，应在任务目录持久化 `prompt.txt`（或 manifest 中的 prompt 字段），否则 C# 崩溃/重启后无法还原当时的提示词。该文件不应算作导出产物，不应参与 `artifacts` 哈希；它是任务元数据。

### 3.3 内容如何生成

`【事实】` `render_prompt` 用字符串模板生成提示词，输入是 manifest 的归档路径、会话名/id、有效时间窗、消息数、查询、job id，加上场景正文；场景正文中的 `{{...}}` 占位符会替换，未知占位符原样保留；最后追加固定的引用规则 `[消息ID]`（`scenes/prompt.py:19-24,36-82`）。如果场景没有 prompt，使用默认任务文本（`scenes/prompt.py:19-22`）。

`【事实】` 场景时间窗的解析顺序是：显式 start/end 优先；否则按 `days` 取“现在往前 N 天”到“现在”；否则不限（`scenes/models.py:77-90`）。这里的“现在”是本地 naive 时间，和当前项目的 UTC+08:00 固定时区不是同一套语义。

### 3.4 哪些是模板、哪些依赖模型

`【事实】` 4 个内置场景和用户自定义场景都只是**模板文本**；`scenes/` 没有导入模型 SDK、HTTP 客户端或推理运行时，`render_prompt` 只做字符串替换。上游 README 也明确 AI 问答/RAG 未实现（`README.md:114`，路线图 `:164-172`）。因此：

- “群聊总结”“项目决策复盘”等不是应用自动生成的摘要，而是给外部 Agent/模型的任务说明；
- 应用本身不调用模型，不需要 AI，不需要网络；
- 只有用户把提示词投递给某个 Agent，且该 Agent 自己使用模型时，才可能产生模型调用；这属于外部应用行为，不是本项目的场景实现；
- 自定义场景可以写任意提示词，但仍然是文本，不会自动执行。

`【建议】` UI 文案必须避免把“场景”说成“AI 总结”。准确说法是“场景模板/提示词”，结果由用户选择的 Agent 或手工流程产生。

### 3.5 场景对 native 的额外约束

`【事实】` 场景不仅有 prompt，还有 `include_image_pages`、`markdown`、`json_output`、`target` 等字段（`scenes/models.py:38-54`）；CLI 会用这些字段控制导出（`cli.py:120-148`）。当前 native worker 只支持 Markdown，不支持 PDF、图片页、JSON（`worker.py:238-244`，`docs/protocol.md:19-31`）。

`【建议】` P0 只使用场景的“时间窗、query、target、prompt”四个维度；对 `json_output/include_image_pages` 明确降级或显示“当前版本不支持”，不能静默假装已经按场景导出。若场景要求 JSON/PDF，UI 应在导出前提示，并允许用户改用 Markdown 或取消。场景的 target 可以映射到 UI 的默认目标，但不应绕过用户的显式目标选择。

## 4. 与本项目 UI/worker 的映射

### 4.1 现状事实

`【事实】` 当前应用是 C#/WinUI 3 + MVVM，worker 是独立的 Python 子进程：

- UI 只有会话、日期筛选、消息分页、Markdown 导出、打开文件/文件夹和错误条（`src/NativeApp/MainWindow.xaml:21-92`）。
- `MainViewModel` 只有 `Reconnect/ApplyFilters/ClearFilters/LoadMore` 等命令；导出直接调用 `export_markdown`，保存路径由 `FileSavePicker` 选择（`src/NativeApp/ViewModels/MainViewModel.cs:39-54,131-144`，`MainWindow.xaml.cs:57-73`）。
- worker 只接受 `source:"demo"`，命令是 `hello/list_conversations/get_messages/export_markdown/shutdown`（`worker.py:207-211,238-244,359-365`）。
- 协议 v1 一次只允许一个 outstanding 请求，默认 30 秒、导出 120 秒，没有进度事件，stderr 不记录（`src/NativeApp/Services/WorkerClient.cs:62-104`，`docs/protocol.md:3-13,35-39`）。
- 当前没有任务记录、场景、投递、历史列表，也没有单实例限制（`App.xaml.cs:11-27`）。
- `WorkerLocator` 只查找发布目录或项目内 `.venv`，并固定传 `--demo`；接入任务/场景需要新增启动参数（`WorkerLocator.cs:7-31`）。

### 4.2 worker / C# 分工建议

| 能力 | 放 worker | 放 C# | 理由 |
|---|---|---|---|
| Markdown 导出、任务 staging、manifest/state、sha256 | 是 | 否 | 任务状态应由单一写入者维护；复用 Python 逻辑 |
| 场景读取、合并、渲染提示词 | 是 | 否 | 纯文本逻辑，和导出/manifest 紧邻 |
| 文件夹投递 | 是 | 否 | 纯文件复制，可在 worker 内完成并原子记录状态 |
| Obsidian 投递 | 是 | 否 | 纯文件写入；worker 可测试、可回滚 |
| `list_jobs/get_job/recover_jobs/prune_jobs` | 是 | 否 | 权威状态在磁盘，避免 C# 复制状态 |
| 剪贴板写入 | 可选 | 推荐 | 剪贴板是全局 UI 资源；C# 更容易和前台窗口/用户确认整合 |
| Agent 激活/粘贴 | 否 | 是 | 需要前台窗口和用户交互；C# 负责目标选择与 dry-run |
| 任务历史/状态展示 | 否 | 是 | 只做投影；不直接读写 manifest/state |
| 保存对话框、打开文件/文件夹、单实例 | 否 | 是 | 现有 WinUI/Windows API 已有能力 |
| 应用设置（上次目标、窗口状态） | 否 | 是 | 与任务权威状态分离，不含聊天内容 |

`【建议】` 采用“worker 负责导出和文件型投递，C# 负责剪贴板和 Agent”的边界。这样 worker 不需要把 Win32 前景窗口逻辑塞进无 UI 子进程，C# 也能在粘贴前显示确认。如果产品坚持“全部在 worker”，则需要把 `delivery/win32.py` 移植到 worker，并接受“worker 无法确认用户是否真的想粘贴、无法可靠处理前台窗口”的风险；不推荐作为 P0。

`【建议】` C# 执行剪贴板/Agent 后必须调用一个 `record_delivery` 命令把 `delivered/copied/failed`、目标、目的描述和错误写回 worker。若 C# 直接改 state.json，会破坏“单一写入者”和原子写约定。若 C# 在写入前崩溃，任务保持 Ready，用户可重新投递；这比写入半状态更安全。

### 4.3 UI 需要的最小改动

`【建议】` 在现有导出面板上增加：

- 场景下拉框（内置 4 个 + 自定义；显示名称和默认目标）；
- 目标选择（先只做 folder/obsidian；clipboard/agent 在 P1/P2 加入）；
- 目标路径选择/最近路径；Obsidian vault 选择器；
- “导出到任务目录”与“另存一份 Markdown”分开：现有 `FileSavePicker` 可以保留为可选的镜像导出，但任务目录不应依赖用户每次选路径；
- 任务历史列表（job id、时间、会话、消息数、状态、目标、目的）；只读详情，不在列表里显示首尾消息预览，避免隐私泄露；
- 失败任务的“重新投递”/“打开任务文件夹”按钮（P2）；
- 场景应用后同步日期控件，并明确显示“场景默认窗口已被手动修改”的状态，避免当前 `FiltersDirty` 语义把导出按钮误禁用。

`【事实】` 当前 `CanExport` 与 `FiltersDirty` 绑定，日期控件改变后必须重新应用筛选（`MainViewModel.cs:57-65,113-129`）。场景会程序化设置日期，接入时要区分“用户手动改日期”和“场景应用日期”，否则会出现场景选完却不能导出的交互问题。

### 4.4 状态持久化建议

`【建议】` 采用“双存储、单权威”：

- **任务权威状态**：worker 的 `manifest.json` + `state.json`；C# 只保存当前选中的 job id/过滤条件，不复制状态。
- **UI 设置**：C# 自己的 JSON（例如 `%LOCALAPPDATA%\WechatMemoryNative\settings.json`），保存上次目标类型、folder/vault 路径、窗口大小、场景选择；不保存聊天正文、不保存完整 prompt。
- **场景**：worker 的 `scenes.json`；C# 可以缓存列表用于显示，但保存/删除必须走 worker。
- **日志**：沿用当前 `AppLog` 的“事件名 + 安全错误码”，不要照搬上游会把会话名/目的路径写进日志的 `JobStore.log`。如果 worker 需要诊断日志，使用独立的、无会话内容的 job 日志，并在 UI 说明其位置。

`【建议】` 启动顺序：启动 worker → `hello` 校验协议/能力 → `recover_jobs` → `list_scenes` → `list_jobs` → 绑定 UI。若旧 worker 没有这些命令，`hello` 的能力位应让 UI 禁用新功能，而不是让每个命令超时。

### 4.5 协议草案（v1 扩展，不写实现）

`【建议】` 保持现有信封、`requestId`、`protocolVersion:1`、stdout 只输出协议 JSON 的约束，新增命令作为 capability 扩展。建议 `hello.result.capabilities` 增加：`jobs_v1`、`scenes_v1`、`delivery_folder_v1`、`delivery_obsidian_v1`、`delivery_client_v1`。

| command | payload | result | 说明 |
|---|---|---|---|
| `list_scenes` | `{}` | `items:[{id,name,description,days,start,end,query,markdown,jsonOutput,includeImagePages,target,shortcut,builtin,prompt?}]`, `warnings` | 默认不回传完整 prompt；编辑时用 `get_scene` |
| `get_scene` | `{sceneId}` | `scene` | 用于编辑/预览 |
| `export_job` | `{source:"demo",conversationId,sceneId?,startDate?,endDate?,query?,target?:{kind:"none"|"folder"|"obsidian",...},mirrorOutputPath?,overwrite?}` | `job:{jobId,status,conversation,messageCount,window,artifacts,target,destination,prompt?}`, `warnings` | 创建任务；显式日期覆盖场景窗口；目标仅 folder/obsidian |
| `list_jobs` | `{limit?:50}` | `items:[summary]`, `warnings` | summary 不含正文/完整首尾预览 |
| `get_job` | `{jobId,includePrompt?:false}` | `manifest,state,artifacts` | 详情；`includePrompt` 供 C# 剪贴板/Agent 使用 |
| `deliver_job` | `{jobId,target:{kind:"folder"|"obsidian",...}}` | `job,delivery` | 仅 worker 支持的文件型投递；可重试 Ready/Failed 任务 |
| `record_delivery` | `{jobId,target:{kind:"clipboard"|"agent",...},status:"delivered"|"copied"|"failed",destination?,detail?,error?}` | `job` | C# 完成客户端投递后回调；worker 校验枚举并原子写 state |
| `recover_jobs` | `{}` | `recovered:[jobId]`, `warnings` | 启动时调用；把遗留 staging 归入 Failed |
| `prune_jobs` | `{days:number}` | `removed:[jobId]`, `warnings` | 显式清理；只删已完成且超期任务 |
| `save_scene` / `delete_scene` | 场景对象 / `{sceneId}` | `scene` / `{deleted:true}` | P3；先校验 id、target、长度，再原子写 scenes.json |

`【建议】` 关键约束：

- `jobId` 必须匹配上游正则；服务端只接受自己任务根下的 job，拒绝 `..`、绝对路径和符号链接逃逸。
- `export_job` 的 `mirrorOutputPath` 必须是绝对 `.md`，父目录存在；覆盖必须显式 `overwrite:true`，与现有 `export_markdown` 一致。
- `target` 是对象而不是裸字符串，便于区分 `folderPath`、`vaultPath`、`agentApp`、`dryRun`；未知字段拒绝或忽略必须有明确规则。
- `record_delivery` 只允许 `clipboard/agent` 和三种状态；worker 不执行这些目标，只记录结果。
- `list_jobs` 不返回聊天正文或完整首尾消息；`get_job` 的 previews 仍属于敏感数据，UI 需要用户主动查看。
- 错误码在现有基础上增加：`SCENE_NOT_FOUND`、`SCENE_INVALID`、`JOB_NOT_FOUND`、`JOB_NOT_READY`、`TARGET_NOT_SUPPORTED`、`DELIVERY_FAILED`、`JOB_PATH_UNSAFE`、`JOB_STORE_UNAVAILABLE`。沿用 `INVALID_ARGUMENT`、`EXPORT_FAILED`、`OUTPUT_*`。
- 超时：`list_scenes/list_jobs/get_job` 30 秒；`export_job/deliver_job` 120 秒（大附件可配置）；`recover_jobs/prune_jobs` 60 秒。没有进度事件，UI 继续使用不确定进度，不伪造百分比。
- 不把 prompt/会话名/路径写入 stderr 或 C# 的 `AppLog`；stderr 仍只做诊断计数。

`【建议】` 若不做 `prompt.txt` 持久化，`deliver_job` 只能重新渲染场景提示词；在场景被修改或删除后会得到不同文本。为了“同一任务可重试”，worker 应在任务根保存渲染后的 `prompt.txt`，`deliver_job` 优先读它；`export_job` 的 result 可只在 `includePrompt` 时返回，减少默认传输的敏感文本。

### 4.6 为什么不直接移植 `pipeline.py`

`【事实】` `pipeline.py` 顶部导入 `service.ExportService`（`:44-53`），而 `service.py:12-16,46-59` 组合 Markdown/JSON/PDF、渲染和进度回调；`pyproject.toml:13-23` 的依赖包括 `reportlab`、`Pillow`、`frida`、`pywinauto`、`pycryptodome`、`faster-whisper` 等。当前 worker 只有标准库 + vendored `models.py`/`text_exporters.py`（`worker.py:37-43`；`docs/source-assessment.md:70-88`）。直接复制 pipeline 会破坏 stdlib-only/PyInstaller 的打包边界。

`【建议】` 采用适配器：把当前 `export_markdown` 的输出重定向到 job 的 `files/`，再用 `JobStore.begin/commit` 包起来；产物收集、哈希、manifest、state 直接用 `jobs/`；场景用 `scenes/`；文件夹/Obsidian 用 `delivery/` 的对应类。`pipeline.py` 只作为流程参考，不整文件复制。这样 P0 不需要新增 Python 运行依赖，也不需要 PDF/渲染。

`【事实】` 当前 worker 的 `export_markdown` 已经有同目录临时文件 + `os.replace` 的原子写（`worker.py:304-356`），但它不是任务级原子提交；任务级仍需要 staging + rename。适配器应保留现有单文件导出作为“另存一份”路径，任务目录作为权威路径。

## 5. 许可证与隐私

### 5.1 许可证

`【事实】` 上游 `wechat-ai-memory` 是 MIT，版权行为 `Copyright (c) 2026 WeChat AI Memory contributors`（`LICENSES/wechat-ai-memory-LICENSE.txt`；`docs/source-assessment.md:190-211`）。MIT 允许修改、再分发和商业使用，但要求在任何副本或实质部分中保留版权声明和许可声明。

`【建议】` 若把 `jobs/`、`scenes/`、`delivery/` 的新文件复制/改写进 `src/PythonWorker/vendor`：

- 保留每个文件头的来源 commit、MIT 归属和 `LICENSES/` 指引；
- 在 `THIRD_PARTY_NOTICES.md` 增加新增文件清单；
- 不因为复制这些模块而引入上游 `pyproject.toml` 的重依赖；任务/场景/投递本身不需要它们；
- 当前仓库没有项目级 `LICENSE`（`docs/source-assessment.md:227-230`），正式分发前需要权利人明确整体许可证；MIT 第三方声明不能替代项目自身许可证；
- 没有复用 DeskBox 的 GPL 代码，继续不要复用（`docs/source-assessment.md:213-225`）。

### 5.2 隐私边界

`【事实】` 上游 README 的隐私声明是：不上传聊天、不写遥测、不使用远程 AI、密钥/数据库只读、临时副本可清理（`README.md:94-104`）。这些目标是设计声明，不是第三方审计；本评估也只做了源码检查，不能替代安全审计。

`【事实】` 任务记录本身已经是本地敏感数据：

- manifest 保存会话名/id、来源标签/路径、首尾消息的 80 字符预览、发送者、时间窗（`jobs/models.py:129-166,214-292`；`pipeline.py:255-310`）。
- state 保存目标、目的路径、错误文本；错误文本可能包含异常细节（`jobs/models.py:345-374`）。
- 日志保存 job id、会话名、状态和目的路径（`jobs/store.py:176-193,232-240`）。
- 任务和场景 JSON 不加密；依赖用户目录权限。

`【建议】` 如果 native 应用继续坚持“日志不含内容/路径”的现有边界，就不能直接复用上游 `JobStore.log` 的写法。可以把任务日志降为“job id + 状态 + 安全错误码”，把会话名/目的路径只放在 state 中；或者在 UI 明示“任务日志会记录会话名和路径”。

`【事实/边界】` 投递目标各自的隐私后果：

- **文件夹**：把完整 Markdown/JSON/PDF、附件和提示词复制到用户选择的目录；如果目录在 OneDrive/Dropbox/企业同步盘，第三方同步工具可能上传，虽然本应用不上传。
- **Obsidian**：把笔记、附件和提示词写入 vault；Obsidian Sync/第三方同步/插件可能上传或索引；应用本身不调用 Obsidian 或网络。frontmatter 有会话名和 id，笔记正文含提示词。
- **剪贴板**：内容进入系统全局剪贴板，任何读取剪贴板的程序都可能获取；文件以绝对路径的 `CF_HDROP` 暴露；提示词含会话名/路径。应用不清空，Windows 剪贴板历史/云同步属于系统能力。
- **Agent**：把提示词/文件复制到剪贴板并粘贴到用户选择的应用；目标应用可能上传、记录或索引。没有白名单，焦点错误时可能粘贴到非预期窗口；`path_only=True` 只降低文件暴露，不降低提示词中的会话名/路径暴露。
- **none**：不离开任务目录，但任务目录仍有 manifest 预览和日志。

`【建议】` 默认策略应是 folder/obsidian；clipboard/agent 每次显式触发并确认，Agent 先 dry-run；不要在导出完成后自动粘贴。若要清空剪贴板，必须是用户可选项，并说明会影响用户原有剪贴板内容。不要添加遥测、崩溃上报、远程日志或“匿名统计”；这些都会突破当前项目“仅本地、无网络”的声明。

## 6. 接入顺序与测试方案

### 6.1 接入顺序

`【建议】` 分四步，先建立可验证的最小闭环，再碰高风险的前台自动化。

**P0：任务记录 + 场景文本 + 文件夹投递 + 历史（MVP）**

1. worker 增加 `--jobs-dir`、`--scenes-file`、`--output-root`（或等价参数），`WorkerLocator` 传入独立目录；不改变现有 `export_markdown`。
2. 移植 `jobs/` 的模型/store/hash，写一个最小适配器：当前 Markdown 导出 → staging → manifest/state → Ready。
3. 移植 `scenes/models.py`、`catalog.py`、`prompt.py`、`store.py`；P0 只使用 window/query/target/prompt，忽略 PDF/JSON/图片场景标志并提示。
4. 移植 `delivery/base.py`、`folder.py`；worker 执行文件夹投递并记录状态。
5. 新增 `list_scenes/export_job/list_jobs/get_job/recover_jobs`；启动时 `recover_jobs`。
6. C# 增加场景选择、folder 目标、任务历史、打开任务目录；保存对话框保留为可选镜像。
7. 测试全部使用临时目录和合成数据，不接真实微信。

**P1：Obsidian + 剪贴板**

1. 移植 `obsidian.py`，增加“拒绝覆盖/包含 job id”的选项；UI 选择 vault。
2. 剪贴板由 C# 实现：worker 返回 prompt/artifacts，C# 写入剪贴板，再 `record_delivery`；或 worker 实现 `clipboard.py` 但 C# 仍负责用户确认。
3. 增加 `record_delivery`、`deliver_job`；失败任务可重试。
4. 所有剪贴板测试使用 fake backend，不在 CI 操作真实剪贴板。

**P2：Agent + 重试/恢复增强**

1. C# 实现 Agent 目标选择、dry-run、前台窗口选择、粘贴确认；worker 只记录结果。
2. 增加 `prune_jobs`、任务历史过滤、失败重试按钮；仍不自动恢复。
3. 只有在前两步稳定后才做真实 Windows 手工验收；不把 Agent 放进无人值守流程。

**P3：可选**

- 自定义场景编辑/删除（`save_scene/delete_scene`）；
- 场景输出标志的完整支持（需要先实现 JSON/PDF，不在本评估范围）；
- 任务保留策略、导出索引、批量操作。

### 6.2 测试方案（合成数据、无 AI/网络）

`【事实】` 上游测试已经提供了可复用的测试思路：任务 store 的原子提交/失败/恢复（`tests/test_job_store.py:69-110,149-179`），投递的 fake backend（`tests/test_delivery.py:21-40,125-204`），场景文件合并/损坏/提示词渲染（`tests/test_scenes.py:102-136,158-206`），CLI 场景投递（`tests/test_cli.py:66-108`），以及环境隔离（`tests/conftest.py:13-25`）。本项目现有 worker 测试通过真实子进程验证 JSON Lines、分页、原子写和错误（`tests/PythonWorker.Tests/test_worker.py:34-44,78-137,172-258`）。

| 层 | 必测内容 | 数据/依赖 |
|---|---|---|
| Python 单元 | manifest/state 往返、job id/artifact 名校验、短预览、哈希、窗口边界 | 合成 manifest、临时目录 |
| 任务 store | staging 只在 Ready 可见、失败进 Failed、上下文 abort、recover_stale、prune 保留 Ready/RUNNING | 临时 jobs 根目录；注入导出失败 |
| 场景 | 4 个内置 id/字段、窗口语义、覆盖/删除/损坏 JSON、未知占位符、固定引用规则 | 临时 `scenes.json`，不调用模型 |
| 文件夹 | 日期目录、文件名清洗、产物顺序、`*_assets` 复制、提示词文件、覆盖行为、权限错误 | 临时目标目录；fake 写失败 |
| Obsidian | vault 不存在报错、frontmatter、相对链接、附件目录、同日覆盖策略 | 临时 vault |
| 剪贴板 | 文本/文件列表、无产物、backend 拒绝、错误转 DeliveryError | fake backend；CI 不碰真实剪贴板 |
| Agent | 目标存在/缺失、dry-run、path_only、复制失败/激活失败/粘贴失败的状态 | fake window/backend；不启动真实应用 |
| 协议 | 新命令信封、能力位、jobId/路径校验、stdout 只有一行一个 JSON、错误码、超时 | 真实子进程 + 临时目录 |
| C# | `Protocol` 解析、`WorkerClient` 超时/退出、fake worker 的 jobs/scenes 响应、ViewModel 投影 | 扩展 `tests/NativeApp.Tests/fixtures/fake_worker.py`；不点真实 UI |
| 端到端 | 合成会话 → 场景 → 任务目录 → folder 投递 → 历史显示；失败后重试 | `demo/conversations.json` 或上游 `examples/demo_chat.json`，临时目录 |

`【建议】` 关键测试原则：

- 所有测试的 jobs/scenes/output/vault 都用临时目录或环境变量覆盖；断言没有写到项目根、用户目录或真实微信目录。
- 不把真实微信路径、真实账号、真实聊天内容放进 fixture；继续使用 `demo/conversations.json`（虚构）和上游 `examples/demo_chat.json`（虚构）。
- 失败注入通过测试替换/依赖注入完成，不在生产协议里加“测试后门”。
- 剪贴板和 Agent 的 CI 测试只验证 fake backend 和状态映射；真实 Win32 行为作为手工验收，并明确记录“未自动化验证”。
- 路径安全测试：`..`、绝对路径、符号链接、非法 job id、artifact 名嵌套、目标路径不存在/不可写、已存在覆盖。
- 协议测试：新命令不改变旧命令；旧 worker 没有能力位时 UI 禁用；stdout 诊断泄漏检查；stderr 不含 prompt/会话名。
- 隐私测试：`list_jobs` 不返回正文；`AppLog` 不含路径/会话名；任务日志策略符合声明。

### 6.3 验收标准

`【建议】` MVP 验收至少满足：

- 一次导出生成 `Ready/<job_id>/`，含 manifest、state、`files/*.md`；哈希与文件内容一致；
- 投递成功后状态为 DELIVERED；失败为 FAILED 且错误可见；无目标时 READY；
- 应用重启后能看到历史，遗留 staging 被归入 Failed；
- folder 投递的提示词、日期目录、文件名符合约定；
- 不新增 Python 运行时依赖（除既有 PyInstaller 构建依赖），不新增网络/AI 调用；
- 现有 20 项 Python 测试和 22 项 C# 测试继续通过，新增测试覆盖上述路径；
- 所有测试数据为合成，未读取真实微信数据库/聊天。

## 7. 优先级与工作量估算

`【建议】` 以下为工程估算，不是实测工时；以当前 stdlib worker 和 WinUI 壳为基础，不含真实微信、PDF/图片/语音、安装器、代码签名。

| 阶段 | 范围 | 估算（人日） |
|---|---|---|
| P0 | 任务模型/store/原子提交、场景读取/渲染、folder 投递、`export_job/list_jobs/get_job/recover_jobs`、C# 场景/目标/历史、测试 | 8–12 |
| P1 | Obsidian、剪贴板、`record_delivery/deliver_job`、覆盖策略、测试 | 5–7 |
| P2 | Agent（C# 前景窗口/粘贴）、dry-run、重试/清理、手工验收 | 4–6 |
| 合计 | 不含 P3 自定义场景编辑和 PDF/JSON | 17–25 |

`【事实】` 上游 `jobs/`、`scenes/`、`delivery/` 三目录约 2.1k 行源码，`pipeline.py` 370 行，相关测试约 0.9–1.2k 行（按文件行数，未逐项筛选）。`【建议】` 工作量主要在适配层、WinUI 交互和测试，不在算法；不要按“复制 2k 行”低估协议/状态/隐私测试。

## 8. 明确不建议或无依据的部分

- `【不建议】` 直接复制 `pipeline.py` 到当前 worker。它会引入 `service.ExportService` 和 PDF/渲染依赖，破坏 stdlib-only/PyInstaller 边界（`pipeline.py:44-53`，`pyproject.toml:13-23`，`worker.py:37-43`）。
- `【不建议】` 复用上游 `paths.py` 的默认输出根 `C:/微信记忆`；它正好是本仓库根目录（`paths.py:21-32`）。也不要在没有提示的情况下复用上游 `%LOCALAPPDATA%\WeChatAIMemory` 任务/场景目录。
- `【不建议】` 默认开启剪贴板或 Agent 自动投递。剪贴板是全局的，Agent 没有目标白名单；上游只测了 fake backend，真实前景窗口行为未验证（`tests/test_delivery.py:21-40`；`delivery/agent.py:56-86`）。
- `【不建议】` 把 `scene` 说成“AI 总结”或“自动复盘”。场景只是提示词模板，应用不调用模型（`scenes/prompt.py:36-82`；上游 README `:114`）。
- `【不建议】` 在 P0 承诺 PDF/JSON/图片页场景。当前 worker 只有 Markdown；场景的 `json_output/include_image_pages` 无法兑现（`worker.py:238-244`，`scenes/models.py:38-54`）。
- `【不建议】` 声称“任务可重试”已经实现。上游文档提到重试，但 CLI/源码没有独立重试命令（`docs/jobs-and-delivery.md:50-61`；`cli.py:25-63`）。
- `【不建议】` 声称“GUI 有任务历史/恢复”。上游 GUI 没有；`recover_stale` 只在 CLI 调用（`cli.py:106`）。native 的历史/恢复是新增工作。
- `【不建议】` 多实例共享同一个任务根目录。上游没有锁/单实例保护（`jobs/store.py` 无锁；`App.xaml.cs:11-27` 无单实例）。
- `【不建议】` 依赖“同日 Obsidian 笔记不会覆盖”或“folder 不覆盖同名文件”。实际会覆盖（`delivery/obsidian.py:49-66`；`delivery/folder.py:38-55`）。
- `【不建议】` 认为 `*_assets` 已被逐个哈希/校验。`collect_artifacts` 只收集顶层 `.pdf/.md/.json`，assets 不单独哈希（`jobs/store.py:394-419`）。
- `【不建议】` 认为任务日志是纯元数据。上游日志会写会话名和目的路径（`jobs/store.py:176-193,232-240`）。
- `【不建议】` 把上游 manifest 的 naive 时间直接当本地时间用；`as_utc` 会按 UTC 解释，文件夹日期也按 UTC（`jobs/models.py:60-103,238-241`；`delivery/folder.py:35`）。
- `【未验证】` 真实 Windows 剪贴板历史/云同步、真实 Agent 窗口匹配、真实 Obsidian vault/插件同步、不同 DPI/焦点/权限下的粘贴、断电持久性、并发多实例、恶意 JSON/符号链接攻击，均未在本项目或上游测试中验证；不能作为已支持能力宣传。

## 9. 主要证据索引

- 任务模型与存储：`reference/wechat-ai-memory/src/wechat_context_exporter/jobs/models.py`、`jobs/store.py`、`pipeline.py:74-120,126-181,246-344`。
- 投递：`delivery/base.py`、`folder.py`、`obsidian.py`、`clipboard.py`、`agent.py`、`win32.py`。
- 场景：`scenes/models.py`、`catalog.py`、`prompt.py`、`store.py`。
- 上游测试：`tests/test_job_store.py`、`test_delivery.py`、`test_scenes.py`、`test_scene_delivery.py`、`test_pipeline.py`、`test_cli.py`、`conftest.py`。
- 本项目：`src/NativeApp/MainWindow.xaml:66-81`、`ViewModels/MainViewModel.cs:39-54,131-144`、`Services/WorkerClient.cs:62-104`、`Services/WorkerLocator.cs:7-31`、`src/PythonWorker/worker.py:238-244,304-365`、`docs/protocol.md:3-39`。
- 许可证/隐私基线：`docs/source-assessment.md:190-230`、`docs/final-report.md:82-88`、`README.md:387-415`、`THIRD_PARTY_NOTICES.md`。
