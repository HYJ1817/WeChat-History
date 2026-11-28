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

已发布 v1 基线的 `capabilities` 包含：`demo`, `wechat4`, `offline_json`, `accounts`, `message_search`, `message_pagination`, `markdown_export`, `json_export`, `pdf_export`, `png_pages`, `image_recovery`, `voice_transcribe`, `tasks`, `scenes`, `delivery`, `progress_events`。当前工作分支增加的能力及 `hello` 附加字段见下文；它们不因存在于分支而自动成为已发布能力。

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

<a id="message-query-roadmap"></a>

## A/B/C 增量契约（当前工作分支实现；尚未发布）

> **发布状态**：上文是既有 v1 发布契约。本节描述当前工作分支已实现、但尚未发布的附加 worker 命令、字段、Native 预览工作流及 parser/export 接入。实现存在不等于已验证；具体自动化、构建和 UI 验收证据与未验证边界分列于本节末尾。协议仍使用 v1 JSON Lines 信封，不改变既有命令的原有字段语义；本分支功能尚未进入安装包或任何已发布版本。

### 能力、来源与共同边界

当前分支 `hello` 在既有基线字段之外还返回 `source`, `sourceSessionId`, `sourceCapabilities`。`capabilities` 增加 `global_message_search`、`message_context`，并在至少一个来源支持时增加分页能力；`sourceCapabilities` 按来源给出实际可用能力：

| `sourceCapabilities` 来源 | 当前能力 |
|---|---|
| `demo` | `message_cursor_pagination`, `message_keyword_pagination` |
| `offline` | `message_cursor_pagination`, `message_keyword_pagination` |
| `wechat4` | `message_cursor_pagination`, `message_keyword_pagination` |

全局搜索、上下文、B2 稳定游标和 B3 关键词分页在当前分支的三种来源均可用。`get_messages_page` 中 `query` 省略或为 `null` 选择 B2；传入字符串（包括空串/纯空白）选择 B3。能力取决于运行 worker 的实现，Native 会优先使用每来源映射；旧 worker 缺少新能力时保留原浏览路径或不启用新入口。不能只凭聚合 `capabilities` 推断某来源支持分页。

- A 搜索当前激活来源中全部可读会话，不与其他账号或离线/实时来源混合，也不受会话列表名称筛选限制。`wechat4` 范围是适配器发现并能读的会话表，不承诺恢复已删除会话、孤立表或不可读分片。
- B2/B3 是单会话预览分页；UI 已接入有界前后窗口。它们不改变完整范围导出、任务或语音命令，也不把已加载的 UI 页当成完整导出范围。
- 微信数据库按只读方式访问，不在原库/WAL 建搜索索引；没有持久明文 FTS、跨来源联合搜索、云端/AI 搜索、OCR、自动转写或客户端 SQL。新搜索/分页不触发微信重启；原 `connect` 行为不变。XML 解析不会跟随 URL/附件路径、发起网络或脚本/文件 I/O。
- live 查询按 worker 已发现的分片/表工作并使用有界读取。会话位置去重，但消息不会按 `server_id`、`local_id`、时间或正文去重；公开消息 ID 可能重复。超时为协作式预算，不能中断所有磁盘/解密阻塞；Native 的 A/B 新请求使用 60 秒命令超时，既有客户端默认超时仍为 30 秒。

### 来源身份与失效

`sourceSessionId` 是不透明的 worker 内存来源实例令牌，不是账户 ID，也不是可持久化 ID。当前分支在 `hello` 返回当前来源和令牌；`list_conversations`、成功的 `connect`、成功的 `import_json` 和 `disconnect` 也返回 `sourceSessionId`。初始来源为 `demo`。新 A/B 命令必须携带令牌并匹配当前来源；B2/B3 还要求显式提供 `source`。

- `list_conversations` 首次访问不同来源时会激活它并轮换令牌；同一激活来源的重复列举不会仅因列举而轮换。
- 成功 `connect` 每次都换令牌（包括重新连接 `wechat4`）；成功导入完成全部验证、切换后换令牌；`disconnect` 总是切回 `demo` 并换令牌。有效切换会清除 A/B3 会话并使相应分页提供者失效。
- JSON 验证失败不会切换来源或改变旧令牌。`connect` 候选连接失败时现有来源/令牌保持不变。只有成功响应中的新令牌可用于新会话。
- live 数据库快照变化、会话 TTL 到期、内存淘汰、parser/排序版本变化或 B3 媒体/转写修订，均可使既有搜索/页失效。失效后应重新从搜索/首屏开始，不把新数据接在旧结果上。多分片快照经逐片验证，不声称是跨分片同一瞬间的原子快照；签名不是对抗所有时间戳篡改的安全快照。

全局搜索和上下文使用规范正文纯投影；不恢复 live 图片/语音，不访问 live 转写缓存。离线 A 搜索可搜索导入的既有 `transcript` 字段；B3 保留既有单会话 sender/content/kind 谓词，transcript 只在既有内容投影（例如 live 已启用语音时的渲染内容）中影响匹配，不额外搜索隐藏元数据。B2 普通消息预览沿用其既有媒体显示语义。


