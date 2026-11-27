# 真实微信 4.x 只读接入原生 worker 的技术评估

> 核验日期：2026-11-27  
> 依据：`reference/wechat-ai-memory`（上游 MIT 快照；快照 HEAD/tag 见 `docs/source-assessment.md:29-36`）与本项目现有源码、协议、测试、打包脚本。  
> 方法：只读静态审查；未读取真实微信数据库、密钥或聊天内容；未联网；未执行 git 操作。  
> 路径约定：下文路径均相对于项目根 `C:\微信记忆`。`已核验` 表示可由具体文件/行号支持；`需实测` 表示没有本机真实微信证据，不能由代码推断。

## 0. 结论摘要

- 上游确实有一套可独立于 Qt 的 Windows 微信 4.x 读取代码：账户发现、密钥捕获、数据库解密、会话/消息查询、图片和语音分别在 `sources/` 下；Qt 只在 UI 层调用它（`reference/wechat-ai-memory/src/wechat_context_exporter/ui/main_window.py:81-104`），命令行只接 JSON 来源（`.../cli.py:76-87`）。
- 但它不是可直接嵌入当前 worker 的库：`WeChat4LocalSource` 构造函数会自动捕获密钥并启动/重启微信（`.../sources/wechat4_source.py:45-58`），查询接口没有关键词和分页，时间使用本机本地时区，读取语音消息时还会把 `.silk` 永久写到用户目录（`.../sources/wechat4_voice.py:20-21,66-72`）。
- 第一阶段做“账户发现 + 会话列表 + 消息 + 日期/关键词搜索”是可行的，但应做一个**只读、纯文本、可清理的薄适配层**，复用 discovery/crypto/key-capture 和会话/消息 SQL，不直接使用完整 `WeChat4LocalSource`，也不引入 Qt、PDF、图片/语音转写。
- 最小运行时依赖是 `frida`、`pycryptodome`、`zstandard`；若原样使用上游密钥捕获，还必须有 `pywinauto`，因为 `_enter_remembered_account()` 在 Windows 上是无条件调用的（`.../wechat4_key_capture.py:196,278-284`）。`sqlite3` 是标准库。
- 当前 worker 协议只支持演示源（`src/PythonWorker/worker.py:207-211`；`docs/protocol.md:17,31`），当前客户端请求默认 30 秒超时（`src/NativeApp/Services/WorkerClient.cs:78`），而密钥捕获最长 300 秒（`.../wechat4_key_capture.py:104-110`），必须做协议与生命周期改造。
- 最大风险不在 SQL，而在密钥捕获和临时明文：Frida 注入会重启微信、可能触发杀毒/权限问题；解密后的数据库和图片在 `%TEMP%`，当前客户端超时/关闭时会直接杀 worker（`WorkerClient.cs:88-90,183`），不能保证临时目录被清理；完整数据源还会把语音 `.silk` 持久化。
- 上游自述曾在 Windows 微信 4.1.2.18 上验证过密钥、会话、消息和图片解密（`.../CHANGELOG.md:171`），但这是上游记录，不是本机复验；真实版本、管理员权限、打包后的 Frida、WAL 与并发读取都必须实机验证。

---

## 1. 文件/类/函数映射

### 1.1 账户发现

文件：`reference/wechat-ai-memory/src/wechat_context_exporter/sources/wechat4_discovery.py`

| 入口 | 签名与返回 | 异常/失败 | 关键行为 |
|---|---|---|---|
| `WeChat4Account` | `WeChat4Account(account_dir: Path)`；frozen/slots dataclass；`id -> str`、`db_dir -> Path`、`attachment_dir -> Path` | 无 | `id` 是账户目录名；`db_dir` 为 `<account>/db_storage`；`attachment_dir` 为 `<account>/msg/attach`（`:10-24`） |
| `discover_wechat4_accounts` | `() -> list[WeChat4Account]` | 不主动抛 `SourceError`；文件系统探测用 safe 助手吞掉 `OSError`（`:33-58`） | 只认含 `db_storage/session/session.db` 的目录；跳过 `all_users`；按账户目录 mtime 倒序（`:44-58`） |
| `select_wechat4_account` | `(path: str \| Path \| None = None) -> WeChat4Account` | `SourceError`：未找到、多个候选、不是账户目录（`:61-81`）；`Path.resolve()`/探测异常未统一包装 | 无路径时取发现列表第一个；接受账户目录、`db_storage` 或 `xwechat_files`；`xwechat_files` 必须唯一匹配 |
| `_accounts_under` | `(root: Path) -> list[WeChat4Account]` | 内部 `OSError` 返回空列表（`:84-95`） | 只扫描一层子目录 |
| `_candidate_xwechat_roots` | `() -> list[Path]` | `OSError` 容忍（`:98-129`） | `%USERPROFILE%\Documents\xwechat_files`、`%USERPROFILE%\xwechat_files`，以及每个盘符的 `X:\xwechat_files` 和 `X:\<一级目录>\xwechat_files`（`:98-120`）；按大小写不敏感去重（`:122-129`） |

要点：

- `SourceError` 是 `RuntimeError` 的子类（`.../sources/base.py:10`）；worker 侧应显式捕获并映射，而不能只靠 `except Exception` 变成 `INTERNAL_ERROR`。
- 账户发现不读取消息内容；但会遍历用户目录和多盘符一级目录，属于隐私敏感的文件枚举。
- `WeChat4Account` 没有 `account_id`、`storage_dir` 字段；上游 dashboard 的自动发现代码却读取这两个属性（`.../ui/dashboard_window.py:742-750`），异常被 `except Exception: pass` 吞掉。**不要把 dashboard 当接入依据**，主 UI 使用的是 `main_window.py:93-98`。
- `select_wechat4_account` 没有“让用户确认账户”的逻辑；多账户时由调用方传入具体目录，否则直接选 mtime 最新的账户。

### 1.2 密钥获取

文件：`reference/wechat-ai-memory/src/wechat_context_exporter/sources/wechat4_key_capture.py`

| 入口 | 签名与返回 | 异常/失败 | 关键行为 |
|---|---|---|---|
| `capture_account_key` | `(account_dir: Path, executable: Path \| None = None, timeout: float = 300.0, progress: ProgressCallback \| None = None, cancelled: Callable[[], bool] \| None = None) -> bytes`（`:104-110`） | `SourceError`：缺 `frida`、找不到 message DB、找不到/不能关闭微信、attach 失败、hook 失败、登录/取密钥超时、取消（`:111-114,125-129,153-155,193-206`） | 取账户下第一个数字后缀 `message_*.db` 的第一页（`:119-129`）；`find_wechat_executable`；`_stop_wechat`；启动 `Weixin.exe`；attach 到最大工作集 PID；等 `READY`；可选自动登录；用 `verify_account_key` 验证候选；返回 32 字节裸密钥（`:131-220`） |
| `find_wechat_executable` | `() -> Path`（`:223`） | `SourceError`：找不到（`:245`） | 先查运行中的 `Weixin.exe` 的 `ExecutablePath`，再找 `Program Files` 两个路径，再找每个盘符根下 `Weixin\Weixin.exe`（`:223-245`） |
| `_stop_wechat` | `() -> None`（`:248`） | `SourceError`：10 秒内未关闭（`:261`） | `taskkill /F /IM Weixin.exe`，按进程名强杀所有微信进程（`:248-261`） |
| `_main_wechat_pid` | `(minimum_working_set: int = 20 * 1024 * 1024) -> int \| None`（`:264`） | PowerShell 子进程失败未统一包装 | 按工作集降序取第一个 > 阈值的 `Weixin` PID |
| `_enter_remembered_account` | `(pid: int) -> bool`（`:278`） | Windows 上缺 `pywinauto` 时抛 `SourceError`（`:282-284`）；其余异常被吞 | 用 UIA 找登录窗口；找宽度 >= 140 的启用按钮，调用最大的一个；最多 20 秒；返回是否看到登录窗口（`:286-321`） |
| `_SHA512_HOOK` | JavaScript 字符串（`:17-101`） | hook 通过 `send("ERROR:...")` 报错 | 扫描 `Weixin.dll` 中的 SHA-512 常量；找代码引用和函数入口；`Interceptor.attach`；从 `this.context.rdx` 读 128 字节；校验 32..127 全为 `0x36`；前 32 字节 XOR `0x36` 得到密钥 hex；`send("KEY:<hex>")`（`:23-88`） |

辅助入口（不是 UI 主路径，但属于“密钥获取”能力）：

| 入口 | 签名与返回 | 异常/失败 | 说明 |
|---|---|---|---|
| `extract_database_keys` | `(db_dir: Path, relative_paths: Iterable[str] \| None = None, progress: ProgressCallback \| None = None) -> dict[str, bytes]`（`wechat4_crypto.py:431-435`） | `SourceError`：没有数据库目标、微信未运行/无法读内存（`:437-438,446`） | 扫描微信进程可读内存中的 `x'<64..192 hex>'`，按盐分组并用 HMAC 验证（`:443-487`）；**`WeChat4LocalSource` 和 UI 都没用它** |
| `collect_database_targets` | `(db_dir: Path, relative_paths: Iterable[str] \| None = None) -> list[DatabaseTarget]`（`:42`） | 文件 `open` 的 `OSError` 未统一包装 | 默认递归所有 `*.db`，跳过小于 4096 字节的文件；读第一页（`:51-62`） |
| `extract_image_key` | `(attachment_dir: Path, progress: ProgressCallback \| None = None, cancelled: Callable[[], bool] \| None = None) -> bytes \| None`（`wechat4_crypto.py:595-599`） | `SourceError`：微信未运行、无法读内存、取消（`:617,652,637`）；找不到样本/密钥返回 `None` | 通过进程内存和 `global_config` 配置推导 16 字节图片 AES key；需要微信正在运行（`:490-653`） |

进程内存辅助（`wechat4_crypto.py:276-401`）：

| 入口 | 签名与返回 | 异常/失败 | 用途 |
|---|---|---|---|
| `WeChatProcessMemory` | `(process_name: str = "Weixin.exe", pid: int \| None = None)`（`:284`） | 非 Windows、找不到进程、`OpenProcess` 失败时 `SourceError`（`:285-314`） | 以 `PROCESS_QUERY_INFORMATION\|PROCESS_VM_READ` 打开微信进程 |
| `.read` | `(address: int, size: int) -> bytes \| None`（`:321`） | 读取失败返回 `None` | 读远程内存 |
| `.regions` | `(writable_first: bool = False, max_size: int = 500 * 1024 * 1024) -> list[tuple[int, int, bool]]`（`:341`） | 无显式异常 | 枚举可读内存区；可把可写区排前 |
| `.iter_region_chunks` | `(base: int, size: int, chunk_size: int = 4MiB, overlap: int = 256)`（`:375`） | 读取失败时 `tail` 清空 | 分块读取，带重叠避免跨块漏匹配 |
| `.close` | `() -> None`（`:316`） | 无 | 关闭句柄 |

关键事实：

