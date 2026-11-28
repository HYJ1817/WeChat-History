# 本地工作进程协议 v1

应用只通过一个受控的本地子进程访问数据。通信是 stdin/stdout 的 UTF-8 JSON Lines：没有 HTTP、端口、socket 服务、遥测或远程 AI。请求串行执行；stdout 每行只有一个 JSON 对象；诊断只写 stderr，客户端不显示也不记录正文。

## 信封

请求：

```json
{"protocolVersion":1,"requestId":"唯一非空字符串","type":"request","command":"hello","payload":{}}
```

成功：

```json
{"protocolVersion":1,"requestId":"原值","type":"response","ok":true,"result":{}}
```

失败：

```json
{"protocolVersion":1,"requestId":"原值","type":"response","ok":false,
 "error":{"code":"INVALID_ARGUMENT","message":"可理解的说明","retryable":false}}
```

进度事件（同一 requestId，可能在最终响应之前出现多次）：

```json
{"protocolVersion":1,"requestId":"原值","type":"progress","current":3,"total":8,"label":"正在读取消息"}
```

- 每个输入行恰好一个最终响应；`progress` 不是最终响应。
- 语法损坏或缺失合法 requestId 时，错误响应的 requestId 为 `null`；已解析的非空字符串 ID 会原样返回。
- 单个连接内不允许重复 requestId。
- 单行上限 1 MiB。客户端只接受匹配 ID 和 v1 的最终响应；无效 stdout 会使连接失效。
- 客户端默认超时 30 秒；连接、PDF、任务、语音使用更长的命令级超时。超时只终止并回收本应用启动的 worker 进程本身（不终止其启动的微信），并删除该 worker 的私有临时目录。

## 数据源与日期

- `source` 省略时为 `"demo"`；支持 `"demo"`、`"wechat4"`，以及成功执行 `import_json` 后的 `"offline"`。其他值返回 `SOURCE_NOT_FOUND`。
- `startDate` / `endDate` 是 `YYYY-MM-DD` 或 `null`。省略表示该端不限；起日不可晚于止日。
- 日期按 UTC+08:00（无夏令时）解释，不随电脑时区变化；用户选择的起日和止日**都包含**。内部区间为 `[起日 00:00+08:00, 止日次日 00:00+08:00)`。
- 真实消息的时间戳在协议中统一为带 `+08:00` 的 ISO 8601。
- 开始和结束日期顺序检查由 UI 的日期范围校验执行；离线导入前也先校验当前日期范围有效，再发送导入请求。

### 离线 JSON 导入

| command | payload | result |
|---|---|---|
| `import_json` | `{path}`：用户选定的绝对 `.json` 文件路径 | `{source:"offline",schema:"wechat-memory-json-v1"|"demo-json-v1",conversationCount,messageCount}` |

- 支持本应用导出的 JSON v1 和兼容的既有 demo JSON；完整校验完成后才替换当前数据源并保留只读内存 snapshot。失败时不切换数据源；不合并历史、不按 ID 去重，也不改写或删除原 JSON。成功后查询只用内存 snapshot，不会再次打开原件。
- 文件以 UTF-8 读取，最大 64 MiB、10,000 个会话、200,000 条消息。合法的 `conversations:[]` 是空历史，不是格式错误。重复 JSON 字段、无效 UTF-8/Unicode、非法时间、超出可表示范围的时间及未来不支持的版本会拒绝；会话 ID 重复、同一会话内消息 ID 重复也会拒绝。
- 离线数据模型不跟随 JSON 内容里可能出现的媒体、音频、文件或网络路径。图片只显示占位；已有语音转写和 `duration_ms` 时长保留。未经识别的字段（包括外部路径字段）不会作为资源打开。
- 导入只替换本应用数据源，不启动或重启微信；UI 成功后进入离线只读模式。

## 命令

### 基础

| command | payload | result |
|---|---|---|
| `hello` | `{}` | `protocolVersion`, `capabilities`, `timezone`, `pageSizeLimit` |
| `shutdown` | `{}` | `{shutdown:true}`；响应后退出，stdin EOF 也正常退出 |

`capabilities` 当前包含：`demo`, `wechat4`, `offline_json`, `accounts`, `message_search`, `message_pagination`, `markdown_export`, `json_export`, `pdf_export`, `png_pages`, `image_recovery`, `voice_transcribe`, `tasks`, `scenes`, `delivery`, `progress_events`。

### 真实微信连接

| command | payload | result |
|---|---|---|
| `list_accounts` | `{}` | `items:[{token,label,lastUsed,recent}]`；`token` 是账户目录的哈希，不是账户 ID；未连接时 `label` 只含最近使用时间 |
| `connect` | `{source:"wechat4",accountToken?,enableImages?,enableVoice?}` | `connected`, `source`, `accountLabel`, `conversationCount`, `messageTableCount`, `imageKeyAvailable`, `voiceAvailable`, `capabilities` |
| `disconnect` | `{}` | `{connected:false}`；清理解密临时目录与缓存 |