### A：全局搜索与命中上下文（当前分支已实现）

当前分支在 `hello.capabilities` 声明 `global_message_search`、`message_context`；命令为 `search_messages` 和 `get_message_context`。Native 工作流与三种来源的 worker JSON-lines 合成协议链路均有自动化测试。既有单会话浏览、搜索和导出仍保留。

`search_messages` 首次请求（`source` 可省略但默认值是 `demo`；必须与当前激活来源匹配）：

```json
{"source":"wechat4","sourceSessionId":"…","query":"关键词","startDate":"2026-11-24","endDate":"2026-11-24","order":"desc","offset":0,"limit":50}
```

允许字段仅为 `source`, `sourceSessionId`, `query`, `startDate`, `endDate`, `order`, `offset`, `limit`。`query` 必须是非空白字符串，UTF-8 编码最多 4 KiB；日期可省略或为 `null`，沿用上文 UTC+08:00 包含式日期；`order` 为 `asc` / `desc`，默认 `desc`；首次 `offset` 必须为 0，默认 0；续页 `offset` 范围 0–1,000,000,000；`limit` 默认 50，范围 1–100。A 命令的 `requestId` 最多 256 个 UTF-8 字节；`sourceSessionId`、`searchId` 和 `hitId` 必须是非空字符串，最多 128 字符。

成功结果字段：`searchId`, `sourceSessionId`, `items`, `total`, `resultLimit`（固定 1000）, `truncated`, `nextOffset`, `coverage`。每个命中有 `hitId`, `conversationId`, `conversationTitle`, `conversationKind`, `messageId`, `sender`, `timestamp`, `kind`, `snippet`, `matchedFields`；live 复杂消息还可带下文 C 的可选展示字段。`matchedFields` 只含 `sender`, `content`, `kind`, `transcript`。搜索续页只接受 `{source?,sourceSessionId,searchId,offset?,limit?}`；`offset` 默认 0，不能替换关键词、日期或顺序。

- 匹配是 Unicode `casefold()` 后的子串匹配，字段为发送者、规范正文、消息 kind，以及当前消息投影中的 transcript；`%` 和 `_` 是普通字符，不是 SQL 通配符。A 搜索 live 来源不恢复媒体，也不读 live 转写缓存，因此该来源的 transcript 不由独立隐藏缓存字段贡献；offline 导入的既有转写可以独立匹配。匹配完整正文后才生成摘要：正文窗口最多 240 字符，边界可能另加 `…`；不含全文正文，不作为 HTML/链接执行。
- 首次请求对全部可读候选做完整有界扫描才发布结果。`total` 是完整扫描中的准确命中数；只保留所选排序的前 1000 个命中摘要及服务端 locator，`truncated` 等于 `total > 1000`。完整 locator 和投影大小在扫描阶段即时计入 A/B3 共用的查询缓存账本；计账超限时以 `SEARCH_CACHE_LIMIT` 失败、释放本次预留/已暂存资源，不发布部分结果。超过 1000 命中时仍只能翻阅这 1000 个已保留结果。续页只切内存缓存，不重扫来源；`nextOffset` 按实际发出的条数推进，受响应字节预算影响也保持连续。
- `coverage` 精确形状为 `{complete:boolean,scannedMessages:int,partialMessages:int,truncatedMessages:int,warnings:string[]}`。非 `ok` parser 状态或规范正文截断会计入 `partialMessages`；截断另计入 `truncatedMessages`。即使没有命中也返回覆盖信息。`complete` 描述规范正文解析覆盖，不代表所有原始 XML 字段/未知结构均可搜索，也不允许不完整扫描伪报准确 total。
- 搜索排序为稳定 `precise-v1` 并按所选方向反转全序。live A 的键为 `(epoch_microseconds, conversation.id, relative_path, table, physical_rowid)`；demo/offline 内存 A 的键为 `(datetime_microseconds, conversation.id, "memory", "messages", snapshot_index)`。Native 不重排。展示 `messageId` 可能重复，`hitId` 是服务端随机 locator 句柄，不暴露物理路径、表名或 rowid。

`search_messages` 续页请求示例：

```json
{"source":"wechat4","sourceSessionId":"…","searchId":"…","offset":50,"limit":50}
```

`get_message_context` 仅接受 `{source?,sourceSessionId,searchId,hitId,before?,after?}`；`source` 仍默认 `demo`，`before` / `after` 各默认为 20，范围 0–20。响应字段为 `{conversation,items,focusIndex,hasEarlier,hasLater}`：

- `conversation` 为 `{id,title,kind,messageCount,countKnown}`；live 不预先统计会话总量，返回 `messageCount:0,countKnown:false`；demo/offline 返回内存会话总条数和 `countKnown:true`。
- `items` 为带 `id,sender,timestamp,content,kind` 的纯投影消息，可含 `isOutgoing`、已有的 `durationMs` / `transcript` / `replyTo` 及 live C 展示字段；不返回 `attachmentPath`。按该会话的 `precise-v1` **升序**返回，不随原搜索的 `order` 改变。
- `focusIndex` 是目标在 `items` 中的零基索引；`items[focusIndex]` 是经过身份与内容指纹复核的原命中，邻居不受原搜索关键词/日期约束。`hasEarlier` / `hasLater` 表示返回窗口之外是否仍有更早/更晚消息，不是搜索命中页标志。