- `capture_account_key` 是“注入 + 重启 + 登录”的组合，不是静默读取。它先按镜像名杀掉所有 `Weixin.exe`，再启动新的微信进程（`:131-141`）；结束后**不会关闭**微信。
- `capture_account_key` 选择的是排序后的第一个 `message_*.db`，没有像 `collect_database_targets` 那样过滤小于 4096 字节的文件（`:119-129` 对比 `wechat4_crypto.py:56-57`）。如果第一个分片损坏/不足一页，`verify_account_key` 永远失败，最终表现为登录超时。**需实测真实目录是否总有非空分片。**
- 自动登录只在 Windows 上执行；`pywinauto` 缺失会直接失败，即使使用者愿意手动扫码。若做薄适配层，建议改成“可选自动点击”，不要无条件导入。
- 密钥只在 Python 内存中返回/持有；代码没有写密钥文件的路径。注意这不等于操作系统层面绝不落盘（崩溃转储/页面文件/杀毒软件取证不在代码控制内），也见第 5 节。

### 1.3 只读打开与数据库解密

文件：`reference/wechat-ai-memory/src/wechat_context_exporter/sources/wechat4_crypto.py`

| 入口 | 签名与返回 | 异常/失败 | 关键行为 |
|---|---|---|---|
| `DatabaseTarget` | `DatabaseTarget(relative_path: str, path: Path, salt: bytes, first_page: bytes)`（`:34-39`） | 无 | 用于密钥验证 |
| `derive_mac_key` | `(encryption_key: bytes, salt: bytes) -> bytes`（`:65`） | 无显式异常 | PBKDF2-HMAC-SHA512，2 次，32 字节（`:65-67`） |
| `derive_encryption_key` | `(raw_key: bytes, salt: bytes) -> bytes`（`:70`） | 裸密钥不是 32 字节时 `SourceError`（`:71-72`） | PBKDF2-HMAC-SHA512，256000 次，32 字节（`:73`） |
| `verify_database_key` | `(encryption_key: bytes, first_page: bytes) -> bool`（`:76`） | 长度不合时返回 `False` | HMAC-SHA512 验证第一页（`:77-85`） |
| `verify_account_key` | `(raw_key: bytes, first_page: bytes) -> bool`（`:88`） | 第一页不足 4096 返回 `False`；裸密钥长度错误会由派生函数抛 `SourceError` | 派生 + 验证第一页（`:88-92`） |
| `decrypt_page` | `(encryption_key: bytes, page: bytes, page_number: int) -> bytes`（`:95`） | 页长不是 4096 时 `SourceError`（`:96-97`） | AES-256-CBC；第一页前置 SQLite 头；末尾 80 字节置零（`:98-105`） |
| `decrypt_database` | `(source: Path, destination: Path, encryption_key: bytes) -> None`（`:108`） | `SourceError`：密钥不匹配、非页对齐、WAL 不支持；`sqlite3.Error` 可能透出 | 逐页解密到 `destination`；应用加密 WAL；最后以只读方式打开 `destination` 做 `sqlite_master` 检查（`:108-130`） |
| `_apply_encrypted_wal` | `(wal_path: Path, destination: Path, encryption_key: bytes) -> None`（`:133`） | WAL magic/页大小不支持时 `SourceError`（`:141-143`） | 读取 WAL 头、盐、帧；只应用到最后一个 commit 帧；按 commit size 截断（`:133-169`） |
| `DecryptedDatabaseCache` | `(db_dir: Path, raw_key: bytes \| None = None, keys: dict[str, bytes] \| None = None)`（`:172-178`） | 两者都无时 `SourceError`（`:179-180`） | 创建 `tempfile.TemporaryDirectory(prefix="wce-wechat4-")`（`:184`） |
| `.get` | `(relative_path: str) -> Path`（`:190`） | 无密钥 `SourceError`；快照反复变化 `SourceError`；文件/解密/SQLite 异常可能透出 | 解析 salt、派生每库 key、快照 DB+WAL、解密到临时目录、缓存（`:190-208`） |
| `._snapshot_database` | `(source: Path, normalized: str) -> Path`（`:210`） | 5 次仍变化或 `OSError` 后 `SourceError`（`:230`） | `copy2` 复制 DB 和 `-wal` 到临时目录；复制前后比较 size/mtime 签名（`:210-230`） |
| `.close` | `() -> None`（`:232`） | 清理异常未显式捕获 | 清缓存并删除 `TemporaryDirectory`（`:232-234`） |
| `_connect`（在 source 模块） | `(path: Path)` 上下文管理器（`wechat4_source.py:302-304`） | `sqlite3.Error` 透出 | `sqlite3.connect("file:<path>?mode=ro", uri=True)`；明文副本只读打开（`:302-309`） |

只读性证据链：

1. 原库只被 `_snapshot_database` 用 `shutil.copy2` 读取，以及 `.get` 读取前 16 字节 salt（`:199-200,217-220`）。
2. 写入目标始终是 `self._encrypted_root` 或 `self._decrypted_root` 下的临时文件（`:205-206,211`），不是 `self.db_dir`。
3. 查询走 `mode=ro` 的 SQLite URI（`wechat4_source.py:304`）。
4. 代码没有对原始 `.db` 执行 `open("wb")`、`DELETE`、`UPDATE` 或 `VACUUM`；但微信进程仍在运行，微信自己可能继续写库。因此“本工具不改原始数据库”可以由代码支持，“原始数据库在连接期间完全不变”不能由本工具单方面保证。

### 1.4 会话列表

文件：`reference/wechat-ai-memory/src/wechat_context_exporter/sources/wechat4_source.py`

| 入口 | 签名与返回 | 异常/失败 | 关键行为 |
|---|---|---|---|
| `WeChat4LocalSource.list_conversations` | `() -> list[Conversation]`（`:60-61`） | 调用时不新查询数据库；返回初始化时缓存列表的副本 | 返回 `Conversation(id, name, kind)` |
| `_load_contacts` | `() -> dict[str, str]`（`:133`） | `SourceError`：contact 表缺 `username`；数据库/快照异常可能透出 | 读 `contact\contact.db` 的 `contact` 表；显示名优先级 `remark > nick_name > alias > username`（`:147-151`） |
| `_load_conversations` | `() -> list[Conversation]`（`:156`） | `SourceError`：SessionTable 缺 `username`；数据库异常可能透出 | 读 `session\session.db` 的 `SessionTable`；可选 `last_timestamp`；按 `last_timestamp DESC`；排除没有消息表的 username（`:156-177`） |
| `_index_message_tables` | `(progress: ProgressCallback \| None) -> dict[str, list[tuple[str, str]]]`（`:179-182`） | 没有任何可用消息表时 `SourceError`（`:211-212`）；分片缺 `Name2Id` 时跳过 | 只扫描 `message_*.db`；表名 `Msg_` + `md5(username)`；把 username 映射到多个 (分片, 表)（`:184-210`） |

`Conversation` 模型只有三个字段：`id`、`name`、`kind`（`reference/wechat-ai-memory/src/wechat_context_exporter/models.py:22-32`），**没有消息数、最后消息预览、头像或未读数**。当前原生协议要求 `messageCount`（`docs/protocol.md:22`），接真实源时需要额外 `COUNT(*)` 或允许该字段为空/0；后者会改变契约。

### 1.5 消息查询

核心入口：

```text
WeChat4LocalSource.get_messages(
    conversation_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Message]
```

位置：`reference/wechat-ai-memory/src/wechat_context_exporter/sources/wechat4_source.py:63-123`

| 行为 | 细节 |
|---|---|
| 会话校验 | 找不到时 `SourceError("Unknown WeChat conversation: ...")`（`:69-71`） |
| 消息表 | 没有位置时 `SourceError("No message table ...")`（`:72-74`） |
| 分片遍历 | 对每个 (分片, 表) 都通过 `DecryptedDatabaseCache.get` 解密（`:76-78`） |
| 必需列 | `local_id`、`create_time`、`local_type`、`real_sender_id`、`message_content`；缺列 `SourceError`（`:79-83`） |
| 可选列 | `server_id`、`WCDB_CT_message_content`、`packed_info_data`；缺列时用 0/0/NULL 代替（`:84-102`） |
| 发送者 | `LEFT JOIN Name2Id n ON n.rowid = m.real_sender_id`（`:113-114`） |
| 日期条件 | `m.create_time >= int(start.timestamp())` 和 `m.create_time <= int(end.timestamp())`（`:103-111`） |
| 排序 | SQL 内 `ORDER BY m.create_time ASC, m.local_id ASC`（`:115`）；多分片合并后再按 `(timestamp, id)` 排序（`:123`） |
| 分页/关键词 | **没有 limit/offset，也没有关键词条件**；一次读取该会话所有匹配行 |
| 返回 | `Message(id, conversation_id, sender, timestamp, type, content, is_outgoing, audio_path, duration_ms, transcript)`；`reply_to` 未设置（`:288-299`） |
| 异常 | 上述两类 `SourceError`，加上 SQLite/快照/解密可能透出的异常 |

字段与类型映射（`_message_from_row`，`:219-299`）：

| 原始 `local_type` 低位 | 模型类型 | 内容/副作用 |
|---|---|---|
| `1` | `TEXT` | UTF-8 解码；空内容变成 `SYSTEM` `[空消息]`（`:276-281`） |
| `3` | `IMAGE` 或 `SYSTEM` | `packed_info_data`/内容里的 32 位 hex 交给 `DecryptedImageCache.resolve`；成功时内容为临时图片路径，失败时 `[图片]`（`:246-254`） |
| `34` | `VOICE` | 读 XML `voicelength` 得时长；查 media 分片的 `VoiceInfo`；成功时 `audio_path` 指向持久 `.silk`；内容为转写或语音占位（`:258-272`） |
| `49` | `FILE` | 解析 XML `title`；子类型 `raw_type >> 32` 映射链接/音乐/文件/小程序等；内容只是标签/标题，**不是文件路径**（`:255-257,377-399`） |
| `42/43/47/48/50/10000/10002` | `SYSTEM` | 固定中文标签（`:25-33,273-275`） |
| 其他非 `1` | `SYSTEM` | `[消息类型 N]`（`:276-278`） |

发送者与 ID：

- 自己的 wxid 由账户目录名去掉末尾 `_<4 位 hex>` 得到（`:312-314`）；`real_sender_id` 等于自己时 sender 为 `"我"`；群聊优先联系人显示名，否则用 `sender_wxid` 或 `"群成员"`；单聊用会话显示名（`:230-239`）。
- 时间戳：`int(create_time)`；大于 10,000,000,000 时按毫秒处理并除以 1000；最后 `datetime.fromtimestamp(timestamp)`，即**本机本地时区的 naive datetime**（`:283-285,292`）。
- 消息 ID：`server_id` 非 0 时用其十进制字符串；否则 `conversation.id:数据库文件名:local_id`（`:286-287`）。
- 同一时间戳的排序使用字符串 `id` 作为次级键（`:123`），不保证与原始 `local_id` 顺序一致；若 worker 需要稳定顺序，应自己保留 `(timestamp, local_id)`。

内容解码：