`connect` 会关闭并重新启动微信一次以捕获内存中的数据库密钥；密钥不会出现在任何请求/响应中。连接成功后 `accountLabel` 优先返回本机微信昵称（从只读联系人表取得），失败时才回退到通用标签；服务端不返回账户目录、数据库路径或密钥。`enableImages` 需要可用的图片密钥；`enableVoice` 只建立语音缓存，实际转写需模型。

### 会话与消息

| command | payload | result |
|---|---|---|
| `list_conversations` | `{source,query?}` | `items:[{id,title,messageCount,kind,countKnown}]` |
| `get_messages` | `{source,conversationId,startDate?,endDate?,query?,order?:asc\|desc,offset:0,limit:50}` | `items:[消息]`, `total`, `nextOffset`（无后续为 `null`） |

消息：

```json
{"id":"...","sender":"...","timestamp":"2026-11-24T09:12:00+08:00",
 "content":"...","kind":"text|image|file|voice|system",
 "isOutgoing":false,
 "attachmentPath":"可选：私有临时图片路径",
 "durationMs":1234,
 "transcript":"可选：本地语音转写"}
```

- `offset` 0–1,000,000,000；`limit` 1–100，默认 50。
- `order` 仅 `asc`（默认，按时间正序）或 `desc`（最新在前）；导出始终按 `asc` 读取，界面预览使用 `desc`。
- `query` 在发送者、内容文本和类型相关文本中做大小写不敏感匹配。
- 真实来源在会话列表中的 `messageCount` 为 0 且 `countKnown:false`（不预先扫描所有数据库）。
- 非文本消息：图片不可读时 `kind:"system"`、内容 `[图片]`；文件/语音/未知类型给明确占位。图片可恢复时 `kind:"image"`，`content` 仍为 `[图片]`，并提供私有 `attachmentPath`。

### 导出

| command | payload | result |
|---|---|---|
| `export_markdown` | `{source,conversationId,startDate?,endDate?,query?,outputPath,overwrite:false}` | `path`, `messageCount`, `bytes` |
| `export_json` | 同上，`outputPath` 必须以 `.json` 结尾 | `path`, `messageCount`, `bytes`, `format`, `pageCount`, `chatPageCount`, `imagePageCount`, `pagesDir`, `assetsDir` |
| `export_pdf` | 同上，另可选 `pagesDir`, `materializeMedia` | 同上 |

- `outputPath` 必须是绝对路径，父目录必须已存在；扩展名必须匹配。
- `overwrite` 默认 `false`；目标已存在时返回 `OUTPUT_EXISTS`。原生保存对话框确认后客户端才传 `true`。
- 写入使用同目录临时文件 + 原子替换；失败不破坏已有文件。
- `export_json`/`export_pdf` 由 `wechat_memory.exporters` 实现；Markdown 仍保留与第一阶段一致的演示输出，真实来源使用通用写入器。
- PDF 使用系统中文字体；无字体时返回固定错误。可选 `pagesDir` 保存 `page_NNNN_chat.png`；`materializeMedia=true` 且图片可读时，每张图片在对应聊天页之后追加一页 `page_NNNN_image.png`，并计入 `pageCount` 与 500 页上限。
- 普通导出读取上限为 200,000 条消息；PDF 另有限制：最多处理 20,000 条消息且最多 500 页。PDF 超限返回 `PAGE_LIMIT_EXCEEDED`。
- `materializeMedia=false` 时只写占位/转写文本，不复制图片文件。

### 场景

| command | payload | result |
|---|---|---|
| `list_scenes` | `{}` | `items:[场景]`, `warnings` |
| `get_scene` | `{sceneId}` | `{scene}` |
| `save_scene` | `{scene:{...}}` | `{scene}` |
| `delete_scene` | `{sceneId}` | `{deleted:boolean}` |

场景字段：`id`（小写字母/数字/`.`/`_`/`-`，1–40 字符）、`name`、`description`、`conversationId`、`days`、`start`、`end`、`query`、`include_image_pages`、`markdown`、`json_output`、`target`、`prompt`、`shortcut`、`builtin`。内置场景不能被删除；保存同 ID 会创建用户覆盖（返回 `builtin:false`）。

### 任务与投递