若响应预算不足，先删除最远邻居并更新索引/边界标志；目标本身超过预算则返回错误，不截断或替换目标。失效/变化的 locator 不会跳转到另一行。live 上下文元数据扫描同样最多 2,000,000 条；只有选中的邻居转换正文。Native 上下文最多 41 条，并按响应字段验证会话、命中身份和 focus。

全局搜索/上下文只是预览，不会把上下文片段作为完整会话交给导出或任务。它不恢复 live 图片/语音、不访问 live 转写缓存；不支持完整跨会话导出、持久索引、已删除/孤立会话恢复、OCR 或任意 XML 子字段搜索。

### B：分页与完整范围读取（当前分支已实现）

**B1：既有 offset 无关键词页优化**。live `get_messages` 在 `query` 为空时尝试安全的元数据快路径：同分片读事务中执行 COUNT；每张表最多读取 `offset + limit` 条候选元数据，再按兼容顺序合并，只有最终页转换正文/恢复媒体。它保留既有 total、offset、nextOffset 和 200,000 候选限制；不对每片独立套 OFFSET。旧 live 升序按 `(截断到秒的时间, local_id)` 稳定排序，平局保留位置枚举顺序和各表原读取顺序；降序反转整个序列，不等同于 `precise-v1`。只有行键、时间单位和旧排序比较器满足检查时才使用快路径；混合单位、精确原始时间/local_id 平局、无可用 rowid 等情形回退旧慢路径。查询仍可能让 SQLite 扫描或排序大量数据，深 offset 的开销随页深增长；这不是 keyset。

**B2：无关键词稳定游标**。当前 `hello.sourceCapabilities` 将 `message_cursor_pagination` 标在 `demo`、`offline` 和 `wechat4`。Native 对支持该能力的来源优先使用 B2；不支持/不兼容的旧 worker 或 live schema 会使用兼容路径，worker 本身不会把不安全 schema 静默当成稳定游标。

首次 `get_messages_page` 请求仅接受 `{source,sourceSessionId,conversationId,startDate?,endDate?,query?,order?,limit?}`；`source` 和 `sourceSessionId` 必须明确提供，`query` 仅可省略或为 `null` 才是 B2，`order` 默认 `desc`，`limit` 默认 50、范围 1–100。日期遵循 UTC+08:00 包含式日期。成功响应字段为 `{pageSessionId,items,total,previousCursor,nextCursor,sortVersion:"precise-v1"}`；`total` 为该会话日期范围内准确消息数，首屏 `previousCursor` 为 `null`，到达边界时相应游标为 `null`。B2 不回显 `sourceSessionId`。续页仅接受 `{source,sourceSessionId,pageSessionId,cursor,limit?}`，不接受任何替换筛选条件。

- live B2 对每个唯一消息分片取得并验证受控快照；首屏 COUNT 复用在会话中。只对指定会话/日期读有界 keyset 候选，再全局合并，不重读深 offset 的前缀；每张表要求统一 seconds/milliseconds/microseconds 时间单位及安全的行映射。当前唯一行键检查使用未被声明列遮蔽的 SQLite intrinsic rowid，不承诺任意主键或 `WITHOUT ROWID` 表支持。升序全序键为 `(epoch_microseconds, relative_path.casefold(), table.casefold(), physical_rowid)`；降序反转此全序。若 schema 不满足条件，返回明确错误，Native 可降级到旧读取路径。
- demo/offline B2 对内存数据快照分页；offline 使用成功导入后不可变的内存 snapshot，并由 `PageSessionManager` 管理会话生命周期。按完整时区时间精度排序；升序的相同时间戳保留来源顺序，降序反转整个升序序列（包括平局）。offline 导入源最大 200,000 条消息。内存 provider 均不把公开消息 `id` 当唯一游标键，也不按 ID 去重。
- 源 generation/快照在每次前后页操作时复核；内容变化返回 `PAGE_SOURCE_CHANGED`，会话 TTL/淘汰/未知游标返回 `PAGE_EXPIRED`，必须从首屏刷新。B2 不受 A 的 1,000 Top-K 或 live 旧 offset 的 200,000 候选上限约束（offline 导入自身仍有 200,000 条总量上限）。
- B2 的 `items` 沿用普通消息序列化字段及可选 live C 投影；**排序精度不等于 wire 时间戳精度**：live/offline B2 当前把 `timestamp` 输出到秒，而内部游标比较保留微秒精度。响应过大返回 `PAGE_RESPONSE_TOO_LARGE`，worker 不自动缩页或截断消息；调用者可减小 `limit` 后重新请求。