- `_decode_content` 在压缩标志为 4 或内容以 zstd magic 开头时解压；`ZstdError` 直接返回空字符串，不报错；UTF-8 错误用替换字符（`:331-346`）。
- `_strip_group_prefix` 对所有文本无条件下执行：匹配 `^([^\s:]{1,80}):(?:\n| )` 时剥离前缀并作为 sender 候选（`:229,349-353`）。这会误伤正常以“英文单词 + 冒号 + 空格”开头的消息；接原生 worker 时应考虑只对群聊或仅在发送者未知时使用。
- `_type_49_content` 对子类型 57（引用）单独处理；XML 解析失败时退化成正则（`:377-413`）。

### 1.6 图片与语音

文件：`reference/wechat-ai-memory/src/wechat_context_exporter/sources/wechat4_media.py`

| 入口 | 签名与返回 | 异常/失败 | 行为 |
|---|---|---|---|
| `derive_image_xor_key` | `(attachment_dir: Path, sample_limit: int = 32) -> int \| None`（`:20`） | 没有投票结果返回 `None` | 取最新 `*_t.dat` 末尾两字节，按 XOR 0xFF/0xD9 投票（`:20-38`） |
| `decrypt_v2_image` | `(source: Path, aes_key: bytes, xor_key: int) -> bytes`（`:41`） | `SourceError`：key 非 16 字节、格式/布局/填充错误 | AES-ECB 头 + 中间明文 + XOR 尾部（`:41-65`） |
| `find_v2_image_sample` | `(attachment_dir: Path) -> Path \| None`（`wechat4_crypto.py:490-500`） | 无异常时返回 `None` | 取最新 200 个 `*_t.dat` 中第一个 V2 magic 样本，供图片 key 扫描 |
| `derive_image_key` | `(cfg_dword: int, wxid: str) -> bytes`（`wechat4_crypto.py:503-505`） | 无显式异常 | `md5(f"{cfg_dword}{wxid}")` 前 16 个 hex 字符作为 ASCII key；具体配置偏移只在私有函数中使用 |
| `decode_wxgf_image` | `(data: bytes, ffmpeg_path: str \| Path \| None = None) -> bytes`（`:82`） | `SourceError`：magic/分区/FFmpeg 失败 | 调 FFmpeg 解 HEVC 分区（`:82-126`） |
| `DecryptedImageCache` | `(attachment_dir: Path, aes_key: bytes \| None)`（`:169-170`） | 构造期可能因临时目录失败抛 `OSError` | 创建 `tempfile.TemporaryDirectory(prefix="wce-images-")`（`:174`） |
| `.resolve` | `(identifiers: list[str]) -> Path \| None`（`:179`） | 单张图片的解密/格式异常被吞并返回 `None`；索引 `rglob` 或写临时文件的 `OSError` 可能透出 | 无 key 或无 xor key 直接 `None`；懒索引所有 `*.dat`；解密后写临时目录（`:179-222`） |
| `.close` | `() -> None`（`:192`） | 清理异常未显式捕获 | 清缓存并删除临时目录（`:192-195`） |

文件：`reference/wechat-ai-memory/src/wechat_context_exporter/sources/wechat4_voice.py`

| 入口 | 签名与返回 | 异常/失败 | 行为 |
|---|---|---|---|
| `WeChatVoiceCache` | `(databases: DecryptedDatabaseCache, account_id: str, root: Path \| None = None)`（`:12-18`） | 无 | 根目录默认 `app_data_dir()/voice-audio/<sha256(account_id)[:16]>`（`:20-21`） |
| `.resolve` | `(message_database: str, conversation_id: str, local_id: int, server_id: int) -> Path \| None`（`:23-29`） | 解密/DB 异常多数吞掉返回 `None`；写文件 `OSError` 可能透出 | 把 `message_N.db` 映射到 `media_N.db`；查 `Name2Id` 和 `VoiceInfo`；把 `voice_data` 写成 `<root>/<sha256(data)>.silk`（`:30-72`） |

文件：`reference/wechat-ai-memory/src/wechat_context_exporter/voice.py`

| 入口 | 签名与返回 | 异常/失败 | 行为 |
|---|---|---|---|
| `VoiceTranscriptCache` | `(root: Path \| None = None)`；`load(audio_path, model=None) -> str \| None`；`save(audio_path, transcript, model) -> None`（`:41-67`） | 读 JSON 的错误被吞；计算音频指纹/写 JSON 的 `OSError` 可能透出 | 默认根 `%LOCALAPPDATA%/WeChatAIMemory/voice-transcripts`（`:22-24,43`） |
| `decode_silk_to_wav` | `(source: Path, destination: Path, sample_rate: int = 24000) -> Path`（`:70`） | 缺 `pysilk` 抛 `RuntimeError`（`:72-74`） | 惰性导入 `pysilk`，写 WAV（`:70-87`） |
| `VoiceTranscriber` | `transcribe_messages(messages, progress=None, cancelled=None)`（`:90-158`） | 缺 `faster_whisper` 等可能抛异常 | 惰性导入 `faster_whisper`；模型缓存到 app data；保存转写（`:90-158`） |
| `voice_placeholder` | `(duration_ms: int \| None, available: bool = True) -> str`（`:27`） | 无 | `[语音消息 · N 秒 · 待转写/音频暂不可读]`（`:27-30`） |

关键后果：

- `WeChat4LocalSource.get_messages` 对每条 type 34 都会调用 `WeChatVoiceCache.resolve`（`wechat4_source.py:258-266`），即使调用方根本不打算转写。只要 media 库里有 `VoiceInfo`，就会在 `%LOCALAPPDATA%\WeChatAIMemory\voice-audio` 下产生持久 `.silk` 文件（`wechat4_voice.py:66-72`）。
- 图片默认不产生临时文件：`WeChat4LocalSource` 的 `image_key` 默认是 `None`，`DecryptedImageCache.resolve` 在无 key 时直接返回 `None`（`wechat4_source.py:41-52`；`wechat4_media.py:179-181`），消息变成 `[图片]` 占位。只有显式调用 `set_image_key`（`wechat4_source.py:129-131`）或构造时传入 key 才会解密图片。
- `.silk` 和语音转写 JSON 都不在临时目录，`close()` 不会删除；这是完整数据源的隐私副作用，不是 bug。

### 1.7 调用方与现有边界

上游 JSON 来源（对照，不是真实微信）：

- `JsonChatSource(path: str | Path)`：构造时读取 JSON 并校验 `version == 1`；`list_conversations() -> list[Conversation]`；`get_messages(conversation_id, start=None, end=None) -> list[Message]`（`reference/wechat-ai-memory/src/wechat_context_exporter/sources/json_source.py:12-126`）。
- 失败统一抛 `SourceError`；时间戳解析为 ISO 8601，带时区时转成本机本地 naive（`:129-137`）；日期过滤也是闭区间（`:120-126`）。
- 它不是真实微信源；上游 CLI 只构造它（`.../cli.py:76-87`）。`ChatSource` 协议只规定 `list_conversations`/`get_messages` 两个方法（`.../sources/base.py:70-78`），比当前 worker 协议简单得多。

上游调用方：

| 位置 | 行为 | 对集成的意义 |
|---|---|---|
| `reference/wechat-ai-memory/src/wechat_context_exporter/ui/main_window.py:81-104` | `SourceLoadWorker` 在 `mode == "wechat"` 时构造 `WeChat4LocalSource(path or None, progress=..., cancelled=...)` | UI 是完整数据源的实际入口；构造函数承担密钥捕获 |
| `.../ui/main_window.py:107-122` | `MessageLoadWorker` 调 `get_messages(conversation_id)`，不传日期 | UI 先加载全部，再在前端过滤 |
| `.../ui/main_window.py:125-147` | `ImageKeyWorker` 调 `extract_image_key`，成功后 `set_image_key` | 图片能力是额外、可选、需要运行中的微信 |
| `.../ui/main_window.py:150-169` | `VoiceTranscriptionWorker` 调 `VoiceTranscriber` | 语音转写与消息读取分离，但读取阶段已经写了 `.silk` |
| `.../ui/main_window.py:832-838` | 连接前提示“连接时会重启微信一次……原始数据库不会被修改” | 需要用户确认的产品文案；但“原始数据库不变”不能覆盖微信自身写库 |
| `.../ui/dashboard_window.py:742-750` | 自动发现后读取不存在的 `acct.account_id`/`acct.storage_dir`，异常被吞 | 上游内部不一致；不要作为 API 参考 |
| `.../service.py:227-242` | `filter_messages(messages, query)` 在内存里对 sender/content/type 做 `casefold` 子串匹配 | 关键词搜索是后置过滤，不是 SQL 搜索 |
| `.../service.py:245-248` | `select_messages` = `source.get_messages` + `filter_messages` | 没有分页参数，没有数据库级查询 |
| `.../cli.py:76-87` | CLI 只构造 `JsonChatSource` | 不能用 CLI 直接验证真实微信源 |

本项目现有边界：

| 位置 | 事实 | 影响 |
|---|---|---|
| `src/PythonWorker/worker.py:1-7` | 明确标准库、演示 JSON、不读微信 DB/Qt/网络 | 当前不是真实源 |
| `src/PythonWorker/worker.py:207-211` | `_checked_source` 只接受 `source == "demo"`，否则 `SOURCE_NOT_FOUND` | 新增真实源必须改协议/命令 |
| `src/PythonWorker/worker.py:238-271` | `hello`、`list_conversations`、`get_messages` 的演示实现；`messageCount` 来自演示内存 | 真实源不能复用这些内存模型 |
| `src/PythonWorker/worker.py:388-438` | 单 pending 请求；意外异常 traceback 写 stderr，返回 `INTERNAL_ERROR` | 真实 `SourceError` 需要映射为专用错误码 |
| `docs/protocol.md:17,22-31` | 只允许 `source:"demo"`；日期 UTC+08、结束日包含；无 AI/语音/数据库解密能力 | 需要新增来源、搜索、分页和真实时间语义 |
| `src/NativeApp/Services/WorkerLocator.cs:7-31` | 无论内置 worker 还是源码 worker，都以 `--demo <demo.json>` 启动 | 真实模式不能依赖路径参数；账户发现应在 worker 内部 |
| `src/NativeApp/Services/WorkerClient.cs:62-104` | 单请求串行；默认 30 秒；超时杀进程树并失效 | 密钥捕获/大库解密会超时 |
| `src/NativeApp/Services/WorkerClient.cs:137-145` | stderr 只计数，不转发不记录 | 对隐私有利；但真实 worker 的诊断也不能写 stdout |
| `src/NativeApp/Services/WorkerClient.cs:173-196` | 有 pending 时直接杀进程；否则发 2 秒 shutdown | 中途关闭不会调用 Python `close()`，临时明文/语音文件可能残留 |
| `src/NativeApp/Services/AppLog.cs:9-24` | 只接受固定事件名和已知错误码，不记录路径/异常消息/正文 | 新代码必须继续遵守 |

---

## 2. 依赖与版本

### 2.1 上游声明与用途

`reference/wechat-ai-memory/pyproject.toml:10` 要求 `requires-python = ">=3.11"`。