| command | payload | result |
|---|---|---|
| `export_job` | `{source,conversationId,formats:["markdown"|"json"|"pdf"],target,sceneId?,prompt?,...筛选}` | `{job,delivery,prompt}` |
| `list_jobs` | `{limit:50}` | `items:[job], warnings` |
| `get_job` | `{jobId,includePrompt?,includePreviews?}` | job 详情（含 `job`, `manifest`, `state`, `artifacts`，可选 `prompt`） |
| `deliver_job` | `{jobId,target}` | `{job,delivery}`；仅 `ready`/`failed` 可投递 |
| `record_delivery` | `{jobId,status:"delivered"|"copied"|"failed",target,destination?,detail?,error?}` | 更新后的任务 |
| `recover_jobs` | `{}` | `{recovered:[jobId]}` |
| `preview_prune_jobs` | `{days}`；Native UI 仅提供 `30`, `90`, `180`, `365` | `{dryRun:true,days,cutoff,candidateCount,knownArtifactBytes,unknownSizeCount,skippedReasonCounts,previewToken}` |
| `confirm_prune_jobs` | `{days,previewToken}` | `{dryRun:false,days,cutoff,removedCount,skippedCount,skippedReasonCounts}` |
| `prune_jobs` | `{days}` | `{removed:[jobId]}`；旧客户端兼容入口，Native UI 不调用 |
| `get_job_artifact_path` | `{jobId,name}` | `{path}`（路径限制在任务目录内） |

目标形状：

```json
{"kind":"none"}
{"kind":"folder","root":"C:\\path","organizeByDate":true}
{"kind":"obsidian","vault":"C:\\vault","folder":"微信记忆","attachments":"_附件","dedupe":true}
```

- `clipboard`/`agent` 在任务模块中存在，但本版本不执行；任务保持 `ready`，投递状态为 `none`。
- 任务目录必须绝对、位于项目树与上游默认目录之外；默认使用 `%LOCALAPPDATA%\WechatMemoryNative\jobs`。
- 任务工件带 SHA-256 清单；Obsidian 不覆盖已有笔记；文件夹投递复制到目标目录。
- `list_jobs` 默认不含消息预览；`get_job` 只有显式 `includePreviews:true` 才返回预览文本。Native 任务详情复用 `get_job`，工件打开路径复用 `get_job_artifact_path`。

#### 手动清理与保护边界

- Native UI 只开放 30 / 90 / 180 / 365 天；worker 命令参数目前验证 1–3650 天。预览只列出达到保留期限、状态为 `DELIVERED`、`COPIED` 或 `EXPIRED` 的本地任务候选。未完成、失败或可恢复任务不作为候选；变化、近期写入、格式不支持、不可读、占用或无法建立保护的任务会跳过。
- `knownArtifactBytes` 是候选任务目录 `files/` 下已统计文件的逻辑长度（含附件），不是预计能释放的实际磁盘空间；`unknownSizeCount` 是跳过/无法统计的数量，不是额外候选。外部文件夹或 Obsidian 已投递副本不属于任务目录，清理不会触及；用户 JSON 原件也不属于清理范围。
- `preview_prune_jobs` 的完整 snapshot 留在 worker 内存，只把摘要和授权 token 返回 UI；worker 同时最多保留 3 个 token，每个有效 10 分钟，worker 重启后失效。`confirm_prune_jobs` 仅接受 `{days,previewToken}`；匹配天数后会在开始删除前消费 token，因此不支持重放。关闭预览或永久删除确认对话框时 UI 不发送确认请求，不会清理。Native UI 不把大 snapshot 发回 worker。旧 `prune_jobs` 仅为兼容旧客户端而保留，Native UI 不调用。
- 执行只允许 Windows 本地固定 NTFS 卷；必须成功建立并持有文件/目录保护，否则 fail closed 并跳过。保护、指纹和预览只覆盖已枚举目录项及 NTFS 默认文件流（unnamed/default stream）；备用数据流 ADS 不在预览、指纹或防写保护中，却会随宿主文件一起删除，包括确认后新建的 ADS。若 ADS 中存有资料，先取消并备份。
- 删除是永久删除，不经过回收站，也不是原子事务；I/O 失败可能造成部分删除，不承诺回滚或总成功。若 worker 连接在执行中断开，重新打开并刷新任务历史核实实际结果。