**B3：单会话关键词快照**。同一个 `get_messages_page` 命令在 `query` 是任意字符串（包括空串/纯空白串）时选择 B3；省略或 `null` 才选择 B2。首次字段为 `{source,sourceSessionId,conversationId,query,startDate?,endDate?,order?,limit?}`，`source`、`sourceSessionId`、`conversationId` 必须明确给出；`query` UTF-8 最多 4 KiB；`order` 默认 `desc`，`limit` 默认 50、范围 1–100。Native 对空白 UI 输入通常转成 `null`，但线协议直接传空字符串仍是 B3，按空字符串字面谓词匹配全部候选。

B3 响应字段为 `{pageSessionId,sourceSessionId,items,total,candidateCount,previousCursor,nextCursor,sortVersion:"precise-v1",projectionVersion,mode:"keyword",order}`；`candidateCount` 是匹配前的完整日期范围候选数，`total` 是匹配条数。首次请求完整扫描成功后，缓存完整的已匹配消息展示快照及定位信息；后续游标只切内存快照，不再扫描、解压、解析 XML 或恢复媒体。续页仅接受 `{source,sourceSessionId,pageSessionId,cursor,limit?}`，不能替换关键词、日期、会话、顺序或媒体选项。续页返回实际 `sourceSessionId`, `mode`, `order`, `candidateCount` 和 `projectionVersion`，并校验它们与原会话一致。

- B3 保留旧单会话 sender/content/kind 的 `casefold()` 字面匹配和有效媒体投影，不使用 SQL LIKE。升序 live 键为 `(epoch_microseconds, conversation.id, relative_path.casefold(), table.casefold(), physical_rowid)`；demo/offline 内存键为 `(datetime_microseconds, conversation.id, "memory", "messages", snapshot_index)`；降序反转全序。live 可读取已启用媒体/转写缓存，并在首次完整匹配时解析复杂消息；demo/offline 仅用内存模型，图片等引用不触发文件或网络 I/O。offline/demo 的 `projectionVersion` 是会话版本标记，不表示导入消息已重新由 appmsg parser 解析。
- B3 对匹配前候选数最多接受 200,000 条；超限返回 `RESULT_LIMIT_EXCEEDED`，不会把结果截成 A 的 1,000 条。`total` 不因分页而缩小。共享缓存放不下时返回 `SEARCH_CACHE_LIMIT`；响应超预算时整条消息不截正文，缩小本页条数并按实际条数推进游标，单条消息过大则返回 `RESPONSE_LIMIT_EXCEEDED`。
- 会话绑定来源令牌、关键词、日期、顺序、投影/排序版本及 live 媒体选择/修订。服务端实际来源/媒体条件变化返回 `PAGE_SOURCE_CHANGED` / `PAGE_CRITERIA_CHANGED` 并丢弃旧会话，之后再用旧会话返回 `PAGE_EXPIRED`；过期或无效 cursor 也返回 `PAGE_EXPIRED`。客户端在续页中夹带替换关键词/日期/会话/顺序，返回 `PAGE_CRITERIA_CHANGED`；夹带 `mode`、媒体选项、版本或 `offset` 等不接受字段，返回 `INVALID_ARGUMENT`。这些请求字段校验拒绝本身不销毁原有效会话。首次全扫描可能较慢，不能把软期限/缓存上限失败解释为零命中。

**B2/B3 共同输入与展示约束**：`conversationId` 必须是非空白字符串，最多 1024 字符；`sourceSessionId`、`pageSessionId`、`cursor` 必须是非空白字符串，最多 256 字符；这些命令的 `requestId` 最多 256 个 UTF-8 字节。客户端必须原样传回不透明令牌，不拼装游标或传物理位置。`previousCursor` / `nextCursor` 按请求的排序方向导航，不固定代表时间更早/更晚；续页可改变 `limit`。B3 的 `items` 是完整消息展示对象，使用普通消息字段及可选 live C 投影；A 命中/上下文和 B3 的 `timestamp` 均按微秒序列化。demo/offline 不补造 C 字段；live B2/B3 的媒体路径仅通过既有受控 `attachmentPath` 展示，offline 媒体引用不作为资源打开。

**Native 窗口与完整范围隔离**：Native 使用每页 50 条，最多保留 20 页且总展示预算最多 8 MiB；前后导航超过窗口时释放另一端。worker 线协议每页上限为 100。修改筛选后须重新应用/刷新首屏；Native 不对 worker 的 `precise-v1` 顺序重排，不按重复 ID 去重。兼容降级仅用于缺少能力或 `PAGE_UNSUPPORTED_SCHEMA`、`PAGE_SOURCE_UNVERIFIABLE`、`CAPABILITY_UNAVAILABLE`、`UNKNOWN_COMMAND`；预算、源变化、失效或响应校验错误不作为静默退回旧读取的理由。兼容路径继续使用原来的加载更多，性能和内存不具备有限窗口保证。普通导出、任务生成和语音仍从完整筛选范围读取，不沿 cursor 或 B3 展示缓存导出；旧普通消息读取/导出候选上限仍为 200,000，PDF 另限 20,000 条和 500 页。

### C：规范正文、引用与转发（当前分支已实现）