| 依赖 | 上游声明（`pyproject.toml:13-23`） | 纯文本接入是否需要 | 实际用途/触发点 |
|---|---|---|---|
| `frida` | `>=17,<18` | **需要** | `capture_account_key` 惰性导入（`wechat4_key_capture.py:111-114`），注入微信进程读派生密钥 |
| `pycryptodome` | `>=3.21,<4` | **需要** | `Crypto.Cipher.AES`，数据库页和 V2 图片（`wechat4_crypto.py:19`；`wechat4_media.py:11`） |
| `zstandard` | `>=0.23,<1` | **需要** | 解压 `WCDB_CT_message_content == 4` 或 zstd magic 的消息（`wechat4_source.py:13,331-346`） |
| `pywinauto` | `>=0.6.9,<1` | 原样使用上游捕获流程时**需要**；可改造掉 | `_enter_remembered_account` 在 Windows 上无条件调用（`wechat4_key_capture.py:196,278-284`）；缺失时直接失败 |
| `imageio-ffmpeg` | `>=0.6,<1` | 不需要 | 仅 WXGF/HEVC 图片解码，`decode_wxgf_image` 惰性使用（`wechat4_media.py:90-92,154-166`） |
| `Pillow` | `>=10.4,<12` | 不需要 | PDF/渲染/图片页；完整 `service.py` 的依赖，不是消息 SQL 依赖 |
| `reportlab` | `>=4.2,<5` | 不需要 | PDF 导出；`service.py`/渲染 |
| `faster-whisper` | `>=1.2,<2` | 不需要 | 语音转写；`voice.py:90-158` 惰性导入。首次 `WhisperModel` 会把模型下载到 `%LOCALAPPDATA%/WeChatAIMemory/models`（`voice.py:123-136`），属于**运行期联网**；纯文本阶段不要引入 |
| `silk-python` | `>=0.2.8,<0.3` | 不需要 | `.silk` 转 WAV；`voice.py:70-74` 惰性导入 |
| `PySide6` | 可选 `gui`：`>=6.7,<6.12`（`pyproject.toml:28`） | 不需要 | 上游 Qt UI |
| `imageio` | 可选 `media`：`>=2.34,<3`（`:29`） | 不需要 | 媒体/渲染辅助 |
| `pypdf` | 可选 `dev`：`>=5,<7`（`:32`） | 不需要 | 上游测试读 PDF |
| `pyinstaller` | 可选 `build`：`>=6.11,<7`（`:30`） | 构建期需要 | 打包，不是运行时库 |

补充：

- `sqlite3` 是 Python 标准库；不需要额外安装，但 PyInstaller 必须带上 `_sqlite3` 扩展和 SQLite DLL。
- 上游 `wechat_context_exporter/__init__.py:3-4` 会导入 `service`，进而可能牵出 Pillow/reportlab。即使只 import `wechat_context_exporter.sources.wechat4_source`，Python 也会先执行包 `__init__`；因此不能直接复制上游包结构而不改 `__init__`。本项目现有的最小 `vendor/wechat_context_exporter/__init__.py:1-6` 已经刻意避开这一点。
- `wechat4_source.py` 自身导入 `wechat4_media`、`wechat4_voice` 和 `voice.py`（`:15-22`），但这些模块对 `faster_whisper`/`pysilk`/`imageio_ffmpeg` 都是惰性导入；**仅导入不会立刻需要重型依赖**。然而构造 `WeChat4LocalSource` 会创建图片临时目录和语音缓存，见 1.6。

### 2.2 Python 3.11 兼容性

- 上游代码使用 `X | None`、`dataclass(slots=True)`、`str.removeprefix()` 等；`requires-python >=3.11` 与这些语法一致（`pyproject.toml:10`；`wechat4_discovery.py:10`；`wechat4_source.py:321`）。
- 本项目 `.venv` 核验时为 CPython **3.11.15**；`frida 17.28.0`、`pycryptodome 3.24.1`、`zstandard 0.25.0`、`pywinauto 0.6.9` 等均可导入。该结果只说明本机开发环境，不是干净机器/CI 证明。
- `scripts/Prepare-Python.ps1:11-23` 允许 3.11 或 3.13；当前发布产物没有在 3.13 上验证。Frida 17 的 wheel/扩展与 3.13 的组合、PyInstaller 在 3.13 的组合都**需实测**。
- 代码没有发现 3.12/3.13 专属语法或 3.11 不支持的 API；但真实微信 hook 与操作系统权限无关 Python 版本，不能用版本兼容性推断 hook 兼容性。

### 2.3 本机核验到的版本与打包线索

以下为核验时 `.venv` 中实际存在的版本/文件，不能当作项目声明或干净环境保证：

| 项目 | 核验值 | 说明 |
|---|---|---|
| Python | 3.11.15 | 与 `requires-python` 一致 |
| frida | 17.28.0 | `_frida.pyd` 为 169,408,512 字节（约 161.6 MiB）；单次 `import frida` 约 0.18 秒 |
| pycryptodome | 3.24.1 | PyInstaller hooks-contrib 中有 `hook-Crypto.py` |
| zstandard | 0.25.0 | 含 `zstandard/_cffi.cp311-win_amd64.pyd` 等扩展 |
| pywinauto | 0.6.9 | 依赖 comtypes/pywin32；hooks-contrib 有 comtypes hook |
| PyInstaller | 6.22.3 | 当前发布脚本使用 |
| pyinstaller-hooks-contrib | 2026.10 | 未发现 frida 专用 hook 文件；有 Crypto/comtypes 等 hook |

PyInstaller 风险（均需构建后实测，不能只靠静态推断）：

1. **体积/启动**：frida 的原生 `_frida.pyd` 约 161.6 MiB；当前 `scripts/Publish.ps1:38-46` 使用 `--onefile`，每次启动解压到临时目录，可能带来明显启动延迟、杀毒扫描和临时空间占用。建议真实源改用 `--onedir`，或至少实测 onefile 冷启动/AV 行为。
2. **收集方式**：`Publish.ps1` 只传 `--paths src/PythonWorker/vendor`，没有 hidden imports/datas（`:38-46`）。`capture_account_key` 里的 `import frida` 是函数内导入，PyInstaller 通常能做字节码分析，但 frida 的原生扩展/运行期资源能否被完整收集没有项目内证据。上游打包 spec 也没有 frida 专项配置（`reference/.../packaging/windows/wechat_ai_memory.spec:8-27` 使用 `hiddenimports=[]`，datas 只收 `imageio_ffmpeg`/`faster_whisper`）。
3. **依赖声明缺失**：项目没有 `src/PythonWorker/requirements.txt`；`scripts/Prepare-Python.ps1:26-27` 只在文件存在时安装，`Publish.ps1:22-24` 只安装 `requirements-build.txt`（当前只有 `pyinstaller==6.22.3`，见 `src/PythonWorker/requirements-build.txt:1-2`）。真实源必须新增运行时 requirements，并确保构建机 `.venv` 中已安装。
4. **frida 许可证**：核验时 frida wheel 的 metadata 标注 wxWindows Library Licence 3.1；如果分发带 frida 的 worker，`THIRD_PARTY_NOTICES.md`/`LICENSES/` 需要复核，当前项目的 notices 主要覆盖已 vendored 的模型/导出文件，不覆盖 frida。
5. **comtypes/生成代码**：pywinauto 通过 comtypes 可能在运行期生成模块/写缓存；冻结后的行为需实测。若改为手动登录/跳过自动点击，可去掉 pywinauto 及这部分风险。
6. **stdout 污染**：Frida/原生库若向 stdout 打日志，当前 `WorkerClient` 会把任何无 pending 的 stdout 行判为 `PROTOCOL_ERROR`（`WorkerClient.cs:110-116`）。真实 worker 必须在导入/调用前把 fd 1 重定向或隔离（当前 `worker.py:449` 只把 Python `sys.stdout` 指向 stderr，不能拦住原生 fd 写）。

---

## 3. 数据路径与只读性

### 3.1 如何定位微信 4.x 账户

1. `discover_wechat4_accounts()` 生成候选根（`wechat4_discovery.py:98-129`）：
   - `%USERPROFILE%\Documents\xwechat_files`
   - `%USERPROFILE%\xwechat_files`
   - 每个存在盘符的 `X:\xwechat_files`
   - 每个盘符一级子目录下的 `X:\<dir>\xwechat_files`
2. 根目录下每个子目录若满足 `<child>/db_storage/session/session.db` 存在，且名字不是 `all_users`，就视为账户（`:44-51`）。
3. 账户对象路径布局：
   - 账户根：`...\xwechat_files\<account_dir>`
   - 数据库根：`<account>/db_storage`
   - 会话库：`<account>/db_storage/session/session.db`
   - 联系人：`<account>/db_storage/contact/contact.db`
   - 消息分片：`<account>/db_storage/message/message_*.db`
   - 媒体分片：`<account>/db_storage/message/media_*.db`（由 `message_N.db` 推导，`wechat4_voice.py:75-81`）
   - 附件：`<account>/msg/attach`
4. 默认选择 mtime 最新账户（`:54-58,78-81`）；多账户时 UI 可以传账户目录或 `xwechat_files` 根，但必须唯一（`:61-72`）。

这段扫描不会打开消息表；但会枚举用户目录和多盘符目录，属于敏感行为。原生 worker 不应把扫描范围扩大到任意路径，应保持与上游一致的根集合，并允许用户显式选择账户。

### 3.2 临时副本、解密副本与持久文件

| 数据 | 位置 | 生命周期 | 是否敏感 |
|---|---|---|---|
| 加密数据库快照 | `%TEMP%\wce-wechat4-*\encrypted\...` | `DecryptedDatabaseCache` 生存期；`close()` 删除 | 仍是密文，但含账户数据 |
| 解密数据库 | `%TEMP%\wce-wechat4-*\decrypted\...` | 同上；`sqlite3` 以 `mode=ro` 打开 | **明文聊天数据库** |
| 解密图片 | `%TEMP%\wce-images-*\...` | `DecryptedImageCache.close()` 删除；只有显式图片 key 时产生 | 明文图片 |
| 语音 `.silk` | `%LOCALAPPDATA%\WeChatAIMemory\voice-audio\<account_hash>\<sha256(data)>.silk` | **持久**，`close()` 不删除（`wechat4_voice.py:20-21,66-72`） | 原始语音 |
| 语音转写 JSON | `%LOCALAPPDATA%\WeChatAIMemory\voice-transcripts\<sha256(audio)>.json` | 持久（`voice.py:41-67`） | 转写正文 |
| Whisper 模型 | `%LOCALAPPDATA%\WeChatAIMemory\models` | 持久（`voice.py:90-158`） | 非聊天数据，但占空间 |

`tempfile.TemporaryDirectory(prefix="wce-wechat4-")` 和 `prefix="wce-images-"` 使用系统临时目录；在 Windows 通常是 `%TEMP%`。`DecryptedDatabaseCache.close()` 会清空缓存并删除目录（`wechat4_crypto.py:232-234`），`WeChat4LocalSource.close()` 关闭图片和数据库缓存（`wechat4_source.py:125-127`）。