设计参考：[WeChatBridge 固定源码快照](https://github.com/freestylefly/WeChatBridge/tree/04cb2e3c783548f539606e2f19ece7afb99280b5)中的持久化状态转换、中断恢复及未完成批次保护，为事务式启用和保留期保护提供思路。本项目在现有 WinUI 3 + Python worker 中独立适配，未复制其代码或素材；注意该 SHA 对应的提交本身仅优化图片资源，并非上述功能逻辑的变更提交。

### 语音

| command | payload | result |
|---|---|---|
| `voice_status` | `{}` | `model`, `installed`, `componentAvailable`, `requiresDownload`, `connectedVoice` |
| `prepare_voice_model` | `{consent:true}` | `{ready:true,model:"small"}`；没有 `consent:true` 返回 `CONSENT_REQUIRED` |
| `transcribe_messages` | `{source,conversationId,startDate?,endDate?,query?}` | `{total,transcribed}` |

- 模型下载到 `%LOCALAPPDATA%\WechatMemoryNative\models`，默认 `small`；下载需要联网，转写在本机进行。
- 转写结果写入本进程的转写缓存，不修改微信数据库；后续 `get_messages` 的语音消息可带 `transcript`。
- 未启用语音、缺少组件或模型时会返回固定错误码，不会静默伪造转写。

## 错误码

协议/本地：

`INVALID_JSON`, `INVALID_REQUEST`, `PROTOCOL_MISMATCH`, `DUPLICATE_REQUEST`, `UNKNOWN_COMMAND`, `INVALID_ARGUMENT`, `SOURCE_NOT_FOUND`, `SOURCE_INVALID`, `SOURCE_TOO_LARGE`, `SOURCE_UNSUPPORTED`, `SOURCE_SWITCH_FAILED`, `CONVERSATION_NOT_FOUND`, `OUTPUT_NOT_WRITABLE`, `OUTPUT_EXISTS`, `EXPORT_FAILED`, `PAGE_LIMIT_EXCEEDED`, `FONT_UNAVAILABLE`, `INTERNAL_ERROR`, `CAPABILITY_UNAVAILABLE`。

客户端本地：

`WORKER_START_FAILED`, `WORKER_EXITED`, `PROTOCOL_ERROR`, `REQUEST_TIMEOUT`。

真实微信：

`WECHAT_NOT_FOUND`, `WECHAT_EXE_NOT_FOUND`, `WECHAT_LOGIN_TIMEOUT`, `WECHAT_NOT_CONNECTED`, `WECHAT_DB_UNSUPPORTED`, `WECHAT_DB_BUSY`, `WECHAT_KEY_UNAVAILABLE`, `WECHAT_NO_MESSAGES`, `ACCOUNT_NOT_FOUND`, `RESULT_LIMIT_EXCEEDED`, `ACCESS_DENIED`, `CANCELLED`。

任务/场景：

`SCENE_INVALID`, `SCENE_NOT_FOUND`, `JOB_NOT_FOUND`, `JOB_NOT_READY`, `JOB_PATH_UNSAFE`, `JOB_STORE_UNAVAILABLE`, `TARGET_NOT_SUPPORTED`, `PRUNE_PREVIEW_REQUIRED`, `PRUNE_PREVIEW_UNKNOWN`, `PRUNE_PREVIEW_EXPIRED`, `PRUNE_PREVIEW_MISMATCH`, `EXPORT_FAILED`, `TASK_FAILED`。

语音：

`CONSENT_REQUIRED`, `MODEL_DOWNLOAD_FAILED`, `VOICE_UNAVAILABLE`, `VOICE_COMPONENT_MISSING`, `VOICE_FAILED`, `TRANSCRIBE_FAILED`。

错误消息是固定的中文说明；不包含路径、账户、聊天正文、密钥或第三方异常文本。未知内部异常只返回 `INTERNAL_ERROR`，诊断写 stderr（客户端不显示、不记录正文）。

## 生命周期

关闭窗口时客户端先发送 `shutdown`，最多等待 2 秒；仍未退出时只终止自己持有的 worker 进程本身，不使用进程树终止，也绝不按进程名清理其他 Python 或微信进程。真实连接期间，worker 会在断开/退出时关闭数据库与图片缓存并删除私有临时目录。正常关闭、请求超时、读取失败和强制终止后由桌面端删除整个 worker 私有根；异常崩溃遗留由下次启动按锁文件清理，无锁旧目录仍以 6 小时阈值兼容。

## 手工试验（演示数据）

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -I -u .\src\PythonWorker\worker.py --demo .\demo\conversations.json
```

逐行输入：

```json
{"protocolVersion":1,"requestId":"a","type":"request","command":"hello","payload":{}}
{"protocolVersion":1,"requestId":"b","type":"request","command":"list_conversations","payload":{"source":"demo"}}
{"protocolVersion":1,"requestId":"c","type":"request","command":"get_messages","payload":{"source":"demo","conversationId":"demo-chat-001","startDate":"2026-11-24","endDate":"2026-11-24","order":"desc","offset":0,"limit":50}}
{"protocolVersion":1,"requestId":"d","type":"request","command":"export_json","payload":{"source":"demo","conversationId":"demo-chat-001","outputPath":"C:\\Temp\\demo.json","overwrite":true}}
{"protocolVersion":1,"requestId":"e","type":"request","command":"shutdown","payload":{}}
```

`tests/PythonWorker.Tests` 通过真实子进程管道执行演示链路、导出、任务/场景/投递与错误路径，不依赖 `reference/`。