当前 `wechat_memory.appmsg_parser` 提供纯函数 `parse_appmsg`；对 live 微信消息类型 49 使用其 `canonical_text` 作为兼容的 `Message.content`，并将有界展示投影附加到普通预览、A 搜索/上下文、B2/B3 页。这些 live 消息在兼容模型中仍为 `kind:"file"`，卡片类别由 `displayKind` 表达，不新增 `kind:"link"` 等枚举。demo/offline 输入是已导入的消息模型，不会在运行时把 `content` 当 XML 再解析。

解析器识别 `<msg><appmsg>…</appmsg></msg>` 或 `appmsg` 根节点的直接字段；`appmsg/type` 支持 5=链接、6=文件、57=引用、19=合并转发。raw `local_type` 高位只用于一致性检查，不覆盖 XML 类型；冲突或未知结构安全回退，不猜测。合成测试覆盖直接卡片字段、`refermsg`、`recorditem → recordinfo/datalist/dataitem`、指定内嵌 XML 位置及有限嵌套。它不是所有微信版本/所有消息变体的兼容承诺。

live 线协议的可选投影字段（即使值为空，worker 序列化时也输出这些键）为：

```json
{
  "displayKind":"link|file|quote|forward|unknown",
  "replyTo":null,
  "quoteSummary":null,
  "quoteSender":null,
  "forwardItems":[{"sender":null,"timestamp":null,"content":"…","displayKind":"text|media|link|file|forward|unknown","forwardItems":[]}],
  "parseStatus":"ok|partial|unsupported|invalid|rejected|limit_exceeded",
  "truncated":false,
  "omittedCount":0
}
```

`canonical_text` 不作为独立 `canonicalText` wire 字段，而是放在兼容消息的 `content`（全局命中则在 `snippet` 中显示命中附近文本）。纯 parser 的本地结构还含 `title`, `description`, `url`, `file_name`, `file_size`, `issues`；这些本地字段和完整原 XML 不会作为独立展示字段发送，`issues` 也不在 wire 响应中。`replyTo` 只代表可信引用关系，不自动跨会话跳转。`omittedCount` 只计未展开的转发条目：可精确计数时为非负整数，无法知道嵌套/不透明条目数时为 `null`；它不是字符数。

- 链接/文件的标题、描述、URL、文件名和大小会组成安全的纯文本 canonical body；引用保留当前回复、被引发送者/摘要和通过验证的引用 ID；合并转发保留有限条目发送者、可用时间、正文/媒体占位及嵌套条目。合并转发仍是一条外层消息，不把内部发送者变成外层参与者、不把条目计作外层消息、不去重相同条目。搜索/定位仍指向外层物理行，转发内部正文可参与规范正文搜索。
- 解析只读取指定 XML 直接子节点与明确的内嵌 XML 位置；最多进行一次指定内嵌文本解码。拒绝 DTD/ENTITY 声明及外部实体，不执行脚本，不打开 URL/附件路径，不修改输入或写出原 XML。坏 XML、未知类型和部分支持会产生有界可见回退与状态，而非静默宣称完整。
- parser 状态为 `ok`, `partial`, `unsupported`, `invalid`, `rejected`, `limit_exceeded`。`partial` 表示字段/条目/输出不完整或需安全回退；`unsupported` 表示顶层结构/类型不支持；`invalid` 表示 XML/编码无效；`rejected` 表示禁止的 XML 声明；`limit_exceeded` 表示硬解析预算耗尽。`truncated` 标记预算/内容截断，不等同于 `omittedCount`。
- global A 的 `coverage` 将任何非 `ok` 状态或 `truncated=true` 计入 `partialMessages`，并对截断消息另计 `truncatedMessages`；即使零命中也报告。覆盖只说明解析器规范正文范围，不承诺任意 XML 字段、媒体内容或未知结构都已索引。
- 默认解析上限：单条 XML 2 MiB；XML 深度 32；根与嵌入 XML 合计 10,000 个节点；嵌套层数 2；合并转发最多展开 200 条；canonical 正文最多 64 KiB UTF-8；保留展示字符串另有 64 KiB 预算；引用摘要最多 2 KiB。live 消息原始 BLOB/解压内容也在解码/构树前受 2 MiB 上限。worker 的线协议投影序列化另限展示字符串合计 128 KiB、条目 200、嵌套深度 2；Native 再校验可选字段及预算。
- 预览、搜索和 Markdown/JSON/PDF 导出复用同一 canonical `content`，不在导出阶段重新解析 XML。Markdown/PDF 将复杂 FILE 卡片作为安全纯文本处理；普通单行文件附件保留旧附件显示语义。JSON v1 将规范 FILE 正文导出为 `type:"text"`，可读回 `content` 和可选 `reply_to`，但不保存/恢复完整卡片展示字段或转发树。
- parser 版本目前为 `appmsg-v1`，缓存投影版本为 `canonical-appmsg-v1`；版本变化会使 A/B3 的旧结果/会话失效。复杂卡片的规范正文扩大了 live 搜索可匹配范围；demo/offline 不由此自动获得 XML 卡片解析能力。

### 当前分支预算与错误语义