**关键缺口**：当前 C# 客户端在请求超时或有 pending 请求关闭窗口时会直接杀 worker（`WorkerClient.cs:88-90,183`）。Python 的 `TemporaryDirectory` 清理依赖 `close()`/终结器；强制终止不保证执行。于是 `%TEMP%` 下可能留下明文数据库/图片。这不是上游代码的错误，但一旦接入就必须在 worker 设计里解决（见 6.4、8）。

### 3.3 原始数据库是否被修改

可以确认的部分：

- `_snapshot_database` 只 `shutil.copy2` 原库和 `-wal` 到临时目录；`decrypt_database` 的写目标是 `destination`（`wechat4_crypto.py:108-130,210-230`）。
- SQLite 查询只打开解密副本，URI 带 `mode=ro`（`wechat4_source.py:304`）。
- 图片/语音读取原库时也用 `mode=ro`（`wechat4_voice.py:38`；图片只读 `*.dat`，`wechat4_media.py:44`）。
- 代码中未发现对原始 `.db` 的写打开、删除、更新或 VACUUM。

不能确认的部分：

- 连接流程会强杀并重启微信（`wechat4_key_capture.py:248-261,141`）；微信重新登录后会继续写自己的数据库、WAL 和索引。上游 UI 文案“微信原始数据库不会被修改”（`main_window.py:836`）只是产品承诺，不能阻止微信自身写库。
- `shutil.copy2` 会更新源文件的访问时间（取决于文件系统/挂载选项），不是内容修改，但也不是“零文件系统副作用”。
- 如果真实 worker 在微信写入时复制，`_snapshot_database` 的签名重试可能失败（`:214-230`），或者拿到不一致的快照；需要在微信静默时复制，或设计更可靠的快照/重试策略。

### 3.4 密钥是否落盘

代码层面：

- `capture_account_key` 返回 `bytes`，没有写文件（`wechat4_key_capture.py:218-220`）。
- `WeChat4LocalSource` 把密钥存在 `self.raw_key`（`wechat4_source.py:46-50`），`DecryptedDatabaseCache` 存在 `self.raw_key`（`wechat4_crypto.py:181-183`）。
- `extract_database_keys`/`extract_image_key` 也只返回内存 bytes。
- 没有发现 key 被写进日志、协议、命令行参数或配置文件。

但“密钥不落盘”不能绝对化：

- 进程内存可能被页面文件、崩溃转储、EDR/杀毒软件采集。
- 如果上层把异常 `str(exc)` 写入日志或 UI，虽然当前异常文本没有密钥，但不能保证未来 Frida 错误描述、调试输出不会带出候选密钥。
- 不要为了“下次免登录”把 raw key 保存到配置；上游代码没有这样做，接入时也不应添加。

### 3.5 需要微信处于什么状态

| 阶段 | 需要的状态 | 证据 |
|---|---|---|
| 账户发现 | 微信可以关闭 | 只读文件系统（`wechat4_discovery.py:33-58`） |
| DB 密钥捕获 | 先关闭所有 `Weixin.exe`，再由工具启动；需要登录/扫码；attach 到启动中的进程；最多 300 秒 | `wechat4_key_capture.py:131-207,248-261` |
| 自动登录 | Windows + `pywinauto`；需要出现登录窗口/按钮；最多 20 秒 | `:278-321` |
| 数据库查询 | 微信可运行也可关闭；运行中可能持续写库，导致快照重试失败 | `wechat4_crypto.py:214-230` |
| 图片 key/图片解密 | 微信必须正在运行；需要读取进程内存；可能要求管理员 | `wechat4_crypto.py:595-653` |
| 语音解码/转写 | 只需已解密的 media 库；转写需要 `faster-whisper`/`pysilk` | `wechat4_voice.py:23-72`；`voice.py:70-158` |

结论：真实接入最轻的“文本”路径也至少需要一次“关闭微信 -> 启动 -> 登录 -> 捕获 key”。这不是纯离线读取。是否可以在捕获后关闭微信再读取，取决于快照/文件锁实测；上游没有这样做。

---

## 4. 查询能力

### 4.1 能力矩阵

| 需求 | 上游能力 | 证据/限制 |
|---|---|---|
| 按会话查消息 | 支持 | `get_messages(conversation_id, ...)`；会话必须同时在 `SessionTable` 和消息表索引中（`wechat4_source.py:69-74,156-177`） |
| 按日期过滤 | 支持，但语义是**闭区间** | SQL `create_time >=` 和 `<=`（`:105-110`）；没有日期归一化，调用方传 `datetime` |
| 按关键词搜索 | 不支持于源层 | 只有 `service.filter_messages` 的内存 `casefold` 子串（`service.py:227-242`）；无 SQL 索引、无跨会话搜索 |
| 分页 | 不支持 | `get_messages` 无 limit/offset，一次返回全部匹配消息（`:112-123`）；UI 的“分页”是原生 worker 内存切片 |
| 会话消息数 | 不支持 | `Conversation` 只有 id/name/kind（`models.py:22-32`）；当前协议要求 `messageCount`（`protocol.md:22`） |
| 最后消息/未读/置顶 | 不支持 | 会话只按 `last_timestamp` 排序（`wechat4_source.py:162-166`） |
| 全局搜索 | 不支持 | 必须先选会话，再查该会话消息表 |
| 稳定排序 | 部分支持 | SQL 按 `(create_time, local_id)`，合并后再按 `(timestamp, id)` 字符串排序（`:115,123`）；可能改变相同时间戳顺序 |
| 附件路径 | 图片成功时给临时路径；文件类型不给路径 | 图片 `content` 是 `wce-images-*` 临时路径（`:248-251`）；type 49 `content` 是标签/标题（`:255-257,377-399`） |

### 4.2 返回字段

`Message` 的完整字段（`models.py:35-47`）：

- `id: str`
- `conversation_id: str`
- `sender: str`
- `timestamp: datetime`（微信源为 naive local datetime）
- `type: MessageType`（text/image/file/voice/system）
- `content: str`
- `is_outgoing: bool`
- `reply_to: str | None`（微信源未设置）
- `audio_path: Path | None`（仅语音且找到 `.silk`）
- `duration_ms: int | None`（语音 XML 的 `voicelength`）
- `transcript: str | None`（可能来自持久转写缓存）

`Conversation` 只有 `id`、`name`、`kind`。没有 `message_count`。

### 4.3 时间语义（最容易接错）

- 源层输入是 `datetime`，用 `int(start.timestamp())`/`int(end.timestamp())` 与库里的 epoch 秒比较（`wechat4_source.py:105-110`）。如果传入 naive datetime，Python 按**运行机器本地时区**解释；如果传入 aware datetime，则按绝对时刻。
- 源层输出 `datetime.fromtimestamp(timestamp)`（`:292`），返回**本机本地时区**的 naive datetime；没有 UTC+08 标记，也没有原始 epoch 字段。
- 源层 SQL 结束条件是 `<=`，即闭区间；如果直接传 `end = 止日次日 00:00+08`，会把恰好在该时刻的消息也包含进来，与当前协议的 `[起日 00:00+08, 止日次日 00:00+08)` 相反。
- 当前原生协议固定 UTC+08、结束日包含（`docs/protocol.md:28`）；`DateRange.Bounds()` 给出 `EndExclusive`（`src/NativeApp/Services/DateRange.cs:20-22`）。适配层必须做“端到端一致”的转换，不能把 C# 的 `EndExclusive` 直接交给源层而不改 SQL/减一秒。
- 源层把大于 10,000,000,000 的 `create_time` 当毫秒除以 1000（`:283-285`）；10 位/13 位边界在真实数据中的分布**需实测**。
- UI 预览用 `time.min`/`time.max` 构造本地 naive 边界（`main_window.py:1254-1257`），源层再按本地时区解释；非 UTC+08 机器上会与产品文案不一致。

建议：真实适配层不要复用 `datetime.fromtimestamp` 的裸输出；在 SQL 层用 epoch 秒查询，把返回的时间统一转为 UTC+08 aware 或 ISO 字符串，并在适配层明确实现结束端排他。

### 4.4 缺失媒体时的行为

| 场景 | 行为 | 证据 |
|---|---|---|
| 图片无 image key | 不尝试解密，`MessageType.SYSTEM` + `[图片]` | `wechat4_source.py:246-254`；`wechat4_media.py:179-181` |
| 图片有 key 但找不到/解密失败 | 同样变成 `SYSTEM` + `[图片]` | `wechat4_media.py:184-190,206-222` |
| 语音无 media 库/无 `VoiceInfo` | `audio_path=None`；内容为 `[语音消息 · N 秒 · 音频暂不可读]` | `wechat4_source.py:258-272`；`voice.py:27-30` |
| 语音有数据 | 写持久 `.silk`，`audio_path` 指向它；内容为转写或 `[语音消息 · N 秒 · 待转写]` | `wechat4_voice.py:62-72`；`voice.py:27-30` |
| type 49 文件/链接 | `MessageType.FILE`，内容只是标签/标题；没有真实文件路径 | `wechat4_source.py:255-257,377-399` |
| 其他系统类型 | `SYSTEM` + 中文标签 | `:25-33,273-278` |

因此“缺失媒体”不会让查询失败，而是静默降级为占位；这对稳定性有利，但会让调用方误以为“有内容”。原生 worker 应在 `kind` 或附加字段中明确区分“占位/不可读”，不要直接把 `FILE` 的标签当作路径导出。

### 4.5 导出语义的额外问题

- `text_exporters.export_markdown` 对 `IMAGE` 生成 Markdown 图片链接（`reference/.../text_exporters.py:22-24`），但微信源的图片路径是临时目录，源 `close()` 后失效；不能把该链接当作长期可用文件。
- 对 `FILE` 生成 ``File attachment: `<content>` ``（`:25-26`），而微信源 `content` 是 `[文件] 标题` 之类标签，不是路径；导出文本会误导。
- 对 `VOICE` 生成 `Voice transcript...`（`:27-29`），即使 `content` 只是“待转写”占位；当前原生 worker 已把非文本演示消息强制转为 system（`worker.py:288-301`），真实源也应采用同样的显式占位策略，而不是把标签伪装成路径/转写。

---

## 5. 安全/隐私边界

### 5.1 密钥捕获机制与风险

机制（代码事实）：

1. `frida` 本地 device attach 到启动中的最大工作集 `Weixin.exe`（`wechat4_key_capture.py:136-155,264-275`）。
2. JS 在 `Weixin.dll` 中扫描 SHA-512 初始常量 `22 ae 28 d7 98 2f 8a 42`，寻找引用该常量的代码，再向前扫描 `0xcc` 前的函数入口（`:23-60`）。
3. `Interceptor.attach` 在该函数入口读取 `this.context.rdx` 指向的 128 字节块；要求 32..127 全为填充字节 `0x36`；前 32 字节 XOR `0x36` 后转为 hex 发送（`:62-88`）。
4. Python 收到 `KEY:<hex>` 后，用目标库第一页做 `verify_account_key`；只有 HMAC 通过才接受（`:164-186`；`wechat4_crypto.py:88-92`）。
5. 超时/登录失败/取消会抛 `SourceError`；Frida 错误通过 `hook_error` 传回（`:166-176,192-206`）。