| 能力/项目 | 当前实现的限制 |
|---|---|
| A 搜索 | 每页默认 50、最多 100；准确命中总数；最多保留排序前 1,000 条；日期范围最多扫描 2,000,000 条；关键词最多 4 KiB UTF-8；snippet 正文窗口最多 240 字符，首尾可能各加一个 `…`。 |
| A / B3 会话 | 自创建起固定 10 分钟 TTL，访问不续期；A 最多 4 个搜索会话，B3 最多 4 个关键词页会话；二者共用进程内 32 MiB 查询缓存账本，在途扫描与已发布缓存竞争，可能跨 A/B3 淘汰旧会话。A 的完整 locator/projection 在扫描时即时计账；超预算返回 `SEARCH_CACHE_LIMIT` 并释放本次查询缓存资源。32 MiB 是查询缓存账本预算，不是整个 worker 的 RSS 硬上限。 |
| B2 游标会话 | 每页默认 50、线协议最多 100；空闲 TTL 10 分钟，每次成功读取后续期；最多 64 个页会话、每会话保留最多 512 个游标，较旧游标可能被淘汰。B2 没有 200,000 条 live 候选硬上限；受来源能力、schema、计数类型、软期限及响应大小约束。 |
| B3 关键词页 | 首次最多 200,000 个候选（匹配前计数）；完整命中快照进入上述共享 32 MiB 预算，不做 1,000 条 Top-K 截断。 |
| Native 消息窗口 | 每页请求 50 条；最多显示 20 页、累计展示字节最多 8 MiB；超过窗口时释放另一端。worker 单页协议最大 100 条。 |
| 时间 | A 首次搜索、B3 首次匹配及 live 上下文/B2 计数与读取阶段默认使用 20 秒协作式软期限；不是整个请求的硬超时保证，也不表示缓存续页重扫。Native 新工作流命令超时为 60 秒。 |
| 响应 | A、B3、上下文响应结果最多 1 MiB 减 4 KiB 信封预留；B2 结果最多 1 MiB 减 2 KiB；JSONL 完整行上限仍为 1 MiB。A/B3 按实际发出条数推进 offset/cursor；上下文先减邻居；单条结果/目标过大报 `RESPONSE_LIMIT_EXCEEDED`，不截正文。B2 不自动减条数，过大报 `PAGE_RESPONSE_TOO_LARGE`。 |
| 消息/XML | live 原始 BLOB、解压内容与 parser 单条 XML 均最多 2 MiB；A 搜索的正文、发送者、kind、transcript 字段单项最多 2 MiB。parser 深度 32、总节点 10,000、嵌入层 2。 |
| 投影/转发 | parser 最多 200 个转发条目，canonical 正文 64 KiB、保留展示字符串 64 KiB、引用摘要 2 KiB；线协议投影字符串合计 128 KiB。Native 另校验转发条数/深度和每页展示预算。 |

32 MiB 是当前 A/B3 共用查询缓存账本的预算，不覆盖离线导入模型、解密数据库副本及所有瞬时分配，因此不是进程 RSS 上限。A 在扫描阶段即时计入完整 locator/projection 的缓存费用；超预算返回 `SEARCH_CACHE_LIMIT` 并释放本次预留和暂存缓存，不发布不完整搜索。B3 命中快照也计入同一账本。B2 保留导航元数据，不另存一份完整消息集。

当前分支新增 API 的主要固定错误码：

- **A 全局搜索/上下文**：`INVALID_ARGUMENT`, `SOURCE_NOT_FOUND`, `SOURCE_SESSION_CHANGED`, `CAPABILITY_UNAVAILABLE`, `WECHAT_NOT_CONNECTED`, `CONVERSATION_NOT_FOUND`, `SEARCH_EXPIRED`, `SEARCH_SOURCE_CHANGED`, `SEARCH_SOURCE_UNREADABLE`, `SEARCH_UNSUPPORTED_SCHEMA`, `SEARCH_BUDGET_EXCEEDED`, `SEARCH_CACHE_LIMIT`, `MESSAGE_TOO_LARGE`, `MESSAGE_DECODE_FAILED`, `WECHAT_DB_UNSUPPORTED`, `RESPONSE_LIMIT_EXCEEDED`, `CONTEXT_TARGET_UNKNOWN`, `CONTEXT_TARGET_CHANGED`。
- **B2/B3 分页**：`INVALID_ARGUMENT`, `SOURCE_NOT_FOUND`, `SOURCE_SESSION_CHANGED`, `CAPABILITY_UNAVAILABLE`, `WECHAT_NOT_CONNECTED`, `CONVERSATION_NOT_FOUND`, `PAGE_UNSUPPORTED_SCHEMA`, `PAGE_SOURCE_UNVERIFIABLE`, `PAGE_SOURCE_CHANGED`, `PAGE_EXPIRED`, `PAGE_CRITERIA_CHANGED`, `PAGE_BUDGET_EXCEEDED`, `PAGE_SOURCE_UNAVAILABLE`, `PAGE_READ_FAILED`, `PAGE_RESPONSE_INVALID`, `PAGE_RESPONSE_TOO_LARGE`, `RESULT_LIMIT_EXCEEDED`, `SEARCH_BUDGET_EXCEEDED`, `SEARCH_CACHE_LIMIT`, `RESPONSE_LIMIT_EXCEEDED`, `MESSAGE_TOO_LARGE`, `MESSAGE_DECODE_FAILED`, `WECHAT_DB_UNSUPPORTED`, `INVALID_SEARCH_DOCUMENT`, `INVALID_SEARCH_SNAPSHOT`。
- B2/B3 的源切换错误要求重新取当前来源令牌；`PAGE_SOURCE_CHANGED` 要重新取首屏；`PAGE_EXPIRED` 要重新建会话；服务端有效条件变化的 `PAGE_CRITERIA_CHANGED` 要以当前条件重新查询。客户端夹带替换条件被拒绝不等于原会话已销毁。live B2/B3 的 SQLite 软期限映射为 `PAGE_BUDGET_EXCEEDED`；B3 外层扫描自身的期限（包括内存来源）为 `SEARCH_BUDGET_EXCEEDED`。A 的来源快照变化为 `SEARCH_SOURCE_CHANGED`。
- C parser 的坏 XML/不支持/硬 XML 预算通常通过 `parseStatus`、有界回退正文及 A 的 coverage 表示，不是独立 parser 异常。live 原始/解压超限及解码失败分别使用 `MESSAGE_TOO_LARGE` / `MESSAGE_DECODE_FAILED`；wire projection 结构无效使用 `PAGE_RESPONSE_INVALID`。客户端本地响应结构错误仍可能由 `PROTOCOL_ERROR` 报告。

worker 错误仍使用固定中文说明，不包含关键词、正文、账号路径、密钥或第三方异常文本；未知内部异常保持 `INTERNAL_ERROR`。本表是当前分支新增能力的码，不把它们追认为旧版 worker 的已发布支持。

### 合成 benchmark 与未验证边界

`scripts/Benchmark-MessageQueries.py` 仅在本次新建临时目录生成确定性合成 SQLite 分片，不发现账号、不启动/连接微信、不收集真实聊天、不访问模型或网络。默认 10,000 条/4 分片/4 会话，1 次进程内首次调用与 1 次重复调用；每场景用独立 Python 子进程。可配置 100,000 / 1,000,000 / 2,000,000 条、1/4/16 分片、索引、长/压缩正文及卡片比例。大配置是显式选择，不是默认完整性能矩阵。

```powershell
# stdout 为 JSON 报告；不保留合成数据库，不写产品数据目录
& '.\.venv\Scripts\python.exe' -I -B -X utf8 scripts/Benchmark-MessageQueries.py
# 轻量自检：断言分页等价、页内媒体、Top-K/准确 total、搜索续页零扫描等
& '.\.venv\Scripts\python.exe' -I -B -X utf8 scripts/Benchmark-MessageQueries.py --self-test
# 更大数据仅显式请求；报告路径必须不存在，避免覆盖已有数据
& '.\.venv\Scripts\python.exe' -I -B -X utf8 scripts/Benchmark-MessageQueries.py --rows 1000000 --shards 16 --conversations 16 --output .\benchmark-large.json
# --index none 对照无索引；--long-every / --compress-every 调整合成分布
```

报告包含配置/seed、源码 SHA-256、Python/SQLite/平台、数据集指纹、生成耗时/字节、实际调用路径、SQLite COUNT/schema/读取时间与查询计划、SQL progress 近似 VM 指令数、Python 产出行/转换/媒体 stub/parser 次数及耗时、首/深页和检索/续页延迟、tracemalloc 峰值/进程峰值 RSS、缓存估算/响应与正文输出字节。计时含监测开销，嵌套耗时不可相加；Python 产出行及 LIMIT **不是 SQLite 实际扫描行数**。首次调用只表示新进程/新连接，OS 磁盘缓存未清空，重复调用也不是跨请求 SQLite 页缓存承诺。

脚本测量可用的 production reader/parser、legacy/B1 source 路径、Top-K/cache helper，以及 B2/B3 生产 helper 与 worker serializer；直接调用 helper 并序列化结果，不经过 `handle_get_messages_page`、worker JSONL stdio 或 Native UI，不是实际端到端 benchmark。B2/B3 首次/续页协议和 Native 有界窗口另由独立合成 worker/Native 集成测试覆盖；不能拿 A 的缓存续页冒充 B3 验收。媒体只计数 stub 调用，无真实恢复/转写；解密/复制时间为 `null`（未测量），不能写成 0 ms。SQLite fixture 是明文、合成结构，不证明真实微信 schema 兼容。生产候选/扫描/软期限仍可能使大配置明确失败，而非静默放宽限制。仅同机同配置比较，不能提前宣传“百万条秒搜”。

当前分支有 worker JSON-lines 合成协议测试，覆盖 A 三来源搜索/上下文、B2 三来源游标、B3 三来源关键词快照及切源失效；Python reader/parser/cache、XML 安全和导出隔离由合成/单元测试覆盖；Native 有工作流、ViewModel 与真实子进程合成集成测试。合成与离线 fixture 的结果不证明真实微信账户/数据库版本兼容、真实账户解密或真实数据上的性能。匿名真实结构样本只能由用户自愿提供。