风险：

- **版本脆弱**：依赖特定 `Weixin.dll` 的常量、指令模式和函数入口；微信升级可能失效。上游只在 `CHANGELOG.md:171` 记录 4.1.2.18 的验证，需按目标版本实测。
- **进程侵入**：注入微信进程、读取寄存器/内存；可能触发反注入、EDR、杀毒或崩溃。上游 README 也提示 Frida/杀毒风险（`reference/.../README.md:82`，上游自述）。
- **语音模型联网**：完整语音转写首次会下载 Whisper 模型到 `%LOCALAPPDATA%/WeChatAIMemory/models`（`voice.py:123-136`），与项目“运行时不联网”的边界冲突；纯文本阶段必须禁用。
- **强制重启**：`taskkill /F /IM Weixin.exe` 会杀掉所有同名进程（`wechat4_key_capture.py:248-261`）；用户未保存的窗口/未读状态可能受影响，必须显式确认。
- **权限**：`OpenProcess`/attach 可能要求管理员；上游图片 key 失败信息直接建议管理员运行（`wechat4_crypto.py:652`）。当前原生 app manifest 没有 `requestedExecutionLevel`（`src/NativeApp/app.manifest:1-11`），默认 asInvoker；若需要提权，不能悄悄提升主程序。
- **自动点击**：`_enter_remembered_account` 选择最大启用按钮并 `invoke/click`（`:290-315`）；登录窗口布局变化可能点错按钮；最多 20 秒，结果不确定。
- **密钥在内存**：raw key 在 Python 进程内；任何同用户进程/调试器/转储都可能读取。不要通过 stdout、stderr、argv、环境变量、日志、崩溃报告传递。
- **失败信息**：Frida 错误描述、DB 路径/账户目录可能出现在异常字符串里；上游 UI 直接显示 `str(exc)`（`main_window.py:101-103,873-881`）。原生 worker 必须映射为安全错误码/固定文案，不能把原始异常透传给 `WorkerClient` 的 `Error` 或 `AppLog`。

### 5.2 日志可能泄漏什么

| 来源 | 可能泄漏 | 现有控制 |
|---|---|---|
| 上游 UI `SourceLoadWorker.failed.emit(str(exc))` | 账户目录、数据库路径、会话名/ID、WAL 文件名、Frida 错误 | 无脱敏；只显示在 UI |
| `decrypt_database` 的 `SourceError(f"... {source}")` | 绝对路径/文件名 | 异常文本直接构造（`wechat4_crypto.py:113,122,143`） |
| `select_wechat4_account`/discovery 错误 | 账户根/目录 | `wechat4_discovery.py:71,75,80` |
| 原生 `WorkerError.message` | 协议 `error.message` 会显示在 UI（`src/NativeApp/ViewModels/MainViewModel.cs:156-158`）；`AppLog` 只记 code | 真实 worker 必须给固定安全文案，不能带路径/会话名/正文 |
| 原生 `AppLog` | 只允许固定事件名/错误码，不记录路径/异常文本/正文 | `src/NativeApp/Services/AppLog.cs:9-24` |
| 原生 `WorkerClient` stderr | 第三方/Frida 诊断可能含数据 | 只计数，不转发/不记录（`WorkerClient.cs:137-145`） |
| 协议 stdout | 任何非 JSON 行会破坏协议 | `WorkerClient.cs:110-116` 判 `PROTOCOL_ERROR`；worker 必须隔离原生 stdout |

建议：

- 真实源的所有错误在 worker 内转换为**固定错误码 + 固定中文安全文案**；详细异常只写 stderr（且 stderr 由宿主丢弃/计数），不写 stdout，不写 Windows 事件日志，不写 AppLog 文本。
- 进度事件也不要带账户目录、会话名、消息内容；只带阶段/计数/百分比。
- 若必须让用户选择账户，UI 显示账户目录最后一段（可能含 wxid）本身也是元数据；是否显示由产品决定，但不要写日志。

### 5.3 失败模式

| 失败 | 当前表现 | 接入时应处理 |
|---|---|---|
| 未安装/找不到微信 | `SourceError` | 提示安装/选择路径，不暴露搜索细节 |
| 没有 `message_*.db` | `SourceError` | 提示先登录微信产生数据 |
| 微信无法关闭 | `SourceError`（10 秒） | 提示用户手动退出；不要无限等待 |
| attach 失败 | `SourceError`（300 秒循环） | 区分权限/版本/进程保护；超时可取消 |
| hook 未找到 | `SourceError`（15 秒） | 提示版本不兼容/需适配，不能重试死循环 |
| 登录未完成 | `SourceError`（300 秒） | 需要可取消、可重试；不要留下半启动微信 |
| pywinauto 缺失/UI 变化 | `SourceError` 或自动点击失败 | 提供手动扫码路径；不要让依赖错误伪装成登录失败 |
| 第一个 message 分片不足一页 | 密钥验证永远失败，最终超时 | 选择大于一页的分片；或先过滤目标 |
| DB 反复变化 | `SourceError`（5 次重试） | 提示关闭微信/稍后重试；不要返回部分不一致数据 |
| WAL 不支持/损坏 | `SourceError` | 提示版本/损坏；不要回退到解密一半的库 |
| schema 缺列 | `SourceError` | 记录版本；UI 提示不兼容，不猜测列 |
| 多账户 | `SourceError`（`xwechat_files` 多匹配） | 让用户选择；不要默认合并 |
| 图片 key 找不到/无权限 | `SourceError` 或 `None` | 图片是可选能力；不要阻塞文本 |
| 语音数据损坏 | `None`/占位 | 保持占位，不报错；不要删除原始 `.silk` |
| worker 超时/被关闭 | 进程被杀，临时明文可能残留 | 启动清理/专用临时根/优雅停止 |

### 5.4 原生 worker 隔离建议

必须遵守的边界：

1. **密钥只存在于 worker 内存**：不通过协议返回给 C#，不写配置/日志/argv/env；C# 只发“连接微信/查询”命令。
2. **stdout 只允许协议 JSON**：在 worker 入口重定向 fd 1（或让所有 Frida/原生输出到 stderr/空设备），再加 Python 层 `sys.stdout = sys.stderr` 保护；当前 `worker.py:449` 只有后者。
3. **错误脱敏**：`SourceError` 不直接透出；映射为 `WECHAT_*` 固定码 + 固定文案；详细异常只到 stderr，宿主不记录文本。
4. **临时明文最小化**：使用应用控制的私有临时根（而非全局 `%TEMP%`），启动时清理旧会话；`close()`/正常退出清理；超时/关闭时先请求取消，再在有限时间后杀进程。强制终止后的残留需要下次启动清理。
5. **语音/图片默认关闭**：纯文本阶段不要构造 `WeChatVoiceCache`/`DecryptedImageCache`，或至少在消息映射里跳过 type 34/3 的媒体解析；否则会产生持久 `.silk` 和临时图片。
6. **用户确认**：连接前明确“将重启微信、需要登录、只读本工具不会写回原库”，并说明临时明文/日志策略；不要把“只读”宣传成“零副作用”。
7. **账户范围**：只扫描上游定义的 roots，或让用户显式选择；不要按进程名/磁盘全盘搜索。
8. **最小权限**：尽量 asInvoker；若 Frida 必须提权，单独设计受控子进程/UAC 提示，不要让整个 WinUI 应用常驻管理员。
9. **进程所有权**：只杀自己启动的 worker；不要按 `Weixin.exe`/`python.exe` 名称清理他人进程。现有 `WorkerClient` 已只回收自己的进程树（`WorkerClient.cs:160-164,173-196`），这一原则要保留。
10. **审计/通知**：如果产品保留日志，只记录固定事件（连接开始/成功/失败码），绝不记录账户、路径、会话名、消息、密钥。

---

## 6. 最小接入面

### 6.1 推荐方案：文本只读适配层，不直接用 `WeChat4LocalSource`

目标：账户发现 + 会话列表 + 按会话消息 + 日期/关键词搜索；不支持图片/文件内容/语音/PDF/Qt。

建议复用的上游文件：

| 文件 | 复用方式 | 原因 |
|---|---|---|
| `sources/base.py` | 直接复制/引用 `SourceError` 与 safe 文件助手 | 无第三方依赖，统一错误/容错 |
| `sources/wechat4_discovery.py` | 直接复用 | 账户根、账户布局、`select` 语义明确 |
| `sources/wechat4_crypto.py` | 复用 `DecryptedDatabaseCache`、`derive_*`、`verify_*`、`decrypt_database`；可选 `extract_*` | 只依赖 pycryptodome + 标准库；是解密核心 |
| `sources/wechat4_key_capture.py` | 复用 `capture_account_key` 的 hook/验证骨架；建议改造成“可选手动登录、可取消、选有效分片” | 只依赖 frida（和可选 pywinauto） |
| `sources/wechat4_source.py` | **不直接实例化**；参考其 `_load_contacts`、`_load_conversations`、`_index_message_tables`、`get_messages` SQL 和 `_message_from_row` 映射，写一个 text-only 版本 | 原版强制构造图片/语音缓存，且无分页/关键词/UTC+08 语义 |
| `models.py` | 本项目已 vendored；复用 `Conversation`/`Message` | 无第三方依赖 |
| `text_exporters.py` | 本项目已 vendored；导出时把非文本降级为 system | 避免虚假链接 |

不建议引入：

- `sources/wechat4_media.py`、`sources/wechat4_voice.py`、`voice.py`：纯文本阶段不需要；会带来临时图片、持久 `.silk`、faster-whisper/pysilk 依赖。
- `service.py`、`cli.py`、`ui/`、`pdf_exporter.py`、`rendering/`：Qt/PDF/任务编排与 worker 无关。
- 上游包级 `__init__.py`：会导入 `service`；使用本项目的最小 vendored init。

文本适配层应实现的接口（内部 API 可自定义，不必照搬上游）：

```text
list_accounts() -> list[AccountInfo]       # 不读内容；目录最后一段可做显示名，但不要写日志
connect_account(account_path) -> None      # 捕获/接收 key；初始化数据库缓存
list_conversations() -> list[Conversation] # id/name/kind；无 messageCount 或单独 count
get_messages(conversation_id, start_epoch, end_epoch_exclusive,
             offset, limit, query) -> Page
close()                                    # 清理临时目录；尽力删除
```

实现要点：