#### 已验证记录与尚未验证边界

- 后端 Python 全量测试：370/370 通过，0 跳过。共享查询缓存账本相关 4 项回归由失败修正为通过；完整 locator/projection 在 A 扫描阶段即时计账。默认 32 MiB 下，1000 条各约 60 KiB 的转发投影触发 `SEARCH_CACHE_LIMIT` 并释放本次缓存资源；常规 1005 命中仍保留准确 `total`、前 1000 条 Top-K、`truncated` 与 `coverage`。
- Demo stdio 合成协议新增覆盖：会话 TTL；切源/版本变化销毁旧页会话；失败导入保留原来源；双向分页并允许续页使用不同 `limit`；精确日期范围的 `total` 与平局顺序；legacy 兼容，以及 A/B3 基于同一来源 snapshot 并共用查询缓存账本。
- Native P2/P3 全量测试：236/236 通过。WinUI 隔离构建：0 警告、0 错误。
- 真实 UI 脚本对离线 fixture 完成验收：通过真实文件 picker 导入 fixture；B2 连续下一页跨越 20 页窗口、淘汰后仍可 previous；全局搜索定位精确 hit context 并返回同一组结果；应用退出码为 0 且 worker 已回收。某张 `PrintWindow` previous-footer 截图可能早于重绘；该项结论以 UIA 状态及运行报告为准，不以该截图单独判定。
- 2026-11-28 已完成 B2/B3 生产 helper + worker serializer 的合成 4-shard benchmark（不是实际 worker JSONL stdio/UI 端到端测量），命令为：

  ```powershell
  & '.\.venv\Scripts\python.exe' -I -B -X utf8 scripts\Benchmark-MessageQueries.py --rows 100000 --shards 4 --conversations 4 --seed 20261128 --index time --page-size 50 --deep-offset 500 --sparse-every 97 --long-every 23 --long-chars 2048 --compress-every 0 --card-percent 12 --media-percent 20 --repeats 1 --timeout 20 --no-tracemalloc
  ```

  - 环境：Windows 11 `10.0.26200`、Python `3.11.15`、SQLite `3.53.1`、`AMD64`；CPU 具体商品型号不可确认。100,000 条合成消息、4 会话、4 个数据库合计 `23,080,960 B`；以下是该单次配置的测量值，不是完整性能矩阵。
  - B2：`total=25,000`，每页 50 条。首屏 `23.68 ms`、响应 `10,678 B`；深页（offset 500）direct `11.40 ms`、独立 setup `131.17 ms`、响应 `14,779 B`；previous direct `9.10 ms`、独立 setup `149.68 ms`、响应 `14,713 B`。setup 与 direct 分列，不能把独立构造游标的耗时混作单次续页耗时。身份、total、SHA 与无缺口 oracle 均通过。
  - B2 三次 direct 调用各产出 204 个 keyset 候选，各读取正文/transform/decode 50 次、媒体 stub 10 次、parser 6 次；首屏、深页、反向页的 SQL/count/progress 计数分别为 `82/4/1704`、`58/0/125`、`58/0/5`。缓存字节为 `null`，因为 `PageSessionManager` 没有 cache-byte ledger，不代表零缓存。进程 working-set high-water 分别为 `34.5/35.9/36.1 MB`，包含 import/setup/fixture，不是请求增量内存。
  - B3 sparse：25,000 个候选、`total=207`；首次完整扫描 `1112.94 ms`，响应 `18,650 B`，查询缓存账本 `1,095,092 B`。body/doc/transform/decode 各 25,000 次，parser 3,000 次，媒体 stub 5,000 次（图片/语音各 2,500），transcript 0 次，SQL 28 次，progress 2,672 次。续页 direct `1.596 ms`、响应 `19,998 B`、缓存账本不变；row/doc/body/keyset/transform/decode/media/transcript/parser/SQL/db-acquisition 均为 0，snapshot validation 为 4。该独立续页场景的前置 setup 扫描另耗 `1099.75 ms`，不包含在 direct 续页耗时内。
  - 完整性与边界：8 个源码文件的 SHA-256 未变；生成的 4-shard fixture 测量前后未变，现已删除。EXPLAIN 显示 `idx_raw` range 加 `TEMP B-TREE` sort；Python 的 204 个 keyset 候选不是 SQLite 实际扫描行数，SQL progress 也不能替代物理扫描行数。本次未测 `handle_get_messages_page`、JSONL stdio/UI、SQLite 实际扫描行数、OS-cold cache、tracemalloc 或真实微信；dense、no-match 及其他规模仍无已确认测量。32 MiB 查询缓存账本不等于 RSS，不能由上述 helper 结果推断 UI 或真实微信已验证。
- 尚未验证：卡片展开的实际交互、人工中文 IME、完整布局/无障碍矩阵、真实微信连接及真实微信数据库版本兼容。真实源只处理当前 adapter 发现且能读取的内容；逐分片版本校验不构成跨分片原子快照保证。发布前应继续分别记录这些边界，不能用合成 fixture、单测或 UI 离线验收代替。

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