1. **账户**：复用 `discover_wechat4_accounts`/`select_wechat4_account`；把账户 id 仅作为内部标识，不把完整路径发给 C#（若必须在 UI 显示，显示脱敏后的末段）。
2. **密钥**：调用 `capture_account_key` 或它的改造版；密钥保存在 worker 内；`progress` 只发阶段/计数；`cancelled` 只在等待登录期间检查（当前实现如此，`wechat4_key_capture.py:200-202`），attach 循环不检查取消（`:142-155`），改造时应补上。
3. **解密**：用 `DecryptedDatabaseCache`，但把 `tempfile.TemporaryDirectory` 换成应用私有根，支持启动清理和强制终止后的残留清理；只 `get()` 需要的 session/contact/message 分片，不要一次性解密全部。
4. **会话**：复制 `_load_contacts`/`_load_conversations` 逻辑；`SessionTable` 过滤无消息表的会话；显示名优先级照旧；不要假设存在 `messageCount`，如需计数用 `SELECT COUNT(*)` 并注意成本。
5. **消息 SQL**：复用 `PRAGMA table_info`、`Name2Id` join、`server_id`/`WCDB_CT_message_content`/`packed_info_data` 可选列逻辑；把 `WHERE create_time >= ? AND create_time < ?` 改成 end-exclusive；将 `start_epoch`/`end_epoch_exclusive` 从 UTC+08 转换而来。
6. **关键词**：优先在 SQL 中做 `message_content LIKE ?`（注意压缩内容在 SQL 里无法匹配；需先解压或只对文本列做应用层过滤）；若保持简单，先按日期取回，再在 worker 内存做 `casefold` 子串，但必须有上限，避免大会话全量加载。不要声称“全库全文检索”。
7. **分页**：在 worker 对排序后的结果切片；返回 `total`/`nextOffset` 与协议一致；若要节省内存，可在 SQL 层用 `LIMIT/OFFSET`，但多分片合并时需要归并排序。
8. **类型**：只映射 text 与明确 system；图片/语音/文件/未知类型一律 `SYSTEM` + 占位，不生成临时路径、不写 `.silk`。群前缀剥离要保守（最好只对群聊或只在 `sender_wxid` 为空时使用）。
9. **时间**：所有对外时间统一为 UTC+08 aware 的 ISO 或 epoch；不要直接返回 `datetime.fromtimestamp` 的 naive 本地时间。
10. **清理**：`close()` 必须关闭所有 SQLite 连接、图片/语音（如果启用）、删除私有临时根；正常 `shutdown` 前调用；超时路径由 C# 先发取消/关闭，而不是直接 kill。

### 6.2 依赖与模块清单

最小运行时：

- Python 3.11（当前环境；3.13 需实测）
- `frida` 17.x
- `pycryptodome` 3.x
- `zstandard` 0.x
- `sqlite3`（标准库）
- 可选：`pywinauto` + comtypes（仅在保留自动登录时；否则去掉）
- 不需要：PySide6、Pillow、reportlab、imageio-ffmpeg、faster-whisper、silk-python、pypdf

构建/发布：

- `requirements-build.txt` 继续只放 PyInstaller；新增 `requirements.txt`（frida/pycryptodome/zstandard，可选 pywinauto）供 `Prepare-Python.ps1` 安装。
- `Publish.ps1` 需要把运行时依赖打进 worker；建议改 onedir 或至少加 frida hiddenimports/data 的显式配置，并在干净 Windows 上跑 smoke test。
- `THIRD_PARTY_NOTICES.md`/`LICENSES/` 增加 frida 及其许可、上游微信兼容代码的第三方声明（参考上游 `THIRD_PARTY_NOTICES.md` 的 MIT/其他许可条目；具体法律判断不在本评估范围）。

### 6.3 协议/客户端改造面

当前协议（`docs/protocol.md`）不足以表达真实源：

| 需要 | 现状 | 建议 |
|---|---|---|
| 选择来源 | `source:"demo"` 默认，其他报错（`:17`；`worker.py:207-211`） | 新增 `source:"wechat4"`；`hello.capabilities` 增加能力标识 |
| 账户发现 | 无 | 新增 `list_accounts`（只返回安全显示信息），或 `connect` 时发现并让用户选 |
| 连接/登录 | 无 | 新增 `connect`/`disconnect`，带进度；不要把 key 放 payload |
| 关键词 | 无 | `get_messages` 增加 `query`；`export_markdown` 增加 `query`（与 `ExportOptions.query` 对应，`service.py:21-31`） |
| 分页 | 有 offset/limit，但服务端先全量加载 | 保留字段，worker 内部实现；返回 `total`/`nextOffset` |
| 时间 | UTC+08 + 结束日包含（`protocol.md:28`） | 明确 `[start,end)`；内部转 epoch；输出带时区 |
| 消息数 | 会话项要求 `messageCount`（`:22`） | 真实源可返回 0/null 或增加 `count_known`；不要让 UI 误显示 |
| 超时 | 客户端默认 30 秒（`WorkerClient.cs:78`） | connect/索引/解密单独长超时（如 300 秒以上）或后台任务 + progress；不要用默认 30 秒 |
| 取消/关闭 | 有 pending 就杀（`:183`） | 支持 `cancel`/`shutdown` 先清理；杀进程作为最后手段 |
| 错误码 | 现有固定集合（`protocol.md:35`） | 增加 `WECHAT_*` 安全码；UI 映射固定文案 |
| 进度 | v1 客户端识别并忽略 `type:progress`（`protocol.md:13`；`Protocol.cs:28`） | 可用于登录/索引进度；确保 stdout 纯 JSON |

`WorkerLocator` 目前总是加 `--demo`（`WorkerLocator.cs:14-15,26`）。真实模式不要依赖这个参数：可以让 worker 始终启动，由命令选择源；或新增显式的 `--mode`，但不要把账户目录/密钥放 argv。`WorkerLocator` 的“只查应用目录/项目标记”边界应保留（`:18-22`），账户扫描发生在 worker 内而不是 locator。

### 6.4 能否不引入 Qt/PDF/语音

可以，前提是接受以下限制：

- 不构造 `WeChat4LocalSource`，或改造它跳过图片/语音；
- 不 import `service.py`（避免 PIL/reportlab）；
- 不 import `wechat4_media`/`wechat4_voice`/`voice.py`（避免临时图片、持久 `.silk`、faster-whisper/pysilk）；
- 导出只用本项目 vendored 的 `text_exporters.export_markdown`，并把 image/file/voice 转成 system 占位（类似 `worker.py:288-301`）；
- 不提供图片预览、语音转写、PDF。

在这个范围内，worker 仍需要 `frida`/`pycryptodome`/`zstandard`，并需要一次微信重启/登录。**“不需要 Qt/PDF/语音”不等于“不需要 Frida/密钥/进程重启”**。

### 6.5 建议的落地顺序

1. 先做无微信依赖的 text-only 数据模型/查询层，用合成明文夹具测试；确认协议的时间/分页/关键词语义。
2. 接入 `wechat4_discovery` + `wechat4_crypto`，用合成加密夹具测试快照/解密/WAL/只读。
3. 接入 `capture_account_key` 的验证逻辑，但先做 mock；再做真实微信、专用测试账户、显式确认的 spike。
4. 增加私有临时根/清理/取消；改造协议超时；做 C# 端错误码/UI 文案。
5. 最后才评估图片/语音/PDF；默认不做。

---

## 7. 合成夹具测试方案与真实微信验证项

### 7.1 合成夹具的目标

不读取任何真实微信数据，用临时目录构造与上游测试同构的账户，覆盖：

- 发现：账户根、`db_storage`、`xwechat_files`、多账户、`all_users`、不可访问目录；
- 解密：第一页 HMAC、错误 key、页对齐、WAL commit、快照复制；
- 会话：SessionTable/contact/Name2Id 映射、群/单聊、无消息表会话；
- 消息：多分片、zstd、类型映射、日期边界、排序、缺失媒体；
- 安全：原库 hash 不变、临时目录只在受控根、密钥不出现在文件/协议/日志、stderr 不含正文。

上游已有可参考的测试：

- `reference/wechat-ai-memory/tests/test_wechat4_discovery.py:8-70`：不可访问根、盘符扫描、不可访问账户目录。
- `reference/wechat-ai-memory/tests/test_wechat4_crypto.py:26-114`：图片 key 推导、第一页加密/验证/解密、DB+WAL 快照、WAL 只保留最后 commit。
- `reference/wechat-ai-memory/tests/test_wechat4_source.py:18-180`：合成 session/contact/message_0/message_1/media_0、zstd、V2 图片、语音；但该测试导入 `ExportService`/pypdf，复制到本项目时要拆掉 PDF 依赖。
- 注意：上游测试用 `monkeypatch` 把 `capture_account_key` 和 `DecryptedDatabaseCache` 替换成明文/假缓存（`test_wechat4_source.py:155-156`），这正是单元测试应采用的隔离方式。

### 7.2 建议的夹具布局

使用测试临时目录，不写入项目源码树：

```text
<tmp>/xwechat_files/<fake_account>/
  db_storage/
    session/session.db
    contact/contact.db
    message/message_0.db
    message/message_1.db
    message/media_0.db
  msg/attach/
```

所有账户名、wxid、会话名、消息内容使用测试生成的虚构值（例如 `test_account`、`test_peer`），不复用任何真实数据。`Message` 内容用测试标记字符串，不使用真实聊天。

### 7.3 测试分层

| 层 | 测试内容 | 是否需要真实微信 | 说明 |
|---|---|---|---|
| 单元：发现 | `discover`/`select` 的根、唯一/多账户、`all_users`、异常路径 | 否 | 构造目录 + `session.db` 占位 |
| 单元：crypto | `derive_*`、`verify_*`、`decrypt_page`、`decrypt_database`、WAL commit | 否 | 用 pycryptodome 生成夹具；可参考上游 crypto 测试 |
| 单元：source SQL | 合成明文 DB + 假 cache，验证会话/消息/类型/分片/排序 | 否 | 不依赖 Frida；直接 monkeypatch 密钥/缓存 |
| 单元：时间 | start/end 边界、UTC+08 转 epoch、end-exclusive、毫秒秒转换 | 否 | 固定时区；断言边界消息 |
| 单元：关键词/分页 | 内存过滤、大小写、类型匹配、offset/limit/total | 否 | 明确“只搜当前会话/当前结果” |
| 集成：worker | 通过 stdin/stdout 发 `list_conversations`/`get_messages`；断言 stdout 纯 JSON、无 key/正文 stderr | 否 | 用假源或合成明文源；不启动 Frida |
| 集成：C# | 假 worker 超时/退出/协议错；真实协议字段 | 否 | 现有 `tests/NativeApp.Tests` 模式 |
| 隐私 | 原库 hash 前后一致；临时根可删除；关闭后无 `.silk`/key 文件；日志无路径/正文 | 否（合成） | 进程 kill 后的残留单独测 |
| 真实微信 | 见 7.5 | **是** | 专用测试账户/VM，人工确认 |

### 7.4 关键断言

- 账户发现：只命中含 `db_storage/session/session.db` 的目录；`all_users` 不出现；多账户时 `select(xwechat_files)` 抛 `SourceError`；显式账户路径可选中。
- 密钥/解密：错误 key 的 `verify_account_key` 为 `False`；正确 key 解密第一页得到 SQLite 头；非页对齐/短页抛 `SourceError`；WAL 只应用到最后 commit。
- 只读：原始 `.db` 和 `-wal` 的 SHA-256/大小/mtime 在读取前后不变（注意 atime 不参与）；所有解密文件位于受控临时根；`close()` 后临时根不存在。
- 会话：无消息表的 SessionTable 行被排除；显示名优先级 remark/nick/alias/username；群/单聊 kind 正确；没有 `messageCount` 时协议字段不崩溃。
- 消息：`local_type & 0xFFFF`；zstd 标志 4 和 magic 都能解；空内容变 system；未知类型 system；群前缀剥离不误伤普通文本（至少测试“英文词: 正文”）；type 49 不生成文件路径；图片无 key 变 `[图片]`；语音无数据变“音频暂不可读”。
- 时间：`start`/`end` 为 UTC+08 日期时，边界消息只出现一次；结束日次日 00:00+08 的消息不出现；返回 timestamp 带 UTC+08 或明确 epoch；毫秒 `create_time` 归一化正确。
- 关键词/分页：大小写不敏感；只匹配 sender/content/type；`offset`/`limit`/`total`/`nextOffset` 正确；空结果稳定。
- 安全：测试读取并断言 stdout 不含 `KEY:`/hex/路径/正文；stderr 有诊断但宿主不记录；配置/临时目录没有 key 文件；协议 payload 不含 raw key。

### 7.5 必须用真实微信才能验证的项（需实测）

| 项目 | 为什么合成夹具不够 | 建议验证 |
|---|---|---|
| `Weixin.dll` SHA-512 常量/函数入口/`rdx` 布局 | 版本、编译、保护都可能变化 | 在目标微信版本（上游记录 4.1.2.18，`CHANGELOG:171`）上实际 attach；记录成功/失败版本 |
| Frida 权限/AV/反注入 | 管理员、EDR、杀毒、进程保护 | 普通用户/管理员各一次；干净机器；观察崩溃/拦截 |
| 登录自动点击 | 真实窗口/UIA 结构/DPI | 手动扫码与自动点击各测；确认不会点错 |
| 账户根/真实目录布局 | 实际安装可能在不同盘/OneDrive/企业目录 | 多账户、非 C 盘、中文/空格路径、云盘/网络盘 |
| 真实 schema | 版本差异、可选列、WAL | 检查 `PRAGMA table_info`、`Name2Id`、`SessionTable`、`VoiceInfo`；记录版本差异 |
| 真实 `create_time` 单位/时区 | 10/13 位、跨时区、夏令时 | 选取已知时间消息，核对 UTC+08 边界 |
| 真实 message 分片/media 分片 | 分片编号、消息分布、并发写 | 多分片账户；确认合并排序/去重/语音映射 |
| 读取期间的并发写 | 快照重试/锁/性能 | 微信活跃聊天时读取；观察 `changed repeatedly`、耗时 |
| 图片解密 | V2/WXGF/版本 | 有 image key/无 key 各一次；确认临时文件清理 |
| 语音 `.silk`/转写 | 真实 VoiceInfo/编码 | 确认是否产生持久文件；确认隐私策略 |
| 启动/登录/300 秒超时 | 真实网络/手机确认 | 断网、扫码超时、取消、重复连接 |
| PyInstaller onefile/onedir | 冻结后的 frida/pywinauto/zstandard | 干净 Windows、无 Python、杀毒开启；启动时间/体积/导入 |
| Python 3.13 | 本机仅 3.11 验证 | 3.13 venv 构建 + 运行；或明确只支持 3.11 |
| 许可证/分发 | frida 许可/第三方微信代码 | 法务/合规复核；更新 notices |

真实测试必须使用专用测试账户/虚拟机，避免读取个人主账户；测试记录只保留版本、错误码、通过/失败，不保留聊天内容或密钥。

---

## 8. 未解决问题与风险

### 8.1 需要实测的问题

1. **目标微信版本**：上游明确记录的真实解密验证版本是 4.1.2.18（`CHANGELOG.md:171`）；另有 4.1.x `message_1.db` 编号兼容说明（`CHANGELOG.md:76`），但不等于全版本验证。当前安装版本、`Weixin.dll` 偏移、hook 是否仍有效，需实测。
2. **管理员权限**：Frida attach/OpenProcess 是否在 asInvoker 下可用；当前 manifest 无提权（`app.manifest:1-11`），需实测普通用户/管理员两种。
3. **登录自动化**：`pywinauto` 的 UIA 按钮选择在真实登录窗口/DPI/多屏下是否可靠；是否需要保留；需实测。
4. **快照与并发写**：微信运行中复制大库是否频繁触发 `changed repeatedly`（`wechat4_crypto.py:214-230`）；是否需要关闭微信后读取；需实测。
5. **真实 schema**：`server_id`、`WCDB_CT_message_content`、`packed_info_data`、`local_type` 高位子类型是否与 4.x 当前版本一致；需实测。
6. **时间单位/时区**：`create_time` 秒/毫秒分布、跨时区/夏令时、非 UTC+08 机器上的显示；需实测。
7. **消息分片/媒体分片**：编号是否从 0 或 1 开始、`media_N.db` 是否总在 message 目录、跨分片 ID 是否唯一；需实测。
8. **密钥目标分片**：`capture_account_key` 选择第一个 `message_*.db` 且不检查大小（`:119-129`）；真实目录是否总有 >4096 字节的第一个分片，需实测；否则会假超时。
9. **Frida 在 PyInstaller 中**：`_frida.pyd` 161.6 MiB 是否被 onefile 正确收集/加载；是否被 AV 拦截；启动/解压耗时；需实测。
10. **pywinauto/comtypes 在冻结环境**：运行期生成模块/缓存是否正常；需实测。
11. **原生 stdout**：Frida/Windows 库是否写 stdout；当前 `worker.py:449` 只保护 Python `sys.stdout`；需在真实 worker 中验证 fd 级隔离。
12. **临时明文残留**：worker 被 `WorkerClient` 杀死后，`%TEMP%\wce-wechat4-*`/`wce-images-*` 是否残留；需在真实/合成环境实测，并设计启动清理。
13. **语音持久化**：完整源是否在用户目录产生 `.silk`/转写 JSON；纯文本适配层是否真的绕过（跳过 type 34）；需实测。
14. **图片/FFmpeg**：WXGF 是否需要 `imageio-ffmpeg`；图片 key 内存扫描是否需要管理员；非文本阶段可暂不验证。
15. **许可证**：frida 的 wxWindows 许可、上游 `THIRD_PARTY_NOTICES.md` 提到的第三方微信兼容代码，分发前需合规复核；本评估不是法律意见。
16. **大账户初始化解密耗时**：`_index_message_tables` 会对每个 message 分片调用 `get()`（`wechat4_source.py:184-190`），每库 256000 次 PBKDF2 + 整库复制/解密（`wechat4_crypto.py:73,108-130`）；真实大账户的启动时间/内存/磁盘占用需实测，可能远超当前 30 秒客户端超时。

### 8.2 设计与产品风险

| 风险 | 后果 | 缓解 |
|---|---|---|
| 把完整 `WeChat4LocalSource` 直接塞进 worker | 自动重启微信、写 `.silk`、无分页/关键词、时间错位 | 用 text-only 适配层；显式关闭媒体 |
| 30 秒默认超时 | 登录/解密被误判失败并杀进程 | 专用长超时/后台进度；不要复用默认 |
| 超时/关闭直接 kill | 临时明文残留、数据库连接未关 | 先取消/优雅关闭；启动清理残留 |
| 错误消息透传 | 路径/会话名进 UI/日志 | 固定错误码 + 安全文案；详细只 stderr |
| 全量消息加载 | 大账户内存/CPU/延迟 | SQL 日期过滤 + 分页；关键词策略明确 |
| 初始化解密所有 message 分片 | 每个分片都走 256000 次 PBKDF2 + 整库复制/解密（`wechat4_source.py:184-190`；`wechat4_crypto.py:73,108-130`），大账户启动慢，且 30 秒默认超时会直接失败 | 先按需/延迟解密分片，或在索引阶段只读 `Name2Id`；用真实大账户实测耗时 |
| 群前缀剥离 | 正常文本被截断 | 仅群聊/未知发送者时剥离；测试误伤 |
| type 49 标签当路径 | 导出虚假附件 | 映射 system 占位；不生成链接 |
| 图片临时路径导出 | Markdown 链接失效 | 不导出图片或复制到导出目录并说明 |
| 多账户默认选最新 | 用户看错账户 | 显式选择/确认；不要静默合并 |
| 反注入/反调试/风控 | 微信崩溃/封号风险（需实测/合规评估） | 专用测试账户、用户确认、版本白名单 |
| 只读宣传过度 | 用户误以为零副作用 | 明确“本工具不写原库；微信自身会写；临时明文/日志策略” |

### 8.3 结论性判断

- **技术可行性：有**。上游已经证明“Frida 捕获 key + 复制/解密 + SQLite 查询”这条链路在 Windows 微信 4.x 上可行（上游自述 4.1.2.18；`CHANGELOG.md:171`）。
- **直接复用：不建议**。完整 `WeChat4LocalSource` 的进程副作用、语音持久化、无分页/关键词、时间语义与当前 worker 契约不匹配。
- **最小可交付：文本只读适配层 + 新协议命令 + 私有临时根 + 严格脱敏**。先只支持账户发现、会话列表、按会话日期/关键词查询和 Markdown 文本导出；图片/语音/PDF 后置。
- **最大风险：Frida 密钥捕获与临时明文生命周期**。前者决定能否连接、是否被杀毒/权限阻断；后者决定“只读”是否会留下明文数据库/图片残留。两者都必须先做真实环境 spike，再谈产品化。

---

## 附录：本次核验读取的主要文件

上游：

- `reference/wechat-ai-memory/pyproject.toml`
- `reference/wechat-ai-memory/src/wechat_context_exporter/sources/base.py`
- `.../sources/json_source.py`
- `.../sources/wechat4_crypto.py`
- `.../sources/wechat4_discovery.py`
- `.../sources/wechat4_key_capture.py`
- `.../sources/wechat4_media.py`
- `.../sources/wechat4_source.py`
- `.../sources/wechat4_voice.py`
- `.../models.py`
- `.../service.py`
- `.../cli.py`
- `.../voice.py`
- `.../text_exporters.py`
- `.../ui/main_window.py`
- `.../ui/dashboard_window.py`
- `.../tests/test_wechat4_source.py`
- `.../tests/test_wechat4_crypto.py`
- `.../tests/test_wechat4_discovery.py`
- `.../packaging/windows/wechat_ai_memory.spec`
- `.../CHANGELOG.md`、`.../README.md`、`.../THIRD_PARTY_NOTICES.md`

本项目：

- `src/PythonWorker/worker.py`
- `src/PythonWorker/requirements-build.txt`
- `src/PythonWorker/vendor/wechat_context_exporter/__init__.py`
- `docs/protocol.md`
- `docs/source-assessment.md`
- `src/NativeApp/Services/WorkerLocator.cs`
- `src/NativeApp/Services/WorkerClient.cs`
- `src/NativeApp/Services/Protocol.cs`
- `src/NativeApp/Services/DateRange.cs`
- `src/NativeApp/Services/AppLog.cs`
- `src/NativeApp/ViewModels/MainViewModel.cs`
- `src/NativeApp/app.manifest`
- `scripts/Publish.ps1`
- `scripts/Prepare-Python.ps1`
